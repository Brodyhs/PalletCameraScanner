"""Watchdog: detection, backoff, reopen, zombies, escalation, runner wiring."""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Iterator

import cv2
import numpy as np
import pytest

from palletscan.app import PipelineRunner
from palletscan.config import (
    AppConfig,
    Backend,
    CameraConfig,
    RetryConfig,
    WatchdogConfig,
)
from palletscan.reliability.flaky import FlakySource
from palletscan.sources.base import FrameSource
from palletscan.sources.camera import CameraSource, build_camera_source
from palletscan.sources.devices import devices_from_names
from palletscan.sources.factory import create_source
from palletscan.sources.render import render_qr
from palletscan.sources.synthetic import SyntheticSource
from palletscan.config import SyntheticConfig
from palletscan.reliability.watchdog import WatchdogEscalation, WatchdogSource
from palletscan.types import Frame, MissEvent, PassEvent
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock

MSMF = int(cv2.CAP_MSMF)


class _FixedRng:
    """uniform() == 1.0: backoff delays become exactly base * 2^n (capped)."""

    def uniform(self, a: float, b: float) -> float:
        return 1.0


class _RecordingSleeper:
    """Injected backoff wait: records delays, never blocks, can advance an
    injected clock so max_outage_s sees time passing."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.delays: list[float] = []
        self._clock = clock

    def __call__(self, delay: float) -> bool:
        self.delays.append(delay)
        if self._clock is not None:
            self._clock.advance(delay)
        return False  # "not stopping"


def _wcfg(**kw) -> WatchdogConfig:
    defaults = dict(
        stall_timeout_s=60.0,  # tests use the error fast path unless stated
        retry=RetryConfig(base_s=0.5, cap_s=15.0),
    )
    defaults.update(kw)
    return WatchdogConfig(**defaults)


def _cam_cfg(**kw) -> CameraConfig:
    defaults = dict(
        id="wd-cam",
        name="See3CAM_24CUG",
        backend=Backend.MSMF,
        connect_verify_s=0.0,
    )
    defaults.update(kw)
    return CameraConfig(**defaults)


def _lister():
    return devices_from_names(["See3CAM_24CUG"], MSMF)


def _camera(
    captures: list, clock: FakeClock, **cfg_kw
) -> tuple[CameraSource, FakeCaptureFactory]:
    factory = FakeCaptureFactory(
        captures=captures,
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0),
    )
    src = CameraSource(
        _cam_cfg(**cfg_kw),
        capture_factory=factory,
        device_lister=_lister,
        clock=clock,
    )
    return src, factory


def _cap(clock: FakeClock, script: list, **kw) -> FakeCapture:
    return FakeCapture(read_script=script, clock=clock, real_fps=30.0, **kw)


# -- wiring ---------------------------------------------------------------------


def test_rejects_non_reopenable_at_wiring() -> None:
    class NoReopen(FrameSource):
        @property
        def source_id(self) -> str:
            return "x"

        def frames(self) -> Iterator[Frame]:  # pragma: no cover
            return iter(())

    with pytest.raises(TypeError, match="not Reopenable"):
        WatchdogSource(NoReopen(), _wcfg())


def test_frames_single_use_and_delegated_properties() -> None:
    clock = FakeClock()
    src, _ = _camera([_cap(clock, [])], clock, fps=30.0)
    wd = WatchdogSource(src, _wcfg())
    assert wd.source_id == "wd-cam"
    assert wd.nominal_fps == 30.0
    assert wd.live is True
    it = wd.frames()
    next(it)
    with pytest.raises(RuntimeError, match="single-use"):
        next(wd.frames())
    wd.close()
    assert (wd.stalls_detected, wd.reconnects) == (0, 0)
    assert (wd.reopen_failures, wd.zombie_readers) == (0, 0)


# -- detection -------------------------------------------------------------------


def test_detects_stall_on_flaky_synthetic_source() -> None:
    """flaky.py's promised fixture: a stalled synthetic stream behind a
    5-line reopenable shim proves detection without any camera code."""

    class ReopenableFlaky(FrameSource):
        def __init__(self) -> None:
            self.opens = 0
            self.inner = self._make()

        def _make(self) -> FlakySource:
            self.opens += 1
            synth = SyntheticSource(
                SyntheticConfig(width=320, height=180, num_passes=1, seed=3),
                tail_s=0.5,
            )
            # Only the first connection stalls (for 30 s — far beyond the
            # test; the abandoned reader sleeps it out as a daemon).
            stall_at = 3 if self.opens == 1 else None
            return FlakySource(synth, stall_at=stall_at, stall_s=30.0)

        @property
        def source_id(self) -> str:
            return self.inner.source_id

        @property
        def live(self) -> bool:
            return True

        def frames(self) -> Iterator[Frame]:
            return self.inner.frames()

        def reopen(self) -> None:
            self.inner = self._make()

        def close(self) -> None:
            self.inner.close()

    shim = ReopenableFlaky()
    wd = WatchdogSource(
        shim,
        _wcfg(stall_timeout_s=0.1, retry=RetryConfig(base_s=0.01, cap_s=0.02)),
        join_timeout_s=0.05,  # the sleeping reader can't join; don't wait
    )
    got = list(itertools.islice(wd.frames(), 10))
    wd.close()
    assert len(got) == 10  # frames on both sides of the stall
    assert wd.stalls_detected == 1
    assert wd.reconnects == 1
    assert wd.zombie_readers == 1  # time.sleep ignores close(); abandoned
    assert shim.opens == 2


def test_reader_exception_is_detected_without_stall_wait() -> None:
    clock = FakeClock()
    src, factory = _camera(
        [_cap(clock, ["ok", RuntimeError("usb reset")])], clock
    )
    sleeper = _RecordingSleeper()
    wd = WatchdogSource(src, _wcfg(), rng=_FixedRng(), sleeper=sleeper)
    started = time.monotonic()
    got = list(itertools.islice(wd.frames(), 3))
    elapsed = time.monotonic() - started
    wd.close()
    assert len(got) == 3
    assert wd.stalls_detected == 0  # fast path, not the 60 s stall timeout
    assert wd.reconnects == 1
    assert elapsed < 5.0
    assert len(factory.created) == 2


# -- backoff ---------------------------------------------------------------------


def test_backoff_sequence_doubles_and_caps_with_reset_on_frame() -> None:
    clock = FakeClock()
    # First capture dies; six reopens fail; the next succeeds, streams one
    # frame, then dies again -> the second recovery starts back at base.
    captures: list = [_cap(clock, ["ok", RuntimeError("die 1")])]
    captures += [FakeCapture(opened=False) for _ in range(6)]
    captures += [_cap(clock, ["ok", RuntimeError("die 2")])]
    src, factory = _camera(captures, clock)
    sleeper = _RecordingSleeper()
    wd = WatchdogSource(src, _wcfg(), rng=_FixedRng(), sleeper=sleeper)
    got = list(itertools.islice(wd.frames(), 3))
    wd.close()
    assert len(got) == 3
    # attempt n waits min(15, 0.5 * 2^(n-1)); counter resets only after a
    # frame actually flowed.
    assert sleeper.delays[:7] == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0]
    assert sleeper.delays[7] == 0.5  # reset-on-frame
    assert wd.reopen_failures == 6
    assert wd.reconnects == 2


def test_backoff_jitter_stays_within_bounds() -> None:
    clock = FakeClock()
    captures: list = [_cap(clock, [RuntimeError("die")])]
    captures += [FakeCapture(opened=False) for _ in range(5)]
    src, _ = _camera(captures, clock)
    sleeper = _RecordingSleeper()
    wd = WatchdogSource(src, _wcfg(), sleeper=sleeper)  # real Random
    got = list(itertools.islice(wd.frames(), 1))
    wd.close()
    assert len(got) == 1
    nominal = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
    assert len(sleeper.delays) == 6
    for delay, nom in zip(sleeper.delays, nominal):
        assert 0.5 * nom <= delay <= min(15.0, 1.5 * nom) + 1e-9


# -- recovery semantics ------------------------------------------------------------


def test_reopen_reenumerates_shuffled_indexes_and_reapplies_settings() -> None:
    clock = FakeClock()
    devices = {"names": ["See3CAM_24CUG", "Other"]}
    lister_calls: list[int] = []

    def lister():
        lister_calls.append(1)
        return devices_from_names(devices["names"], MSMF)

    factory = FakeCaptureFactory(
        captures=[_cap(clock, ["ok", RuntimeError("unplugged")])],
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0),
    )
    cfg = _cam_cfg(
        settings={"exposure_auto": False, "exposure": -6.0, "gain": 10.0}
    )
    src = CameraSource(
        cfg, capture_factory=factory, device_lister=lister, clock=clock
    )
    wd = WatchdogSource(src, _wcfg(), rng=_FixedRng(), sleeper=_RecordingSleeper())
    devices["names"] = ["Other", "See3CAM_24CUG"]  # replug shuffled the bus
    got = list(itertools.islice(wd.frames(), 3))
    wd.close()
    assert len(got) == 3
    assert len(lister_calls) == 2  # re-enumerated by name on reconnect
    assert [c[0] for c in factory.calls] == [0, 1]  # followed the name
    replug = factory.created[1]
    assert replug.sets_for(cv2.CAP_PROP_EXPOSURE) == [-6.0]  # spec §5 re-apply
    assert replug.sets_for(cv2.CAP_PROP_GAIN) == [10.0]
    assert replug.sets_for(cv2.CAP_PROP_AUTO_EXPOSURE) == [0.25]


def test_stale_generation_frames_are_discarded() -> None:
    clock = FakeClock()
    dark = lambda cap: np.full((24, 32), 10, np.uint8)  # noqa: E731
    bright = lambda cap: np.full((24, 32), 200, np.uint8)  # noqa: E731
    wedged = _cap(clock, ["ok", "ok", "zombie"], frame_factory=dark)
    src, factory = _camera(
        [wedged],
        clock,
    )
    factory._default = lambda i, b: FakeCapture(  # recovered device is bright
        clock=clock, real_fps=30.0, frame_factory=bright
    )
    wd = WatchdogSource(
        src,
        _wcfg(stall_timeout_s=0.1, retry=RetryConfig(base_s=0.01, cap_s=0.02)),
        join_timeout_s=0.05,
    )
    it = wd.frames()
    first = [next(it), next(it)]  # dark frames, then the read wedges
    assert all(int(f.image[0, 0]) == 10 for f in first)
    # Recovery happens on the next pull; the wedged reader is abandoned.
    after = next(it)
    assert wd.zombie_readers == 1
    assert wd.reconnects == 1
    # Wake the zombie: it delivers one last stale frame, which the
    # generation token must discard — consumers only ever see bright.
    wedged.zombie_escape.set()
    more = [after] + list(itertools.islice(it, 5))
    wd.close()
    assert all(int(f.image[0, 0]) == 200 for f in more)
    # The woken zombie must also never touch the shared frame_index (its
    # capture was replaced): consumer-visible indexes stay consecutive.
    indexes = [f.frame_index for f in first + more]
    assert indexes == list(range(len(indexes)))


def test_close_racing_reopen_does_not_resurrect() -> None:
    """A close() landing while reopen() is completing must not spawn a
    post-shutdown reader or leave the reopened capture open."""
    clock = FakeClock()
    src, factory = _camera([_cap(clock, ["ok", RuntimeError("die")])], clock)

    class CloseDuringReopen(FrameSource):
        def __init__(self, inner: CameraSource) -> None:
            self.inner = inner
            self.wd: WatchdogSource | None = None

        @property
        def source_id(self) -> str:
            return self.inner.source_id

        @property
        def live(self) -> bool:
            return True

        def frames(self) -> Iterator[Frame]:
            return self.inner.frames()

        def reopen(self) -> None:
            self.inner.reopen()
            assert self.wd is not None
            self.wd.close()  # shutdown lands just as the reopen completes

        def close(self) -> None:
            self.inner.close()

    shim = CloseDuringReopen(src)
    wd = WatchdogSource(shim, _wcfg(), rng=_FixedRng(), sleeper=_RecordingSleeper())
    shim.wd = wd
    got = list(wd.frames())  # must terminate, not stream from a resurrected cap
    assert len(got) == 1
    assert wd.reconnects == 0  # the raced reopen never counted as a reconnect
    reopened = factory.created[-1]
    assert reopened.release_calls >= 1  # not leaked past shutdown


def test_close_during_backoff_exits_promptly() -> None:
    clock = FakeClock()
    src, _ = _camera([_cap(clock, ["ok", RuntimeError("die")])], clock)
    # Real sleeper with a 30 s backoff: close() must interrupt it.
    wd = WatchdogSource(
        src, _wcfg(retry=RetryConfig(base_s=30.0, cap_s=60.0))
    )
    got: list[Frame] = []
    t = threading.Thread(target=lambda: got.extend(wd.frames()), daemon=True)
    t.start()
    for _ in range(400):
        if got:
            break
        time.sleep(0.005)
    assert got  # one frame flowed; the wrapper is now in backoff
    started = time.monotonic()
    wd.close()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert time.monotonic() - started < 2.0


# -- escalation ---------------------------------------------------------------------


def test_zombie_cap_escalates() -> None:
    clock = FakeClock()
    wedged1 = _cap(clock, ["ok", "zombie"])
    wedged2 = _cap(clock, ["ok", "zombie"])
    src, factory = _camera([wedged1, wedged2], clock)
    wd = WatchdogSource(
        src,
        _wcfg(
            stall_timeout_s=0.05,
            retry=RetryConfig(base_s=0.01, cap_s=0.02),
            max_zombie_readers=1,
        ),
        join_timeout_s=0.05,
    )
    with pytest.raises(WatchdogEscalation, match="wedged"):
        # Bounded: if escalation regresses, this fails fast instead of
        # hanging the suite on an endless recovered stream.
        for _ in itertools.islice(wd.frames(), 10_000):
            pass
    assert wd.zombie_readers == 2  # second zombie breached the cap of 1
    wd.close()
    wedged1.zombie_escape.set()  # let the daemon threads exit
    wedged2.zombie_escape.set()


def test_max_outage_escalates_on_injected_clock() -> None:
    clock = FakeClock()
    captures: list = [_cap(clock, ["ok", RuntimeError("die")])]
    src, factory = _camera(captures, clock)
    factory._default = lambda i, b: FakeCapture(opened=False)  # never recovers
    sleeper = _RecordingSleeper(clock=clock)  # backoff advances source clock
    wd = WatchdogSource(
        src,
        _wcfg(max_outage_s=10.0),
        clock=clock,
        rng=_FixedRng(),
        sleeper=sleeper,
    )
    with pytest.raises(WatchdogEscalation, match="max_outage_s"):
        for _ in wd.frames():
            pass
    wd.close()
    # 0.5+1+2+4+8 = 15.5 s of failed backoff > the 10 s valve.
    assert sum(sleeper.delays) > 10.0
    assert wd.reopen_failures >= 3


# -- factory wiring -------------------------------------------------------------------


def test_build_camera_source_wraps_in_watchdog() -> None:
    clock = FakeClock()
    cfg = AppConfig.model_validate(
        {
            "source": {"type": "camera"},
            "cameras": [
                {
                    "id": "wd-cam",
                    "name": "See3CAM_24CUG",
                    "backend": "msmf",
                    "connect_verify_s": 0.0,
                }
            ],
        }
    )
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    src = build_camera_source(
        cfg, capture_factory=factory, device_lister=_lister, clock=clock
    )
    assert isinstance(src, WatchdogSource)
    assert isinstance(src.inner, CameraSource)
    assert src.source_id == "wd-cam"
    src.close()


def test_create_source_camera_requires_configured_entry() -> None:
    cfg = AppConfig.model_validate({"source": {"type": "camera"}})
    with pytest.raises(ValueError, match="at least one"):
        create_source(cfg)


# -- runner integration -----------------------------------------------------------


def _pass_frame_factory(payload: str, idle_head: int = 10, pass_frames: int = 40):
    """Frames that stage a decodable pallet pass: idle, QR sweeping across
    the field of view, idle again (so the motion segment opens and closes)."""
    sym = render_qr(payload, px_per_module=4.0).image

    def factory(cap: FakeCapture) -> np.ndarray:
        i = cap.reads - 1
        frame = np.full((360, 640), 128, np.uint8)
        j = i - idle_head
        if 0 <= j < pass_frames:
            h, w = sym.shape
            x = 30 + 8 * j
            y = (360 - h) // 2
            frame[y : y + h, x : x + w] = sym
        return frame

    return factory


def test_runner_recovers_midrun_with_events_on_both_sides(tmp_path) -> None:
    clock = FakeClock()
    cap1 = FakeCapture(
        read_script=["ok"] * 70 + [RuntimeError("usb reset")],
        frame_factory=_pass_frame_factory("WD-PASS-A"),
        clock=clock,
        real_fps=30.0,
    )
    cap2 = FakeCapture(
        read_script=["ok"] * 80 + ["hang"],
        frame_factory=_pass_frame_factory("WD-PASS-B"),
        clock=clock,
        real_fps=30.0,
    )
    factory = FakeCaptureFactory(captures=[cap1, cap2])
    cfg = AppConfig.model_validate(
        {
            "source": {"type": "camera"},
            "cameras": [
                {
                    "id": "wd-cam",
                    "name": "See3CAM_24CUG",
                    "backend": "msmf",
                    "connect_verify_s": 0.0,
                }
            ],
            "watchdog": {
                "stall_timeout_s": 60.0,
                "retry": {"base_s": 0.01, "cap_s": 0.02},
            },
            "sinks": {
                "console": {"enabled": False},
                "jsonl": {"enabled": True, "path": str(tmp_path / "e.jsonl")},
                "sqlite": {"enabled": False},
            },
            "evidence": {"dir": str(tmp_path / "evidence")},
        }
    )
    source = build_camera_source(
        cfg, capture_factory=factory, device_lister=_lister, clock=clock
    )
    runner = PipelineRunner.from_config(cfg, source=source)
    results: list = []
    t = threading.Thread(target=lambda: results.append(runner.run()), daemon=True)
    t.start()
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        passes = [e for e in runner.collected_events if isinstance(e, PassEvent)]
        if len(passes) >= 2:
            break
        time.sleep(0.02)
    runner.stop()
    t.join(timeout=10.0)
    assert not t.is_alive()
    assert results, "runner.run() raised instead of absorbing the outage"
    summary = results[0]

    passes = [e for e in runner.collected_events if isinstance(e, PassEvent)]
    misses = [e for e in runner.collected_events if isinstance(e, MissEvent)]
    assert {p.payload for p in passes} == {"WD-PASS-A", "WD-PASS-B"}
    assert misses == []  # both segments fully decoded; nothing unaccounted
    assert isinstance(source, WatchdogSource) and source.reconnects == 1
    snap = summary.metrics
    assert snap is not None
    assert snap["source"]["reconnects"] == 1
    assert snap["source"]["zombie_readers"] == 0
    assert summary.frames > 0


def test_real_clock_recovery_well_under_the_10s_gate() -> None:
    clock = FakeClock()
    cap1 = _cap(clock, ["ok", "ok", "hang"])
    src, _ = _camera([cap1], clock)
    wd = WatchdogSource(
        src, _wcfg(stall_timeout_s=0.2, retry=RetryConfig(base_s=0.05, cap_s=0.1))
    )
    it = wd.frames()
    assert next(it) is not None
    assert next(it) is not None
    stalled_at = time.monotonic()  # the next read hangs in the driver
    recovered = next(it)  # detection + reopen must hand back a frame
    recovery_s = time.monotonic() - stalled_at
    wd.close()
    assert recovered is not None
    assert wd.stalls_detected == 1
    assert wd.reconnects == 1
    assert wd.zombie_readers == 0  # release() unblocked the hung read
    assert recovery_s < 5.0, f"recovery took {recovery_s:.2f}s (gate is 10s)"
