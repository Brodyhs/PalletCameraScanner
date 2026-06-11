"""Pure rendering/degradation functions for the synthetic source.

All functions are deterministic given their inputs (randomness comes in via
an explicit ``numpy.random.Generator``); none mutate their arguments.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import qrcode

from palletscan._compat import import_pylibdmtx
from palletscan.types import Symbology

pylibdmtx = import_pylibdmtx()

_QUIET_ZONE_MODULES = {Symbology.QR: 4, Symbology.DATAMATRIX: 3}


@dataclass(frozen=True, slots=True)
class RenderedSymbol:
    """A rendered symbol on a white background including its quiet zone."""

    image: np.ndarray  # 2-D uint8
    symbology: Symbology
    modules: int  # modules per side, excluding quiet zone
    px_per_module: float  # achieved pitch in ``image``


def _scale_and_pad(
    symbol: np.ndarray, modules: int, px_per_module: float, symbology: Symbology
) -> RenderedSymbol:
    """Resize a crisp symbol bitmap to the requested pitch and add quiet zone."""
    target = max(modules, round(modules * px_per_module))
    resized = cv2.resize(symbol, (target, target), interpolation=cv2.INTER_AREA)
    quiet = round(_QUIET_ZONE_MODULES[symbology] * px_per_module)
    padded = cv2.copyMakeBorder(
        resized, quiet, quiet, quiet, quiet, cv2.BORDER_CONSTANT, value=255
    )
    return RenderedSymbol(
        image=padded,
        symbology=symbology,
        modules=modules,
        px_per_module=target / modules,
    )


def render_qr(payload: str, px_per_module: float) -> RenderedSymbol:
    """Render a QR code at ECC level Q with the requested px/module pitch."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=8, border=0
    )
    qr.add_data(payload)
    qr.make(fit=True)
    pil = qr.make_image(fill_color="black", back_color="white").convert("L")
    return _scale_and_pad(
        np.asarray(pil, dtype=np.uint8), qr.modules_count, px_per_module, Symbology.QR
    )


def render_datamatrix(payload: str, px_per_module: float) -> RenderedSymbol:
    """Render an ECC200 Data Matrix with the requested px/module pitch."""
    enc = pylibdmtx.encode(payload.encode("utf-8"))
    img = np.frombuffer(enc.pixels, dtype=np.uint8).reshape(
        enc.height, enc.width, enc.bpp // 8
    )[:, :, 0].copy()
    # Crop the symbol out of libdmtx's white margin.
    black_y, black_x = np.nonzero(img < 128)
    x0, x1 = int(black_x.min()), int(black_x.max()) + 1
    y0, y1 = int(black_y.min()), int(black_y.max()) + 1
    symbol = img[y0:y1, x0:x1]
    # The top edge is the alternating clock track: one transition per module
    # boundary, so modules = transitions + 1. Robust to libdmtx's rendering
    # scale without assuming its default module pixel size.
    top_row = (symbol[0, :] < 128).astype(np.int8)
    modules = int(np.count_nonzero(np.diff(top_row))) + 1
    return _scale_and_pad(symbol, modules, px_per_module, Symbology.DATAMATRIX)


def perspective_warp(
    patch: np.ndarray, angle_deg: float, background: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Foreshorten ``patch`` for an approach angle, as seen by a fixed camera.

    Horizontal scale shrinks by cos(angle); the far (right) edge additionally
    keystones vertically. Returns ``(warped, mask)`` where ``mask`` is uint8
    255 over valid patch pixels — both the same size as ``patch``.
    """
    h, w = patch.shape[:2]
    theta = np.deg2rad(angle_deg)
    w2 = w * float(np.cos(theta))
    keystone = 0.5 * h * float(np.sin(theta)) * 0.35  # far-edge vertical shrink
    x0 = (w - w2) / 2.0
    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    dst = np.array(
        [
            [x0, 0],
            [x0 + w2, keystone],
            [x0 + w2, h - keystone],
            [x0, h],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(
        patch, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=background
    )
    mask = cv2.warpPerspective(
        np.full((h, w), 255, np.uint8), m, (w, h), flags=cv2.INTER_NEAREST,
        borderValue=0,
    )
    return warped, mask


def motion_blur(img: np.ndarray, length_px: float) -> np.ndarray:
    """Horizontal directional blur: kernel length == pixel displacement
    during the exposure. Lengths under 2 px are a no-op."""
    length = int(round(length_px))
    if length < 2:
        return img.copy()
    kernel = np.full((1, length), 1.0 / length, dtype=np.float32)
    return cv2.filter2D(img, -1, kernel, borderType=cv2.BORDER_REPLICATE)


def apply_contrast(img: np.ndarray, contrast: float) -> np.ndarray:
    """Scale contrast about mid-gray: 1.0 is identity, 0.0 is flat gray."""
    out = 128.0 + contrast * (img.astype(np.float32) - 128.0)
    return np.clip(out, 0, 255).astype(np.uint8)


def lighting_gradient(
    img: np.ndarray, amplitude: float, direction_deg: float
) -> np.ndarray:
    """Add a linear illumination ramp of ±``amplitude`` across the image."""
    if amplitude == 0:
        return img.copy()
    h, w = img.shape[:2]
    theta = np.deg2rad(direction_deg)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    proj = (xx / max(w - 1, 1)) * np.cos(theta) + (yy / max(h - 1, 1)) * np.sin(theta)
    ramp = (proj - proj.mean()) * 2.0 * amplitude
    return np.clip(img.astype(np.float32) + ramp, 0, 255).astype(np.uint8)


def add_noise(
    img: np.ndarray, sigma: float, rng: np.random.Generator
) -> np.ndarray:
    """Add zero-mean Gaussian sensor noise."""
    if sigma <= 0:
        return img.copy()
    noisy = img.astype(np.float32) + rng.normal(0.0, sigma, img.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def occlude(
    img: np.ndarray, frac: float, rng: np.random.Generator, value: int = 110
) -> np.ndarray:
    """Cover ``frac`` of the image area with a vertical bar at a random x."""
    if frac <= 0:
        return img.copy()
    h, w = img.shape[:2]
    bar_w = max(1, int(round(w * frac)))
    x = int(rng.integers(0, max(w - bar_w, 1)))
    out = img.copy()
    out[:, x : x + bar_w] = value
    return out
