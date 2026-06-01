"""PDF page extraction to images."""

from __future__ import annotations

import os
from pathlib import Path

from pipeline.paths import configure_runtime, poppler_bin_dir

configure_runtime()


def _poppler_path() -> str | None:
    env = os.environ.get("POPPLER_PATH", "").strip()
    if env and Path(env).is_dir():
        return env
    found = poppler_bin_dir()
    return str(found) if found else None


def pdf_to_page_images(pdf_path: Path, output_dir: Path, dpi: int = 200) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise SystemExit(
            "PDF input requires pdf2image and Poppler.\n"
            "  pip install pdf2image\n"
            "  Bundle Poppler in vendor/poppler/ or install Poppler and add to PATH."
        ) from exc

    poppler = _poppler_path()
    kwargs = {"dpi": dpi}
    if poppler:
        kwargs["poppler_path"] = poppler

    images = convert_from_path(str(pdf_path), **kwargs)
    paths = []
    stem = pdf_path.stem
    for i, img in enumerate(images, start=1):
        out = output_dir / f"{stem}_page_{i:03d}.png"
        img.save(out, "PNG")
        paths.append(out)
    return paths


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def collect_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if is_pdf(input_path):
            tmp = input_path.parent / "_pdf_pages"
            return pdf_to_page_images(input_path, tmp)
        return [input_path]
    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".pdf"}
    files = sorted(p for p in input_path.iterdir() if p.suffix.lower() in exts)
    result = []
    for f in files:
        if is_pdf(f):
            result.extend(pdf_to_page_images(f, input_path / "_pdf_pages" / f.stem))
        else:
            result.append(f)
    return result
