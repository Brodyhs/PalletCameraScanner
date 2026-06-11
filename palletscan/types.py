"""Core data types shared across the pipeline.

Frames carry a *source clock* timestamp (``ts``): for live cameras it is a
monotonic capture time, for synthetic/replay sources it is simulated
(``frame_index / fps``). All downstream time logic (dedup windows, rolling
buffer eviction, segment timing) keys off ``Frame.ts`` — never wall clock —
so accelerated and non-realtime runs behave identically to realtime.

Frames hold numpy arrays by reference; they are read-only by convention
after creation.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import numpy as np


class Symbology(enum.StrEnum):
    """Supported barcode symbologies."""

    QR = "qr"
    DATAMATRIX = "datamatrix"


@dataclass(frozen=True, slots=True)
class Roi:
    """Axis-aligned region of interest in full-resolution pixel coordinates."""

    x: int
    y: int
    w: int
    h: int

    def clamp(self, shape: tuple[int, ...]) -> "Roi":
        """Clamp to an image of the given (height, width, ...) shape."""
        ih, iw = int(shape[0]), int(shape[1])
        x = max(0, min(self.x, iw - 1))
        y = max(0, min(self.y, ih - 1))
        w = max(1, min(self.w, iw - x))
        h = max(1, min(self.h, ih - y))
        return Roi(x, y, w, h)

    def pad(self, px: int) -> "Roi":
        """Grow the ROI by ``px`` on every side (unclamped)."""
        return Roi(self.x - px, self.y - px, self.w + 2 * px, self.h + 2 * px)

    def crop(self, image: np.ndarray) -> np.ndarray:
        """Return the view of ``image`` covered by this ROI (clamped)."""
        c = self.clamp(image.shape)
        return image[c.y : c.y + c.h, c.x : c.x + c.w]


@dataclass(frozen=True, slots=True)
class Frame:
    """A single grayscale frame from any FrameSource."""

    image: np.ndarray  # 2-D uint8 grayscale
    ts: float  # source clock, seconds
    frame_index: int
    source_id: str


@dataclass(frozen=True, slots=True)
class MotionResult:
    """Per-frame output of the MotionGate."""

    active: bool
    candidate_id: str | None
    roi: Roi | None
    motion_frac: float


class SegmentKind(enum.StrEnum):
    """Lifecycle transitions for a motion pass-candidate segment."""

    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class SegmentEvent:
    """Internal pipeline signal marking a segment open/close (not a bus event)."""

    kind: SegmentKind
    candidate_id: str
    frame_index: int
    ts: float


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """A single successful decode of a symbol within one frame."""

    payload: str
    symbology: Symbology
    roi: Roi
    frame_index: int
    ts: float
    source_id: str
    decoder: str  # e.g. "pyzbar", "pyzbar+clahe", "pylibdmtx"
    latency_ms: float


@dataclass(frozen=True, slots=True)
class PassEvent:
    """Business event: one pallet pass, deduplicated by payload."""

    payload: str
    symbology: Symbology
    first_seen_ts: float
    last_seen_ts: float
    decode_count: int
    cameras: dict[str, int]  # source_id -> decode count
    best_frame: tuple[str, int]  # (source_id, frame_index) of first decode
    candidate_ids: list[str]
    event_id: str
    wall_time_iso: str

    @property
    def kind(self) -> str:
        return "pass"


@dataclass(frozen=True, slots=True)
class MissEvent:
    """Exception event: a motion segment ended with zero decodes."""

    candidate_id: str
    source_id: str
    start_ts: float
    end_ts: float
    first_frame: int
    last_frame: int
    evidence_dir: str
    evidence_frame_count: int
    event_id: str
    wall_time_iso: str

    @property
    def kind(self) -> str:
        return "miss"


Event = PassEvent | MissEvent


@dataclass(frozen=True, slots=True)
class GroundTruthRecord:
    """Synthetic-source ground truth for one generated pass.

    ``params`` always includes ``px_per_module`` and ``blur_modules`` — the
    two dimensionless ratios that govern decodability — so any acceptance
    failure shows where in the envelope it broke.
    """

    pass_id: int
    payload: str
    symbology: Symbology
    first_frame: int
    last_frame: int
    params: dict[str, Any] = field(default_factory=dict)
