#!/usr/bin/env python3
"""Drag-and-drop UI for PDF transcribe (Claude). Double-click Launch PDF Transcribe.bat."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

import pdf_transcribe

ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / "_pdf_transcribe_uploads"
ALLOWED = {".pdf"}


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

    job: dict = {"thread": None, "work_dir": None, "error": None, "running": False}

    @app.route("/")
    def index():
        return render_template("pdf_transcribe.html")

    @app.route("/api/key-status")
    def api_key_status():
        status = pdf_transcribe.api_key_status()
        status["app_version"] = 5
        from pdf_transcribe_lang import list_source_ids

        status["source_ids"] = list_source_ids()
        return jsonify(status)

    @app.route("/api/save-key", methods=["POST"])
    def api_save_key():
        data = request.get_json(silent=True) or {}
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return jsonify({"ok": False, "error": "No key to save."}), 400
        path = pdf_transcribe.save_settings(api_key=api_key)
        return jsonify({"ok": True, "hint": pdf_transcribe.mask_api_key(api_key), "path": str(path)})

    @app.route("/api/start", methods=["POST"])
    def api_start():
        if job["running"]:
            return jsonify({"ok": False, "error": "A job is already running."}), 409

        api_key = (request.form.get("api_key") or "").strip()
        remember = request.form.get("remember_key", "1") in ("1", "on", "true", "yes")
        try:
            api_key = pdf_transcribe.resolve_api_key(api_key or None)
        except ValueError:
            return jsonify(
                {
                    "ok": False,
                    "error": "No Claude key. Paste one below or run Save Claude Key.bat first.",
                }
            ), 400
        if remember and (request.form.get("api_key") or "").strip():
            pdf_transcribe.save_settings(api_key=api_key)

        upload = request.files.get("pdf")
        if not upload or not upload.filename:
            return jsonify({"ok": False, "error": "Drop or choose a PDF file."}), 400

        if not upload.filename.lower().endswith(".pdf"):
            return jsonify({"ok": False, "error": "Only PDF files are supported."}), 400

        pilot_raw = (request.form.get("pilot_pages") or "").strip()
        explicit_pages = pdf_transcribe.parse_page_list(pilot_raw) if pilot_raw else None
        mode = request.form.get("mode", "test")
        max_pages = None if explicit_pages else (10 if mode == "test" else None)
        try:
            skip_front_pages = max(0, int(request.form.get("skip_front_pages", "2") or "2"))
        except ValueError:
            skip_front_pages = 2

        processing_mode = (request.form.get("processing_mode") or "auto").strip().lower()
        if processing_mode not in pdf_transcribe.PROCESSING_MODES:
            processing_mode = pdf_transcribe.DEFAULT_PROCESSING_MODE
        spot_check = request.form.get("spot_check", "1") in ("1", "on", "true", "yes")
        language = (request.form.get("language") or "spanish").strip().lower()
        source_id = (request.form.get("source_id") or "ixtlilxochitl").strip().lower()
        script = (request.form.get("script") or "latin").strip().lower()
        pdf_transcribe.save_settings(
            processing_mode=processing_mode,
            spot_check_enabled=spot_check,
            language=language,
            source_id=source_id,
            script=script,
        )

        UPLOADS.mkdir(parents=True, exist_ok=True)
        safe = secure_filename(upload.filename) or "book.pdf"
        pdf_path = UPLOADS / safe
        upload.save(pdf_path)

        work_dir = pdf_transcribe.work_dir_for_pdf(pdf_path)

        def worker() -> None:
            job["running"] = True
            job["error"] = None
            job["work_dir"] = str(work_dir)
            try:
                pdf_transcribe.run_transcription(
                    pdf_path,
                    api_key,
                    max_pages=max_pages,
                    work_dir=work_dir,
                    skip_front_pages=skip_front_pages,
                    processing_mode=processing_mode,
                    language=language,
                    source_id=source_id,
                    script=script,
                    explicit_pages=explicit_pages,
                )
            except ValueError as exc:
                job["error"] = str(exc)
                pdf_transcribe.write_progress(
                    work_dir,
                    phase="error",
                    current_run=0,
                    page=0,
                    total_pages=0,
                    message=str(exc),
                )
            except Exception as exc:
                job["error"] = str(exc)
                pdf_transcribe.write_progress(
                    work_dir,
                    phase="error",
                    current_run=0,
                    page=0,
                    total_pages=0,
                    message=str(exc),
                )
            finally:
                job["running"] = False

        threading.Thread(target=worker, daemon=True).start()

        resolved = pdf_transcribe.resolve_processing_mode(processing_mode, max_pages=max_pages)
        mode_label = pdf_transcribe.processing_mode_label(processing_mode, max_pages=max_pages)
        return jsonify(
            {
                "ok": True,
                "work_dir": str(work_dir),
                "max_pages": max_pages,
                "processing_mode": resolved,
                "message": (
                    f"Test run (10 pages) · {mode_label}"
                    if max_pages
                    else f"Full book · {mode_label}"
                ),
            }
        )

    @app.route("/api/progress")
    def api_progress():
        work_dir = job.get("work_dir")
        if not work_dir:
            return jsonify({"phase": "idle", "message": "Waiting to start…"})
        prog_path = Path(work_dir) / "progress.json"
        if prog_path.is_file():
            import json

            data = json.loads(prog_path.read_text(encoding="utf-8"))
            data["running"] = job["running"]
            data["error"] = job.get("error")
            data["work_dir"] = work_dir
            return jsonify(data)
        return jsonify(
            {
                "phase": "starting",
                "message": "Starting…",
                "running": job["running"],
                "work_dir": work_dir,
            }
        )

    @app.route("/api/open-folder", methods=["POST"])
    def api_open_folder():
        data = request.get_json(silent=True) or {}
        folder = (data.get("path") or job.get("work_dir") or "").strip()
        if not folder or not Path(folder).is_dir():
            return jsonify({"ok": False, "error": "No output folder yet."}), 400
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", folder], check=False)
        else:
            subprocess.run(["xdg-open", folder], check=False)
        return jsonify({"ok": True})

    return app


def main() -> None:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    try:
        pdf_transcribe.resolve_api_key()
    except ValueError:
        print("\n  No Claude API key yet.")
        print("  Double-click: Save Claude Key.bat\n")
    port = int(os.environ.get("PDF_TRANSCRIBE_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  PDF Transcribe (Claude) → {url}\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    create_app().run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
