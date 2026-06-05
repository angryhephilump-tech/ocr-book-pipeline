#!/usr/bin/env python3
"""Quick logic tests (no API calls). Run: python scripts/test_transcribe_logic.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pdf_transcribe as pt  # noqa: E402
from pdf_transcribe_lang import (  # noqa: E402
    job_language_config,
    pages_need_content_reconcile,
    strip_whitespace_for_compare,
)
from pdf_transcribe_spot import (  # noqa: E402
    REJECT_MISSING_HARD_TERM,
    apply_all_patches,
    bracket_terms_in_sentence,
    collect_patch_operations,
    validate_patch_response,
)


def test_skip_format() -> None:
    assert pt.skip_line("blank page") == "[Skipped: blank page]"
    assert pt.is_skip_body("[Skipped: Google boilerplate]")


def test_pages_disagree() -> None:
    assert pt.pages_disagree("a", "b")
    assert not pt.pages_disagree("same", "same")


def test_content_reconcile_skip() -> None:
    a = "Hola mundo.\nSegunda linea."
    b = "Hola  mundo.\n  Segunda linea."
    assert pt.pages_disagree(a, b)
    assert not pages_need_content_reconcile(a, b)
    assert strip_whitespace_for_compare(a) == strip_whitespace_for_compare(b)
    c = "Hola mundo.\nSegunda línea."
    assert pages_need_content_reconcile(a, c)


def test_hard_terms() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    terms = ["Ixtlilxóchitl", "Nezahualcoyotl"]
    assert pt.page_needs_hard_term_spot_check("Text by Nezahualcoyotl here", terms, cfg)
    assert not pt.page_needs_hard_term_spot_check("[Skipped: blank page]", terms, cfg)


def test_parse_reconcile_uncertain() -> None:
    raw = "Line one\nLine two\nUNCERTAIN: faded ink on name"
    body, uncertain, note = pt.parse_reconcile_output(raw)
    assert uncertain
    assert "faded" in note
    assert "UNCERTAIN" not in body


def test_parse_batch_ids() -> None:
    assert pt.parse_batch_custom_id("r1_p0042") == ("transcribe", 1, 42)
    assert pt.parse_batch_custom_id("rec_p0007") == ("reconcile", 0, 7)
    assert pt.parse_batch_custom_id("spot_p0003") == ("spot", 0, 3)
    assert pt.parse_batch_custom_id("spot_p0003_02") == ("spot", 2, 3)


def test_work_page_range() -> None:
    r = pt.resolve_work_page_range(500, skip_front_pages=12, max_pages=10)
    assert r.page_numbers == list(range(13, 23))
    assert r.first_page == 13
    assert r.last_page == 22
    r2 = pt.resolve_work_page_range(500, skip_front_pages=2, max_pages=10)
    assert r2.page_numbers == list(range(3, 13))


def test_chatter_to_skip() -> None:
    chatter = "I can see this image shows a blank page with no text."
    out = pt.normalize_transcription_output(chatter)
    assert out.startswith("[Skipped:")


def test_bracket_terms() -> None:
    s = "Ordenanzas de Nezahualcoyotl en Texcoco."
    out = bracket_terms_in_sentence(s, ["Nezahualcoyotl", "Texcoco"])
    assert "[Nezahualcoyotl]" in out
    assert "[Texcoco]" in out


def test_collect_patch_ops() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    text = "El rey Nezahualcoyotl gobernó Texcoco.\nOtra línea sin nombres."
    terms = ["Nezahualcoyotl", "Texcoco"]
    ops = collect_patch_operations(text, terms, cfg)
    assert len(ops) >= 1
    assert any("Nezahualcoyotl" in op.sentence for op in ops)


def test_impossible_string_rejects_patch() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    original = "Nezahualcoyotl reinó en Texcoco."
    bad = "Nezahualcoyotf reinó en Texcoco."
    impossible = ["Nezahualcoyotf"]
    chosen, reason = validate_patch_response(
        original, bad, cfg, impossible, expected_terms=("Nezahualcoyotl",)
    )
    assert chosen == original
    assert reason == "impossible_string"


def test_missing_hard_term_rejects_patch() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    original = "Nezahualcoyotl reinó en Texcoco."
    wrong_sentence = "El rey gobernó muchos años sin nombre."
    chosen, reason = validate_patch_response(
        original,
        wrong_sentence,
        cfg,
        [],
        expected_terms=("Nezahualcoyotl", "Texcoco"),
    )
    assert chosen == original
    assert reason == REJECT_MISSING_HARD_TERM


def test_apply_patch_preserves_rest() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    base = "Linea uno.\nNezahualcoyotl aquí.\nLinea tres."
    ops = collect_patch_operations(base, ["Nezahualcoyotl"], cfg)
    assert ops
    patched, applied, rejected, _log = apply_all_patches(
        base, ops, [ops[0].sentence], cfg, []
    )
    assert applied == 0
    assert patched == base


def main() -> int:
    tests = [
        test_skip_format,
        test_pages_disagree,
        test_content_reconcile_skip,
        test_hard_terms,
        test_parse_reconcile_uncertain,
        test_parse_batch_ids,
        test_work_page_range,
        test_chatter_to_skip,
        test_bracket_terms,
        test_collect_patch_ops,
        test_impossible_string_rejects_patch,
        test_missing_hard_term_rejects_patch,
        test_apply_patch_preserves_rest,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
