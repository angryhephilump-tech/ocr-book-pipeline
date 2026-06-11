#!/usr/bin/env python3
"""Per-term spot-patch stats and soft-term demotion candidates from a pilot run.

Usage:
  python scripts/term_tuning_report.py path/to/work_dir
  python scripts/term_tuning_report.py path/to/work_dir --min-checks 5
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe import load_state  # noqa: E402
from pdf_transcribe_finalize import spot_patch_log_path  # noqa: E402
from pdf_transcribe_source import load_soft_terms, resolve_source_slug  # noqa: E402

_TERM_RE = re.compile(r"\[([^\]]+)\]")
_OP_LINE_RE = re.compile(r"^\s*op\s+\d+:\s*(APPLIED|REJECTED)", re.IGNORECASE)
_REJECT_REASON_RE = re.compile(r"REJECTED\s*\(([^)]+)\)", re.IGNORECASE)


@dataclass
class TermStats:
    checked: int = 0
    applied: int = 0
    rejected_unchanged: int = 0
    rejected_review: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "applied": self.applied,
            "rejected_unchanged": self.rejected_unchanged,
            "rejected_review": self.rejected_review,
        }


def _terms_from_brackets(text: str) -> list[str]:
    m = _TERM_RE.search(text)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def parse_spot_patch_term_stats(log_text: str) -> dict[str, TermStats]:
    """Aggregate per-term stats from spot_patch_log.txt body."""
    stats: dict[str, TermStats] = defaultdict(TermStats)
    for line in log_text.splitlines():
        if "op " not in line:
            continue
        if not _OP_LINE_RE.search(line):
            continue
        terms = _terms_from_brackets(line)
        if not terms:
            continue
        is_applied = "APPLIED" in line
        reason = ""
        if not is_applied:
            rm = _REJECT_REASON_RE.search(line)
            reason = (rm.group(1) if rm else "").strip().lower()
        for term in terms:
            key = term.lower()
            entry = stats[key]
            entry.checked += 1
            if is_applied:
                entry.applied += 1
            elif reason == "unchanged":
                entry.rejected_unchanged += 1
            else:
                entry.rejected_review += 1
    return dict(stats)


def demotion_candidates(
    stats: dict[str, TermStats],
    *,
    min_checks: int = 5,
    existing_soft: set[str] | None = None,
) -> list[str]:
    """Terms with enough checks and zero applies — candidates for soft_terms."""
    soft = {t.lower() for t in (existing_soft or set())}
    out: list[str] = []
    for term, s in sorted(stats.items(), key=lambda kv: (-kv[1].checked, kv[0])):
        if s.checked >= min_checks and s.applied == 0 and term not in soft:
            out.append(term)
    return out


def format_soft_terms_block(terms: list[str]) -> str:
    if not terms:
        return "# (no new demotion candidates)"
    return "\n".join(sorted(terms, key=str.lower))


def run_report(work_dir: Path, *, min_checks: int = 5) -> int:
    work_dir = work_dir.resolve()
    log_path = spot_patch_log_path(work_dir)
    if not log_path.is_file():
        print(f"No spot_patch_log.txt in {work_dir}")
        return 1

    state = load_state(work_dir)
    slug = resolve_source_slug(state) or state.get("source_name") or "unknown"
    soft_existing = load_soft_terms(slug, state)
    stats = parse_spot_patch_term_stats(log_path.read_text(encoding="utf-8"))

    print("TERM TUNING REPORT")
    print(f"Work dir: {work_dir}")
    print(f"Source: {slug}")
    print(f"Min checks for demotion: {min_checks}")
    print()
    print(f"{'Term':<32} {'Checked':>8} {'Applied':>8} {'Unchg':>8} {'Review':>8}")
    print("-" * 72)
    for term in sorted(stats.keys(), key=lambda t: (-stats[t].checked, t)):
        s = stats[term]
        print(
            f"{term:<32} {s.checked:>8} {s.applied:>8} "
            f"{s.rejected_unchanged:>8} {s.rejected_review:>8}"
        )

    candidates = demotion_candidates(
        stats, min_checks=min_checks, existing_soft=soft_existing
    )
    print()
    print(f"Demotion candidates (≥{min_checks} checks, 0 applied, not already soft):")
    if candidates:
        for term in candidates:
            s = stats[term]
            print(f"  {term} — {s.checked} checks, {s.rejected_unchanged} unchanged, {s.rejected_review} review")
    else:
        print("  (none)")

    print()
    print("Paste block for soft_terms file:")
    print("---")
    print(format_soft_terms_block(candidates))
    print("---")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument(
        "--min-checks",
        type=int,
        default=5,
        help="Minimum spot-check attempts before demotion (default: 5)",
    )
    args = parser.parse_args()
    return run_report(args.work_dir, min_checks=max(1, args.min_checks))


if __name__ == "__main__":
    raise SystemExit(main())
