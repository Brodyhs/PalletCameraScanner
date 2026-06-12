"""selftest: asset guards, camera checks, full-pipeline decode, disk, exit codes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from palletscan.cli import main
from palletscan.config import AppConfig
from palletscan.pipeline.decoders import PylibdmtxDecoder, PyzbarDecoder
from palletscan.selftest import SELFTEST_ASSETS, run_selftest
from palletscan.types import Symbology
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock

_GB = 1024**3


def _disk(free_bytes: int = 100 * _GB):
    return lambda path: SimpleNamespace(
        total=500 * _GB, used=10 * _GB, free=free_bytes
    )


def _lister_for(*names: str, backend: int | None = None):
    from palletscan.sources.devices import devices_from_names

    # Default flag matches the test configs' explicit `backend: msmf`: an
    # explicit backend that is NOT the enumeration backend is a hard
    # resolve failure since REVIEW finding 8 (see the dedicated test).
    flag = backend if backend is not None else int(cv2.CAP_MSMF)
    return lambda: devices_from_names(list(names), flag)


def _camera_cfg(tmp_path: Path, fps: float = 30.0) -> AppConfig:
    return AppConfig.model_validate(
        {
            "cameras": [
                {
                    "id": "cam",
                    "name": "See3CAM_24CUG",
                    "backend": "msmf",
                    "fps": fps,
                }
            ],
            "evidence": {"dir": str(tmp_path / "evidence")},
            "sinks": {"http": {"outbox_path": str(tmp_path / "outbox.db")}},
        }
    )


# -- asset guards (the committed PNGs must stay decodable) ------------------------


def test_bundled_qr_asset_decodes() -> None:
    payload, path = SELFTEST_ASSETS[Symbology.QR]
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    assert img is not None, f"missing committed asset {path}"
    assert [r.payload for r in PyzbarDecoder().decode(img)] == [payload]


def test_bundled_dm_asset_decodes() -> None:
    payload, path = SELFTEST_ASSETS[Symbology.DATAMATRIX]
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    assert img is not None, f"missing committed asset {path}"
    decodes = PylibdmtxDecoder().decode(img, timeout_ms=500)
    assert [r.payload for r in decodes] == [payload]


# -- run_selftest -------------------------------------------------------------------


def test_all_green_without_cameras_configured(tmp_path: Path) -> None:
    report = run_selftest(
        AppConfig(), disk_usage=_disk(), data_dir=tmp_path / "scratch"
    )
    assert report.ok
    notice = next(c for c in report.checks if c.name == "cameras")
    assert "no cameras configured" in notice.detail
    decode = next(c for c in report.checks if c.name == "pipeline/decode")
    assert decode.ok and "0 miss(es)" in decode.detail
    assert "FAILED" not in report.format()


def test_camera_checks_green_with_fakes(tmp_path: Path) -> None:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    report = run_selftest(
        _camera_cfg(tmp_path, fps=30.0),
        capture_factory=factory,
        device_lister=_lister_for("See3CAM_24CUG"),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    assert report.ok, report.format()
    fps_check = next(c for c in report.checks if c.name.endswith("/fps"))
    assert "achieved 30" in fps_check.detail


def test_achieved_fps_below_tolerance_is_hard_failure(tmp_path: Path) -> None:
    clock = FakeClock()
    slow = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=40.0)
    )
    report = run_selftest(
        _camera_cfg(tmp_path, fps=120.0),  # 0.85 floor = 102 fps
        capture_factory=slow,
        device_lister=_lister_for("See3CAM_24CUG"),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    assert not report.ok
    fps_check = next(c for c in report.checks if c.name.endswith("/fps"))
    assert not fps_check.ok and fps_check.hard


def test_missing_device_is_hard_failure_and_skip_flag_bypasses(
    tmp_path: Path,
) -> None:
    cfg = _camera_cfg(tmp_path)
    report = run_selftest(
        cfg,
        capture_factory=FakeCaptureFactory(),
        device_lister=_lister_for("FaceTime HD Camera"),  # wrong camera
        disk_usage=_disk(),
        data_dir=tmp_path / "scratch",
    )
    assert not report.ok
    resolve = next(c for c in report.checks if c.name.endswith("/resolve"))
    assert "no camera matching" in resolve.detail
    skipped = run_selftest(
        cfg, skip_camera=True, disk_usage=_disk(), data_dir=tmp_path / "scratch"
    )
    assert skipped.ok


def test_ignored_buffersize_never_fails_selftest(tmp_path: Path) -> None:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            hooks={cv2.CAP_PROP_BUFFERSIZE: lambda v: None},
            clock=clock,
            real_fps=30.0,
        )
    )
    report = run_selftest(
        _camera_cfg(tmp_path, fps=30.0),
        capture_factory=factory,
        device_lister=_lister_for("See3CAM_24CUG"),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    assert report.ok, report.format()
    controls = next(c for c in report.checks if c.name.endswith("/controls"))
    assert controls.ok


def test_controls_warn_not_fail_on_avfoundation(tmp_path: Path) -> None:
    from tests.camera_fakes import avfoundation_hooks

    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            hooks=avfoundation_hooks(), clock=clock, real_fps=30.0
        )
    )
    cfg = AppConfig.model_validate(
        {
            "cameras": [
                {
                    "id": "cam-dev",
                    "name": "FaceTime",
                    "backend": "avfoundation",
                    "fps": 30.0,
                    "settings": {"exposure_auto": False, "exposure": -6.0},
                }
            ],
            "evidence": {"dir": str(tmp_path / "ev")},
            "sinks": {"http": {"outbox_path": str(tmp_path / "o.db")}},
        }
    )
    report = run_selftest(
        cfg,
        capture_factory=factory,
        # The dev-Mac scenario: enumeration backend IS AVFoundation, so the
        # explicit backend matches (finding 8's mismatch rule stays quiet).
        device_lister=_lister_for("FaceTime", backend=int(cv2.CAP_AVFOUNDATION)),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    # Controls and exposure-effect both unhappy, but AVFoundation is not
    # controls_reliable: honest WARNs, selftest still passes overall.
    assert report.ok, report.format()
    controls = next(c for c in report.checks if c.name.endswith("/controls"))
    assert controls.status == "WARN"
    effect = next(c for c in report.checks if c.name.endswith("/exposure_effect"))
    assert effect.status == "WARN"


def test_dead_exposure_control_hard_fails_on_reliable_backend(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(
            hooks={cv2.CAP_PROP_EXPOSURE: lambda v: None},  # set ignored
            clock=clock,
            real_fps=30.0,
        )
    )
    cfg = AppConfig.model_validate(
        {
            "cameras": [
                {
                    "id": "cam",
                    "name": "See3CAM_24CUG",
                    "backend": "msmf",
                    "fps": 30.0,
                    "settings": {"exposure_auto": False, "exposure": -6.0},
                }
            ],
            "evidence": {"dir": str(tmp_path / "ev")},
            "sinks": {"http": {"outbox_path": str(tmp_path / "o.db")}},
        }
    )
    report = run_selftest(
        cfg,
        capture_factory=factory,
        device_lister=_lister_for("See3CAM_24CUG"),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    assert not report.ok
    effect = next(c for c in report.checks if c.name.endswith("/exposure_effect"))
    assert effect.status == "FAIL"  # brightness never moved


def test_disk_pressure_fails_hard_then_warns(tmp_path: Path) -> None:
    cfg = AppConfig()  # 500 MB evidence + 200 MB outbox caps
    hard = run_selftest(
        cfg, disk_usage=_disk(1 * _GB), data_dir=tmp_path / "a"
    )  # 1 GB < 2x 700 MB
    assert not hard.ok
    assert any(c.name.startswith("disk/") and c.hard and not c.ok for c in hard.checks)
    warn = run_selftest(
        cfg, disk_usage=_disk(2 * _GB), data_dir=tmp_path / "b"
    )  # between 2x and 4x
    assert warn.ok  # warns, but no hard failure
    assert any(c.status == "WARN" for c in warn.checks if c.name.startswith("disk/"))


def test_undecodable_asset_produces_miss_and_fails(tmp_path: Path) -> None:
    blank = tmp_path / "blank.png"
    import numpy as np

    cv2.imwrite(str(blank), np.full((200, 200), 200, np.uint8))
    report = run_selftest(
        AppConfig(),
        disk_usage=_disk(),
        data_dir=tmp_path / "scratch",
        assets={Symbology.QR: ("NOPE", blank)},
    )
    assert not report.ok
    decode = next(c for c in report.checks if c.name == "pipeline/decode")
    assert not decode.ok
    assert "1 miss(es)" in decode.detail  # account-for-everything held


def test_selftest_cli_exit_codes(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "ok.yaml"
    cfg.write_text(
        f"evidence: {{dir: {tmp_path / 'ev'}, max_total_mb: 1.0}}\n"
        f"sinks: {{http: {{outbox_path: {tmp_path / 'o.db'}, max_mb: 1.0}}, "
        "console: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(
        ["selftest", "--config", str(cfg), "--data-dir", str(tmp_path / "s")]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "selftest: OK" in out

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        # An impossible cap makes the disk check fail hard everywhere.
        "evidence: {max_total_mb: 100000000.0}\n"
        "sinks: {console: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["selftest", "--config", str(bad), "--data-dir", str(tmp_path / "s2")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "refusing to run blind" in out


# -- REVIEW_SYSTEM_0c30c77 findings b10 and 8 (selftest side) ------------------


def test_decode_check_never_touches_production_evidence(tmp_path: Path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding b10 (repro: pointing --data-dir at
    the live station data dir aimed the decode-check's burst at the
    production evidence tree, and the unconditional post-write prune
    deleted the oldest REAL miss bursts against the shared cap): the
    check's outputs live in an isolated <data-dir>/selftest subtree."""
    data = tmp_path / "data"
    real_burst = data / "evidence" / "2026-06-01" / "cam-a-000007"
    real_burst.mkdir(parents=True)
    marker = real_burst / "frame_00000001.jpg"
    marker.write_bytes(b"real miss evidence")

    cfg = AppConfig.model_validate(
        {
            "evidence": {"dir": str(data / "evidence"), "max_total_mb": 0.0001},
            "sinks": {"http": {"outbox_path": str(data / "outbox.db")}},
        }
    )
    report = run_selftest(
        cfg, skip_camera=True, disk_usage=_disk(), data_dir=data
    )
    decode = next(c for c in report.checks if c.name == "pipeline/decode")
    assert decode.ok, decode.detail
    assert marker.read_bytes() == b"real miss evidence"
    assert real_burst.exists(), (
        "the selftest run pruned a production miss burst (finding b10)"
    )
    assert (data / "selftest").exists()


def test_explicit_backend_mismatch_fails_resolve_without_fallback(
    tmp_path: Path,
) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 8, selftest side: the gate must fail
    the same configuration the run path refuses, instead of green-lighting
    a station that opens DSHOW-ordered indexes under MSMF."""
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    cfg = _camera_cfg(tmp_path)  # backend msmf
    report = run_selftest(
        cfg,
        capture_factory=factory,
        device_lister=_lister_for("See3CAM_24CUG", backend=int(cv2.CAP_DSHOW)),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    resolve = next(c for c in report.checks if c.name.endswith("/resolve"))
    assert not resolve.ok
    assert "enumeration backend" in resolve.detail
    assert not report.ok


def test_explicit_backend_with_fallback_index_resolves(tmp_path: Path) -> None:
    """Finding 8, selftest side: the pinned-index escape hatch passes the
    resolve check and probes the pinned index under the explicit flag."""
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    cfg = _camera_cfg(tmp_path)
    cam = cfg.cameras[0].model_copy(update={"fallback_index": 1})
    cfg = cfg.model_copy(update={"cameras": [cam]})
    report = run_selftest(
        cfg,
        capture_factory=factory,
        device_lister=_lister_for("See3CAM_24CUG", backend=int(cv2.CAP_DSHOW)),
        disk_usage=_disk(),
        clock=clock,
        data_dir=tmp_path / "scratch",
    )
    resolve = next(c for c in report.checks if c.name.endswith("/resolve"))
    assert resolve.ok, resolve.detail
    assert factory.calls and factory.calls[0] == (1, int(cv2.CAP_MSMF))
