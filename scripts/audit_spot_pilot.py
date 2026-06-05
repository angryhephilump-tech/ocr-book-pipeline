#!/usr/bin/env python3
"""Audit a completed pilot run for spot-patch coverage and failures.

Usage:
  python scripts/audit_spot_pilot.py "path/to/pilot_transcribe_output"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe import job_page_numbers, load_state  # noqa: E402
from pdf_transcribe_finalize import base_page_body, spot_patch_log_path  # noqa: E402
from pdf_transcribe_lang import effective_hard_terms, job_language_config, page_has_hard_term  # noqa: E402
from pdf_transcribe_spot import collect_patch_operations  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path)
    args = parser.parse_args()
    work_dir = args.work_dir.resolve()
    state = load_state(work_dir)
    lang_cfg = job_language_config(state.get("language"), state.get("source_id"))
    pages = job_page_numbers(state)

    summary_path = work_dir / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}

    print("SPOT PILOT AUDIT")
    print(f"Work dir: {work_dir}")
    print()
    print("From run_summary.json:")
    print(f"  spot_patches_applied: {summary.get('spot_patches_applied', '?')}")
    print(f"  spot_patches_rejected: {summary.get('spot_patches_rejected', '?')}")
    print(f"  spot_missing_term_reviews: {summary.get('spot_missing_term_reviews', '?')}")
    print(f"  spot_expected_no_patch: {summary.get('spot_expected_no_patch', [])}")
    print(f"  human_review_pages: {summary.get('human_review_pages', [])}")
    print()

    expected_no_patch: list[int] = []
    would_patch: list[int] = []
    for page in pages:
        base = base_page_body(work_dir, page)
        if base.startswith("[Skipped:"):
            continue
        terms = effective_hard_terms(base, lang_cfg)
        if not page_has_hard_term(base, terms):
            continue
        ops = collect_patch_operations(base, terms, lang_cfg)
        if ops:
            would_patch.append(page)
        else:
            expected_no_patch.append(page)

    if expected_no_patch:
        print("FAIL — hard terms on page but extraction found zero sentences:")
        print(f"  {expected_no_patch}")
    else:
        print("OK — every hard-term page had at least one extractable patch sentence.")

    print()
    print(f"Pages that would patch today: {len(would_patch)}")
    log = spot_patch_log_path(work_dir)
    if log.is_file():
        print()
        print(f"See details: {log}")
    return 1 if expected_no_patch or summary.get("spot_missing_term_reviews") else 0


if __name__ == "__main__":
    raise SystemExit(main())
