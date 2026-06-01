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

import argparse
import json
import sys
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
from pipeline.language import (
    apply_secondary_placeholders,
    detect_language_switching,
    load_language_config,
)
from pipeline.layout import IMAGE_MARKER, compose_page_text
from pipeline.ocr_engines import run_four_passes
from pipeline.pdf_loader import collect_inputs
from pipeline.preprocess import light_preprocess, load_bgr

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
            "secondary_languages": lang_cfg.get("secondary_languages", ["nah"]),
            "extract_secondary": lang_cfg.get("extract_secondary", False),
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


def process_page(
    page_num: int,
    image_path: Path,
    output_dir: Path,
    project_cfg: dict,
    lang_cfg: dict,
) -> dict:
    page_id = f"page_{page_num:03d}"
    print(f"Processing {page_id}: {image_path.name}")

    original = load_bgr(str(image_path))
    preprocessed = light_preprocess(original.copy())

    primary_lang = project_cfg.get("primary_language", "spa")
    threshold = float(project_cfg.get("confidence_threshold", lang_cfg.get("confidence_threshold", 85)))

    runs = run_four_passes(original, preprocessed, primary_lang)

    for run_id, result in runs.items():
        (output_dir / f"{page_id}_run{run_id}.txt").write_text(result.full_text, encoding="utf-8")

    composed, layout_meta = compose_page_text(original, runs["A"])
    draft_raw = composed if composed.strip() else pick_draft_text(runs)
    draft, secondary_flags = apply_secondary_placeholders(draft_raw, {**lang_cfg, **project_cfg})

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

    if detect_language_switching(draft, {**lang_cfg, **project_cfg}):
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


def run_pipeline(
    input_path: Path,
    output_dir: Path,
    *,
    title: str | None = None,
    project_config: Path | None = None,
    no_interactive: bool = True,
    on_progress=None,
) -> dict:
    """Run the full OCR pipeline. Optional on_progress(current, total, filename)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    class _Args:
        pass

    args = _Args()
    args.project_config = str(project_config) if project_config else None
    args.title = title
    args.no_interactive = no_interactive

    project_cfg = setup_project_config(output_dir, args)
    lang_cfg = load_language_config()

    inputs = collect_inputs(input_path)
    if not inputs:
        raise ValueError(f"No images or PDF pages found in {input_path}")

    total = len(inputs)
    pages_meta = []
    for i, img_path in enumerate(inputs, start=1):
        if on_progress:
            on_progress(i, total, img_path.name)
        else:
            print(f"Processing page {i}/{total}: {img_path.name}")
        meta = process_page(i, img_path, output_dir, project_cfg, lang_cfg)
        pages_meta.append(meta)
        if not on_progress:
            print(f"  {meta['page_id']}: {meta['flag_count']} flag(s)")

    manifest = {
        "book_title": project_cfg.get("book_title"),
        "project": project_cfg,
        "total_pages": len(pages_meta),
        "flagged_pages": sum(1 for p in pages_meta if p["needs_review"]),
        "pages": pages_meta,
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
