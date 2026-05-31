"""OCR engine wrappers for PaddleOCR and Tesseract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import pytesseract
from PIL import Image


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


_paddle_instance: Any = None


def _get_paddle(lang: str = "es"):
    global _paddle_instance
    if _paddle_instance is None:
        from paddleocr import PaddleOCR

        _paddle_instance = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
    return _paddle_instance


def _paddle_lang(primary: str) -> str:
    mapping = {"spa": "es", "eng": "en", "fra": "fr"}
    return mapping.get(primary, "es")


def run_paddle(bgr, run_id: str, primary_lang: str = "spa") -> OcrResult:
    ocr = _get_paddle(_paddle_lang(primary_lang))
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    raw = ocr.ocr(rgb, cls=True)
    words: list[WordSpan] = []
    if raw and raw[0]:
        for line_idx, item in enumerate(raw[0]):
            if not item or len(item) < 2:
                continue
            box, (text, conf) = item[0], item[1]
            if not text or not str(text).strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            words.append(
                WordSpan(
                    text=str(text).strip(),
                    confidence=float(conf) * 100.0 if conf <= 1 else float(conf),
                    bbox=bbox,
                    line_id=line_idx,
                )
            )
    result = OcrResult(run_id=run_id, engine="paddle", words=words)
    result.rebuild_text()
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
