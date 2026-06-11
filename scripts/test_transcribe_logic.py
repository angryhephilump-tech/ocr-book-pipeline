#!/usr/bin/env python3
"""Quick logic tests (no API calls). Run: python scripts/test_transcribe_logic.py"""

from __future__ import annotations

import json
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
from pdf_transcribe_assembly import (  # noqa: E402
    apply_cross_page_bracket_rejoins,
    has_rejoinable_bracket_split,
    page_needs_bracket_boundary_review,
    rejoin_bracket_fragment_pair,
)
from pdf_transcribe_lang import (  # noqa: E402
    DocumentTermIndex,
    build_normalization_rules_text,
    filter_terms_by_min_occurrences,
    job_language_config,
    job_language_config_from_state,
    load_impossible_strings,
    canonicalize_superscript_notation,
    normalize_notation_tier1,
    pages_need_content_reconcile,
    resolve_unify_abbreviation_marks,
    unify_macron_to_tilde,
    strip_whitespace_for_compare,
    term_present_in_text,
    unify_abbreviation_marks_tier2,
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
from pdf_transcribe_finalize import (  # noqa: E402
    Pass4WorkItem,
    _finalize_reconcile_page,
    _resolve_pass4_page,
    reconcile_needs_pass4,
    resolve_pass4_outcome,
)
from pdf_transcribe_spot import (  # noqa: E402
    REJECT_IMPOSSIBLE_SIC,
    REJECT_MISSING_HARD_TERM,
    PatchOperation,
    apply_all_patches,
    bracket_terms_in_sentence,
    build_page_patch_prompt,
    cap_patch_operations,
    collect_patch_operations,
    estimate_spot_output_tokens,
    page_spot_output_max_tokens,
    parse_page_patch_response,
    sentence_contains_hard_terms,
    split_patch_operations_by_token_budget,
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


def test_unify_abbreviation_marks_default_false() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    assert cfg.unify_abbreviation_marks is False


def test_historia_spanish_only_unify_on() -> None:
    state = {
        "source_name": "historiaverdade03castgoog_part_2",
        "source_id": "historiaverdade03castgoog_part_2",
        "language": "spanish",
        "script": "latin",
        "detected_source_profile": {
            "languages": {"spanish": 1.0},
            "confirmed": True,
        },
    }
    cfg = job_language_config_from_state(state)
    assert cfg.unify_abbreviation_marks is True


def test_anales_mixed_forces_unify_off() -> None:
    state = {
        "source_name": "anales_de_tlatelolco",
        "source_id": "anales_de_tlatelolco",
        "language": "spanish",
        "script": "latin",
        "detected_source_profile": {
            "languages": {"spanish": 0.7, "nahuatl": 0.3},
            "unify_abbreviation_marks": True,
            "confirmed": True,
        },
        "unify_abbreviation_marks": True,
    }
    assert resolve_unify_abbreviation_marks(state, state["detected_source_profile"]) is False
    cfg = job_language_config_from_state(state)
    assert cfg.unify_abbreviation_marks is False
    assert cfg.language == "spanish"


def test_notation_tier1_html_and_caret() -> None:
    assert normalize_notation_tier1("ver nota<sup>1</sup> aquí") == "ver nota¹ aquí"
    assert normalize_notation_tier1("capítulo N^o 5") == "capítulo Nº 5"
    assert normalize_notation_tier1("ref^12 al pie") == "ref¹² al pie"


def test_canonicalize_superscript_ordinal_after_letter() -> None:
    assert canonicalize_superscript_notation("rresçebimiº") == "rresçebimiᵒ"
    assert canonicalize_superscript_notation("rresçebimiᵒ") == "rresçebimiᵒ"
    assert normalize_notation_tier1("rresçebimiº") == "rresçebimiᵒ"
    assert pt.normalize_transcription_output("rresçebimiº") == "rresçebimiᵒ"
    assert canonicalize_superscript_notation("capítulo Nº 5") == "capítulo Nº 5"
    a = "rresçebimiº en el texto."
    b = "rresçebimiᵒ en el texto."
    assert not pages_need_content_reconcile(a, b)


def test_macron_to_tilde_when_flag_on() -> None:
    cfg = job_language_config(
        "spanish", "historiaverdade03castgoog_part_2", unify_abbreviation_marks=True
    )
    q_macron = "q\u0304"
    q_tilde = "q\u0303"
    assert unify_macron_to_tilde(f"por{q_macron}") == f"por{q_tilde}"
    assert pt.normalize_transcription_output(f"palabra {q_macron} fin", cfg) == (
        f"palabra {q_tilde} fin"
    )
    assert pt.normalize_transcription_output("vocālis", cfg) == "vocãlis"
    assert pt.normalize_transcription_output("ēcho", cfg) == "ẽcho"


def test_macron_to_tilde_when_flag_off() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl", unify_abbreviation_marks=False)
    q_macron = "q\u0304"
    raw = f"palabra {q_macron} y vocālis"
    assert pt.normalize_transcription_output(raw, cfg) == raw


def test_nahuatl_macrons_protected_by_override() -> None:
    state = {
        "source_name": "anales_de_tlatelolco",
        "source_id": "anales_de_tlatelolco",
        "language": "spanish",
        "script": "latin",
        "detected_source_profile": {
            "languages": {"spanish": 0.7, "nahuatl": 0.3},
            "unify_abbreviation_marks": True,
            "confirmed": True,
        },
        "unify_abbreviation_marks": True,
    }
    cfg = job_language_config_from_state(state)
    assert cfg.unify_abbreviation_marks is False
    raw = "Tenōchtitlan Āltepētl q\u0304"
    assert pt.normalize_transcription_output(raw, cfg) == raw


def test_macron_tilde_superscript_digits_untouched() -> None:
    cfg = job_language_config("spanish", "test", unify_abbreviation_marks=True)
    raw = "mx⁰⁰ y más"
    assert pt.normalize_transcription_output(raw, cfg) == raw


def test_macron_tilde_no_reconcile_when_unify_on() -> None:
    cfg = job_language_config("spanish", "test", unify_abbreviation_marks=True)
    q_macron = "q\u0304"
    q_tilde = "q\u0303"
    a = f"por{q_macron} el camino."
    b = f"por{q_tilde} el camino."
    assert pt.pages_disagree(a, b)
    assert not pages_need_content_reconcile(a, b, cfg)


def test_tier2_section_gating_spanish_vs_paleographic() -> None:
    from pdf_transcribe_lang import (
        section_skips_tier2_unification,
        should_apply_tier2_unification,
    )

    cfg = job_language_config(
        "spanish", "historiaverdade03castgoog_part_2", unify_abbreviation_marks=True
    )
    q_macron = "q\u0304"
    q_tilde = "q\u0303"

    assert not section_skips_tier2_unification("spanish_translation")
    assert section_skips_tier2_unification("paleographic_nahuatl")
    assert should_apply_tier2_unification(cfg, section="spanish_translation")
    assert not should_apply_tier2_unification(cfg, section="paleographic_nahuatl")

    assert pt.normalize_transcription_output(
        f"por{q_macron}", cfg, section="spanish_translation"
    ) == f"por{q_tilde}"
    raw = f"por{q_macron} [Nican moteneu] Ācatl"
    assert pt.normalize_transcription_output(raw, cfg, section="paleographic_nahuatl") == raw
    assert (
        pt.normalize_transcription_output("rresçebimiº", cfg, section="paleographic_nahuatl")
        == "rresçebimiᵒ"
    )

    a = f"por{q_macron} el camino."
    b = f"por{q_tilde} el camino."
    assert not pages_need_content_reconcile(a, b, cfg, section="spanish_translation")


def test_notation_tier2_unify_spanish_abbreviations() -> None:
    cfg = job_language_config("spanish", "test", unify_abbreviation_marks=True)
    raw = "El N.o 3 y la M.a reina."
    out = pt.normalize_transcription_output(raw, cfg)
    assert "\u00ba" in out  # Nº masculine ordinal
    assert "\u00aa" in out  # Mª feminine ordinal


def test_notation_only_no_reconcile() -> None:
    a = "Texto con N^o 5 y nota<sup>1</sup> al final."
    b = "Texto con Nº 5 y nota¹ al final."
    cfg = job_language_config("spanish", "historiaverdade03castgoog_part_2", unify_abbreviation_marks=True)
    assert pt.pages_disagree(a, b)
    assert not pages_need_content_reconcile(a, b, cfg)
    c = "Texto con Nº 6 y nota¹ al final."
    assert pages_need_content_reconcile(a, c, cfg)


def test_normalize_transcription_output_tier1_without_lang_cfg() -> None:
    out = pt.normalize_transcription_output("ver <sup>2</sup> aquí")
    assert out == "ver ² aquí"


def test_reconcile_pass4_third_reading() -> None:
    run1 = "Linea uno.\nLinea dos."
    run2 = "Linea 1.\nLinea 2."
    reconcile = "Linea primera.\nLinea segunda."
    assert reconcile_needs_pass4(reconcile, run1, run2)
    assert not reconcile_needs_pass4(run1, run1, run2)
    assert not reconcile_needs_pass4("Linea  uno.\nLinea dos.", run1, run2)


def test_document_term_index_cache() -> None:
    doc = "Rarelongword appears here. Rarelongword again."
    index = DocumentTermIndex(doc)
    assert index.count("Rarelongword") == 2
    assert index.count("Rarelongword") == 2
    filtered = filter_terms_by_min_occurrences(["Rarelongword"], doc, min_count=2, term_index=index)
    assert filtered == ["Rarelongword"]


def test_pass4_batch_resolve_missing_result() -> None:
    item = Pass4WorkItem(
        page_num=7,
        reconcile_body="gamma",
        run1_text="alpha",
        run2_text="beta",
        image_path=Path("page.png"),
        uncertain=False,
        uncertain_note=None,
    )
    final, extra, review = _resolve_pass4_page(
        item, "[Pass 4 failed: missing batch result]"
    )
    assert final == "alpha"
    assert review
    assert extra["pass4_fired"]
    assert "missing batch result" in extra["pass4_reading"]


def test_pass4_batch_resolve_matches_reconcile() -> None:
    item = Pass4WorkItem(
        page_num=3,
        reconcile_body="gamma",
        run1_text="alpha",
        run2_text="beta",
        image_path=Path("page.png"),
        uncertain=True,
        uncertain_note="faded",
    )
    final, extra, review = _resolve_pass4_page(item, "gamma")
    assert final == "gamma"
    assert not review
    assert "reconcile" in extra["pass4_outcome"]


def test_finalize_reconcile_without_pass4() -> None:
    wd = Path(tempfile.mkdtemp())
    state: dict = {"reconcile": {"completed": []}}
    entry, review = _finalize_reconcile_page(
        wd,
        5,
        "final text",
        state=state,
        resolution="batch reconcile from image",
        uncertain=False,
        uncertain_note=None,
    )
    assert entry["page"] == 5
    assert not review
    assert 5 in state["reconcile"]["completed"]
    assert (wd / "reconcile" / "pages" / "page_0005.txt").read_text(
        encoding="utf-8"
    ) == "final text"


def test_resolve_pass4_outcome() -> None:
    run1 = "alpha"
    run2 = "beta"
    reconcile = "gamma"
    final, outcome, review = resolve_pass4_outcome(run1, run2, reconcile, run1)
    assert final == run1
    assert "run1" in outcome
    assert not review
    final, outcome, review = resolve_pass4_outcome(run1, run2, reconcile, run2)
    assert final == run2
    assert not review
    final, outcome, review = resolve_pass4_outcome(run1, run2, reconcile, reconcile)
    assert final == reconcile
    assert not review
    final, outcome, review = resolve_pass4_outcome(run1, run2, reconcile, "delta")
    assert final == run1
    assert review
    assert "fourth unique" in outcome


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
    assert pt.parse_batch_custom_id("spotpg_p0042") == ("spotpg", 0, 42)
    assert pt.parse_batch_custom_id("p4_p0010") == ("pass4", 0, 10)


def test_build_page_patch_prompt() -> None:
    cfg = job_language_config("spanish", "ixtlilxochitl")
    ops = [
        PatchOperation(
            section="body",
            start=0,
            end=30,
            sentence="Nezahualcoyotl reinó en Texcoco.",
            terms=("Nezahualcoyotl", "Texcoco"),
            bracketed_sentence="[Nezahualcoyotl] reinó en [Texcoco].",
            op_index=0,
        ),
        PatchOperation(
            section="body",
            start=31,
            end=55,
            sentence="Moquíhuix en Tenochtitlan.",
            terms=("Moquíhuix",),
            bracketed_sentence="[Moquíhuix] en Tenochtitlan.",
            op_index=1,
        ),
    ]
    prompt = build_page_patch_prompt(ops, cfg, section_hint="spanish_translation")
    assert "1. [Nezahualcoyotl] reinó en [Texcoco]." in prompt
    assert "2. [Moquíhuix] en Tenochtitlan." in prompt
    assert "Nezahualcoyotl, Texcoco" in prompt
    assert "Moquíhuix" in prompt
    assert "EXACTLY 2 numbered sentence" in prompt
    assert "one single line" in prompt
    assert "Reproduce each sentence in full" in prompt
    assert "Spanish narrative section" in prompt
    assert "not paleographic Nahuatl" in prompt


def test_parse_page_patch_response() -> None:
    originals = ["Alpha uno.", "Beta dos."]
    clean = "1: Alpha UNO.\n2: Beta DOS."
    out = parse_page_patch_response(clean, 2, originals)
    assert out == ["Alpha UNO.", "Beta DOS."]

    shuffled = "2) Beta DOS.\n1) Alpha UNO."
    out2 = parse_page_patch_response(shuffled, 2, originals)
    assert out2 == ["Alpha UNO.", "Beta DOS."]

    missing = "1: Alpha UNO."
    out3 = parse_page_patch_response(missing, 2, originals)
    assert out3[0] == "Alpha UNO."
    assert out3[1] == originals[1]

    garbage = "I cannot verify these sentences."
    out4 = parse_page_patch_response(garbage, 2, originals)
    assert out4 == originals

    wrapped = (
        "1: Esta es una oración colonial muy larga que el modelo\n"
        "decidió envolver en varias líneas sin puntuación final\n"
        "2: Segunda oración corta."
    )
    out5 = parse_page_patch_response(wrapped, 2, originals)
    assert out5[0] == (
        "Esta es una oración colonial muy larga que el modelo "
        "decidió envolver en varias líneas sin puntuación final"
    )
    assert out5[1] == "Segunda oración corta."

    long_sentence = "A" * 9000
    long_originals = [long_sentence, "tail."]
    truncated = f"1: {long_sentence[:1200]}"
    out6 = parse_page_patch_response(truncated, 2, long_originals)
    assert out6[0] == long_sentence[:1200]
    assert out6[1] == long_originals[1]

    dotted = "1. Alpha UNO.\n2. Beta DOS."
    out7 = parse_page_patch_response(dotted, 2, originals)
    assert out7 == ["Alpha UNO.", "Beta DOS."]


def test_spot_token_budget_helpers() -> None:
    short_ops = [
        PatchOperation(
            section="body",
            start=0,
            end=10,
            sentence="Corto.",
            terms=("Corto",),
            bracketed_sentence="[Corto].",
            op_index=0,
        )
    ]
    assert estimate_spot_output_tokens(["Corto."]) == len("Corto.") // 3 + 512
    assert page_spot_output_max_tokens(short_ops) >= 256
    assert split_patch_operations_by_token_budget(short_ops) == [short_ops]

    long_ops = [
        PatchOperation(
            section="body",
            start=0,
            end=5000,
            sentence="X" * 5000,
            terms=("X",),
            bracketed_sentence="[X]" + "X" * 4999,
            op_index=0,
        ),
        PatchOperation(
            section="body",
            start=5000,
            end=10000,
            sentence="Y" * 5000,
            terms=("Y",),
            bracketed_sentence="[Y]" + "Y" * 4999,
            op_index=1,
        ),
    ]
    chunks = split_patch_operations_by_token_budget(long_ops, max_output_tokens=3000)
    assert len(chunks) >= 2
    assert sum(len(c) for c in chunks) == 2
    assert pt.spot_page_custom_id(42, 1) == "spotpg_p0042_01"
    assert pt.parse_batch_custom_id("spotpg_p0042_01") == ("spotpg", 1, 42)


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


def test_flex_match_span_no_regex_hang() -> None:
    from pdf_transcribe_spot import _flex_match_span, _map_logical_span_to_original

    original = "El rey\nNezahualcoyotl   gobernó Texcoco."
    logical = "El rey Nezahualcoyotl gobernó Texcoco."
    span = _flex_match_span(original, logical)
    assert span == (0, len(original))
    mapped = _map_logical_span_to_original(original, logical, 0, len(logical))
    assert mapped == span

    padding = "ab " * 8000
    original_long = padding + "foo bar baz end"
    span = _flex_match_span(original_long, "foo bar baz end")
    assert span is not None
    assert original_long[span[0] : span[1]] == "foo bar baz end"

    de_padding = "de " * 4000
    original_de = de_padding + "rey Nezahualcoyotl gobernó Texcoco."
    span = _flex_match_span(original_de, "rey Nezahualcoyotl gobernó Texcoco.")
    assert span is not None
    assert "Nezahualcoyotl" in original_de[span[0] : span[1]]


def test_extract_json_blob_no_regex_hang() -> None:
    from pdf_transcribe_detect import _extract_json_blob

    assert _extract_json_blob('{"a": 1}') == {"a": 1}
    assert _extract_json_blob('noise {"b": 2} trailing') == {"b": 2}
    assert _extract_json_blob("prefix [1, 2, 3] suffix") == [1, 2, 3]
    malformed = "{" + "a" * 5000
    try:
        _extract_json_blob(malformed)
        assert False, "expected JSONDecodeError"
    except json.JSONDecodeError:
        pass


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
    assert "unoaconejaron" not in [s.lower() for s in imp]


def test_impossible_string_rejects_sic_patch() -> None:
    cfg = job_language_config("spanish", "anales_de_tlatelolco")
    original = "Ellos unoaconejaron al rey."
    patched = "Ellos unoaconejaron[sic] al rey."
    chosen, reason = validate_patch_response(
        original,
        patched,
        cfg,
        ["unoaconejaron"],
        expected_terms=("unoaconejaron",),
    )
    assert chosen == original
    assert reason == REJECT_IMPOSSIBLE_SIC


def test_bracket_match_back_fuzzy_nahuatl() -> None:
    cfg = job_language_config("nahuatl", "anales_de_tlatelolco")
    sentence = "Reinó Quauhtlatoatzin en la ciudad."
    assert sentence_contains_hard_terms(
        sentence,
        ("Cuauhtlatoatzin",),
        cfg,
        section_hint="paleographic_nahuatl",
    )


def test_bracket_strip_match_back() -> None:
    cfg = job_language_config("spanish", "anales_de_tlatelolco")
    sentence = "Gobernó [Ahuitzotzin] muchos años."
    assert term_present_in_text("Ahuitzotzin", sentence, cfg)


def test_single_occurrence_auto_term_filtered() -> None:
    cfg = job_language_config("nahuatl", "anales_de_tlatelolco")
    doc = "Una sola Rarelongword aquí y nada más."
    filtered = filter_terms_by_min_occurrences(["Rarelongword"], doc, min_count=2)
    assert filtered == []
    doc2 = doc + "\nOtra Rarelongword aparece."
    filtered2 = filter_terms_by_min_occurrences(["Rarelongword"], doc2, min_count=2)
    assert filtered2 == ["Rarelongword"]


def test_bracket_rejoin_across_pages() -> None:
    p11 = "Texto previo [de edifi-"
    p13 = "car] continúa aquí."
    assert has_rejoinable_bracket_split(p11, p13)
    merged = rejoin_bracket_fragment_pair(p11, p13)
    assert merged is not None
    end, start = merged
    assert end.endswith("[de edificar]")
    assert "contin" in start
    bodies = apply_cross_page_bracket_rejoins({11: p11, 13: p13}, [11, 13])
    assert bodies[11].endswith("[de edificar]")
    assert "car]" not in bodies[13]
    assert "aqu" in bodies[13]


def test_cross_page_bracket_split_not_flagged() -> None:
    p11 = "Algo [de edifi-"
    p13 = "car] más texto"
    assert not page_needs_bracket_boundary_review(p13, prev_body=p11)
    assert not page_needs_bracket_boundary_review(p11, next_body=p13)


def test_true_orphan_bracket_flagged() -> None:
    assert page_needs_bracket_boundary_review("car] sin apertura")
    assert page_needs_bracket_boundary_review("termina con [abierto")


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


def test_classify_archaic_spanish_not_paleographic() -> None:
    """Regression: colonial Spanish (ç, q̄) must not be tagged paleographic_nahuatl."""
    q_macron = "q\u0304"
    archaic = (
        f"COMO abian venido alli a tezcuco por{q_macron} el señor de la çiudad "
        "çerto çaminos y açordaron que los mexicas avian de yr a la guerra."
    )
    assert classify_page_section(archaic) == "spanish_translation"


def test_write_progress_retries_windows_file_lock() -> None:
    import os

    wd = Path(tempfile.mkdtemp()) / "work"
    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(5, "Access is denied")
        real_replace(src, dst)

    old_replace = os.replace
    os.replace = flaky_replace
    try:
        pt.write_progress(
            wd,
            phase="transcribe",
            current_run=1,
            page=3,
            total_pages=10,
            message="page 3",
        )
        prog = pt.load_progress(wd)
        assert prog is not None
        assert prog["phase"] == "transcribe"
        assert prog["page"] == 3
        assert calls["n"] == 3
    finally:
        os.replace = old_replace


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


def test_spot_patch_rejection_check_current_run_pages_only() -> None:
    from pdf_transcribe_integrity import spot_patch_rejection_check_ok

    wd = Path(tempfile.mkdtemp())
    log = wd / "spot_patch_log.txt"
    log.write_text(
        "Page 13\n"
        "  op 0: REJECTED (unchanged) (body) [a]\n"
        "  op 1: REJECTED (unchanged) (body) [b]\n"
        "Page 24\n"
        "  op 0: REJECTED (unchanged) (body) [c]\n"
        "  op 1: REJECTED (impossible_string) (body) [d]\n",
        encoding="utf-8",
    )
    stats = {
        "spot_patches_applied": 2,
        "spot_patches_rejected": 3,
        "page_numbers": list(range(23, 33)),
    }
    ok, detail = spot_patch_rejection_check_ok(wd, stats)
    assert ok
    assert "other: -" not in detail
    assert "unchanged (already correct): 1" in detail
    assert "impossible_string (blocked): 1" in detail


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


def test_job_lock_acquire_release() -> None:
    from pdf_transcribe_job_lock import (
        JobLockError,
        acquire_job_lock,
        job_lock_path,
        release_job_lock,
    )

    wd = Path(tempfile.mkdtemp())
    acquire_job_lock(wd)
    assert job_lock_path(wd).is_file()
    try:
        acquire_job_lock(wd)
        raise AssertionError("expected JobLockError for contended lock")
    except JobLockError as exc:
        assert "locked" in str(exc).lower()
    release_job_lock(wd)
    assert not job_lock_path(wd).is_file()
    acquire_job_lock(wd, force=True)


def test_work_dir_has_completed_work() -> None:
    from pdf_transcribe_integrity import work_dir_has_completed_work

    assert not work_dir_has_completed_work({})
    assert work_dir_has_completed_work({"runs": {"1": {"completed": [3]}}})
    assert work_dir_has_completed_work({"reconcile": {"completed": [1]}})


def test_backup_zip_excludes_images() -> None:
    import zipfile

    from pdf_transcribe_integrity import backup_work_dir_before_reset

    wd = Path(tempfile.mkdtemp())
    (wd / "state.json").write_text('{"runs": {"1": {"completed": [1]}}}', encoding="utf-8")
    (wd / "run1" / "pages").mkdir(parents=True)
    (wd / "run1" / "pages" / "page_0001.txt").write_text("hello", encoding="utf-8")
    img_dir = wd / "images"
    img_dir.mkdir()
    (img_dir / "page_0001.png").write_bytes(b"\x89PNG")

    zip_path = backup_work_dir_before_reset(wd)
    assert zip_path is not None
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "state.json" in names
    assert any(n.startswith("run1/") for n in names)
    assert not any(n.startswith("images/") for n in names)


def test_term_tuning_report_stats() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from term_tuning_report import demotion_candidates, parse_spot_patch_term_stats

    log = (
        "Page 1\n"
        "  op 0: APPLIED (body) [Tenochtitlan]\n"
        "  op 1: REJECTED (unchanged) (body) [Tlatelolco]\n"
        "  op 2: REJECTED (unchanged) (body) [Tlatelolco]\n"
        "  op 3: REJECTED (impossible_string) (body) [badtypo] — HUMAN REVIEW\n"
        "Page 2\n"
        "  op 0: REJECTED (unchanged) (body) [Tlatelolco, Tenochtitlan]\n"
    )
    stats = parse_spot_patch_term_stats(log)
    assert stats["tenochtitlan"].checked == 2
    assert stats["tenochtitlan"].applied == 1
    assert stats["tlatelolco"].checked == 3
    assert stats["tlatelolco"].applied == 0
    assert stats["tlatelolco"].rejected_unchanged == 3
    assert stats["badtypo"].rejected_review == 1
    candidates = demotion_candidates(stats, min_checks=3)
    assert "tlatelolco" in candidates
    assert "tenochtitlan" not in candidates


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
        test_unify_abbreviation_marks_default_false,
        test_historia_spanish_only_unify_on,
        test_anales_mixed_forces_unify_off,
        test_notation_tier1_html_and_caret,
        test_canonicalize_superscript_ordinal_after_letter,
        test_macron_to_tilde_when_flag_on,
        test_macron_to_tilde_when_flag_off,
        test_nahuatl_macrons_protected_by_override,
        test_macron_tilde_superscript_digits_untouched,
        test_macron_tilde_no_reconcile_when_unify_on,
        test_tier2_section_gating_spanish_vs_paleographic,
        test_notation_tier2_unify_spanish_abbreviations,
        test_notation_only_no_reconcile,
        test_normalize_transcription_output_tier1_without_lang_cfg,
        test_reconcile_pass4_third_reading,
        test_document_term_index_cache,
        test_pass4_batch_resolve_missing_result,
        test_pass4_batch_resolve_matches_reconcile,
        test_finalize_reconcile_without_pass4,
        test_resolve_pass4_outcome,
        test_hard_terms,
        test_parse_reconcile_uncertain,
        test_parse_batch_ids,
        test_build_page_patch_prompt,
        test_parse_page_patch_response,
        test_spot_token_budget_helpers,
        test_work_page_range,
        test_chatter_to_skip,
        test_bracket_terms,
        test_collect_patch_ops,
        test_flex_match_span_no_regex_hang,
        test_extract_json_blob_no_regex_hang,
        test_impossible_string_rejects_patch,
        test_missing_hard_term_rejects_patch,
        test_script_mismatch_latin,
        test_too_short_on_content_page,
        test_apply_patch_preserves_rest,
        test_parse_detection_response,
        test_latin_script_forces_ltr,
        test_source_identity_mismatch_raises,
        test_impossible_strings_per_source,
        test_impossible_string_rejects_sic_patch,
        test_bracket_match_back_fuzzy_nahuatl,
        test_bracket_strip_match_back,
        test_single_occurrence_auto_term_filtered,
        test_bracket_rejoin_across_pages,
        test_cross_page_bracket_split_not_flagged,
        test_true_orphan_bracket_flagged,
        test_patch_cap,
        test_classify_page_section,
        test_classify_anales_pilot_page_pattern,
        test_classify_archaic_spanish_not_paleographic,
        test_write_progress_retries_windows_file_lock,
        test_resolve_source_slug_hint,
        test_spot_patch_rejection_check,
        test_spot_patch_rejection_check_current_run_pages_only,
        test_integrity_heal_stale_state,
        test_impossible_strings_source_header,
        test_source_lock_blocks_mismatch,
        test_kaqchikel_normalization_threshold,
        test_merge_hard_terms,
        test_slugify_source_name,
        test_job_lock_acquire_release,
        test_work_dir_has_completed_work,
        test_backup_zip_excludes_images,
        test_term_tuning_report_stats,
        test_detection_sample_spread,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
