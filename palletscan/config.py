"""Configuration models and YAML loading.

A single YAML file configures the whole system. Every field has a default,
so an empty (or absent) file yields a fully working synthetic-mode config.
Unknown keys are rejected to catch typos early.
"""

from __future__ import annotations

import enum
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from palletscan.types import Symbology


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceConfig(_StrictModel):
    """Which FrameSource to run."""

    type: Literal["synthetic", "video", "camera"] = "synthetic"
    #: ``cameras[].id`` to run; optional when exactly one entry exists.
    camera: str | None = None
    #: A/B mode: run one pipeline per listed ``cameras[].id`` with
    #: cross-camera business dedup (Phase 4). Mutually exclusive with
    #: ``camera``; requires ``type: camera`` and >= 2 distinct entries.
    cameras: list[str] | None = None

    @model_validator(mode="after")
    def _ab_mode_consistent(self) -> "SourceConfig":
        if self.cameras is None:
            return self
        if self.camera is not None:
            raise ValueError(
                "source.camera and source.cameras are mutually exclusive"
            )
        if len(self.cameras) < 2:
            raise ValueError(
                f"source.cameras needs >= 2 entries (A/B mode), got {self.cameras}"
            )
        dupes = sorted({c for c in self.cameras if self.cameras.count(c) > 1})
        if dupes:
            raise ValueError(f"duplicate ids in source.cameras: {dupes}")
        if self.type != "camera":
            raise ValueError(
                f"source.cameras requires source.type=camera, got {self.type!r}"
            )
        return self


class VideoConfig(_StrictModel):
    """VideoFileSource: replay a recorded clip through the pipeline.

    ``speed`` shapes only wall-clock delivery (1.0 = as-if-live pacing,
    >1 = accelerated, 0 = unpaced/max rate); frame timestamps always use
    the file's native clock, so pipeline behavior is identical at any
    playback speed.
    """

    path: Path = Path("")
    fps_override: float | None = None  # for files with broken fps metadata
    speed: float = 1.0
    loop: int = 1  # play count; 0 = loop forever (soak runs)

    @field_validator("fps_override")
    @classmethod
    def _fps_positive(cls, v: float | None) -> float | None:
        # NaN compares False to everything, so check finiteness explicitly
        # or it sails through and poisons every Frame.ts downstream.
        if v is not None and (not math.isfinite(v) or v <= 0):
            raise ValueError(f"video.fps_override must be finite and > 0, got {v}")
        return v

    @field_validator("speed")
    @classmethod
    def _speed_non_negative(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"video.speed must be finite and >= 0, got {v}")
        return v

    @field_validator("loop")
    @classmethod
    def _loop_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"video.loop must be >= 0 (0 = forever), got {v}")
        return v


class SyntheticConfig(_StrictModel):
    """Synthetic pallet-pass generator.

    Decodability is governed by two dimensionless ratios, not pixel knobs:

    - **px/module** (``px_per_module_range``): the optics envelope at the
      real 3–15 ft read distance.
    - **blur in modules**: ``speed_mps * exposure_s / module_size_m``,
      derived from speed and exposure. ``exposure_fraction`` defaults to
      the real locked global-shutter operating point (~1 ms at 30 fps);
      raise it as a stress knob.

    Pixel scale is derived (``px_per_meter = px_per_module / module_size_m``),
    so the ratios hold constant if frame size changes.
    """

    width: int = 1280
    height: int = 720
    fps: float = 30.0
    seed: int = 42
    num_passes: int = 20
    speed_mph_range: tuple[float, float] = (2.0, 10.0)
    angle_deg_range: tuple[float, float] = (0.0, 35.0)
    px_per_module_range: tuple[float, float] = (3.0, 6.0)
    module_size_mm: float = 5.0
    exposure_fraction: float = 0.03  # ~1 ms shutter at 30 fps
    contrast_range: tuple[float, float] = (0.45, 1.0)
    noise_sigma_range: tuple[float, float] = (2.0, 8.0)
    lighting_gradient_max: float = 30.0
    occlusion_max_frac: float = 0.15
    symbologies: list[Symbology] = Field(
        default_factory=lambda: [Symbology.QR, Symbology.DATAMATRIX]
    )
    idle_s_range: tuple[float, float] = (0.3, 1.5)
    realtime: bool = False
    payload_prefix: str = "PLT-"
    #: Travel directions a pass may take across the frame (one chosen per pass).
    #: Default ["right"] preserves the original left->right behavior (tests rely
    #: on it); the demo passes a varied set. Allowed: right left up down
    #: upright upleft downright downleft.
    directions: list[str] = Field(default_factory=lambda: ["right"])
    #: Max simultaneously-composited passes (CameraInjectionSource only). 1 =
    #: one code at a time (default; tests rely on it). >1 shows MULTIPLE moving
    #: codes at once for multi-object demos (decode + preview already render a
    #: box per code). NOTE: concurrent codes share one motion segment, so an
    #: undecoded co-located code is not miss-flagged — keep them decodable.
    max_concurrent: int = 1

    @field_validator(
        "speed_mph_range",
        "angle_deg_range",
        "px_per_module_range",
        "contrast_range",
        "noise_sigma_range",
        "idle_s_range",
    )
    @classmethod
    def _range_ordered(cls, v: tuple[float, float]) -> tuple[float, float]:
        if v[0] > v[1]:
            raise ValueError(f"range must be (low, high), got {v}")
        return v

    @field_validator("speed_mph_range")
    @classmethod
    def _speed_positive(cls, v: tuple[float, float]) -> tuple[float, float]:
        # Pass planning divides by px-per-frame, which is proportional to
        # speed; zero speed means a pass that never crosses the frame.
        if v[0] <= 0:
            raise ValueError(f"speed_mph_range must be > 0, got {v}")
        return v


class MotionAlgorithm(enum.StrEnum):
    FRAMEDIFF = "framediff"
    MOG2 = "mog2"


class MotionConfig(_StrictModel):
    """Cheap motion gating on downscaled grayscale."""

    algorithm: MotionAlgorithm = MotionAlgorithm.FRAMEDIFF
    downscale_width: int = 160
    diff_threshold: int = 12
    min_area_frac: float = 0.01
    # A 1-frame flash yields 2 active diffs (appear + disappear), so true
    # debounce needs 3 consecutive active frames. Opens are backdated.
    open_frames: int = 3
    quiet_frames: int = 8  # consecutive quiet frames to close a segment
    roi_pad_px: int = 32
    #: Static/idle scan (user request): 0 (default) = purely motion-gated
    #: (CPU-cheap; static codes are NOT read). Set > 0 to ALSO full-frame-decode
    #: every N seconds while no motion segment is open, so a STOPPED pallet /
    #: static code is still read + shown. These idle reads are additive (shown on
    #: the dashboard + counted as decode.idle_reads) and never feed the
    #: motion-segment pass/miss accounting.
    idle_scan_s: float = 0.0
    #: TIER 2 multi-object tracking (opt-in). "single" (default) is the
    #: historical whole-mask-union gate — ONE segment at a time — byte-for-byte
    #: unchanged. "multi" segments the motion mask into per-object blobs,
    #: associates them across frames (greedy IoU + centroid), and runs an
    #: INDEPENDENT open/close debounce + decode + pass/miss accounting per
    #: track, so two pallets crossing the zone at once are each accounted for
    #: (a decoded pallet can no longer swallow a co-located undecoded one's
    #: MissEvent). ``min_area_frac`` stays the WHOLE-MASK floor in both modes.
    tracking: Literal["single", "multi"] = "single"
    #: Multi mode: cap on simultaneously-tracked blobs per frame (largest by
    #: area win); bounds the per-frame association cost.
    track_max_objects: int = 8
    #: Multi mode: minimum IoU for a blob<->track association candidate.
    track_iou_gate: float = 0.2
    #: Multi mode: centroid-distance association fallback, as a fraction of the
    #: frame diagonal (covers fast objects whose boxes no longer overlap).
    track_centroid_max_frac: float = 0.15
    #: Multi mode: per-blob area floor as a fraction of the (downscaled) mask
    #: area — rejects noise specks that the whole-mask floor would let through.
    track_min_blob_area_frac: float = 0.004
    #: Multi mode: a split/merge ambiguity must persist this many consecutive
    #: frames before it is committed (anti-churn hysteresis).
    track_merge_hysteresis_frames: int = 4
    #: Multi mode: morphological-close kernel as a fraction of downscale_width,
    #: applied to the mask before connected-components so ONE object's fragmented
    #: motion blob coalesces into a SINGLE component instead of minting several
    #: churny micro-tracks (each of which opens + closes as a spurious miss).
    #: Sized to bridge intra-object gaps without fusing separate objects; 0 off.
    track_close_kernel_frac: float = 0.04

    @field_validator("track_max_objects")
    @classmethod
    def _max_objects_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"motion.track_max_objects must be >= 1, got {v}")
        return v

    @field_validator(
        "track_iou_gate",
        "track_centroid_max_frac",
        "track_min_blob_area_frac",
    )
    @classmethod
    def _frac_unit_interval(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError(f"motion track fraction must be in (0, 1], got {v}")
        return v

    @field_validator("track_merge_hysteresis_frames")
    @classmethod
    def _hysteresis_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"motion.track_merge_hysteresis_frames must be >= 1, got {v}"
            )
        return v

    @field_validator("track_close_kernel_frac")
    @classmethod
    def _close_frac_unit(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(
                f"motion.track_close_kernel_frac must be in [0, 1], got {v}"
            )
        return v


class ExecutorKind(enum.StrEnum):
    THREAD = "thread"
    PROCESS = "process"


class DecodeEngineKind(enum.StrEnum):
    """Which decode backend the cascade uses."""

    LEGACY = "legacy"  # pyzbar (QR) + pylibdmtx (DM) cascade — the default
    ZXING = "zxing"  # zxing-cpp: one call, both symbologies, more robust


class DecodeConfig(_StrictModel):
    """Budget-aware decode cascade."""

    symbology_priority: list[Symbology] = Field(
        default_factory=lambda: [Symbology.QR, Symbology.DATAMATRIX]
    )
    engine: DecodeEngineKind = DecodeEngineKind.LEGACY
    frame_budget_ms: float = 50.0
    dm_timeout_ms: int = 40
    executor: ExecutorKind = ExecutorKind.THREAD
    workers: int = 2
    fallback_after_frames: int = 4  # undecoded frames before preprocessing variants
    confirmations: int = 1
    #: Optional payload-shape gate (REVIEW DEC-01): a regex a decoded payload
    #: must match to count as a real pass — the defense against decoder
    #: false-positives becoming "phantom" passes. None = permissive (only empty/
    #: control-byte garbage is dropped, no behavior change). For a trial, set to
    #: your label format, e.g. "^PLT-\\d{6}$". Non-matching decodes are dropped
    #: and counted as decode.spurious_rejected.
    payload_pattern: str | None = None
    #: Minimum Data Matrix payload length accepted by the default gate (when
    #: no payload_pattern is configured). pylibdmtx's characteristic
    #: false-positive on noisy crops is a SHORT printable string (e.g. "F'm"),
    #: which the control-byte check cannot catch; real placard payloads are
    #: much longer. Applies to DATAMATRIX results only — QR is not affected.
    dm_min_payload_len: int = Field(default=4, ge=1)


class DedupConfig(_StrictModel):
    window_s: float = 12.0


class BufferConfig(_StrictModel):
    """Rolling pre/post evidence buffer horizon (seconds of source time)."""

    pre_s: float = 2.0
    post_s: float = 2.0


class EvidenceConfig(_StrictModel):
    dir: Path = Path("data/evidence")
    frame_stride: int = 3
    jpeg_quality: int = 85
    max_total_mb: float = 500.0
    max_age_days: float = 14.0


class ConsoleSinkConfig(_StrictModel):
    enabled: bool = True


class JsonlSinkConfig(_StrictModel):
    enabled: bool = True
    path: Path = Path("data/events.jsonl")


class SqliteSinkConfig(_StrictModel):
    enabled: bool = True
    path: Path = Path("data/palletscan.db")


class RetryConfig(_StrictModel):
    """Exponential backoff for the HTTP uploader (jittered, success resets)."""

    base_s: float = 1.0
    cap_s: float = 60.0

    @field_validator("base_s", "cap_s")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"retry delays must be > 0, got {v}")
        return v


class HttpSinkConfig(_StrictModel):
    """Store-and-forward HTTP POST sink (offline-first; see http_sink.py).

    Delivery contract: one event per POST, body = event JSON, any 2xx is
    the ack; at-least-once, so the receiver dedupes on ``event_id``.
    """

    enabled: bool = False
    url: str = "http://127.0.0.1:8808/events"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 5.0
    outbox_path: Path = Path("data/outbox.db")
    retry: RetryConfig = Field(default_factory=RetryConfig)
    max_mb: float = 200.0
    max_age_days: float = 14.0


class SinksConfig(_StrictModel):
    console: ConsoleSinkConfig = Field(default_factory=ConsoleSinkConfig)
    jsonl: JsonlSinkConfig = Field(default_factory=JsonlSinkConfig)
    sqlite: SqliteSinkConfig = Field(default_factory=SqliteSinkConfig)
    http: HttpSinkConfig = Field(default_factory=HttpSinkConfig)


class MetricsConfig(_StrictModel):
    """Metrics windows and reservoir sizing (see palletscan/metrics.py)."""

    window_s: float = 60.0  # wall-clock window for the fps rate
    latency_samples: int = 2048  # decode wall-time reservoir size

    @field_validator("window_s")
    @classmethod
    def _window_positive(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError(f"metrics.window_s must be >= 1, got {v}")
        return v

    @field_validator("latency_samples")
    @classmethod
    def _samples_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"metrics.latency_samples must be >= 1, got {v}")
        return v


class WebConfig(_StrictModel):
    """Dashboard server (FastAPI/uvicorn). Localhost-bound by default; no
    auth (spec §12 — note for future work, do not expose beyond the host)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    preview_fps: float = 10.0  # MJPEG pacing per client
    preview_quality: int = 80  # JPEG encode quality
    preview_width: int = 640  # downscale target for the live view
    #: Cosmetic brightness multiplier for the live view ONLY (does not touch
    #: capture / exposure / decode) — brighten a dim unlit scene without the
    #: fps / lag / noise cost of raising exposure or gain. 1.0 = off.
    preview_gain: float = 1.0

    @field_validator("port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        # 0 = ephemeral (the OS picks a free port; the CLI prints it).
        if not 0 <= v <= 65535:
            raise ValueError(f"web.port must be 0-65535, got {v}")
        return v

    @field_validator("preview_fps")
    @classmethod
    def _fps_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"web.preview_fps must be finite and > 0, got {v}")
        return v

    @field_validator("preview_quality")
    @classmethod
    def _quality_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError(f"web.preview_quality must be 1-100, got {v}")
        return v

    @field_validator("preview_width")
    @classmethod
    def _width_positive(cls, v: int) -> int:
        if v < 16:
            raise ValueError(f"web.preview_width must be >= 16, got {v}")
        return v


class ReportConfig(_StrictModel):
    """Trial reporting inputs."""

    #: CSV of expected pallet payloads (first column); fallback used when
    #: no manifest has been uploaded through the dashboard.
    manifest_path: Path | None = None


class LogFileConfig(_StrictModel):
    """Rotating JSONL diagnostics file (writer commands only).

    ``run``/``synth``/``replay`` install the file handler **after** the
    instance lock is held: ``doRollover``'s rename fails on Windows when
    another process holds the file open, so lock scope == file-logging
    scope makes single-writer rotation an invariant. Total size cap is
    ``max_mb * (backups + 1)``; files older than ``max_age_days`` are
    pruned at handler install (``restarts.jsonl`` is always spared).
    """

    enabled: bool = True
    dir: Path = Path("data/logs")
    max_mb: float = 20.0
    backups: int = 5
    max_age_days: float = 14.0

    @field_validator("max_mb")
    @classmethod
    def _max_mb_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"logging.file.max_mb must be finite and > 0, got {v}")
        return v

    @field_validator("backups")
    @classmethod
    def _backups_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"logging.file.backups must be >= 1, got {v}")
        return v

    @field_validator("max_age_days")
    @classmethod
    def _age_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(
                f"logging.file.max_age_days must be finite and > 0, got {v}"
            )
        return v


class LoggingConfig(_StrictModel):
    level: str = "INFO"
    file: LogFileConfig = Field(default_factory=LogFileConfig)

    @field_validator("level")
    @classmethod
    def _level_known(cls, v: str) -> str:
        # An unknown name would otherwise blow up inside setup_logging's
        # setLevel with a raw traceback (exit 1); making it a validation
        # error keeps it on the documented exit-2 fix-the-config path.
        name = v.upper()
        known = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if name not in known:
            raise ValueError(
                f"logging.level must be one of {sorted(known)}, got {v!r}"
            )
        return name


class LockConfig(_StrictModel):
    """Single-instance lock (see ``reliability/instance_lock.py``).

    Held by the writer commands (``run``/``synth``/``replay``) for the
    process lifetime; scoped per data-dir so a multi-station box or a
    parallel dev run with its own ``--data-dir`` coexists by design.
    Contention exits with code 4.
    """

    path: Path = Path("data/palletscan.lock")


class Backend(enum.StrEnum):
    """Capture backend selector. ``auto`` picks the platform default
    (DSHOW on Windows, AVFoundation on macOS); the per-backend control
    quirks live as data in :mod:`palletscan.sources.controls`."""

    AUTO = "auto"
    DSHOW = "dshow"
    MSMF = "msmf"
    AVFOUNDATION = "avfoundation"
    #: DirectShow SampleGrabber via pygrabber — for mono (Y8/Y12) cameras that
    #: OpenCV's MSMF/DSHOW backends cannot read (e.g. See3CAM_37CUGM). See
    #: palletscan.sources.pygrabber_capture.
    PYGRABBER = "pygrabber"


class CameraSettings(_StrictModel):
    """UVC control values persisted per camera and re-applied on every
    (re)connect.

    Exposure and gain are stored as **raw backend values** — the number
    handed to ``CAP_PROP_EXPOSURE``/``CAP_PROP_GAIN`` — not milliseconds:
    DSHOW's log2 scaling vs MSMF's semantics make unit conversion a guess
    that cannot be verified without hardware. Calibrate records what
    *worked*; reconnect replays exactly that. ``None`` means "do not touch
    this control".
    """

    exposure_auto: bool = True
    exposure: float | None = None
    gain: float | None = None
    brightness: float | None = None


class CameraIdentity(_StrictModel):
    """Stable-hardware identity guard for the MSMF wrong-camera-swap risk.

    The color See3CAM_24CUG runs ``backend: msmf`` + a pinned index, so it
    is opened BY POSITION with no identity check: a replug reorder or an
    OBS Virtual Camera grabbing index 0 silently swaps in the wrong camera
    at the same 1920x1200, which the first-frame shape gate cannot catch.
    This block lets an operator pin the expected hardware fingerprint and
    choose how hard a drift is enforced.

    DORMANT BY DEFAULT: ``policy='warn'`` reproduces today's exact
    non-raising behavior (a single WARNING + a connect_mismatches bump,
    never an exception). Operators opt into ``'strict'`` deliberately,
    after ``calibrate --save`` has stamped a fingerprint to compare
    against. ``expected_vid_pid``/``expected_device_path`` are normally
    populated by calibrate, not hand-edited.
    """

    policy: Literal["strict", "warn", "off"] = "warn"
    #: ``xxxx:xxxx`` hex (vid:pid), normalized to lowercase. None = not pinned.
    expected_vid_pid: str | None = None
    #: Full Windows DevicePath; the strongest fingerprint when present.
    expected_device_path: str | None = None

    @field_validator("expected_vid_pid")
    @classmethod
    def _vid_pid_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        norm = v.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", norm):
            raise ValueError(
                f"expected_vid_pid must be 'xxxx:xxxx' hex (vid:pid), got {v!r}"
            )
        return norm


class CameraConfig(_StrictModel):
    """One physical camera, identified by device-name substring (never a
    bare index — indexes shuffle on reboot/replug)."""

    id: str
    name: str
    backend: Backend = Backend.AUTO
    fourcc: str | None = None  # None = leave the device's default format
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    convert_rgb: bool = True
    #: Last-resort index when the platform yields no device names.
    fallback_index: int | None = None
    read_fail_limit: int = 5
    #: Achieved-fps sample seconds per (re)connect; 0 disables.
    connect_verify_s: float = 1.0
    settings: CameraSettings = Field(default_factory=CameraSettings)
    #: MSMF wrong-camera-swap guard; dormant (warn) by default.
    identity: CameraIdentity = Field(default_factory=CameraIdentity)

    @field_validator("id", "name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("cameras[].id and name must be non-empty")
        return v

    @field_validator("fourcc")
    @classmethod
    def _fourcc_four_chars(cls, v: str | None) -> str | None:
        if v is not None and len(v) != 4:
            raise ValueError(f"fourcc must be exactly 4 characters, got {v!r}")
        return v

    @field_validator("width", "height")
    @classmethod
    def _dim_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(f"width/height must be > 0, got {v}")
        return v

    @field_validator("fps")
    @classmethod
    def _fps_positive(cls, v: float | None) -> float | None:
        if v is not None and (not math.isfinite(v) or v <= 0):
            raise ValueError(f"cameras[].fps must be finite and > 0, got {v}")
        return v

    @field_validator("fallback_index")
    @classmethod
    def _index_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"fallback_index must be >= 0, got {v}")
        return v

    @field_validator("read_fail_limit")
    @classmethod
    def _limit_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"read_fail_limit must be >= 1, got {v}")
        return v

    @field_validator("connect_verify_s")
    @classmethod
    def _verify_non_negative(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"connect_verify_s must be finite and >= 0, got {v}")
        return v


class WatchdogConfig(_StrictModel):
    """Stall detection and reconnect policy for live camera sources.

    The watchdog never gives up by default (process exit cannot fix an
    unplugged camera); the two escalation valves are ``max_outage_s``
    (None = off) and ``max_zombie_readers`` — abandoned reader threads
    stuck in hung ``read()`` calls are the wedged-USB-stack signature,
    and only a process restart resets a wedged stack (exit code 3).
    """

    stall_timeout_s: float = 2.0
    retry: RetryConfig = Field(
        default_factory=lambda: RetryConfig(base_s=0.5, cap_s=15.0)
    )
    max_outage_s: float | None = None
    max_zombie_readers: int = 3

    @field_validator("stall_timeout_s")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"watchdog.stall_timeout_s must be > 0, got {v}")
        return v

    @field_validator("max_outage_s")
    @classmethod
    def _outage_positive(cls, v: float | None) -> float | None:
        if v is not None and (not math.isfinite(v) or v <= 0):
            raise ValueError(f"watchdog.max_outage_s must be > 0 or null, got {v}")
        return v

    @field_validator("max_zombie_readers")
    @classmethod
    def _zombies_non_negative(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"watchdog.max_zombie_readers must be >= 1, got {v}")
        return v


class StationPolicy(enum.StrEnum):
    """What a multi-camera (A/B) station does when ONE arm's pipeline fails."""

    #: Stop the whole station so the supervisor restarts everything. Keeps an
    #: A/B comparison valid (a half-running trial silently biases it) — the
    #: right default for science / a matched A/B validation run.
    STOP_ALL = "stop_all"
    #: Let the failed arm stop in isolation while the healthy arm(s) keep
    #: scanning and publishing — one camera beats none for an unattended
    #: PRODUCTION station (REVIEW REL-1).
    CONTINUE_OTHERS = "continue_others"


class StationConfig(_StrictModel):
    """Multi-camera (A/B) station behavior."""

    #: stop_all preserves the existing fail-fast default; continue_others is the
    #: availability-first option for an unattended deployment.
    on_arm_failure: StationPolicy = StationPolicy.STOP_ALL


class AppConfig(_StrictModel):
    source: SourceConfig = Field(default_factory=SourceConfig)
    synthetic: SyntheticConfig = Field(default_factory=SyntheticConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    station: StationConfig = Field(default_factory=StationConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    decode: DecodeConfig = Field(default_factory=DecodeConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    sinks: SinksConfig = Field(default_factory=SinksConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    lock: LockConfig = Field(default_factory=LockConfig)
    #: Per-source capture->pipeline frame buffer depth (drops oldest when full).
    #: 64 absorbs decode bursts for account-for-everything; a live-view/demo wants
    #: it SMALL — a FIFO backlog is queue_depth/fps of latency when the pipeline
    #: can't keep up (the heavy mono cam at 72fps shows ~1s lag at depth 64).
    #: ge=1 because queue.Queue treats maxsize <= 0 as INFINITE, which would
    #: silently turn DroppingQueue's bounded drop-oldest into an unbounded FIFO.
    frame_queue_size: int = Field(default=64, ge=1)

    @model_validator(mode="after")
    def _unique_camera_ids(self) -> "AppConfig":
        ids = [c.id for c in self.cameras]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate cameras[].id: {dupes}")
        if self.source.cameras is not None:
            missing = [i for i in self.source.cameras if i not in ids]
            if missing:
                raise ValueError(
                    f"source.cameras entries not in cameras[]: {missing}; "
                    f"configured ids: {ids}"
                )
        return self


def resolve_camera(cfg: AppConfig) -> CameraConfig:
    """Resolve ``source.camera`` to a ``cameras[]`` entry.

    A single entry is the default; multiple entries require an explicit
    selector. Errors always list what *is* configured.
    """
    ids = [c.id for c in cfg.cameras]
    if not cfg.cameras:
        raise ValueError(
            "source.type=camera requires at least one cameras[] entry "
            "(create one with `palletscan calibrate --camera <id> "
            "--name '<device-name substring>' --save --config <file>`)"
        )
    if cfg.source.camera is None:
        if len(cfg.cameras) == 1:
            return cfg.cameras[0]
        raise ValueError(
            f"multiple cameras configured ({ids}); set source.camera "
            "or pass --camera to pick one"
        )
    for cam in cfg.cameras:
        if cam.id == cfg.source.camera:
            return cam
    raise ValueError(
        f"source.camera {cfg.source.camera!r} not found; configured ids: {ids}"
    )


def upsert_camera_yaml(path: Path | str, camera: CameraConfig) -> None:
    """Replace-or-append one ``cameras[]`` entry in a YAML config file.

    Narrow and targeted: every other key in the file is preserved (key
    order survives; comments do not — ``config/default.yaml`` remains the
    commented reference). The merged result is validated as a full
    AppConfig **before** anything touches disk, so a corrupt save can
    never brick the station; the write is tmp-file + ``os.replace`` with
    a timestamped ``.bak`` of the original.
    """
    path = Path(path)
    raw: dict = {}
    existed = path.is_file()
    if existed:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError(
                f"config root must be a mapping, got {type(loaded).__name__}"
            )
        raw = loaded
    entry = camera.model_dump(mode="json")
    cams = raw.get("cameras")
    if cams is None:
        cams = []
    if not isinstance(cams, list):
        raise ValueError("cameras must be a list")
    for i, existing in enumerate(cams):
        if isinstance(existing, dict) and existing.get("id") == camera.id:
            cams[i] = entry
            break
    else:
        cams.append(entry)
    raw["cameras"] = cams
    AppConfig.model_validate(raw)  # refuse to write anything unloadable

    stamp = time.strftime("%Y%m%dT%H%M%S")
    header = f"# updated by palletscan calibrate {stamp}\n"
    body = header + yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if existed:
        backup = path.with_name(f"{path.name}.{stamp}.bak")
        backup.write_bytes(path.read_bytes())
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load config from YAML; missing file or empty YAML yields full defaults."""
    if path is None:
        return AppConfig()
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if data is None:
        return AppConfig()
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping, got {type(data).__name__}")
    return AppConfig.model_validate(data)


def apply_overrides(
    cfg: AppConfig,
    *,
    num_passes: int | None = None,
    seed: int | None = None,
    data_dir: Path | str | None = None,
) -> AppConfig:
    """Return a copy of ``cfg`` with CLI-style overrides applied.

    ``data_dir`` rebases evidence and file-sink paths under one directory.
    """
    update: dict = {}
    synth: dict = {}
    if num_passes is not None:
        synth["num_passes"] = num_passes
    if seed is not None:
        synth["seed"] = seed
    if synth:
        update["synthetic"] = cfg.synthetic.model_copy(update=synth)
    if data_dir is not None:
        base = Path(data_dir)
        update["evidence"] = cfg.evidence.model_copy(update={"dir": base / "evidence"})
        update["sinks"] = cfg.sinks.model_copy(
            update={
                "jsonl": cfg.sinks.jsonl.model_copy(
                    update={"path": base / "events.jsonl"}
                ),
                "sqlite": cfg.sinks.sqlite.model_copy(
                    update={"path": base / "palletscan.db"}
                ),
                "http": cfg.sinks.http.model_copy(
                    update={"outbox_path": base / "outbox.db"}
                ),
            }
        )
        update["logging"] = cfg.logging.model_copy(
            update={
                "file": cfg.logging.file.model_copy(update={"dir": base / "logs"})
            }
        )
        update["lock"] = cfg.lock.model_copy(
            update={"path": base / "palletscan.lock"}
        )
    return cfg.model_copy(update=update) if update else cfg
