"""Per-source identity, section tagging, and soft-term optimization (Pipeline v3)."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from pdf_transcribe_detect import slugify_source_name

MAX_PATCHES_PER_PAGE = 8
SOFT_TERM_PROMOTE_PATCHES = 10

# Known per-source operator notes (saved into source config on finalize).
SOURCE_ACCURACY_NOTES: dict[str, str] = {
    "anales_de_tlatelolco": (
        "Intentional spelling variation: Nahuatl paleographic pages use colonial forms "
        "(e.g. Moquiuix without accent); Spanish translation pages use modern forms "
        "(e.g. Moquíhuix). Do not standardize across sections. "
        "unoaconejaron is source-accurate in Tena's edition (not an OCR error)."
    ),
}

DEFAULT_SOFT_TERMS_BY_SOURCE: dict[str, list[str]] = {
    "anales_de_tlatelolco": [
        "Tlatelolco",
        "Tenochtitlan",
        "Azcapotzalco",
        "México",
        "Méjico",
    ],
}


class SourceIdentityError(ValueError):
    """source_name and source_id disagree — wrong config would load."""


def resolve_source_slug(state: dict | None = None, explicit: str | None = None) -> str:
    if explicit:
        return slugify_source_name(explicit)
    if state:
        name = (state.get("source_name") or "").strip()
        sid = (state.get("source_id") or "").strip()
        if name and sid and name != sid:
            raise SourceIdentityError(
                f"source_name ({name!r}) and source_id ({sid!r}) must match. "
                "Use one source name per book and re-run detection."
            )
        if name:
            return name
        if sid:
            return sid
    return ""


def ensure_source_identity(state: dict, *, strict: bool = True) -> str:
    """Sync source_id to source_name. Raises if both set and differ."""
    slug = resolve_source_slug(state)
    if strict and (state.get("source_name") or "").strip() and (state.get("source_id") or "").strip():
        n = (state.get("source_name") or "").strip()
        s = (state.get("source_id") or "").strip()
        if n != s:
            raise SourceIdentityError(
                f"source_name ({n!r}) != source_id ({s!r}). Delete state.json or fix source name."
            )
    if slug:
        state["source_name"] = slug
        state["source_id"] = slug
    return slug


def direction_for_script(script: str, detected: str | None = None) -> str:
    """Latin and CJK are LTR; only Arabic defaults RTL. Model cannot override Latin → RTL."""
    s = (script or "latin").strip().lower()
    if s in ("latin", "korean", "japanese", "chinese"):
        return "ltr"
    if s == "arabic":
        return "rtl"
    d = (detected or "ltr").strip().lower()
    if d in ("rtl", "mixed"):
        return d
    return "ltr"


def impossible_extra_path(slug: str) -> Path:
    from pdf_transcribe_lang import CONFIG_DIR

    return CONFIG_DIR / f"impossible_extra_{slug}.txt"


def soft_terms_path(slug: str) -> Path:
    from pdf_transcribe_lang import CONFIG_DIR

    return CONFIG_DIR / f"soft_terms_{slug}.txt"


def read_term_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_soft_terms(slug: str, state: dict | None = None) -> set[str]:
    terms = set(DEFAULT_SOFT_TERMS_BY_SOURCE.get(slug, []))
    if state and state.get("soft_terms"):
        terms.update(state["soft_terms"])
    path = soft_terms_path(slug)
    for t in read_term_lines(path):
        terms.add(t)
    return {t.lower() for t in terms if t}


def save_soft_terms(slug: str, terms: list[str]) -> Path:
    path = soft_terms_path(slug)
    lines = ["# Terms logged but not sent to spot-patch API (stable / zero historical fixes)"]
    lines.extend(sorted(set(terms), key=str.lower))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_impossible_extra(slug: str) -> list[str]:
    return read_term_lines(impossible_extra_path(slug))


def filter_generated_impossible(candidates: list[str], hard_terms: list[str]) -> list[str]:
    """Drop variants that are actually valid hard terms or common Spanish words."""
    from pdf_transcribe_lang import _SPANISH_COMMON

    hard_lower = {t.strip().lower() for t in hard_terms if t.strip()}
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        c = raw.strip()
        if not c:
            continue
        key = c.lower()
        if key in seen:
            continue
        if key in hard_lower:
            continue
        if key in _SPANISH_COMMON:
            continue
        seen.add(key)
        out.append(c)
    return out


def auto_file_matches_slug(path: Path | str | None, slug: str) -> bool:
    if not path or not slug:
        return False
    name = Path(path).name.lower()
    return slug.lower() in name


_EDITORIAL_HEADERS = (
    "PRESENTACIÓN",
    "PRESENTACION",
    "INTRODUCCIÓN",
    "INTRODUCCION",
    "PRÓLOGO",
    "PROLOGO",
    "NOTA DEL EDITOR",
    "AGRADECIMIENTOS",
    "ESTUDIO PRELIMINAR",
    "NOTA PRELIMINAR",
)

_EDITORIAL_PROSE_MARKERS = (
    "presentación",
    "biblioteca",
    "universidad",
    "edición",
    "colección",
    "paleografía",
    "paleografia",
    "transcripción",
    "transcripcion",
    "manuscrito",
    "introducción",
    "introduccion",
    "capítulo",
    "capitulo",
    "agradecimiento",
    "sin embargo",
    "por lo tanto",
    "asimismo",
    "cabe señalar",
    "cabe senalar",
    "en este trabajo",
    "el presente volumen",
)

_TRANSLATION_PROSE_MARKERS = (
    "traducción",
    "traduccion",
    "dice el texto",
    "en español",
    "en espanol",
    "moquíhuix",
    "moquihuix",
    "los mexicas",
    "dieron principio",
    "año de",
    "año del",
)

# Distinct Nahuatl morphemes / paleographic cues — not place names used in Spanish prose.
_NAHUATL_LINGUISTIC_MARKERS = (
    "nican",
    "moteneu",
    "çoatl",
    "coatl",
    "tepetl",
    "tlahtoani",
    "tlahtoa",
    "xiuhmolpilli",
    "quauhtla",
    "xochitl",
    "ēhecatl",
    "ehecatl",
    "miquiztli",
    "tochtli",
    "acatl",
)

# Colonial Spanish function words (often without accents); ç and q̄ are scribal Spanish, not Nahuatl.
_SPANISH_COLONIAL_MARKERS = (
    "como",
    "que",
    "los",
    "las",
    "del",
    "por",
    "con",
    "habia",
    "habían",
    "abian",
    "venido",
    "alli",
    "allí",
    "entonces",
    "despues",
    "después",
    "dijo",
    "tenia",
    "tenía",
    "señor",
    "senor",
    "año",
    "porque",
    "porq",
    "esto",
    "esta",
    "aquel",
    "aquella",
    "donde",
    "cuando",
    "muchos",
    "muchas",
    "tambien",
    "también",
    "ciudad",
    "açordaron",
    "çerto",
    "çaminos",
)

_BRACKET_SKIP = frozenset({"sic", "illegible", "damaged", "?", "…", "..."})


def _has_editorial_header(text: str) -> bool:
    head = text[:1200].upper()
    return any(h in head for h in _EDITORIAL_HEADERS)


def _manuscript_bracket_count(text: str) -> int:
    """Bracketed colonial reconstructions like [Nican moteneu] — not [sic] or [illegible]."""
    count = 0
    for m in re.finditer(r"\[([^\]]+)\]", text):
        inner = m.group(1).strip()
        key = inner.lower().rstrip(".")
        if key in _BRACKET_SKIP or key.startswith("sic"):
            continue
        if len(inner) < 4:
            continue
        if re.search(r"[A-Za-zÀ-ÿĀāĒēĪīŌō]", inner):
            count += 1
    return count


def _marker_score(text: str, markers: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for m in markers if m in lower)


def _word_marker_score(text: str, markers: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for m in markers if re.search(rf"\b{re.escape(m)}\b", lower))


def _precomposed_nahuatl_macron_count(text: str) -> int:
    """Vowel macrons in Nahuatl editions (ĀāĒē) — semantically meaningful, not Spanish q̄ abbrev."""
    return len(re.findall(r"[ĀāĒēĪīŌō]", text))


def _scribal_combining_macron_count(text: str) -> int:
    """Combining macron on a letter (e.g. q̄, n̄) — colonial Spanish abbreviation stroke."""
    nfd = unicodedata.normalize("NFD", text)
    return len(re.findall(r"[^\s\d\W]\u0304", nfd))


def _looks_like_archaic_spanish_prose(text: str) -> bool:
    """
    Colonial Spanish narrative: Spanish function words + ç / q̄ scribal marks.
    Not paleographic Nahuatl even when diacritic-poor.
    """
    spanish_score = _word_marker_score(text, _SPANISH_COLONIAL_MARKERS)
    nahuatl_score = _marker_score(text, _NAHUATL_LINGUISTIC_MARKERS)
    precomposed = _precomposed_nahuatl_macron_count(text)
    if nahuatl_score > 0 or precomposed > 0:
        return False
    if spanish_score >= 3:
        return True
    if spanish_score >= 2 and _scribal_combining_macron_count(text) >= 1:
        return True
    if spanish_score >= 2 and text.lower().count("ç") >= 2:
        return True
    return False


def classify_page_section(page_text: str, languages: dict[str, float] | None = None) -> str:
    """
    Section-aware tag for mixed colonial books.
    paleographic_nahuatl | spanish_translation | editorial_spanish | mixed

    Priority: editorial headers → archaic Spanish prose → paleographic Nahuatl
    (strict: manuscript brackets + Nahuatl morphemes/macrons) → editorial → translation.

    Archaic Spanish (q̄ abbreviations, ç, no accents) is spanish_translation, not Nahuatl.
    Mislabeling Spanish as paleographic_nahuatl only skips Tier-2 unification (safe);
    mislabeling Nahuatl as Spanish would harm meaningful diacritics (avoid).
    """
    text = (page_text or "").strip()
    if not text or text.startswith("[Skipped:"):
        return "skipped"

    if _has_editorial_header(text):
        return "editorial_spanish"

    manuscript_brackets = _manuscript_bracket_count(text)
    precomposed_macrons = _precomposed_nahuatl_macron_count(text)
    nahuatl_score = _marker_score(text, _NAHUATL_LINGUISTIC_MARKERS)
    editorial_score = _marker_score(text, _EDITORIAL_PROSE_MARKERS)
    translation_score = _marker_score(text, _TRANSLATION_PROSE_MARKERS)

    if _looks_like_archaic_spanish_prose(text):
        return "spanish_translation"

    if manuscript_brackets >= 2 and (precomposed_macrons >= 1 or nahuatl_score >= 1):
        return "paleographic_nahuatl"
    if manuscript_brackets >= 1 and precomposed_macrons >= 1 and nahuatl_score >= 1:
        return "paleographic_nahuatl"
    if precomposed_macrons >= 2 and nahuatl_score >= 1:
        return "paleographic_nahuatl"

    if manuscript_brackets == 0:
        if editorial_score >= 2:
            return "editorial_spanish"
        if translation_score >= 2 or (
            translation_score >= 1 and editorial_score == 0
        ):
            return "spanish_translation"
        if editorial_score >= 1:
            return "editorial_spanish"
        return "spanish_translation"

    if editorial_score >= 2:
        return "editorial_spanish"
    if translation_score >= 1:
        return "spanish_translation"
    return "mixed"


def tag_page_sections_from_run1(work_dir: Path, state: dict) -> dict[int, str]:
    from pdf_transcribe import job_page_numbers, read_run_page_body

    profile = state.get("detected_source_profile") or {}
    langs = profile.get("languages") or {}
    tags: dict[int, str] = {}
    for page_num in job_page_numbers(state):
        body = read_run_page_body(work_dir, 1, page_num)
        tags[page_num] = classify_page_section(body, langs)
    state["page_sections"] = {str(k): v for k, v in tags.items()}
    return tags


def optimize_soft_terms_from_log(work_dir: Path, slug: str, state: dict) -> list[str]:
    """Terms with 10+ patch attempts and zero applies move to soft_terms (no API calls)."""
    log_path = work_dir / "spot_patch_log.txt"
    soft = set(load_soft_terms(slug, state))
    if not log_path.is_file():
        return sorted(soft, key=str.lower)

    attempts: dict[str, int] = {}
    applied: dict[str, int] = {}
    term_re = re.compile(r"\[([^\]]+)\]")

    for line in log_path.read_text(encoding="utf-8").splitlines():
        if "op " not in line:
            continue
        m = term_re.search(line)
        if not m:
            continue
        terms = [t.strip() for t in m.group(1).split(",")]
        for t in terms:
            key = t.lower()
            attempts[key] = attempts.get(key, 0) + 1
            if "APPLIED" in line:
                applied[key] = applied.get(key, 0) + 1

    promoted: list[str] = []
    for term, count in attempts.items():
        if count >= SOFT_TERM_PROMOTE_PATCHES and applied.get(term, 0) == 0:
            if term not in soft:
                promoted.append(term)
            soft.add(term)
    merged = sorted(soft, key=str.lower)
    save_soft_terms(slug, merged)
    state["soft_terms"] = merged
    state["soft_terms_promoted"] = promoted
    return merged


def patch_terms_for_page(
    base: str,
    lang_cfg,
    state: dict,
    *,
    document_text: str | None = None,
    term_index=None,
) -> list[str]:
    """Hard terms minus soft terms (stable names that skip spot-patch API)."""
    from pdf_transcribe_lang import effective_hard_terms

    slug = resolve_source_slug(state)
    soft = load_soft_terms(slug, state)
    terms = effective_hard_terms(
        base,
        lang_cfg,
        state,
        document_text=document_text,
        term_index=term_index,
    )
    return [t for t in terms if t.lower() not in soft]


def spot_patch_operations_for_page(
    base: str,
    lang_cfg,
    state: dict,
    *,
    document_text: str | None = None,
    term_index=None,
) -> list:
    from pdf_transcribe_spot import cap_patch_operations, collect_patch_operations

    patch_terms = patch_terms_for_page(
        base,
        lang_cfg,
        state,
        document_text=document_text,
        term_index=term_index,
    )
    ops = collect_patch_operations(base, patch_terms, lang_cfg)
    return cap_patch_operations(ops, MAX_PATCHES_PER_PAGE)


def page_section_hint(state: dict, page_num: int) -> str | None:
    sections = state.get("page_sections") or {}
    return sections.get(str(page_num)) or sections.get(page_num)


def source_accuracy_notes(slug: str) -> str:
    return SOURCE_ACCURACY_NOTES.get(slug, "")
