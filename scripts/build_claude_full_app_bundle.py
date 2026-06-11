"""Build a single text file with full app architecture + source for LLM review."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "CLAUDE_FULL_APP_BUNDLE.txt"
SEP = "=" * 72

DOCS = [
    ROOT / "docs" / "CLAUDE_ARCHITECTURE_REFERENCE.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "PDF_TRANSCRIBE_PIPELINE.md",
    ROOT / "README.md",
]

ROOT_PY = sorted(
    [
        ROOT / "ocr_book.py",
        ROOT / "review_ui.py",
        ROOT / "license.py",
        *ROOT.glob("pdf_transcribe*.py"),
    ]
)

PIPELINE_PY = sorted((ROOT / "pipeline").glob("*.py"))
GATEWAY_PY = sorted((ROOT / "gateway").glob("*.py"))

CONFIG = sorted(
    p
    for p in [
        ROOT / "config" / "product.json",
        ROOT / "config" / "gateway.json",
        ROOT / "config" / "languages.json",
        ROOT / "config" / "tesseract_languages.json",
        ROOT / "config" / "transcribe_languages.json",
        ROOT / "config" / "transcription_prompt.txt",
        ROOT / "config" / "hard_terms.txt",
        ROOT / "config" / "hard_terms_ixtlilxochitl.txt",
        *sorted((ROOT / "config" / "sources").rglob("*")),
        *sorted(ROOT.glob("config/hard_terms_*.txt")),
        *sorted(ROOT.glob("config/soft_terms_*.txt")),
        *sorted(ROOT.glob("config/impossible*.txt")),
    ]
    if p.is_file()
)

SCRIPTS = sorted(
    [
        ROOT / "scripts" / "test_transcribe_logic.py",
        ROOT / "scripts" / "finish_job.py",
        ROOT / "scripts" / "diagnose_spot_planning.py",
        ROOT / "scripts" / "save_claude_key.py",
        ROOT / "scripts" / "save_deepseek_key.py",
        ROOT / "scripts" / "bundle_transcribe_output.py",
        ROOT / "scripts" / "analyze_run_disagreement.py",
        ROOT / "scripts" / "audit_spot_pilot.py",
        ROOT / "scripts" / "find_pilot_pages.py",
        ROOT / "scripts" / "verify_engines.py",
    ]
)


def collect_paths() -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for group in (DOCS, ROOT_PY, PIPELINE_PY, GATEWAY_PY, CONFIG, SCRIPTS):
        for path in group:
            resolved = path.resolve()
            if resolved.is_file() and resolved not in seen:
                seen.add(resolved)
                ordered.append(path)
    return ordered


def main() -> None:
    all_paths = collect_paths()
    header = [
        "OCR BOOK PIPELINE — FULL APP ARCHITECTURE + SOURCE BUNDLE FOR LLM REVIEW",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Repository: {ROOT}",
        "",
        "Purpose: Drop this single file into Claude to analyze the entire codebase.",
        "Products: Archive Studios (DeepSeek OCR), PDF Transcribe (Claude vision),",
        "          Archive Gateway (license + credits proxy).",
        "",
        "Excluded: page images, work-dir outputs (_pdf_transcribe_uploads/, output/),",
        "          vendor binaries, HTML/CSS/JS templates, .env / API keys.",
        "",
        "Suggested use: attach this file, then ask Claude to review a module or bug",
        "by path (e.g. pdf_transcribe_finalize.py, pipeline/consensus.py).",
        "",
        "Regenerate: python scripts/build_claude_full_app_bundle.py",
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
