"""CameraSource: connect/reopen, ts anchoring, gray ingest, failure paths."""

from __future__ import annotations

import itertools
import logging
import threading

import cv2
import numpy as np
import pytest

from palletscan.config import Backend, CameraConfig, CameraSettings
from palletscan.sources.camera import (
    CameraConnectError,
    CameraReadError,
    CameraSource,
)
from palletscan.sources.devices import devices_from_names
from palletscan.sources.video import to_gray
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock

MSMF = int(cv2.CAP_MSMF)
DSHOW = int(cv2.CAP_DSHOW)


def _cfg(**kw) -> CameraConfig:
    defaults = dict(
        id="cam-test",
        name="See3CAM_24CUG",
        backend=Backend.MSMF,
        connect_verify_s=0.0,
    )
    defaults.update(kw)
    return CameraConfig(**defaults)


def _lister(*names: str, backend: int = MSMF, counter: list | None = None):
    def lister():
        if counter is not None:
            counter.append(1)
        return devices_from_names(list(names) or ["See3CAM_24CUG"], backend)

    return lister


def _source(
    cfg: CameraConfig | None = None,
    *,
    factory: FakeCaptureFactory | None = None,
    clock: FakeClock | None = None,
    **lister_kw,
) -> tuple[CameraSource, FakeCaptureFactory, FakeClock]:
    clock = clock or FakeClock()
    factory = factory or FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    src = CameraSource(
        cfg or _cfg(),
        capture_factory=factory,
        device_lister=_lister(**lister_kw),
        clock=clock,
    )
    return src, factory, clock


# -- fail-fast construction ------------------------------------------------------


def test_fail_fast_nothing_enumerated_no_fallback() -> None:
    with pytest.raises(CameraConnectError, match="no devices enumerated"):
        CameraSource(
            _cfg(),
            capture_factory=FakeCaptureFactory(),
            device_lister=lambda: [],
        )


def test_fail_fast_named_device_missing() -> None:
    with pytest.raises(ValueError, match="no camera matching"):
        CameraSource(
            _cfg(name="See3CAM_37CUGM"),
            capture_factory=FakeCaptureFactory(),
            device_lister=_lister("FaceTime HD Camera"),
        )


def test_fail_fast_capture_does_not_open() -> None:
    dead = FakeCapture(opened=False)
    with pytest.raises(CameraConnectError, match="did not open"):
        CameraSource(
            _cfg(),
            capture_factory=FakeCaptureFactory(captures=[dead]),
            device_lister=_lister(),
        )
    assert dead.release_calls == 1  # no leaked handle


# -- connect ----------------------------------------------------------------------


def test_connect_applies_mode_then_settings_in_order() -> None:
    cfg = _cfg(
        fourcc="UYVY",
        width=1920,
        height=1200,
        fps=120.0,
        settings=CameraSettings(exposure_auto=False, exposure=-6.0, gain=10.0),
    )
    src, factory, _ = _source(cfg)
    cap = factory.created[0]
    assert [p for p, _ in cap.set_calls] == [
        cv2.CAP_PROP_FOURCC,
        cv2.CAP_PROP_FRAME_WIDTH,
        cv2.CAP_PROP_FRAME_HEIGHT,
        cv2.CAP_PROP_FPS,
        cv2.CAP_PROP_CONVERT_RGB,
        cv2.CAP_PROP_BUFFERSIZE,
        cv2.CAP_PROP_AUTO_EXPOSURE,
        cv2.CAP_PROP_EXPOSURE,
        cv2.CAP_PROP_GAIN,
    ]
    assert cap.sets_for(cv2.CAP_PROP_AUTO_EXPOSURE) == [0.25]  # MSMF manual
    src.close()


def test_resolves_by_name_with_auto_backend_from_enumeration() -> None:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    src = CameraSource(
        _cfg(backend=Backend.AUTO, name="37cugm"),
        capture_factory=factory,
        device_lister=_lister("See3CAM_24CUG", "See3CAM_37CUGM", backend=DSHOW),
        clock=clock,
    )
    assert factory.calls == [(1, DSHOW)]  # matched index, enumeration backend
    src.close()


def test_explicit_backend_overrides_enumeration_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="palletscan.sources.camera"):
        src, factory, _ = _source(_cfg(backend=Backend.DSHOW), backend=MSMF)
    assert factory.calls[0][1] == DSHOW
    assert any("differs from the enumeration backend" in r.message for r in caplog.records)
    src.close()


def test_fallback_index_when_platform_gives_no_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    with caplog.at_level(logging.WARNING, logger="palletscan.sources.camera"):
        src = CameraSource(
            _cfg(fallback_index=2),
            capture_factory=factory,
            device_lister=lambda: [],
            clock=clock,
        )
    assert factory.calls == [(2, MSMF)]
    assert any("falling back to bare index" in r.message for r in caplog.records)
    src.close()


def test_connect_verify_warns_below_tolerance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    slow = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=40.0)
    )
    with caplog.at_level(logging.WARNING, logger="palletscan.sources.camera"):
        src, factory, _ = _source(
            _cfg(fps=120.0, connect_verify_s=1.0), factory=slow, clock=clock
        )
    assert any("below" in r.message for r in caplog.records)
    assert factory.created[0].reads > 0
    src.close()
    # connect_verify_s=0 disables the sample entirely.
    src2, factory2, _ = _source(_cfg(fps=120.0, connect_verify_s=0.0))
    assert factory2.created[0].reads == 0
    src2.close()


# -- streaming ----------------------------------------------------------------------


def test_frames_gray_ts_and_index() -> None:
    src, _, _ = _source()
    frames = list(itertools.islice(src.frames(), 3))
    src.close()
    assert [f.frame_index for f in frames] == [0, 1, 2]
    assert all(f.image.ndim == 2 and f.image.dtype == np.uint8 for f in frames)
    assert all(f.source_id == "cam-test" for f in frames)
    # ts = clock - t0, sampled after each read (one frame interval apart).
    assert frames[0].ts == pytest.approx(1 / 30.0)
    assert frames[2].ts - frames[1].ts == pytest.approx(1 / 30.0)
    assert src.live is True
    assert src.nominal_fps is None  # fps not configured in _cfg()


def test_ts_anchor_and_frame_index_survive_reopen() -> None:
    src, factory, clock = _source()
    it = src.frames()
    first = [next(it), next(it)]
    src.close()
    clock.advance(5.0)  # the outage
    src.reopen()
    resumed = next(src.frames())
    src.close()
    assert [f.frame_index for f in first] == [0, 1]
    assert resumed.frame_index == 2  # continuity across reopen
    # ts never re-anchors: the outage shows as a real source-time gap.
    assert resumed.ts - first[1].ts == pytest.approx(5.0 + 1 / 30.0)
    assert len(factory.created) == 2


def test_reopen_reenumerates_and_reapplies_settings() -> None:
    calls: list[int] = []
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    cfg = _cfg(settings=CameraSettings(exposure_auto=False, exposure=-6.0))

    shuffled = {"devices": ["See3CAM_24CUG", "Other"]}

    def lister():
        calls.append(1)
        return devices_from_names(shuffled["devices"], MSMF)

    src = CameraSource(
        cfg, capture_factory=factory, device_lister=lister, clock=clock
    )
    shuffled["devices"] = ["Other", "See3CAM_24CUG"]  # replug shuffled indexes
    src.reopen()
    assert len(calls) == 2  # re-enumerated on reopen
    assert [c[0] for c in factory.calls] == [0, 1]  # followed the name, not index
    # Persisted settings re-applied on the fresh capture (spec §5).
    assert factory.created[1].sets_for(cv2.CAP_PROP_EXPOSURE) == [-6.0]
    assert factory.created[1].sets_for(cv2.CAP_PROP_AUTO_EXPOSURE) == [0.25]
    assert factory.created[0].release_calls == 1
    src.close()


def test_read_fail_limit_raises_and_resets_on_success() -> None:
    clock = FakeClock()
    # Interleaved failures below the limit recover...
    cap = FakeCapture(
        read_script=["ok", "fail", "fail", "ok", "fail", "ok"],
        clock=clock,
        real_fps=30.0,
    )
    src = CameraSource(
        _cfg(read_fail_limit=3),
        capture_factory=FakeCaptureFactory(captures=[cap]),
        device_lister=_lister(),
        clock=clock,
    )
    assert len(list(itertools.islice(src.frames(), 4))) == 4
    src.close()
    # ...but the limit of consecutive failures raises for the watchdog.
    cap2 = FakeCapture(
        read_script=["ok", "fail", "fail", "fail"], clock=clock, real_fps=30.0
    )
    src2 = CameraSource(
        _cfg(read_fail_limit=3),
        capture_factory=FakeCaptureFactory(captures=[cap2]),
        device_lister=_lister(),
        clock=clock,
    )
    with pytest.raises(CameraReadError, match="3 consecutive"):
        list(src2.frames())
    src2.close()


def test_close_is_idempotent_and_unblocks_hung_read() -> None:
    clock = FakeClock()
    cap = FakeCapture(read_script=["ok", "hang"], clock=clock, real_fps=30.0)
    src = CameraSource(
        _cfg(),
        capture_factory=FakeCaptureFactory(captures=[cap]),
        device_lister=_lister(),
        clock=clock,
    )
    got: list = []
    t = threading.Thread(target=lambda: got.extend(src.frames()), daemon=True)
    t.start()
    for _ in range(200):
        if got:
            break
        threading.Event().wait(0.005)
    assert len(got) == 1  # consumer is now stuck in the hung read
    src.close()  # release() unblocks it
    t.join(timeout=2.0)
    assert not t.is_alive()
    src.close()  # idempotent
    assert cap.release_calls == 1


def test_close_unblocks_read_hung_inside_connect_verify() -> None:
    """The connect sequence (incl. the connect-verify read loop) runs on the
    watchdog's consumer thread; a wedged driver there must still be
    releasable by close() or shutdown/escalation can never happen."""
    clock = FakeClock()
    healthy = FakeCapture(clock=clock, real_fps=30.0)
    wedged = FakeCapture(read_script=["hang"], clock=clock, real_fps=30.0)
    factory = FakeCaptureFactory(captures=[healthy, wedged])
    src = CameraSource(
        _cfg(connect_verify_s=1.0),
        capture_factory=factory,
        device_lister=_lister(),
        clock=clock,
    )
    src.close()
    errors: list[BaseException] = []

    def reopen_into_wedge() -> None:
        try:
            src.reopen()  # hangs in the warmup read of connect-verify
        except CameraConnectError as exc:
            errors.append(exc)

    t = threading.Thread(target=reopen_into_wedge, daemon=True)
    t.start()
    for _ in range(400):
        if wedged.reads >= 1:
            break
        threading.Event().wait(0.005)
    assert wedged.reads >= 1  # reopen is now stuck inside the driver
    src.close()  # must release the published in-flight capture
    t.join(timeout=2.0)
    assert not t.is_alive(), "close() failed to unblock the connect sequence"
    assert wedged.release_calls == 1
    assert errors and "closed during connect" in str(errors[0])


def test_connect_warns_but_continues_on_ignored_controls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run-path policy: frames at slightly-wrong exposure beat no frames."""
    from palletscan.config import Backend as B
    from tests.camera_fakes import avfoundation_hooks

    clock = FakeClock()
    hooks = avfoundation_hooks()
    hooks[cv2.CAP_PROP_BUFFERSIZE] = lambda v: None  # backend ignores it too
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(hooks=hooks, clock=clock, real_fps=30.0)
    )
    cfg = _cfg(
        backend=B.AVFOUNDATION,
        settings={"exposure_auto": False, "exposure": -6.0, "gain": 5.0},
    )
    with caplog.at_level(logging.WARNING, logger="palletscan.sources.camera"):
        src = CameraSource(
            cfg,
            capture_factory=factory,
            device_lister=_lister(backend=int(cv2.CAP_AVFOUNDATION)),
            clock=clock,
        )
    warning = next(
        r for r in caplog.records if "unverified after connect" in r.message
    )
    assert "exposure" in warning.message
    assert "buffersize" not in warning.message  # informational: never warned
    assert len(list(itertools.islice(src.frames(), 2))) == 2  # still streams
    src.close()


def test_uyvy_raw_frames_take_luma_from_channel_1() -> None:
    """UYVY interleaves U Y V Y: with CONVERT_RGB=0 the luma plane is
    channel 1 (YUY2's is channel 0)."""
    clock = FakeClock()

    def packed(cap: FakeCapture) -> np.ndarray:
        frame = np.empty((24, 32, 2), np.uint8)
        frame[:, :, 0] = 128  # chroma
        frame[:, :, 1] = 207  # luma
        return frame

    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            clock=clock, real_fps=30.0, frame_factory=packed
        )
    )
    src = CameraSource(
        _cfg(fourcc="UYVY", convert_rgb=False),
        capture_factory=factory,
        device_lister=_lister(),
        clock=clock,
    )
    frame = next(src.frames())
    src.close()
    assert int(frame.image[0, 0]) == 207  # luma, not chroma


# -- to_gray extension ---------------------------------------------------------------


def test_to_gray_all_camera_layouts() -> None:
    u8 = np.full((4, 4), 7, np.uint8)
    assert to_gray(u8) is u8  # passthrough, no copy
    y16 = np.full((4, 4), 0x1234, np.uint16)
    out16 = to_gray(y16)
    assert out16.dtype == np.uint8 and int(out16[0, 0]) == 0x12
    hx1 = np.full((4, 4, 1), 9, np.uint8)
    assert to_gray(hx1).shape == (4, 4)
    yuyv = np.zeros((4, 4, 2), np.uint8)
    yuyv[:, :, 0] = 200  # luma plane
    yuyv[:, :, 1] = 128  # chroma
    assert int(to_gray(yuyv)[0, 0]) == 200
    bgr = np.zeros((4, 4, 3), np.uint8)
    bgr[..., 2] = 255  # pure red -> luma ~76
    assert int(to_gray(bgr)[0, 0]) == pytest.approx(76, abs=3)
