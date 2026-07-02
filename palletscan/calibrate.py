"""``palletscan calibrate``: probe, verify, lock-and-save camera settings.

Non-interactive and flag-driven by default (testable, SSH-able, fine on a
headless factory PC): list devices, probe the mode matrix (or pin one),
apply/verify controls including the exposure-effect check, stream a
focus/fps/decode line per second, and ``--save`` upserts the locked entry
into the YAML config (spec §8). ``--preview`` adds an optional cv2 window
(main-thread only on macOS); it is the one path pytest does not cover.

Hard-fail policy mirrors the control layer's: readback-unverified
controls fail calibration on ``readback_reliable`` backends (DSHOW) and
print an honest warning where readback is untrustworthy (MSMF,
AVFoundation, pygrabber). A control write the device REJECTED (``set()``
returned False) and the exposure-EFFECT check (which measures the image,
not readback) fail calibration on EVERY backend.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

import cv2

from palletscan.config import (
    AppConfig,
    Backend,
    CameraConfig,
    CameraIdentity,
    resolve_camera,
    upsert_camera_yaml,
)
from palletscan.sources.camera import (
    CaptureFactory,
    DeviceLister,
    default_capture_factory,
    select_capture_factory,
)
from palletscan.sources.controls import (
    all_verified,
    apply_mode,
    apply_settings,
    fourcc_str,
    log_reports,
    measure_achieved_fps,
    quirks_for,
    verify_exposure_effect,
)
from palletscan.sources.devices import (
    backend_flag,
    find_device,
    identity_for_name,
    list_devices,
)
from palletscan.sources.probe import (
    ModeCandidate,
    candidates_for,
    choose_mode,
    current_mode,
    format_probe_table,
    probe_modes,
)
from palletscan.sources.video import packed_luma_channel_for, to_gray

log = logging.getLogger(__name__)

_FLAG_TO_BACKEND = {
    int(cv2.CAP_DSHOW): Backend.DSHOW,
    int(cv2.CAP_MSMF): Backend.MSMF,
    int(cv2.CAP_AVFOUNDATION): Backend.AVFOUNDATION,
}


def focus_metric(gray) -> float:
    """Variance of the Laplacian: sharper image -> higher value."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


@dataclass(slots=True)
class CalibrateOptions:
    """Flag set for one calibration run (built by the CLI parser)."""

    list_only: bool = False
    camera: str | None = None  # cameras[].id to calibrate / save as
    name: str | None = None  # device-name substring for a fresh entry
    fourcc: str | None = None  # any pin flag set -> skip the full matrix
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    exposure: float | None = None
    gain: float | None = None
    auto_exposure: bool | None = None
    seconds: int = 5
    save: bool = False
    config_path: Path | None = None
    preview: bool = False
    probe_sample_s: float = 1.0


def _resolve_entry(cfg: AppConfig, opts: CalibrateOptions) -> CameraConfig:
    if opts.name is not None:
        return CameraConfig(id=opts.camera or "cam-main", name=opts.name)
    selected = cfg.model_copy(
        update={"source": cfg.source.model_copy(update={"camera": opts.camera})}
    )
    return resolve_camera(selected)


def _pinned_candidate(entry: CameraConfig, opts: CalibrateOptions, cap) -> ModeCandidate:
    cur = current_mode(cap)
    return ModeCandidate(
        fourcc=opts.fourcc or entry.fourcc or cur.fourcc,
        width=opts.width or entry.width or cur.width,
        height=opts.height or entry.height or cur.height,
        fps=opts.fps or entry.fps or cur.fps,
    )


def run_calibration(
    cfg: AppConfig,
    opts: CalibrateOptions,
    *,
    capture_factory: CaptureFactory = default_capture_factory,
    device_lister: DeviceLister = list_devices,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO = sys.stdout,
) -> int:
    """Orchestrate one calibration run; returns the process exit code."""
    say = lambda msg: print(msg, file=out)  # noqa: E731
    devices = device_lister()
    if opts.list_only:
        if not devices:
            say("no cameras enumerated (see log for the platform reason)")
        for d in devices:
            say(f"[{d.index}] {d.name}  (backend flag {d.backend})")
        return 0

    try:
        entry = _resolve_entry(cfg, opts)
    except ValueError as exc:
        say(f"calibrate: {exc}")
        if opts.name is None:
            # Day-one bootstrap (cameras: [] or an unknown id): without
            # this recipe the error's only remedy was the same failing
            # command — a circular arrival-day blocker (REVIEW finding b3).
            say(
                "calibrate: to create a new cameras[] entry, pass the "
                "device name too:\n"
                f"  palletscan calibrate --camera {opts.camera or '<id>'} "
                "--name '<device-name substring>' --save --config <file>\n"
                "(list device names with: palletscan calibrate --list)"
            )
        return 2

    if devices:
        try:
            dev = find_device(devices, entry.name)
        except ValueError as exc:
            say(f"calibrate: {exc}")
            return 1
        if entry.backend is Backend.AUTO:
            index, flag = dev.index, dev.backend
        else:
            flag = backend_flag(entry.backend)
            if flag != dev.backend:
                # Same rule as the run path (REVIEW finding 8): calibrating
                # the wrong physical device would persist its settings under
                # this camera's id.
                if entry.fallback_index is None:
                    say(
                        f"calibrate: explicit backend {entry.backend} is not "
                        "the enumeration backend; a name-resolved index "
                        "under another backend can open the wrong camera. "
                        "Pin cameras[].fallback_index to calibrate under "
                        f"{entry.backend} (forfeits name stability)."
                    )
                    return 1
                index = entry.fallback_index
                say(
                    f"warning: using pinned fallback_index {index} under "
                    f"{entry.backend} — name resolution forfeited; index "
                    "order is NOT stable across replugs"
                )
            else:
                index = dev.index
    elif entry.fallback_index is not None:
        index, flag = entry.fallback_index, backend_flag(entry.backend)
        say(f"warning: no device names; using fallback index {index}")
    else:
        say("calibrate: no devices enumerated and no fallback_index")
        return 1
    backend = (
        entry.backend
        if entry.backend is not Backend.AUTO
        else _FLAG_TO_BACKEND.get(flag, Backend.AUTO)
    )
    quirks = quirks_for(backend)
    # Per-backend capture dispatch (shared with CameraSource/selftest): the
    # pygrabber mono cam cannot be opened by cv2.VideoCapture, and calibrate
    # is the step that stamps the fingerprint the identity guard consumes —
    # it must be able to open every backend the run path can (REVIEW
    # bringup-4d95b67). The entry is resolved to the chosen backend first so
    # a backend:auto config still dispatches correctly.
    entry = entry.model_copy(update={"backend": backend})
    chosen_factory = select_capture_factory(entry, capture_factory)
    make_cap = lambda: chosen_factory(index, flag)  # noqa: E731

    def make_cap_for(cand: ModeCandidate) -> Any:
        """A capture programmed FOR ``cand``. pygrabber's DirectShow format
        is fixed at graph construction and its mode ``set()`` calls are
        accepted no-ops, so each candidate must be built into its own graph
        with the candidate's geometry/fps — through the entry-closed factory
        every candidate was measured on the seed-negotiated format while the
        unprogrammed candidate got locked into the YAML (re-review of REVIEW
        bringup-4d95b67). cv2 backends keep the entry-closed capture;
        probe_modes programs them via ``set()``."""
        if backend is not Backend.PYGRABBER:
            return make_cap()
        cand_entry = entry.model_copy(
            update={
                "fourcc": cand.fourcc,
                "width": cand.width,
                "height": cand.height,
                "fps": cand.fps,
            }
        )
        return select_capture_factory(cand_entry, capture_factory)(index, flag)

    # Capture the chosen device's stable hardware fingerprint so --save can
    # stamp it for the identity guard. The POLICY is left at its default
    # ('warn'): calibrate records the fingerprint, the operator opts into
    # 'strict' deliberately later. None on macOS / when no DevicePath.
    captured_identity = identity_for_name(devices, entry.name)

    # -- probe ---------------------------------------------------------------
    pinned = any(v is not None for v in (opts.fourcc, opts.width, opts.height, opts.fps))
    seed_cap = make_cap()
    try:
        if pinned:
            candidates = [_pinned_candidate(entry, opts, seed_cap)]
        else:
            candidates = candidates_for(entry.name, current=current_mode(seed_cap))
    finally:
        seed_cap.release()
    results = probe_modes(
        make_cap_for, candidates, sample_s=opts.probe_sample_s, clock=clock
    )
    chosen = choose_mode(results)
    say(format_probe_table(results, chosen))
    # The full table + choice also lands in the structured log (spec §2).
    log.info(
        "probe complete: %d candidate(s), chosen %s",
        len(results),
        chosen.candidate.describe() if chosen else None,
        extra={
            "stats": {
                "probe": [asdict(r) for r in results],
                "chosen": asdict(chosen) if chosen else None,
            }
        },
    )
    if chosen is None:
        say("calibrate: no probed mode sustained its requested frame rate")
        return 1
    say(f"chosen mode: {chosen.candidate.describe()} "
        f"(achieved {chosen.achieved_fps:.1f} fps)")
    if backend is Backend.PYGRABBER and (
        (chosen.actual_width, chosen.actual_height)
        != (chosen.candidate.width, chosen.candidate.height)
    ):
        # The candidate's geometry was requested at graph construction, so a
        # different negotiated size means the device does not offer it (the
        # capture fell back to another capability). Geometry readback IS the
        # negotiated graph on this backend — trustworthy — and locking the
        # never-streamed request would stamp a mode the run path's shape
        # gate can only reject (re-review of REVIEW bringup-4d95b67).
        say(
            f"calibrate: device negotiated {chosen.actual_width}x"
            f"{chosen.actual_height}, not the requested "
            f"{chosen.candidate.width}x{chosen.candidate.height} — that mode "
            "never streamed; refusing to lock it (hard on pygrabber)"
        )
        return 1

    # -- lock settings ----------------------------------------------------------
    settings = entry.settings.model_copy()
    if opts.auto_exposure is not None:
        settings = settings.model_copy(update={"exposure_auto": opts.auto_exposure})
    elif opts.exposure is not None:
        settings = settings.model_copy(update={"exposure_auto": False})
    if opts.exposure is not None:
        settings = settings.model_copy(update={"exposure": opts.exposure})
    if opts.gain is not None:
        settings = settings.model_copy(update={"gain": opts.gain})
    # Stamp the identity fingerprint (device_path + vid:pid) onto the entry,
    # keeping whatever policy the operator already configured (default 'warn')
    # so the strict opt-in is deliberate. When no identity is available the
    # block is left at its defaults.
    if captured_identity is not None:
        vid_pid = (
            f"{captured_identity.vid}:{captured_identity.pid}"
            if captured_identity.vid and captured_identity.pid
            else None
        )
        identity = entry.identity.model_copy(
            update={
                "expected_device_path": captured_identity.device_path,
                "expected_vid_pid": vid_pid,
            }
        )
    else:
        identity = entry.identity
    locked = entry.model_copy(
        update={
            "backend": backend,
            "fourcc": chosen.candidate.fourcc,
            "width": chosen.candidate.width,
            "height": chosen.candidate.height,
            "fps": chosen.candidate.fps,
            "settings": settings,
            "identity": identity,
        }
    )
    if captured_identity is not None:
        say(
            "captured identity fingerprint: "
            f"device_path={captured_identity.device_path!r} "
            f"vid:pid={locked.identity.expected_vid_pid} "
            f"(policy={locked.identity.policy})"
        )
    else:
        say(
            "captured identity fingerprint: unavailable on this platform/device "
            "(no DevicePath) — identity guard will fall back to NAME match only"
        )

    rc = 0
    # The verification capture streams the CHOSEN mode (on pygrabber the
    # format only exists if it was built into the graph).
    cap = make_cap_for(chosen.candidate)
    try:
        mode_reports = apply_mode(cap, locked)  # mode first: it can reset controls
        control_reports = apply_settings(
            cap, settings, quirks, backend_name=backend.value
        )
        reports = mode_reports + control_reports
        log_reports(f"calibrate cameras[{locked.id}]", reports)
        for r in reports:
            if r.verified:
                mark = "ok"
            elif r.informational:
                mark = "info"
            elif not r.accepted:
                # The backend refused the write: a failure on EVERY backend
                # (readback trustworthiness is irrelevant to a rejected set).
                mark = "REJECTED"
            elif not r.verifiable:
                # Applied, but readback can't confirm it on this backend:
                # honest 'asserted', NOT a failure (would read as MISMATCH).
                mark = "asserted"
            else:
                mark = "MISMATCH"
            say(
                f"  {r.prop:<14} requested {r.requested:<12g} readback "
                f"{r.readback:<12g} {mark} {r.note}"
            )
        # Informational props never gate; a REJECTED control write fails
        # calibration on EVERY backend — the device itself refused the
        # set(), so readback trustworthiness is irrelevant (REVIEW
        # bringup-4d95b67 + re-review). Scoped to apply_settings reports:
        # mode props are gated empirically instead (the probe's achieved
        # fps and, on pygrabber, the negotiated-geometry check above —
        # pygrabber deliberately rejects a set(FPS) it cannot program even
        # when the probe just measured that rate).
        rejected = [
            r for r in control_reports if not r.accepted and not r.informational
        ]
        # Asserted-but-unverifiable controls (readback-unreliable backends;
        # never populated where readback is trusted) keep the honest
        # 'controls unverified' warning even when every write was accepted.
        asserted_only = [
            r
            for r in control_reports
            if not r.informational and not r.verifiable and not r.verified
        ]
        controls_ok = all_verified(reports) and not rejected and not asserted_only
        effect_ok = True
        if not settings.exposure_auto and settings.exposure is not None:
            effect = verify_exposure_effect(cap, settings.exposure)
            say(
                f"  exposure effect: baseline {effect.baseline_mean:.1f} -> "
                f"stepped {effect.stepped_mean:.1f} (delta {effect.delta:+.1f}) "
                f"{'ok' if effect.ok else 'NO EFFECT'}"
            )
            effect_ok = effect.ok
        if not effect_ok:
            # The exposure-effect check measures the IMAGE, not readback: a
            # physically dead exposure control fails calibration on EVERY
            # backend — untrustworthy readback must never demote it (REVIEW
            # bringup-4d95b67: exposure-effect gate restoration).
            say(
                "calibrate: exposure has NO measurable effect on the image "
                "(hard on every backend)"
            )
            rc = 1
        if not controls_ok:
            if quirks.readback_reliable:
                say("calibrate: control verification failed (hard on this backend)")
                rc = 1
            elif rejected:
                # Readback-unreliable backend, but the write never needed
                # readback to fail: the device itself REFUSED the set()
                # (on pygrabber a False set() is device-confirmed). Warn-
                # downgrading this locked settings the device provably
                # refused (re-review of REVIEW bringup-4d95b67).
                say(
                    "calibrate: control write(s) REJECTED by the backend "
                    f"({', '.join(r.prop for r in rejected)}) — hard on "
                    "every backend: the device refused the set()"
                )
                rc = 1
            else:
                say(
                    "warning: controls unverified — readback is unreliable on "
                    "this backend; trusting the exposure-effect and "
                    "achieved-fps checks instead"
                )

        # -- live metrics loop -------------------------------------------------
        decoders = _build_decoders(cfg)
        # The metrics must read the same luma plane production will read,
        # derived from the format the device actually NEGOTIATED: for raw
        # packed-YUV modes, to_gray() without the channel reads the chroma
        # plane — decodes always [], garbage focus/brightness — steering
        # the operator away from a working mode (REVIEW finding b9).
        luma = packed_luma_channel_for(
            fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
        )
        if luma is None:
            luma = packed_luma_channel_for(locked.fourcc)
        for _ in range(max(0, opts.seconds)):
            m = measure_achieved_fps(cap, sample_s=1.0, warmup_frames=0, clock=clock)
            ok, img = cap.read()
            line = f"fps {m.fps:6.1f}"
            if ok and img is not None:
                gray = to_gray(img, packed_luma_channel=0 if luma is None else luma)
                payloads = [
                    p for decode in decoders for p in decode(gray)
                ]
                line += (
                    f"  focus {focus_metric(gray):9.1f}"
                    f"  brightness {float(gray.mean()):6.1f}"
                    f"  decodes {payloads if payloads else '[]'}"
                )
                if opts.preview:  # pragma: no cover - requires a display
                    shown = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                    cv2.putText(
                        shown, line, (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 255, 0), 1,
                    )
                    cv2.imshow("palletscan calibrate", shown)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        break
                    if key == ord("s"):
                        opts.save = True
                        break
            say(line)
    finally:
        cap.release()
        if opts.preview:  # pragma: no cover - requires a display
            cv2.destroyAllWindows()

    # -- save -----------------------------------------------------------------
    if opts.save and rc == 0:
        if opts.config_path is None:
            say("calibrate: --save requires --config (the file to update)")
            return 2
        upsert_camera_yaml(opts.config_path, locked)
        say(f"saved cameras[{locked.id}] to {opts.config_path}")
    return rc


def _build_decoders(cfg: AppConfig) -> "list[Callable[[Any], list[str]]]":
    """Decode callables in configured priority order (live decode test)."""
    from palletscan.pipeline.decoders import PylibdmtxDecoder, PyzbarDecoder
    from palletscan.types import Symbology

    decoders: list[Callable[[Any], list[str]]] = []
    for sym in cfg.decode.symbology_priority:
        if sym is Symbology.QR:
            qr = PyzbarDecoder()

            def _decode_qr(g: Any, _d: PyzbarDecoder = qr) -> list[str]:
                return [r.payload for r in _d.decode(g)]

            decoders.append(_decode_qr)
        else:
            dm = PylibdmtxDecoder()
            timeout = cfg.decode.dm_timeout_ms

            def _decode_dm(
                g: Any, _d: PylibdmtxDecoder = dm, _t: int = timeout
            ) -> list[str]:
                return [r.payload for r in _d.decode(g, timeout_ms=_t)]

            decoders.append(_decode_dm)
    return decoders
