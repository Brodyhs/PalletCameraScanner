r"""WATCH multiple cameras live on the web dashboard, each with injected pallets
sweeping its feed + decode overlays. (Single-camera sweep+report is
tools/inject_run.py; this is the multi-camera live-watch view.)

Builds one CameraInjectionSource + PipelineRunner per camera, registers all of
their live previews in ONE dashboard, and runs them concurrently. Each camera
gets its own pallet sequence (seed offset) so the two feeds look distinct.

Run (nothing else may hold the cameras):
  .\.venv\Scripts\python.exe tools\inject_dashboard.py                       # both cameras
  .\.venv\Scripts\python.exe tools\inject_dashboard.py --cameras cam-mono     # one
"""
from __future__ import annotations

import argparse
import sys
import threading
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
    ap.add_argument("--cameras", default="cam-color,cam-mono",
                    help="comma-separated cameras[].id to show together")
    ap.add_argument("--exposure-ms", type=float, default=1.0,
                    help="modeled field shutter driving injected blur")
    ap.add_argument("--passes", type=int, default=100000, help="huge = effectively continuous")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    app_cfg = load_config(a.config)
    # Live-watch tuning: smoother preview (25 vs the 10fps default) + a SHALLOW
    # frame buffer so the heavier mono pipeline can't run a ~1s FIFO backlog
    # behind real time (the lag) — a demo prefers low latency over completeness.
    app_cfg = app_cfg.model_copy(update={
        # 25fps preview + a cosmetic brightness boost (the unlit scene is dim);
        # a 20-deep buffer smooths the moving codes without the ~1s lag of 64.
        "web": app_cfg.web.model_copy(update={"preview_fps": 25.0, "preview_gain": 4.0}),
        "frame_queue_size": 20,
    })
    cam_ids = [c.strip() for c in a.cameras.split(",") if c.strip()]

    runners: dict[str, PipelineRunner] = {}
    for i, cam_id in enumerate(cam_ids):
        cfg_i = app_cfg.model_copy(
            update={"source": app_cfg.source.model_copy(update={"camera": cam_id})}
        )
        syn = cfg_i.synthetic.model_copy(
            update={
                "num_passes": a.passes,
                "fps": 55.0,
                "seed": a.seed + i,  # distinct pallet stream per camera
                # Decodable demo envelope (vs the harsh full sweep): bigger codes
                # + moderate speed so the demo SCANS them well, with few misses.
                "speed_mph_range": [0.8, 5.0],
                "px_per_module_range": [4.0, 8.0],
                "angle_deg_range": [0.0, 30.0],
                "contrast_range": [0.6, 1.0],
                "noise_sigma_range": [0.0, 4.0],
                "occlusion_max_frac": 0.1,
                "idle_s_range": [0.3, 0.7],
                "symbologies": [Symbology.QR, Symbology.DATAMATRIX],
                # Pallets arrive from VARIED directions, not always left->right.
                "directions": ["right", "left", "down", "up",
                               "downright", "downleft", "upright", "upleft"],
                # Several pallets on screen at once -> multi-object tracking demo.
                "max_concurrent": 3,
            }
        )
        # Idle/static scan: also read the operator's STATIC codes (and a stopped
        # pallet), not just the moving injected ones.
        cfg_i = cfg_i.model_copy(update={
            "synthetic": syn,
            # idle_scan also reads STATIC codes; tracking="multi" gives each
            # concurrent pallet its OWN amber box + identity + miss accounting
            # (Tier 2) instead of one union box around all movement.
            "motion": cfg_i.motion.model_copy(update={
                "idle_scan_s": 2.0, "tracking": "multi"}),
        })
        # Brighten the mono for the WATCH view: its production -8 (~3.9ms motion-
        # freeze) is ~4x darker than the color's -6 and there's no scan light, so
        # match the color's exposure here. Bonus: longer exposure => lower capture
        # fps => lighter pipeline => less lag. Production station.yaml keeps -8.
        if cam_id == "cam-mono":
            cfg_i = cfg_i.model_copy(update={"cameras": [
                (c.model_copy(update={
                    "settings": c.settings.model_copy(update={"exposure": -6.0})})
                 if c.id == cam_id else c)
                for c in cfg_i.cameras
            ]})
        src = CameraInjectionSource(
            syn, cfg_i, source_id=f"inject-{cam_id}", exposure_s=a.exposure_ms / 1000.0
        )
        runners[src.source_id] = PipelineRunner.from_config(cfg_i, source=src)
        print(f"built injection source for {cam_id} -> {src.source_id} "
              f"({src._cfg.width}x{src._cfg.height} @ {src.nominal_fps}fps)", flush=True)

    from palletscan.cli import _DashboardUnavailable, _start_dashboard  # noqa: E402
    try:
        dashboard = _start_dashboard(app_cfg, runners, None)
    except _DashboardUnavailable as exc:
        print(f"dashboard unavailable: {exc}", file=sys.stderr, flush=True)
        return 2
    print(f"  ^^ open that URL to watch {len(runners)} camera(s) with injected pallets ^^\n",
          flush=True)

    threads = [
        threading.Thread(target=r.run, name=sid, daemon=True)
        for sid, r in runners.items()
    ]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    finally:
        dashboard.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
