"""Spot-check v2: sentence-level constrained patches (pipeline v2)."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from pdf_transcribe_lang import (
    JobLanguageConfig,
    strip_for_term_match,
    strip_whitespace_for_compare,
    term_present_in_text,
)

REJECT_MISSING_HARD_TERM = "missing_hard_term"
REJECT_IMPOSSIBLE_SIC = "impossible_string"

_SIC_RE = re.compile(r"\[sic\]", re.IGNORECASE)

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
        "This page is a Spanish narrative section (colonial or modern translation). "
        "Archaic Spanish orthography (ç, q̄/q̃ abbreviation strokes, missing accents) "
        "is still Spanish — not paleographic Nahuatl. "
        "Use edition-appropriate Spanish for proper names; do not apply Nahuatl macron rules.\n"
    ),
    "editorial_spanish": (
        "This page is editorial/front matter in modern Spanish.\n"
    ),
    "mixed": (
        "This page mixes Spanish and Nahuatl — preserve each term exactly as printed "
        "in the image without cross-section normalization.\n"
    ),
}


def _patch_instruction_block(lang_cfg: JobLanguageConfig) -> str:
    return (
        "Instructions:\n"
        "— Look at the page image and find each sentence\n"
        "— Check each bracketed term against exactly what is printed in the image\n"
        "— Return only the corrected sentence with the brackets removed\n"
        "— Do not change anything outside the bracketed terms\n"
        f"— {lang_cfg.normalization_rule}\n"
        "— Do not correct what appears to be a printing error — if the image shows it, "
        "transcribe it exactly and add [sic] immediately after\n"
        "— Never add [sic] to words flagged as impossible OCR corruptions — leave those "
        "unchanged for human review\n"
        "— If a footnote is numbered 8 in the image transcribe 8 even if the in-text superscript is ³\n"
        "— Preserve all emphasis markers exactly as they appear in the original sentence\n"
        "— If you cannot locate a sentence in the image or cannot read it clearly "
        "return that sentence completely unchanged\n"
    )


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
        f"{_patch_instruction_block(lang_cfg)}"
        "— Return nothing except the single corrected sentence. No explanation, no preamble."
    )


def build_page_patch_prompt(
    operations: list[PatchOperation],
    lang_cfg: JobLanguageConfig,
    *,
    section_hint: str | None = None,
) -> str:
    """Single prompt verifying all patch operations on one page."""
    section_note = _SECTION_HINTS.get(section_hint or "", "")
    sorted_ops = sorted(operations, key=lambda op: op.op_index)
    lines: list[str] = [
        "You are verifying multiple sentences from a transcription of a historical book. "
        "The full page image is provided so you can locate and read the relevant text.\n"
        f"{section_note}"
        f"Source language: {lang_cfg.language}\n"
        f"Source script: {lang_cfg.script}\n"
        f"Script direction: {lang_cfg.direction}\n"
        "Sentences to verify (numbered in order):\n",
    ]
    for i, op in enumerate(sorted_ops, start=1):
        term_list = ", ".join(op.terms)
        lines.append(f"{i}. {op.bracketed_sentence}")
        lines.append(f"   Terms to check: {term_list}")
    lines.append("")
    lines.append(_patch_instruction_block(lang_cfg))
    lines.append(
        f"— Return EXACTLY {len(sorted_ops)} numbered sentence(s), in this format:\n"
        "  N: <corrected sentence with brackets removed>\n"
        "— Put each numbered sentence on one single line — do not wrap or break lines\n"
        "— Reproduce each sentence in full; never truncate or omit text\n"
        "— Use the same numbering (1, 2, …) and order as given above\n"
        "— Return nothing else — no explanation, no preamble, no extra lines"
    )
    return "\n".join(lines)


_PAGE_PATCH_MARKER_RE = re.compile(
    r"^\s*(\d+)\s*[:.)]",
    re.MULTILINE,
)

SPOT_PAGE_CHARS_PER_TOKEN = 3
SPOT_PAGE_OUTPUT_TOKEN_MARGIN = 512
SPOT_PAGE_MAX_OUTPUT_TOKENS = 8192


def estimate_spot_output_tokens(sentences: list[str]) -> int:
    """Rough output token budget: ~1 token per 3 chars plus margin."""
    total_chars = sum(len(s) for s in sentences)
    return total_chars // SPOT_PAGE_CHARS_PER_TOKEN + SPOT_PAGE_OUTPUT_TOKEN_MARGIN


def page_spot_output_max_tokens(operations: list[PatchOperation]) -> int:
    """max_tokens for a page-level spot call, scaled to expected response size."""
    sentences = [op.sentence for op in operations]
    needed = estimate_spot_output_tokens(sentences)
    return min(SPOT_PAGE_MAX_OUTPUT_TOKENS, max(256, needed))


def split_patch_operations_by_token_budget(
    operations: list[PatchOperation],
    *,
    max_output_tokens: int = SPOT_PAGE_MAX_OUTPUT_TOKENS,
) -> list[list[PatchOperation]]:
    """Split page ops into multiple API requests when output would exceed safe budget."""
    if not operations:
        return []
    sorted_ops = sorted(operations, key=lambda op: op.op_index)
    total = estimate_spot_output_tokens([op.sentence for op in sorted_ops])
    if total <= max_output_tokens:
        return [sorted_ops]

    chunks: list[list[PatchOperation]] = []
    current: list[PatchOperation] = []
    current_tokens = SPOT_PAGE_OUTPUT_TOKEN_MARGIN
    for op in sorted_ops:
        op_tokens = max(1, len(op.sentence) // SPOT_PAGE_CHARS_PER_TOKEN)
        if current and current_tokens + op_tokens > max_output_tokens:
            chunks.append(current)
            current = []
            current_tokens = SPOT_PAGE_OUTPUT_TOKEN_MARGIN
        current.append(op)
        current_tokens += op_tokens
    if current:
        chunks.append(current)
    return chunks


def parse_page_patch_response(
    text: str,
    expected_count: int,
    originals: list[str],
) -> list[str]:
    """Extract per-sentence responses; missing/unparseable → original sentence."""
    if expected_count <= 0:
        return []
    fallback = list(originals[:expected_count])
    while len(fallback) < expected_count:
        fallback.append("")
    parsed: dict[int, str] = {}
    raw = text or ""
    matches = list(_PAGE_PATCH_MARKER_RE.finditer(raw))
    for i, match in enumerate(matches):
        try:
            idx = int(match.group(1))
        except ValueError:
            continue
        if idx < 1 or idx > expected_count or idx in parsed:
            continue
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = re.sub(r"\s+", " ", raw[body_start:body_end].strip())
        if body:
            parsed[idx] = body
    return [parsed.get(i + 1, fallback[i]) for i in range(expected_count)]


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


_MAX_FLEX_MATCH_ANCHOR_TRIES = 48
_COMMON_ANCHOR_TOKENS = frozenset(
    {
        "a",
        "al",
        "con",
        "de",
        "del",
        "el",
        "en",
        "es",
        "la",
        "las",
        "le",
        "lo",
        "los",
        "por",
        "que",
        "se",
        "su",
        "un",
        "una",
        "y",
    }
)


def _pick_anchor_token_index(tokens: list[str], lower_h: str) -> int:
    """Prefer longest token; among ties prefer rarest in haystack (not common stop-words)."""
    best_idx = 0
    best_key: tuple[int, int, int] = (-1, 0, 0)
    for i, tok in enumerate(tokens):
        tok_l = tok.lower()
        count = lower_h.count(tok_l)
        uncommon = 0 if tok_l in _COMMON_ANCHOR_TOKENS else 1
        key = (len(tok), uncommon, -count)
        if key > best_key:
            best_key = key
            best_idx = i
    return best_idx


def _tokens_match_at_anchor(
    haystack: str,
    tokens: list[str],
    anchor_idx: int,
    anchor_start: int,
) -> tuple[int, int] | None:
    anchor = tokens[anchor_idx]
    if haystack[anchor_start : anchor_start + len(anchor)].lower() != anchor.lower():
        return None

    span_start = anchor_start
    cur = anchor_start
    for i in range(anchor_idx - 1, -1, -1):
        tok = tokens[i]
        end = cur
        while end > 0 and haystack[end - 1].isspace():
            end -= 1
        start = end - len(tok)
        if start < 0 or haystack[start:end].lower() != tok.lower():
            return None
        span_start = start
        cur = start

    cur = anchor_start + len(anchor)
    for i in range(anchor_idx + 1, len(tokens)):
        tok = tokens[i]
        while cur < len(haystack) and haystack[cur].isspace():
            cur += 1
        if haystack[cur : cur + len(tok)].lower() != tok.lower():
            return None
        cur += len(tok)
    return span_start, cur


def _flex_match_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """Find needle in haystack allowing flexible whitespace (linear scan, no regex backtracking)."""
    needle = needle.strip()
    if not needle:
        return None
    idx = haystack.find(needle)
    if idx >= 0:
        return idx, idx + len(needle)

    tokens = needle.split()
    if not tokens:
        return None
    if len(tokens) == 1:
        token = tokens[0]
        lower_h = haystack.lower()
        i = lower_h.find(token.lower())
        return (i, i + len(token)) if i >= 0 else None

    lower_h = haystack.lower()
    anchor_idx = _pick_anchor_token_index(tokens, lower_h)
    anchor = tokens[anchor_idx].lower()
    pos = 0
    tries = 0
    while pos <= len(haystack) and tries < _MAX_FLEX_MATCH_ANCHOR_TRIES:
        start = lower_h.find(anchor, pos)
        if start < 0:
            break
        tries += 1
        matched = _tokens_match_at_anchor(haystack, tokens, anchor_idx, start)
        if matched:
            return matched
        pos = start + 1
    return None


def _flex_find(haystack: str, needle: str) -> int | None:
    span = _flex_match_span(haystack, needle)
    return span[0] if span else None


def _map_logical_span_to_original(original: str, logical: str, l_start: int, l_end: int) -> tuple[int, int] | None:
    snippet = logical[l_start:l_end].strip()
    if not snippet:
        return None
    return _flex_match_span(original, snippet)


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


def _logical_word_spans(logical: str) -> tuple[list[tuple[int, int]], list[int]]:
    """Word (start, end) spans in logical text and parallel start offsets for bisect."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", logical)]
    starts = [s for s, _ in spans]
    return spans, starts


def _word_count_before_spans(word_starts: list[int], pos: int) -> int:
    return bisect.bisect_left(word_starts, pos)


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
        word_spans, word_starts = _logical_word_spans(logical)
        total_words = len(word_spans)
        handled: set[tuple[int, int]] = set()

        for span_idx, (l_start, l_end) in enumerate(spans):
            logical_sentence = logical[l_start:l_end]
            matched_terms = _terms_in_sentence(logical_sentence, terms, lang_cfg)
            if not matched_terms:
                continue

            use_idx = span_idx
            if _word_count_before_spans(word_starts, l_start) < 10 or _word_count_before_spans(
                word_starts, l_end
            ) > total_words - 10:
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
    *,
    section_hint: str | None = None,
) -> bool:
    """True if every term that triggered the patch still appears in the sentence."""
    if not terms:
        return True
    return all(
        term_present_in_text(t, sentence, lang_cfg, section_hint=section_hint) for t in terms
    )


def _patch_added_sic(original: str, candidate: str) -> bool:
    return len(_SIC_RE.findall(candidate)) > len(_SIC_RE.findall(original))


def _sentence_has_impossible_token(sentence: str, impossible_strings: list[str]) -> bool:
    lower = sentence.lower()
    return any(bad and bad.lower() in lower for bad in impossible_strings)


def validate_patch_response(
    original_sentence: str,
    returned: str,
    lang_cfg: JobLanguageConfig,
    impossible_strings: list[str],
    expected_terms: tuple[str, ...] = (),
    *,
    section_hint: str | None = None,
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

    if impossible_strings and _patch_added_sic(original_sentence, candidate):
        if _sentence_has_impossible_token(original_sentence, impossible_strings):
            return original_sentence, REJECT_IMPOSSIBLE_SIC
        for bad in impossible_strings:
            if bad and bad.lower() in candidate.lower():
                return original_sentence, REJECT_IMPOSSIBLE_SIC

    orig_markers = _emphasis_markers(original_sentence, lang_cfg)
    new_markers = _emphasis_markers(candidate, lang_cfg)
    if orig_markers != new_markers:
        return original_sentence, "emphasis_markers"

    if strip_whitespace_for_compare(candidate) == strip_whitespace_for_compare(original_sentence):
        return original_sentence, "unchanged"

    if expected_terms and not sentence_contains_hard_terms(
        candidate, expected_terms, lang_cfg, section_hint=section_hint
    ):
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
    *,
    section_hint: str | None = None,
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
            section_hint=section_hint,
        )
        needs_review = reason in (
            REJECT_MISSING_HARD_TERM,
            REJECT_IMPOSSIBLE_SIC,
            "impossible_string",
        ) or _sentence_has_impossible_token(op.sentence, impossible_strings)
        entry = {
            "op_index": op.op_index,
            "section": op.section,
            "terms": list(op.terms),
            "reject_reason": reason,
            "needs_human_review": needs_review,
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
