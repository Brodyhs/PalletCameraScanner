"""Record -> replay: the spec §11 "replay of a recorded clip" criterion.

Phase 2 verifies payload-level equivalence (decoded payloads == recorded
truth, decode-XOR-miss accounted); the manifest reconciliation *report* is
Phase 4. The MJPG round trip is part of what is under test: recording must
not perturb the decodability envelope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from palletscan import cli
from palletscan.app import PipelineRunner, reconcile_truth
from palletscan.config import AppConfig, SyntheticConfig, apply_overrides
from palletscan.sources.record import record_synthetic_clip
from palletscan.sources.synthetic import load_truth_jsonl


def _replay_config(clip: Path, data_dir: Path) -> AppConfig:
    cfg = AppConfig.model_validate(
        {
            "source": {"type": "video"},
            "video": {"path": str(clip), "speed": 0},
            "sinks": {"console": {"enabled": False}, "sqlite": {"enabled": False}},
        }
    )
    return apply_overrides(cfg, data_dir=data_dir)


def test_record_replay_roundtrip_small(fast_synth_config: AppConfig, tmp_path: Path) -> None:
    clip = tmp_path / "clips" / "small.avi"
    res = record_synthetic_clip(fast_synth_config, clip)
    truth = load_truth_jsonl(res.truth_path)
    assert len(truth) == 3
    assert res.frames > truth[-1].last_frame

    runner = PipelineRunner.from_config(_replay_config(clip, tmp_path / "replay"))
    summary = runner.run()
    rec = reconcile_truth(truth, runner.collected_events, fps=res.fps)
    assert rec.unaccounted == []
    decoded = {e.payload for e in runner.collected_events if e.kind == "pass"}
    assert decoded == {t.payload for t in truth}
    assert summary.misses == 0
    assert summary.frames == res.frames


def test_replay_cli_with_truth_reconciliation(
    fast_synth_config: AppConfig, tmp_path: Path
) -> None:
    clip = tmp_path / "clips" / "cli.avi"
    res = record_synthetic_clip(fast_synth_config, clip)
    code = cli.main(
        [
            "replay",
            str(clip),
            "--speed",
            "0",
            "--truth",
            str(res.truth_path),
            "--data-dir",
            str(tmp_path / "cli-replay"),
        ]
    )
    assert code == 0


def test_record_rejects_non_avi(fast_synth_config: AppConfig, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="avi"):
        record_synthetic_clip(fast_synth_config, tmp_path / "clip.mp4")


def test_replay_cli_rejects_invalid_flags(
    fast_synth_config: AppConfig, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """CLI overrides must go through VideoConfig validation (model_copy
    would skip the validators) and --truth must demand a single play."""
    clip = tmp_path / "clips" / "flags.avi"
    res = record_synthetic_clip(fast_synth_config, clip)
    base = ["replay", str(clip), "--data-dir", str(tmp_path / "out")]
    assert cli.main(base + ["--speed", "-2"]) == 2
    assert cli.main(base + ["--fps-override", "0"]) == 2
    assert cli.main(base + ["--fps-override", "nan"]) == 2
    assert cli.main(base + ["--loop", "-1"]) == 2
    assert (
        cli.main(base + ["--truth", str(res.truth_path), "--loop", "2"]) == 2
    )
    err = capsys.readouterr().err
    assert "invalid replay options" in err and "--loop 1" in err


@pytest.mark.acceptance
def test_replay_of_recorded_clip_decodes_all_payloads(tmp_path: Path) -> None:
    """~40 recorded passes across a moderate in-spec envelope must replay to
    exactly the truth payload set (the codec must not eat any pass)."""
    cfg = AppConfig().model_copy(
        update={
            "synthetic": SyntheticConfig(
                width=960,
                height=540,
                fps=30.0,
                seed=20260611,
                num_passes=40,
                # Moderate envelope: inside spec, away from the extreme
                # corners where the 400-pass gate tolerates <=2 misses —
                # this test demands equality, not >=99.5%.
                speed_mph_range=(2.0, 8.0),
                angle_deg_range=(0.0, 25.0),
                px_per_module_range=(3.5, 6.0),
                contrast_range=(0.55, 1.0),
                noise_sigma_range=(2.0, 6.0),
                occlusion_max_frac=0.10,
                idle_s_range=(0.3, 0.8),
            ),
        }
    )
    cfg = apply_overrides(cfg, data_dir=tmp_path / "record")
    clip = tmp_path / "clips" / "synth40.avi"
    res = record_synthetic_clip(cfg, clip)
    truth = load_truth_jsonl(res.truth_path)
    assert len(truth) == 40

    runner = PipelineRunner.from_config(_replay_config(clip, tmp_path / "replay"))
    summary = runner.run()
    rec = reconcile_truth(truth, runner.collected_events, fps=res.fps)

    assert rec.unaccounted == [], (
        f"{len(rec.unaccounted)} recorded passes produced neither decode nor "
        f"miss on replay: {rec.unaccounted}"
    )
    decoded = {e.payload for e in runner.collected_events if e.kind == "pass"}
    truth_payloads = {t.payload for t in truth}
    missed = truth_payloads - decoded
    by_payload = {t.payload: t for t in truth}
    detail = "\n".join(
        "  {p}: px/module={ppm:.2f} blur={bm:.2f}mod contrast={c:.2f}".format(
            p=p,
            ppm=by_payload[p].params["px_per_module"],
            bm=by_payload[p].params["blur_modules"],
            c=by_payload[p].params["contrast"],
        )
        for p in sorted(missed)
    )
    assert decoded == truth_payloads, (
        f"replay failed to decode {len(missed)} recorded passes:\n{detail}"
    )
    assert summary.misses == 0
