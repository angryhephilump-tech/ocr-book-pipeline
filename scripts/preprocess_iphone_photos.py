#!/usr/bin/env python3
"""Prepare iPhone document photos for the PDF transcribe pipeline.

Usage:
  python scripts/preprocess_iphone_photos.py path/to/photos_folder

Output goes to <folder>_preprocessed/ as grayscale JPEGs, longest edge 2576px.
Tips when shooting: flat surface, camera straight down, even lighting, no shadows,
highest resolution, fill the frame with the page.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe import MAX_IMAGE_DIM, prepare_image_file  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preprocess iPhone photos for Claude OCR.")
    parser.add_argument("input_dir", type=Path, help="Folder of phone photos")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output folder (default: <input>_preprocessed)",
    )
    args = parser.parse_args(argv)
    src = args.input_dir.resolve()
    if not src.is_dir():
        raise SystemExit(f"Not a directory: {src}")

    out = args.output_dir or src.parent / f"{src.name}_preprocessed"
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p for p in src.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not files:
        raise SystemExit(f"No images found in {src}")

    for idx, path in enumerate(files, start=1):
        dest = out / f"page_{idx:04d}.jpg"
        prepare_image_file(path, dest)
        print(f"  {path.name} -> {dest.name}")

    print(f"\nDone. {len(files)} images in:\n  {out}")
    print("Feed these into the main pipeline (folder of images mode) or PDF workflow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
