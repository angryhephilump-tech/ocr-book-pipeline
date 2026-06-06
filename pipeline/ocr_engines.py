"""OCR engine wrappers — DeepSeek OCR via Archive Gateway (3 runs)."""

from __future__ import annotations

import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image

from pipeline.gateway_client import GatewayError, request_ocr

logger = logging.getLogger(__name__)

PASS_LABELS: dict[str, str] = {
    "A": "DeepSeek — original scan (run 1)",
    "B": "DeepSeek — original scan (run 2)",
    "C": "DeepSeek — enhanced scan (run 3)",
}

class DeepSeekEngineError(RuntimeError):
    """DeepSeek OCR failed — pipeline requires gateway access."""


@dataclass
class WordSpan:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]
    line_id: int = 0


@dataclass
class OcrResult:
    run_id: str
    engine: str
    words: list[WordSpan] = field(default_factory=list)
    full_text: str = ""
    lines: list[str] = field(default_factory=list)

    def rebuild_text(self) -> None:
        if not self.words:
            self.full_text = ""
            self.lines = []
            return
        by_line: dict[int, list[WordSpan]] = {}
        for w in self.words:
            by_line.setdefault(w.line_id, []).append(w)
        lines_out = []
        for lid in sorted(by_line):
            line_words = sorted(by_line[lid], key=lambda w: w.bbox[0])
            lines_out.append(" ".join(w.text for w in line_words if w.text))
        self.lines = lines_out
        self.full_text = "\n".join(lines_out)


def _encode_png_bytes(bgr) -> bytes:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def _fallback_word_boxes_with_tesseract(bgr) -> list[dict[str, Any]]:
    try:
        import pytesseract
    except Exception:
        return []
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    data = pytesseract.image_to_data(pil, output_type=pytesseract.Output.DICT, config="--psm 3")
    out: list[dict[str, Any]] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = str(data["text"][i] or "").strip()
        if not text:
            continue
        conf = float(data["conf"][i]) if str(data["conf"][i]).strip() else 0.0
        if conf < 0:
            conf = 0.0
        x = int(data["left"][i]); y = int(data["top"][i])
        w = int(data["width"][i]); h = int(data["height"][i])
        out.append(
            {
                "text": text,
                "confidence": conf,
                "bbox": [x, y, x + w, y + h],
                "line_id": int(data.get("line_num", [0])[i] or 0),
            }
        )
    return out


def _parse_gateway_words(raw_words: list[dict[str, Any]], fallback_text: str) -> list[WordSpan]:
    words: list[WordSpan] = []
    if raw_words:
        for idx, w in enumerate(raw_words):
            text = str(w.get("text") or "").strip()
            if not text:
                continue
            conf = float(w.get("confidence", 70.0))
            box = w.get("bbox") or [0, 0, 0, 0]
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                box = [0, 0, 0, 0]
            line_id = int(w.get("line_id", idx))
            words.append(
                WordSpan(
                    text=text,
                    confidence=max(0.0, min(100.0, conf)),
                    bbox=(int(box[0]), int(box[1]), int(box[2]), int(box[3])),
                    line_id=line_id,
                )
            )
        return words

    line_id = 0
    for line in fallback_text.splitlines():
        for token in line.split():
            words.append(WordSpan(text=token, confidence=70.0, bbox=(0, 0, 0, 0), line_id=line_id))
        line_id += 1
    return words


def _maybe_extract_json_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    raw = text.strip()
    if not raw:
        return "", []
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = json.loads(raw)
            parsed_text = str(payload.get("text") or "")
            words = payload.get("words") or []
            if isinstance(words, list):
                words = [w for w in words if isinstance(w, dict)]
            else:
                words = []
            return parsed_text, words
        except json.JSONDecodeError:
            return text, []
    return text, []


def _call_gateway(
    *,
    bgr,
    run_id: str,
    license_key: str,
    temperature: float,
    primary_lang: str,
    secondary_lang: str | None,
    indigenous_mode: bool,
) -> OcrResult:
    payload = request_ocr(
        license_key=license_key,
        image_bytes=_encode_png_bytes(bgr),
        temperature=temperature,
        primary_language=primary_lang,
        secondary_language=secondary_lang,
        indigenous_mode=indigenous_mode,
    )
    text = str(payload.get("text") or "")
    raw_words = payload.get("words") or []
    if not raw_words and text:
        parsed_text, parsed_words = _maybe_extract_json_text(text)
        if parsed_text:
            text = parsed_text
        if parsed_words:
            raw_words = parsed_words
    if not raw_words:
        raw_words = _fallback_word_boxes_with_tesseract(bgr)
    words = _parse_gateway_words(raw_words, text)
    result = OcrResult(run_id=run_id, engine="deepseek", words=words)
    if text.strip():
        result.full_text = text.strip()
        result.lines = text.splitlines()
    else:
        result.rebuild_text()
    return result


def verify_deepseek_available(license_key: str, primary_lang: str = "spa") -> None:
    img = np.ones((64, 220, 3), dtype=np.uint8) * 255
    cv2.putText(img, "test", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    try:
        result = _call_gateway(
            bgr=img,
            run_id="verify",
            license_key=license_key,
            temperature=0.3,
            primary_lang=primary_lang,
            secondary_lang=None,
            indigenous_mode=False,
        )
    except GatewayError as exc:
        raise DeepSeekEngineError(f"DeepSeek verification failed: {exc}") from exc
    if not result.full_text.strip() and not result.words:
        raise DeepSeekEngineError("DeepSeek verification returned empty output")


def _run_pass_task(args: tuple) -> tuple[str, OcrResult]:
    bgr, run_id, license_key, temperature, primary_lang, secondary_lang, indigenous_mode, on_pass = args
    if on_pass:
        on_pass(
            {
                "stage": "pass_start",
                "pass_id": run_id,
                "pass_label": PASS_LABELS.get(run_id, run_id),
                "engine": "deepseek",
            }
        )
    try:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                result = _call_gateway(
                    bgr=bgr,
                    run_id=run_id,
                    license_key=license_key,
                    temperature=temperature,
                    primary_lang=primary_lang,
                    secondary_lang=secondary_lang,
                    indigenous_mode=indigenous_mode,
                )
                break
            except Exception as exc:  # transient gateway or upstream failure
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise exc
    except Exception as exc:
        if on_pass:
            on_pass(
                {
                    "stage": "pass_error",
                    "pass_id": run_id,
                    "pass_label": PASS_LABELS.get(run_id, run_id),
                }
            )
        raise DeepSeekEngineError(str(exc)) from exc
    if on_pass:
        on_pass(
            {
                "stage": "pass_done",
                "pass_id": run_id,
                "pass_label": PASS_LABELS.get(run_id, run_id),
                "engine": result.engine,
                "word_count": len(result.words),
            }
        )
    return run_id, result


def run_four_passes(
    original_bgr,
    preprocessed_bgr,
    license_key: str,
    primary_lang: str = "spa",
    *,
    secondary_lang: str | None = None,
    indigenous_mode: bool = False,
    parallel: bool = True,
    on_pass=None,
) -> dict[str, OcrResult]:
    """Run 3 DeepSeek passes with input variation; parallel by default."""
    tasks = [
        (original_bgr, "A", license_key, 0.3, primary_lang, secondary_lang, indigenous_mode, on_pass),
        (original_bgr, "B", license_key, 0.3, primary_lang, secondary_lang, indigenous_mode, on_pass),
        (preprocessed_bgr, "C", license_key, 0.3, primary_lang, secondary_lang, indigenous_mode, on_pass),
    ]
    results: dict[str, OcrResult] = {}
    if not parallel:
        for t in tasks:
            rid, res = _run_pass_task(t)
            results[rid] = res
        return results

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_run_pass_task, t) for t in tasks]
        for fut in as_completed(futures):
            rid, res = fut.result()
            results[rid] = res
    return results
