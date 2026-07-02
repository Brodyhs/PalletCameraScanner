r"""Phase A smoke test for CameraInjectionSource (no pipeline yet).

Builds the injection source against the REAL camera, drives ONE clean slow pass,
and asserts the FrameSource contract: 2-D uint8 1920x1200 frames, monotonic ts,
exactly one INJ- ground-truth record with px_per_module + blur_modules. Saves a
mid-pass composited frame to data/inject_phaseA.png so you can eyeball the QR
riding on your live scene (next to your real codes).

Run (stop tools/bench.py first — it holds the camera):
  .\.venv\Scripts\python.exe tools\inject_smoke.py --config config\station.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from palletscan.config import load_config  # noqa: E402
from palletscan.sources.inject import CameraInjectionSource  # noqa: E402
from palletscan.types import Symbology  # noqa: E402


def pick_mid_pass_frame(frames, rec, nominal_fps):
    """The collected (frame_index, image, ts) tuple nearest the pass midpoint.

    Truth ``first_frame``/``last_frame`` are nominal-fps ticks of the live
    ts clock (TRUTH TIME-BASE in palletscan/sources/inject.py), NOT camera
    frame indices — matching them against ``frame_index`` picked a frame
    offset by the camera's connect time x fps (past the pass entirely, so
    the saved 'mid-pass' PNG showed a code-free scene while the tool printed
    PHASE A OK — re-review of REVIEW_bringup_4d95b67). Match on ts."""
    mid_ts = (rec.first_frame + rec.last_frame) / (2.0 * nominal_fps)
    return min(frames, key=lambda f: abs(f[2] - mid_ts))


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
    # A clean, slow, low-blur pass so Phase A simply PROVES the flow (the sweep
    # comes later). exposure_s short -> minimal injected blur -> decodable.
    syn = app_cfg.synthetic.model_copy(
        update={
            "num_passes": 1,
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
    src = CameraInjectionSource(syn, app_cfg, exposure_s=0.001)
    fh, fw = src._cfg.height, src._cfg.width  # source overrode these to camera res
    print(f"camera: {src.source_id} via inner {type(src._inner).__name__}; "
          f"frame target {fw}x{fh} @ {src.nominal_fps} fps", flush=True)

    frames: list[tuple[int, np.ndarray, float]] = []
    done_at = None
    try:
        for i, fr in enumerate(src.frames()):
            frames.append((fr.frame_index, fr.image, fr.ts))
            if src.truth and done_at is None:
                done_at = i
            if (done_at is not None and i > done_at + 3) or i > 280:
                break
    finally:
        src.close()

    assert src.truth, "no ground-truth record produced — pass never completed"
    rec = src.truth[0]
    for fi, img, _ in frames:
        assert img.dtype == np.uint8 and img.ndim == 2, f"bad frame dtype/ndim @ {fi}"
        assert img.shape == (fh, fw), f"bad shape {img.shape} @ {fi}"
    tss = [t for _, _, t in frames]
    assert all(tss[i] <= tss[i + 1] for i in range(len(tss) - 1)), "ts not monotonic"
    assert rec.payload.startswith("INJ-"), f"payload not tagged: {rec.payload}"
    assert rec.first_frame < rec.last_frame, f"bad span {rec.first_frame}->{rec.last_frame}"
    assert "px_per_module" in rec.params and "blur_modules" in rec.params

    pick = pick_mid_pass_frame(frames, rec, src.nominal_fps)
    out = Path("data/inject_phaseA.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), pick[1])

    print("PHASE A OK", flush=True)
    print(f"  frames consumed: {len(frames)}", flush=True)
    print(f"  truth: payload={rec.payload} sym={rec.symbology.value} "
          f"frames {rec.first_frame}->{rec.last_frame}", flush=True)
    print(f"  params: px/module={rec.params['px_per_module']:.1f} "
          f"blur_modules={rec.params['blur_modules']:.2f} "
          f"speed_mph={rec.params['speed_mph']:.1f}", flush=True)
    print(f"  saved mid-pass composited frame -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
