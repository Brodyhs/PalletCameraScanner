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


def test_explicit_backend_mismatch_without_fallback_is_fatal() -> None:
    """REVIEW finding 8 (repro-derived): opening a DSHOW-enumerated index
    under an explicit MSMF-style backend silently captured whatever device
    sat at that slot after a replug shifted the order — warn-and-open is
    now a hard connect error."""
    with pytest.raises(CameraConnectError, match="enumeration backend"):
        _source(_cfg(backend=Backend.DSHOW), backend=MSMF)


def test_explicit_backend_with_fallback_index_uses_pinned_index(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sanctioned escape hatch for finding 8: an explicit
    non-enumeration backend requires a pinned index, opens (index, explicit
    flag), and says loudly that name stability is forfeited."""
    with caplog.at_level(logging.WARNING, logger="palletscan.sources.camera"):
        src, factory, _ = _source(
            _cfg(backend=Backend.DSHOW, fallback_index=1), backend=MSMF
        )
    assert factory.calls[0] == (1, DSHOW)  # pinned index, explicit backend
    assert any(
        "name resolution is forfeited" in r.message for r in caplog.records
    )
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


def test_connect_reports_backend_unverifiable_controls_as_info_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run-path readback honesty: on a readback-unreliable backend
    (AVFoundation) controls the device ACCEPTED but whose readback can't
    confirm them are 'asserted but unverifiable' (INFO), NOT a 'control
    unverified after connect' failure (WARNING + mismatch). Frames still
    flow either way.

    The fake ACCEPTS each set while its readback sticks at garbage (0.0) —
    the MSMF/AVFoundation reality this bucket exists for. (It previously
    REJECTED the sets, which is the genuine-failure bucket per REVIEW
    bringup-4d95b67 — covered by the rejected-control test below.)"""
    from palletscan.config import Backend as B

    clock = FakeClock()
    hooks = {
        # accepted=True (value 'stored' as garbage 0.0), readback untrusted
        cv2.CAP_PROP_AUTO_EXPOSURE: lambda v: 0.0,
        cv2.CAP_PROP_EXPOSURE: lambda v: 0.0,
        cv2.CAP_PROP_GAIN: lambda v: 0.0,
        cv2.CAP_PROP_BUFFERSIZE: lambda v: None,  # informational: ignored
    }
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(hooks=hooks, clock=clock, real_fps=30.0)
    )
    cfg = _cfg(
        backend=B.AVFOUNDATION,
        settings={"exposure_auto": False, "exposure": -6.0, "gain": 5.0},
    )
    with caplog.at_level(logging.INFO, logger="palletscan.sources.camera"):
        src = CameraSource(
            cfg,
            capture_factory=factory,
            device_lister=_lister(backend=int(cv2.CAP_AVFOUNDATION)),
            clock=clock,
        )
    # No genuine-failure WARNING and no mismatch bump for these.
    assert not any(
        "unverified after connect" in r.message for r in caplog.records
    )
    assert src.connect_mismatches == 0
    info = next(
        r
        for r in caplog.records
        if "asserted but unverifiable" in r.message
    )
    assert "avfoundation" in info.message
    assert "exposure" in info.message
    assert "buffersize" not in info.message  # informational: never reported here
    assert len(list(itertools.islice(src.frames(), 2))) == 2  # still streams
    src.close()


def test_connect_counts_rejected_control_as_failure_even_when_unverifiable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REVIEW bringup-4d95b67: a control write the backend outright
    REJECTED (accepted=False) filed into the INFO 'asserted but
    unverifiable' bucket on readback-unreliable backends — never warned,
    never bumped connect_mismatches. Rejection is a failure on EVERY
    backend: WARNING + connect_mismatches, note says rejected."""
    from palletscan.config import Backend as B

    clock = FakeClock()
    hooks = {
        cv2.CAP_PROP_GAIN: lambda v: None,  # REJECTED by the backend
    }
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(hooks=hooks, clock=clock, real_fps=30.0)
    )
    cfg = _cfg(
        backend=B.AVFOUNDATION,  # readback-unreliable
        settings={"exposure_auto": False, "exposure": -6.0, "gain": 5.0},
    )
    with caplog.at_level(logging.INFO, logger="palletscan.sources.camera"):
        src = CameraSource(
            cfg,
            capture_factory=factory,
            device_lister=_lister(backend=int(cv2.CAP_AVFOUNDATION)),
            clock=clock,
        )
    assert src.connect_mismatches >= 1
    warning = next(
        r for r in caplog.records if "unverified after connect" in r.message
    )
    assert "gain" in warning.getMessage()
    assert "rejected" in warning.getMessage()
    # The accepted-but-unconfirmable controls stay in the INFO bucket.
    info = next(
        r for r in caplog.records if "asserted but unverifiable" in r.message
    )
    assert "gain" not in info.getMessage()
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


# -- REVIEW_SYSTEM_0c30c77 findings 3, b8 + the first-frame gates ---------------


def _packed_frame_factory(luma_channel: int):
    """HxWx2 frames: the given channel carries a bright luma plane (~200),
    the other a flat 128 chroma plane."""

    def factory(cap: FakeCapture) -> np.ndarray:
        h = int(cap.props[cv2.CAP_PROP_FRAME_HEIGHT])
        w = int(cap.props[cv2.CAP_PROP_FRAME_WIDTH])
        img = np.full((h, w, 2), 128, np.uint8)
        img[:, :, luma_channel] = 200
        return img

    return factory


def test_negotiated_fourcc_overrides_configured_luma_channel() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 3 (repro: 0 active frames vs 18 for
    the identical scene on the correct channel): a UYVY config running on
    a device that silently negotiated YUY2 read the interleaved chroma
    plane as 'grayscale' — a blind camera that emitted neither passes nor
    misses while every health signal looked good. The luma channel must
    follow the NEGOTIATED format."""
    from palletscan.sources.controls import fourcc_float

    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            # The device "accepts" the UYVY request but silently snaps to
            # the sibling packed format — probe.py's "UVC devices lie".
            hooks={cv2.CAP_PROP_FOURCC: lambda v: fourcc_float("YUY2")},
            props={cv2.CAP_PROP_FOURCC: fourcc_float("YUY2")},
            clock=clock,
            real_fps=30.0,
            frame_factory=_packed_frame_factory(0),  # YUY2 luma = channel 0
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
    assert float(frame.image.mean()) == pytest.approx(200.0, abs=1.0), (
        "the configured-UYVY channel (chroma on this device) was scanned"
    )
    assert src.connect_mismatches >= 1  # the divergence was counted


def test_unverifiable_fourcc_readback_falls_back_to_configured() -> None:
    """Finding 3: a backend whose fourcc readback is 0.0 cannot confirm
    the format; the configured value is the best remaining knowledge —
    used, warned about, and counted."""
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            # set() rejected and readback stuck at 0.0: nothing to verify.
            hooks={cv2.CAP_PROP_FOURCC: lambda v: None},
            props={cv2.CAP_PROP_FOURCC: 0.0},
            clock=clock,
            real_fps=30.0,
            frame_factory=_packed_frame_factory(1),  # UYVY luma = channel 1
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
    assert float(frame.image.mean()) == pytest.approx(200.0, abs=1.0)
    assert src.connect_mismatches >= 1


def test_unknown_packed_format_refuses_to_scan_chroma() -> None:
    """Findings 3/8 policy: 2-channel frames with NO known luma layout
    (nothing configured, readback unverifiable) must fail loudly into the
    watchdog's retry path instead of silently scanning a chroma plane."""
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            hooks={cv2.CAP_PROP_FOURCC: lambda v: None},
            props={cv2.CAP_PROP_FOURCC: 0.0},
            clock=clock,
            real_fps=30.0,
            frame_factory=_packed_frame_factory(1),
        )
    )
    src = CameraSource(
        _cfg(fourcc=None, convert_rgb=False),
        capture_factory=factory,
        device_lister=_lister(),
        clock=clock,
    )
    with pytest.raises(CameraReadError, match="luma"):
        next(src.frames())
    src.close()


def test_delivered_geometry_mismatch_fails_loudly() -> None:
    """Findings 3/8 policy: the delivered frame is the format oracle — a
    device delivering a different geometry than the locked mode corrupts
    the optics envelope and must fail the connection, not stream."""
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            # The device ignores the resolution set and keeps delivering
            # its native 640x480 — readback would lie; the frame does not.
            hooks={
                cv2.CAP_PROP_FRAME_WIDTH: lambda v: None,
                cv2.CAP_PROP_FRAME_HEIGHT: lambda v: None,
            },
            clock=clock,
            real_fps=30.0,
        )
    )
    src = CameraSource(
        _cfg(width=1920, height=1200),
        capture_factory=factory,
        device_lister=_lister(),
        clock=clock,
    )
    with pytest.raises(CameraReadError, match="1920x1200"):
        next(src.frames())
    src.close()
    # Without locked dimensions the same device streams fine.
    src2 = CameraSource(
        _cfg(),
        capture_factory=FakeCaptureFactory(
            default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
        ),
        device_lister=_lister(),
        clock=clock,
    )
    assert next(src2.frames()) is not None
    src2.close()


def test_shared_epoch_aligns_cross_camera_timestamps() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b8 (per-camera ts epochs anchored at
    construction; sequential connects skewed every cross-camera ts
    comparison): a shared epoch makes the skew zero by construction."""
    clock = FakeClock(1000.0)
    epoch = clock()  # sampled before any device opens

    def build() -> CameraSource:
        return CameraSource(
            _cfg(),
            capture_factory=FakeCaptureFactory(
                default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
            ),
            device_lister=_lister(),
            clock=clock,
            epoch=epoch,
        )

    cam_a = build()
    clock.advance(3.0)  # camA's slow connect-verify, mode apply, ...
    cam_b = build()
    frame_b = next(cam_b.frames())
    # Without the shared epoch camB's first frame would sit near ts 1/30;
    # with it, both cameras measure the same instant identically.
    assert frame_b.ts == pytest.approx(3.0 + 1 / 30.0, abs=0.01)
    cam_a.close()
    cam_b.close()


def test_epoch_wall_pairs_with_the_monotonic_anchor() -> None:
    """Finding b8/10 bridge: epoch_wall is the wall instant of ts=0 —
    computed from the paired samples, or stored verbatim when given."""
    clock = FakeClock(1000.0)
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    src = CameraSource(
        _cfg(),
        capture_factory=factory,
        device_lister=_lister(),
        clock=clock,
        wall_clock=lambda: 5000.0,
        epoch=990.0,
    )
    assert src.epoch_wall == pytest.approx(5000.0 - (1000.0 - 990.0))
    src.close()
    src2 = CameraSource(
        _cfg(),
        capture_factory=FakeCaptureFactory(
            default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
        ),
        device_lister=_lister(),
        clock=clock,
        epoch=990.0,
        epoch_wall=123.0,
    )
    assert src2.epoch_wall == 123.0
    src2.close()
