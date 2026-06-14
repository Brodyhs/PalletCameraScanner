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
from palletscan.types import Frame, now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Where a burst landed and how many frames it holds.

    ``directory=None`` + ``error`` set means the burst could not be stored
    at all (e.g. full disk): the caller must still emit its MissEvent —
    evidence-less and flagged — never swallow it (REVIEW_SYSTEM_0c30c77
    finding 1: disk exhaustion degrades loudly, not silently).
    """

    directory: Path | None
    frame_count: int
    error: str | None = None


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
        """Write every ``frame_stride``-th frame as JPEG plus ``meta.json``.

        Never raises on storage failure: an OSError anywhere on this path
        degrades to a flagged EvidenceRef so the MissEvent it documents is
        still emitted (the miss IS the product; the burst is supporting
        evidence).
        """
        cfg = self._cfg
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = self._root / day / candidate_id
        # Candidate ids restart per process; a same-day restart must never
        # merge into (and byte-overwrite) an existing burst's directory
        # (finding 5). The run-token in the id makes collisions unexpected;
        # this guard keeps a collision loud and lossless anyway.
        suffix = 0
        try:
            while target.exists():
                suffix += 1
                target = self._root / day / f"{candidate_id}-r{suffix}"
        except OSError:
            pass  # probing failed; fall through to mkdir, which decides
        if suffix:
            log.warning(
                "evidence dir for %s already exists; writing to %s instead "
                "of overwriting", candidate_id, target,
            )
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error(
                "evidence dir %s could not be created (%r); emitting "
                "evidence-less miss", target, exc,
            )
            return EvidenceRef(directory=None, frame_count=0, error=repr(exc))
        kept = frames[:: max(1, cfg.frame_stride)]
        written: list[Frame] = []
        error: str | None = None
        for f in kept:
            path = target / f"frame_{f.frame_index:08d}.jpg"
            # cv2.imwrite signals failure (full disk, lost permission,
            # encoder error) by returning False, not raising.
            if cv2.imwrite(
                str(path), f.image, [cv2.IMWRITE_JPEG_QUALITY, cfg.jpeg_quality]
            ):
                written.append(f)
            else:
                log.error("evidence frame write failed: %s", path)
        if len(written) < len(kept):
            # Lost frames are an evidence failure too: the meta.json that
            # follows can still write on a near-full disk (it is tiny), so
            # without flagging here the miss would report fully-evidenced
            # with frames silently dropped, defeating the loud-degradation
            # guarantee the error field exists for (REVIEW finding 1).
            error = (
                f"{len(kept) - len(written)}/{len(kept)} "
                "evidence frame(s) failed to write"
            )
            log.error(
                "evidence burst %s incomplete: %d/%d frames written",
                candidate_id,
                len(written),
                len(kept),
            )
        payload = {
            "candidate_id": candidate_id,
            "frame_count": len(written),
            "frame_indices": [f.frame_index for f in written],
            "ts_range": [frames[0].ts, frames[-1].ts] if frames else None,
            "written_utc": now_iso(),
            **meta,
        }
        try:
            (target / "meta.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            # Append, never clobber: a burst that lost frames AND its meta
            # (the same full disk causes both) must report both causes, not
            # just the last one observed.
            meta_err = repr(exc)
            error = meta_err if error is None else f"{error}; meta.json: {meta_err}"
            log.error(
                "evidence meta.json for %s failed (%r); burst kept %d "
                "frame(s) without metadata", candidate_id, exc, len(written),
            )
        self.prune(keep=target)
        return EvidenceRef(
            directory=target, frame_count=len(written), error=error
        )

    def _candidate_dirs(self) -> list[tuple[Path, float]]:
        """All candidate directories with their mtime, oldest first.

        Tolerates entries vanishing mid-scan (external cleanup, another
        process pruning): a stat race must degrade to a smaller listing,
        never abort the miss write that triggered the prune.
        """
        dirs: list[tuple[Path, float]] = []
        try:
            days = [d for d in self._root.iterdir() if d.is_dir()]
        except OSError:
            return []
        for day in days:
            try:
                children = list(day.iterdir())
            except OSError:
                continue
            for c in children:
                try:
                    if c.is_dir():
                        dirs.append((c, c.stat().st_mtime))
                except OSError:
                    continue
        return sorted(dirs, key=lambda it: it[1])

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        try:
            for f in path.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        return total

    def prune(self, keep: Path | None = None) -> None:
        """Enforce the age cap, then the total-size cap (oldest first).

        ``keep`` is never deleted: a burst must survive its own post-write
        prune so the MissEvent's evidence_dir stays valid. Its size still
        counts toward the total, so older bursts go first.
        """
        cfg = self._cfg
        now = time.time()
        survivors: list[Path] = []
        for d, mtime in self._candidate_dirs():
            if d != keep and now - mtime > cfg.max_age_days * 86400:
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
            if d == keep:
                continue
            total -= sizes[d]
            shutil.rmtree(d, ignore_errors=True)
            log.info("evidence pruned by size: %s", d)
        # Drop empty day directories left behind — with the same OSError
        # tolerance as the scan helpers above: concurrent churn (a day
        # vanishing mid-scan, a racing writer repopulating one before the
        # rmdir) must degrade to a skipped sweep, never abort the miss
        # write that triggered this prune (ASSUMPTIONS #43 amendment).
        try:
            days = list(self._root.iterdir())
        except OSError:
            return
        for day in days:
            try:
                if day.is_dir() and not any(day.iterdir()):
                    day.rmdir()
            except OSError:
                continue
