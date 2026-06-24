"""Control layer: quirks, apply order, honest readback, exposure effect."""

from __future__ import annotations

import cv2
import pytest

from palletscan.config import Backend, CameraConfig, CameraSettings
from palletscan.sources.controls import (
    QUIRKS,
    ControlReport,
    all_verified,
    apply_mode,
    apply_settings,
    fourcc_float,
    fourcc_str,
    measure_achieved_fps,
    quirks_for,
    resolve_backend,
    verify_exposure_effect,
)
from tests.camera_fakes import (
    FakeCapture,
    FakeClock,
    avfoundation_hooks,
    dshow_hooks,
    msmf_hooks,
)


def _cam(**kw) -> CameraConfig:
    return CameraConfig(id="cam", name="See3CAM_24CUG", **kw)


# -- quirks ------------------------------------------------------------------


def test_quirks_resolution_and_table_values() -> None:
    assert resolve_backend(Backend.AUTO, platform="win32") is Backend.DSHOW
    assert (
        resolve_backend(Backend.AUTO, platform="darwin") is Backend.AVFOUNDATION
    )
    assert resolve_backend(Backend.MSMF, platform="darwin") is Backend.MSMF
    msmf = quirks_for(Backend.MSMF)
    assert (msmf.auto_exposure_off, msmf.auto_exposure_on) == (0.25, 0.75)
    assert QUIRKS[Backend.DSHOW].exposure_is_log2
    assert not QUIRKS[Backend.AVFOUNDATION].controls_reliable


def test_fourcc_round_trip() -> None:
    assert fourcc_str(fourcc_float("UYVY")) == "UYVY"
    assert fourcc_str(fourcc_float("GREY")) == "GREY"


# -- apply_mode ----------------------------------------------------------------


def test_apply_mode_order_and_verification() -> None:
    cap = FakeCapture()
    reports = apply_mode(
        cap, _cam(fourcc="UYVY", width=1920, height=1200, fps=120.0)
    )
    assert [p for p, _ in cap.set_calls] == [
        cv2.CAP_PROP_FOURCC,
        cv2.CAP_PROP_FRAME_WIDTH,
        cv2.CAP_PROP_FRAME_HEIGHT,
        cv2.CAP_PROP_FPS,
        cv2.CAP_PROP_CONVERT_RGB,
        cv2.CAP_PROP_BUFFERSIZE,
    ]
    assert [r.prop for r in reports] == [
        "fourcc",
        "width",
        "height",
        "fps",
        "convert_rgb",
        "buffersize",
    ]
    assert all_verified(reports)
    assert cap.get(cv2.CAP_PROP_CONVERT_RGB) == 1.0


def test_apply_mode_skips_unset_fields_and_convert_rgb_off() -> None:
    cap = FakeCapture()
    reports = apply_mode(cap, _cam(convert_rgb=False))
    assert [r.prop for r in reports] == ["convert_rgb", "buffersize"]
    assert cap.get(cv2.CAP_PROP_CONVERT_RGB) == 0.0


def test_apply_mode_reports_device_snapping_honestly() -> None:
    # Device refuses 1920 wide and snaps to 640.
    cap = FakeCapture(hooks={cv2.CAP_PROP_FRAME_WIDTH: lambda v: 640.0})
    reports = {r.prop: r for r in apply_mode(cap, _cam(width=1920, height=1080))}
    assert reports["width"].accepted  # set() lied, readback caught it
    assert not reports["width"].verified
    assert "640" in reports["width"].note
    assert reports["height"].verified
    assert not all_verified(list(reports.values()))


# -- apply_settings -------------------------------------------------------------


def test_apply_settings_msmf_order_and_values() -> None:
    cap = FakeCapture(hooks=msmf_hooks())
    settings = CameraSettings(
        exposure_auto=False, exposure=-6.0, gain=10.0, brightness=2.0
    )
    reports = apply_settings(cap, settings, quirks_for(Backend.MSMF))
    assert [r.prop for r in reports] == [
        "auto_exposure",
        "exposure",
        "gain",
        "brightness",
    ]
    # Auto-exposure off lands first with MSMF's 0.25 magic value.
    assert cap.set_calls[0] == (cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
    assert all_verified(reports)


def test_apply_settings_auto_exposure_on_skips_nothing_set() -> None:
    cap = FakeCapture(hooks=msmf_hooks())
    reports = apply_settings(cap, CameraSettings(), quirks_for(Backend.MSMF))
    assert [r.prop for r in reports] == ["auto_exposure"]
    assert cap.set_calls[0] == (cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)


def test_dshow_quantized_exposure_counts_as_verified() -> None:
    cap = FakeCapture(hooks=dshow_hooks())
    settings = CameraSettings(exposure_auto=False, exposure=-5.7)
    reports = apply_settings(cap, settings, quirks_for(Backend.DSHOW))
    exposure = next(r for r in reports if r.prop == "exposure")
    assert exposure.readback == -6.0
    assert exposure.verified  # within a whole log2 stop
    assert "quantized" in exposure.note


def test_avfoundation_ignored_sets_reported_unverified() -> None:
    # AVFoundation is controls-unreliable: readback cannot confirm the write,
    # so each control is reported verifiable=False (asserted, not confirmed)
    # with the backend named in the note — not a verification 'failure'.
    cap = FakeCapture(hooks=avfoundation_hooks())
    settings = CameraSettings(exposure_auto=False, exposure=-6.0, gain=5.0)
    reports = apply_settings(
        cap, settings, quirks_for(Backend.AVFOUNDATION), backend_name="avfoundation"
    )
    assert not any(r.verified for r in reports)
    assert all(not r.verifiable for r in reports)
    assert all("not confirmed" in r.note.lower() for r in reports)
    assert all("avfoundation" in r.note for r in reports)
    # all_verified tolerates an unverifiable control (warn-not-gate).
    assert all_verified(reports)


# -- readback honesty: verifiable=False ----------------------------------------


def test_control_report_verifiable_default_true() -> None:
    r = ControlReport(
        prop="exposure", requested=-6.0, accepted=True, readback=-6.0, verified=True
    )
    assert r.verifiable is True


def test_unverifiable_report_renders_asserted_not_confirmed() -> None:
    # MSMF: readback can't confirm the write -> verified False, verifiable
    # False, and the note states intent (asserted), naming the backend.
    cap = FakeCapture(hooks=msmf_hooks())
    reports = apply_settings(
        cap,
        CameraSettings(exposure_auto=False, exposure=-6.0, gain=10.0),
        quirks_for(Backend.MSMF),
        backend_name="msmf",
    )
    exposure = next(r for r in reports if r.prop == "exposure")
    assert exposure.verifiable is False
    assert exposure.verified is False
    assert "intent asserted, not confirmed" in exposure.note
    assert "msmf" in exposure.note


def test_unverifiable_report_does_not_fail_all_verified() -> None:
    # A mixed bag: one reliable verified control + one backend-unverifiable
    # control must still pass all_verified (the unverifiable one is tolerated).
    good = ControlReport(
        prop="width", requested=1920.0, accepted=True, readback=1920.0, verified=True
    )
    unverifiable = ControlReport(
        prop="exposure",
        requested=-6.0,
        accepted=True,
        readback=0.0,
        verified=False,
        note="applied request=-6; readback 0 NOT trustworthy on msmf — "
        "intent asserted, not confirmed",
        verifiable=False,
    )
    assert all_verified([good, unverifiable])
    # A genuinely failed (verifiable) control still fails the gate.
    failed = ControlReport(
        prop="width", requested=1920.0, accepted=True, readback=640.0, verified=False
    )
    assert not all_verified([good, failed])


def test_pygrabber_backend_named_in_unverifiable_note() -> None:
    # The note must name the ACTUAL backend, not hardcode MSMF: the 37CUGM
    # pygrabber path says 'pygrabber'.
    cap = FakeCapture()
    reports = apply_settings(
        cap,
        CameraSettings(exposure_auto=False, exposure=-6.0),
        quirks_for(Backend.PYGRABBER),
        backend_name="pygrabber",
    )
    exposure = next(r for r in reports if r.prop == "exposure")
    assert exposure.verifiable is False
    assert "pygrabber" in exposure.note


# -- measure_achieved_fps --------------------------------------------------------


def test_measure_achieved_fps_empirical_not_requested() -> None:
    clock = FakeClock()
    # Device claims 120 fps but actually delivers 57.
    cap = FakeCapture(
        props={cv2.CAP_PROP_FPS: 120.0}, clock=clock, real_fps=57.0
    )
    m = measure_achieved_fps(cap, sample_s=1.0, warmup_frames=5, clock=clock)
    assert m.fps == pytest.approx(57.0, rel=0.01)
    assert m.frames in (57, 58)  # fp accumulation may add one boundary read
    assert m.read_failures == 0
    assert cap.reads == 5 + m.frames  # warmup excluded from the sample


def test_measure_achieved_fps_counts_failures() -> None:
    clock = FakeClock()
    cap = FakeCapture(
        read_script=["ok", "fail", "ok", "fail"],
        clock=clock,
        real_fps=10.0,
    )
    m = measure_achieved_fps(cap, sample_s=1.0, warmup_frames=0, clock=clock)
    assert m.read_failures == 2
    assert m.frames == 8  # 10 reads in the window, 2 failed


# -- verify_exposure_effect ------------------------------------------------------


def test_verify_exposure_effect_detects_and_restores() -> None:
    cap = FakeCapture(hooks=msmf_hooks())
    cap.set(cv2.CAP_PROP_EXPOSURE, -6.0)
    report = verify_exposure_effect(cap, -6.0, step=2.0, margin=4.0)
    assert report.ok
    # default_frame model: 8 counts per stop -> +16 for a +2 step.
    assert report.delta == pytest.approx(16.0, abs=1.0)
    assert cap.sets_for(cv2.CAP_PROP_EXPOSURE)[-1] == -6.0  # restored


def test_verify_exposure_effect_flags_dead_control() -> None:
    cap = FakeCapture(hooks=avfoundation_hooks())
    report = verify_exposure_effect(cap, -6.0)
    assert not report.ok
    assert "no effect" in report.note
