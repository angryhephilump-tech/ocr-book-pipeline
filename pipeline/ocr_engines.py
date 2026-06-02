"""OCR engine wrappers for PaddleOCR and Tesseract."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import cv2
import pytesseract
from PIL import Image

from pipeline.paddle_env import configure_paddle_env
from pipeline.paths import configure_runtime, paddle_model_dirs, paddlex_models_root

configure_paddle_env()
configure_runtime()

logger = logging.getLogger(__name__)

_paddle_instance: Any = None
_paddle_disabled = False
_paddle_disable_reason = ""


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


def paddle_is_disabled() -> bool:
    return _paddle_disabled


def paddle_disable_reason() -> str:
    return _paddle_disable_reason


def _disable_paddle(reason: str) -> None:
    global _paddle_disabled, _paddle_disable_reason
    _paddle_disabled = True
    _paddle_disable_reason = reason
    logger.warning("PaddleOCR disabled for this session: %s", reason)


def _paddle_lang(primary: str) -> str:
    mapping = {"spa": "es", "eng": "en", "fra": "fr"}
    return mapping.get(primary, "es")


def _get_paddle(lang: str = "es"):
    global _paddle_instance
    if _paddle_disabled:
        raise RuntimeError(_paddle_disable_reason or "PaddleOCR unavailable")
    if _paddle_instance is None:
        from paddleocr import PaddleOCR

        kwargs: dict[str, Any] = {
            "lang": lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
        }
        root = paddlex_models_root()
        if root:
            os.environ["PADDLE_PDX_HOME"] = str(root)
        try:
            _paddle_instance = PaddleOCR(**kwargs)
        except TypeError:
            model_dirs = paddle_model_dirs()
            legacy = {
                "use_angle_cls": True,
                "lang": lang,
            }
            for key, path in model_dirs.items():
                if path and key in ("det_model_dir", "rec_model_dir", "cls_model_dir"):
                    legacy[key] = str(path)
            try:
                legacy["show_log"] = False
                _paddle_instance = PaddleOCR(**legacy)
            except (TypeError, ValueError):
                legacy.pop("show_log", None)
                _paddle_instance = PaddleOCR(**legacy)
    return _paddle_instance


def _bbox_from_box(box) -> tuple[int, int, int, int]:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def _parse_paddle_v2(raw, run_id: str) -> OcrResult:
    words: list[WordSpan] = []
    if raw and raw[0]:
        for line_idx, item in enumerate(raw[0]):
            if not item or len(item) < 2:
                continue
            box, (text, conf) = item[0], item[1]
            if not text or not str(text).strip():
                continue
            words.append(
                WordSpan(
                    text=str(text).strip(),
                    confidence=float(conf) * 100.0 if conf <= 1 else float(conf),
                    bbox=_bbox_from_box(box),
                    line_id=line_idx,
                )
            )
    result = OcrResult(run_id=run_id, engine="paddle", words=words)
    result.rebuild_text()
    return result


def _parse_paddle_v3(results, run_id: str) -> OcrResult:
    words: list[WordSpan] = []
    if not results:
        return OcrResult(run_id=run_id, engine="paddle", words=words)

    page = results[0]
    data = page if isinstance(page, dict) else getattr(page, "json", None) or {}
    if hasattr(page, "keys"):
        data = dict(page)

    rec_texts = data.get("rec_texts") or []
    rec_scores = data.get("rec_scores") or []
    rec_boxes = data.get("rec_boxes") or data.get("dt_polys") or []

    for line_idx, text in enumerate(rec_texts):
        text = str(text).strip()
        if not text:
            continue
        conf = float(rec_scores[line_idx]) if line_idx < len(rec_scores) else 0.0
        if conf <= 1:
            conf *= 100.0
        bbox = (0, 0, 0, 0)
        if line_idx < len(rec_boxes):
            box = rec_boxes[line_idx]
            if hasattr(box, "tolist"):
                box = box.tolist()
            if box and isinstance(box[0], (list, tuple)):
                bbox = _bbox_from_box(box)
            elif len(box) >= 4:
                xs, ys = box[0::2], box[1::2]
                bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))

        words.append(WordSpan(text=text, confidence=conf, bbox=bbox, line_id=line_idx))

    result = OcrResult(run_id=run_id, engine="paddle", words=words)
    result.rebuild_text()
    return result


def _run_paddle_core(bgr, run_id: str, primary_lang: str = "spa") -> OcrResult:
    ocr = _get_paddle(_paddle_lang(primary_lang))
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        cv2.imwrite(tmp.name, rgb)
        path = tmp.name

    try:
        if hasattr(ocr, "predict"):
            raw = ocr.predict(path)
            return _parse_paddle_v3(raw, run_id)
        raw = ocr.ocr(rgb, cls=True)
        return _parse_paddle_v2(raw, run_id)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run_paddle(bgr, run_id: str, primary_lang: str = "spa") -> OcrResult:
    """Run PaddleOCR; on Windows CPU bugs, fall back to Tesseract for this pass."""
    if _paddle_disabled:
        result = run_tesseract(bgr, run_id, primary_lang)
        result.engine = "tesseract"
        return result
    try:
        return _run_paddle_core(bgr, run_id, primary_lang)
    except Exception as exc:
        _disable_paddle(str(exc))
        result = run_tesseract(bgr, run_id, primary_lang)
        result.engine = "tesseract"
        return result


def run_tesseract(bgr, run_id: str, primary_lang: str = "spa") -> OcrResult:
    lang_map = {"spa": "spa", "eng": "eng", "fra": "fra"}
    tess_lang = lang_map.get(primary_lang, "spa")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    data = pytesseract.image_to_data(
        pil, lang=tess_lang, output_type=pytesseract.Output.DICT, config="--psm 3"
    )
    words: list[WordSpan] = []
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            conf = 0.0
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(
            WordSpan(
                text=text,
                confidence=conf,
                bbox=(x, y, x + w, y + h),
                line_id=int(data["line_num"][i]),
            )
        )
    result = OcrResult(run_id=run_id, engine="tesseract", words=words)
    result.rebuild_text()
    return result


def run_four_passes(original_bgr, preprocessed_bgr, primary_lang: str = "spa") -> dict[str, OcrResult]:
    return {
        "A": run_paddle(original_bgr, "A", primary_lang),
        "B": run_paddle(preprocessed_bgr, "B", primary_lang),
        "C": run_tesseract(original_bgr, "C", primary_lang),
        "D": run_tesseract(preprocessed_bgr, "D", primary_lang),
    }
