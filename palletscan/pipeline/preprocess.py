"""Preprocessing variants for the decode fallback cascade.

Pure functions, applied to ROI crops only when a pass remains undecoded.
Ordered by measured usefulness on the synthetic envelope: unsharp masking
recovers motion blur (the dominant failure), adaptive threshold and CLAHE
recover low contrast, small rotations help borderline symbol geometry.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import cv2
import numpy as np

from palletscan.types import Roi

Variant = Callable[[np.ndarray], np.ndarray]

_tls = threading.local()


def unsharp(img: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(img, (0, 0), 1.5)
    return cv2.addWeighted(img, 2.5, blur, -1.5, 0)


def adaptive_threshold(img: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
    )


def clahe(img: np.ndarray) -> np.ndarray:
    # cv2 CLAHE objects are not documented thread-safe, so keep one per
    # worker thread rather than reconstructing per call.
    c = getattr(_tls, "clahe", None)
    if c is None:
        c = _tls.clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return c.apply(img)


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

VARIANTS_BY_NAME: dict[str, Variant] = dict(VARIANTS)

_ROTATION_DEG = {"rot+10": 10.0, "rot-10": -10.0}


def map_roi_back(variant_name: str, roi: Roi, shape: tuple[int, ...]) -> Roi:
    """Map a decode ROI from a variant's output image back to its input.

    Every variant is geometry-preserving except the rotations, whose hits
    come back in rotated coordinates: inverse-rotate the rect's corners
    about the same center and take the bounding box.
    """
    deg = _ROTATION_DEG.get(variant_name)
    if deg is None:
        return roi
    h, w = int(shape[0]), int(shape[1])
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -deg, 1.0)
    corners = np.array(
        [
            [roi.x, roi.y],
            [roi.x + roi.w, roi.y],
            [roi.x, roi.y + roi.h],
            [roi.x + roi.w, roi.y + roi.h],
        ],
        dtype=np.float32,
    )
    pts = cv2.transform(corners[None, :, :], m)[0]
    x0 = int(np.floor(pts[:, 0].min()))
    y0 = int(np.floor(pts[:, 1].min()))
    x1 = int(np.ceil(pts[:, 0].max()))
    y1 = int(np.ceil(pts[:, 1].max()))
    return Roi(x0, y0, x1 - x0, y1 - y0).clamp((h, w))
