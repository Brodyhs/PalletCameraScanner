"""EvidenceWriter: burst layout, stride, metadata, pruning."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

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
