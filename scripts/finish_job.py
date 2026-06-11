#!/usr/bin/env python3
"""Finish a half-done PDF Transcribe job (reconcile → spot-check → transcribed.txt)."""

from __future__ import annotations

import argparse
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
from pdf_transcribe_job_lock import JobLockError, acquire_job_lock, release_job_lock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Override a stale job lock (dead PID)",
    )
    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    if not work_dir.is_dir():
        print(f"Error: not a directory: {work_dir}")
        return 1

    try:
        acquire_job_lock(work_dir, force=args.force)
    except JobLockError as exc:
        print(f"Error: {exc}")
        return 1

    try:
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
    finally:
        release_job_lock(work_dir)


if __name__ == "__main__":
    raise SystemExit(main())
