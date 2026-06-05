"""Bundle all text outputs from a pdf_transcribe work dir into one file (no PDF/images)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {"images"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
ROOT_ORDER = [
    "run_summary.txt",
    "run_summary.json",
    "reconcile_log.txt",
    "differences.txt",
    "transcribed.txt",
    "run1.txt",
    "run2.txt",
    "progress.json",
    "state.json",
    "batch_state.json",
]


def should_include(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    return path.is_file()


def rel(work_dir: Path, path: Path) -> str:
    return path.relative_to(work_dir).as_posix()


def sorted_paths(work_dir: Path) -> list[Path]:
    all_files = [p for p in work_dir.rglob("*") if should_include(p)]
    root = {p.name: p for p in all_files if p.parent == work_dir}
    ordered: list[Path] = []
    for name in ROOT_ORDER:
        if name in root:
            ordered.append(root.pop(name))
    ordered.extend(sorted(root.values(), key=lambda p: p.name.lower()))
    rest = [p for p in all_files if p.parent != work_dir]
    rest.sort(key=lambda p: rel(work_dir, p).lower())
    return ordered + rest


def bundle(work_dir: Path, out_path: Path) -> None:
    work_dir = work_dir.resolve()
    paths = sorted_paths(work_dir)
    lines: list[str] = [
        "=" * 80,
        "PDF TRANSCRIBE — FULL TEXT BUNDLE",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Source folder: {work_dir}",
        "Included: all text/json outputs from the transcription job",
        "Excluded: PDF source, page images (images/*.png), this bundle file itself",
        f"Files bundled: {len(paths)}",
        "=" * 80,
        "",
    ]
    for path in paths:
        if path.resolve() == out_path.resolve():
            continue
        name = rel(work_dir, path)
        lines.extend(
            [
                "",
                "=" * 80,
                f"FILE: {name}",
                "=" * 80,
                "",
            ]
        )
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        lines.append(text.rstrip("\n"))
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path, help="*_transcribe_output folder")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file (default: <work_dir>/full_transcribe_bundle.txt)",
    )
    args = parser.parse_args()
    out = args.output or (args.work_dir / "full_transcribe_bundle.txt")
    bundle(args.work_dir, out)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"Wrote {out} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
