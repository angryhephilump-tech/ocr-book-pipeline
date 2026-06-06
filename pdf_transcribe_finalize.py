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
    build_reconcile_params,
    build_spot_patch_params,
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
    read_run_page_body,
    reconcile_custom_id,
    reconcile_page_path,
    save_state,
    spot_check_custom_id,
    spot_check_page_path,
    wait_for_batch,
    pages_disagree,
    pages_need_content_reconcile,
    anthropic_request,
)
from pdf_transcribe_lang import effective_hard_terms, page_has_hard_term
from pdf_transcribe_source import (
    optimize_soft_terms_from_log,
    page_section_hint,
    resolve_source_slug,
    spot_patch_operations_for_page,
    tag_page_sections_from_run1,
)
from pdf_transcribe_spot import PatchOperation, apply_all_patches


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


def count_reconcile_whitespace_skips(work_dir: Path, page_numbers: list[int]) -> int:
    n = 0
    for page_num in page_numbers:
        t1 = read_run_page_body(work_dir, 1, page_num)
        t2 = read_run_page_body(work_dir, 2, page_num)
        if is_skip_body(t1) and is_skip_body(t2):
            continue
        if pages_disagree(t1, t2) and not pages_need_content_reconcile(t1, t2):
            n += 1
    return n


def pages_needing_reconcile(work_dir: Path, page_numbers: list[int]) -> list[ReconcileWorkItem]:
    items: list[ReconcileWorkItem] = []
    for page_num in page_numbers:
        t1 = read_run_page_body(work_dir, 1, page_num)
        t2 = read_run_page_body(work_dir, 2, page_num)
        if is_skip_body(t1) and is_skip_body(t2):
            continue
        if not pages_need_content_reconcile(t1, t2):
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


def collect_spot_patch_requests(
    work_dir: Path,
    page_numbers: list[int],
    state: dict,
    lang_cfg,
) -> list[SpotPatchRequest]:
    done = completed_special_pages(state, "spot_check")
    requests: list[SpotPatchRequest] = []
    for page_num in page_numbers:
        if page_num in done:
            continue
        base = base_page_body(work_dir, page_num)
        if is_skip_body(base):
            continue
        terms = effective_hard_terms(base, lang_cfg, state)
        if not page_has_hard_term(base, terms):
            continue
        image_path = work_dir / "images" / f"page_{page_num:04d}.png"
        if not image_path.is_file():
            continue
        ops = spot_patch_operations_for_page(base, lang_cfg, state)
        for op in ops:
            requests.append(
                SpotPatchRequest(page_num=page_num, image_path=image_path, operation=op)
            )
    return requests


def spot_patch_log_path(work_dir: Path) -> Path:
    return work_dir / "spot_patch_log.txt"


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
) -> tuple[int, int, bool]:
    patched, applied, rejected, log_entries = apply_all_patches(
        base, operations, responses, lang_cfg, impossible
    )
    _append_spot_patch_log(work_dir, page_num, log_entries)
    needs_review = any(e.get("needs_human_review") for e in log_entries)
    if applied > 0:
        spot_check_page_path(work_dir, page_num).parent.mkdir(parents=True, exist_ok=True)
        spot_check_page_path(work_dir, page_num).write_text(patched, encoding="utf-8")
    return applied, rejected, needs_review


def write_reconcile_log(work_dir: Path, entries: list[dict]) -> Path:
    lines = ["RECONCILE LOG", f"Generated: {datetime.now().isoformat(timespec='seconds')}", ""]
    for e in entries:
        lines.append(f"Page {e['page']}")
        lines.append(f"  Disagreement: {e.get('disagreement', '')}")
        lines.append(f"  Resolution: {e.get('resolution', '')}")
        lines.append(f"  UNCERTAIN: {'yes' if e.get('uncertain') else 'no'}")
        if e.get("uncertain_note"):
            lines.append(f"  Note: {e['uncertain_note']}")
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
    parts = [format_page_block(n, final_page_body(work_dir, n)) for n in page_numbers]
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
) -> list[dict]:
    lines_out: list[dict] = []
    for i in range(0, len(requests_payload), BATCH_MAX_REQUESTS):
        chunk = requests_payload[i : i + BATCH_MAX_REQUESTS]
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
        info = wait_for_batch(api_key, batch_id)
        lines_out.extend(iter_batch_results(api_key, batch_id, info))
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
    stats: dict = {
        "model": model,
        "total_pages": total_pages,
        "pdf_first_page": page_numbers[0] if page_numbers else None,
        "pdf_last_page": page_numbers[-1] if page_numbers else None,
        "skip_front_pages": state.get("skip_front_pages", 0),
        "page_numbers": page_numbers,
        "pages_reconciled": 0,
        "pages_reconcile_skipped_whitespace": count_reconcile_whitespace_skips(
            work_dir, page_numbers
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
    lang_cfg = job_config_from_state(state)
    impossible = load_impossible_strings(lang_cfg.source_id, state)
    if not state.get("page_sections"):
        tag_page_sections_from_run1(work_dir, state)
        save_state(work_dir, state)

    reconcile_items = pages_needing_reconcile(work_dir, page_numbers)
    if reconcile_items and report:
        report("reconcile", 0, 0, total_pages, None, f"Reconciling {len(reconcile_items)} pages…")

    if reconcile_items:
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
            for line in _run_batch_custom(api_key, model, payload):
                cid, text, _ = parse_batch_result_line(line)
                if not cid or text is None:
                    continue
                try:
                    kind, _, page_num = parse_batch_custom_id(cid)
                except ValueError:
                    continue
                if kind != "reconcile":
                    continue
                body, uncertain, note = parse_reconcile_output(text)
                reconcile_page_path(work_dir, page_num).parent.mkdir(parents=True, exist_ok=True)
                reconcile_page_path(work_dir, page_num).write_text(body, encoding="utf-8")
                mark_special_page_complete(state, "reconcile", page_num)
                log_entries.append(
                    {
                        "page": page_num,
                        "disagreement": "run1 and run2 differ (content)",
                        "resolution": "batch reconcile from image",
                        "uncertain": uncertain,
                        "uncertain_note": note,
                    }
                )
                if uncertain:
                    stats["pages_uncertain"] += 1
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
                    body, uncertain, note = parse_reconcile_output(raw)
                except RuntimeError as exc:
                    body, uncertain, note = f"[Reconcile failed: {exc}]", True, str(exc)
                reconcile_page_path(work_dir, item.page_num).parent.mkdir(parents=True, exist_ok=True)
                reconcile_page_path(work_dir, item.page_num).write_text(body, encoding="utf-8")
                mark_special_page_complete(state, "reconcile", item.page_num)
                log_entries.append(
                    {
                        "page": item.page_num,
                        "disagreement": "run1 and run2 differ (content)",
                        "resolution": "live reconcile from image",
                        "uncertain": uncertain,
                        "uncertain_note": note,
                    }
                )
                if uncertain:
                    stats["pages_uncertain"] += 1
                    stats["human_review_pages"].append(item.page_num)
                stats["pages_reconciled"] += 1
                time.sleep(API_DELAY_SEC)
        save_state(work_dir, state)

    write_reconcile_log(work_dir, log_entries)

    if spot_check_enabled:
        patch_requests = collect_spot_patch_requests(work_dir, page_numbers, state, lang_cfg)
        pages_with_spot = sorted({r.page_num for r in patch_requests})
        if patch_requests and report:
            report(
                "spot_check",
                0,
                0,
                total_pages,
                None,
                f"Spot patches: {len(patch_requests)} sentences on {len(pages_with_spot)} pages…",
            )
        if patch_requests:
            if use_batch:
                payload = [
                    {
                        "custom_id": spot_check_custom_id(
                            req.page_num, req.operation.op_index
                        ),
                        "params": build_spot_patch_params(
                            req.image_path,
                            model,
                            req.operation.sentence,
                            list(req.operation.terms),
                            lang_cfg,
                            section_hint=page_section_hint(state, req.page_num),
                        ),
                    }
                    for req in patch_requests
                ]
                results_by_page: dict[int, list[tuple[int, str]]] = {}
                for line in _run_batch_custom(api_key, model, payload):
                    cid, text, _ = parse_batch_result_line(line)
                    if not cid or text is None:
                        continue
                    try:
                        kind, op_idx, page_num = parse_batch_custom_id(cid)
                    except ValueError:
                        continue
                    if kind != "spot":
                        continue
                    results_by_page.setdefault(page_num, []).append(
                        (op_idx, normalize_transcription_output(text))
                    )
                for page_num in pages_with_spot:
                    base = base_page_body(work_dir, page_num)
                    ops = spot_patch_operations_for_page(base, lang_cfg, state)
                    page_results = sorted(results_by_page.get(page_num, []))
                    responses = [t for _, t in page_results]
                    if len(responses) < len(ops):
                        responses.extend([""] * (len(ops) - len(responses)))
                    applied, rejected, needs_review = _save_spot_patched_page(
                        work_dir, page_num, base, ops, responses, lang_cfg, impossible
                    )
                    mark_special_page_complete(state, "spot_check", page_num)
                    stats["pages_spot_checked"] += 1
                    stats["spot_patches_applied"] += applied
                    stats["spot_patches_rejected"] += rejected
                    if needs_review:
                        stats["spot_missing_term_reviews"] += 1
                        stats["human_review_pages"].append(page_num)
            else:
                by_page: dict[int, list[SpotPatchRequest]] = {}
                for req in patch_requests:
                    by_page.setdefault(req.page_num, []).append(req)
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
                    base = base_page_body(work_dir, page_num)
                    reqs = sorted(by_page[page_num], key=lambda r: r.operation.op_index)
                    responses: list[str] = []
                    for req in reqs:
                        try:
                            raw = _post_message(
                                api_key,
                                build_spot_patch_params(
                                    req.image_path,
                                    model,
                                    req.operation.sentence,
                                    list(req.operation.terms),
                                    lang_cfg,
                                    section_hint=page_section_hint(state, page_num),
                                ),
                            )
                            responses.append(normalize_transcription_output(raw))
                        except RuntimeError:
                            responses.append(req.operation.sentence)
                        time.sleep(API_DELAY_SEC)
                    ops = [r.operation for r in reqs]
                    applied, rejected, needs_review = _save_spot_patched_page(
                        work_dir, page_num, base, ops, responses, lang_cfg, impossible
                    )
                    mark_special_page_complete(state, "spot_check", page_num)
                    stats["pages_spot_checked"] += 1
                    stats["spot_patches_applied"] += applied
                    stats["spot_patches_rejected"] += rejected
                    if needs_review:
                        stats["spot_missing_term_reviews"] += 1
                        stats["human_review_pages"].append(page_num)
            save_state(work_dir, state)

        # Pages with hard terms but no extractable sentences — mark done; flag for review.
        for page_num in page_numbers:
            if page_num in completed_special_pages(state, "spot_check"):
                continue
            base = base_page_body(work_dir, page_num)
            if is_skip_body(base):
                continue
            terms = effective_hard_terms(base, lang_cfg, state)
            if not page_has_hard_term(base, terms):
                continue
            ops = spot_patch_operations_for_page(base, lang_cfg, state)
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
