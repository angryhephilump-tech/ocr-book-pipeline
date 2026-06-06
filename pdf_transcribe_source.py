"""Per-source identity, section tagging, and soft-term optimization (Pipeline v3)."""

from __future__ import annotations

import json
import re
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
        "Confirmed both-runs-wrong: unoaconejaron → aconsejaron (line-break merge error)."
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


_NAHUATL_WORD_MARKERS = (
    "moquiuix",
    "moquíhuix",
    "tlatelolco",
    "náhuatl",
    "nahuatl",
    "quauh",
    "xochitl",
    "huix",
    "mexica",
    "mexicas",
    "ç",
    "tzin",
    "tlan",
)


def _nahuatl_word_ratio(text: str) -> float:
    words = re.findall(r"\S+", text)
    if not words:
        return 0.0
    hits = 0
    for w in words:
        wl = re.sub(r"^[*_\[\]]+|[*_\]\].,;:!?]+$", "", w).lower()
        if not wl:
            continue
        if re.search(r"[ĀāĒēĪīŌō]", w) or "ç" in w:
            hits += 1
            continue
        if any(m in wl for m in _NAHUATL_WORD_MARKERS):
            hits += 1
    return hits / len(words)


def classify_page_section(page_text: str, languages: dict[str, float] | None = None) -> str:
    """
    Section-aware tag for mixed colonial books.
    paleographic_nahuatl | spanish_translation | editorial_spanish | mixed
    """
    text = (page_text or "").strip()
    if not text or text.startswith("[Skipped:"):
        return "skipped"
    nahuatl_ratio = _nahuatl_word_ratio(text)
    if nahuatl_ratio > 0.5:
        return "paleographic_nahuatl"
    lower = text.lower()
    bracket_count = len(re.findall(r"\[[^\]]{3,}\]", text))
    macron_count = len(re.findall(r"[ĀāĒēĪīŌō]", text))
    nahuatl_markers = sum(
        1
        for m in (
            "moquiuix",
            "tlatelolco",
            "náhuatl",
            "nahuatl",
            "quauh",
            "xochitl",
            "ç",
            "huix",
        )
        if m in lower
    )
    editorial_markers = sum(
        1
        for m in (
            "capítulo",
            "introducción",
            "biblioteca",
            "universidad",
            "edición",
            "paleografía",
            "transcripción",
        )
        if m in lower
    )
    if bracket_count >= 2 or (macron_count >= 2 and nahuatl_markers >= 2):
        return "paleographic_nahuatl"
    if editorial_markers >= 2 and bracket_count == 0:
        return "editorial_spanish"
    langs = languages or {}
    nahuatl_pct = langs.get("nahuatl", 0) + langs.get("classical_maya", 0)
    if nahuatl_markers >= 3 and editorial_markers == 0:
        return "paleographic_nahuatl"
    if editorial_markers >= 1 and nahuatl_markers == 0:
        return "spanish_translation"
    if nahuatl_ratio > 0.25 or (nahuatl_pct >= 0.4 and nahuatl_markers >= 1):
        return "mixed"
    return "spanish_translation" if editorial_markers else "mixed"


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
) -> list[str]:
    """Hard terms minus soft terms (stable names that skip spot-patch API)."""
    from pdf_transcribe_lang import effective_hard_terms

    slug = resolve_source_slug(state)
    soft = load_soft_terms(slug, state)
    terms = effective_hard_terms(base, lang_cfg, state)
    return [t for t in terms if t.lower() not in soft]


def spot_patch_operations_for_page(
    base: str,
    lang_cfg,
    state: dict,
) -> list:
    from pdf_transcribe_spot import cap_patch_operations, collect_patch_operations

    patch_terms = patch_terms_for_page(base, lang_cfg, state)
    ops = collect_patch_operations(base, patch_terms, lang_cfg)
    return cap_patch_operations(ops, MAX_PATCHES_PER_PAGE)


def page_section_hint(state: dict, page_num: int) -> str | None:
    sections = state.get("page_sections") or {}
    return sections.get(str(page_num)) or sections.get(page_num)


def source_accuracy_notes(slug: str) -> str:
    return SOURCE_ACCURACY_NOTES.get(slug, "")
