"""Background OCR jobs for the web UI."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from ocr_book import run_pipeline
from pipeline.progress_log import append_progress_log
from pipeline.system_keepawake import keep_system_awake

_lock = threading.Lock()
_job: dict[str, Any] = {
    "status": "idle",
    "current": 0,
    "total": 0,
    "filename": "",
    "message": "",
    "error": "",
    "stage": "",
    "pass_id": "",
    "pass_label": "",
    "page_id": "",
    "updated_at": 0.0,
    "pass_started_at": 0.0,
    "started_at": 0.0,
}


def _stale_seconds() -> int:
    try:
        return int(os.environ.get("ARCHIVE_STUDIOS_STALE_SEC", "900"))
    except ValueError:
        return 900


def _build_message(event: dict[str, Any]) -> str:
    stage = event.get("stage", "")
    page_current = int(event.get("page_current") or 0)
    page_total = int(event.get("page_total") or 0)
    pass_label = event.get("pass_label") or ""

    if stage == "pass_start" and pass_label:
        if page_current and page_total:
            return f"Page {page_current}/{page_total} — {pass_label}…"
        return f"{pass_label}…"
    if stage == "pass_done" and pass_label:
        if page_current and page_total:
            return f"Page {page_current}/{page_total} — finished {event.get('pass_id', '')}"
        return f"Finished {event.get('pass_id', '')}"
    if event.get("message"):
        return str(event["message"])
    if page_current and page_total:
        return f"Transcribing page {page_current} of {page_total}"
    return "Transcribing…"


def get_job_status() -> dict:
    with _lock:
        out = dict(_job)
    now = time.time()
    updated = float(out.get("updated_at") or 0)
    out["seconds_since_update"] = round(now - updated, 1) if updated else 0
    pass_started = float(out.get("pass_started_at") or 0)
    out["seconds_on_pass"] = round(now - pass_started, 1) if pass_started else 0
    stale_after = _stale_seconds()
    out["stale_after_seconds"] = stale_after
    if out.get("status") == "running" and updated:
        out["stale"] = (now - updated) > stale_after
    else:
        out["stale"] = False
    started_at = float(out.get("started_at") or 0)
    current = int(out.get("current") or 0)
    total = int(out.get("total") or 0)
    if out.get("status") == "running" and started_at and current > 0 and total > current:
        elapsed = now - started_at
        per_page = elapsed / max(1, current)
        out["eta_seconds"] = int(per_page * (total - current))
    else:
        out["eta_seconds"] = None
    return out


def _set_job(**kwargs) -> None:
    now = time.time()
    with _lock:
        if kwargs.get("stage") == "pass_start":
            _job["pass_started_at"] = now
        elif kwargs.get("stage") in ("pass_done", "page_done", "page_start"):
            _job["pass_started_at"] = 0.0
        _job.update(kwargs)
        _job["updated_at"] = now


def is_running() -> bool:
    with _lock:
        return _job["status"] == "running"


def start_job(
    photos_dir: Path,
    output_dir: Path,
    *,
    title: str | None = None,
    language_config: dict | None = None,
    license_key: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> bool:
    with _lock:
        if _job["status"] == "running":
            return False

    def worker() -> None:
        _set_job(
            status="running",
            current=0,
            total=0,
            filename="",
            message="Preparing OCR engines…",
            error="",
            stage="prepare",
            pass_id="",
            pass_label="",
            page_id="",
            started_at=time.time(),
        )
        append_progress_log(
            output_dir,
            {"stage": "job_start", "title": title or "Untitled Book"},
        )

        def on_progress(event: dict) -> None:
            msg = _build_message(event)
            _set_job(
                current=int(event.get("page_current") or 0),
                total=int(event.get("page_total") or 0),
                filename=str(event.get("filename") or ""),
                message=msg,
                stage=str(event.get("stage") or ""),
                pass_id=str(event.get("pass_id") or ""),
                pass_label=str(event.get("pass_label") or ""),
                page_id=str(event.get("page_id") or ""),
            )
            append_progress_log(output_dir, event)

        try:
            with keep_system_awake():
                manifest = run_pipeline(
                    photos_dir,
                    output_dir,
                    title=title or "Untitled Book",
                    language_config=language_config,
                    no_interactive=True,
                    on_progress=on_progress,
                    resume=True,
                    license_key=license_key,
                    page_start=page_start,
                    page_end=page_end,
                )
            append_progress_log(
                output_dir,
                {"stage": "job_done", "total_pages": manifest["total_pages"]},
            )
            _set_job(
                status="done",
                message=f"Finished — {manifest['total_pages']} pages processed",
                current=manifest["total_pages"],
                total=manifest["total_pages"],
                stage="done",
                pass_id="",
                pass_label="",
            )
        except Exception as exc:
            err = str(exc)
            append_progress_log(output_dir, {"stage": "job_error", "error": err})
            if "License key missing" in err:
                err = "License not activated. Enter your key on the activation screen first."
            _set_job(status="error", error=err, message="OCR failed", stage="error")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return True


def reset_job() -> None:
    with _lock:
        if _job["status"] == "running":
            return
        _job.update(
            {
                "status": "idle",
                "current": 0,
                "total": 0,
                "filename": "",
                "message": "",
                "error": "",
                "stage": "",
                "pass_id": "",
                "pass_label": "",
                "page_id": "",
                "updated_at": 0.0,
                "pass_started_at": 0.0,
                "started_at": 0.0,
            }
        )
