"""EvidenceWriter: burst layout, stride, metadata, pruning."""

from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path

import numpy as np
import pytest

from palletscan.config import EvidenceConfig
from palletscan.events.evidence import EvidenceWriter
from palletscan.types import Frame


def _frames(n: int) -> list[Frame]:
    rng = np.random.default_rng(0)
    return [
        Frame(
            image=rng.integers(0, 255, (60, 80), np.uint8),
            ts=i / 30.0,
            frame_index=i,
            source_id="cam0",
        )
        for i in range(n)
    ]


def test_burst_writes_strided_jpegs_and_meta(tmp_path: Path) -> None:
    cfg = EvidenceConfig(dir=tmp_path / "ev", frame_stride=3)
    writer = EvidenceWriter(cfg)
    ref = writer.write_burst("cam0-000001", _frames(10), {"reason": "no decode"})
    jpgs = sorted(ref.directory.glob("*.jpg"))
    assert len(jpgs) == 4  # ceil(10 / 3)
    assert ref.frame_count == 4
    meta = json.loads((ref.directory / "meta.json").read_text())
    assert meta["candidate_id"] == "cam0-000001"
    assert meta["reason"] == "no decode"
    assert meta["frame_indices"] == [0, 3, 6, 9]
    assert "cam0-000001" in str(ref.directory)


def test_prune_by_age(tmp_path: Path) -> None:
    cfg = EvidenceConfig(dir=tmp_path / "ev", max_age_days=1.0)
    writer = EvidenceWriter(cfg)
    old = writer.write_burst("cam0-000001", _frames(3), {})
    young = writer.write_burst("cam0-000002", _frames(3), {})
    # age the first burst 2 days
    two_days_ago = time.time() - 2 * 86400
    os.utime(old.directory, (two_days_ago, two_days_ago))
    writer.prune()
    assert not old.directory.exists()
    assert young.directory.exists()


def test_empty_day_cleanup_race_does_not_abort_miss_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7e4c22c review, finding 2 (reproduced there): prune()'s trailing
    empty-day cleanup had none of the OSError tolerance of the scan
    helpers, so concurrent dir churn (rmdir on a repopulated day) aborted
    write_burst after the pending miss was popped — silently eating the
    MissEvent that ASSUMPTIONS #43 claims can no longer be lost."""
    cfg = EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)
    writer = EvidenceWriter(cfg)
    (tmp_path / "ev" / "1999-01-01").mkdir()  # empty day: cleanup target

    def racing_rmdir(self: Path) -> None:
        raise OSError(errno.ENOTEMPTY, "Directory not empty (concurrent churn)")

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)
    ref = writer.write_burst("cam0-000009", _frames(3), {})  # must not raise
    assert ref.frame_count == 3
    assert (ref.directory / "meta.json").is_file()


def test_day_vanishing_mid_cleanup_scan_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same finding, other arm of the race: the day directory disappears
    between the root scan and the emptiness check."""
    cfg = EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)
    writer = EvidenceWriter(cfg)
    ghost = tmp_path / "ev" / "1999-01-02"
    ghost.mkdir()
    original_iterdir = Path.iterdir

    def vanishing_iterdir(self: Path):
        if self == ghost:
            raise FileNotFoundError(errno.ENOENT, "vanished mid-scan")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", vanishing_iterdir)
    ref = writer.write_burst("cam0-000010", _frames(3), {})  # must not raise
    assert ref.frame_count == 3


def test_uncreatable_burst_dir_degrades_to_flagged_ref(tmp_path: Path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 1 (mkdir raiser, evidence.py): an
    ENOSPC/PermissionError out of write_burst propagated into
    _finalize_miss AFTER the pending miss was destructively popped — the
    MissEvent reached no sink, ever. write_burst must degrade to a flagged
    EvidenceRef instead of raising."""
    from datetime import datetime, timezone

    cfg = EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)
    writer = EvidenceWriter(cfg)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # The day path is a FILE: creating the candidate dir under it raises
    # (NotADirectoryError is an OSError — same family as ENOSPC).
    (tmp_path / "ev" / day).write_text("not a directory")
    ref = writer.write_burst("cam0-r1-000001", _frames(3), {})  # must not raise
    assert ref.directory is None
    assert ref.frame_count == 0
    assert ref.error is not None


def test_meta_json_failure_keeps_frames_and_flags_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 1 (meta.json raiser, evidence.py):
    the second unguarded raiser — cv2.imwrite fails soft by design, but
    the meta.json write_text raised through the miss path."""
    cfg = EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)
    writer = EvidenceWriter(cfg)
    original_write_text = Path.write_text

    def enospc_for_meta(self: Path, *args, **kwargs):
        if self.name == "meta.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", enospc_for_meta)
    ref = writer.write_burst("cam0-r1-000002", _frames(3), {})  # must not raise
    assert ref.directory is not None
    assert ref.frame_count == 3  # the JPEGs landed
    assert len(list(ref.directory.glob("*.jpg"))) == 3
    assert ref.error is not None


def test_same_candidate_id_never_overwrites_existing_burst(
    tmp_path: Path,
) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 5 (reproduced there): candidate ids
    restart per process, and a same-day restart's first miss byte-overwrote
    the morning burst's JPEGs and replaced its meta.json — the dashboard
    then presented the afternoon pallet's frames as the morning pallet's
    evidence. A collision must land in a suffixed directory, losslessly."""
    cfg = EvidenceConfig(dir=tmp_path / "ev", frame_stride=1)
    writer = EvidenceWriter(cfg)
    first = writer.write_burst("cam0-000001", _frames(3), {"run": "morning"})
    assert first.directory is not None
    meta_before = (first.directory / "meta.json").read_bytes()
    jpg = sorted(first.directory.glob("*.jpg"))[0]
    jpg_before = jpg.read_bytes()

    second = writer.write_burst("cam0-000001", _frames(5), {"run": "afternoon"})
    assert second.directory is not None
    assert second.directory != first.directory
    assert second.directory.name == "cam0-000001-r1"
    assert (first.directory / "meta.json").read_bytes() == meta_before
    assert jpg.read_bytes() == jpg_before
    # a third collision keeps stepping the suffix
    third = writer.write_burst("cam0-000001", _frames(2), {})
    assert third.directory is not None
    assert third.directory.name == "cam0-000001-r2"


def test_prune_by_size_drops_oldest_first(tmp_path: Path) -> None:
    cfg = EvidenceConfig(
        dir=tmp_path / "ev", frame_stride=1, max_total_mb=0.02  # ~20 KB cap
    )
    writer = EvidenceWriter(cfg)
    refs = []
    for i in range(4):
        ref = writer.write_burst(f"cam0-{i:06d}", _frames(5), {})
        past = time.time() - (4 - i) * 1000
        os.utime(ref.directory, (past, past))
        refs.append(ref)
    writer.prune()
    survivors = [r for r in refs if r.directory.exists()]
    assert survivors, "pruning must not delete everything"
    assert len(survivors) < 4
    # the newest burst survives
    assert refs[-1].directory.exists()
