#!/usr/bin/env python3
"""Pre-download PaddleOCR / PaddleX models into vendor/models/paddlex for offline installs."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_PADDLEX = ROOT / "vendor" / "paddlex" / "official_models"
LANG = "es"

os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["FLAGS_use_mkldnn"] = "0"


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def main() -> int:
    print("Initializing PaddleOCR (downloads models on first run)...")
    from paddleocr import PaddleOCR

    try:
        PaddleOCR(
            lang=LANG,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
        )
    except Exception as exc:
        print(f"PaddleOCR init warning: {exc}", file=sys.stderr)

    sources = [
        Path.home() / ".paddlex" / "official_models",
        Path.home() / ".paddleocr",
    ]

    copied_any = False
    VENDOR_PADDLEX.mkdir(parents=True, exist_ok=True)

    for src_root in sources:
        if not src_root.is_dir():
            continue
        for item in src_root.iterdir():
            if not item.is_dir():
                continue
            dest = VENDOR_PADDLEX / item.name
            if dest.exists():
                continue
            _copytree(item, dest)
            print(f"Copied {item.name} -> {dest}")
            copied_any = True

    legacy = VENDOR_PADDLEX.parent
    for name in ("det", "rec", "cls"):
        src = legacy / name
        if src.is_dir() and not (VENDOR_PADDLEX / name).exists():
            _copytree(src, VENDOR_PADDLEX / name)
            copied_any = True

    if not copied_any:
        print("No models found under ~/.paddlex or ~/.paddleocr", file=sys.stderr)
        return 1

    print(f"Done. Models in {VENDOR_PADDLEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
