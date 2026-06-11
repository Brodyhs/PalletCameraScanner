"""EvidenceWriter: persist miss evidence (JPEG burst + metadata), capped.

Layout: ``<dir>/<YYYY-MM-DD>/<candidate_id>/frame_<index>.jpg`` + ``meta.json``.
Pruning runs on every write: oldest candidate directories are deleted until
the total size cap is met, and anything older than the age cap is removed.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from palletscan.config import EvidenceConfig
from palletscan.types import Frame

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Where a burst landed and how many frames it holds."""

    directory: Path
    frame_count: int


class EvidenceWriter:
    def __init__(self, cfg: EvidenceConfig) -> None:
        self._cfg = cfg
        self._root = Path(cfg.dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def write_burst(
        self,
        candidate_id: str,
        frames: list[Frame],
        meta: dict[str, Any],
    ) -> EvidenceRef:
        """Write every ``frame_stride``-th frame as JPEG plus ``meta.json``."""
        cfg = self._cfg
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = self._root / day / candidate_id
        target.mkdir(parents=True, exist_ok=True)
        kept = frames[:: max(1, cfg.frame_stride)]
        for f in kept:
            cv2.imwrite(
                str(target / f"frame_{f.frame_index:08d}.jpg"),
                f.image,
                [cv2.IMWRITE_JPEG_QUALITY, cfg.jpeg_quality],
            )
        payload = {
            "candidate_id": candidate_id,
            "frame_count": len(kept),
            "frame_indices": [f.frame_index for f in kept],
            "ts_range": [frames[0].ts, frames[-1].ts] if frames else None,
            "written_utc": datetime.now(timezone.utc).isoformat(),
            **meta,
        }
        (target / "meta.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        self.prune()
        return EvidenceRef(directory=target, frame_count=len(kept))

    def _candidate_dirs(self) -> list[Path]:
        """All candidate directories, oldest first (by mtime)."""
        dirs = [
            c
            for day in self._root.iterdir()
            if day.is_dir()
            for c in day.iterdir()
            if c.is_dir()
        ]
        return sorted(dirs, key=lambda d: d.stat().st_mtime)

    @staticmethod
    def _dir_size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    def prune(self) -> None:
        """Enforce the age cap, then the total-size cap (oldest first)."""
        cfg = self._cfg
        now = time.time()
        dirs = self._candidate_dirs()
        survivors = []
        for d in dirs:
            if now - d.stat().st_mtime > cfg.max_age_days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                log.info("evidence pruned by age: %s", d)
            else:
                survivors.append(d)
        sizes = {d: self._dir_size(d) for d in survivors}
        total = sum(sizes.values())
        cap = cfg.max_total_mb * 1024 * 1024
        for d in survivors:
            if total <= cap:
                break
            total -= sizes[d]
            shutil.rmtree(d, ignore_errors=True)
            log.info("evidence pruned by size: %s", d)
        # Drop empty day directories left behind.
        for day in self._root.iterdir():
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
