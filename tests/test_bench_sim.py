"""Automated checks of the bench's decode + motion logic via the offline
synthetic harness (tools/bench_sim.py). Skipped when zxing-cpp is absent.

These run in milliseconds and need no camera — the fast iteration loop for the
bench's algorithm. They pin the decode envelope (slow+short decodes, fast+long
blurs out), that the motion box contains a moving code, and the speed estimate.
"""

from __future__ import annotations

import pytest

pytest.importorskip("zxingcpp")

from tools.bench_sim import FRAME_W, MAX_CHANCES, MPH_TO_MPS, make_pass, run_pass

PAYLOAD = "PLT-000042"
_PX_PER_M = 5.0 / (5.0 / 1000.0)  # 5 px/module, 5 mm modules


def _pass(sym: str, ppm: float, module_mm: float, mph: float, exp_ms: float, fps: float = 55.0):
    px_per_m = ppm / (module_mm / 1000.0)
    speed_pxps = mph * MPH_TO_MPS * px_per_m
    blur_px = speed_pxps * (exp_ms / 1000.0)
    frames = make_pass(PAYLOAD, sym, ppm, speed_pxps, blur_px, fps, noise_sigma=4.0)
    return run_pass(frames, PAYLOAD)


def test_slow_short_exposure_decodes() -> None:
    # 1 mph, 4 ms, 5 mm modules: well inside the freeze budget -> reads
    r = _pass("qr", 5.0, 5.0, 1.0, 4.0)
    assert r["decoded"] and r["decode_frames"] >= 1


def test_datamatrix_slow_decodes() -> None:
    r = _pass("datamatrix", 5.0, 5.0, 1.0, 4.0)
    assert r["decoded"]


def test_motion_box_finds_the_moving_code() -> None:
    # motion is detected and boxed on (almost) every frame of the pass
    r = _pass("qr", 5.0, 5.0, 1.0, 4.0)
    assert r["box_frames"] >= 5


def test_fast_long_exposure_blurs_out() -> None:
    # 8 mph at 33 ms ~= 23 modules of blur: must NOT decode (physics, not a bug)
    r = _pass("qr", 5.0, 5.0, 8.0, 33.0)
    assert not r["decoded"]


def test_short_exposure_rescues_speed() -> None:
    # same 5 mph that fails at a long exposure decodes at ~1 ms (the whole point)
    slow_shutter = _pass("qr", 5.0, 5.0, 5.0, 16.0)
    fast_shutter = _pass("qr", 5.0, 5.0, 5.0, 1.0)
    assert not slow_shutter["decoded"]
    assert fast_shutter["decoded"]


def test_speed_estimate_tracks_truth() -> None:
    ppm, module_mm, mph, fps = 5.0, 5.0, 1.0, 55.0
    r = _pass("qr", ppm, module_mm, mph, 2.0, fps)
    true_pxpf = mph * MPH_TO_MPS * (ppm / (module_mm / 1000.0)) / fps
    assert r["measured_pxpf"] is not None
    assert 0.5 * true_pxpf <= r["measured_pxpf"] <= 1.8 * true_pxpf


def test_pass_starts_fully_off_canvas() -> None:
    # Frame 0 is the code-free motion prior: a code already mid-canvas on the
    # first frame is an unboxable (wasted) chance and skews chance accounting.
    speed_pxps = 3.0 * MPH_TO_MPS * _PX_PER_M
    frames = make_pass(PAYLOAD, "qr", 5.0, speed_pxps, 0.0, 55.0, noise_sigma=0.0)
    assert frames[0].min() > 60  # no black QR modules anywhere on frame 0


def test_slow_pass_chances_capped_and_reported() -> None:
    # 1 mph crosses in far more than MAX_CHANCES frames: the cap applies and
    # the per-pass chance count is reported, not assumed.
    r = _pass("qr", 5.0, 5.0, 1.0, 4.0)
    assert r["chances"] == MAX_CHANCES


def test_fast_pass_gets_its_full_geometric_crossing() -> None:
    # 8 mph exits the 960 px canvas quickly: chances = the full crossing (the
    # old fixed 40-frame run entered at x=0 and burned ~60% of its frames
    # after the code had already left the canvas).
    fps = 55.0
    r = _pass("qr", 5.0, 5.0, 8.0, 1.0, fps)
    pxpf = 8.0 * MPH_TO_MPS * _PX_PER_M / fps
    assert 0 < r["chances"] < MAX_CHANCES  # physics: fewer in-view frames
    # at least the on-canvas span's worth of in-view frames, every one a chance
    assert r["chances"] >= int(FRAME_W / pxpf)
