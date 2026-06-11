#!/usr/bin/env python3
"""Finish a half-done PDF Transcribe job (reconcile → spot-check → transcribed.txt)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe import (  # noqa: E402
    job_page_numbers,
    load_settings,
    load_state,
    resolve_api_key,
)
from pdf_transcribe_finalize import finalize_pipeline  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/finish_job.py <work_dir>")
        return 1

    work_dir = Path(sys.argv[1]).resolve()
    if not work_dir.is_dir():
        print(f"Error: not a directory: {work_dir}")
        return 1

    api_key = resolve_api_key(None)
    settings = load_settings()
    state = load_state(work_dir)
    page_numbers = job_page_numbers(state)
    total_pages = len(page_numbers)

    def report(phase, run=0, page=0, total=0, eta=None, message=None, **kw):
        print(message or phase, flush=True)

    finalize_pipeline(
        api_key,
        work_dir,
        state,
        total_pages,
        model=settings["model"],
        use_batch=True,
        spot_check_enabled=True,
        report=report,
    )
    print(f"Done — look for transcribed.txt in {work_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
