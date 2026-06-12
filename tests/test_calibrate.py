"""calibrate: list, probe+choose, focus metric, decode line, save, exit codes."""

from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pytest

from palletscan.calibrate import CalibrateOptions, focus_metric, run_calibration
from palletscan.config import AppConfig, load_config
from palletscan.selftest import SELFTEST_ASSETS
from palletscan.sources.controls import fourcc_float
from palletscan.sources.devices import devices_from_names
from palletscan.types import Symbology
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock

MSMF = int(cv2.CAP_MSMF)


def _lister(*names: str):
    return lambda: devices_from_names(
        list(names) or ["See3CAM_24CUG"], MSMF
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
        _cfg(),  # backend msmf: controls_reliable
        CalibrateOptions(seconds=0, exposure=-6.0),
        FakeCaptureFactory(default=make),
        _lister(),
        clock,
    )
    assert rc == 1
    assert "hard on this backend" in out


def test_unverified_controls_warn_on_avfoundation() -> None:
    from tests.camera_fakes import avfoundation_hooks

    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            hooks=avfoundation_hooks(),
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
    assert "verify on the Windows target" in out


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
