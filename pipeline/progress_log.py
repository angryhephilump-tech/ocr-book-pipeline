"""Append-only OCR job progress log (output folder)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

LOG_NAME = "ocr_progress.log"


def append_progress_log(output_dir: Path, event: dict[str, Any]) -> None:
    if not output_dir:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    record = {**event, "ts": time.time()}
    path = output_dir / LOG_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
