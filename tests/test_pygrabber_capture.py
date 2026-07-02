"""Unit tests for the pygrabber mono-capture backend's pure logic.

The DirectShow graph itself needs COM + hardware (covered by the live
tools/mono_camerasource_check.py bring-up check); here we lock down the two
camera-specific pieces that were the actual failure source: the Y8/Y12 GUID
table patch (pygrabber KeyErrors without it) and the Y8-preferring format pick.
"""

from __future__ import annotations

import pytest

from palletscan.sources.pygrabber_capture import (
    _MONO_SUBTYPES,
    choose_format,
    patch_pygrabber_subtypes,
)

_FORMATS = [
    {"index": 0, "media_type_str": "Y12 ", "width": 2064, "height": 1552},
    {"index": 2, "media_type_str": "Y12 ", "width": 1920, "height": 1080},
    {"index": 6, "media_type_str": "Y8  ", "width": 2064, "height": 1552},
    {"index": 10, "media_type_str": "Y8  ", "width": 640, "height": 480},
]


def test_choose_format_prefers_y8_at_target_resolution() -> None:
    assert choose_format(_FORMATS, 2064, 1552) == 6


def test_choose_format_exact_resolution_outranks_y8_preference() -> None:
    # 1920x1080 is offered ONLY in Y12. Opening "any Y8" instead would stream
    # a different geometry, and CameraSource's first-frame shape gate then
    # rejects every frame -> infinite watchdog reconnect loop (REVIEW finding
    # 13; this replaces the old assertion that pinned the any-Y8-first bug).
    assert choose_format(_FORMATS, 1920, 1080) == 2


def test_choose_format_mono_at_res_outranks_color_at_res() -> None:
    formats = [
        {"index": 1, "media_type_str": "YUY2", "width": 1920, "height": 1080},
        {"index": 2, "media_type_str": "Y12 ", "width": 1920, "height": 1080},
    ]
    assert choose_format(formats, 1920, 1080) == 2


def test_choose_format_any_y8_only_when_target_res_absent_entirely() -> None:
    # 800x600 exists in no format at all -> the Y8 geometry fallback engages.
    assert choose_format(_FORMATS, 800, 600) == 6


def test_choose_format_uses_target_res_when_no_y8_exists() -> None:
    y12_only = [f for f in _FORMATS if f["media_type_str"].strip() == "Y12"]
    assert choose_format(y12_only, 1920, 1080) == 2


def test_choose_format_none_when_nothing_matches() -> None:
    assert choose_format([], 2064, 1552) is None
    y12_only = [f for f in _FORMATS if f["media_type_str"].strip() == "Y12"]
    # prefer_y8 with a res that doesn't exist and no Y8 anywhere -> None
    assert choose_format(y12_only, 800, 600) is None


def test_choose_format_no_dims_picks_first_y8() -> None:
    assert choose_format(_FORMATS, None, None) == 6
    # ...but with neither a Y8 preference nor dims there is nothing to go on.
    assert choose_format(_FORMATS, None, None, prefer_y8=False) is None


# -- per-capability framerate preference (REVIEW finding 8: fps honesty) ------

_FPS_FORMATS = [
    # pygrabber swaps min/max (min_framerate = 1e7/MinFrameInterval = the
    # HIGHEST fps); both orders must be tolerated.
    {"index": 0, "media_type_str": "Y8  ", "width": 64, "height": 48,
     "min_framerate": 72.0, "max_framerate": 72.0},
    {"index": 1, "media_type_str": "Y8  ", "width": 64, "height": 48,
     "min_framerate": 30.0, "max_framerate": 30.0},
    {"index": 2, "media_type_str": "Y8  ", "width": 64, "height": 48,
     "min_framerate": 120.0, "max_framerate": 90.0},
]


def test_choose_format_prefers_capability_containing_requested_fps() -> None:
    assert choose_format(_FPS_FORMATS, 64, 48, fps=30.0) == 1
    assert choose_format(_FPS_FORMATS, 64, 48, fps=72.0) == 0
    # swapped min/max range 90..120 still matches a request inside it
    assert choose_format(_FPS_FORMATS, 64, 48, fps=100.0) == 2


def test_choose_format_fps_is_a_preference_not_a_filter() -> None:
    # No capability offers 999 fps -> fall back to plain tier order.
    assert choose_format(_FPS_FORMATS, 64, 48, fps=999.0) == 0
    # No fps request -> plain tier order (arrival-day table has no rate keys).
    assert choose_format(_FPS_FORMATS, 64, 48) == 0
    assert choose_format(_FORMATS, 2064, 1552, fps=72.0) == 6


def test_patch_adds_mono_guids_idempotently_without_clobbering() -> None:
    ids = pytest.importorskip("pygrabber.dshow_ids")
    yuy2 = "{32595559-0000-0010-8000-00AA00389B71}"  # pygrabber's own entry
    assert ids.subtypes.get(yuy2) == "YUY2"

    patch_pygrabber_subtypes()
    for guid, name in _MONO_SUBTYPES.items():
        assert ids.subtypes[guid] == name
    assert ids.subtypes[yuy2] == "YUY2"  # existing entries untouched

    # idempotent: a second call changes nothing
    snapshot = dict(ids.subtypes)
    patch_pygrabber_subtypes()
    assert ids.subtypes == snapshot


def test_extract_mono_luma_by_depth() -> None:
    """The BufferCB luma extractor must pick the right bytes for each pin depth
    (the split/interlaced-image bug was reading a 2-byte buffer as 1-byte, and a
    later miss took byte 1 - the neutral 0x80 filler - instead of byte 0)."""
    import numpy as np

    from palletscan.sources.pygrabber_capture import _extract_mono_luma

    w, h = 4, 3
    px = w * h

    # Y8: one byte per pixel -> (h, w) as-is.
    out = _extract_mono_luma(np.arange(px, dtype=np.uint8), w, h)
    assert out.shape == (h, w) and out[0, 0] == 0 and out[h - 1, w - 1] == px - 1

    # Packed 2-byte (this 37CUGM): luma in byte 0, neutral 0x80 filler in byte 1.
    flat2 = np.empty(px * 2, np.uint8)
    flat2[0::2] = np.arange(px)     # byte 0: the real luma ramp
    flat2[1::2] = 0x80              # byte 1: constant filler
    out2 = _extract_mono_luma(flat2, w, h)
    assert out2.shape == (h, w)
    # MUST be the luma ramp (byte 0), NOT the constant 0x80 (byte 1).
    assert np.array_equal(out2.ravel(), np.arange(px, dtype=np.uint8))

    # RGB24: three bytes per pixel -> (h, w, 3) for _on_frame's downstream slice.
    assert _extract_mono_luma(np.zeros(px * 3, np.uint8), w, h).shape == (h, w, 3)

    # Buffer shorter than one plane -> None (skip, never over-read).
    assert _extract_mono_luma(np.zeros(px - 1, np.uint8), w, h) is None
