"""Light image preprocessing for OCR runs B and D."""

from __future__ import annotations

import cv2
import numpy as np


def _deskew_small_angle(gray: np.ndarray, max_angle: float = 8.0) -> np.ndarray:
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=120, minLineLength=gray.shape[1] // 4, maxLineGap=20
    )
    if lines is None:
        return gray
    angles = []
    for line in lines[:40]:
        x1, y1, x2, y2 = line[0]
        if x2 == x1:
            continue
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if -max_angle <= angle <= max_angle:
            angles.append(angle)
    if not angles:
        return gray
    median = float(np.median(angles))
    if abs(median) < 0.3:
        return gray
    h, w = gray.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, median, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def _conservative_border_trim(gray: np.ndarray, blank_threshold: int = 245) -> np.ndarray:
    h, w = gray.shape[:2]
    top, bottom, left, right = 0, h, 0, w
    margin = min(h, w) // 20
    max_trim = min(h, w) // 8

    def row_blank(y: int) -> bool:
        return float(np.mean(gray[y, :])) >= blank_threshold

    def col_blank(x: int) -> bool:
        return float(np.mean(gray[:, x])) >= blank_threshold

    while top < max_trim and row_blank(top):
        top += 1
    while bottom > h - max_trim and row_blank(bottom - 1):
        bottom -= 1
    while left < max_trim and col_blank(left):
        left += 1
    while right > w - max_trim and col_blank(right - 1):
        right -= 1

    if bottom - top < h - 2 * margin and right - left < w - 2 * margin:
        return gray[top:bottom, left:right]
    return gray


def _mild_contrast(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _gentle_bleed_reduction(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
    enhanced = cv2.addWeighted(gray, 1.4, blur, -0.4, 0)
    return np.clip(enhanced, 0, 255).astype(np.uint8)


def load_bgr(path: str) -> np.ndarray:
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    return img


def to_gray(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def light_preprocess(bgr: np.ndarray) -> np.ndarray:
    gray = to_gray(bgr)
    gray = _deskew_small_angle(gray)
    gray = _mild_contrast(gray)
    gray = _gentle_bleed_reduction(gray)
    gray = _conservative_border_trim(gray)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
