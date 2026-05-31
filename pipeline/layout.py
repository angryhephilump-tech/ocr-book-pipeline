"""Layout detection: footnotes, image regions, reading order."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from pipeline.ocr_engines import OcrResult, WordSpan


FOOTNOTE_START = "--- FOOTNOTE ---"
FOOTNOTE_END = "--- END FOOTNOTE ---"
IMAGE_MARKER = "[IMAGE]"


@dataclass
class Region:
    kind: str  # body | footnote | image
    bbox: tuple[int, int, int, int]
    y_order: int


def _region_has_text(words: list[WordSpan], bbox: tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = bbox
    count = 0
    for w in words:
        wx1, wy1, wx2, wy2 = w.bbox
        cx, cy = (wx1 + wx2) / 2, (wy1 + wy2) / 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            count += len(w.text)
    return count >= 8


def detect_image_regions(bgr: np.ndarray, words: list[WordSpan]) -> list[tuple[int, int, int, int]]:
    """Find large visual regions with little OCR text -> [IMAGE] candidates."""
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = (h * w) * 0.04
    regions = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = bw * bh
        if area < min_area:
            continue
        if bw > w * 0.92 and bh > h * 0.92:
            continue
        bbox = (x, y, x + bw, y + bh)
        if not _region_has_text(words, bbox):
            roi = gray[y : y + bh, x : x + bw]
            if float(np.std(roi)) > 18:
                regions.append(bbox)
    regions.sort(key=lambda b: (b[1], b[0]))
    return _merge_overlapping(regions)


def _merge_overlapping(boxes: list[tuple[int, int, int, int]], iou_thresh: float = 0.3):
    if not boxes:
        return []
    merged = []
    used = [False] * len(boxes)

    def iou(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        return inter / float(area_a + area_b - inter + 1e-6)

    for i, box in enumerate(boxes):
        if used[i]:
            continue
        group = [box]
        used[i] = True
        for j in range(i + 1, len(boxes)):
            if used[j]:
                continue
            if iou(box, boxes[j]) >= iou_thresh:
                group.append(boxes[j])
                used[j] = True
        xs1 = min(b[0] for b in group)
        ys1 = min(b[1] for b in group)
        xs2 = max(b[2] for b in group)
        ys2 = max(b[3] for b in group)
        merged.append((xs1, ys1, xs2, ys2))
    return merged


def estimate_footnote_bbox(h: int, w: int) -> tuple[int, int, int, int]:
    return (0, int(h * 0.78), w, h)


def split_body_footnote_words(
    words: list[WordSpan], h: int, w: int
) -> tuple[list[WordSpan], list[WordSpan]]:
    fn_bbox = estimate_footnote_bbox(h, w)
    fx1, fy1, fx2, fy2 = fn_bbox
    body, foot = [], []
    for wspan in words:
        cy = (wspan.bbox[1] + wspan.bbox[3]) / 2
        if cy >= fy1:
            foot.append(wspan)
        else:
            body.append(wspan)
    if len(foot) < 2:
        return words, []
    return body, foot


def words_to_text(words: list[WordSpan]) -> str:
    if not words:
        return ""
    by_line: dict[int, list[WordSpan]] = {}
    for w in words:
        by_line.setdefault(w.line_id, []).append(w)
    lines = []
    for lid in sorted(by_line):
        line_words = sorted(by_line[lid], key=lambda x: x.bbox[0])
        lines.append(" ".join(x.text for x in line_words))
    return "\n".join(lines)


def compose_page_text(
    bgr: np.ndarray,
    primary_result: OcrResult,
    image_regions: list[tuple[int, int, int, int]] | None = None,
) -> tuple[str, dict]:
    h, w = bgr.shape[:2]
    words = primary_result.words
    body_words, foot_words = split_body_footnote_words(words, h, w)
    images = image_regions if image_regions is not None else detect_image_regions(bgr, words)

    segments: list[tuple[int, str, str]] = []
    body_text = words_to_text(body_words)
    if body_text:
        segments.append((0, "body", body_text))

    for idx, bbox in enumerate(images):
        segments.append((bbox[1], "image", IMAGE_MARKER))

    if foot_words:
        foot_text = words_to_text(foot_words)
        segments.append((estimate_footnote_bbox(h, w)[1], "footnote", foot_text))

    segments.sort(key=lambda s: s[0])
    parts = []
    meta = {"has_footnotes": bool(foot_words), "image_regions": images, "footnote_bbox": None}
    if foot_words:
        meta["footnote_bbox"] = estimate_footnote_bbox(h, w)

    for _, kind, content in segments:
        if kind == "footnote":
            parts.append(FOOTNOTE_START)
            parts.append(content)
            parts.append(FOOTNOTE_END)
        else:
            parts.append(content)

    return "\n\n".join(p for p in parts if p), meta
