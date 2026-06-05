"""Post-batch content sanity checks and live re-runs for bad pages."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from pdf_transcribe_lang import VALID_SCRIPTS, normalize_script

# Re-export for tests
__all__ = [
    "VALID_SCRIPTS",
    "SanityFailure",
    "check_page_sanity",
    "image_likely_blank",
    "run_batch_content_sanity_pass",
    "script_mismatch_detail",
]

MIN_CONTENT_CHARS = 50
SCRIPT_MISMATCH_THRESHOLD = 0.20
MIN_LETTERS_FOR_SCRIPT_CHECK = 8


@dataclass(frozen=True)
class SanityFailure:
    page_num: int
    run: int
    reason: str
    bad_output: str
    detail: str


def _letter_chars(text: str) -> list[str]:
    return [c for c in text if unicodedata.category(c).startswith("L")]


def _is_latin_char(c: str) -> bool:
    o = ord(c)
    if o <= 0x024F:
        return True
    if 0x1E00 <= o <= 0x1EFF:
        return True
    if 0x2C60 <= o <= 0x2C7F:
        return True
    if 0xA720 <= o <= 0xA7FF:
        return True
    return False


def _is_arabic_char(c: str) -> bool:
    o = ord(c)
    return (
        0x0600 <= o <= 0x06FF
        or 0x0750 <= o <= 0x077F
        or 0x08A0 <= o <= 0x08FF
        or 0xFB50 <= o <= 0xFDFF
        or 0xFE70 <= o <= 0xFEFF
    )


def _is_hangul_char(c: str) -> bool:
    o = ord(c)
    return 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F


def _is_kana_char(c: str) -> bool:
    o = ord(c)
    return 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF


def _is_cjk_char(c: str) -> bool:
    o = ord(c)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x20000 <= o <= 0x2A6DF
        or 0xF900 <= o <= 0xFAFF
    )


def char_matches_script(c: str, script: str) -> bool:
    if script == "latin":
        return _is_latin_char(c)
    if script == "arabic":
        return _is_arabic_char(c)
    if script == "korean":
        return _is_hangul_char(c) or _is_cjk_char(c)
    if script == "japanese":
        return _is_kana_char(c) or _is_cjk_char(c) or _is_hangul_char(c)
    if script == "chinese":
        return _is_cjk_char(c)
    return True


def script_mismatch_detail(text: str, script: str) -> str | None:
    letters = _letter_chars(text)
    if len(letters) < MIN_LETTERS_FOR_SCRIPT_CHECK:
        return None
    matched = sum(1 for c in letters if char_matches_script(c, script))
    ratio = matched / len(letters)
    if script == "latin":
        if ratio < (1.0 - SCRIPT_MISMATCH_THRESHOLD):
            non = len(letters) - matched
            return f"{non}/{len(letters)} letters non-latin ({100 - int(ratio * 100)}% non-latin)"
        return None
    if ratio < SCRIPT_MISMATCH_THRESHOLD:
        return f"{matched}/{len(letters)} letters in expected script ({int(ratio * 100)}% {script})"
    return None


def image_likely_blank(image_path: Path) -> bool:
    try:
        with Image.open(image_path) as img:
            gray = img.convert("L")
            w, h = gray.size
            if w > 320 or h > 320:
                gray = gray.resize((max(1, w * 320 // max(w, h)), max(1, h * 320 // max(w, h))))
            pixels = list(gray.getdata())
        if not pixels:
            return True
        mean = sum(pixels) / len(pixels)
        var = sum((p - mean) ** 2 for p in pixels) / len(pixels)
        if mean > 242 and var < 120:
            return True
        if mean < 12 and var < 80:
            return True
    except OSError:
        return False
    return False


def check_page_sanity(
    text: str,
    *,
    script: str,
    image_path: Path,
    is_skip_fn: Callable[[str], bool],
) -> list[tuple[str, str]]:
    """Return list of (reason, detail) failures. Empty if OK."""
    body = (text or "").strip()
    if not body or is_skip_fn(body):
        return []

    failures: list[tuple[str, str]] = []
    mismatch = script_mismatch_detail(body, script)
    if mismatch:
        failures.append(("script_mismatch", mismatch))

    if len(re.sub(r"\s+", "", body)) < MIN_CONTENT_CHARS and not image_likely_blank(image_path):
        failures.append(("too_short", f"{len(body)} chars on non-blank image"))

    return failures


def run_batch_content_sanity_pass(
    api_key: str,
    work_dir: Path,
    state: dict,
    page_numbers: list[int],
    image_by_page: dict[int, Path],
    *,
    script: str,
    skip_front_pages: int,
    is_skip_fn: Callable[[str], bool],
    read_page_fn: Callable[[Path, int, int], str],
    write_page_fn: Callable[[Path, dict, int, int, str], None],
    transcribe_fn: Callable[..., str],
    report: Callable | None = None,
    api_delay_sec: float = 0.5,
) -> list[dict]:
    """Scan batch outputs; re-run bad pages on live API. Return batch_collisions log."""
    from pdf_transcribe import NUM_TRANSCRIPTION_RUNS

    expected_script = normalize_script(script, state.get("language"))
    collisions: list[dict] = []
    failures: list[SanityFailure] = []

    for run in range(1, NUM_TRANSCRIPTION_RUNS + 1):
        for page_num in page_numbers:
            text = read_page_fn(work_dir, run, page_num)
            image_path = image_by_page.get(page_num)
            if not image_path or not image_path.is_file():
                continue
            for reason, detail in check_page_sanity(
                text,
                script=expected_script,
                image_path=image_path,
                is_skip_fn=is_skip_fn,
            ):
                failures.append(
                    SanityFailure(
                        page_num=page_num,
                        run=run,
                        reason=reason,
                        bad_output=text[:500],
                        detail=detail,
                    )
                )

    if not failures:
        return []

    if report:
        report(
            "sanity",
            0,
            0,
            len(page_numbers),
            None,
            f"Re-running {len(failures)} batch collision(s) on live API…",
        )

    merged: dict[tuple[int, int], SanityFailure] = {}
    for fail in failures:
        key = (fail.run, fail.page_num)
        if key not in merged:
            merged[key] = fail
            continue
        prev = merged[key]
        merged[key] = SanityFailure(
            page_num=fail.page_num,
            run=fail.run,
            reason=f"{prev.reason}+{fail.reason}",
            bad_output=prev.bad_output,
            detail=f"{prev.detail}; {fail.detail}",
        )

    for fail in merged.values():
        image_path = image_by_page.get(fail.page_num)
        if not image_path:
            continue
        try:
            new_text = transcribe_fn(
                api_key,
                image_path,
                page_num=fail.page_num,
                skip_front_pages=skip_front_pages,
            )
        except Exception as exc:
            new_text = fail.bad_output
            detail = f"{fail.detail}; rerun failed: {exc}"
        else:
            detail = fail.detail

        write_page_fn(work_dir, state, fail.run, fail.page_num, new_text)
        collisions.append(
            {
                "page": fail.page_num,
                "run": fail.run,
                "reason": fail.reason,
                "bad_output": fail.bad_output,
                "detail": detail,
                "replacement_preview": (new_text or "")[:500],
                "script_expected": expected_script,
            }
        )
        time.sleep(api_delay_sec)

    return collisions
