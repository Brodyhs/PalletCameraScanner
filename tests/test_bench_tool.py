"""bench.py capture-loop disconnect handling + honest decode-rate stats, and
bench_decoders' small-n p95 (the 18-item hard workload printed max() under a
column headed "p95 ms").
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from tools import bench
from tools.bench_decoders import _p95


def test_p95_interpolates_small_samples() -> None:
    lat = [float(i) for i in range(1, 19)]  # n=18: the hard-workload size
    p95 = _p95(lat)
    assert p95 == pytest.approx(float(np.percentile(lat, 95)))
    assert p95 < max(lat)  # the old len>=20 guard silently printed max()


def test_status_reports_decodes_and_attempts_separately() -> None:
    b = bench.Bench()
    b.attempts_per_sec, b.decodes_per_sec = 50.0, 2.0
    s = b.status()
    assert s["attempts_per_sec"] == 50
    assert s["decodes_per_sec"] == 2.0
    # the tile labeled decodes/s must be driven by decodes, not attempts
    assert 'id="dec"' in bench.PAGE
    assert "dec.textContent=s.decodes_per_sec" in bench.PAGE


class _FailingCap:
    """cv2.VideoCapture stand-in whose read() always fails."""

    def __init__(self, opened: bool = True) -> None:
        self.opened = opened
        self.reads = 0
        self.release_calls = 0

    def isOpened(self) -> bool:  # noqa: N802 — cv2 naming
        return self.opened

    def read(self):  # noqa: ANN201
        self.reads += 1
        return False, None

    def release(self) -> None:
        self.release_calls += 1


def test_capture_read_failures_flip_status_and_attempt_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead camera must flip the UI status to disconnected, zero the fps
    (the LIVE dot keys off it), and attempt reopen with backoff — the old
    bare `continue` busy-spun forever behind a green dot and stale fps."""
    monkeypatch.setattr(bench, "_READ_FAIL_SLEEP_S", 0.001)
    monkeypatch.setattr(bench, "_RECONNECT_WAIT_S", 0.001)
    caps: list[_FailingCap] = []

    def fake_open(self: bench.Bench) -> _FailingCap:
        # first open succeeds (then reads fail); reopens stay closed so the
        # loop keeps reporting the disconnect instead of oscillating
        cap = _FailingCap(opened=(len(caps) == 0))
        caps.append(cap)
        return cap

    monkeypatch.setattr(bench.Bench, "_open", fake_open)
    b = bench.Bench()
    b.fps = 55.0  # pretend we had been live
    t = threading.Thread(target=b._capture_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with b.lock:
            disconnected = "disconnected" in b.camera
        if disconnected and len(caps) >= 3:
            break
        time.sleep(0.01)
    b._stop.set()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert "disconnected" in b.camera
    assert b.fps == 0.0
    assert len(caps) >= 3, "reopen was never attempted"
    assert caps[0].release_calls >= 1
