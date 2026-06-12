"""LivePreview: update/render round-trip, overlay drawing, linger pruning."""

from __future__ import annotations

import cv2
import numpy as np

from palletscan.config import WebConfig
from palletscan.types import DecodeResult, Frame, MotionResult, Roi, Symbology
from palletscan.web.preview import LivePreview

_CFG = WebConfig(preview_width=320, preview_quality=80)


def _frame(ts: float = 0.0, index: int = 0) -> Frame:
    image = np.tile(
        np.linspace(40, 200, 640, dtype=np.uint8), (360, 1)
    )  # horizontal gradient, decidedly non-flat
    return Frame(image=image, ts=ts, frame_index=index, source_id="camA")


def _decode(ts: float) -> DecodeResult:
    return DecodeResult(
        payload="PLT-000042",
        symbology=Symbology.QR,
        roi=Roi(100, 80, 200, 200),
        frame_index=int(ts * 30),
        ts=ts,
        source_id="camA",
        decoder="pyzbar",
        latency_ms=1.0,
    )


def _jpeg_pixels(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def test_render_before_first_frame_is_none() -> None:
    preview = LivePreview("camA", _CFG)
    data, stamp = preview.render_jpeg()
    assert data is None
    assert stamp == 0


def test_update_render_round_trip_and_stamp_advance() -> None:
    preview = LivePreview("camA", _CFG)
    preview.update(_frame(0.0, 0), MotionResult(False, None, None, 0.0), [])
    data, stamp = preview.render_jpeg()
    assert data is not None and data[:2] == b"\xff\xd8"  # JPEG magic
    assert stamp == 1
    pixels = _jpeg_pixels(data)
    assert pixels.shape[1] == 320  # downscaled to preview_width
    preview.update(_frame(1 / 30, 1), MotionResult(False, None, None, 0.0), [])
    assert preview.stamp == 2


def test_overlays_change_pixels() -> None:
    plain = LivePreview("camA", _CFG)
    boxed = LivePreview("camA", _CFG)
    plain.update(_frame(), MotionResult(False, None, None, 0.0), [])
    boxed.update(
        _frame(),
        MotionResult(True, "camA-000001", Roi(50, 50, 300, 250), 0.2),
        [_decode(0.0)],
    )
    plain_px = _jpeg_pixels(plain.render_jpeg()[0])
    boxed_px = _jpeg_pixels(boxed.render_jpeg()[0])
    assert plain_px.shape == boxed_px.shape
    assert (plain_px != boxed_px).any(), "overlay boxes were not drawn"


def test_decode_overlays_linger_then_expire() -> None:
    preview = LivePreview("camA", _CFG)
    motion = MotionResult(False, None, None, 0.0)
    preview.update(_frame(0.0, 0), motion, [_decode(0.0)])
    preview.update(_frame(0.5, 15), motion, [])  # within 1 s linger
    assert len(preview._decodes) == 1
    preview.update(_frame(2.0, 60), motion, [])  # past linger
    assert len(preview._decodes) == 0
