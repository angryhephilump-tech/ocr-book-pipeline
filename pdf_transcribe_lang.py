"""Language and per-source config for PDF Transcribe pipeline v2."""

from __future__ import annotations

import json
import re
import unicodedata
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
    unify_abbreviation_marks: bool = False

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
    """Return slug or empty string — never default to a legacy source id."""
    if value is None or not str(value).strip():
        return ""
    return str(value).strip().lower().replace(" ", "_")


def normalize_script(value: str | None = None, language: str | None = None) -> str:
    raw = (value or "").strip().lower()
    if raw in VALID_SCRIPTS:
        return raw
    lang = normalize_language(language) if language else DEFAULT_LANGUAGE
    catalog = _load_language_catalog()
    meta = catalog.get(lang, {})
    return str(meta.get("script") or _LANGUAGE_DEFAULT_SCRIPT.get(lang, DEFAULT_SCRIPT))


_MEANINGFUL_DIACRITICS_FALLBACK = frozenset(
    {"nahuatl", "arabic", "kaqchikel", "yucatec_maya", "classical_maya", "japanese", "korean"}
)

_NOTATION_PROMPT_LINE = (
    "Superscripts and ordinals: use Unicode superscript characters "
    "(¹²³⁴⁵⁶⁷⁸⁹⁰, º, ª), never HTML tags (<sup>…</sup>) or caret notation (^1, N^o)."
)
_MACRON_TILDE_PROMPT_LINE = (
    "Render the scribal abbreviation stroke over a letter as a combining tilde "
    "(q̃, porq̃)."
)
_COMBINING_MACRON = "\u0304"
_COMBINING_TILDE = "\u0303"


def notation_prompt_line() -> str:
    return _NOTATION_PROMPT_LINE


def macron_tilde_prompt_line() -> str:
    return _MACRON_TILDE_PROMPT_LINE


def language_has_meaningful_diacritics(lang: str) -> bool:
    catalog = _load_language_catalog()
    meta = catalog.get(lang, {})
    if "meaningful_diacritics" in meta:
        return bool(meta["meaningful_diacritics"])
    return lang in _MEANINGFUL_DIACRITICS_FALLBACK


def detected_languages_have_meaningful_diacritics(
    lang_pcts: dict[str, float], *, threshold: float = 0.10
) -> bool:
    for lang, pct in lang_pcts.items():
        if pct >= threshold and language_has_meaningful_diacritics(lang):
            return True
    return False


def _load_source_config_json(source_id: str) -> dict:
    if not source_id:
        return {}
    from pdf_transcribe_detect import source_config_path

    path = source_config_path(source_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_unify_abbreviation_marks(state: dict, profile: dict | None = None) -> bool:
    """Source opt-in, forced OFF when any detected language has meaningful diacritics."""
    profile = profile or {}
    slug = (
        state.get("source_name")
        or state.get("source_id")
        or profile.get("source_name")
        or ""
    )
    src_cfg = _load_source_config_json(str(slug))
    if "unify_abbreviation_marks" in state:
        requested = bool(state["unify_abbreviation_marks"])
    elif "unify_abbreviation_marks" in profile:
        requested = bool(profile["unify_abbreviation_marks"])
    else:
        requested = bool(src_cfg.get("unify_abbreviation_marks", False))
    lang_pcts = (
        profile.get("languages")
        or src_cfg.get("detected_languages")
        or {}
    )
    if not lang_pcts and state.get("language"):
        lang_pcts = {state["language"]: 1.0}
    if detected_languages_have_meaningful_diacritics(lang_pcts):
        return False
    return requested


def job_language_config(
    language: str | None = None,
    source_id: str | None = None,
    script: str | None = None,
    *,
    direction: str | None = None,
    normalization_rules: str | None = None,
    unify_abbreviation_marks: bool = False,
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
        unify_abbreviation_marks=unify_abbreviation_marks,
    )


def job_language_config_from_state(state: dict) -> JobLanguageConfig:
    from pdf_transcribe_source import ensure_source_identity

    ensure_source_identity(state)
    profile = state.get("detected_source_profile") or {}
    langs = profile.get("languages") or {}
    primary = max(langs, key=langs.get) if langs else state.get("language", DEFAULT_LANGUAGE)
    sid = state.get("source_name") or state.get("source_id") or DEFAULT_SOURCE_ID
    unify = resolve_unify_abbreviation_marks(state, profile)
    return job_language_config(
        primary,
        sid,
        state.get("script"),
        direction=state.get("direction") or profile.get("direction"),
        normalization_rules=state.get("normalization_rules")
        or profile.get("normalization_rules"),
        unify_abbreviation_marks=unify,
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
    from pdf_transcribe_source import auto_file_matches_slug, resolve_source_slug

    slug = resolve_source_slug(state, source_id)
    if state:
        if state.get("hard_terms"):
            return list(state["hard_terms"])
        auto = state.get("hard_terms_file")
        if auto and auto_file_matches_slug(auto, slug) and Path(auto).is_file():
            terms = _read_term_lines(Path(auto))
            if terms:
                return terms
    if not slug:
        return _read_term_lines(LEGACY_HARD_TERMS_FILE)
    auto_path = CONFIG_DIR / f"hard_terms_auto_{slug}.txt"
    terms = _read_term_lines(auto_path)
    if terms:
        return terms
    path = hard_terms_path(slug)
    terms = _read_term_lines(path)
    if terms:
        return terms
    if slug == DEFAULT_SOURCE_ID:
        return _read_term_lines(LEGACY_HARD_TERMS_FILE)
    return []


def load_impossible_strings(source_id: str | None = None, state: dict | None = None) -> list[str]:
    """Per-source only — stored under config/sources/{slug}/impossible_strings.txt."""
    from pdf_transcribe_integrity import load_impossible_strings_for_source
    from pdf_transcribe_source import resolve_source_slug

    slug = resolve_source_slug(state, source_id)
    if not slug:
        return []
    return load_impossible_strings_for_source(slug, state)


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
        "inscripción",
        "inscripcion",
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
        core = re.sub(r"[\[\]]+", "", core)
        if len(core) < 12 or " " in core:
            continue
        if core.lower() in common:
            continue
        if not re.search(r"[A-Za-zÀ-ÿĀāĒēĪīŌō]", core):
            continue
        out.append(core)
    return sorted(set(out))


def term_occurrence_count(term: str, document_text: str) -> int:
    """Count case-insensitive occurrences of term in document (brackets ignored)."""
    if not term or not document_text:
        return 0
    view = re.sub(r"[\[\]]", "", strip_for_term_match(document_text))
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    return len(pattern.findall(view))


class DocumentTermIndex:
    """Cached term counts over a assembled document (one view build per book)."""

    __slots__ = ("_view", "_counts")

    def __init__(self, document_text: str) -> None:
        self._view = (
            re.sub(r"[\[\]]", "", strip_for_term_match(document_text)) if document_text else ""
        )
        self._counts: dict[str, int] = {}

    def count(self, term: str) -> int:
        if not term:
            return 0
        key = term.lower()
        if key not in self._counts:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            self._counts[key] = len(pattern.findall(self._view))
        return self._counts[key]


def filter_terms_by_min_occurrences(
    terms: list[str],
    document_text: str,
    *,
    min_count: int = 2,
    always_keep: set[str] | None = None,
    term_index: DocumentTermIndex | None = None,
) -> list[str]:
    """Drop single-occurrence auto terms; keep seeds and listed hard terms."""
    index = term_index or (DocumentTermIndex(document_text) if document_text else None)
    keep_lower = {k.lower() for k in (always_keep or set())}
    out: list[str] = []
    for term in terms:
        if term.lower() in keep_lower:
            out.append(term)
            continue
        if index is None:
            if term_occurrence_count(term, document_text) >= min_count:
                out.append(term)
            continue
        if index.count(term) >= min_count:
            out.append(term)
    return out


def nahuatl_orthographic_variants(term: str) -> set[str]:
    """Colonial Nahuatl spelling variants (cuauh/quauh, etc.) for fuzzy match-back."""
    t = term.strip().lower()
    forms: set[str] = {t}
    if "cuauh" in t:
        forms.add(t.replace("cuauh", "quauh"))
    if "quauh" in t:
        forms.add(t.replace("quauh", "cuauh"))
    if t.startswith("cuau"):
        forms.add("quau" + t[4:])
    if t.startswith("quau"):
        forms.add("cuau" + t[4:])
    return forms


def _match_view(text: str) -> str:
    return re.sub(r"[\[\]]", "", strip_for_term_match(text)).lower()


def term_present_in_text(
    term: str,
    text: str,
    lang_cfg: JobLanguageConfig,
    *,
    section_hint: str | None = None,
) -> bool:
    """Match-back with bracket stripping and paleographic fuzzy variants."""
    if not term.strip():
        return False
    view = _match_view(text)
    forms = {term.lower()}
    if section_hint == "paleographic_nahuatl" or lang_cfg.language == "nahuatl":
        forms.update(nahuatl_orthographic_variants(term))
    return any(f in view for f in forms)


def effective_hard_terms(
    text: str,
    lang_cfg: JobLanguageConfig,
    state: dict | None = None,
    *,
    document_text: str | None = None,
    term_index: DocumentTermIndex | None = None,
) -> list[str]:
    base = load_hard_terms(lang_cfg.source_id, state)
    auto = auto_hard_term_candidates(text, lang_cfg)
    if document_text:
        seed = set()
        if state:
            seed.update(str(t).lower() for t in (state.get("seed_hard_terms") or []))
            profile = state.get("detected_source_profile") or {}
            seed.update(str(t).lower() for t in (profile.get("seed_hard_terms") or []))
        auto = filter_terms_by_min_occurrences(
            auto,
            document_text,
            min_count=2,
            always_keep=seed,
            term_index=term_index,
        )
    seen: set[str] = set()
    merged: list[str] = []
    for term in base + auto:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(term)
    return merged


def page_has_hard_term(
    text: str,
    terms: list[str],
    lang_cfg: JobLanguageConfig | None = None,
    *,
    section_hint: str | None = None,
) -> bool:
    if not terms:
        return False
    if lang_cfg is None:
        match_text = strip_for_term_match(text)
        lower = match_text.lower()
        return any(t.strip() and t.lower() in lower for t in terms)
    return any(
        term_present_in_text(t, text, lang_cfg, section_hint=section_hint) for t in terms
    )


def strip_for_term_match(text: str) -> str:
    """Strip superscripts before hard-term matching; preserve original for patching."""
    return re.sub(r"[" + _SUPERSCRIPT_CHARS + r"]+", "", text)


def strip_whitespace_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text)


_SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_MODIFIER_SUPERSCRIPT: dict[str, str] = {
    "a": "ᵃ",
    "b": "ᵇ",
    "c": "ᶜ",
    "d": "ᵈ",
    "e": "ᵉ",
    "f": "ᶠ",
    "g": "ᵍ",
    "h": "ʰ",
    "i": "ⁱ",
    "j": "ʲ",
    "k": "ᵏ",
    "l": "ˡ",
    "m": "ᵐ",
    "n": "ⁿ",
    "o": "ᵒ",
    "p": "ᵖ",
    "r": "ʳ",
    "s": "ˢ",
    "t": "ᵗ",
    "u": "ᵘ",
    "v": "ᵛ",
    "w": "ʷ",
    "x": "ˣ",
    "y": "ʸ",
    "z": "ᶻ",
}
_HTML_SUP_RE = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)
_CARET_AFTER_LETTER_RE = re.compile(
    r"([A-Za-zÀ-ÿ])\^([0-9]+|[oaOA])(?![A-Za-zÀ-ÿ0-9])"
)
_CARET_STANDALONE_RE = re.compile(r"(?<![A-Za-zÀ-ÿ])\^([0-9]+)")


def _char_to_superscript(ch: str) -> str:
    if ch.isdigit():
        return _SUPERSCRIPT_DIGITS[int(ch)]
    low = ch.lower()
    if low == "o":
        return "º"
    if low == "a":
        return "ª"
    return _MODIFIER_SUPERSCRIPT.get(low, ch)


def _content_to_superscript(content: str) -> str:
    content = content.strip()
    if not content:
        return ""
    if len(content) == 1:
        return _char_to_superscript(content)
    return "".join(_char_to_superscript(ch) for ch in content)


_ORDINAL_AFTER_LOWERCASE_RE = re.compile(r"([a-zà-ÿñç])(º|ª)")


def canonicalize_superscript_notation(text: str) -> str:
    """
    Unify superscript letter forms after a lowercase base letter.
    Ordinal º (U+00BA) / ª (U+00AA) → modifier ᵒ (U+1D52) / ᵃ (U+1D43).
    Preserves abbreviation tokens like Nº / Mª (uppercase lead-in).
    """

    def _repl(match: re.Match[str]) -> str:
        base, mark = match.group(1), match.group(2)
        if mark == "º":
            return base + "ᵒ"
        if mark == "ª":
            return base + "ᵃ"
        return match.group(0)

    return _ORDINAL_AFTER_LOWERCASE_RE.sub(_repl, text)


def normalize_notation_tier1(text: str) -> str:
    """Always-on: HTML <sup>, caret notation, then superscript canonicalization."""

    def _html_repl(match: re.Match[str]) -> str:
        return _content_to_superscript(match.group(1))

    text = _HTML_SUP_RE.sub(_html_repl, text)

    def _caret_word_repl(match: re.Match[str]) -> str:
        return match.group(1) + _content_to_superscript(match.group(2))

    text = _CARET_AFTER_LETTER_RE.sub(_caret_word_repl, text)

    def _caret_standalone_repl(match: re.Match[str]) -> str:
        return _content_to_superscript(match.group(1))

    text = _CARET_STANDALONE_RE.sub(_caret_standalone_repl, text)
    return canonicalize_superscript_notation(text)


def unify_macron_to_tilde(text: str) -> str:
    """Macron → tilde (NFD combining marks and precomposed letters)."""
    decomposed = unicodedata.normalize("NFD", text)
    decomposed = decomposed.replace(_COMBINING_MACRON, _COMBINING_TILDE)
    return unicodedata.normalize("NFC", decomposed)


TIER2_SKIP_SECTIONS = frozenset({"paleographic_nahuatl"})


def section_skips_tier2_unification(section: str | None) -> bool:
    """Section types where meaningful diacritics must not be unified (Tier 2 off)."""
    return (section or "") in TIER2_SKIP_SECTIONS


def should_apply_tier2_unification(
    lang_cfg: JobLanguageConfig | None,
    *,
    section: str | None = None,
) -> bool:
    if lang_cfg is None or not lang_cfg.unify_abbreviation_marks:
        return False
    return not section_skips_tier2_unification(section)


def unify_abbreviation_marks_tier2(text: str) -> str:
    """Opt-in: Spanish abbreviation marks + scribal macron→tilde unification."""
    text = re.sub(r"\bN\.?\s*o\.?", "Nº", text, flags=re.IGNORECASE)
    text = re.sub(r"\bN\s*°", "Nº", text)
    text = re.sub(r"\bN\s*ᵒ", "Nº", text)
    text = re.sub(r"\bM\.?\s*a\.?", "Mª", text, flags=re.IGNORECASE)
    text = re.sub(r"\bM\s*°", "Mª", text)
    text = re.sub(r"\bS\.?\s*ra\.?", "Sra.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bS\.?\s*r\.?", "Sr.", text, flags=re.IGNORECASE)
    return unify_macron_to_tilde(text)


def canonical_for_content_compare(
    text: str,
    lang_cfg: JobLanguageConfig | None = None,
    *,
    section: str | None = None,
) -> str:
    t = normalize_notation_tier1(text)
    if should_apply_tier2_unification(lang_cfg, section=section):
        t = unify_abbreviation_marks_tier2(t)
    return strip_whitespace_for_compare(t)


def pages_need_content_reconcile(
    body1: str,
    body2: str,
    lang_cfg: JobLanguageConfig | None = None,
    *,
    section: str | None = None,
) -> bool:
    """Skip reconcile when runs differ only in whitespace or notation conventions."""
    if body1.strip() == body2.strip():
        return False
    return canonical_for_content_compare(body1, lang_cfg, section=section) != (
        canonical_for_content_compare(body2, lang_cfg, section=section)
    )
