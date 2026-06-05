#!/usr/bin/env python3
"""Find PDF page numbers for a 20-page spot-check pilot from existing transcription output.

Scans run1 page files for four edge-case patterns, then fills remaining slots with
other hard-term pages.

Usage:
  python scripts/find_pilot_pages.py "path/to/book_transcribe_output"
  python scripts/find_pilot_pages.py "path/to/book_transcribe_output" --count 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe_lang import (  # noqa: E402
    effective_hard_terms,
    job_language_config,
    load_hard_terms,
    page_has_hard_term,
)
from pdf_transcribe_spot import FOOTNOTE_SEP  # noqa: E402

HARD_TERMS = load_hard_terms("ixtlilxochitl")
LANG = job_language_config("spanish", "ixtlilxochitl")


def read_page_body(work_dir: Path, page: int) -> str:
    for sub in ("run1/pages", "run2/pages"):
        path = work_dir / sub / f"page_{page:04d}.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return ""


def list_content_pages(work_dir: Path) -> list[int]:
    pages_dir = work_dir / "run1" / "pages"
    if not pages_dir.is_dir():
        return []
    out: list[int] = []
    for path in pages_dir.glob("page_*.txt"):
        page = int(path.stem.split("_")[1])
        text = path.read_text(encoding="utf-8").strip()
        if text and not text.startswith("[Skipped:"):
            out.append(page)
    return sorted(out)


def classify_page(text: str) -> set[str]:
    tags: set[str] = set()
    if not text or text.startswith("[Skipped:"):
        return tags

    terms = effective_hard_terms(text, LANG)
    if not page_has_hard_term(text, terms):
        return tags

    # Hyphenated name across line break (Latin)
    for term in HARD_TERMS:
        if len(term) < 6:
            continue
        parts = term.split()
        if len(parts) == 1 and re.search(
            rf"{re.escape(term[: max(4, len(term)//2)])}-\s*\n", text, re.IGNORECASE
        ):
            tags.add("hyphen_break")
            break
        if re.search(r"-\s*\n", text) and term.lower() in text.lower():
            tags.add("hyphen_break")
            break

    if FOOTNOTE_SEP in text:
        body, foot = text.split(FOOTNOTE_SEP, 1)
        body_hit = page_has_hard_term(body, terms)
        foot_hit = page_has_hard_term(foot, terms)
        if body_hit and foot_hit:
            tags.add("body_and_footnote")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines[:8]:
        if len(line) < 120 and any(t.lower() in line.lower() for t in HARD_TERMS):
            if line.endswith(".") or line.endswith(":"):
                words = line.split()
                if len(words) <= 12:
                    tags.add("chapter_heading")
                    break

    logical = re.sub(r"\n(?!\n)", " ", text)
    hits = sum(1 for t in HARD_TERMS if t.lower() in logical.lower())
    if hits >= 2:
        for term in HARD_TERMS:
            if term.lower() not in logical.lower():
                continue
            chunk = logical[max(0, logical.lower().find(term.lower()) - 200) :]
            others = sum(
                1 for t in HARD_TERMS if t != term and t.lower() in chunk.lower()
            )
            if others:
                tags.add("multi_term_sentence")
                break

    return tags


def pick_pilot_pages(work_dir: Path, count: int = 20) -> dict:
    required = [
        "hyphen_break",
        "body_and_footnote",
        "chapter_heading",
        "multi_term_sentence",
    ]
    all_pages = list_content_pages(work_dir)
    by_tag: dict[str, list[int]] = {t: [] for t in required}
    hard_pages: list[int] = []

    for page in all_pages:
        text = read_page_body(work_dir, page)
        tags = classify_page(text)
        terms = effective_hard_terms(text, LANG)
        if page_has_hard_term(text, terms):
            hard_pages.append(page)
        for tag in tags:
            by_tag.setdefault(tag, []).append(page)

    chosen: list[int] = []
    chosen_tags: dict[str, int] = {}

    for tag in required:
        for page in by_tag.get(tag, []):
            if page not in chosen:
                chosen.append(page)
                chosen_tags[tag] = page
                break

    for page in hard_pages:
        if len(chosen) >= count:
            break
        if page not in chosen:
            chosen.append(page)

    for page in all_pages:
        if len(chosen) >= count:
            break
        if page not in chosen:
            chosen.append(page)

    return {
        "work_dir": str(work_dir),
        "pilot_pages": chosen[:count],
        "edge_cases_found": chosen_tags,
        "edge_cases_missing": [t for t in required if t not in chosen_tags],
        "pages_command": ",".join(str(p) for p in chosen[:count]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path, help="Existing *_transcribe_output folder")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()
    work_dir = args.work_dir.resolve()
    if not work_dir.is_dir():
        print(f"Not a directory: {work_dir}", file=sys.stderr)
        return 1

    result = pick_pilot_pages(work_dir, count=args.count)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("PILOT PAGE FINDER")
    print(f"Source: {result['work_dir']}")
    print()
    print("Edge cases located:")
    for tag, page in result["edge_cases_found"].items():
        print(f"  {tag}: PDF page {page}")
    if result["edge_cases_missing"]:
        print()
        print("Edge cases NOT found in corpus (pick manually from PDF):")
        for tag in result["edge_cases_missing"]:
            print(f"  - {tag}")
    print()
    print(f"Suggested {len(result['pilot_pages'])} pilot pages:")
    print(result["pages_command"])
    print()
    print("Run pilot (live API, Pipeline v2):")
    pdf = work_dir.parent / (work_dir.name.replace("_transcribe_output", "") + ".pdf")
    if not pdf.is_file():
        pdfs = list(work_dir.parent.glob("*.pdf"))
        pdf = pdfs[0] if pdfs else pdf
    print(
        f'  python pdf_transcribe.py "{pdf}" --pages {result["pages_command"]} '
        f'--processing realtime --output-dir "{work_dir}_pilot_v2"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
