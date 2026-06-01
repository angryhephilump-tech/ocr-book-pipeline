#!/usr/bin/env python3
"""Pre-download PaddleOCR models into vendor/models/ for offline installs."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_MODELS = ROOT / "vendor" / "models"
LANG = "es"


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def main() -> int:
    print("Initializing PaddleOCR (downloads models on first run)…")
    from paddleocr import PaddleOCR

    PaddleOCR(use_angle_cls=True, lang=LANG, show_log=False)

    home = Path.home()
    paddle_home = home / ".paddleocr"
    if not paddle_home.is_dir():
        print(f"No cache at {paddle_home}. Check PaddleOCR install.", file=sys.stderr)
        return 1

    VENDOR_MODELS.mkdir(parents=True, exist_ok=True)
    copied = 0

    for item in sorted(paddle_home.rglob("*")):
        if not item.is_dir():
            continue
        name = item.name.lower()
        if name in {"det", "rec", "cls"} and any(item.iterdir()):
            dest = VENDOR_MODELS / name
            _copytree(item, dest)
            print(f"Copied {item} -> {dest}")
            copied += 1

    if copied == 0:
        print("No det/rec/cls folders found under ~/.paddleocr — inspect cache layout manually.", file=sys.stderr)
        return 1

    print(f"Done. Models in {VENDOR_MODELS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
