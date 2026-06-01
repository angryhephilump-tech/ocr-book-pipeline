#!/usr/bin/env python3
"""
Verbatim Studio — web UI for the OCR book pipeline.

Usage:
  python review_ui.py
  python review_ui.py ./output --port 5050
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

import cv2
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from pipeline.export import export_pdf, export_plain_text, load_pages_for_export
from pipeline.web_jobs import get_job_status, is_running, reset_job, start_job

ROOT = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".pdf"}


def create_app(output_dir: Path, photos_dir: Path) -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    app.config["OUTPUT_DIR"] = output_dir
    app.config["PHOTOS_DIR"] = photos_dir
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    output_dir.mkdir(parents=True, exist_ok=True)
    photos_dir.mkdir(parents=True, exist_ok=True)

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
        if has_manifest():
            return render_template("review.html")
        return render_template("home.html")

    @app.route("/review")
    def review():
        if not has_manifest():
            return render_template("home.html"), 404
        return render_template("review.html")

    @app.route("/api/status")
    def api_status():
        uploads = list_uploads()
        manifest = load_manifest() if has_manifest() else {}
        return jsonify(
            {
                "has_manifest": has_manifest(),
                "upload_count": len(uploads),
                "uploads": uploads,
                "job": get_job_status(),
                "book_title": manifest.get("book_title"),
                "total_pages": manifest.get("total_pages", 0),
                "flagged_pages": manifest.get("flagged_pages", 0),
            }
        )

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
        reset_job()
        started = start_job(photos_dir, output_dir, title=title)
        if not started:
            return jsonify({"error": "Could not start OCR."}), 409
        return jsonify({"ok": True})

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch Verbatim Studio")
    parser.add_argument("output", nargs="?", default=str(ROOT / "output"), help="Output folder")
    parser.add_argument("--photos", default=str(ROOT / "photos"), help="Upload folder")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    photos_dir = Path(args.photos).resolve()

    app = create_app(output_dir, photos_dir)
    print(f"Verbatim Studio: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
