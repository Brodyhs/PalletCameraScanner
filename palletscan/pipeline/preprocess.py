"""Preprocessing variants for the decode fallback cascade.

Pure functions, applied to ROI crops only when a pass remains undecoded.
Ordered by measured usefulness on the synthetic envelope: unsharp masking
recovers motion blur (the dominant failure), adaptive threshold and CLAHE
recover low contrast, small rotations help borderline symbol geometry.
"""

from __future__ import annotations

from collections.abc import Callable

import cv2
import numpy as np

Variant = Callable[[np.ndarray], np.ndarray]


def unsharp(img: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(img, (0, 0), 1.5)
    return cv2.addWeighted(img, 2.5, blur, -1.5, 0)


def adaptive_threshold(img: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
    )


def clahe(img: np.ndarray) -> np.ndarray:
    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(img)


def _rotate(img: np.ndarray, deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    return cv2.warpAffine(
        img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def rotate_plus(img: np.ndarray) -> np.ndarray:
    return _rotate(img, 10.0)


def rotate_minus(img: np.ndarray) -> np.ndarray:
    return _rotate(img, -10.0)


#: Fallback order: most effective first.
VARIANTS: list[tuple[str, Variant]] = [
    ("unsharp", unsharp),
    ("athresh", adaptive_threshold),
    ("clahe", clahe),
    ("rot+10", rotate_plus),
    ("rot-10", rotate_minus),
]
