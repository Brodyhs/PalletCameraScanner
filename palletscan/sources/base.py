"""FrameSource abstraction — the seam that keeps the pipeline source-agnostic.

Later phases add ``CameraSource`` (live UVC) and ``VideoFileSource`` (replay)
behind this same interface; downstream stages never know the difference.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from palletscan.types import Frame


class FrameSource(ABC):
    """Produces a stream of grayscale frames with source-clock timestamps."""

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Stable identifier for this source (used in events and stats)."""

    @property
    def nominal_fps(self) -> float | None:
        """Nominal frame rate if the source knows it (e.g. buffer sizing)."""
        return None

    @property
    def live(self) -> bool:
        """True for live capture (drop frames under backpressure rather than
        stall the device); False for finite replay (block, never drop)."""
        return False

    @abstractmethod
    def frames(self) -> Iterator[Frame]:
        """Yield frames in order. Single-use **per connection**: call once
        per source instance — except that after a successful
        ``Reopenable.reopen()`` the reliability watchdog may call it again
        for a fresh stream; all other callers still call it exactly once.

        A finite source (synthetic, replay) simply stops iterating; a live
        source iterates until :meth:`close`.
        """

    def close(self) -> None:
        """Release any underlying resources. Idempotent."""
