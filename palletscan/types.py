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
from datetime import datetime, timezone
from typing import Any

import numpy as np


def now_iso() -> str:
    """Wall-clock UTC timestamp (ISO 8601) for event and evidence records.

    The single formatter keeps event ``wall_time_iso`` and evidence
    ``written_utc`` correlatable."""
    return datetime.now(timezone.utc).isoformat()


def iso_at(wall: float) -> str:
    """UNIX wall time -> the same ISO 8601 UTC format as :func:`now_iso`.

    Used to stamp events with the wall time of their source-clock instant
    (segment close), not of the deferred finalize that emitted them — an
    outage-deferred miss must land in the report window where the pallet
    passed (REVIEW finding b12)."""
    return datetime.fromtimestamp(wall, timezone.utc).isoformat()


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
    """A single grayscale frame from any FrameSource.

    ``discontinuity`` marks the first frame after a source recovery
    (watchdog reconnect): motion continuity across the gap is unknowable,
    so the gate must close any open segment before processing it. The
    timestamp itself stays monotonic across the gap (ASSUMPTIONS #29) —
    the flag is the boundary signal, not a time re-anchor.
    """

    image: np.ndarray  # 2-D uint8 grayscale
    ts: float  # source clock, seconds
    frame_index: int
    source_id: str
    discontinuity: bool = False


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
    """Business event: one pallet pass, deduplicated by payload.

    ``revision`` orders re-emissions of the same ``event_id`` (cross-camera
    merges, Phase 4): storage keeps the highest revision per id, so a stale
    pre-merge version arriving late can never overwrite a merged row.
    ``camera_detail`` carries per-camera timing for the A/B report;
    time-to-first-decode uses same-camera timestamps, so cross-camera clock
    skew cancels.
    """

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
    first_decode_ts: float | None = None
    #: source_id -> {first_seen_ts, first_decode_ts, last_seen_ts, decode_count}
    camera_detail: dict[str, dict[str, Any]] | None = None
    revision: int = 0

    @property
    def kind(self) -> str:
        return "pass"


@dataclass(frozen=True, slots=True)
class MissEvent:
    """Exception event: a motion segment ended with zero decodes.

    ``evidence_error`` is set (and ``evidence_dir`` may be ``""``) when the
    evidence burst could not be stored — full disk, lost permission. The
    miss is emitted anyway: losing the burst must never lose the event.
    """

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
    evidence_error: str | None = None

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
