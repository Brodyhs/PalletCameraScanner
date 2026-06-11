"""End-to-end synthetic acceptance test (spec §7/§11, Phase 1 gate).

400 passes drawn across the full spec envelope — speed 2-10 mph, approach
angle 0-35°, px/module 3-6, blur derived at the ~1 ms global-shutter
operating point, contrast/noise/occlusion ranges — at 960x540. The two
dimensionless ratios (px/module, blur-in-modules) are resolution-invariant,
so the smaller frame only speeds up compositing.

Asserts:
1. pass-level read rate >= 99.5% (i.e. at most 2 misses in 400);
2. the account-for-everything invariant: every truth pass produces a decode
   event XOR a miss event with on-disk evidence — nothing silently dropped;
3. payload integrity: decoded payloads are exactly truth payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from palletscan.app import PipelineRunner
from palletscan.config import AppConfig, SyntheticConfig, apply_overrides

NUM_PASSES = 400
SEED = 20260610


def _acceptance_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig().model_copy(
        update={
            "synthetic": SyntheticConfig(
                width=960,
                height=540,
                fps=30.0,
                seed=SEED,
                num_passes=NUM_PASSES,
                # full spec envelope; exposure_fraction stays at the 0.03
                # (~1 ms at 30 fps) operating-point default
                speed_mph_range=(2.0, 10.0),
                angle_deg_range=(0.0, 35.0),
                px_per_module_range=(3.0, 6.0),
                contrast_range=(0.45, 1.0),
                noise_sigma_range=(2.0, 8.0),
                occlusion_max_frac=0.15,
                idle_s_range=(0.3, 0.8),
            ),
        }
    )
    cfg = apply_overrides(cfg, data_dir=tmp_path)
    return cfg.model_copy(
        update={
            "sinks": cfg.sinks.model_copy(
                update={
                    "console": cfg.sinks.console.model_copy(
                        update={"enabled": False}
                    ),
                    "sqlite": cfg.sinks.sqlite.model_copy(
                        update={"enabled": False}
                    ),
                }
            ),
        }
    )


@pytest.mark.acceptance
def test_synthetic_acceptance_99_5_and_account_for_everything(tmp_path: Path) -> None:
    cfg = _acceptance_config(tmp_path)
    runner = PipelineRunner.from_config(cfg)
    summary = runner.run()
    rec = summary.reconciliation
    assert rec is not None

    truth_by_payload = {t.payload: t for t in runner.source.truth}

    # -- 2. account for everything: no truth pass without decode XOR miss --
    assert rec.unaccounted == [], (
        f"{len(rec.unaccounted)} passes produced neither a decode nor a miss "
        f"event: {rec.unaccounted}"
    )
    events = runner.collected_events
    misses = [e for e in events if e.kind == "miss"]
    for miss in misses:
        d = Path(miss.evidence_dir)
        assert d.is_dir(), f"miss {miss.candidate_id} evidence dir missing"
        assert list(d.glob("*.jpg")), f"miss {miss.candidate_id} burst empty"
        assert (d / "meta.json").exists()

    # -- 3. payload integrity --
    decoded_payloads = {e.payload for e in events if e.kind == "pass"}
    assert decoded_payloads <= set(truth_by_payload), (
        "decoded payloads that never existed: "
        f"{decoded_payloads - set(truth_by_payload)}"
    )

    # -- 1. read rate >= 99.5% (at most 2 misses in 400) --
    missed_payloads = set(truth_by_payload) - decoded_payloads
    detail = "\n".join(
        "  {p}: px/module={ppm:.2f} blur={bm:.2f}mod speed={s:.1f}mph "
        "angle={a:.0f} contrast={c:.2f} occl={o:.2f}".format(
            p=p,
            ppm=truth_by_payload[p].params["px_per_module"],
            bm=truth_by_payload[p].params["blur_modules"],
            s=truth_by_payload[p].params["speed_mph"],
            a=truth_by_payload[p].params["angle_deg"],
            c=truth_by_payload[p].params["contrast"],
            o=truth_by_payload[p].params["occlusion_frac"],
        )
        for p in sorted(missed_payloads)
    )
    assert rec.read_rate >= 0.995, (
        f"read rate {rec.read_rate:.2%} < 99.5% "
        f"({rec.decoded}/{rec.truth_passes} decoded; envelope of each miss:\n"
        f"{detail}\n)"
    )

    # events also landed on the JSONL sink
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == len(events)
    assert all(json.loads(line) for line in lines)
