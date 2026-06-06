#!/usr/bin/env python3
"""Automated QA mode for DeepSeek pivot release checks."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.store import CreditStore
from pipeline.consensus import analyze_runs
from pipeline.ocr_engines import OcrResult, WordSpan
from ocr_book import load_project_state, save_project_state

STATUS_PATH = ROOT / "status.json"


def _run_disagreement_check() -> bool:
    runs = {
        "A": OcrResult("A", "deepseek", [WordSpan("palabra", 95, (1, 1, 2, 2), 0)], "palabra"),
        "B": OcrResult("B", "deepseek", [WordSpan("paiabra", 94, (1, 1, 2, 2), 0)], "paiabra"),
        "C": OcrResult("C", "deepseek", [WordSpan("palabra", 93, (1, 1, 2, 2), 0)], "palabra"),
    }
    flags, _stats = analyze_runs(runs, confidence_threshold=85.0)
    return any("engine_disagreement" in f.reason for f in flags)


def _run_low_confidence_check() -> bool:
    runs = {
        "A": OcrResult("A", "deepseek", [WordSpan("texto", 60, (1, 1, 2, 2), 0)], "texto"),
        "B": OcrResult("B", "deepseek", [WordSpan("texto", 62, (1, 1, 2, 2), 0)], "texto"),
        "C": OcrResult("C", "deepseek", [WordSpan("texto", 59, (1, 1, 2, 2), 0)], "texto"),
    }
    flags, _stats = analyze_runs(runs, confidence_threshold=85.0)
    return any("low_confidence" in f.reason for f in flags)


def _run_credit_idempotency_check() -> bool:
    td = Path(tempfile.gettempdir()) / f"archive_qa_{int(time.time()*1000)}"
    td.mkdir(parents=True, exist_ok=True)
    store = CreditStore(td / "credits.db")
    store.upsert_activation(license_key="ABC", product_id="prod", email="x@y.z", credits=10)
    before = store.get_license("ABC").remaining_credits
    store.commit_page_credit(license_key="ABC", idempotency_key="p001", page_id="page_001")
    mid = store.get_license("ABC").remaining_credits
    store.commit_page_credit(license_key="ABC", idempotency_key="p001", page_id="page_001")
    after = store.get_license("ABC").remaining_credits
    # Best-effort cleanup on Windows file locks.
    try:
        os.remove(td / "credits.db")
        td.rmdir()
    except Exception:
        pass
    return before == 10 and mid == 9 and after == 9


def _run_resume_check() -> bool:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        state = load_project_state(out)
        state["completed_pages"] = {"page_001": {"flag_count": 0, "needs_review": False}}
        state["last_completed_page"] = 1
        save_project_state(out, state)
        loaded = load_project_state(out)
        return loaded.get("last_completed_page") == 1 and "page_001" in loaded.get("completed_pages", {})


def _run_crop_bbox_check() -> bool:
    # Structural check: flagged spans should carry bbox slot.
    runs = {
        "A": OcrResult("A", "deepseek", [WordSpan("abc", 95, (10, 10, 40, 24), 0)], "abc"),
        "B": OcrResult("B", "deepseek", [WordSpan("abd", 95, (10, 10, 40, 24), 0)], "abd"),
        "C": OcrResult("C", "deepseek", [WordSpan("abc", 95, (10, 10, 40, 24), 0)], "abc"),
    }
    flags, _ = analyze_runs(runs, 85.0)
    return len(flags) > 0 and hasattr(flags[0], "bbox")


def main() -> int:
    checks = {
        "disagreement_spans_surface": _run_disagreement_check(),
        "crop_bbox_structure_present": _run_crop_bbox_check(),
        "low_confidence_never_bypass": _run_low_confidence_check(),
        "credit_idempotency_retry_safe": _run_credit_idempotency_check(),
        "resume_state_roundtrip": _run_resume_check(),
    }
    qa_ok = all(checks.values())

    status = {}
    if STATUS_PATH.is_file():
        status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    status["qa_test_mode_passed"] = qa_ok
    status["qa_test_mode_checks"] = checks
    status["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")

    print(json.dumps({"ok": qa_ok, "checks": checks}, indent=2))
    return 0 if qa_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

