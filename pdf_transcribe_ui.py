#!/usr/bin/env python3
"""Drag-and-drop UI for PDF transcribe (Claude). Double-click Launch PDF Transcribe.bat."""

from __future__ import annotations

import json
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


def _parse_job_form() -> tuple[dict, str | None]:
    """Parse shared form fields. Returns (params, error)."""
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
    language = (request.form.get("language") or "").strip().lower() or None
    source_id = (request.form.get("source_id") or "").strip().lower() or None
    script = (request.form.get("script") or "").strip().lower() or None
    source_name = (request.form.get("source_name") or "").strip()
    if not source_name:
        return {}, "Name this source (e.g. kaqchikel_chronicles)."

    return {
        "max_pages": max_pages,
        "explicit_pages": explicit_pages,
        "skip_front_pages": skip_front_pages,
        "processing_mode": processing_mode,
        "spot_check": spot_check,
        "language": language,
        "source_id": source_id,
        "script": script,
        "source_name": source_name,
    }, None


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
        status["app_version"] = 6
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

    def _resolve_api_key_from_form() -> tuple[str | None, str | None]:
        api_key = (request.form.get("api_key") or "").strip()
        remember = request.form.get("remember_key", "1") in ("1", "on", "true", "yes")
        try:
            resolved = pdf_transcribe.resolve_api_key(api_key or None)
        except ValueError:
            return None, "No Claude key. Paste one below or run Save Claude Key.bat first."
        if remember and api_key:
            pdf_transcribe.save_settings(api_key=resolved)
        return resolved, None

    @app.route("/api/prepare", methods=["POST"])
    def api_prepare():
        if job["running"]:
            return jsonify({"ok": False, "error": "A job is already running."}), 409

        api_key, key_err = _resolve_api_key_from_form()
        if key_err:
            return jsonify({"ok": False, "error": key_err}), 400

        params, form_err = _parse_job_form()
        if form_err:
            return jsonify({"ok": False, "error": form_err}), 400

        upload = request.files.get("pdf")
        if not upload or not upload.filename:
            return jsonify({"ok": False, "error": "Drop or choose a PDF file."}), 400
        if not upload.filename.lower().endswith(".pdf"):
            return jsonify({"ok": False, "error": "Only PDF files are supported."}), 400

        pdf_transcribe.save_settings(
            processing_mode=params["processing_mode"],
            spot_check_enabled=params["spot_check"],
        )

        UPLOADS.mkdir(parents=True, exist_ok=True)
        safe = secure_filename(upload.filename) or "book.pdf"
        pdf_path = UPLOADS / safe
        upload.save(pdf_path)
        work_dir = pdf_transcribe.work_dir_for_pdf(pdf_path)
        job["work_dir"] = str(work_dir)

        pdf_transcribe.write_progress(
            work_dir,
            phase="detecting",
            current_run=0,
            page=0,
            total_pages=0,
            message="Analyzing sample pages…",
        )

        def detect_worker() -> None:
            job["running"] = True
            job["error"] = None
            try:
                from pdf_transcribe_detect import profile_display_lines

                def report(phase, run, page, total, eta, msg, **_extra) -> None:
                    pdf_transcribe.write_progress(
                        work_dir,
                        phase=phase,
                        current_run=run,
                        page=page,
                        total_pages=total,
                        message=msg,
                    )

                report("rendering", 0, 0, 0, None, "Converting PDF pages to images…")
                _wd, _pages, page_range, state = pdf_transcribe._init_transcription_job(
                    pdf_path,
                    max_pages=params["max_pages"],
                    skip_front_pages=params["skip_front_pages"],
                    work_dir=work_dir,
                    language=params["language"],
                    source_id=params["source_id"],
                    script=params["script"],
                    explicit_pages=params["explicit_pages"],
                )
                profile = pdf_transcribe.run_source_detection(
                    api_key,
                    work_dir,
                    state,
                    page_range.page_numbers,
                    params["source_name"],
                    report,
                    use_saved=True,
                )
                job["profile"] = profile
                job["needs_confirmation"] = not profile.get("confirmed")
                job["prepare_params"] = {**params, "pdf_path": str(pdf_path)}
                lines = profile_display_lines(profile)
                pdf_transcribe.write_progress(
                    work_dir,
                    phase="awaiting_confirm" if job["needs_confirmation"] else "detecting",
                    current_run=0,
                    page=0,
                    total_pages=page_range.job_page_count,
                    message="Review detected profile before proceeding…"
                    if job["needs_confirmation"]
                    else f"Loaded saved profile for {params['source_name']}",
                )
                if not job["needs_confirmation"]:
                    _start_transcription_job(api_key, work_dir, params, pdf_path)
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
                if job.get("needs_confirmation"):
                    job["running"] = False

        def _start_transcription_job(
            key: str, wd: Path, p: dict, pdf: Path
        ) -> None:
            def worker() -> None:
                job["running"] = True
                job["error"] = None
                try:
                    pdf_transcribe.run_transcription(
                        pdf,
                        key,
                        max_pages=p["max_pages"],
                        work_dir=wd,
                        skip_front_pages=p["skip_front_pages"],
                        processing_mode=p["processing_mode"],
                        language=p["language"],
                        source_id=p["source_id"],
                        source_name=p["source_name"],
                        script=p["script"],
                        explicit_pages=p["explicit_pages"],
                        skip_detection=True,
                    )
                except Exception as exc:
                    job["error"] = str(exc)
                    pdf_transcribe.write_progress(
                        wd,
                        phase="error",
                        current_run=0,
                        page=0,
                        total_pages=0,
                        message=str(exc),
                    )
                finally:
                    job["running"] = False

            threading.Thread(target=worker, daemon=True).start()

        threading.Thread(target=detect_worker, daemon=True).start()

        resolved = pdf_transcribe.resolve_processing_mode(
            params["processing_mode"], max_pages=params["max_pages"]
        )
        mode_label = pdf_transcribe.processing_mode_label(
            params["processing_mode"], max_pages=params["max_pages"]
        )
        return jsonify(
            {
                "ok": True,
                "work_dir": str(work_dir),
                "message": f"Detecting source profile · {mode_label}",
                "processing_mode": resolved,
            }
        )

    @app.route("/api/profile")
    def api_profile():
        work_dir = job.get("work_dir")
        if not work_dir:
            return jsonify({"ready": False})
        state_path = Path(work_dir) / "state.json"
        if not state_path.is_file():
            return jsonify({"ready": False})
        state = json.loads(state_path.read_text(encoding="utf-8"))
        profile = state.get("detected_source_profile") or job.get("profile") or {}
        from pdf_transcribe_detect import profile_display_lines

        return jsonify(
            {
                "ready": True,
                "needs_confirmation": not profile.get("confirmed"),
                "from_saved": bool(profile.get("from_saved_config")),
                "lines": profile_display_lines(profile),
                "profile": profile,
                "work_dir": work_dir,
            }
        )

    @app.route("/api/confirm", methods=["POST"])
    def api_confirm():
        if job["running"]:
            return jsonify({"ok": False, "error": "A job is already running."}), 409

        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "yes").strip().lower()
        if action == "cancel":
            job["needs_confirmation"] = False
            job["profile"] = None
            return jsonify({"ok": True, "cancelled": True})

        work_dir = (data.get("work_dir") or job.get("work_dir") or "").strip()
        if not work_dir or not Path(work_dir).is_dir():
            return jsonify({"ok": False, "error": "No prepared job. Upload and detect first."}), 400

        prepare = job.get("prepare_params") or {}
        pdf_path = Path(prepare.get("pdf_path") or "")
        if not pdf_path.is_file():
            return jsonify({"ok": False, "error": "PDF path missing. Run prepare again."}), 400

        try:
            api_key = pdf_transcribe.resolve_api_key()
        except ValueError:
            return jsonify({"ok": False, "error": "No Claude API key saved."}), 400

        state = pdf_transcribe.load_state(Path(work_dir))
        profile = state.get("detected_source_profile") or job.get("profile") or {}
        source_name = prepare.get("source_name") or state.get("source_name") or "unknown"

        if action in ("yes", "confirm"):
            pdf_transcribe.confirm_source_profile(
                Path(work_dir),
                state,
                source_name,
                profile,
                language=(data.get("language") or prepare.get("language") or None),
                script=(data.get("script") or prepare.get("script") or None),
            )
        elif action == "edit":
            overrides = data.get("overrides") or {}
            prof = dict(profile)
            if overrides.get("language"):
                prof["languages"] = {overrides["language"]: 1.0}
                prof["languages_raw"] = overrides["language"]
            if overrides.get("script"):
                prof["script"] = overrides["script"]
            pdf_transcribe.confirm_source_profile(
                Path(work_dir),
                state,
                source_name,
                prof,
                language=overrides.get("language"),
                script=overrides.get("script"),
            )
        else:
            return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400

        job["needs_confirmation"] = False

        def worker() -> None:
            job["running"] = True
            job["error"] = None
            try:
                pdf_transcribe.run_transcription(
                    pdf_path,
                    api_key,
                    max_pages=prepare.get("max_pages"),
                    work_dir=Path(work_dir),
                    skip_front_pages=prepare.get("skip_front_pages", 2),
                    processing_mode=prepare.get("processing_mode"),
                    language=prepare.get("language"),
                    source_id=prepare.get("source_id"),
                    source_name=source_name,
                    script=prepare.get("script"),
                    explicit_pages=prepare.get("explicit_pages"),
                    skip_detection=True,
                )
            except Exception as exc:
                job["error"] = str(exc)
                pdf_transcribe.write_progress(
                    Path(work_dir),
                    phase="error",
                    current_run=0,
                    page=0,
                    total_pages=0,
                    message=str(exc),
                )
            finally:
                job["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True, "work_dir": work_dir, "message": "Transcription started."})

    @app.route("/api/progress")
    def api_progress():
        work_dir = job.get("work_dir")
        if not work_dir:
            return jsonify({"phase": "idle", "message": "Waiting to start…"})
        prog_path = Path(work_dir) / "progress.json"
        if prog_path.is_file():
            data = json.loads(prog_path.read_text(encoding="utf-8"))
            data["running"] = job["running"]
            data["error"] = job.get("error")
            data["work_dir"] = work_dir
            data["awaiting_confirm"] = bool(job.get("needs_confirmation"))
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
