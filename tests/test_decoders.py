"""Decoder wrapper normalization, timeouts, and error messages."""

from __future__ import annotations

import sys
import time

import numpy as np
import pytest

from palletscan.pipeline.decoders import (
    PylibdmtxDecoder,
    PyzbarDecoder,
    _import_pyzbar,
)
from palletscan.sources.render import render_datamatrix, render_qr
from palletscan.types import Symbology

PAYLOAD = "PLT-000777"


def _paste(symbol: np.ndarray, canvas_hw: tuple[int, int], at: tuple[int, int]) -> np.ndarray:
    canvas = np.full(canvas_hw, 255, np.uint8)
    y, x = at
    canvas[y : y + symbol.shape[0], x : x + symbol.shape[1]] = symbol
    return canvas


def test_pyzbar_normalizes_to_rawdecode_with_contained_roi() -> None:
    sym = render_qr(PAYLOAD, 4.0)
    canvas = _paste(sym.image, (400, 600), (120, 250))
    results = PyzbarDecoder().decode(canvas)
    assert len(results) == 1
    r = results[0]
    assert r.payload == PAYLOAD
    assert r.symbology is Symbology.QR
    # ROI must land on the pasted symbol (within the quiet zone margin)
    assert 250 <= r.roi.x <= 250 + sym.image.shape[1]
    assert 120 <= r.roi.y <= 120 + sym.image.shape[0]
    assert r.roi.w > 0 and r.roi.h > 0


def test_pylibdmtx_roi_converted_from_bottom_left_origin() -> None:
    sym = render_datamatrix(PAYLOAD, 4.0)
    canvas = _paste(sym.image, (400, 600), (150, 200))
    results = PylibdmtxDecoder().decode(canvas, timeout_ms=500)
    assert len(results) == 1
    r = results[0]
    assert r.payload == PAYLOAD
    assert r.symbology is Symbology.DATAMATRIX
    assert 200 <= r.roi.x <= 200 + sym.image.shape[1]
    assert 150 <= r.roi.y <= 150 + sym.image.shape[0]


def test_pylibdmtx_timeout_is_honored_on_undecodable_image() -> None:
    noise = np.random.default_rng(0).integers(0, 255, (800, 800), np.uint8)
    start = time.perf_counter()
    results = PylibdmtxDecoder().decode(noise, timeout_ms=50)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert results == []
    # generous margin: libdmtx checks its deadline between internal scans
    assert elapsed_ms < 1500


def test_missing_native_lib_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod in list(sys.modules):
        if mod.startswith("pyzbar"):
            monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, "pyzbar", None)  # forces ImportError
    with pytest.raises(RuntimeError, match="brew install"):
        _import_pyzbar()


# -- REVIEW_SYSTEM_0c30c77 finding 14 ------------------------------------------


def test_decode_payload_utf8_round_trips() -> None:
    from palletscan.pipeline.decoders import decode_payload

    assert decode_payload(b"PLT-000123") == "PLT-000123"
    assert decode_payload("PLT-MÜNCHEN-0042".encode("utf-8")) == "PLT-MÜNCHEN-0042"


def test_decode_payload_latin1_fallback_never_replaces() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 14 (repro: the stored payload was
    PLT-M�NCHEN-0042 and reconciliation bucketed the pallet as missing
    AND unexpected forever): non-UTF-8 symbol bytes decode via Latin-1 —
    ISO/IEC 16022's default interpretation, a total 1:1 mapping — so no
    byte sequence ever becomes U+FFFD."""
    from palletscan.pipeline.decoders import decode_payload

    assert decode_payload(b"PLT-M\xdcNCHEN-0042") == "PLT-MÜNCHEN-0042"
    for raw in (b"\x80\x81\xfe\xff", b"PLT-\xa0\xa1", bytes(range(128, 256))):
        decoded = decode_payload(raw)
        assert "�" not in decoded
        assert decoded.encode("latin-1") == raw  # lossless


def test_latin1_dm_label_decodes_to_manifest_matchable_payload() -> None:
    """Finding 14 end-to-end: a spec-compliant DM printer emitting Latin-1
    produces a symbol whose decoded payload equals the strict-UTF-8
    manifest string — the headline trial number stops deflating."""
    from palletscan._compat import import_pylibdmtx

    pylibdmtx = import_pylibdmtx()
    expected = "PLT-MÜNCHEN-0042"
    encoded = pylibdmtx.encode(expected.encode("latin-1"))
    img = np.frombuffer(encoded.pixels, np.uint8).reshape(
        encoded.height, encoded.width, 3
    )[:, :, 0]
    decodes = PylibdmtxDecoder().decode(np.ascontiguousarray(img), timeout_ms=2000)
    assert [r.payload for r in decodes] == [expected]
