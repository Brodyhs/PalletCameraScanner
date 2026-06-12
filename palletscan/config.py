"""Configuration models and YAML loading.

A single YAML file configures the whole system. Every field has a default,
so an empty (or absent) file yields a fully working synthetic-mode config.
Unknown keys are rejected to catch typos early.
"""

from __future__ import annotations

import enum
import math
import os
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


class ExecutorKind(enum.StrEnum):
    THREAD = "thread"
    PROCESS = "process"


class DecodeConfig(_StrictModel):
    """Budget-aware decode cascade."""

    symbology_priority: list[Symbology] = Field(
        default_factory=lambda: [Symbology.QR, Symbology.DATAMATRIX]
    )
    frame_budget_ms: float = 50.0
    dm_timeout_ms: int = 40
    executor: ExecutorKind = ExecutorKind.THREAD
    workers: int = 2
    fallback_after_frames: int = 4  # undecoded frames before preprocessing variants
    confirmations: int = 1


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


class LoggingConfig(_StrictModel):
    level: str = "INFO"


class Backend(enum.StrEnum):
    """Capture backend selector. ``auto`` picks the platform default
    (DSHOW on Windows, AVFoundation on macOS); the per-backend control
    quirks live as data in :mod:`palletscan.sources.controls`."""

    AUTO = "auto"
    DSHOW = "dshow"
    MSMF = "msmf"
    AVFOUNDATION = "avfoundation"


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


class AppConfig(_StrictModel):
    source: SourceConfig = Field(default_factory=SourceConfig)
    synthetic: SyntheticConfig = Field(default_factory=SyntheticConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    decode: DecodeConfig = Field(default_factory=DecodeConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    sinks: SinksConfig = Field(default_factory=SinksConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @model_validator(mode="after")
    def _unique_camera_ids(self) -> "AppConfig":
        ids = [c.id for c in self.cameras]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate cameras[].id: {dupes}")
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
            "(run `palletscan calibrate --save` to create one)"
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
    return cfg.model_copy(update=update) if update else cfg
