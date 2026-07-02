"""tools/replay_bursts: offline re-score of recorded bursts, read-only.

Builds a recording directory in the SegmentRecorder on-disk layout
(``<day>/<candidate>/frame_*.jpg`` + ``meta.json``) with decodable and blank
bursts, then checks that replay classifies recoveries/regressions correctly,
attributes the decoder, and never mutates the recording dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from palletscan.config import AppConfig, DecodeEngineKind
from palletscan.sources.render import render_qr
from tools import replay_bursts as rb

_QR_CANVAS = 260  # comfortably decodable full-frame after JPEG


def _qr_frame(payload: str) -> np.ndarray:
    """A decodable QR centered on a white canvas."""
    sym = render_qr(payload, px_per_module=8.0).image
    canvas = np.full((_QR_CANVAS, _QR_CANVAS), 255, np.uint8)
    h, w = sym.shape
    y, x = (_QR_CANVAS - h) // 2, (_QR_CANVAS - w) // 2
    canvas[y : y + h, x : x + w] = sym
    return canvas


def _blank_frame() -> np.ndarray:
    return np.full((_QR_CANVAS, _QR_CANVAS), 255, np.uint8)


def _write_burst(
    root: Path,
    candidate_id: str,
    image: np.ndarray,
    *,
    outcome: str,
    payloads: list[str],
    n_frames: int = 3,
) -> None:
    """Persist one burst exactly as EvidenceWriter.write_burst does."""
    directory = root / "2026-07-02" / candidate_id
    directory.mkdir(parents=True, exist_ok=True)
    indices = list(range(n_frames))
    for i in indices:
        assert cv2.imwrite(
            str(directory / f"frame_{i:08d}.jpg"),
            image,
            [cv2.IMWRITE_JPEG_QUALITY, 95],
        )
    meta = {
        "candidate_id": candidate_id,
        "frame_count": n_frames,
        "frame_indices": indices,
        "schema": "recording/v1",
        "outcome": outcome,
        "payloads": payloads,
        "symbologies": ["qr"] * len(payloads),
        "source_id": "cam0",
        "segment_frames": [indices[0], indices[-1]],
    }
    (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


@pytest.fixture()
def recording_dir(tmp_path: Path) -> Path:
    root = tmp_path / "recordings"
    # A live miss that IS decodable -> a recovery candidate.
    _write_burst(root, "cam0-a", _qr_frame("PLT-RECOVER"), outcome="miss", payloads=[])
    # A live pass this config still reads -> unchanged.
    _write_burst(
        root, "cam0-b", _qr_frame("PLT-PASS"), outcome="pass", payloads=["PLT-PASS"]
    )
    # A genuine no-read (blank) -> stays a miss.
    _write_burst(root, "cam0-c", _blank_frame(), outcome="miss", payloads=[])
    # A live pass whose frames no longer decode -> a regression.
    _write_burst(
        root, "cam0-d", _blank_frame(), outcome="pass", payloads=["PLT-GONE"]
    )
    return root


def _legacy_cfg():
    return AppConfig().decode.model_copy(update={"engine": DecodeEngineKind.LEGACY})


def test_find_bursts_reads_every_segment(recording_dir: Path) -> None:
    bursts = rb.find_bursts(recording_dir)
    assert {b.meta["candidate_id"] for b in bursts} == {
        "cam0-a",
        "cam0-b",
        "cam0-c",
        "cam0-d",
    }
    assert all(b.frame_paths for b in bursts)


def test_replay_classifies_recovery_and_regression(recording_dir: Path) -> None:
    bursts = rb.find_bursts(recording_dir)
    report = rb.score_variant("legacy", _legacy_cfg(), bursts)

    recovered = {r.candidate_id for r in report.recovered}
    regressed = {r.candidate_id for r in report.regressions}
    assert recovered == {"cam0-a"}, "the decodable live-miss must surface as recovered"
    assert regressed == {"cam0-d"}, "the blank live-pass must surface as a regression"
    # cam0-b (decodable pass) is neither; cam0-c (blank miss) is neither.
    assert report.decoded == 2  # a and b decode; c and d do not
    # Attribution names the decoder that actually fired.
    assert "pyzbar" in report.attribution


def test_replay_is_read_only_over_the_recording_dir(recording_dir: Path) -> None:
    before = {p: p.stat().st_mtime_ns for p in recording_dir.rglob("*")}
    bursts = rb.find_bursts(recording_dir)
    rb.score_variant("legacy", _legacy_cfg(), bursts)
    after = {p: p.stat().st_mtime_ns for p in recording_dir.rglob("*")}
    assert before == after, "replay must not create, delete, or touch any file"


def test_main_writes_report_and_leaves_dir_untouched(
    recording_dir: Path, tmp_path: Path, capsys
) -> None:
    before = sorted(str(p.relative_to(recording_dir)) for p in recording_dir.rglob("*"))
    out = tmp_path / "replay.md"
    rc = rb.main([str(recording_dir), "--config", _config_yaml(tmp_path), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "Burst replay report" in text
    assert "HYPOTHETICAL" in text  # the integrity banner is prominent
    # The recording dir is unchanged; only --out (outside it) was written.
    after = sorted(str(p.relative_to(recording_dir)) for p in recording_dir.rglob("*"))
    assert before == after


def test_main_rejects_missing_dir(tmp_path: Path) -> None:
    assert rb.main([str(tmp_path / "nope")]) == 2


def _config_yaml(tmp_path: Path) -> str:
    """A minimal legacy-engine config file for the CLI path."""
    path = tmp_path / "legacy.yaml"
    path.write_text("decode: {engine: legacy}\n", encoding="utf-8")
    return str(path)
