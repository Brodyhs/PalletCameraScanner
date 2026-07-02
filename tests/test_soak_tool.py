"""soak --mode inject artifacts: the snapshots series must rebase under
--data-dir like every other soak artifact and start fresh per run.

Regression: SNAPSHOTS_PATH was a hardcoded module constant (ignored the
--data-dir override) opened in append mode, so successive runs interleaved
their series. No camera: the camera-holding injection source is monkeypatched
with a small SyntheticSource — the snapshots-path plumbing under test is
identical either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("psutil")

from palletscan.sources.synthetic import SyntheticSource
from tools import soak


def test_inject_soak_snapshots_rebase_under_data_dir_and_start_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_inject_source(syn_cfg, app_cfg, *, exposure_s):  # noqa: ANN001, ANN202
        small = syn_cfg.model_copy(
            update={
                "width": 320,
                "height": 240,
                "num_passes": 2,
                "px_per_module_range": (2.0, 2.0),
                "speed_mph_range": (8.0, 10.0),
                "noise_sigma_range": (0.0, 0.0),
                "idle_s_range": (0.1, 0.1),
            }
        )
        return SyntheticSource(small)

    monkeypatch.setattr(soak, "CameraInjectionSource", fake_inject_source)
    data_dir = tmp_path / "run1"
    expected = data_dir / "snapshots.jsonl"
    # a stale series from a "previous run": it must NOT be appended to
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text('{"sentinel": true}\n', encoding="utf-8")
    args = soak.parse_args(
        [
            "--minutes", "0.02",
            "--mode", "inject",
            "--data-dir", str(data_dir),
            "--snapshot-interval-s", "0.02",
            "--rss-interval-s", "0.2",
            "--stats-interval", "0",
        ]
    )
    report = soak.run_soak(args)
    assert report.snapshots_path == expected
    assert expected.exists()
    content = expected.read_text(encoding="utf-8")
    assert "sentinel" not in content, "previous run's series was appended to"
    for line in content.splitlines():  # whatever landed is well-formed JSONL
        json.loads(line)
