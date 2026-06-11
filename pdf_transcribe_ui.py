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
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import pdf_transcribe
from pdf_transcribe_job_lock import JobLockError, acquire_job_lock, release_job_lock

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
    work_dir_custom = (request.form.get("work_dir") or "").strip() or None

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
        "work_dir": work_dir_custom,
    }, None


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

    job: dict = {"thread": None, "work_dir": None, "error": None, "running": False}

    def _resolve_work_dir() -> str | None:
        raw = (request.args.get("work_dir") or "").strip() or (job.get("work_dir") or "")
        if not raw:
            return None
        path = Path(raw).resolve()
        if not path.is_dir():
            return None
        wd = str(path)
        if not job.get("work_dir"):
            job["work_dir"] = wd
        return wd

    def _find_pdf_path(work_dir: Path, state: dict) -> Path | None:
        pdf_raw = (state.get("pdf") or "").strip()
        if pdf_raw:
            candidate = Path(pdf_raw)
            if candidate.is_file():
                return candidate
        source = (state.get("source_name") or "").strip().lower()
        if source:
            slug = source.replace("_", "")
            for path in UPLOADS.glob("*.pdf"):
                stem = path.stem.lower().replace("_", "")
                if slug in stem or stem in slug:
                    return path
        for path in sorted(UPLOADS.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
            return path
        return None

    def _prepare_params_from_state(work_dir: Path, state: dict) -> dict:
        settings = pdf_transcribe.load_settings()
        pdf_path = _find_pdf_path(work_dir, state)
        return {
            "pdf_path": str(pdf_path) if pdf_path else state.get("pdf"),
            "max_pages": state.get("max_pages"),
            "skip_front_pages": state.get("skip_front_pages", 2),
            "processing_mode": settings.get("processing_mode"),
            "language": state.get("language"),
            "source_id": state.get("source_id"),
            "source_name": state.get("source_name"),
            "script": state.get("script"),
            "explicit_pages": None,
            "spot_check": settings.get("spot_check_enabled", True),
        }

    def _launch_transcription(key: str, wd: Path, p: dict, pdf: Path) -> None:
        def worker() -> None:
            job["running"] = True
            job["error"] = None
            try:
                acquire_job_lock(wd)
                pdf_transcribe.run_transcription(
                    pdf,
                    key,
                    max_pages=p.get("max_pages"),
                    work_dir=wd,
                    skip_front_pages=p.get("skip_front_pages", 2),
                    processing_mode=p.get("processing_mode"),
                    language=p.get("language"),
                    source_id=p.get("source_id"),
                    source_name=p.get("source_name"),
                    script=p.get("script"),
                    explicit_pages=p.get("explicit_pages"),
                    skip_detection=True,
                )
            except JobLockError as exc:
                job["error"] = str(exc)
                pdf_transcribe.write_progress(
                    wd,
                    phase="error",
                    current_run=0,
                    page=0,
                    total_pages=0,
                    message=str(exc),
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
                release_job_lock(wd)
                job["running"] = False

        threading.Thread(target=worker, daemon=True).start()

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_exc: RequestEntityTooLarge):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "PDF too large (max 500 MB)."}), 413
        return _exc

    @app.errorhandler(404)
    def handle_404(exc):
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"Unknown API route {request.path}. "
                            "Close the browser tab and reopen Launch PDF Transcribe.bat."
                        ),
                    }
                ),
                404,
            )
        return exc

    @app.errorhandler(500)
    def handle_500(exc):
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Server error — check the PDF Transcribe terminal window for details.",
                    }
                ),
                500,
            )
        return exc

    @app.route("/")
    def index():
        return render_template("pdf_transcribe.html", app_version=6)

    @app.route("/api/key-status")
    def api_key_status():
        status = pdf_transcribe.api_key_status()
        status["app_version"] = 6
        from pdf_transcribe_integrity import list_saved_sources
        from pdf_transcribe_lang import list_source_ids

        status["source_ids"] = list_source_ids()
        status["sources"] = list_saved_sources()
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

    def _api_prepare_impl():
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
        from pdf_transcribe_integrity import (
            run_startup_integrity,
            summarize_completed_work,
            work_dir_contains_source_name,
            work_dir_has_completed_work,
        )
        work_dir = pdf_transcribe.work_dir_for_pdf(
            pdf_path,
            params["source_name"],
            custom_work_dir=params.get("work_dir"),
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        integrity, _healed = run_startup_integrity(work_dir, params["source_name"])
        if integrity.blocking:
            return jsonify({"ok": False, "error": integrity.blocking[0]}), 409
        existing_state = pdf_transcribe.load_state(work_dir)
        has_existing_work = work_dir_has_completed_work(existing_state)
        existing_summary = (
            summarize_completed_work(existing_state) if has_existing_work else {}
        )
        job["work_dir"] = str(work_dir)
        job["auto_proceed_attempted"] = False
        job["has_existing_work"] = has_existing_work
        job["existing_work_summary"] = existing_summary
        job["integrity"] = integrity.to_dict()
        folder_warn = None
        if not work_dir_contains_source_name(work_dir, params["source_name"]):
            folder_warn = (
                f"Tip: consider naming this folder after your source ({params['source_name']}) "
                "to avoid mixing runs."
            )

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
                        step_done=_extra.get("step_done"),
                        step_total=_extra.get("step_total"),
                        batch_done=_extra.get("batch_done"),
                        batch_total=_extra.get("batch_total"),
                    )

                report("rendering", 0, 0, 0, None, "Converting PDF pages to images…")
                _wd, _pages, page_range, state = pdf_transcribe._init_transcription_job(
                    pdf_path,
                    max_pages=params["max_pages"],
                    skip_front_pages=params["skip_front_pages"],
                    work_dir=work_dir,
                    language=params["language"],
                    source_id=params["source_id"],
                    source_name=params["source_name"],
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
                    if has_existing_work:
                        job["needs_resume_choice"] = True
                        pdf_transcribe.write_progress(
                            work_dir,
                            phase="awaiting_confirm",
                            current_run=0,
                            page=0,
                            total_pages=page_range.job_page_count,
                            message="Existing work found — resume or start over?",
                        )
                    else:
                        _launch_transcription(api_key, work_dir, params, pdf_path)
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
                if job.get("error") or job.get("needs_confirmation") or job.get(
                    "needs_resume_choice"
                ):
                    job["running"] = False

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
                "integrity": integrity.to_dict(),
                "folder_warning": folder_warn,
                "has_existing_work": has_existing_work,
                "existing_work_summary": existing_summary,
            }
        )

    @app.route("/api/prepare", methods=["POST"])
    @app.route("/api/start", methods=["POST"])
    def api_prepare():
        try:
            return _api_prepare_impl()
        except Exception as exc:
            job["running"] = False
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/profile")
    def api_profile():
        work_dir = _resolve_work_dir()
        if not work_dir:
            return jsonify({"ready": False})
        state_path = Path(work_dir) / "state.json"
        if not state_path.is_file():
            return jsonify({"ready": False})
        state = json.loads(state_path.read_text(encoding="utf-8"))
        profile = state.get("detected_source_profile") or job.get("profile") or {}
        from pdf_transcribe_detect import profile_display_lines

        from pdf_transcribe_integrity import (
            summarize_completed_work,
            work_dir_has_completed_work,
        )

        existing_state = pdf_transcribe.load_state(Path(work_dir))
        has_existing = work_dir_has_completed_work(existing_state)
        prog = pdf_transcribe.load_progress(Path(work_dir))
        phase_awaiting = (prog or {}).get("phase") == "awaiting_confirm"
        needs_resume = bool(job.get("needs_resume_choice"))
        if not needs_resume and phase_awaiting and profile.get("confirmed") and has_existing:
            needs_resume = True
        needs_confirmation = not profile.get("confirmed")
        return jsonify(
            {
                "ready": True,
                "needs_confirmation": needs_confirmation,
                "needs_resume_choice": needs_resume,
                "has_existing_work": has_existing,
                "existing_work_summary": summarize_completed_work(existing_state)
                if has_existing
                else {},
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

        wd_path = Path(work_dir)
        if not job.get("work_dir"):
            job["work_dir"] = work_dir

        prepare = job.get("prepare_params") or {}
        state_for_prepare = pdf_transcribe.load_state(wd_path)
        if not prepare:
            prepare = _prepare_params_from_state(wd_path, state_for_prepare)
            job["prepare_params"] = prepare

        pdf_path = _find_pdf_path(wd_path, state_for_prepare)
        if not pdf_path:
            raw = prepare.get("pdf_path") or state_for_prepare.get("pdf") or ""
            pdf_path = Path(raw) if raw else None
        if not pdf_path or not pdf_path.is_file():
            return jsonify({"ok": False, "error": "PDF path missing. Run prepare again."}), 400
        prepare["pdf_path"] = str(pdf_path)

        try:
            api_key = pdf_transcribe.resolve_api_key()
        except ValueError:
            return jsonify({"ok": False, "error": "No Claude API key saved."}), 400

        state = pdf_transcribe.load_state(Path(work_dir))
        profile = state.get("detected_source_profile") or job.get("profile") or {}
        source_name = prepare.get("source_name") or state.get("source_name") or "unknown"

        from pdf_transcribe_integrity import (
            backup_work_dir_before_reset,
            reset_source_work_dir,
            work_dir_has_completed_work,
        )

        if action == "start_over":
            if work_dir_has_completed_work(state):
                backup_path = backup_work_dir_before_reset(Path(work_dir))
                if backup_path:
                    job["last_backup"] = str(backup_path)
            reset_source_work_dir(Path(work_dir), source_name)
            state = pdf_transcribe.load_state(Path(work_dir))
            if not profile:
                from pdf_transcribe_detect import load_saved_source_profile

                profile = load_saved_source_profile(source_name) or {}
            pdf_transcribe.confirm_source_profile(
                Path(work_dir),
                state,
                source_name,
                profile,
                language=(data.get("language") or prepare.get("language") or None),
                script=(data.get("script") or prepare.get("script") or None),
            )
        elif action in ("yes", "confirm", "resume"):
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
        job["needs_resume_choice"] = False
        job["auto_proceed_attempted"] = True
        prepare["source_name"] = source_name
        _launch_transcription(api_key, Path(work_dir), prepare, pdf_path)
        msg = "Transcription started."
        if action == "start_over" and job.get("last_backup"):
            msg = f"Start over — backup saved to {job['last_backup']}"
        elif action in ("resume", "yes", "confirm"):
            msg = "Resuming transcription."
        return jsonify({"ok": True, "work_dir": work_dir, "message": msg})

    @app.route("/api/pilot-status")
    def api_pilot_status():
        source_name = (request.args.get("source_name") or "").strip()
        if not source_name:
            return jsonify({"ok": False, "error": "source_name required"}), 400
        from pdf_transcribe_integrity import pilot_gate_status

        return jsonify({"ok": True, **pilot_gate_status(source_name)})

    @app.route("/api/reset-source", methods=["POST"])
    def api_reset_source():
        data = request.get_json(silent=True) or {}
        source_name = (data.get("source_name") or "").strip()
        work_dir_raw = (data.get("work_dir") or job.get("work_dir") or "").strip()
        if not source_name:
            return jsonify({"ok": False, "error": "source_name required"}), 400
        if not work_dir_raw:
            return jsonify({"ok": False, "error": "work_dir required"}), 400
        from pdf_transcribe_integrity import reset_source_work_dir

        deleted = reset_source_work_dir(Path(work_dir_raw), source_name)
        return jsonify(
            {
                "ok": True,
                "deleted": deleted,
                "message": f"Reset {source_name} — {len(deleted)} item(s) removed.",
            }
        )

    @app.route("/api/progress")
    def api_progress():
        from pdf_transcribe_integrity import work_dir_has_completed_work

        work_dir = _resolve_work_dir()
        if not work_dir:
            return jsonify({"phase": "idle", "message": "Waiting to start…"})
        wd_path = Path(work_dir)
        data = pdf_transcribe.load_progress(wd_path)
        if data is not None:
            data["running"] = job["running"]
            data["error"] = job.get("error")
            data["work_dir"] = work_dir
            data["awaiting_confirm"] = bool(
                job.get("needs_confirmation")
                or job.get("needs_resume_choice")
                or data.get("phase") == "awaiting_confirm"
            )
            if (
                data.get("phase") == "awaiting_confirm"
                and not job["running"]
                and not job.get("auto_proceed_attempted")
            ):
                state = pdf_transcribe.load_state(wd_path)
                profile = state.get("detected_source_profile") or {}
                has_existing = work_dir_has_completed_work(state)
                if profile.get("confirmed") and not has_existing:
                    try:
                        api_key = pdf_transcribe.resolve_api_key()
                    except ValueError:
                        pass
                    else:
                        prepare = job.get("prepare_params") or _prepare_params_from_state(
                            wd_path, state
                        )
                        job["prepare_params"] = prepare
                        pdf_path = _find_pdf_path(wd_path, state)
                        if pdf_path:
                            job["auto_proceed_attempted"] = True
                            job["needs_confirmation"] = False
                            job["needs_resume_choice"] = False
                            _launch_transcription(api_key, wd_path, prepare, pdf_path)
                            data["running"] = True
                            data["awaiting_confirm"] = False
            pilot_path = wd_path / "pilot_report.json"
            if pilot_path.is_file():
                try:
                    pilot_text = pilot_path.read_text(encoding="utf-8").strip()
                    if pilot_text:
                        data["pilot_report"] = json.loads(pilot_text)
                except (json.JSONDecodeError, OSError):
                    pass
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
