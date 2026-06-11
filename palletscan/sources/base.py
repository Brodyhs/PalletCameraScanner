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

    @abstractmethod
    def frames(self) -> Iterator[Frame]:
        """Yield frames in order. Single-use: call once per source instance.

        A finite source (synthetic, replay) simply stops iterating; a live
        source iterates until :meth:`close`.
        """

    def close(self) -> None:
        """Release any underlying resources. Idempotent."""
