#!/usr/bin/env python3
"""Analyze run1 vs run2 disagreement and spot-check deltas for a transcribe output folder."""

from __future__ import annotations

import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = "[Skipped:"


def read_body(work_dir: Path, run: int, page: int) -> str:
    p = work_dir / f"run{run}" / "pages" / f"page_{page:04d}.txt"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def read_spot(work_dir: Path, page: int) -> str:
    p = work_dir / "spot_check" / "pages" / f"page_{page:04d}.txt"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def read_rec(work_dir: Path, page: int) -> str:
    p = work_dir / "reconcile" / "pages" / f"page_{page:04d}.txt"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def classify_pair(a: str, b: str) -> str:
    if a == b:
        return "identical"
    if re.sub(r"\s+", "", a) == re.sub(r"\s+", "", b):
        return "whitespace_only"
    pa = re.sub(r"[\s\W_]+", "", a, flags=re.UNICODE)
    pb = re.sub(r"[\s\W_]+", "", b, flags=re.UNICODE)
    if pa == pb:
        return "punctuation_or_symbols"
    if pa.lower() == pb.lower():
        return "accent_or_case"
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if r >= 0.99:
        return "tiny_edit"
    if r >= 0.95:
        return "minor_substantive"
    return "major_substantive"


def main() -> int:
    work_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / (
        "_pdf_transcribe_uploads/"
        "Texcoco_perspective._Nezahualcoyotl_the_philosopher-king_poet_transcribe_output"
    )
    st = json.loads((work_dir / "state.json").read_text(encoding="utf-8"))
    nums = st["page_numbers"]

    agree_pages = disagree_pages = skip_pages = 0
    char_ratios: list[float] = []
    pair_cat: Counter[str] = Counter()
    mism_words = 0
    total_w = 0

    for n in nums:
        t1, t2 = read_body(work_dir, 1, n), read_body(work_dir, 2, n)
        if not t1 or not t2:
            continue
        if t1.startswith(SKIP) and t2.startswith(SKIP):
            skip_pages += 1
            continue
        w1, w2 = t1.split(), t2.split()
        total_w += len(w1)
        if t1 == t2:
            agree_pages += 1
            continue
        disagree_pages += 1
        char_ratios.append(difflib.SequenceMatcher(None, t1, t2).ratio())
        sm = difflib.SequenceMatcher(None, w1, w2)
        matched = sum(b.size for b in sm.get_matching_blocks())
        mism_words += max(len(w1), len(w2)) - matched
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            a = " ".join(w1[i1:i2])
            b = " ".join(w2[j1:j2])
            pair_cat[classify_pair(a or " ", b or " ")] += 1

    content = agree_pages + disagree_pages
    print("=== PAGE LEVEL ===")
    print(f"content pages: {content}")
    print(f"exact agree: {agree_pages} ({100 * agree_pages / content:.1f}%)")
    print(f"disagree: {disagree_pages} ({100 * disagree_pages / content:.1f}%)")
    print(f"skipped both: {skip_pages}")
    print()
    print("=== CHARACTER LEVEL (disagree pages) ===")
    if char_ratios:
        print(f"avg char similarity: {sum(char_ratios) / len(char_ratios):.4f}")
        print(f"median char similarity: {sorted(char_ratios)[len(char_ratios) // 2]:.4f}")
        print(f"pages similarity < 0.95: {sum(1 for r in char_ratios if r < 0.95)}")
        print(f"pages similarity < 0.90: {sum(1 for r in char_ratios if r < 0.90)}")
    print()
    print("=== WORD LEVEL (all content pages) ===")
    print(f"run1 word count (whitespace tokens): {total_w:,}")
    print(f"word tokens differing between runs: {mism_words:,}")
    if total_w:
        print(f"fraction of words touched: {mism_words / total_w:.5f}")
        print(f"~1 differing token per {total_w / max(mism_words, 1):.0f} words")
    print()
    print("=== DISAGREEMENT SEGMENT TYPES (word-diff opcodes) ===")
    total_segs = sum(pair_cat.values()) or 1
    for k, v in pair_cat.most_common():
        print(f"  {k}: {v} ({100 * v / total_segs:.1f}%)")

    hard_path = ROOT / "config" / "hard_terms.txt"
    hard = []
    if hard_path.is_file():
        hard = [
            ln.strip().lower()
            for ln in hard_path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]

    spot_nums = sorted(
        int(p.stem.split("_")[1]) for p in (work_dir / "spot_check" / "pages").glob("page_*.txt")
    )
    print()
    print("=== SPOT-CHECK vs RUNS ===")
    spot_vs_r1: list[float] = []
    spot_vs_agreed: list[float] = []
    agreed_spot_changed = 0
    for n in spot_nums:
        s = read_spot(work_dir, n)
        t1, t2 = read_body(work_dir, 1, n), read_body(work_dir, 2, n)
        spot_vs_r1.append(difflib.SequenceMatcher(None, s, t1).ratio())
        if t1 == t2:
            r = difflib.SequenceMatcher(None, s, t1).ratio()
            spot_vs_agreed.append(r)
            if r < 0.995:
                agreed_spot_changed += 1

    print(f"spot pages: {len(spot_nums)}")
    print(f"spot vs run1 avg similarity: {sum(spot_vs_r1) / len(spot_vs_r1):.4f}")
    if spot_vs_agreed:
        print(f"pages where run1==run2: {len(spot_vs_agreed)}")
        print(f"spot vs agreed avg similarity: {sum(spot_vs_agreed) / len(spot_vs_agreed):.4f}")
        print(f"agreed pages where spot changed text (sim < 0.995): {agreed_spot_changed}")

    # Example diffs
    print()
    print("=== SAMPLE: page 14 first word diffs ===")
    t1, t2 = read_body(work_dir, 1, 14), read_body(work_dir, 2, 14)
    w1, w2 = t1.split(), t2.split()
    shown = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, w1, w2).get_opcodes():
        if tag == "equal":
            continue
        print(f"  {tag}: {w1[i1:i2]!r} | {w2[j1:j2]!r}")
        shown += 1
        if shown >= 6:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
