"""Automated checks of the bench's decode + motion logic via the offline
synthetic harness (tools/bench_sim.py). Skipped when zxing-cpp is absent.

These run in milliseconds and need no camera — the fast iteration loop for the
bench's algorithm. They pin the decode envelope (slow+short decodes, fast+long
blurs out), that the motion box contains a moving code, and the speed estimate.
"""

from __future__ import annotations

import pytest

pytest.importorskip("zxingcpp")

from tools.bench_sim import MPH_TO_MPS, make_pass, run_pass

PAYLOAD = "PLT-000042"


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
