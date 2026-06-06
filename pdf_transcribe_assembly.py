"""Cross-page text assembly: bracket-aware rejoins and boundary validation."""

from __future__ import annotations

import re

# Page ends with open manuscript reconstruction split at syllable boundary.
_OPEN_BRACKET_END = re.compile(r"\[([^\]\n]+)-\s*$")
# Page ends with an unclosed '[' (no matching ']' on this page).
_UNCLOSED_BRACKET_END = re.compile(r"\[[^\]\n]*$")
# Page starts with closing fragment of a reconstruction.
_CLOSE_BRACKET_START = re.compile(r"^([^\[\n\]]+)\](.*)$", re.DOTALL)
# Page starts with orphan ']' before any '['.
_ORPHAN_CLOSE_START = re.compile(r"^[^\[]*\]")


def has_open_bracket_end_fragment(text: str) -> bool:
    return bool(_OPEN_BRACKET_END.search(text.rstrip()))


def has_close_bracket_start_fragment(text: str) -> bool:
    return bool(_CLOSE_BRACKET_START.match(text.lstrip()))


def has_rejoinable_bracket_split(prev_body: str, next_body: str) -> bool:
    """True when prev ends [fragment- and next starts fragment]."""
    if not prev_body or not next_body:
        return False
    m_open = _OPEN_BRACKET_END.search(prev_body.rstrip())
    m_close = _CLOSE_BRACKET_START.match(next_body.lstrip())
    return bool(m_open and m_close)


def rejoin_bracket_fragment_pair(open_tail: str, close_head: str) -> tuple[str, str] | None:
    """
    Merge '... [de edifi-' + 'car] rest' → ('... [de edificar]', 'rest').
    Returns (merged_prev_suffix, merged_next_prefix) or None.
    """
    m_open = _OPEN_BRACKET_END.search(open_tail.rstrip())
    m_close = _CLOSE_BRACKET_START.match(close_head.lstrip())
    if not m_open or not m_close:
        return None
    inner_open = m_open.group(1)
    inner_close = m_close.group(1)
    rest = m_close.group(2)
    merged = f"[{inner_open}{inner_close}]"
    prefix = open_tail[: m_open.start()]
    return prefix + merged, rest.lstrip("\n")


def find_rejoin_partner(
    page_bodies: dict[int, str],
    page_numbers: list[int],
    start_idx: int,
) -> int | None:
    """Index of the next page that closes a bracket fragment opened at start_idx."""
    opener = page_bodies.get(page_numbers[start_idx], "")
    if not has_open_bracket_end_fragment(opener):
        return None
    for j in range(start_idx + 1, len(page_numbers)):
        closer = page_bodies.get(page_numbers[j], "")
        if has_rejoinable_bracket_split(opener, closer):
            return j
    return None


def apply_cross_page_bracket_rejoins(
    page_bodies: dict[int, str],
    page_numbers: list[int],
) -> dict[int, str]:
    """
    Rejoin manuscript reconstructions split across page breaks.
    Per-page files stay source-accurate; assembly merges bracket fragments only.
    """
    out = dict(page_bodies)
    i = 0
    while i < len(page_numbers):
        pn = page_numbers[i]
        body = out.get(pn, "")
        partner_idx = find_rejoin_partner(out, page_numbers, i)
        if partner_idx is None:
            i += 1
            continue
        partner_pn = page_numbers[partner_idx]
        partner_body = out.get(partner_pn, "")
        merged = rejoin_bracket_fragment_pair(body, partner_body)
        if not merged:
            i += 1
            continue
        new_end, new_start = merged
        out[pn] = new_end
        out[partner_pn] = new_start
        i = partner_idx + 1
    return out


def page_needs_bracket_boundary_review(
    body: str,
    *,
    prev_body: str | None = None,
    next_body: str | None = None,
) -> bool:
    """
    Flag orphan brackets that are not explainable by a cross-page reconstruction split.
    Correct per-page splits like [de edifi- / car] are not flagged.
    """
    stripped = body.strip()
    if not stripped:
        return False

    if prev_body and has_rejoinable_bracket_split(prev_body, body):
        return False
    if next_body and has_rejoinable_bracket_split(body, next_body):
        return False

    if has_close_bracket_start_fragment(stripped) and not (
        prev_body
        and (
            has_open_bracket_end_fragment(prev_body)
            or _UNCLOSED_BRACKET_END.search(prev_body.rstrip())
        )
    ):
        return True

    if has_open_bracket_end_fragment(stripped) and not (
        next_body and has_close_bracket_start_fragment(next_body)
    ):
        return True

    if _UNCLOSED_BRACKET_END.search(stripped) and not has_open_bracket_end_fragment(
        stripped
    ):
        if not (next_body and has_close_bracket_start_fragment(next_body)):
            return True

    if _ORPHAN_CLOSE_START.match(stripped) and not has_close_bracket_start_fragment(
        stripped
    ):
        if not (prev_body and has_open_bracket_end_fragment(prev_body)):
            return True

    return False
