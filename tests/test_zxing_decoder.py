"""ZxingDecoder + the ``decode.engine: zxing`` cascade path.

Skipped in full when the optional zxing-cpp package is absent, so the default
(legacy) install needs nothing extra.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

pytest.importorskip("zxingcpp")  # the optional [zxing] extra

from palletscan.config import DecodeConfig, DecodeEngineKind
from palletscan.pipeline import preprocess
from palletscan.pipeline.decode_engine import (
    DecodeEngine,
    PassDecodeContext,
    _variant_task_zxing,
)
from palletscan.pipeline.decoders import ZxingDecoder
from palletscan.sources.render import motion_blur, render_datamatrix, render_qr
from palletscan.types import Frame, Roi, Symbology

PAYLOAD = "PLT-000042"


@pytest.fixture()
def executor():
    with ThreadPoolExecutor(max_workers=2) as ex:
        yield ex


def _frame_with(symbol: np.ndarray, at: tuple[int, int] = (60, 80)) -> tuple[Frame, Roi]:
    canvas = np.full((360, 640), 120, np.uint8)
    y, x = at
    canvas[y : y + symbol.shape[0], x : x + symbol.shape[1]] = symbol
    frame = Frame(image=canvas, ts=1.0, frame_index=30, source_id="cam0")
    roi = Roi(x - 20, y - 20, symbol.shape[1] + 40, symbol.shape[0] + 40)
    return frame, roi


def test_zxing_decoder_reads_qr_and_dm() -> None:
    dec = ZxingDecoder()
    qr_hits = dec.decode(render_qr(PAYLOAD, 5.0).image)
    dm_hits = dec.decode(render_datamatrix(PAYLOAD, 5.0).image)
    assert [h.payload for h in qr_hits] == [PAYLOAD]
    assert qr_hits[0].symbology is Symbology.QR
    assert [h.payload for h in dm_hits] == [PAYLOAD]
    assert dm_hits[0].symbology is Symbology.DATAMATRIX
    # position maps to a sane, non-empty bounding box
    r = qr_hits[0].roi
    assert r.w > 0 and r.h > 0 and r.x >= 0 and r.y >= 0


def test_zxing_decoder_symbology_filter() -> None:
    dec = ZxingDecoder()
    dm = render_datamatrix(PAYLOAD, 5.0).image
    assert dec.decode(dm, (Symbology.QR,)) == []  # restricted away from DM
    assert [h.payload for h in dec.decode(dm, (Symbology.DATAMATRIX,))] == [PAYLOAD]


def test_zxing_non_ascii_payload_roundtrips() -> None:
    # bytes -> decode_payload (UTF-8 then Latin-1), never a U+FFFD replacement
    payload = "PLT-café"  # café
    hits = ZxingDecoder().decode(render_qr(payload, 6.0).image)
    assert [h.payload for h in hits] == [payload]
    assert "�" not in hits[0].payload


def test_engine_zxing_decodes_qr(executor) -> None:
    engine = DecodeEngine(DecodeConfig(engine=DecodeEngineKind.ZXING), executor)
    frame, roi = _frame_with(render_qr(PAYLOAD, 4.0).image)
    results = engine.decode_frame(frame, roi, PassDecodeContext())
    assert [r.payload for r in results] == [PAYLOAD]
    assert results[0].decoder == "zxing"
    assert engine.counters.zxing_calls == 1
    # the legacy decoders are never touched on the zxing path
    assert engine.counters.pyzbar_calls == 0
    assert engine.counters.dmtx_calls == 0


def test_engine_zxing_decodes_datamatrix(executor) -> None:
    engine = DecodeEngine(DecodeConfig(engine=DecodeEngineKind.ZXING), executor)
    frame, roi = _frame_with(render_datamatrix(PAYLOAD, 4.0).image)
    results = engine.decode_frame(frame, roi, PassDecodeContext())
    assert [r.payload for r in results] == [PAYLOAD]
    assert results[0].decoder == "zxing"
    assert results[0].symbology is Symbology.DATAMATRIX


def test_zxing_variant_task_decodes_clean_code() -> None:
    """The preprocessing-variant fallback uses zxing and tags the decoder."""
    img = np.ascontiguousarray(render_qr(PAYLOAD, 5.0).image)
    syms = (Symbology.QR, Symbology.DATAMATRIX)
    decoded = []
    for name, _ in preprocess.VARIANTS:
        _, decoder, hits = _variant_task_zxing(name, img, syms, 40)
        if hits:
            decoded.append((decoder, [h.payload for h in hits]))
    assert decoded, "no zxing preprocessing variant decoded a clean QR"
    assert all(d.startswith("zxing+") for d, _ in decoded)
    assert any(p == [PAYLOAD] for _, p in decoded)
