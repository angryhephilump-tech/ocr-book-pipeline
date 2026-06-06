#!/usr/bin/env python3
"""
OCR Book Extraction Pipeline — main runner.

Usage:
  python ocr_book.py ./photos ./output
  python ocr_book.py ./photos ./output --project-config ./output/project.json
  python ocr_book.py book.pdf ./output

Original photos/PDFs are NEVER modified.
"""

from __future__ import annotations

from pipeline.paddle_env import configure_paddle_env

configure_paddle_env()

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2

from pipeline.consensus import (
    FlaggedSpan,
    analyze_runs,
    build_review_needed,
    consensus_to_json,
    flags_to_csv,
    pick_draft_text,
)
from pipeline.lang_catalog import effective_project_settings
from pipeline.language import (
    apply_secondary_placeholders,
    detect_language_switching,
    load_language_config,
)
from pipeline.gateway_client import commit_page_credit
from pipeline.ocr_engines import verify_deepseek_available
from pipeline.layout import IMAGE_MARKER, compose_page_text
from pipeline.ocr_engines import run_four_passes
from pipeline.paths import configure_runtime
from pipeline.pdf_loader import apply_page_range, collect_inputs
from pipeline.preprocess import light_preprocess, load_bgr
from pipeline.text_cleanup import normalize_line_breaks

configure_runtime()

ROOT = Path(__file__).resolve().parent


def setup_project_config(output_dir: Path, args) -> dict:
    project_path = output_dir / "project.json"
    if args.project_config and Path(args.project_config).exists():
        cfg = json.loads(Path(args.project_config).read_text(encoding="utf-8"))
    elif project_path.exists():
        cfg = json.loads(project_path.read_text(encoding="utf-8"))
    else:
        lang_cfg = load_language_config()
        cfg = {
            "book_title": args.title or "Untitled Book",
            "primary_language": lang_cfg.get("primary_language", "spa"),
            "secondary_language": lang_cfg.get("secondary_language"),
            "indigenous_minority_mode": lang_cfg.get("indigenous_minority_mode", False),
            "confidence_threshold": lang_cfg.get("confidence_threshold", 85),
        }
        if not args.no_interactive and sys.stdin.isatty():
            print("\n=== New book project setup ===")
            cfg["book_title"] = input(f"Book title [{cfg['book_title']}]: ").strip() or cfg["book_title"]
            extract = input("Extract indigenous/secondary language text? (y/N): ").strip().lower()
            cfg["extract_secondary"] = extract in ("y", "yes")
        project_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"Saved project config: {project_path}")
    return cfg


def _emit_progress(on_progress, event: dict) -> None:
    if on_progress:
        on_progress(event)


def process_page(
    page_num: int,
    image_path: Path,
    output_dir: Path,
    project_cfg: dict,
    lang_cfg: dict,
    license_key: str,
    *,
    page_total: int = 0,
    on_progress=None,
) -> dict:
    page_id = f"page_{page_num:03d}"
    print(f"Processing {page_id}: {image_path.name}")

    base = {
        "page_current": page_num,
        "page_total": page_total,
        "filename": image_path.name,
        "page_id": page_id,
    }
    _emit_progress(on_progress, {**base, "stage": "load_image", "message": "Loading page image…"})
    original = load_bgr(str(image_path))
    _emit_progress(on_progress, {**base, "stage": "preprocess", "message": "Preprocessing scan…"})
    preprocessed = light_preprocess(original.copy())

    settings = effective_project_settings(project_cfg, lang_cfg)
    primary_lang = settings["primary_language"]
    secondary_lang = settings.get("secondary_language")
    indigenous_mode = bool(settings.get("indigenous_minority_mode", False))
    threshold = float(settings["confidence_threshold"])

    def on_pass(evt: dict) -> None:
        _emit_progress(on_progress, {**base, **evt})

    _emit_progress(
        on_progress,
        {**base, "stage": "ocr_passes", "message": "Running 3 OCR passes (A–C)…"},
    )
    runs = run_four_passes(
        original,
        preprocessed,
        license_key,
        primary_lang,
        secondary_lang=secondary_lang,
        indigenous_mode=indigenous_mode,
        on_pass=on_pass if on_progress else None,
    )

    for run_id, result in runs.items():
        (output_dir / f"{page_id}_run{run_id}.txt").write_text(result.full_text, encoding="utf-8")

    composed, layout_meta = compose_page_text(original, runs["A"])
    draft_raw = composed if composed.strip() else pick_draft_text(runs)
    draft_raw = normalize_line_breaks(draft_raw)
    merged_lang = {**lang_cfg, **settings}
    draft, secondary_flags = apply_secondary_placeholders(draft_raw, merged_lang)

    extra_flags: list[FlaggedSpan] = []

    if layout_meta.get("has_footnotes"):
        extra_flags.append(
            FlaggedSpan(
                span_id=f"{page_id}_footnote",
                line_index=0,
                word_index=0,
                char_start=0,
                char_end=0,
                text="[FOOTNOTE REGION]",
                reason="footnote_region",
            )
        )

    if layout_meta.get("image_regions"):
        for idx, bbox in enumerate(layout_meta["image_regions"]):
            extra_flags.append(
                FlaggedSpan(
                    span_id=f"{page_id}_image_{idx}",
                    line_index=0,
                    word_index=idx,
                    char_start=0,
                    char_end=len(IMAGE_MARKER),
                    text=IMAGE_MARKER,
                    reason="image_placeholder",
                    bbox=bbox,
                )
            )

    if detect_language_switching(draft, merged_lang):
        extra_flags.append(
            FlaggedSpan(
                span_id=f"{page_id}_lang_switch",
                line_index=0,
                word_index=0,
                char_start=0,
                char_end=0,
                text="[LANGUAGE SWITCH]",
                reason="complex_layout_language_switch",
            )
        )

    for sf in secondary_flags:
        extra_flags.append(
            FlaggedSpan(
                span_id=f"{page_id}_secondary_{sf['line']}_{sf['word'][:8]}",
                line_index=sf["line"],
                word_index=0,
                char_start=0,
                char_end=len(sf["word"]),
                text=sf["word"],
                reason="secondary_language_segment",
            )
        )

    _emit_progress(
        on_progress,
        {**base, "stage": "consensus", "message": "Building consensus and flags…"},
    )
    flags, stats = analyze_runs(runs, threshold, extra_flags)

    (output_dir / f"{page_id}_draft.txt").write_text(draft, encoding="utf-8")
    consensus_to_json(str(output_dir / f"{page_id}_consensus.json"), page_id, flags, stats, layout_meta)
    flags_to_csv(str(output_dir / f"{page_id}_review.csv"), flags)

    ref_image = output_dir / f"{page_id}_source.jpg"
    if not ref_image.exists():
        cv2.imwrite(str(ref_image), original)

    return {
        "page_id": page_id,
        "page_num": page_num,
        "source_image": str(ref_image.name),
        "original_path": str(image_path.resolve()),
        "flag_count": len(flags),
        "needs_review": len(flags) > 0,
        "stats": stats,
    }


def build_book_outputs(output_dir: Path, pages: list[dict]) -> None:
    draft_parts = []
    review_parts = []
    for p in pages:
        page_id = p["page_id"]
        draft_path = output_dir / f"{page_id}_draft.txt"
        if draft_path.exists():
            draft_parts.append(draft_path.read_text(encoding="utf-8"))
        if p["needs_review"]:
            consensus = json.loads((output_dir / f"{page_id}_consensus.json").read_text(encoding="utf-8"))
            from pipeline.consensus import FlaggedSpan

            flags = [
                FlaggedSpan(
                    span_id=f["span_id"],
                    line_index=f["line_index"],
                    word_index=f["word_index"],
                    char_start=f["char_start"],
                    char_end=f["char_end"],
                    text=f["text"],
                    reason=f["reason"],
                    engine_texts=f.get("engine_texts", {}),
                    engine_confidences=f.get("engine_confidences", {}),
                )
                for f in consensus.get("flags", [])
            ]
            review_parts.append(build_review_needed(flags, draft_path.read_text(encoding="utf-8")))

    (output_dir / "book_draft.txt").write_text("\n\n--- PAGE BREAK ---\n\n".join(draft_parts), encoding="utf-8")
    (output_dir / "book_review_needed.txt").write_text("\n\n".join(review_parts), encoding="utf-8")


def save_project_languages(output_dir: Path, language_cfg: dict) -> Path:
    path = output_dir / "languages.json"
    path.write_text(json.dumps(language_cfg, indent=2), encoding="utf-8")
    return path


def load_project_languages(output_dir: Path) -> dict | None:
    path = output_dir / "languages.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def page_output_complete(output_dir: Path, page_num: int) -> bool:
    page_id = f"page_{page_num:03d}"
    return (output_dir / f"{page_id}_consensus.json").is_file()


def _state_path(output_dir: Path) -> Path:
    return output_dir / "project_state.json"


def load_project_state(output_dir: Path) -> dict:
    path = _state_path(output_dir)
    if not path.is_file():
        return {"completed_pages": {}, "last_completed_page": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"completed_pages": {}, "last_completed_page": 0}


def save_project_state(output_dir: Path, state: dict) -> None:
    _state_path(output_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_page_meta_from_output(
    page_num: int,
    image_path: Path,
    output_dir: Path,
) -> dict:
    page_id = f"page_{page_num:03d}"
    consensus_path = output_dir / f"{page_id}_consensus.json"
    consensus = json.loads(consensus_path.read_text(encoding="utf-8"))
    flags = consensus.get("flags", [])
    ref_image = output_dir / f"{page_id}_source.jpg"
    return {
        "page_id": page_id,
        "page_num": page_num,
        "source_image": str(ref_image.name) if ref_image.exists() else image_path.name,
        "original_path": str(image_path.resolve()),
        "flag_count": len(flags),
        "needs_review": len(flags) > 0,
        "stats": consensus.get("stats", {}),
        "skipped_resume": True,
    }


def run_pipeline(
    input_path: Path,
    output_dir: Path,
    *,
    title: str | None = None,
    project_config: Path | None = None,
    language_config: dict | None = None,
    no_interactive: bool = True,
    on_progress=None,
    resume: bool = True,
    license_key: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict:
    """Run the full OCR pipeline. Optional on_progress(event dict)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    def report(event: dict) -> None:
        if on_progress:
            on_progress(event)

    class _Args:
        pass

    args = _Args()
    args.project_config = str(project_config) if project_config else None
    args.title = title
    args.no_interactive = no_interactive

    lang_cfg = load_language_config()
    if language_config:
        save_project_languages(output_dir, language_config)
    elif load_project_languages(output_dir):
        lang_cfg = {**lang_cfg, **load_project_languages(output_dir)}

    project_cfg = setup_project_config(output_dir, args)
    settings = effective_project_settings({**lang_cfg, **project_cfg})
    project_cfg.update(settings)
    (output_dir / "project.json").write_text(json.dumps(project_cfg, indent=2), encoding="utf-8")

    active_license = (license_key or os.environ.get("ARCHIVE_LICENSE_KEY", "")).strip()
    if not active_license:
        raise ValueError("License key missing. Activate on first launch before transcription.")

    report({"stage": "engines", "message": "Verifying DeepSeek gateway…", "page_current": 0, "page_total": 0})
    verify_deepseek_available(active_license, settings["primary_language"])
    lang_cfg = {**lang_cfg, **settings}

    report({"stage": "inputs", "message": "Collecting page images…", "page_current": 0, "page_total": 0})
    inputs = collect_inputs(input_path)
    inputs = apply_page_range(inputs, page_start, page_end)
    if not inputs:
        raise ValueError(f"No images or PDF pages found in {input_path}")

    total = len(inputs)
    skipped = 0
    state = load_project_state(output_dir)
    completed = state.get("completed_pages", {}) if isinstance(state, dict) else {}
    if resume:
        skipped = sum(
            1
            for i in range(1, total + 1)
            if completed.get(f"page_{i:03d}") or page_output_complete(output_dir, i)
        )
    if skipped:
        report(
            {
                "stage": "resume",
                "message": f"Resuming — {skipped} of {total} pages already done",
                "page_current": skipped,
                "page_total": total,
                "skipped_pages": skipped,
            }
        )
    else:
        report({"stage": "ready", "message": f"Starting OCR on {total} pages…", "page_current": 0, "page_total": total})

    pages_meta: list[dict] = []
    pending_jobs: list[tuple[int, Path]] = []
    for i, img_path in enumerate(inputs, start=1):
        page_key = f"page_{i:03d}"
        if resume and (completed.get(page_key) or page_output_complete(output_dir, i)):
            meta = load_page_meta_from_output(i, img_path, output_dir)
            pages_meta.append(meta)
            report(
                {
                    "stage": "page_skip",
                    "page_current": i,
                    "page_total": total,
                    "filename": img_path.name,
                    "page_id": meta["page_id"],
                    "message": f"Skipped page {i} (already transcribed)",
                }
            )
            continue
        pending_jobs.append((i, img_path))

    page_workers = max(2, min(4, int(os.environ.get("ARCHIVE_PAGE_WORKERS", "2"))))
    with ThreadPoolExecutor(max_workers=page_workers) as pool:
        future_map = {}
        for page_num, img_path in pending_jobs:
            report(
                {
                    "stage": "page_start",
                    "page_current": page_num,
                    "page_total": total,
                    "filename": img_path.name,
                    "message": f"Transcribing page {page_num} of {total}",
                }
            )
            fut = pool.submit(
                process_page,
                page_num,
                img_path,
                output_dir,
                project_cfg,
                lang_cfg,
                active_license,
                page_total=total,
                on_progress=report if on_progress else None,
            )
            future_map[fut] = (page_num, img_path)

        for fut in as_completed(future_map):
            page_num, img_path = future_map[fut]
            meta = fut.result()
            commit_page_credit(
                license_key=active_license,
                page_id=meta["page_id"],
                idempotency_key=meta["page_id"],
                credits_used=1,
                details={"filename": img_path.name},
            )
            pages_meta.append(meta)
            page_id = str(meta["page_id"])
            state.setdefault("completed_pages", {})[page_id] = {
                "flag_count": int(meta.get("flag_count", 0)),
                "needs_review": bool(meta.get("needs_review", False)),
            }
            state["last_completed_page"] = max(int(state.get("last_completed_page", 0)), int(page_num))
            save_project_state(output_dir, state)
            report(
                {
                    "stage": "page_done",
                    "page_current": page_num,
                    "page_total": total,
                    "filename": img_path.name,
                    "page_id": meta["page_id"],
                    "flag_count": meta["flag_count"],
                    "message": f"Finished page {page_num} of {total}",
                }
            )

    pages_meta.sort(key=lambda p: p.get("page_num", 0))
    manifest = {
        "book_title": project_cfg.get("book_title"),
        "project": project_cfg,
        "total_pages": len(pages_meta),
        "flagged_pages": sum(1 for p in pages_meta if p["needs_review"]),
        "pages": pages_meta,
        "resumed_skipped_pages": skipped,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    build_book_outputs(output_dir, pages_meta)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR book extraction pipeline (4 runs per page)")
    parser.add_argument("input", help="Input folder of photos, single image, or PDF")
    parser.add_argument("output", help="Output folder")
    parser.add_argument("--project-config", help="Path to project.json")
    parser.add_argument("--title", help="Book title for new projects")
    parser.add_argument("--no-interactive", action="store_true", help="Skip project setup prompts")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)

    try:
        manifest = run_pipeline(
            input_path,
            output_dir,
            title=args.title,
            project_config=Path(args.project_config) if args.project_config else None,
            no_interactive=args.no_interactive,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    flagged = manifest["flagged_pages"]
    print(f"\nDone. {manifest['total_pages']} pages processed, {flagged} need review.")
    print(f"Launch review UI: python review_ui.py {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
