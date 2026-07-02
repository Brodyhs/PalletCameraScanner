"""calibrate: list, probe+choose, focus metric, decode line, save, exit codes."""

from __future__ import annotations

import io
import re
from pathlib import Path

import cv2
import numpy as np
import pytest

from palletscan.calibrate import CalibrateOptions, focus_metric, run_calibration
from palletscan.config import AppConfig, load_config
from palletscan.selftest import SELFTEST_ASSETS
from palletscan.sources.controls import fourcc_float
from palletscan.sources.devices import DeviceInfo, IdentityInfo, devices_from_names
from palletscan.types import Symbology
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock

MSMF = int(cv2.CAP_MSMF)


def _lister(*names: str, backend: int = MSMF):
    return lambda: devices_from_names(
        list(names) or ["See3CAM_24CUG"], backend
    )


def _cfg(**cameras_kw) -> AppConfig:
    entry = {"id": "cam-color", "name": "See3CAM_24CUG", "backend": "msmf"}
    entry.update(cameras_kw)
    return AppConfig.model_validate({"cameras": [entry]})


def _see3cam_factory(clock: FakeClock) -> FakeCaptureFactory:
    """Device that sustains 120 fps only on MJPG, 30 fps uncompressed."""

    def real_fps(cap: FakeCapture) -> float:
        requested = cap.get(cv2.CAP_PROP_FPS) or 30.0
        ceiling = 120.0 if cap.get(cv2.CAP_PROP_FOURCC) == fourcc_float("MJPG") else 30.0
        return min(requested, ceiling)

    return FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=real_fps)
    )


def _run(cfg: AppConfig, opts: CalibrateOptions, factory, lister, clock):
    out = io.StringIO()
    rc = run_calibration(
        cfg,
        opts,
        capture_factory=factory,
        device_lister=lister,
        clock=clock,
        out=out,
    )
    return rc, out.getvalue()


def test_list_prints_device_table() -> None:
    rc, out = _run(
        AppConfig(),
        CalibrateOptions(list_only=True),
        FakeCaptureFactory(),
        _lister("See3CAM_24CUG", "See3CAM_37CUGM"),
        FakeClock(),
    )
    assert rc == 0
    assert "[0] See3CAM_24CUG" in out
    assert "[1] See3CAM_37CUGM" in out


def test_probe_matrix_prints_table_and_picks_sustainable_mode() -> None:
    clock = FakeClock()
    rc, out = _run(
        _cfg(),
        CalibrateOptions(seconds=1, exposure=-6.0),
        _see3cam_factory(clock),
        _lister(),
        clock,
    )
    assert rc == 0, out
    assert "UYVY 1920x1200@120" in out  # full table shown, incl. losers
    assert "CHOSEN" in out
    assert "chosen mode: MJPG 1920x1200@120" in out  # only MJPG sustained 120
    assert "exposure effect" in out and "ok" in out
    assert "fps " in out  # live metrics loop line


def test_pinned_mode_probes_single_candidate() -> None:
    clock = FakeClock()
    factory = _see3cam_factory(clock)
    rc, out = _run(
        _cfg(),
        CalibrateOptions(fourcc="MJPG", width=1920, height=1200, fps=60.0, seconds=0),
        factory,
        _lister(),
        clock,
    )
    assert rc == 0, out
    assert out.count("MJPG 1920x1200@60") >= 1
    assert "UYVY" not in out  # matrix skipped
    # seed cap + 1 probe + verification cap
    assert len(factory.created) == 3


def test_focus_metric_orders_sharp_above_blurred() -> None:
    rng = np.random.default_rng(7)
    sharp = (rng.random((120, 160)) * 255).astype(np.uint8)
    blurred = cv2.GaussianBlur(sharp, (15, 15), 5.0)
    assert focus_metric(sharp) > focus_metric(blurred) * 5


def test_live_decode_line_reports_payload(tmp_path: Path) -> None:
    clock = FakeClock()
    _, qr_path = SELFTEST_ASSETS[Symbology.QR]
    sym = cv2.imread(str(qr_path), cv2.IMREAD_GRAYSCALE)

    def qr_frame(cap: FakeCapture) -> np.ndarray:
        frame = np.full((480, 640), 255, np.uint8)
        frame[40 : 40 + sym.shape[0], 40 : 40 + sym.shape[1]] = sym
        return frame

    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            clock=clock, real_fps=30.0, frame_factory=qr_frame
        )
    )
    rc, out = _run(
        _cfg(),
        CalibrateOptions(fourcc="YUY2", width=640, height=480, fps=30.0, seconds=1),
        factory,
        _lister(),
        clock,
    )
    assert rc == 0, out
    assert "PALLETSCAN-SELFTEST-QR" in out


def test_save_upserts_locked_entry(tmp_path: Path) -> None:
    clock = FakeClock()
    config_path = tmp_path / "station.yaml"
    config_path.write_text("dedup: {window_s: 9.0}\n", encoding="utf-8")
    opts = CalibrateOptions(
        seconds=0,
        exposure=-6.0,
        gain=10.0,
        save=True,
        config_path=config_path,
    )
    rc, out = _run(_cfg(), opts, _see3cam_factory(clock), _lister(), clock)
    assert rc == 0, out
    assert "saved cameras[cam-color]" in out
    saved = load_config(config_path)
    cam = saved.cameras[0]
    assert (cam.fourcc, cam.width, cam.height, cam.fps) == ("MJPG", 1920, 1200, 120.0)
    assert cam.backend.value == "msmf"  # values and backend travel together
    assert cam.settings.exposure == -6.0 and cam.settings.exposure_auto is False
    assert cam.settings.gain == 10.0
    assert saved.dedup.window_s == 9.0  # neighbors untouched


def test_save_stamps_identity_fingerprint_leaving_policy_default(
    tmp_path: Path,
) -> None:
    """calibrate --save stamps the chosen device's device_path + vid:pid into
    identity, but leaves policy at the default 'warn' so the operator opts
    into strict deliberately. The captured fingerprint is also printed."""
    clock = FakeClock()
    config_path = tmp_path / "station.yaml"

    def lister():
        return [
            DeviceInfo(
                name="See3CAM_24CUG",
                index=0,
                backend=MSMF,
                identity=IdentityInfo(
                    friendly_name="See3CAM_24CUG",
                    device_path=r"usb#vid_2560&pid_c128&mi_00#calib",
                    vid="2560",
                    pid="c128",
                ),
            )
        ]

    opts = CalibrateOptions(
        seconds=0, exposure=-6.0, save=True, config_path=config_path
    )
    rc, out = _run(_cfg(), opts, _see3cam_factory(clock), lister, clock)
    assert rc == 0, out
    assert "captured identity fingerprint" in out
    assert "vid:pid=2560:c128" in out
    saved = load_config(config_path)
    ident = saved.cameras[0].identity
    assert ident.policy == "warn"  # operator opts into strict deliberately
    assert ident.expected_vid_pid == "2560:c128"
    assert ident.expected_device_path == r"usb#vid_2560&pid_c128&mi_00#calib"


def test_save_without_identity_leaves_fingerprint_unset(tmp_path: Path) -> None:
    """No identity available (macOS / name-only enumeration): --save still
    works, prints the 'unavailable' note, and leaves the fingerprint unset."""
    clock = FakeClock()
    config_path = tmp_path / "station.yaml"
    opts = CalibrateOptions(
        seconds=0, exposure=-6.0, save=True, config_path=config_path
    )
    rc, out = _run(_cfg(), opts, _see3cam_factory(clock), _lister(), clock)
    assert rc == 0, out
    assert "captured identity fingerprint: unavailable" in out
    ident = load_config(config_path).cameras[0].identity
    assert ident.policy == "warn"
    assert ident.expected_vid_pid is None
    assert ident.expected_device_path is None


def test_save_without_config_is_usage_error() -> None:
    clock = FakeClock()
    rc, out = _run(
        _cfg(),
        CalibrateOptions(seconds=0, save=True, config_path=None),
        _see3cam_factory(clock),
        _lister(),
        clock,
    )
    assert rc == 2
    assert "--save requires --config" in out


def test_ignored_buffersize_never_fails_calibration() -> None:
    """DSHOW/MSMF do not implement CAP_PROP_BUFFERSIZE; that best-effort
    property must not hard-fail an otherwise healthy calibration."""
    clock = FakeClock()

    def make(i: int, b: int) -> FakeCapture:
        return FakeCapture(
            hooks={cv2.CAP_PROP_BUFFERSIZE: lambda v: None},
            clock=clock,
            real_fps=lambda cap: min(cap.get(cv2.CAP_PROP_FPS) or 30.0, 120.0),
        )

    rc, out = _run(
        _cfg(),
        CalibrateOptions(seconds=0, exposure=-6.0),
        FakeCaptureFactory(default=make),
        _lister(),
        clock,
    )
    assert rc == 0, out
    assert "buffersize" in out and "info" in out
    assert "MISMATCH" not in out


def test_rejected_control_hard_fails_on_reliable_backend() -> None:
    clock = FakeClock()

    def make(i: int, b: int) -> FakeCapture:
        return FakeCapture(
            hooks={cv2.CAP_PROP_EXPOSURE: lambda v: None},  # driver ignores it
            clock=clock,
            real_fps=lambda cap: min(cap.get(cv2.CAP_PROP_FPS) or 30.0, 120.0),
        )

    rc, out = _run(
        _cfg(backend="dshow"),  # DSHOW: truthful readback -> hard-fails
        CalibrateOptions(seconds=0, exposure=-6.0),
        FakeCaptureFactory(default=make),
        _lister(backend=int(cv2.CAP_DSHOW)),
        clock,
    )
    assert rc == 1
    assert "hard on this backend" in out


def test_unverified_controls_warn_on_avfoundation() -> None:
    """Readback-unreliable backend, exposure PHYSICALLY working: unverified
    controls are an honest warning, not a hard failure. (The fixture used
    to reject the exposure set too, which also killed the exposure-EFFECT
    check — under the restored gate, REVIEW bringup-4d95b67, a dead effect
    is hard on EVERY backend, so the exposure here must actually work for
    this test to isolate the readback-warn contract.)"""
    from tests.camera_fakes import avfoundation_hooks

    clock = FakeClock()
    hooks = avfoundation_hooks()
    del hooks[cv2.CAP_PROP_EXPOSURE]  # exposure applies; readback still untrusted
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            hooks=hooks,
            clock=clock,
            real_fps=lambda cap: min(cap.get(cv2.CAP_PROP_FPS) or 30.0, 120.0),
        )
    )
    cfg = AppConfig.model_validate(
        {"cameras": [{"id": "cam-dev", "name": "FaceTime", "backend": "avfoundation"}]}
    )
    rc, out = _run(
        cfg,
        CalibrateOptions(seconds=0, exposure=-6.0),
        factory,
        lambda: devices_from_names(["FaceTime"], int(cv2.CAP_AVFOUNDATION)),
        clock,
    )
    assert rc == 0, out  # honest warning, not a hard failure
    assert "controls unverified" in out
    assert "exposure effect" in out and "NO EFFECT" not in out


def test_dead_exposure_control_hard_fails_even_on_msmf() -> None:
    """REVIEW bringup-4d95b67 (exposure-effect gate restoration): flipping
    MSMF to readback-unreliable also demoted the exposure-EFFECT gate, so a
    physically dead exposure control exited calibrate rc=0 with a warning
    claiming to trust 'the exposure-effect check' — the very check that had
    just reported NO EFFECT. The effect check measures pixels, not
    readback: hard on every backend."""
    clock = FakeClock()

    def make(i: int, b: int) -> FakeCapture:
        return FakeCapture(
            hooks={cv2.CAP_PROP_EXPOSURE: lambda v: None},  # physically dead
            clock=clock,
            real_fps=lambda cap: min(cap.get(cv2.CAP_PROP_FPS) or 30.0, 120.0),
        )

    rc, out = _run(
        _cfg(),  # backend msmf: readback-unreliable, effect gate still hard
        CalibrateOptions(seconds=0, exposure=-6.0),
        FakeCaptureFactory(default=make),
        _lister(),
        clock,
    )
    assert rc == 1, out
    assert "NO EFFECT" in out
    assert "hard on every backend" in out


def test_exit_codes_for_failure_paths() -> None:
    clock = FakeClock()
    # No devices, no fallback_index.
    rc, out = _run(
        _cfg(), CalibrateOptions(), FakeCaptureFactory(), lambda: [], clock
    )
    assert rc == 1 and "no devices enumerated" in out
    # Devices exist but nothing sustains its requested rate.
    crawling = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=2.0)
    )
    rc, out = _run(_cfg(), CalibrateOptions(), crawling, _lister(), clock)
    assert rc == 1 and "no probed mode" in out
    # No cameras configured and no --name: nothing to calibrate.
    rc, out = _run(
        AppConfig(), CalibrateOptions(), FakeCaptureFactory(), _lister(), clock
    )
    assert rc == 2 and "at least one" in out


# -- REVIEW_SYSTEM_0c30c77 findings b3, b9 and 8 (calibrate side) -------------


def test_bootstrap_without_name_prints_actionable_recipe() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b3 (repro: the day-one calibration
    command against the bootstrapped `cameras: []` config failed exit 2
    with a circular error whose suggested remedy was the same failing
    command): the error must hand the operator the --name recipe."""
    clock = FakeClock()
    cfg = AppConfig.model_validate(
        {"source": {"type": "camera"}, "cameras": []}
    )
    rc, out = _run(
        cfg,
        CalibrateOptions(camera="cam-color", save=True, seconds=0),
        _see3cam_factory(clock),
        _lister(),
        clock,
    )
    assert rc == 2
    assert "--name" in out
    assert "calibrate --list" in out


def test_explicit_backend_mismatch_without_fallback_refuses() -> None:
    """REVIEW_SYSTEM_0c30c77 finding 8, calibrate side: calibrating the
    wrong physical device would persist its settings under this camera's
    id; mixing a name-resolved index with another backend is refused."""
    clock = FakeClock()

    def dshow_lister():
        return devices_from_names(["See3CAM_24CUG"], int(cv2.CAP_DSHOW))

    rc, out = _run(
        _cfg(),  # backend msmf, enumeration DSHOW
        CalibrateOptions(camera="cam-color", seconds=0),
        _see3cam_factory(clock),
        dshow_lister,
        clock,
    )
    assert rc == 1
    assert "enumeration backend" in out
    assert "fallback_index" in out


def test_explicit_backend_with_fallback_uses_pinned_index() -> None:
    """Finding 8, calibrate side: the sanctioned escape hatch — pinned
    index under the explicit backend flag, with the forfeit said out
    loud."""
    clock = FakeClock()

    def dshow_lister():
        return devices_from_names(["See3CAM_24CUG"], int(cv2.CAP_DSHOW))

    factory = _see3cam_factory(clock)
    rc, out = _run(
        _cfg(fallback_index=1),
        CalibrateOptions(camera="cam-color", fourcc="MJPG", seconds=0),
        factory,
        dshow_lister,
        clock,
    )
    assert rc == 0, out
    assert "fallback_index 1" in out
    assert all(call[0] == 1 for call in factory.calls)
    assert all(call[1] == MSMF for call in factory.calls)


def test_pygrabber_backend_dispatches_pygrabber_capture_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVIEW bringup-4d95b67: run_calibration built cv2.VideoCapture for
    EVERY backend, so the Y8-only mono cam (which this commit documents
    OpenCV cannot open) could never be calibrated — and calibrate is the
    step that stamps the identity fingerprint. The capture factory must
    dispatch by backend exactly like CameraSource. A stub module stands in
    for pygrabber_capture so no COM/hardware is touched."""
    import sys
    import types

    clock = FakeClock()
    created: list[tuple] = []

    class StubPyGrabberCapture(FakeCapture):
        def __init__(
            self,
            index: int,
            *,
            width: int | None = None,
            height: int | None = None,
            fps: float | None = None,
        ) -> None:
            super().__init__(
                clock=clock,
                real_fps=lambda cap: min(cap.get(cv2.CAP_PROP_FPS) or 30.0, 120.0),
            )
            created.append((index, width, height, fps))

    stub_mod = types.ModuleType("palletscan.sources.pygrabber_capture")
    stub_mod.PyGrabberCapture = StubPyGrabberCapture  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "palletscan.sources.pygrabber_capture", stub_mod
    )

    from palletscan.sources.camera import default_capture_factory

    rc, out = _run(
        _cfg(backend="pygrabber", width=640, height=480),
        CalibrateOptions(fourcc="GREY", width=640, height=480, fps=30.0,
                         seconds=0, exposure=-6.0),
        default_capture_factory,  # the production default — must NOT be used
        _lister(backend=int(cv2.CAP_DSHOW)),  # pygrabber enumerates as DSHOW
        clock,
    )
    assert created, "the pygrabber capture factory was never dispatched"
    assert rc == 0, out
    # The cfg-closed factory hands the graph the entry's target geometry
    # (fps is None here: _cfg does not set one on the camera entry).
    assert all(c == (0, 640, 480, None) for c in created)


def test_metrics_loop_reads_negotiated_luma_plane() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b9 (repro: for raw packed-YUV modes
    the metrics loop read the chroma plane — decodes always [], garbage
    focus/brightness — steering the operator away from a working mode):
    the loop must read the luma plane of the format the device actually
    negotiated."""
    clock = FakeClock()

    def packed_frame(cap: FakeCapture) -> np.ndarray:
        h = int(cap.props[cv2.CAP_PROP_FRAME_HEIGHT])
        w = int(cap.props[cv2.CAP_PROP_FRAME_WIDTH])
        img = np.empty((h, w, 2), np.uint8)
        img[:, :, 0] = 128  # chroma plane (flat — what the bug measured)
        img[:, :, 1] = 200  # UYVY luma plane
        return img

    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            props={cv2.CAP_PROP_FOURCC: fourcc_float("UYVY")},
            clock=clock,
            real_fps=30.0,
            frame_factory=packed_frame,
        )
    )
    rc, out = _run(
        _cfg(fourcc="UYVY", convert_rgb=False),
        CalibrateOptions(camera="cam-color", fourcc="UYVY", seconds=1),
        factory,
        _lister(),
        clock,
    )
    assert rc == 0, out
    brightness = re.search(r"brightness\s+([0-9.]+)", out)
    assert brightness is not None, out
    assert float(brightness.group(1)) == pytest.approx(200.0, abs=2.0), (
        "metrics read the chroma plane, not the negotiated luma"
    )
