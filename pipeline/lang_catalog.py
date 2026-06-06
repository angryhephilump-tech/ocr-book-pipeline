"""Tesseract language catalog and project language helpers."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "config" / "tesseract_languages.json"

# Bundled in installer (top 20)
BUNDLED_LANG_CODES = (
    "spa",
    "eng",
    "fra",
    "deu",
    "por",
    "ita",
    "nld",
    "rus",
    "ara",
    "chi_sim",
    "chi_tra",
    "jpn",
    "kor",
    "hin",
    "pol",
    "tur",
    "vie",
    "ind",
    "ron",
    "swe",
)


def load_catalog() -> dict:
    if CATALOG_PATH.is_file():
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {"languages": []}


def list_languages() -> list[dict]:
    return load_catalog().get("languages", [])


def language_name(code: str) -> str:
    for lang in list_languages():
        if lang.get("code") == code:
            return lang.get("name", code)
    return code


def effective_project_settings(project_cfg: dict, base_lang_cfg: dict | None = None) -> dict:
    """Merge project + defaults; apply indigenous-minority mode rules."""
    base = dict(base_lang_cfg or load_catalog().get("defaults", {}))
    out = {**base, **project_cfg}
    primary = (out.get("primary_language") or "spa").strip()
    secondary = (out.get("secondary_language") or "").strip() or None
    if secondary == primary:
        secondary = None
    indigenous = bool(out.get("indigenous_minority_mode", False))
    threshold = float(out.get("confidence_threshold", base.get("confidence_threshold", 85)))
    if indigenous:
        threshold = float(out.get("indigenous_confidence_threshold", 65))
    out["primary_language"] = primary
    out["secondary_language"] = secondary
    out["indigenous_minority_mode"] = indigenous
    out["confidence_threshold"] = threshold
    # When indigenous mode: never treat unknown words as Spanish errors
    out["skip_dictionary_validation"] = indigenous
    out["extract_secondary"] = not indigenous
    return out
