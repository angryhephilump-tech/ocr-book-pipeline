"""Export reviewed book to PDF and plain text."""

from __future__ import annotations

import json
from pathlib import Path

from fpdf import FPDF

from pipeline.layout import FOOTNOTE_END, FOOTNOTE_START


class BookPDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def _split_footnotes(text: str) -> list[tuple[str, str]]:
    parts = []
    remaining = text
    while FOOTNOTE_START in remaining:
        before, rest = remaining.split(FOOTNOTE_START, 1)
        if before.strip():
            parts.append(("body", before.strip()))
        if FOOTNOTE_END in rest:
            foot, remaining = rest.split(FOOTNOTE_END, 1)
            parts.append(("footnote", foot.strip()))
        else:
            parts.append(("footnote", rest.strip()))
            remaining = ""
    if remaining.strip():
        parts.append(("body", remaining.strip()))
    return parts


def export_pdf(pages: list[dict], output_path: Path, title: str = "OCR Book Export") -> None:
    pdf = BookPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, title)
    pdf.ln(4)

    for page in pages:
        text = page.get("reviewed_text") or page.get("draft_text") or ""
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, f"Source page {page.get('page_num', '?')}", ln=True)
        pdf.ln(2)
        for kind, block in _split_footnotes(text):
            if kind == "footnote":
                pdf.set_font("Helvetica", "I", 9)
                pdf.set_text_color(80, 80, 80)
                pdf.multi_cell(0, 5, "--- Footnote ---")
                pdf.multi_cell(0, 5, block)
                pdf.set_text_color(0, 0, 0)
            else:
                pdf.set_font("Helvetica", "", 11)
                pdf.multi_cell(0, 6, block)
        pdf.ln(2)

    pdf.output(str(output_path))


def export_plain_text(pages: list[dict], output_path: Path) -> None:
    chunks = []
    for page in pages:
        num = page.get("page_num", "?")
        text = page.get("reviewed_text") or page.get("draft_text") or ""
        chunks.append(f"\n\n===== PAGE {num} =====\n\n{text}")
    output_path.write_text("\n".join(chunks).strip() + "\n", encoding="utf-8")


def load_pages_for_export(output_dir: Path) -> list[dict]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pages = []
    for entry in manifest.get("pages", []):
        page_id = entry["page_id"]
        reviewed = output_dir / f"{page_id}_reviewed.txt"
        draft = output_dir / f"{page_id}_draft.txt"
        text = reviewed.read_text(encoding="utf-8") if reviewed.exists() else ""
        if not text and (output_dir / f"{page_id}_draft.txt").exists():
            text = draft.read_text(encoding="utf-8")
        pages.append({"page_num": entry.get("page_num"), "page_id": page_id, "reviewed_text": text})
    return pages
