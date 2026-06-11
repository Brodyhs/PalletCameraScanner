"""Configuration models and YAML loading.

A single YAML file configures the whole system. Every field has a default,
so an empty (or absent) file yields a fully working synthetic-mode config.
Unknown keys are rejected to catch typos early.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from palletscan.types import Symbology


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceConfig(_StrictModel):
    """Which FrameSource to run. Phase 1: synthetic only."""

    type: Literal["synthetic"] = "synthetic"


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


class SinksConfig(_StrictModel):
    console: ConsoleSinkConfig = Field(default_factory=ConsoleSinkConfig)
    jsonl: JsonlSinkConfig = Field(default_factory=JsonlSinkConfig)
    sqlite: SqliteSinkConfig = Field(default_factory=SqliteSinkConfig)


class LoggingConfig(_StrictModel):
    level: str = "INFO"


class AppConfig(_StrictModel):
    source: SourceConfig = Field(default_factory=SourceConfig)
    synthetic: SyntheticConfig = Field(default_factory=SyntheticConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    decode: DecodeConfig = Field(default_factory=DecodeConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    sinks: SinksConfig = Field(default_factory=SinksConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


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
            }
        )
    return cfg.model_copy(update=update) if update else cfg
