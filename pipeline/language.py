"""Language configuration and non-primary segment detection."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LANG_CONFIG = ROOT / "config" / "languages.json"

SPANISH_HINTS = {
    "el", "la", "los", "las", "de", "del", "que", "y", "en", "un", "una", "por", "con",
    "su", "sus", "se", "al", "lo", "como", "pero", "mas", "más", "para", "es", "son",
    "fue", "ser", "esta", "este", "estos", "estas", "don", "doña", "donde", "cuando",
}

NAHUATL_PATTERN = re.compile(r"[āēīōū]|tl|tz|qu|tzin|catl|tlan", re.IGNORECASE)


def load_language_config(path: Path | None = None) -> dict:
    cfg_path = path or DEFAULT_LANG_CONFIG
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def looks_spanish(word: str) -> bool:
    w = word.lower().strip(".,;:!?\"'()[]")
    if not w:
        return True
    if w in SPANISH_HINTS:
        return True
    return bool(re.match(r"^[a-záéíóúüñ]+$", w))


def looks_indigenous(word: str, secondary_langs: list[str]) -> bool:
    if "nah" in secondary_langs or "ncx" in secondary_langs:
        if NAHUATL_PATTERN.search(word):
            return True
    return False


def apply_secondary_placeholders(text: str, lang_cfg: dict) -> tuple[str, list[dict]]:
    if lang_cfg.get("skip_dictionary_validation") or lang_cfg.get("indigenous_minority_mode"):
        return text, []
    if lang_cfg.get("extract_secondary", False):
        return text, []

    secondary = lang_cfg.get("secondary_languages", [])
    labels = lang_cfg.get("language_labels", {})
    default_label = labels.get("default_indigenous", "INDIGENOUS")
    nah_label = labels.get("nah", "NAHUATL")

    flags = []
    lines_out = []
    for line_idx, line in enumerate(text.splitlines()):
        tokens = re.findall(r"\S+|\s+", line)
        new_tokens = []
        for tok in tokens:
            if tok.isspace():
                new_tokens.append(tok)
                continue
            word = tok.strip()
            if looks_indigenous(word, secondary):
                placeholder = f"[{nah_label}]" if "nah" in secondary else f"[{default_label}]"
                flags.append({"line": line_idx, "word": word, "placeholder": placeholder})
                new_tokens.append(placeholder)
            else:
                new_tokens.append(tok)
        lines_out.append("".join(new_tokens))

    return "\n".join(lines_out), flags


def detect_language_switching(text: str, lang_cfg: dict) -> bool:
    if lang_cfg.get("skip_dictionary_validation") or lang_cfg.get("indigenous_minority_mode"):
        return False
    words = re.findall(r"\S+", text)
    if not words:
        return False
    secondary = lang_cfg.get("secondary_languages", [])
    indigenous_hits = sum(1 for w in words if looks_indigenous(w, secondary))
    spanish_hits = sum(1 for w in words if looks_spanish(w))
    return indigenous_hits >= 2 and spanish_hits >= 5
