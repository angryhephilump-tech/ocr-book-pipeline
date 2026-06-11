"""Build a single text file with all PDF Transcribe source for LLM review."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "CLAUDE_PDF_TRANSCRIBE_SOURCE_BUNDLE.txt"

PY_FILES = sorted(ROOT.glob("pdf_transcribe*.py"))
EXTRA = [
    ROOT / "config" / "transcription_prompt.txt",
    ROOT / "config" / "transcribe_languages.json",
    ROOT / "docs" / "CLAUDE_ARCHITECTURE_REFERENCE.md",
]
SEP = "=" * 72


def main() -> None:
    all_paths = [p for p in PY_FILES + EXTRA if p.is_file()]
    header = [
        "PDF TRANSCRIBE — FULL SOURCE BUNDLE FOR LLM REVIEW",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Repository: {ROOT}",
        "",
        "Purpose: Drop this single file into Claude for code review, debugging, or feature design.",
        "Includes: all pdf_transcribe*.py modules, key config, and architecture reference.",
        "Excluded: page images, transcription outputs, vendor binaries, Archive Studios / Gateway code.",
        "",
        "FILE INDEX",
        "----------",
    ]
    for path in all_paths:
        header.append(f"  {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")
    header.append("")
    header.append(SEP)

    chunks: list[str] = ["\n".join(header) + "\n"]
    for path in all_paths:
        rel = path.relative_to(ROOT)
        chunks.append(f"\n{SEP}\nFILE: {rel}\n{SEP}\n\n")
        text = path.read_text(encoding="utf-8")
        chunks.append(text)
        if not text.endswith("\n"):
            chunks.append("\n")

    OUT.write_text("".join(chunks), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"Size: {OUT.stat().st_size:,} bytes ({len(all_paths)} files)")


if __name__ == "__main__":
    main()
