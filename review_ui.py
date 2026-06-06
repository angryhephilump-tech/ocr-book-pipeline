#!/usr/bin/env python3
"""
Archive Studios — web UI for the OCR book pipeline.
"""

from __future__ import annotations

from pipeline.paddle_env import configure_paddle_env

configure_paddle_env()

import argparse
import json
import os
import re
import shutil
from pathlib import Path

import cv2
from pypdf import PdfReader, PdfWriter
from werkzeug.utils import secure_filename

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

import license
from pipeline.export import export_pdf, export_plain_text, load_pages_for_export
from pipeline.paths import configure_runtime, resource_root
from ocr_book import load_project_languages, save_project_languages
from pipeline.lang_catalog import effective_project_settings, language_name
from pipeline.tessdata_manager import ensure_project_languages, languages_for_api
from pipeline.web_jobs import get_job_status, is_running, reset_job, start_job

configure_runtime()

ROOT = resource_root()
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".pdf", ".jfif", ".heic", ".heif"}


def create_app(output_dir: Path, photos_dir: Path) -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    app.config["OUTPUT_DIR"] = output_dir
    app.config["PHOTOS_DIR"] = photos_dir
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    output_dir.mkdir(parents=True, exist_ok=True)
    photos_dir.mkdir(parents=True, exist_ok=True)

    @app.before_request
    def require_license():
        if not license.license_required() or license.is_activated():
            return None
        allowed = {"/activate", "/api/activate", "/static"}
        if request.path.startswith("/static/"):
            return None
        if request.path in ("/activate", "/api/activate"):
            return None
        if request.path.startswith("/api/activate"):
            return None
        return redirect(url_for("activate_page"))

    @app.route("/activate")
    def activate_page():
        if license.is_activated():
            return redirect(url_for("index"))
        return render_template("activate.html")

    @app.route("/api/activate", methods=["POST"])
    def api_activate():
        data = request.get_json(silent=True) or {}
        key = (data.get("license_key") or "").strip()
        ok, message = license.verify_license_key(key)
        if ok:
            credits = license.credits_status()
            return jsonify({"ok": True, "message": message, "credits": credits})
        return jsonify({"ok": False, "error": message}), 400

    state_path = output_dir / "review_state.json"

    def has_manifest() -> bool:
        return (output_dir / "manifest.json").exists()

    def load_state() -> dict:
        if state_path.exists():
            return json.loads(state_path.read_text(encoding="utf-8"))
        return {"resolved_flags": {}, "page_texts": {}}

    def save_state(state: dict) -> None:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_manifest() -> dict:
        mp = output_dir / "manifest.json"
        if not mp.exists():
            return {"pages": []}
        return json.loads(mp.read_text(encoding="utf-8"))

    def buy_more_url() -> str:
        cfg = ROOT / "config" / "gateway.json"
        if cfg.is_file():
            try:
                return str(json.loads(cfg.read_text(encoding="utf-8")).get("buy_more_credits_url") or "https://gumroad.com/")
            except json.JSONDecodeError:
                return "https://gumroad.com/"
        return "https://gumroad.com/"

    def flagged_page_ids() -> list[str]:
        manifest = load_manifest()
        return [p["page_id"] for p in manifest.get("pages", []) if p.get("needs_review")]

    def list_uploads() -> list[dict]:
        items = []
        for path in sorted(photos_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
                items.append({"name": path.name, "size": path.stat().st_size})
        return items

    @app.route("/")
    def index():
        if license.license_required() and not license.is_activated():
            return redirect(url_for("activate_page"))
        # Always show upload home unless user explicitly goes to /review
        return render_template("home.html")

    @app.route("/review")
    def review():
        if not has_manifest():
            return render_template("home.html"), 404
        return render_template("review.html")

    @app.route("/api/languages")
    def api_languages():
        return jsonify({"languages": languages_for_api()})

    @app.route("/api/project-languages", methods=["GET", "POST"])
    def api_project_languages():
        if request.method == "GET":
            saved = load_project_languages(output_dir) or {}
            return jsonify({"languages": effective_project_settings(saved)})

        if is_running():
            return jsonify({"error": "OCR is running."}), 409

        data = request.get_json(silent=True) or {}
        cfg = {
            "primary_language": (data.get("primary_language") or "spa").strip(),
            "secondary_language": (data.get("secondary_language") or "").strip() or None,
            "indigenous_minority_mode": bool(data.get("indigenous_minority_mode", False)),
        }
        settings = effective_project_settings(cfg)
        status = ensure_project_languages(
            settings["primary_language"],
            settings.get("secondary_language"),
        )
        failed = {k: v for k, v in status.items() if str(v).startswith("error")}
        if failed:
            return jsonify({"error": f"Language pack install failed: {failed}"}), 400
        save_project_languages(output_dir, settings)
        return jsonify(
            {
                "ok": True,
                "languages": settings,
                "primary_name": language_name(settings["primary_language"]),
                "secondary_name": language_name(settings["secondary_language"])
                if settings.get("secondary_language")
                else None,
                "tessdata_status": status,
            }
        )

    @app.route("/api/status")
    def api_status():
        uploads = list_uploads()
        manifest = load_manifest() if has_manifest() else {}
        lang_saved = load_project_languages(output_dir)
        credits = license.credits_status()
        return jsonify(
            {
                "has_manifest": has_manifest(),
                "upload_count": len(uploads),
                "uploads": uploads,
                "job": get_job_status(),
                "book_title": manifest.get("book_title"),
                "total_pages": manifest.get("total_pages", 0),
                "flagged_pages": manifest.get("flagged_pages", 0),
                "languages": effective_project_settings(lang_saved or {}),
                "credits": credits,
                "buy_more_url": buy_more_url(),
            }
        )

    @app.route("/api/credits")
    def api_credits():
        return jsonify(license.credits_status())

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        if is_running():
            return jsonify({"error": "OCR is running — please wait."}), 409

        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files received."}), 400

        saved = []
        for file in files:
            if not file.filename:
                continue
            ext = Path(file.filename).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                continue
            safe = secure_filename(Path(file.filename).name)
            if not safe:
                safe = f"upload{ext}"
            dest = photos_dir / safe
            stem, suffix = dest.stem, dest.suffix
            counter = 1
            while dest.exists():
                dest = photos_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            file.save(dest)
            saved.append(dest.name)

        if not saved:
            return jsonify({"error": "No valid images or PDFs uploaded."}), 400

        return jsonify({"ok": True, "saved": saved, "upload_count": len(list_uploads())})

    @app.route("/api/clear-uploads", methods=["POST"])
    def api_clear_uploads():
        if is_running():
            return jsonify({"error": "OCR is running."}), 409
        removed = 0
        for path in photos_dir.iterdir():
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
                path.unlink()
                removed += 1
        return jsonify({"ok": True, "removed": removed})

    @app.route("/api/run-ocr", methods=["POST"])
    def api_run_ocr():
        if is_running():
            return jsonify({"error": "OCR already running."}), 409
        if not list_uploads():
            return jsonify({"error": "Upload at least one page first."}), 400

        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "Untitled Book").strip() or "Untitled Book"
        page_start = data.get("page_start")
        page_end = data.get("page_end")
        try:
            page_start = int(page_start) if page_start not in (None, "", 0) else None
            page_end = int(page_end) if page_end not in (None, "", 0) else None
        except (TypeError, ValueError):
            return jsonify({"error": "Page range must be numeric."}), 400
        lang_cfg = {
            "primary_language": (data.get("primary_language") or "spa").strip(),
            "secondary_language": (data.get("secondary_language") or "").strip() or None,
            "indigenous_minority_mode": bool(data.get("indigenous_minority_mode", False)),
        }
        credits = license.credits_status()
        if int(credits.get("remaining_credits", 0)) <= 0:
            return jsonify({"error": "No credits remaining."}), 402
        language_config = effective_project_settings(lang_cfg)
        save_project_languages(output_dir, language_config)
        reset_job()
        started = start_job(
            photos_dir,
            output_dir,
            title=title,
            language_config=language_config,
            license_key=(license.load_activation() or {}).get("license_key"),
            page_start=page_start,
            page_end=page_end,
        )
        if not started:
            return jsonify({"error": "Could not start OCR."}), 409
        return jsonify({"ok": True})

    @app.route("/api/pdf/split", methods=["POST"])
    def api_pdf_split():
        if is_running():
            return jsonify({"error": "OCR is running."}), 409
        data = request.get_json(silent=True) or {}
        name = str(data.get("filename") or "").strip()
        ranges = data.get("ranges") or []
        if not name or not ranges:
            return jsonify({"error": "filename and ranges are required"}), 400
        pdf_path = photos_dir / name
        if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
            return jsonify({"error": "PDF file not found in uploads"}), 404
        reader = PdfReader(str(pdf_path))
        out_dir = output_dir / "pdf_tools"
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs = []
        for idx, r in enumerate(ranges, start=1):
            start = max(1, int(r.get("start", 1)))
            end = min(len(reader.pages), int(r.get("end", len(reader.pages))))
            if start > end:
                continue
            writer = PdfWriter()
            for page_i in range(start - 1, end):
                writer.add_page(reader.pages[page_i])
            out_path = out_dir / f"{pdf_path.stem}_part_{idx:02d}_{start}-{end}.pdf"
            with out_path.open("wb") as fh:
                writer.write(fh)
            outputs.append(str(out_path))
        return jsonify({"ok": True, "outputs": outputs})

    @app.route("/api/pdf/merge", methods=["POST"])
    def api_pdf_merge():
        if is_running():
            return jsonify({"error": "OCR is running."}), 409
        data = request.get_json(silent=True) or {}
        files = data.get("filenames") or []
        out_name = str(data.get("output_name") or "merged.pdf").strip()
        if not files:
            return jsonify({"error": "No files provided"}), 400
        writer = PdfWriter()
        for name in files:
            path = photos_dir / str(name)
            if not path.exists() or path.suffix.lower() != ".pdf":
                return jsonify({"error": f"Missing PDF: {name}"}), 404
            reader = PdfReader(str(path))
            for p in reader.pages:
                writer.add_page(p)
        out_dir = output_dir / "pdf_tools"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / out_name
        if out_path.suffix.lower() != ".pdf":
            out_path = out_path.with_suffix(".pdf")
        with out_path.open("wb") as fh:
            writer.write(fh)
        return jsonify({"ok": True, "output": str(out_path)})

    @app.route("/api/reset-project", methods=["POST"])
    def api_reset_project():
        if is_running():
            return jsonify({"error": "OCR is running."}), 409
        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        reset_job()
        return jsonify({"ok": True})

    @app.route("/api/manifest")
    def api_manifest():
        manifest = load_manifest()
        flagged = flagged_page_ids()
        all_pages = [p["page_id"] for p in manifest.get("pages", [])]
        return jsonify({**manifest, "flagged_queue": flagged, "all_pages": all_pages})

    @app.route("/api/page/<page_id>")
    def api_page(page_id: str):
        if not re.fullmatch(r"page_\d{3}", page_id):
            return jsonify({"error": "Invalid page id"}), 400

        consensus_path = output_dir / f"{page_id}_consensus.json"
        draft_path = output_dir / f"{page_id}_draft.txt"
        reviewed_path = output_dir / f"{page_id}_reviewed.txt"
        image_path = output_dir / f"{page_id}_source.jpg"

        if not consensus_path.exists():
            return jsonify({"error": "Page not found"}), 404

        consensus = json.loads(consensus_path.read_text(encoding="utf-8"))
        state = load_state()
        text = state.get("page_texts", {}).get(page_id)
        if text is None and reviewed_path.exists():
            text = reviewed_path.read_text(encoding="utf-8")
        elif text is None and draft_path.exists():
            text = draft_path.read_text(encoding="utf-8")
        else:
            text = text or ""

        flags = consensus.get("flags", [])
        resolved = state.get("resolved_flags", {})
        for f in flags:
            f["resolved"] = resolved.get(f["span_id"], False)

        return jsonify(
            {
                "page_id": page_id,
                "text": text,
                "flags": flags,
                "stats": consensus.get("stats", {}),
                "layout": consensus.get("layout", {}),
                "image_url": f"/images/{page_id}_source.jpg" if image_path.exists() else None,
            }
        )

    @app.route("/images/<path:filename>")
    def serve_image(filename: str):
        return send_from_directory(output_dir, filename)

    @app.route("/api/crop/<page_id>/<int:flag_index>")
    def api_crop(page_id: str, flag_index: int):
        consensus_path = output_dir / f"{page_id}_consensus.json"
        image_path = output_dir / f"{page_id}_source.jpg"
        if not consensus_path.exists() or not image_path.exists():
            return jsonify({"error": "Not found"}), 404

        flags = json.loads(consensus_path.read_text(encoding="utf-8")).get("flags", [])
        if flag_index < 0 or flag_index >= len(flags):
            return jsonify({"error": "Flag index out of range"}), 404

        flag = flags[flag_index]
        bbox = flag.get("bbox")
        img = cv2.imread(str(image_path))
        if img is None:
            return jsonify({"error": "Image read failed"}), 500

        if bbox:
            x1, y1, x2, y2 = bbox
            pad = 10
            h, w = img.shape[:2]
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
            crop = img[y1:y2, x1:x2]
        else:
            crop = img

        crop_dir = output_dir / "_crops"
        crop_dir.mkdir(exist_ok=True)
        crop_name = f"{page_id}_flag_{flag_index}.jpg"
        cv2.imwrite(str(crop_dir / crop_name), crop)
        return jsonify({"crop_url": f"/crops/{crop_name}"})

    @app.route("/crops/<path:filename>")
    def serve_crop(filename: str):
        return send_from_directory(output_dir / "_crops", filename)

    @app.route("/api/save", methods=["POST"])
    def api_save():
        data = request.get_json(force=True)
        page_id = data.get("page_id")
        text = data.get("text", "")
        resolved_flag = data.get("resolved_flag")
        resolution = data.get("resolution", "")

        if not page_id:
            return jsonify({"error": "page_id required"}), 400

        state = load_state()
        state.setdefault("page_texts", {})[page_id] = text
        if resolved_flag:
            state.setdefault("resolved_flags", {})[resolved_flag] = True
            state.setdefault("resolutions", {})[resolved_flag] = resolution
        save_state(state)

        reviewed_path = output_dir / f"{page_id}_reviewed.txt"
        reviewed_path.write_text(text, encoding="utf-8")
        return jsonify({"ok": True})

    @app.route("/api/export", methods=["POST"])
    def api_export():
        manifest = load_manifest()
        pages = load_pages_for_export(output_dir)
        state = load_state()
        for p in pages:
            pid = p["page_id"]
            if pid in state.get("page_texts", {}):
                p["reviewed_text"] = state["page_texts"][pid]

        title = manifest.get("book_title", "OCR Book Export")
        pdf_out = output_dir / "book_final.pdf"
        txt_out = output_dir / "book_final.txt"
        export_pdf(pages, pdf_out, title=title)
        export_plain_text(pages, txt_out)
        return jsonify({"pdf": str(pdf_out), "txt": str(txt_out)})

    return app


def _default_data_dir() -> Path:
    from pipeline.paths import app_root, is_frozen

    if is_frozen():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Archive Studios"
        return base
    return app_root()


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch Archive Studios")
    data = _default_data_dir()
    parser.add_argument("output", nargs="?", default=str(data / "output"), help="Output folder")
    parser.add_argument("--photos", default=str(data / "photos"), help="Upload folder")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    photos_dir = Path(args.photos).resolve()

    app = create_app(output_dir, photos_dir)
    print(f"Archive Studios: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
