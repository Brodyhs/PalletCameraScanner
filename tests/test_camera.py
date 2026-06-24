"""CameraSource identity guard: the MSMF wrong-camera-swap defense.

The color See3CAM_24CUG opens BY POSITION under MSMF (pinned index, no
name check), so a replug reorder or an OBS Virtual Camera at the same
1920x1200 silently swaps in the wrong device under the first-frame shape
gate's radar. ``_guard_identity()`` fingerprints what ``cfg.name`` resolves
to in the live enumeration and asserts it has not drifted from calibration.

DORMANT BY DEFAULT: policy='warn' (the model default) is byte-for-byte
today's behavior — a single WARNING + connect_mismatches, never a raise.
These tests exercise warn/strict/off through the same
capture_factory/device_lister seams production uses (no hardware).
"""

from __future__ import annotations

import logging

import cv2
import pytest

from palletscan.config import Backend, CameraConfig, CameraIdentity
from palletscan.sources.camera import CameraConnectError, CameraSource
from palletscan.sources.devices import DeviceInfo, IdentityInfo
from tests.camera_fakes import FakeCapture, FakeCaptureFactory, FakeClock

MSMF = int(cv2.CAP_MSMF)
DSHOW = int(cv2.CAP_DSHOW)

# Two cameras that SHARE the configured name+resolution but differ in
# VID/PID — the exact same-resolution impostor the shape gate cannot catch.
_CALIBRATED = IdentityInfo(
    friendly_name="See3CAM_24CUG",
    device_path=r"usb#vid_2560&pid_c128&mi_00#calibrated",
    vid="2560",
    pid="c128",
)
_IMPOSTOR = IdentityInfo(
    friendly_name="See3CAM_24CUG",
    device_path=r"usb#vid_1234&pid_5678&mi_00#impostor",
    vid="1234",
    pid="5678",
)


def _cfg(identity: CameraIdentity | None = None, **kw) -> CameraConfig:
    # Mirrors the production color cam: backend=msmf opened by a pinned
    # fallback_index, while enumeration is DSHOW (the index!=DSHOW-index
    # nuance the guard documents). fallback_index lets the MSMF open path
    # run under a DSHOW enumeration without the finding-8 hard stop.
    defaults = dict(
        id="cam-color",
        name="See3CAM_24CUG",
        backend=Backend.MSMF,
        fallback_index=0,
        connect_verify_s=0.0,
    )
    defaults.update(kw)
    if identity is not None:
        defaults["identity"] = identity
    return CameraConfig(**defaults)


def _lister(identity: IdentityInfo | None, *, name: str = "See3CAM_24CUG"):
    """Single-device enumeration carrying the given identity (or None for
    the name-only / macOS path)."""

    def lister() -> list[DeviceInfo]:
        return [DeviceInfo(name=name, index=0, backend=DSHOW, identity=identity)]

    return lister


def _build(cfg: CameraConfig, lister) -> CameraSource:
    clock = FakeClock()
    factory = FakeCaptureFactory(
        default=lambda i, b: FakeCapture(clock=clock, real_fps=30.0)
    )
    return CameraSource(
        cfg, capture_factory=factory, device_lister=lister, clock=clock
    )


# -- mismatch: strict raises, warn counts --------------------------------------


def test_vid_pid_mismatch_strict_raises() -> None:
    """Same name + resolution, different VID/PID: strict refuses to scan
    the wrong camera."""
    cfg = _cfg(
        CameraIdentity(policy="strict", expected_vid_pid="2560:c128")
    )
    with pytest.raises(CameraConnectError, match="identity MISMATCH"):
        _build(cfg, _lister(_IMPOSTOR))


def test_vid_pid_mismatch_warn_increments_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _cfg(CameraIdentity(policy="warn", expected_vid_pid="2560:c128"))
    with caplog.at_level(logging.WARNING, logger="palletscan.sources.camera"):
        src = _build(cfg, _lister(_IMPOSTOR))
    assert src.connect_mismatches >= 1
    assert any("identity MISMATCH" in r.message for r in caplog.records)
    src.close()


def test_device_path_mismatch_strict_raises() -> None:
    """device_path is the strongest fingerprint and wins the ladder when
    both expected and actual paths are present."""
    cfg = _cfg(
        CameraIdentity(
            policy="strict",
            expected_device_path=_CALIBRATED.device_path,
        )
    )
    with pytest.raises(CameraConnectError, match="device_path"):
        _build(cfg, _lister(_IMPOSTOR))


def test_matching_identity_strict_connects() -> None:
    cfg = _cfg(
        CameraIdentity(
            policy="strict",
            expected_vid_pid="2560:c128",
            expected_device_path=_CALIBRATED.device_path,
        )
    )
    src = _build(cfg, _lister(_CALIBRATED))  # exact match -> no raise
    assert src.connect_mismatches == 0
    src.close()


# -- identity unavailable (macOS, unreadable DevicePath) -----------------------


def test_identity_unavailable_warn_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No identity on the enumerated device (macOS / name-only fakes):
    proceed on NAME match only, never raise under warn, never claim
    confirmed."""
    cfg = _cfg(CameraIdentity(policy="warn", expected_vid_pid="2560:c128"))
    with caplog.at_level(logging.INFO, logger="palletscan.sources.camera"):
        src = _build(cfg, _lister(None))  # identity=None
    assert src.connect_mismatches == 0
    assert any(
        "identity unverifiable" in r.message and "NAME match only" in r.message
        for r in caplog.records
    )
    src.close()


def test_strict_with_no_fingerprint_pinned_does_not_raise_on_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """strict but NOTHING pinned (no expected_vid_pid/device_path): there is
    no expectation to violate, so an unavailable identity must NOT raise —
    strict gates a mismatch, not the mere absence of a fingerprint."""
    cfg = _cfg(CameraIdentity(policy="strict"))  # no expected_* fields
    with caplog.at_level(logging.INFO, logger="palletscan.sources.camera"):
        src = _build(cfg, _lister(None))
    assert src.connect_mismatches == 0
    src.close()


def test_strict_msmf_with_pinned_fingerprint_but_no_identity_raises() -> None:
    """strict + MSMF (opens by position) + a pinned fingerprint, but the
    live enumeration yields NO identity to check it against: refuse to open
    by position with no identity check."""
    cfg = _cfg(CameraIdentity(policy="strict", expected_vid_pid="2560:c128"))
    with pytest.raises(CameraConnectError, match="no identity could be obtained"):
        _build(cfg, _lister(None))


def test_strict_dshow_with_pinned_fingerprint_no_identity_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 'no identity to check' hard-stop is scoped to MSMF (by-position).
    Under DSHOW the name resolution is itself the proof, so an unavailable
    identity degrades to name-only without raising even in strict."""
    cfg = _cfg(
        CameraIdentity(policy="strict", expected_vid_pid="2560:c128"),
        backend=Backend.DSHOW,
    )
    with caplog.at_level(logging.INFO, logger="palletscan.sources.camera"):
        src = _build(cfg, _lister(None, name="See3CAM_24CUG"))
    assert src.connect_mismatches == 0
    src.close()


# -- no expectation pinned (calibrate not yet run) -----------------------------


def test_no_expected_fingerprint_logs_present_identity_no_gate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default warn, identity present but nothing pinned: log the live
    fingerprint, gate nothing (there is no calibration to compare)."""
    cfg = _cfg(CameraIdentity())  # policy=warn, no expected_*
    with caplog.at_level(logging.INFO, logger="palletscan.sources.camera"):
        src = _build(cfg, _lister(_CALIBRATED))
    assert src.connect_mismatches == 0
    assert any(
        "no expected fingerprint pinned" in r.message for r in caplog.records
    )
    src.close()


# -- policy 'off' --------------------------------------------------------------


def test_policy_off_skips_guard_even_on_mismatch() -> None:
    """policy='off' skips the check entirely: even a VID/PID mismatch is
    ignored (the operator explicitly disabled the guard)."""
    cfg = _cfg(
        CameraIdentity(policy="off", expected_vid_pid="2560:c128")
    )
    src = _build(cfg, _lister(_IMPOSTOR))  # would mismatch, but guard is off
    assert src.connect_mismatches == 0
    src.close()


# -- default behavior is unchanged ---------------------------------------------


def test_default_policy_is_warn_and_dormant() -> None:
    """The model default must be warn (dormant): a plain CameraConfig opens
    with no identity gate and no mismatch, identical to pre-guard behavior."""
    cfg = CameraConfig(
        id="cam-color", name="See3CAM_24CUG", backend=Backend.MSMF,
        fallback_index=0, connect_verify_s=0.0,
    )
    assert cfg.identity.policy == "warn"
    assert cfg.identity.expected_vid_pid is None
    src = _build(cfg, _lister(_CALIBRATED))
    assert src.connect_mismatches == 0
    src.close()
