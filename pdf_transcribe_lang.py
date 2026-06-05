"""Language and per-source config for PDF Transcribe pipeline v2."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
LANGUAGES_FILE = CONFIG_DIR / "transcribe_languages.json"

VALID_LANGUAGES = frozenset(
    {"spanish", "nahuatl", "korean", "arabic", "japanese", "kaqchikel", "yucatec_maya", "classical_maya"}
)
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
_CJK_NO_SPACE = frozenset({"japanese", "korean", "chinese"})

NORMALIZATION_RULES: dict[str, str] = {
    "spanish": (
        "Do not normalize old Spanish spelling — fué, á, hácia, substituído are correct as printed. "
        "Never modernize these forms."
    ),
    "nahuatl": (
        "Do not normalize long vowel markers. Āā, Ēē, Īī, Ōō with macrons are semantically meaningful. "
        "Do not substitute unmarked vowels for marked ones."
    ),
    "yucatec_maya": (
        "Do not normalize classical Maya orthography. Glottal stops marked with apostrophes or special "
        "characters are phonemically meaningful — preserve exactly. Do not substitute modern unified Maya "
        "orthography for colonial-era spelling conventions."
    ),
    "classical_maya": (
        "Same as yucatec_maya. Colonial-era Maya texts use inconsistent Spanish-influenced spelling — "
        "transcribe exactly as printed, never standardize."
    ),
    "kaqchikel": (
        "Do not normalize colonial Kaqchikel Maya orthography. This text uses 16th century Spanish-influenced "
        "spelling conventions for Maya sounds. Glottal stops, ejective consonants (tz', k', q'), and long vowels "
        "may be marked inconsistently across the manuscript — transcribe exactly what is printed, never "
        "standardize to modern unified Maya orthography. The letters q, tz, ch, x, and combinations like qu "
        "represent specific Maya phonemes — do not substitute or modernize. Do not normalize variant spellings "
        "of the same name across pages — each spelling is source-accurate."
    ),
    "korean": (
        "Do not normalize hanja to hangul. Do not modernize historical hangul orthography."
    ),
    "japanese": (
        "Do not normalize historical kana. ゐ and ゑ are valid historical characters."
    ),
    "arabic": (
        "Do not normalize classical Arabic. Preserve tashkeel exactly as printed."
    ),
}

_LANGUAGE_ALIASES: dict[str, str] = {
    "spanish": "spanish",
    "colonial spanish": "spanish",
    "castilian": "spanish",
    "nahuatl": "nahuatl",
    "classical nahuatl": "nahuatl",
    "mexica nahuatl": "nahuatl",
    "kaqchikel": "kaqchikel",
    "kaqchikel maya": "kaqchikel",
    "yucatec": "yucatec_maya",
    "yucatec maya": "yucatec_maya",
    "maya": "classical_maya",
    "classical maya": "classical_maya",
    "korean": "korean",
    "japanese": "japanese",
    "arabic": "arabic",
    "chinese": "chinese",
}


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


def normalize_language_key(label: str) -> str | None:
    key = (label or "").strip().lower()
    if key in NORMALIZATION_RULES:
        return key
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    for alias, canonical in _LANGUAGE_ALIASES.items():
        if alias in key or key in alias:
            return canonical
    return None


def parse_language_percentages(raw: str) -> dict[str, float]:
    """Parse 'Spanish 60%, Classical Nahuatl 40%' into normalized fractions."""
    out: dict[str, float] = {}
    if not raw:
        return out
    for part in re.split(r"[,;]+", raw):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(.+?)\s*(\d+(?:\.\d+)?)\s*%", part)
        if m:
            label, pct = m.group(1).strip(), float(m.group(2))
            key = normalize_language_key(label)
            if key:
                out[key] = out.get(key, 0.0) + pct / 100.0
            continue
        key = normalize_language_key(part)
        if key:
            out[key] = out.get(key, 0.0) + 1.0
    total = sum(out.values())
    if total > 1.5:
        out = {k: v / total for k, v in out.items()}
    return out


def build_normalization_rules_text(lang_pcts: dict[str, float], threshold: float = 0.10) -> str:
    rules: list[str] = []
    for lang, pct in sorted(lang_pcts.items(), key=lambda x: -x[1]):
        if pct < threshold:
            continue
        rule = NORMALIZATION_RULES.get(lang)
        if rule and rule not in rules:
            rules.append(rule)
    return "\n".join(rules) if rules else NORMALIZATION_RULES["spanish"]


def normalize_language(value: str | None) -> str:
    lang = (value or DEFAULT_LANGUAGE).strip().lower()
    key = normalize_language_key(lang) or lang
    return key if key in VALID_LANGUAGES or key in NORMALIZATION_RULES else DEFAULT_LANGUAGE


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
    *,
    direction: str | None = None,
    normalization_rules: str | None = None,
) -> JobLanguageConfig:
    lang = normalize_language(language)
    sid = normalize_source_id(source_id)
    catalog = _load_language_catalog()
    meta = catalog.get(lang, {}) if lang in catalog else {}
    norm = normalization_rules or build_normalization_rules_text({lang: 1.0})
    hyphen = lang in ("spanish", "nahuatl", "kaqchikel", "yucatec_maya", "classical_maya")
    return JobLanguageConfig(
        language=lang,
        source_id=sid,
        script=normalize_script(script or meta.get("script"), lang),
        direction=str(direction or meta.get("direction", "ltr")),
        hyphenation_join=bool(meta.get("hyphenation_join", hyphen)),
        emphasis=str(meta.get("emphasis", "asterisk")),
        normalization_rule=norm,
    )


def job_language_config_from_state(state: dict) -> JobLanguageConfig:
    profile = state.get("detected_source_profile") or {}
    langs = profile.get("languages") or {}
    primary = max(langs, key=langs.get) if langs else state.get("language", DEFAULT_LANGUAGE)
    return job_language_config(
        primary,
        state.get("source_id") or state.get("source_name"),
        state.get("script"),
        direction=state.get("direction") or profile.get("direction"),
        normalization_rules=state.get("normalization_rules")
        or profile.get("normalization_rules"),
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


def load_hard_terms(source_id: str | None = None, state: dict | None = None) -> list[str]:
    if state:
        if state.get("hard_terms"):
            return list(state["hard_terms"])
        auto = state.get("hard_terms_file")
        if auto and Path(auto).is_file():
            terms = _read_term_lines(Path(auto))
            if terms:
                return terms
    sid = normalize_source_id(source_id)
    auto_path = CONFIG_DIR / f"hard_terms_auto_{sid}.txt"
    terms = _read_term_lines(auto_path)
    if terms:
        return terms
    path = hard_terms_path(sid)
    terms = _read_term_lines(path)
    if terms:
        return terms
    return _read_term_lines(LEGACY_HARD_TERMS_FILE)


def load_impossible_strings(source_id: str | None = None, state: dict | None = None) -> list[str]:
    if state:
        if state.get("impossible_strings"):
            return list(state["impossible_strings"])
        auto = state.get("impossible_strings_file")
        if auto and Path(auto).is_file():
            return _read_term_lines(Path(auto))
    sid = normalize_source_id(source_id)
    auto_path = CONFIG_DIR / f"impossible_auto_{sid}.txt"
    terms = _read_term_lines(auto_path)
    if terms:
        return terms
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


def effective_hard_terms(
    text: str, lang_cfg: JobLanguageConfig, state: dict | None = None
) -> list[str]:
    base = load_hard_terms(lang_cfg.source_id, state)
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
