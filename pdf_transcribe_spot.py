"""Spot-check v2: sentence-level constrained patches (pipeline v2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pdf_transcribe_lang import (
    JobLanguageConfig,
    strip_for_term_match,
    strip_whitespace_for_compare,
)

REJECT_MISSING_HARD_TERM = "missing_hard_term"

FOOTNOTE_SEP = "--- FOOTNOTES ---"
_SUPERSCRIPT_CHARS = "¹²³⁴⁵⁶⁷⁸⁹⁰"
_LATIN_CAP = r"A-ZÁÉÍÓÚÑÜÀÂÊÎÔÛÄËÏÖŪĀĒĪŌ"
_JA_TERMINATORS = "。、！？"


@dataclass(frozen=True)
class PatchOperation:
    """One API call: verify a single sentence containing hard term(s)."""

    section: str
    start: int
    end: int
    sentence: str
    terms: tuple[str, ...]
    bracketed_sentence: str
    op_index: int = 0


def bracket_terms_in_sentence(sentence: str, terms: list[str]) -> str:
    out = sentence
    for term in sorted(terms, key=len, reverse=True):
        pattern = re.compile(re.escape(term), re.IGNORECASE)

        def repl(m: re.Match[str]) -> str:
            return f"[{m.group(0)}]"

        out = pattern.sub(repl, out, count=0)
    return out


_SECTION_HINTS: dict[str, str] = {
    "paleographic_nahuatl": (
        "This page is a paleographic Nahuatl section — preserve colonial spellings, "
        "cedillas, and bracketed manuscript reconstructions exactly as printed. "
        "Do not apply modern Spanish accent normalization.\n"
    ),
    "spanish_translation": (
        "This page is a modern Spanish translation section — use modern Spanish "
        "orthography for proper names (accents as in the edition). "
        "Do not preserve colonial Nahuatl spellings on this page.\n"
    ),
    "editorial_spanish": (
        "This page is editorial/front matter in modern Spanish.\n"
    ),
    "mixed": (
        "This page mixes Spanish and Nahuatl — preserve each term exactly as printed "
        "in the image without cross-section normalization.\n"
    ),
}


def build_patch_prompt(
    sentence: str,
    terms: list[str],
    lang_cfg: JobLanguageConfig,
    *,
    section_hint: str | None = None,
) -> str:
    bracketed = bracket_terms_in_sentence(sentence, terms)
    term_list = ", ".join(terms)
    section_note = _SECTION_HINTS.get(section_hint or "", "")
    return (
        "You are verifying a single sentence from a transcription of a historical book. "
        "The full page image is provided so you can locate and read the relevant text.\n"
        f"{section_note}"
        f"Source language: {lang_cfg.language}\n"
        f"Source script: {lang_cfg.script}\n"
        f"Script direction: {lang_cfg.direction}\n"
        "The sentence to verify is:\n"
        f"{bracketed}\n"
        f"The bracketed terms are the ones to check: {term_list}\n"
        "Instructions:\n"
        "— Look at the page image and find this sentence\n"
        "— Check each bracketed term against exactly what is printed in the image\n"
        "— Return only the corrected sentence with the brackets removed\n"
        "— Do not change anything outside the bracketed terms\n"
        f"— {lang_cfg.normalization_rule}\n"
        "— Do not correct what appears to be a printing error — if the image shows it, "
        "transcribe it exactly and add [sic] immediately after\n"
        "— If a footnote is numbered 8 in the image transcribe 8 even if the in-text superscript is ³\n"
        "— Preserve all emphasis markers exactly as they appear in the original sentence\n"
        "— If you cannot locate this sentence in the image or cannot read it clearly "
        "return the original sentence completely unchanged\n"
        "— Return nothing except the single corrected sentence. No explanation, no preamble."
    )


def _join_hyphen_breaks(text: str) -> str:
    return re.sub(r"-\s*\n\s*", "", text)


def _logical_text(section: str) -> str:
    paragraphs = re.split(r"\n\s*\n", section.strip())
    return "\n\n".join(re.sub(r"\n", " ", p.strip()) for p in paragraphs if p.strip())


def _latin_sentence_spans(logical: str) -> list[tuple[int, int]]:
    if not logical.strip():
        return []
    boundaries = [0]
    for m in re.finditer(
        rf"(?<=[.;])\s+(?=[{_LATIN_CAP}*\"'])|(?<=[{_JA_TERMINATORS}])|(?:\n\n+)",
        logical,
    ):
        boundaries.append(m.start())
        boundaries.append(m.end())
    boundaries.append(len(logical))
    boundaries = sorted(set(boundaries))
    spans: list[tuple[int, int]] = []
    i = 0
    while i + 1 < len(boundaries):
        start = boundaries[i]
        end = boundaries[i + 1]
        chunk = logical[start:end].strip()
        if chunk:
            spans.append((start, end))
        i += 1
    if not spans:
        spans = [(0, len(logical))]
    return spans


def _cjk_sentence_spans(logical: str) -> list[tuple[int, int]]:
    if not logical.strip():
        return []
    parts = re.split(rf"([{_JA_TERMINATORS}])", logical)
    spans: list[tuple[int, int]] = []
    pos = 0
    buf = ""
    buf_start = 0
    for part in parts:
        if not part:
            continue
        if buf == "":
            buf_start = pos
        buf += part
        pos += len(part)
        if part in _JA_TERMINATORS or part.strip() == "":
            if buf.strip():
                spans.append((buf_start, pos))
            buf = ""
    if buf.strip():
        spans.append((buf_start, pos))
    return spans or [(0, len(logical))]


def _sentence_spans(logical: str, lang_cfg: JobLanguageConfig) -> list[tuple[int, int]]:
    if lang_cfg.uses_cjk_boundaries:
        return _cjk_sentence_spans(logical)
    return _latin_sentence_spans(logical)


def _flex_find(haystack: str, needle: str) -> int | None:
    if not needle:
        return None
    idx = haystack.find(needle)
    if idx >= 0:
        return idx
    pattern = re.escape(needle.strip())
    pattern = pattern.replace(r"\ ", r"\s+")
    m = re.search(pattern, haystack, re.IGNORECASE | re.DOTALL)
    return m.start() if m else None


def _map_logical_span_to_original(original: str, logical: str, l_start: int, l_end: int) -> tuple[int, int] | None:
    snippet = logical[l_start:l_end].strip()
    if not snippet:
        return None
    flex = re.escape(snippet[: min(40, len(snippet))])
    flex = flex.replace(r"\ ", r"[\s\n]+")
    m = re.search(flex, original, re.IGNORECASE | re.DOTALL)
    if not m:
        idx = _flex_find(original, snippet)
        if idx is None:
            return None
        return idx, idx + len(snippet)
    start = m.start()
    end_pat = re.escape(snippet[-min(30, len(snippet)) :]).replace(r"\ ", r"[\s\n]+")
    m_end = re.search(end_pat, original[start:], re.IGNORECASE | re.DOTALL)
    end = start + (m_end.end() if m_end else len(snippet))
    return start, end


def _terms_in_sentence(sentence: str, terms: list[str], lang_cfg: JobLanguageConfig) -> list[str]:
    view = sentence
    if lang_cfg.hyphenation_join:
        view = _join_hyphen_breaks(view)
    view = strip_for_term_match(view)
    lower = view.lower()
    found: list[str] = []
    for term in terms:
        if term.lower() in lower:
            found.append(term)
    return found


def _word_count_before(text: str, pos: int) -> int:
    return len(re.findall(r"\S+", text[:pos]))


def _maybe_widen_heading(
    logical: str,
    spans: list[tuple[int, int]],
    span_idx: int,
    lang_cfg: JobLanguageConfig,
) -> int:
    """Heading-only sentence: include next sentence for context."""
    l_start, l_end = spans[span_idx]
    sentence = logical[l_start:l_end].strip()
    words = re.findall(r"\S+", sentence)
    if len(words) > 8:
        return span_idx
    if not sentence.rstrip().endswith((".", ";", ":", "。", "、", "！", "？")):
        if span_idx + 1 < len(spans):
            return span_idx + 1
    if len(words) <= 6 and span_idx + 1 < len(spans):
        return span_idx + 1
    return span_idx


def _merge_span_range(
    spans: list[tuple[int, int]], start_idx: int, end_idx: int
) -> tuple[int, int]:
    return spans[start_idx][0], spans[end_idx][1]


def _find_term_positions(text: str, term: str, lang_cfg: JobLanguageConfig) -> list[int]:
    view = text
    if lang_cfg.hyphenation_join:
        view = _join_hyphen_breaks(view)
    view = strip_for_term_match(view)
    positions: list[int] = []
    lower_view = view.lower()
    t = term.lower()
    start = 0
    while True:
        idx = lower_view.find(t, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + max(1, len(t))
    return positions


def collect_patch_operations(
    page_text: str,
    terms: list[str],
    lang_cfg: JobLanguageConfig,
) -> list[PatchOperation]:
    if not terms or not page_text.strip():
        return []

    sections: list[tuple[str, str]] = []
    if FOOTNOTE_SEP in page_text:
        body, foot = page_text.split(FOOTNOTE_SEP, 1)
        sections.append(("body", body))
        sections.append(("footnotes", foot))
    else:
        sections.append(("body", page_text))

    operations: list[PatchOperation] = []
    op_index = 0
    section_offset = 0

    for section_name, section_text in sections:
        logical = _logical_text(section_text)
        spans = _sentence_spans(logical, lang_cfg)
        handled: set[tuple[int, int]] = set()

        for span_idx, (l_start, l_end) in enumerate(spans):
            logical_sentence = logical[l_start:l_end]
            matched_terms = _terms_in_sentence(logical_sentence, terms, lang_cfg)
            if not matched_terms:
                continue

            use_idx = span_idx
            if _word_count_before(logical, l_start) < 10 or _word_count_before(
                logical, l_end
            ) > len(re.findall(r"\S+", logical)) - 10:
                widened = _maybe_widen_heading(logical, spans, span_idx, lang_cfg)
                if widened != span_idx:
                    l_start, l_end = _merge_span_range(spans, span_idx, widened)
                    logical_sentence = logical[l_start:l_end]
                    matched_terms = _terms_in_sentence(logical_sentence, terms, lang_cfg)

            mapped = _map_logical_span_to_original(section_text, logical, l_start, l_end)
            if not mapped:
                continue
            o_start, o_end = mapped
            key = (section_offset + o_start, section_offset + o_end)
            if key in handled:
                continue
            handled.add(key)

            sentence = page_text[section_offset + o_start : section_offset + o_end]
            if not sentence.strip():
                continue

            operations.append(
                PatchOperation(
                    section=section_name,
                    start=section_offset + o_start,
                    end=section_offset + o_end,
                    sentence=sentence,
                    terms=tuple(matched_terms),
                    bracketed_sentence=bracket_terms_in_sentence(sentence, matched_terms),
                    op_index=op_index,
                )
            )
            op_index += 1

        section_offset += len(section_text)
        if section_name == "body" and FOOTNOTE_SEP in page_text:
            section_offset += len(FOOTNOTE_SEP)

    return operations


def patch_operation_priority(op: PatchOperation) -> tuple[int, int, int]:
    max_term = max((len(t) for t in op.terms), default=0)
    return (max_term, len(op.terms), len(op.sentence))


def cap_patch_operations(
    operations: list[PatchOperation],
    max_per_page: int = 8,
) -> list[PatchOperation]:
    if len(operations) <= max_per_page:
        return operations
    ranked = sorted(
        enumerate(operations),
        key=lambda item: patch_operation_priority(item[1]),
        reverse=True,
    )
    keep_indices = sorted(idx for idx, _ in ranked[:max_per_page])
    return [operations[i] for i in keep_indices]


def _emphasis_markers(sentence: str, lang_cfg: JobLanguageConfig) -> dict[str, int]:
    if lang_cfg.emphasis == "asterisk":
        return {"*": sentence.count("*")}
    if lang_cfg.emphasis == "double_asterisk":
        return {"**": sentence.count("**")}
    if lang_cfg.emphasis == "japanese_quotes":
        return {
            "《": sentence.count("《"),
            "》": sentence.count("》"),
            "「": sentence.count("「"),
            "」": sentence.count("」"),
        }
    return {}


def sentence_contains_hard_terms(
    sentence: str,
    terms: tuple[str, ...],
    lang_cfg: JobLanguageConfig,
) -> bool:
    """True if every term that triggered the patch still appears in the sentence."""
    if not terms:
        return True
    matched = _terms_in_sentence(sentence, list(terms), lang_cfg)
    return len(matched) == len(terms)


def validate_patch_response(
    original_sentence: str,
    returned: str,
    lang_cfg: JobLanguageConfig,
    impossible_strings: list[str],
    expected_terms: tuple[str, ...] = (),
) -> tuple[str, str | None]:
    """Return (sentence to use, reject_reason). reject_reason set when patch is rejected."""
    candidate = returned.strip()
    if not candidate:
        return original_sentence, "empty_response"
    if looks_like_chatter_patch(candidate):
        return original_sentence, "chatter"

    for bad in impossible_strings:
        if bad and bad.lower() in candidate.lower():
            return original_sentence, "impossible_string"

    orig_markers = _emphasis_markers(original_sentence, lang_cfg)
    new_markers = _emphasis_markers(candidate, lang_cfg)
    if orig_markers != new_markers:
        return original_sentence, "emphasis_markers"

    if strip_whitespace_for_compare(candidate) == strip_whitespace_for_compare(original_sentence):
        return original_sentence, "unchanged"

    if expected_terms and not sentence_contains_hard_terms(candidate, expected_terms, lang_cfg):
        return original_sentence, REJECT_MISSING_HARD_TERM

    return candidate, None


def looks_like_chatter_patch(text: str) -> bool:
    lower = text.lower()
    markers = (
        "i can see",
        "i cannot",
        "however,",
        "the sentence",
        "here is",
        "corrected sentence",
    )
    return any(m in lower for m in markers)


def apply_patch(text: str, start: int, end: int, new_sentence: str) -> str:
    if start < 0 or end > len(text) or start >= end:
        return text
    return text[:start] + new_sentence + text[end:]


def apply_all_patches(
    base_text: str,
    operations: list[PatchOperation],
    responses: list[str],
    lang_cfg: JobLanguageConfig,
    impossible_strings: list[str],
) -> tuple[str, int, int, list[dict]]:
    """Return (patched_text, applied_count, rejected_count, per-op log entries)."""
    pairs: list[tuple[PatchOperation, str]] = []
    applied = 0
    rejected = 0
    log_entries: list[dict] = []
    for op, raw in zip(operations, responses, strict=False):
        chosen, reason = validate_patch_response(
            op.sentence,
            raw,
            lang_cfg,
            impossible_strings,
            expected_terms=op.terms,
        )
        entry = {
            "op_index": op.op_index,
            "section": op.section,
            "terms": list(op.terms),
            "reject_reason": reason,
            "needs_human_review": reason == REJECT_MISSING_HARD_TERM,
        }
        if reason:
            rejected += 1
            log_entries.append(entry)
            continue
        applied += 1
        entry["applied"] = True
        log_entries.append(entry)
        pairs.append((op, chosen))
    for op, new_text in sorted(pairs, key=lambda x: x[0].start, reverse=True):
        base_text = apply_patch(base_text, op.start, op.end, new_text)
    return base_text, applied, rejected, log_entries
