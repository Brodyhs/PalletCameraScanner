r"""Phase A step 5: drive ONE injected pass through the REAL pipeline and confirm
it decodes + reconciles green against ground truth (and the TTFD path is live).

Run (stop tools/bench.py first):
  .\.venv\Scripts\python.exe tools\inject_pipeline_check.py --config config\station.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.app import PipelineRunner  # noqa: E402
from palletscan.config import load_config  # noqa: E402
from palletscan.sources.inject import CameraInjectionSource  # noqa: E402
from palletscan.types import Symbology  # noqa: E402


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/station.yaml")
    ap.add_argument("--camera", default=None,
                    help="cameras[].id to inject onto (required when >1 camera configured)")
    a = ap.parse_args()

    app_cfg = load_config(a.config)
    if a.camera is not None:
        app_cfg = app_cfg.model_copy(
            update={"source": app_cfg.source.model_copy(update={"camera": a.camera})}
        )
    syn = app_cfg.synthetic.model_copy(
        update={
            "num_passes": 1,
            "fps": 55.0,  # match camera so reconcile_truth's frame->time lines up
            "speed_mph_range": [5.0, 5.0],
            "px_per_module_range": [5.0, 5.0],
            "angle_deg_range": [0.0, 0.0],
            "contrast_range": [1.0, 1.0],
            "noise_sigma_range": [0.0, 0.0],
            "occlusion_max_frac": 0.0,
            "idle_s_range": [0.3, 0.3],
            "symbologies": [Symbology.QR],
        }
    )
    app_cfg = app_cfg.model_copy(update={"synthetic": syn})
    src = CameraInjectionSource(syn, app_cfg, exposure_s=0.001)
    runner = PipelineRunner.from_config(app_cfg, source=src)
    summary = runner.run()

    print("\n==== run summary ====", flush=True)
    try:
        print(summary.format(), flush=True)
    except Exception:
        print(summary, flush=True)

    rec = summary.reconciliation
    print(f"\nreconciliation: truth_passes={rec.truth_passes} decoded={rec.decoded} "
          f"missed={rec.missed} unaccounted={rec.unaccounted}", flush=True)

    for ev in getattr(runner, "collected_events", []):
        pl = getattr(ev, "payload", "")
        if isinstance(pl, str) and pl.startswith("INJ-"):
            fd = getattr(ev, "first_decode_ts", None)
            fs = getattr(ev, "first_seen_ts", None)
            if fd is not None and fs is not None:
                print(f"TTFD {pl}: {(fd - fs) * 1000:.0f} ms "
                      f"(decodes={getattr(ev, 'decode_count', '?')})", flush=True)

    ok = rec.decoded >= 1 and not rec.unaccounted
    print(f"\nPHASE A STEP 5: {'OK — injected code decoded through the real pipeline' if ok else 'FAILED'}",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
