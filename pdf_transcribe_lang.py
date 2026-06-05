"""Language and per-source config for PDF Transcribe pipeline v2."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
LANGUAGES_FILE = CONFIG_DIR / "transcribe_languages.json"

VALID_LANGUAGES = frozenset({"spanish", "nahuatl", "korean", "arabic", "japanese"})
VALID_SCRIPTS = frozenset({"latin", "arabic", "korean", "japanese", "chinese"})
DEFAULT_LANGUAGE = "spanish"
DEFAULT_SOURCE_ID = "ixtlilxochitl"
DEFAULT_SCRIPT = "latin"

_LANGUAGE_DEFAULT_SCRIPT = {
    "spanish": "latin",
    "nahuatl": "latin",
    "korean": "korean",
    "arabic": "arabic",
    "japanese": "japanese",
}

# Legacy default file — redirects to Ixtlilxóchitl list when no source_id set.
LEGACY_HARD_TERMS_FILE = CONFIG_DIR / "hard_terms.txt"

_SUPERSCRIPT_CHARS = "¹²³⁴⁵⁶⁷⁸⁹⁰"
_JA_TERMINATORS = "。、！？"
_CJK_NO_SPACE = frozenset({"japanese", "korean"})


@dataclass(frozen=True)
class JobLanguageConfig:
    language: str
    source_id: str
    script: str
    direction: str
    hyphenation_join: bool
    emphasis: str
    normalization_rule: str

    @property
    def is_rtl(self) -> bool:
        return self.direction == "rtl"

    @property
    def uses_cjk_boundaries(self) -> bool:
        return self.language in _CJK_NO_SPACE


def _load_language_catalog() -> dict:
    if LANGUAGES_FILE.is_file():
        return json.loads(LANGUAGES_FILE.read_text(encoding="utf-8"))
    return {}


def normalize_language(value: str | None) -> str:
    lang = (value or DEFAULT_LANGUAGE).strip().lower()
    return lang if lang in VALID_LANGUAGES else DEFAULT_LANGUAGE


def normalize_source_id(value: str | None) -> str:
    sid = (value or DEFAULT_SOURCE_ID).strip().lower().replace(" ", "_")
    return sid or DEFAULT_SOURCE_ID


def normalize_script(value: str | None = None, language: str | None = None) -> str:
    raw = (value or "").strip().lower()
    if raw in VALID_SCRIPTS:
        return raw
    lang = normalize_language(language) if language else DEFAULT_LANGUAGE
    catalog = _load_language_catalog()
    meta = catalog.get(lang, {})
    return str(meta.get("script") or _LANGUAGE_DEFAULT_SCRIPT.get(lang, DEFAULT_SCRIPT))


def job_language_config(
    language: str | None = None,
    source_id: str | None = None,
    script: str | None = None,
) -> JobLanguageConfig:
    lang = normalize_language(language)
    sid = normalize_source_id(source_id)
    catalog = _load_language_catalog()
    meta = catalog.get(lang, {})
    return JobLanguageConfig(
        language=lang,
        source_id=sid,
        script=normalize_script(script or meta.get("script"), lang),
        direction=str(meta.get("direction", "ltr")),
        hyphenation_join=bool(meta.get("hyphenation_join", lang in ("spanish", "nahuatl"))),
        emphasis=str(meta.get("emphasis", "asterisk")),
        normalization_rule=str(
            meta.get(
                "normalization_rule",
                "Transcribe exactly what is printed; do not normalize or modernize.",
            )
        ),
    )


def _read_term_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def hard_terms_path(source_id: str) -> Path:
    return CONFIG_DIR / f"hard_terms_{source_id}.txt"


def impossible_strings_path(source_id: str) -> Path:
    return CONFIG_DIR / f"impossible_strings_{source_id}.txt"


def load_hard_terms(source_id: str | None = None) -> list[str]:
    sid = normalize_source_id(source_id)
    path = hard_terms_path(sid)
    terms = _read_term_lines(path)
    if terms:
        return terms
    return _read_term_lines(LEGACY_HARD_TERMS_FILE)


def load_impossible_strings(source_id: str | None = None) -> list[str]:
    sid = normalize_source_id(source_id)
    return _read_term_lines(impossible_strings_path(sid))


def list_source_ids() -> list[str]:
    found: list[str] = []
    for path in CONFIG_DIR.glob("hard_terms_*.txt"):
        name = path.stem.replace("hard_terms_", "", 1)
        if name:
            found.append(name)
    if not found:
        found.append(DEFAULT_SOURCE_ID)
    return sorted(set(found))


_SPANISH_COMMON = frozenset(
    {
        "biblioteca",
        "universidad",
        "imprenta",
        "documentos",
        "historiadores",
        "colección",
        "publicación",
        "manuscrito",
        "traducción",
        "apéndice",
        "introducción",
    }
)


def auto_hard_term_candidates(text: str, lang_cfg: JobLanguageConfig) -> list[str]:
    """Tokens ≥12 chars with no spaces that are not common source-language words."""
    if lang_cfg.uses_cjk_boundaries:
        return []
    tokens = re.findall(r"\S+", text)
    out: list[str] = []
    common = _SPANISH_COMMON if lang_cfg.language == "spanish" else frozenset()
    for tok in tokens:
        core = re.sub(r"^[*_]+|[*_]+$", "", tok)
        core = re.sub(r"[" + _SUPERSCRIPT_CHARS + r"]+$", "", core)
        if len(core) < 12 or " " in core:
            continue
        if core.lower() in common:
            continue
        if not re.search(r"[A-Za-zÀ-ÿĀāĒēĪīŌō]", core):
            continue
        out.append(core)
    return sorted(set(out))


def effective_hard_terms(text: str, lang_cfg: JobLanguageConfig) -> list[str]:
    base = load_hard_terms(lang_cfg.source_id)
    auto = auto_hard_term_candidates(text, lang_cfg)
    seen: set[str] = set()
    merged: list[str] = []
    for term in base + auto:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(term)
    return merged


def page_has_hard_term(text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    match_text = strip_for_term_match(text)
    lower = match_text.lower()
    return any(t.strip() and t.lower() in lower for t in terms)


def strip_for_term_match(text: str) -> str:
    """Strip superscripts before hard-term matching; preserve original for patching."""
    return re.sub(r"[" + _SUPERSCRIPT_CHARS + r"]+", "", text)


def strip_whitespace_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text)


def pages_need_content_reconcile(body1: str, body2: str) -> bool:
    """Binary: whitespace-stripped identical → skip reconcile; any content diff → reconcile."""
    if body1.strip() == body2.strip():
        return False
    return strip_whitespace_for_compare(body1) != strip_whitespace_for_compare(body2)
