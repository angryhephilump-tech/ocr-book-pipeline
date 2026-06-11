"""Pipeline v3: intent-based source auto-detection and per-source profiles."""

from __future__ import annotations

import json
import random
import re
from datetime import date
from pathlib import Path
from typing import Any

import requests

from pdf_transcribe_lang import (
    CONFIG_DIR,
    build_normalization_rules_text,
    normalize_language_key,
    normalize_script,
    parse_language_percentages,
)

DETECTION_PROMPT = """You are analyzing a historical document before transcription.

These sample pages span the early, middle, and late book. Some consecutive page pairs from the first half are included on purpose — colonial bilingual manuscripts often alternate Spanish and an indigenous language on every other page in the first half, then indigenous-only in later sections. Identify ALL languages present in this material, including any language that appears on only some pages or only in part of the book. Do not assume the whole book is one language just because several sample pages match.

Look at these sample pages and answer the following questions precisely:
1. What language or languages are present? List all you can identify with rough percentage estimates for the WHOLE book (e.g. 'Spanish 60%, Classical Nahuatl 40%' or 'Colonial Spanish 100%' or 'Yucatec Maya 100%').
2. What script is used? (Latin, Arabic, Korean hangul, Japanese kana/kanji, Chinese, or mixed)
3. What writing direction? (left-to-right, right-to-left, or mixed)
4. Approximately what era is the typography? (e.g. '19th century printed', 'modern printed', 'handwritten manuscript', '16th century colonial')
5. List up to 20 proper nouns, place names, or technical terms you can see that are most likely to cause transcription errors. These will seed the hard terms list.
6. Are there footnotes separate from body text? Yes/No
7. Are there headers or running titles at top of pages? Yes/No
8. Estimate average words per page.
Return your answer as JSON only, no preamble. Use this schema:
{"languages": "Spanish 60%, Classical Nahuatl 40%", "script": "Latin", "direction": "left-to-right", "era": "...", "seed_hard_terms": ["..."], "footnotes": true, "headers": false, "avg_words_per_page": 250}"""

HARD_TERM_EXTRACT_PROMPT = """Scan this transcription and extract every token that: (a) is 10 or more characters with no spaces, (b) contains non-standard character combinations for the detected language, or (c) appears to be a proper noun, place name, or technical term. Return as a JSON list of unique strings, sorted by frequency descending. JSON only, no preamble."""

IMPOSSIBLE_STRINGS_PROMPT = """Given this list of hard terms that should appear in this transcription, generate a list of corrupted variants that should NEVER appear — obvious OCR typos only: transposed adjacent characters, doubled characters, dropped characters, substituted similar-looking characters (l/I/1, o/0, u/v, rn/m, cl/d).

Rules:
— Do NOT include alternate valid spellings, colonial orthography, cedilla variants, or manuscript forms.
— Do NOT include any string that could be correct in this specific document.
— Do NOT include common Spanish dictionary words.
— Maximum 40 items.
Return as a JSON list. JSON only, no preamble. Hard terms:
{terms}"""

SOURCES_DIR = CONFIG_DIR / "sources"
DETECTION_SAMPLE_COUNT = 5
DETECTION_MAX_SAMPLES = 10


def source_config_path(source_name: str) -> Path:
    slug = slugify_source_name(source_name)
    return SOURCES_DIR / f"{slug}.json"


def hard_terms_auto_path(source_name: str) -> Path:
    return CONFIG_DIR / f"hard_terms_auto_{slugify_source_name(source_name)}.txt"


def impossible_auto_path(source_name: str) -> Path:
    from pdf_transcribe_integrity import impossible_strings_file

    return impossible_strings_file(source_name)


def slugify_source_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "unknown").strip().lower())
    return slug.strip("_") or "unknown"


def _balanced_bracket_slice(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the first balanced {...} or [...] slice without regex backtracking."""
    idx = text.find(open_ch)
    if idx < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(idx, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[idx : i + 1]
    return None


def _extract_json_blob(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for open_ch in "{[":
            idx = text.find(open_ch)
            if idx < 0:
                continue
            try:
                obj, _end = decoder.raw_decode(text, idx)
                return obj
            except json.JSONDecodeError:
                pass
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            blob = _balanced_bracket_slice(text, open_ch, close_ch)
            if blob:
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    pass
        raise


def normalize_direction(raw: str) -> str:
    t = (raw or "").strip().lower()
    if "right" in t or t == "rtl":
        return "rtl"
    if "mixed" in t:
        return "mixed"
    return "ltr"


def normalize_script_from_detection(raw: str) -> str:
    t = (raw or "").strip().lower()
    if "arabic" in t:
        return "arabic"
    if "korean" in t or "hangul" in t:
        return "korean"
    if "japanese" in t or "kana" in t or "kanji" in t:
        return "japanese"
    if "chinese" in t:
        return "chinese"
    return "latin"


def parse_detection_response(text: str) -> dict:
    from pdf_transcribe_source import direction_for_script

    data = _extract_json_blob(text)
    if not isinstance(data, dict):
        raise ValueError("Detection response was not a JSON object.")
    langs_raw = str(data.get("languages") or data.get("language") or "")
    lang_pcts = parse_language_percentages(langs_raw)
    seed = data.get("seed_hard_terms") or data.get("hard_terms") or []
    if isinstance(seed, str):
        seed = [s.strip() for s in seed.split(",") if s.strip()]
    script = normalize_script_from_detection(str(data.get("script", "latin")))
    direction = direction_for_script(script, normalize_direction(str(data.get("direction", "ltr"))))
    return {
        "languages_raw": langs_raw,
        "languages": lang_pcts,
        "script": script,
        "direction": direction,
        "era": str(data.get("era") or "").strip(),
        "seed_hard_terms": [str(t).strip() for t in seed if str(t).strip()][:20],
        "footnotes": _as_bool(data.get("footnotes")),
        "headers": _as_bool(data.get("headers")),
        "avg_words_per_page": int(data.get("avg_words_per_page") or 0),
        "normalization_rules": build_normalization_rules_text(lang_pcts),
        "normalization_languages": [
            k for k, v in lang_pcts.items() if v >= 0.10
        ],
    }


def _as_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("yes", "true", "1")


def profile_display_lines(profile: dict) -> list[str]:
    langs = profile.get("languages_raw") or profile.get("languages_display") or ""
    if not langs and profile.get("languages"):
        parts = [f"{k.replace('_', ' ').title()} {int(v*100)}%" for k, v in profile["languages"].items()]
        langs = ", ".join(parts)
    lines = [
        f"Languages: {langs}",
        f"Script: {profile.get('script', 'latin')}",
        f"Direction: {profile.get('direction', 'ltr').upper()}",
        f"Era: {profile.get('era', '')}",
        f"Footnotes: {'Yes' if profile.get('footnotes') else 'No'}",
        f"Headers: {'Yes' if profile.get('headers') else 'No'}",
        f"Seed hard terms: {', '.join(profile.get('seed_hard_terms') or [])}",
    ]
    return lines


def apply_profile_to_state(state: dict, profile: dict, source_name: str) -> None:
    from pdf_transcribe_source import direction_for_script, source_accuracy_notes

    lang_pcts = profile.get("languages") or {}
    primary = max(lang_pcts, key=lang_pcts.get) if lang_pcts else "spanish"
    slug = slugify_source_name(source_name)
    script = profile.get("script") or "latin"
    direction = direction_for_script(script, profile.get("direction"))
    state["source_name"] = slug
    state["source_id"] = slug
    state["detected_source_profile"] = {
        **profile,
        "source_name": slug,
        "confirmed": True,
        "script": script,
        "direction": direction,
    }
    state["language"] = primary
    state["script"] = script
    state["direction"] = direction
    state["normalization_rules"] = profile.get("normalization_rules") or ""
    if "unify_abbreviation_marks" in profile:
        state["unify_abbreviation_marks"] = bool(profile["unify_abbreviation_marks"])
    state["hard_terms_file"] = str(hard_terms_auto_path(slug))
    state["impossible_strings_file"] = str(impossible_auto_path(slug))
    state["seed_hard_terms"] = list(profile.get("seed_hard_terms") or [])
    state["impossible_strings"] = []
    notes = source_accuracy_notes(slug)
    if notes:
        state["accuracy_notes"] = notes
    _write_seed_hard_terms(slug, state["seed_hard_terms"])


def _write_seed_hard_terms(source_slug: str, terms: list[str]) -> Path:
    path = hard_terms_auto_path(source_slug)
    lines = ["# Auto-generated seed terms from Phase 0 detection", *terms]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_saved_source_profile(source_name: str) -> dict | None:
    from pdf_transcribe_source import direction_for_script

    path = source_config_path(source_name)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    profile = {
        "languages": data.get("detected_languages") or {},
        "languages_raw": ", ".join(
            f"{k.replace('_', ' ').title()} {int(v*100)}%"
            for k, v in (data.get("detected_languages") or {}).items()
        ),
        "script": data.get("detected_script", "latin"),
        "direction": direction_for_script(
            data.get("detected_script", "latin"),
            data.get("detected_direction", "ltr"),
        ),
        "era": data.get("detected_era", ""),
        "seed_hard_terms": _read_terms_file(data.get("hard_terms_file")),
        "footnotes": data.get("footnotes", False),
        "headers": data.get("headers", False),
        "from_saved_config": True,
        "normalization_rules": build_normalization_rules_text(
            data.get("detected_languages") or {}
        ),
        "normalization_languages": data.get("normalization_rules_applied") or [],
    }
    if "unify_abbreviation_marks" in data:
        profile["unify_abbreviation_marks"] = bool(data["unify_abbreviation_marks"])
    return profile


def _read_terms_file(rel_or_name: str | None) -> list[str]:
    if not rel_or_name:
        return []
    path = Path(rel_or_name)
    if not path.is_file():
        path = CONFIG_DIR / Path(rel_or_name).name
    if not path.is_file():
        return []
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def save_source_config(work_dir: Path, state: dict, stats: dict) -> Path:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    profile = state.get("detected_source_profile") or {}
    slug = state.get("source_name") or state.get("source_id") or "unknown"
    hard_file = f"hard_terms_auto_{slug}.txt"
    imp_file = f"impossible_auto_{slug}.txt"
    from pdf_transcribe_source import source_accuracy_notes

    notes = (
        stats.get("accuracy_notes")
        or state.get("accuracy_notes")
        or source_accuracy_notes(slug)
        or ""
    )
    payload = {
        "source_name": slug,
        "detected_languages": profile.get("languages") or {},
        "detected_script": state.get("script", "latin"),
        "detected_direction": state.get("direction", "ltr"),
        "detected_era": profile.get("era", ""),
        "hard_terms_file": hard_file,
        "impossible_strings_file": imp_file,
        "soft_terms_file": f"soft_terms_{slug}.txt",
        "normalization_rules_applied": profile.get("normalization_languages")
        or list((profile.get("languages") or {}).keys()),
        "footnotes": profile.get("footnotes", False),
        "headers": profile.get("headers", False),
        "page_sections": state.get("page_sections") or {},
        "spelling_variation_notes": notes,
        "run_date": date.today().isoformat(),
        "pages_processed": stats.get("total_pages", 0),
        "accuracy_notes": notes,
    }
    path = source_config_path(slug)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def pick_detection_sample_pages(
    page_numbers: list[int],
    count: int = DETECTION_SAMPLE_COUNT,
    *,
    max_samples: int = DETECTION_MAX_SAMPLES,
) -> list[int]:
    """Spread samples across the book; include consecutive pairs from the first half.

    Pure random sampling can miss Spanish (or other colonial languages) when the first
    half alternates every other page and the second half is indigenous-only — this
    strategy samples pairs from the early section plus quartiles across the full range.
    """
    pages = sorted(page_numbers)
    n = len(pages)
    if n <= count:
        return pages

    page_set = set(pages)
    chosen: list[int] = []
    seen: set[int] = set()

    def add(page: int) -> None:
        if page in page_set and page not in seen:
            seen.add(page)
            chosen.append(page)

    def add_pair_at_index(idx: int) -> None:
        idx = max(0, min(idx, n - 1))
        add(pages[idx])
        if idx + 1 < n:
            add(pages[idx + 1])

    # First half: two consecutive pairs (catches every-other-page bilingual layouts)
    mid = max(2, n // 2)
    for frac in (0.15, 0.45):
        add_pair_at_index(int(mid * frac))

    # Full book: quartile anchors (catches late indigenous-only sections + any early Spanish)
    for frac in (0.08, 0.33, 0.67, 0.92):
        add(pages[min(int(n * frac), n - 1)])

    # Pad from first quarter if still thin — Spanish often only in early alternating pages
    q1 = pages[: max(1, n // 4)]
    attempts = 0
    while len(chosen) < max(count, 6) and attempts < 30:
        add(random.choice(q1))
        attempts += 1

    result = sorted(chosen)
    return result[:max_samples]


def build_multi_image_detection_params(
    image_paths: list[Path],
    model: str,
    user_text: str,
) -> dict:
    from pdf_transcribe import SYSTEM_PROMPT, image_to_base64_jpeg

    content: list[dict] = []
    for path in image_paths:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_to_base64_jpeg(path),
                },
            }
        )
    content.append({"type": "text", "text": user_text})
    return {
        "model": model,
        "system": SYSTEM_PROMPT,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": content}],
    }


def run_phase0_detection(
    api_key: str,
    sample_images: list[Path],
    *,
    model: str | None = None,
) -> dict:
    from pdf_transcribe import ANTHROPIC_URL, _format_api_error, anthropic_headers, load_settings

    model = (model or load_settings()["model"]).strip()
    payload = build_multi_image_detection_params(sample_images, model, DETECTION_PROMPT)
    resp = requests.post(
        ANTHROPIC_URL,
        headers=anthropic_headers(api_key),
        json=payload,
        timeout=(60, 300),
    )
    if resp.status_code >= 400:
        raise RuntimeError(_format_api_error(resp.status_code, resp.text))
    parts = resp.json().get("content") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    return parse_detection_response(text)


def merge_hard_terms(seed: list[str], extracted: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for term in seed + extracted:
        key = term.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(term.strip())
    return merged


def write_hard_terms_file(path: Path, terms: list[str], header: str = "") -> None:
    lines = []
    if header:
        lines.append(f"# {header}")
    lines.extend(terms)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def extract_hard_terms_from_run1_text(
    api_key: str,
    run1_text: str,
    *,
    model: str | None = None,
    max_chars: int = 120_000,
) -> list[str]:
    from pdf_transcribe import ANTHROPIC_URL, _format_api_error, anthropic_headers, load_settings

    model = (model or load_settings()["model"]).strip()
    sample = run1_text[:max_chars]
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": f"{HARD_TERM_EXTRACT_PROMPT}\n\nTRANSCRIPTION:\n{sample}",
            }
        ],
    }
    resp = requests.post(
        ANTHROPIC_URL,
        headers=anthropic_headers(api_key),
        json=payload,
        timeout=(60, 300),
    )
    if resp.status_code >= 400:
        return []
    parts = resp.json().get("content") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    try:
        data = _extract_json_blob(text)
        if isinstance(data, list):
            return [str(t).strip() for t in data if str(t).strip()]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def generate_impossible_strings(
    api_key: str,
    hard_terms: list[str],
    *,
    model: str | None = None,
) -> list[str]:
    from pdf_transcribe import ANTHROPIC_URL, _format_api_error, anthropic_headers, load_settings

    if not hard_terms:
        return []
    model = (model or load_settings()["model"]).strip()
    terms_block = "\n".join(hard_terms[:80])
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": IMPOSSIBLE_STRINGS_PROMPT.format(terms=terms_block),
            }
        ],
    }
    resp = requests.post(
        ANTHROPIC_URL,
        headers=anthropic_headers(api_key),
        json=payload,
        timeout=(60, 300),
    )
    if resp.status_code >= 400:
        return []
    parts = resp.json().get("content") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    try:
        data = _extract_json_blob(text)
        if isinstance(data, list):
            return [str(t).strip() for t in data if str(t).strip()]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def run_post_run1_term_pipeline(api_key: str, work_dir: Path, state: dict) -> None:
    """Merge seed + run1 extraction; write auto hard terms and impossible strings."""
    from pdf_transcribe import assemble_run_txt, job_page_numbers
    from pdf_transcribe_source import (
        ensure_source_identity,
        filter_generated_impossible,
        load_impossible_extra,
        tag_page_sections_from_run1,
    )

    slug = ensure_source_identity(state)
    nums = job_page_numbers(state)
    assemble_run_txt(work_dir, 1, nums, source_slug=slug)
    run1_path = work_dir / "run1.txt"
    run1_text = run1_path.read_text(encoding="utf-8") if run1_path.is_file() else ""
    seed = list(state.get("seed_hard_terms") or [])
    profile = state.get("detected_source_profile") or {}
    seed.extend(profile.get("seed_hard_terms") or [])
    from pdf_transcribe_lang import filter_terms_by_min_occurrences, job_language_config_from_state

    extracted = extract_hard_terms_from_run1_text(api_key, run1_text)
    lang_cfg = job_language_config_from_state(state)
    seed_keys = {s.lower() for s in seed}
    extracted = filter_terms_by_min_occurrences(
        extracted, run1_text, min_count=2, always_keep=seed_keys
    )
    merged = merge_hard_terms(seed, extracted)
    hard_path = hard_terms_auto_path(slug)
    write_hard_terms_file(
        hard_path,
        merged,
        "Auto-generated: Phase 0 seeds + run 1 extraction",
    )
    impossible = generate_impossible_strings(api_key, merged)
    impossible = filter_generated_impossible(impossible, merged)
    impossible.extend(load_impossible_extra(slug))
    seen_imp: set[str] = set()
    deduped: list[str] = []
    for item in impossible:
        key = item.lower()
        if key and key not in seen_imp:
            seen_imp.add(key)
            deduped.append(item)
    impossible = deduped
    from pdf_transcribe_integrity import write_impossible_strings_file

    imp_path = write_impossible_strings_file(
        slug,
        impossible,
        note="Auto-generated corrupted variants from hard terms (this source only)",
    )
    tag_page_sections_from_run1(work_dir, state)
    state["hard_terms"] = merged
    state["impossible_strings"] = impossible
    state["hard_terms_file"] = str(hard_path)
    state["impossible_strings_file"] = str(imp_path)
