#!/usr/bin/env python3
"""Time spot-patch planning per page (no API calls)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe import (  # noqa: E402
    is_skip_body,
    job_config_from_state,
    job_page_numbers,
    load_state,
)
from pdf_transcribe_finalize import (  # noqa: E402
    build_document_text,
    collect_spot_patch_requests,
    load_page_bodies,
)
from pdf_transcribe_lang import DocumentTermIndex, effective_hard_terms, page_has_hard_term  # noqa: E402
from pdf_transcribe_source import page_section_hint, spot_patch_operations_for_page  # noqa: E402

DEFAULT_WORK_DIR = (
    ROOT
    / "_pdf_transcribe_uploads"
    / "historia_verdadera_tomo_1_genaro_garc_a_s_edition"
    / "historiaverdade04castgoog_output"
)
SLOW_PAGE_MS = 500


def main() -> int:
    work_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_WORK_DIR
    if not work_dir.is_dir():
        print(f"Error: not a directory: {work_dir}")
        return 1

    state = load_state(work_dir)
    page_numbers = job_page_numbers(state)
    lang_cfg = job_config_from_state(state)

    page_bodies = load_page_bodies(work_dir, page_numbers)
    document_text = build_document_text(work_dir, page_numbers, page_bodies=page_bodies)
    term_index = DocumentTermIndex(document_text) if document_text else None

    slow_pages: list[tuple[int, float, int]] = []
    per_page_start = time.perf_counter()
    planned_ops = 0

    for page_num in page_numbers:
        t0 = time.perf_counter()
        base = page_bodies[page_num]
        if is_skip_body(base):
            continue
        terms = effective_hard_terms(
            base,
            lang_cfg,
            state,
            document_text=document_text,
            term_index=term_index,
        )
        hint = page_section_hint(state, page_num)
        if not page_has_hard_term(base, terms, lang_cfg, section_hint=hint):
            continue
        ops = spot_patch_operations_for_page(
            base,
            lang_cfg,
            state,
            document_text=document_text,
            term_index=term_index,
        )
        ms = (time.perf_counter() - t0) * 1000
        planned_ops += len(ops)
        if ms > SLOW_PAGE_MS:
            slow_pages.append((page_num, ms, len(ops)))

    per_page_elapsed = time.perf_counter() - per_page_start

    t0 = time.perf_counter()
    requests = collect_spot_patch_requests(
        work_dir,
        page_numbers,
        state,
        lang_cfg,
        document_text=document_text,
        term_index=term_index,
        page_bodies=page_bodies,
    )
    batch_elapsed = time.perf_counter() - t0

    last_page = page_numbers[-1] if page_numbers else 0
    print(f"Work dir: {work_dir}")
    print(f"Pages: {len(page_numbers)} (last page {last_page})")
    print(f"Per-page planning: {per_page_elapsed:.2f}s, {planned_ops} ops")
    print(f"collect_spot_patch_requests: {batch_elapsed:.2f}s, {len(requests)} requests")
    if slow_pages:
        print(f"SLOW pages (>{SLOW_PAGE_MS} ms):")
        for page_num, ms, n_ops in slow_pages:
            print(f"  page {page_num}: {ms:.0f} ms, {n_ops} ops")
        return 1
    print("All pages fast — no slow pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
