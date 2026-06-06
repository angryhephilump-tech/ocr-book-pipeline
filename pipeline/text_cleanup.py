"""Normalize OCR text line breaks without destroying structure."""

from __future__ import annotations


def normalize_line_breaks(text: str) -> str:
    if not text.strip():
        return text

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            out.append("")
            i += 1
            continue

        # Keep likely list/poetry lines as-is.
        if line.lstrip().startswith(("-", "*", "•")):
            out.append(line)
            i += 1
            continue

        merged = line
        while i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if not nxt:
                break
            if merged.endswith("-") and merged[-2:-1].isalpha():
                merged = merged[:-1] + nxt
            elif merged.endswith((".", "!", "?", ":", ";")):
                break
            else:
                merged = f"{merged} {nxt}"
            i += 1
        out.append(merged)
        i += 1
    return "\n".join(out)

