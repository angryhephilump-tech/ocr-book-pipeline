"""Post-processing: reconcile, spot-check, transcribed.txt, summary (imported by pdf_transcribe)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from pdf_transcribe import (
    ANTHROPIC_BATCH_URL,
    ANTHROPIC_URL,
    API_DELAY_SEC,
    BATCH_MAX_REQUESTS,
    NUM_TRANSCRIPTION_RUNS,
    _format_api_error,
    anthropic_headers,
    assemble_run_txt,
    build_page_spot_patch_params,
    build_reconcile_params,
    build_vision_message_params,
    call_claude,
    format_page_block,
    is_skip_body,
    iter_batch_results,
    job_config_from_state,
    job_page_numbers,
    load_impossible_strings,
    load_state,
    normalize_transcription_output,
    parse_batch_custom_id,
    parse_batch_result_line,
    parse_pages,
    parse_reconcile_output,
    pass4_custom_id,
    read_run_page_body,
    reconcile_custom_id,
    reconcile_page_path,
    save_state,
    spot_check_page_path,
    spot_page_custom_id,
    transcription_user_prompt,
    wait_for_batch,
    pages_disagree,
    pages_need_content_reconcile,
    anthropic_request,
)
from pdf_transcribe_lang import DocumentTermIndex, effective_hard_terms, page_has_hard_term
from pdf_transcribe_source import page_section_hint

SPOT_PREP_REPORT_EVERY = 25
from pdf_transcribe_source import (
    optimize_soft_terms_from_log,
    page_section_hint,
    resolve_source_slug,
    spot_patch_operations_for_page,
    tag_page_sections_from_run1,
)
from pdf_transcribe_spot import (
    PatchOperation,
    apply_all_patches,
    parse_page_patch_response,
    split_patch_operations_by_token_budget,
)


@dataclass(frozen=True)
class ReconcileWorkItem:
    page_num: int
    image_path: Path
    run1_text: str
    run2_text: str


@dataclass(frozen=True)
class SpotPatchRequest:
    page_num: int
    image_path: Path
    operation: PatchOperation


@dataclass(frozen=True)
class Pass4WorkItem:
    page_num: int
    reconcile_body: str
    run1_text: str
    run2_text: str
    image_path: Path
    uncertain: bool
    uncertain_note: str | None


def mark_special_page_complete(state: dict, key: str, page_num: int) -> None:
    entry = state.setdefault(key, {"completed": []})
    done = set(entry.get("completed", []))
    done.add(page_num)
    entry["completed"] = sorted(done)


def completed_special_pages(state: dict, key: str) -> set[int]:
    return set((state.get(key) or {}).get("completed", []))


def generate_differences(work_dir: Path) -> Path:
    pages = []
    for i in range(1, NUM_TRANSCRIPTION_RUNS + 1):
        path = work_dir / f"run{i}.txt"
        if path.is_file():
            pages.append(parse_pages(path.read_text(encoding="utf-8")))
    all_nums = sorted({n for p in pages for n in p.keys() if n > 0})
    out_lines = [
        "DIFFERENCES REPORT (run 1 vs run 2)",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "=" * 72,
        "",
    ]
    count = 0
    if len(pages) >= 2:
        for page_num in all_nums:
            b1 = pages[0].get(page_num, "")
            b2 = pages[1].get(page_num, "")
            if b1.strip() == b2.strip():
                continue
            count += 1
            out_lines.extend(
                [
                    f"--- Page {page_num} ---",
                    f"RUN 1:\n{b1[:800]}",
                    f"RUN 2:\n{b2[:800]}",
                    "",
                ]
            )
    out_lines.append(f"Pages with disagreements: {count}")
    out_path = work_dir / "differences.txt"
    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return out_path


def count_reconcile_whitespace_skips(
    work_dir: Path, page_numbers: list[int], state: dict | None = None
) -> int:
    lang_cfg = job_config_from_state(state) if state else None
    n = 0
    for page_num in page_numbers:
        t1 = read_run_page_body(work_dir, 1, page_num)
        t2 = read_run_page_body(work_dir, 2, page_num)
        if is_skip_body(t1) and is_skip_body(t2):
            continue
        section = page_section_hint(state, page_num) if state else None
        if pages_disagree(t1, t2) and not pages_need_content_reconcile(
            t1, t2, lang_cfg, section=section
        ):
            n += 1
    return n


def pages_needing_reconcile(
    work_dir: Path, page_numbers: list[int], state: dict | None = None
) -> list[ReconcileWorkItem]:
    lang_cfg = job_config_from_state(state) if state else None
    items: list[ReconcileWorkItem] = []
    for page_num in page_numbers:
        t1 = read_run_page_body(work_dir, 1, page_num)
        t2 = read_run_page_body(work_dir, 2, page_num)
        if is_skip_body(t1) and is_skip_body(t2):
            continue
        section = page_section_hint(state, page_num) if state else None
        if not pages_need_content_reconcile(t1, t2, lang_cfg, section=section):
            continue
        rec_path = reconcile_page_path(work_dir, page_num)
        if rec_path.is_file():
            existing = rec_path.read_text(encoding="utf-8").strip()
            if existing and not existing.startswith("[Reconcile failed"):
                continue
        image_path = work_dir / "images" / f"page_{page_num:04d}.png"
        if image_path.is_file():
            items.append(
                ReconcileWorkItem(
                    page_num=page_num,
                    image_path=image_path,
                    run1_text=t1,
                    run2_text=t2,
                )
            )
    return items


def base_page_body(work_dir: Path, page_num: int) -> str:
    """Text after reconcile (or run 1) — before spot patches."""
    rec = reconcile_page_path(work_dir, page_num)
    if rec.is_file():
        return rec.read_text(encoding="utf-8").strip()
    t1 = read_run_page_body(work_dir, 1, page_num)
    t2 = read_run_page_body(work_dir, 2, page_num)
    return t1


def final_page_body(work_dir: Path, page_num: int) -> str:
    spot = spot_check_page_path(work_dir, page_num)
    if spot.is_file():
        return spot.read_text(encoding="utf-8").strip()
    return base_page_body(work_dir, page_num)


def load_page_bodies(work_dir: Path, page_numbers: list[int]) -> dict[int, str]:
    """Load base page text once per page (reconcile or run 1)."""
    return {page_num: base_page_body(work_dir, page_num) for page_num in page_numbers}


def build_document_text(
    work_dir: Path,
    page_numbers: list[int],
    *,
    page_bodies: dict[int, str] | None = None,
) -> str:
    """Assemble non-skipped page bodies once (single disk read per page)."""
    parts: list[str] = []
    for page_num in page_numbers:
        body = (
            page_bodies[page_num]
            if page_bodies is not None
            else base_page_body(work_dir, page_num)
        )
        if not is_skip_body(body):
            parts.append(body)
    return "\n\n".join(parts)


def collect_spot_patch_requests(
    work_dir: Path,
    page_numbers: list[int],
    state: dict,
    lang_cfg,
    *,
    document_text: str | None = None,
    term_index: DocumentTermIndex | None = None,
    page_bodies: dict[int, str] | None = None,
    report: Callable | None = None,
    total_pages: int = 0,
) -> list[SpotPatchRequest]:
    done = completed_special_pages(state, "spot_check")
    requests: list[SpotPatchRequest] = []
    if document_text and term_index is None:
        term_index = DocumentTermIndex(document_text)
    total = total_pages or len(page_numbers)
    for idx, page_num in enumerate(page_numbers, start=1):
        if report and (idx == 1 or idx % SPOT_PREP_REPORT_EVERY == 0 or idx == len(page_numbers)):
            report(
                "spot_prep",
                0,
                idx,
                total,
                None,
                f"Planning spot checks: page {idx}/{len(page_numbers)}…",
                step_done=idx,
                step_total=len(page_numbers),
            )
        if page_num in done:
            continue
        base = (
            page_bodies[page_num]
            if page_bodies is not None
            else base_page_body(work_dir, page_num)
        )
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
        image_path = work_dir / "images" / f"page_{page_num:04d}.png"
        if not image_path.is_file():
            continue
        ops = spot_patch_operations_for_page(
            base,
            lang_cfg,
            state,
            document_text=document_text,
            term_index=term_index,
        )
        for op in ops:
            requests.append(
                SpotPatchRequest(page_num=page_num, image_path=image_path, operation=op)
            )
    return requests


def spot_patch_log_path(work_dir: Path) -> Path:
    return work_dir / "spot_patch_log.txt"


def spot_raw_page_response_path(work_dir: Path, page_num: int) -> Path:
    return work_dir / "spot_check" / f"raw_page_{page_num:04d}.txt"


def _save_spot_raw_page_response(
    work_dir: Path,
    page_num: int,
    raw_text: str,
    *,
    chunk_idx: int = 0,
) -> None:
    path = spot_raw_page_response_path(work_dir, page_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    if chunk_idx > 0:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- chunk {chunk_idx + 1} ---\n{raw_text}")
    else:
        path.write_text(raw_text, encoding="utf-8")


def _append_spot_patch_log(work_dir: Path, page_num: int, entries: list[dict]) -> None:
    if not entries:
        return
    path = spot_patch_log_path(work_dir)
    lines: list[str] = []
    if not path.is_file():
        lines.extend(
            [
                "SPOT PATCH LOG",
                f"Generated: {datetime.now().isoformat(timespec='seconds')}",
                "",
            ]
        )
    lines.append(f"Page {page_num}")
    for e in entries:
        terms = ", ".join(e.get("terms") or [])
        if e.get("applied"):
            lines.append(f"  op {e.get('op_index')}: APPLIED ({e.get('section')}) [{terms}]")
        else:
            reason = e.get("reject_reason", "unknown")
            review = " — HUMAN REVIEW" if e.get("needs_human_review") else ""
            lines.append(
                f"  op {e.get('op_index')}: REJECTED ({reason}) ({e.get('section')}) [{terms}]{review}"
            )
    lines.append("")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _save_spot_patched_page(
    work_dir: Path,
    page_num: int,
    base: str,
    operations: list[PatchOperation],
    responses: list[str],
    lang_cfg,
    impossible: list[str],
    *,
    section_hint: str | None = None,
) -> tuple[int, int, bool]:
    patched, applied, rejected, log_entries = apply_all_patches(
        base,
        operations,
        responses,
        lang_cfg,
        impossible,
        section_hint=section_hint,
    )
    _append_spot_patch_log(work_dir, page_num, log_entries)
    needs_review = any(e.get("needs_human_review") for e in log_entries)
    if applied > 0:
        spot_check_page_path(work_dir, page_num).parent.mkdir(parents=True, exist_ok=True)
        spot_check_page_path(work_dir, page_num).write_text(patched, encoding="utf-8")
    return applied, rejected, needs_review


def _reconcile_content_matches(a: str, b: str) -> bool:
    from pdf_transcribe_lang import strip_whitespace_for_compare

    return strip_whitespace_for_compare(a) == strip_whitespace_for_compare(b)


def reconcile_needs_pass4(reconcile_body: str, run1: str, run2: str) -> bool:
    return not (
        _reconcile_content_matches(reconcile_body, run1)
        or _reconcile_content_matches(reconcile_body, run2)
    )


def resolve_pass4_outcome(
    run1: str,
    run2: str,
    reconcile_body: str,
    pass4_body: str,
) -> tuple[str, str, bool]:
    """Return (final_text, outcome_label, needs_human_review)."""
    if _reconcile_content_matches(pass4_body, run1):
        return run1, "pass4 matched run1 — using run1", False
    if _reconcile_content_matches(pass4_body, run2):
        return run2, "pass4 matched run2 — using run2", False
    if _reconcile_content_matches(pass4_body, reconcile_body):
        return reconcile_body, "pass4 matched reconcile — accepting reconcile", False
    return run1, "fourth unique reading — human review, fallback run1", True


def _run_pass4_transcription(
    api_key: str,
    image_path: Path,
    *,
    model: str,
    work_dir: Path,
    state: dict,
    page_num: int,
) -> str:
    user_prompt = transcription_user_prompt(work_dir, state)
    try:
        raw = call_claude(
            api_key,
            image_path,
            model=model,
            user_text=user_prompt,
        )
    except RuntimeError as exc:
        return f"[Pass 4 failed: {exc}]"
    lang_cfg = job_config_from_state(state)
    section = page_section_hint(state, page_num)
    return normalize_transcription_output(raw, lang_cfg, section=section)


def _apply_pass4_if_third_reading(
    reconcile_body: str,
    run1_text: str,
    run2_text: str,
    image_path: Path,
    *,
    api_key: str,
    model: str,
    work_dir: Path,
    state: dict,
    page_num: int,
) -> tuple[str, dict | None, bool]:
    """If reconcile is a third reading, run pass 4 and resolve."""
    if not reconcile_needs_pass4(reconcile_body, run1_text, run2_text):
        return reconcile_body, None, False

    pass4_body = _run_pass4_transcription(
        api_key,
        image_path,
        model=model,
        work_dir=work_dir,
        state=state,
        page_num=page_num,
    )
    time.sleep(API_DELAY_SEC)
    final_body, outcome, needs_review = resolve_pass4_outcome(
        run1_text, run2_text, reconcile_body, pass4_body
    )
    extra = {
        "pass4_fired": True,
        "run1_reading": run1_text,
        "run2_reading": run2_text,
        "reconcile_reading": reconcile_body,
        "pass4_reading": pass4_body,
        "pass4_outcome": outcome,
    }
    return final_body, extra, needs_review


def _format_reading_for_log(label: str, text: str) -> list[str]:
    lines = [f"  {label}:"]
    if text:
        for line in text.splitlines():
            lines.append(f"    {line}")
    else:
        lines.append("    (empty)")
    return lines


def _finalize_reconcile_page(
    work_dir: Path,
    page_num: int,
    final_body: str,
    *,
    state: dict,
    resolution: str,
    uncertain: bool,
    uncertain_note: str | None,
    pass4_extra: dict | None = None,
) -> tuple[dict, bool]:
    """Write reconcile page file; return (log_entry, needs_human_review_from_pass4)."""
    reconcile_page_path(work_dir, page_num).parent.mkdir(parents=True, exist_ok=True)
    reconcile_page_path(work_dir, page_num).write_text(final_body, encoding="utf-8")
    mark_special_page_complete(state, "reconcile", page_num)
    entry: dict = {
        "page": page_num,
        "disagreement": "run1 and run2 differ (content)",
        "resolution": resolution,
        "uncertain": uncertain,
        "uncertain_note": uncertain_note,
    }
    pass4_review = False
    if pass4_extra:
        entry.update(pass4_extra)
        entry["resolution"] = f"{resolution} + pass 4 cross-check"
        outcome = pass4_extra.get("pass4_outcome") or ""
        pass4_review = "fourth unique" in outcome
    return entry, pass4_review


def _pass4_extra_from_outcome(
    run1_text: str,
    run2_text: str,
    reconcile_body: str,
    pass4_body: str,
    final_body: str,
    outcome: str,
) -> dict:
    return {
        "pass4_fired": True,
        "run1_reading": run1_text,
        "run2_reading": run2_text,
        "reconcile_reading": reconcile_body,
        "pass4_reading": pass4_body,
        "pass4_outcome": outcome,
    }


def _resolve_pass4_page(
    item: Pass4WorkItem,
    pass4_body: str,
) -> tuple[str, dict, bool]:
    final_body, outcome, needs_review = resolve_pass4_outcome(
        item.run1_text, item.run2_text, item.reconcile_body, pass4_body
    )
    extra = _pass4_extra_from_outcome(
        item.run1_text,
        item.run2_text,
        item.reconcile_body,
        pass4_body,
        final_body,
        outcome,
    )
    return final_body, extra, needs_review


def _build_pass4_params(
    image_path: Path,
    model: str,
    work_dir: Path,
    state: dict,
) -> dict:
    user_prompt = transcription_user_prompt(work_dir, state)
    return build_vision_message_params(image_path, model, user_text=user_prompt)


def _process_pass4_batch(
    api_key: str,
    model: str,
    work_dir: Path,
    state: dict,
    pending: list[Pass4WorkItem],
    *,
    resolution: str,
    report: Callable | None,
    total_pages: int,
) -> list[tuple[dict, bool]]:
    """Run batched pass-4 transcriptions; return (log_entry, pass4_review) per page."""
    if not pending:
        return []
    payload = [
        {
            "custom_id": pass4_custom_id(item.page_num),
            "params": _build_pass4_params(item.image_path, model, work_dir, state),
        }
        for item in pending
    ]
    results_by_page: dict[int, str] = {}
    for line in _run_batch_custom(
        api_key,
        model,
        payload,
        report=report,
        phase="reconcile",
        total_pages=total_pages,
        label="Pass 4",
    ):
        cid, text, _ = parse_batch_result_line(line)
        if not cid or text is None:
            continue
        try:
            kind, _, page_num = parse_batch_custom_id(cid)
        except ValueError:
            continue
        if kind != "pass4":
            continue
        results_by_page[page_num] = text

    lang_cfg = job_config_from_state(state)
    outcomes: list[tuple[dict, bool]] = []
    for item in pending:
        raw = results_by_page.get(item.page_num)
        if raw is None:
            pass4_body = "[Pass 4 failed: missing batch result]"
        else:
            section = page_section_hint(state, item.page_num)
            pass4_body = normalize_transcription_output(raw, lang_cfg, section=section)
        final_body, pass4_extra, pass4_review = _resolve_pass4_page(item, pass4_body)
        entry, review = _finalize_reconcile_page(
            work_dir,
            item.page_num,
            final_body,
            state=state,
            resolution=resolution,
            uncertain=item.uncertain,
            uncertain_note=item.uncertain_note,
            pass4_extra=pass4_extra,
        )
        outcomes.append((entry, review or pass4_review))
    return outcomes


def _process_reconcile_result(
    work_dir: Path,
    page_num: int,
    reconcile_body: str,
    run1_text: str,
    run2_text: str,
    image_path: Path,
    *,
    api_key: str,
    model: str,
    state: dict,
    resolution: str,
    uncertain: bool,
    uncertain_note: str | None,
) -> tuple[dict, bool]:
    """Write reconcile page file; return (log_entry, needs_human_review_from_pass4)."""
    final_body, pass4_extra, pass4_review = _apply_pass4_if_third_reading(
        reconcile_body,
        run1_text,
        run2_text,
        image_path,
        api_key=api_key,
        model=model,
        work_dir=work_dir,
        state=state,
        page_num=page_num,
    )
    return _finalize_reconcile_page(
        work_dir,
        page_num,
        final_body,
        state=state,
        resolution=resolution,
        uncertain=uncertain,
        uncertain_note=uncertain_note,
        pass4_extra=pass4_extra,
    )


def write_reconcile_log(work_dir: Path, entries: list[dict]) -> Path:
    lines = ["RECONCILE LOG", f"Generated: {datetime.now().isoformat(timespec='seconds')}", ""]
    for e in entries:
        lines.append(f"Page {e['page']}")
        lines.append(f"  Disagreement: {e.get('disagreement', '')}")
        lines.append(f"  Resolution: {e.get('resolution', '')}")
        lines.append(f"  UNCERTAIN: {'yes' if e.get('uncertain') else 'no'}")
        if e.get("uncertain_note"):
            lines.append(f"  Note: {e['uncertain_note']}")
        if e.get("pass4_fired"):
            lines.append("  Pass 4 fired: yes")
            for key, label in (
                ("run1_reading", "Run 1 reading"),
                ("run2_reading", "Run 2 reading"),
                ("reconcile_reading", "Reconcile reading"),
                ("pass4_reading", "Pass 4 reading"),
            ):
                if key in e:
                    lines.extend(_format_reading_for_log(label, e[key]))
            if e.get("pass4_outcome"):
                lines.append(f"  Pass 4 outcome: {e['pass4_outcome']}")
        lines.append("")
    path = work_dir / "reconcile_log.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_transcribed_txt(
    work_dir: Path,
    page_numbers: list[int],
    *,
    state: dict | None = None,
) -> Path:
    from pdf_transcribe_assembly import apply_cross_page_bracket_rejoins
    from pdf_transcribe_integrity import (
        read_source_lock,
        verify_run_file_headers,
        write_sourced_text,
    )

    mismatches = verify_run_file_headers(work_dir)
    if mismatches:
        raise RuntimeError(
            "Cannot assemble transcribed.txt — run file source headers do not match: "
            + "; ".join(mismatches)
        )
    page_bodies = {n: final_page_body(work_dir, n) for n in page_numbers}
    page_bodies = apply_cross_page_bracket_rejoins(page_bodies, page_numbers)
    parts = [format_page_block(n, page_bodies[n]) for n in page_numbers]
    path = work_dir / "transcribed.txt"
    body = "\n\n".join(parts) + "\n"
    slug = (state or {}).get("source_name") or (state or {}).get("source_id") or read_source_lock(work_dir)
    if slug:
        write_sourced_text(path, slug, body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


def write_summary_report(work_dir: Path, stats: dict) -> Path:
    stats["generated_at"] = datetime.now(timezone.utc).isoformat()
    (work_dir / "run_summary.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    lines = [
        "RUN SUMMARY",
        f"Generated: {stats['generated_at']}",
        f"Model: {stats.get('model', '')}",
        f"Pages in job: {stats.get('total_pages', 0)}",
        f"PDF page range: {stats.get('pdf_first_page', '?')}–{stats.get('pdf_last_page', '?')}",
        f"Skipped front (not rendered): {stats.get('skip_front_pages', 0)}",
        f"Pages skipped: {stats.get('pages_skipped', 0)}",
        f"Pages reconciled: {stats.get('pages_reconciled', 0)}",
        f"Pages reconcile skipped (whitespace only): {stats.get('pages_reconcile_skipped_whitespace', 0)}",
        f"Pages spot-checked: {stats.get('pages_spot_checked', 0)}",
        f"Spot patches applied: {stats.get('spot_patches_applied', 0)}",
        f"Spot patches rejected: {stats.get('spot_patches_rejected', 0)}",
        f"Spot patches missing hard term (human review): {stats.get('spot_missing_term_reviews', 0)}",
        f"Pages with hard terms but no patch fired: {stats.get('spot_expected_no_patch', [])}",
        f"Pages flagged UNCERTAIN: {stats.get('pages_uncertain', 0)}",
        f"Characters in transcribed.txt: {stats.get('char_count', 0)}",
        f"Human review pages: {stats.get('human_review_pages', [])}",
        f"Batch collisions (sanity re-runs): {len(stats.get('batch_collisions', []))}",
    ]
    path = work_dir / "run_summary.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _post_message(api_key: str, payload: dict) -> str:
    resp = requests.post(
        ANTHROPIC_URL,
        headers=anthropic_headers(api_key),
        json=payload,
        timeout=(60, 300),
    )
    if resp.status_code >= 400:
        raise RuntimeError(_format_api_error(resp.status_code, resp.text))
    parts = resp.json().get("content") or []
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


def _run_batch_custom(
    api_key: str,
    model: str,
    requests_payload: list[dict],
    *,
    report: Callable | None = None,
    phase: str = "batch",
    total_pages: int = 0,
    label: str = "Batch",
) -> list[dict]:
    lines_out: list[dict] = []
    chunks = [
        requests_payload[i : i + BATCH_MAX_REQUESTS]
        for i in range(0, len(requests_payload), BATCH_MAX_REQUESTS)
    ]
    total_chunks = len(chunks)
    total_requests = len(requests_payload)
    for chunk_idx, chunk in enumerate(chunks):
        if report:
            report(
                phase,
                0,
                0,
                total_pages,
                None,
                f"{label}: submitted chunk {chunk_idx + 1}/{total_chunks} ({len(chunk)} requests)…",
                step_done=chunk_idx,
                step_total=total_chunks,
                batch_done=chunk_idx * BATCH_MAX_REQUESTS,
                batch_total=total_requests,
            )

        def on_batch_status(info: dict, _idx=chunk_idx) -> None:
            if not report:
                return
            counts = info.get("request_counts") or {}
            succeeded = int(counts.get("succeeded") or 0)
            report(
                phase,
                0,
                0,
                total_pages,
                None,
                f"{label}: chunk {chunk_idx + 1}/{total_chunks} — {succeeded}/{len(chunk)} done…",
                step_done=_idx,
                step_total=total_chunks,
                batch_done=_idx * BATCH_MAX_REQUESTS + succeeded,
                batch_total=total_requests,
            )

        resp = anthropic_request(
            "POST",
            ANTHROPIC_BATCH_URL,
            api_key,
            json_body={"requests": chunk},
            timeout=(120, 600),
            max_retries=8,
        )
        if resp.status_code >= 400:
            raise RuntimeError(_format_api_error(resp.status_code, resp.text))
        batch_id = resp.json().get("id")
        if not batch_id:
            raise RuntimeError("No batch id returned.")
        info = wait_for_batch(api_key, batch_id, on_status=on_batch_status)
        lines_out.extend(iter_batch_results(api_key, batch_id, info))
        if report:
            report(
                phase,
                0,
                0,
                total_pages,
                None,
                f"{label}: finished chunk {chunk_idx + 1}/{total_chunks}",
                step_done=chunk_idx + 1,
                step_total=total_chunks,
                batch_done=min((chunk_idx + 1) * BATCH_MAX_REQUESTS, total_requests),
                batch_total=total_requests,
            )
    return lines_out


def count_skipped_pages(work_dir: Path, page_numbers: list[int]) -> int:
    n = 0
    for page_num in page_numbers:
        body = final_page_body(work_dir, page_num)
        if is_skip_body(body):
            n += 1
    return n


def finalize_pipeline(
    api_key: str,
    work_dir: Path,
    state: dict,
    total_pages: int,
    *,
    model: str,
    use_batch: bool,
    spot_check_enabled: bool,
    report: Callable | None = None,
) -> dict:
    page_numbers = job_page_numbers(state)
    total_pages = len(page_numbers)
    slug = state.get("source_name") or state.get("source_id")
    for run in range(1, NUM_TRANSCRIPTION_RUNS + 1):
        assemble_run_txt(work_dir, run, page_numbers, source_slug=slug)
    generate_differences(work_dir)

    log_entries: list[dict] = []
    lang_cfg = job_config_from_state(state)
    stats: dict = {
        "model": model,
        "total_pages": total_pages,
        "pdf_first_page": page_numbers[0] if page_numbers else None,
        "pdf_last_page": page_numbers[-1] if page_numbers else None,
        "skip_front_pages": state.get("skip_front_pages", 0),
        "page_numbers": page_numbers,
        "pages_reconciled": 0,
        "pages_reconcile_skipped_whitespace": count_reconcile_whitespace_skips(
            work_dir, page_numbers, state
        ),
        "pages_spot_checked": 0,
        "spot_patches_applied": 0,
        "spot_patches_rejected": 0,
        "spot_missing_term_reviews": 0,
        "spot_expected_no_patch": [],
        "pages_uncertain": 0,
        "human_review_pages": [],
        "language": state.get("language", "spanish"),
        "source_id": state.get("source_id", "ixtlilxochitl"),
        "script": state.get("script", "latin"),
        "batch_collisions": state.get("batch_collisions") or [],
    }
    impossible = load_impossible_strings(lang_cfg.source_id, state)
    if not state.get("page_sections"):
        tag_page_sections_from_run1(work_dir, state)
        save_state(work_dir, state)

    page_bodies = load_page_bodies(work_dir, page_numbers)
    document_text = build_document_text(work_dir, page_numbers, page_bodies=page_bodies)
    term_index = DocumentTermIndex(document_text) if document_text else None

    reconcile_items = pages_needing_reconcile(work_dir, page_numbers, state)
    if reconcile_items and report:
        report(
            "reconcile",
            0,
            0,
            total_pages,
            None,
            f"Reconciling {len(reconcile_items)} pages…",
            step_done=0,
            step_total=len(reconcile_items),
        )

    if reconcile_items:
        reconcile_by_page = {item.page_num: item for item in reconcile_items}
        pass4_pending: list[Pass4WorkItem] = []
        if use_batch:
            payload = [
                {
                    "custom_id": reconcile_custom_id(item.page_num),
                    "params": build_reconcile_params(
                        item.image_path, model, item.run1_text, item.run2_text, lang_cfg
                    ),
                }
                for item in reconcile_items
            ]
            for line in _run_batch_custom(
                api_key,
                model,
                payload,
                report=report,
                phase="reconcile",
                total_pages=total_pages,
                label="Reconcile",
            ):
                cid, text, _ = parse_batch_result_line(line)
                if not cid or text is None:
                    continue
                try:
                    kind, _, page_num = parse_batch_custom_id(cid)
                except ValueError:
                    continue
                if kind != "reconcile":
                    continue
                item = reconcile_by_page.get(page_num)
                if not item:
                    continue
                section = page_section_hint(state, page_num)
                body, uncertain, note = parse_reconcile_output(
                    text, lang_cfg, section=section
                )
                if reconcile_needs_pass4(body, item.run1_text, item.run2_text):
                    pass4_pending.append(
                        Pass4WorkItem(
                            page_num=page_num,
                            reconcile_body=body,
                            run1_text=item.run1_text,
                            run2_text=item.run2_text,
                            image_path=item.image_path,
                            uncertain=uncertain,
                            uncertain_note=note,
                        )
                    )
                    continue
                entry, pass4_review = _finalize_reconcile_page(
                    work_dir,
                    page_num,
                    body,
                    state=state,
                    resolution="batch reconcile from image",
                    uncertain=uncertain,
                    uncertain_note=note,
                )
                log_entries.append(entry)
                if uncertain:
                    stats["pages_uncertain"] += 1
                    stats["human_review_pages"].append(page_num)
                if pass4_review:
                    stats["human_review_pages"].append(page_num)
                stats["pages_reconciled"] += 1
            if pass4_pending:
                for entry, pass4_review in _process_pass4_batch(
                    api_key,
                    model,
                    work_dir,
                    state,
                    pass4_pending,
                    resolution="batch reconcile from image",
                    report=report,
                    total_pages=total_pages,
                ):
                    log_entries.append(entry)
                    page_num = entry["page"]
                    if entry.get("uncertain"):
                        stats["pages_uncertain"] += 1
                        stats["human_review_pages"].append(page_num)
                    if pass4_review:
                        stats["human_review_pages"].append(page_num)
                    stats["pages_reconciled"] += 1
        else:
            for idx, item in enumerate(reconcile_items, start=1):
                if report:
                    report("reconcile", 0, idx, total_pages, None, f"Reconcile {idx}/{len(reconcile_items)}…")
                try:
                    raw = _post_message(
                        api_key,
                        build_reconcile_params(
                            item.image_path, model, item.run1_text, item.run2_text, lang_cfg
                        ),
                    )
                    section = page_section_hint(state, item.page_num)
                    body, uncertain, note = parse_reconcile_output(
                        raw, lang_cfg, section=section
                    )
                except RuntimeError as exc:
                    body, uncertain, note = f"[Reconcile failed: {exc}]", True, str(exc)
                entry, pass4_review = _process_reconcile_result(
                    work_dir,
                    item.page_num,
                    body,
                    item.run1_text,
                    item.run2_text,
                    item.image_path,
                    api_key=api_key,
                    model=model,
                    state=state,
                    resolution="live reconcile from image",
                    uncertain=uncertain,
                    uncertain_note=note,
                )
                log_entries.append(entry)
                if uncertain:
                    stats["pages_uncertain"] += 1
                    stats["human_review_pages"].append(item.page_num)
                if pass4_review:
                    stats["human_review_pages"].append(item.page_num)
                stats["pages_reconciled"] += 1
                time.sleep(API_DELAY_SEC)
        save_state(work_dir, state)

    write_reconcile_log(work_dir, log_entries)

    if spot_check_enabled:
        if report:
            report(
                "spot_prep",
                0,
                0,
                total_pages,
                None,
                f"Planning spot checks ({total_pages} pages)…",
                step_done=0,
                step_total=total_pages,
            )
        patch_requests = collect_spot_patch_requests(
            work_dir,
            page_numbers,
            state,
            lang_cfg,
            document_text=document_text,
            term_index=term_index,
            page_bodies=page_bodies,
            report=report,
            total_pages=total_pages,
        )
        by_page: dict[int, list[SpotPatchRequest]] = {}
        for req in patch_requests:
            by_page.setdefault(req.page_num, []).append(req)
        pages_with_spot = sorted(by_page.keys())
        spot_chunks = max(1, len(pages_with_spot))
        if patch_requests and report:
            report(
                "spot_check",
                0,
                0,
                total_pages,
                None,
                f"Spot patches: {len(patch_requests)} sentences on {len(pages_with_spot)} pages…",
                step_done=0,
                step_total=spot_chunks,
                batch_done=0,
                batch_total=len(pages_with_spot),
            )
        if patch_requests:

            def _ops_for_page(page_num: int) -> list[PatchOperation]:
                reqs = sorted(by_page[page_num], key=lambda r: r.operation.op_index)
                return [r.operation for r in reqs]

            def _parse_chunk_responses(
                chunk_ops: list[PatchOperation],
                raw_text: str,
                page_num: int,
            ) -> list[str]:
                originals = [op.sentence for op in chunk_ops]
                responses = parse_page_patch_response(
                    raw_text, len(chunk_ops), originals
                )
                section = page_section_hint(state, page_num)
                return [
                    normalize_transcription_output(r, lang_cfg, section=section)
                    for r in responses
                ]

            def _apply_page_spot_results(
                page_num: int,
                chunk_results: list[tuple[list[PatchOperation], list[str]]],
            ) -> None:
                base = base_page_body(work_dir, page_num)
                ops: list[PatchOperation] = []
                responses: list[str] = []
                for chunk_ops, chunk_responses in chunk_results:
                    ops.extend(chunk_ops)
                    responses.extend(chunk_responses)
                hint = page_section_hint(state, page_num)
                applied, rejected, needs_review = _save_spot_patched_page(
                    work_dir,
                    page_num,
                    base,
                    ops,
                    responses,
                    lang_cfg,
                    impossible,
                    section_hint=hint,
                )
                mark_special_page_complete(state, "spot_check", page_num)
                stats["pages_spot_checked"] += 1
                stats["spot_patches_applied"] += applied
                stats["spot_patches_rejected"] += rejected
                if needs_review:
                    stats["spot_missing_term_reviews"] += 1
                    stats["human_review_pages"].append(page_num)

            def _spot_chunks_for_page(page_num: int) -> list[list[PatchOperation]]:
                return split_patch_operations_by_token_budget(_ops_for_page(page_num))

            if use_batch:
                payload: list[dict] = []
                for page_num in pages_with_spot:
                    chunks = _spot_chunks_for_page(page_num)
                    image_path = by_page[page_num][0].image_path
                    hint = page_section_hint(state, page_num)
                    for chunk_idx, chunk_ops in enumerate(chunks):
                        payload.append(
                            {
                                "custom_id": spot_page_custom_id(page_num, chunk_idx),
                                "params": build_page_spot_patch_params(
                                    image_path,
                                    model,
                                    chunk_ops,
                                    lang_cfg,
                                    section_hint=hint,
                                ),
                            }
                        )
                results_by_chunk: dict[tuple[int, int], str] = {}
                for line in _run_batch_custom(
                    api_key,
                    model,
                    payload,
                    report=report,
                    phase="spot_check",
                    total_pages=total_pages,
                    label="Spot patch",
                ):
                    cid, text, _ = parse_batch_result_line(line)
                    if not cid or text is None:
                        continue
                    try:
                        kind, chunk_idx, page_num = parse_batch_custom_id(cid)
                    except ValueError:
                        continue
                    if kind not in ("spotpg", "spot"):
                        continue
                    results_by_chunk[(page_num, chunk_idx)] = text
                for page_num in pages_with_spot:
                    chunk_results: list[tuple[list[PatchOperation], list[str]]] = []
                    for chunk_idx, chunk_ops in enumerate(
                        _spot_chunks_for_page(page_num)
                    ):
                        raw_text = results_by_chunk.get((page_num, chunk_idx), "")
                        _save_spot_raw_page_response(
                            work_dir, page_num, raw_text, chunk_idx=chunk_idx
                        )
                        chunk_results.append(
                            (
                                chunk_ops,
                                _parse_chunk_responses(chunk_ops, raw_text, page_num),
                            )
                        )
                    _apply_page_spot_results(page_num, chunk_results)
            else:
                for idx, page_num in enumerate(pages_with_spot, start=1):
                    if report:
                        report(
                            "spot_check",
                            0,
                            idx,
                            total_pages,
                            None,
                            f"Spot-check page {page_num} ({idx}/{len(pages_with_spot)})…",
                        )
                    reqs = sorted(by_page[page_num], key=lambda r: r.operation.op_index)
                    hint = page_section_hint(state, page_num)
                    page_chunks = _spot_chunks_for_page(page_num)
                    chunk_results = []
                    for chunk_idx, chunk_ops in enumerate(page_chunks):
                        try:
                            raw = _post_message(
                                api_key,
                                build_page_spot_patch_params(
                                    reqs[0].image_path,
                                    model,
                                    chunk_ops,
                                    lang_cfg,
                                    section_hint=hint,
                                ),
                            )
                        except RuntimeError:
                            raw = ""
                        _save_spot_raw_page_response(
                            work_dir, page_num, raw, chunk_idx=chunk_idx
                        )
                        chunk_results.append(
                            (chunk_ops, _parse_chunk_responses(chunk_ops, raw, page_num))
                        )
                        if chunk_idx + 1 < len(page_chunks):
                            time.sleep(API_DELAY_SEC)
                    _apply_page_spot_results(page_num, chunk_results)
                    time.sleep(API_DELAY_SEC)
            save_state(work_dir, state)

        # Pages with hard terms but no extractable sentences — mark done; flag for review.
        for page_num in page_numbers:
            if page_num in completed_special_pages(state, "spot_check"):
                continue
            base = base_page_body(work_dir, page_num)
            if is_skip_body(base):
                continue
            hint = page_section_hint(state, page_num)
            terms = effective_hard_terms(
                base, lang_cfg, state, document_text=document_text
            )
            if not page_has_hard_term(base, terms, lang_cfg, section_hint=hint):
                continue
            ops = spot_patch_operations_for_page(
                base, lang_cfg, state, document_text=document_text
            )
            if not ops:
                stats["spot_expected_no_patch"].append(page_num)
                stats["human_review_pages"].append(page_num)
            mark_special_page_complete(state, "spot_check", page_num)
        save_state(work_dir, state)

    slug = resolve_source_slug(state)
    if slug:
        from pdf_transcribe_integrity import log_soft_term_promotions
        from pdf_transcribe_source import load_soft_terms

        before = load_soft_terms(slug, state)
        optimize_soft_terms_from_log(work_dir, slug, state)
        promoted = state.get("soft_terms_promoted") or []
        if promoted:
            log_soft_term_promotions(work_dir, slug, before, state.get("soft_terms") or [])
        save_state(work_dir, state)

    from pdf_transcribe_assembly import page_needs_bracket_boundary_review

    page_bodies_for_review = {n: final_page_body(work_dir, n) for n in page_numbers}
    for idx, page_num in enumerate(page_numbers):
        body = page_bodies_for_review.get(page_num, "")
        if is_skip_body(body):
            continue
        prev_body = (
            page_bodies_for_review.get(page_numbers[idx - 1]) if idx > 0 else None
        )
        next_body = (
            page_bodies_for_review.get(page_numbers[idx + 1])
            if idx + 1 < len(page_numbers)
            else None
        )
        if page_needs_bracket_boundary_review(
            body, prev_body=prev_body, next_body=next_body
        ):
            stats["human_review_pages"].append(page_num)

    transcribed_path = build_transcribed_txt(work_dir, page_numbers, state=state)
    stats["pages_skipped"] = count_skipped_pages(work_dir, page_numbers)
    stats["char_count"] = len(transcribed_path.read_text(encoding="utf-8"))
    stats["human_review_pages"] = sorted(set(stats["human_review_pages"]))
    stats["accuracy_notes"] = state.get("accuracy_notes", "")
    write_summary_report(work_dir, stats)
    from pdf_transcribe_detect import save_source_config

    from pdf_transcribe_integrity import evaluate_pilot_checks

    save_source_config(work_dir, state, stats)
    stats["pilot_report"] = evaluate_pilot_checks(work_dir, state, stats)
    (work_dir / "pilot_report.json").write_text(
        json.dumps(stats["pilot_report"], indent=2), encoding="utf-8"
    )
    return stats
