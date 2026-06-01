"""Background OCR jobs for the web UI."""

from __future__ import annotations

import threading
from pathlib import Path

from ocr_book import run_pipeline

_lock = threading.Lock()
_job: dict = {
    "status": "idle",
    "current": 0,
    "total": 0,
    "filename": "",
    "message": "",
    "error": "",
}


def get_job_status() -> dict:
    with _lock:
        return dict(_job)


def _set_job(**kwargs) -> None:
    with _lock:
        _job.update(kwargs)


def is_running() -> bool:
    with _lock:
        return _job["status"] == "running"


def start_job(photos_dir: Path, output_dir: Path, *, title: str | None = None) -> bool:
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
        )

        def on_progress(current: int, total: int, filename: str) -> None:
            _set_job(
                current=current,
                total=total,
                filename=filename,
                message=f"Transcribing page {current} of {total}",
            )

        try:
            manifest = run_pipeline(
                photos_dir,
                output_dir,
                title=title or "Untitled Book",
                no_interactive=True,
                on_progress=on_progress,
            )
            _set_job(
                status="done",
                message=f"Finished — {manifest['total_pages']} pages processed",
                current=manifest["total_pages"],
                total=manifest["total_pages"],
            )
        except Exception as exc:
            _set_job(status="error", error=str(exc), message="OCR failed")

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
            }
        )
