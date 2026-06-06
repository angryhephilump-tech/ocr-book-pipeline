"""Install and resolve Tesseract language packs for a project."""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

from pipeline.lang_catalog import BUNDLED_LANG_CODES
from pipeline.paths import tessdata_dir

logger = logging.getLogger(__name__)

TESSDATA_BASE_URL = "https://github.com/tesseract-ocr/tessdata/raw/main/{lang}.traineddata"


def installed_language_codes() -> set[str]:
    data_dir = tessdata_dir()
    if not data_dir or not data_dir.is_dir():
        return set()
    return {p.stem for p in data_dir.glob("*.traineddata") if p.stem not in ("osd",)}


def is_language_installed(code: str) -> bool:
    return code in installed_language_codes()


def download_language(code: str, dest_dir: Path | None = None) -> Path:
    dest = dest_dir or tessdata_dir()
    if not dest:
        raise RuntimeError("Tesseract tessdata directory not found")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{code}.traineddata"
    if target.is_file():
        return target
    url = TESSDATA_BASE_URL.format(lang=code)
    logger.info("Downloading tessdata %s from %s", code, url)
    urllib.request.urlretrieve(url, target)
    return target


def ensure_languages(codes: list[str]) -> dict[str, str]:
    """Ensure traineddata files exist; download on demand. Returns status per code."""
    needed = sorted({c for c in codes if c})
    status: dict[str, str] = {}
    for code in needed:
        if is_language_installed(code):
            status[code] = "installed"
            continue
        try:
            download_language(code)
            status[code] = "downloaded"
        except Exception as exc:
            status[code] = f"error: {exc}"
    return status


def ensure_project_languages(primary: str, secondary: str | None = None) -> dict[str, str]:
    codes = [primary]
    if secondary:
        codes.append(secondary)
    return ensure_languages(codes)


def languages_for_api() -> list[dict]:
    from pipeline.lang_catalog import list_languages

    installed = installed_language_codes()
    bundled = set(BUNDLED_LANG_CODES)
    out = []
    for lang in list_languages():
        code = lang["code"]
        out.append(
            {
                "code": code,
                "name": lang.get("name", code),
                "installed": code in installed,
                "bundled": code in bundled,
            }
        )
    return out
