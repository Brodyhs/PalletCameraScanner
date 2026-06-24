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


def test_choose_format_falls_back_to_any_y8_when_target_res_has_none() -> None:
    # No Y8 at 1920x1080 -> prefer Y8 over exact resolution (first Y8 = idx 6).
    assert choose_format(_FORMATS, 1920, 1080) == 6


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
