#!/usr/bin/env python3
"""Quick logic tests (no API calls). Run: python scripts/test_transcribe_logic.py"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pdf_transcribe as pt  # noqa: E402
from pdf_transcribe_detect import (  # noqa: E402
    merge_hard_terms,
    parse_detection_response,
    pick_detection_sample_pages,
    slugify_source_name,
)
from pdf_transcribe_lang import (  # noqa: E402
    build_normalization_rules_text,
    job_language_config,
    load_impossible_strings,
    pages_need_content_reconcile,
    strip_whitespace_for_compare,
)
from pdf_transcribe_sanity import (  # noqa: E402
    check_page_sanity,
    script_mismatch_detail,
)
from pdf_transcribe_source import (  # noqa: E402
    SourceIdentityError,
    classify_page_section,
    direction_for_script,
    ensure_source_identity,
)
from pdf_transcribe_spot import (  # noqa: E402
    REJECT_MISSING_HARD_TERM,
    apply_all_patches,
    bracket_terms_in_sentence,
    cap_patch_operations,
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


def test_script_mismatch_latin() -> None:
    spanish = "El rey gobernó en Texcoco con sus consejeros."
    assert script_mismatch_detail(spanish, "latin") is None
    korean = "이것은 한국어 텍스트입니다 " * 6
    assert script_mismatch_detail(korean, "latin") is not None


def test_too_short_on_content_page() -> None:
    from PIL import Image

    img_path = Path(__file__).resolve().parent / "_sanity_test_page.png"
    Image.new("L", (200, 300), color=128).save(img_path)
    try:
        fails = check_page_sanity(
            "Hola.",
            script="latin",
            image_path=img_path,
            is_skip_fn=pt.is_skip_body,
        )
        assert any(r == "too_short" for r, _ in fails)
    finally:
        if img_path.is_file():
            img_path.unlink()


def test_parse_detection_response() -> None:
    raw = """{"languages": "Spanish 60%, Kaqchikel Maya 40%", "script": "Latin",
    "direction": "left-to-right", "era": "16th century colonial",
    "seed_hard_terms": ["Hunyg", "Atitlan"], "footnotes": true, "headers": false,
    "avg_words_per_page": 220}"""
    profile = parse_detection_response(raw)
    assert "spanish" in profile["languages"]
    assert "kaqchikel" in profile["languages"]
    assert profile["script"] == "latin"
    assert profile["direction"] == "ltr"
    assert profile["seed_hard_terms"] == ["Hunyg", "Atitlan"]
    rules = profile["normalization_rules"]
    assert "Kaqchikel" in rules or "kaqchikel" in rules.lower()
    assert "Spanish" in rules or "fué" in rules


def test_latin_script_forces_ltr() -> None:
    assert direction_for_script("latin", "rtl") == "ltr"
    assert direction_for_script("latin", "mixed") == "ltr"
    assert direction_for_script("arabic", "ltr") == "rtl"
    raw = """{"languages": "Spanish 70%, Nahuatl 30%", "script": "Latin",
    "direction": "right-to-left", "era": "modern", "seed_hard_terms": [],
    "footnotes": false, "headers": false, "avg_words_per_page": 200}"""
    profile = parse_detection_response(raw)
    assert profile["direction"] == "ltr"


def test_source_identity_mismatch_raises() -> None:
    state = {"source_name": "anales_de_tlatelolco", "source_id": "ixtlilxochitl"}
    try:
        ensure_source_identity(state)
        raise AssertionError("expected SourceIdentityError")
    except SourceIdentityError:
        pass


def test_impossible_strings_per_source() -> None:
    state = {
        "source_name": "anales_de_tlatelolco",
        "source_id": "anales_de_tlatelolco",
        "impossible_strings": [],
    }
    imp = load_impossible_strings(state=state)
    assert "unoaconejaron" in [s.lower() for s in imp]


def test_patch_cap() -> None:
    cfg = job_language_config("spanish", "test")
    text = "\n".join(
        f"Ordenanza {i} con Nezahualcoyotl y Texcoco en la línea."
        for i in range(20)
    )
    terms = ["Nezahualcoyotl", "Texcoco"]
    ops = collect_patch_operations(text, terms, cfg)
    capped = cap_patch_operations(ops, 8)
    assert len(capped) <= 8
    assert len(ops) > 8


def test_classify_page_section() -> None:
    paleo = (
        "Nican moteneu [xiuhmolpilli] y [Nican moteneu] que Ācatl ça çoatl "
        "en el texto náhuatl colonial."
    )
    assert classify_page_section(paleo) == "paleographic_nahuatl"

    editorial = (
        "PRESENTACIÓN\n\nLa biblioteca universitaria presenta esta edición "
        "de paleografía y transcripción del manuscrito náhuatl. "
        "Sin embargo, cabe señalar que el presente volumen…"
    )
    assert classify_page_section(editorial) == "editorial_spanish"

    # Editorial intro mentioning náhuatl must not become paleographic.
    editorial2 = (
        "Capítulo introductorio. La transcripción del texto náhuatl y la "
        "paleografía fueron realizadas en la universidad. Por lo tanto…"
    )
    assert classify_page_section(editorial2) == "editorial_spanish"

    translation = (
        "Moquíhuix reinó en Tenochtitlan. Los mexicas dieron principio "
        "al año del fuego. Dice el texto en español lo que sigue…"
    )
    assert classify_page_section(translation) == "spanish_translation"


def test_classify_anales_pilot_page_pattern() -> None:
    """Regression: Anales pages 2-7 editorial, 8/10 paleographic, 9/11 translation."""
    pages = {
        2: (
            "PRESENTACIÓN\n\nEdición de la biblioteca nacional. "
            "Paleografía y transcripción del códice. Sin embargo…"
        ),
        7: (
            "El manuscrito náhuatl y su estudio preliminar en la universidad. "
            "Cabe señalar que la colección…"
        ),
        8: "[Nican moteneu] [xiuhmolpilli] ça Ācatl moquiuix",
        9: "Moquíhuix y los mexicas en Tlatelolco. Traducción al español del pasaje.",
        10: "[quauhtla] [xochitl] Ēhecatl çoatl",
        11: "Los mexicas se aconsejaron. En español el relato continúa.",
    }
    expected = {
        2: "editorial_spanish",
        7: "editorial_spanish",
        8: "paleographic_nahuatl",
        9: "spanish_translation",
        10: "paleographic_nahuatl",
        11: "spanish_translation",
    }
    for num, body in pages.items():
        assert classify_page_section(body) == expected[num], f"page {num}"


def test_integrity_heal_stale_state() -> None:
    from pdf_transcribe_integrity import heal_stale_state

    wd = Path(tempfile.mkdtemp()) / "work"
    wd.mkdir()
    (wd / "state.json").write_text(
        '{"source_name": "anales_de_tlatelolco", "source_id": "ixtlilxochitl"}',
        encoding="utf-8",
    )
    report = heal_stale_state(wd, "anales_de_tlatelolco")
    assert report.fixes
    assert not (wd / "state.json").is_file()


def test_impossible_strings_source_header() -> None:
    from pdf_transcribe_integrity import (
        read_impossible_strings_file,
        write_impossible_strings_file,
    )

    slug = "test_source"
    path = write_impossible_strings_file(slug, ["badtypo", "otherbad"])
    assert path.is_file()
    loaded = read_impossible_strings_file(path, slug)
    assert loaded == ["badtypo", "otherbad"]
    assert read_impossible_strings_file(path, "other_source") is None


def test_resolve_source_slug_hint() -> None:
    from pdf_transcribe_integrity import resolve_source_slug_hint

    assert resolve_source_slug_hint("anales_de_tlatelolco") == "anales_de_tlatelolco"
    assert resolve_source_slug_hint("anales_de_tlatelolco_copyright") == "anales_de_tlatelolco"


def test_spot_patch_rejection_check() -> None:
    from pdf_transcribe_integrity import spot_patch_rejection_check_ok

    wd = Path(tempfile.mkdtemp())
    log = wd / "spot_patch_log.txt"
    log.write_text(
        "Page 2\n"
        "  op 0: REJECTED (unchanged) (body) [Tlatelolco]\n"
        "  op 1: REJECTED (unchanged) (body) [Tenochtitlan]\n"
        "  op 2: APPLIED (body) [náhuatl]\n",
        encoding="utf-8",
    )
    stats = {"spot_patches_applied": 1, "spot_patches_rejected": 2}
    ok, detail = spot_patch_rejection_check_ok(wd, stats)
    assert ok
    assert "clean transcription" in detail

    log.write_text(
        "Page 3\n"
        "  op 0: REJECTED (impossible_string) (body) [foo]\n"
        "  op 1: REJECTED (impossible_string) (body) [bar]\n"
        "  op 2: REJECTED (unchanged) (body) [baz]\n",
        encoding="utf-8",
    )
    stats2 = {"spot_patches_applied": 0, "spot_patches_rejected": 3}
    ok2, detail2 = spot_patch_rejection_check_ok(wd, stats2)
    assert not ok2
    assert "blocked" in detail2


def test_source_lock_blocks_mismatch() -> None:
    from pdf_transcribe_integrity import run_startup_integrity, write_source_lock

    wd = Path(tempfile.mkdtemp()) / "work"
    wd.mkdir()
    write_source_lock(wd, "ixtlilxochitl")
    report, _ = run_startup_integrity(wd, "anales_de_tlatelolco")
    assert report.blocking


def test_kaqchikel_normalization_threshold() -> None:
    langs = {"kaqchikel": 0.4, "spanish": 0.6}
    text = build_normalization_rules_text(langs)
    assert "Kaqchikel" in text or "Maya" in text
    assert "fué" in text or "Spanish" in text


def test_merge_hard_terms() -> None:
    merged = merge_hard_terms(["Hunyg", "Atitlan"], ["Atitlan", "Tz'utujil"])
    assert merged == ["Hunyg", "Atitlan", "Tz'utujil"]


def test_slugify_source_name() -> None:
    assert slugify_source_name("Kaqchikel Chronicles") == "kaqchikel_chronicles"


def test_detection_sample_spread() -> None:
    """Samples must cover first half pairs and multiple book regions (not pure random)."""
    pages = list(range(1, 201))
    samples = pick_detection_sample_pages(pages)
    assert len(samples) >= 5
    assert min(samples) <= 40
    assert max(samples) >= 150
    # At least one consecutive pair from the first half
    first_half = set(range(1, 101))
    has_pair = any(p in first_half and (p + 1) in samples for p in samples)
    assert has_pair, f"expected consecutive pair in first half, got {samples}"


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
        test_script_mismatch_latin,
        test_too_short_on_content_page,
        test_apply_patch_preserves_rest,
        test_parse_detection_response,
        test_latin_script_forces_ltr,
        test_source_identity_mismatch_raises,
        test_impossible_strings_per_source,
        test_patch_cap,
        test_classify_page_section,
        test_classify_anales_pilot_page_pattern,
        test_resolve_source_slug_hint,
        test_spot_patch_rejection_check,
        test_integrity_heal_stale_state,
        test_impossible_strings_source_header,
        test_source_lock_blocks_mismatch,
        test_kaqchikel_normalization_threshold,
        test_merge_hard_terms,
        test_slugify_source_name,
        test_detection_sample_spread,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
