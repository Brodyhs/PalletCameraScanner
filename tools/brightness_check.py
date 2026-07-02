r"""Measure actual CAPTURE brightness (what the decoder sees) for each camera at
the relevant exposures, plus the DISPLAYED brightness after the dashboard's
preview_gain. Run with the dashboard STOPPED (cameras exclusive).

  .\.venv\Scripts\python.exe tools\brightness_check.py
"""
from __future__ import annotations
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import load_config  # noqa: E402
from palletscan.sources.camera import CameraSource  # noqa: E402

PREVIEW_GAIN = 2.2  # the demo's web.preview_gain


def measure(cam_cfg, exposure, n=25):
    cam = cam_cfg.model_copy(update={
        "settings": cam_cfg.settings.model_copy(update={"exposure": float(exposure)})
    })
    src = CameraSource(cam)
    means, t0 = [], time.monotonic()
    try:
        for i, fr in enumerate(src.frames()):
            means.append(float(fr.image.mean()))
            if i >= n:
                break
    finally:
        src.close()
    return sum(means) / len(means) if means else float("nan")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config("config/station.yaml")
    cams = {c.id: c for c in cfg.cameras}
    rows = [
        ("color  -6 (15.6ms)", "cam-color", -6),
        ("mono   -8 (3.9ms, production)", "cam-mono", -8),
        ("mono   -6 (15.6ms, demo)", "cam-mono", -6),
    ]
    print(f"{'camera / exposure':32s} {'capture(decoder sees)':>22s} {'displayed(x2.2 gain)':>22s}")
    print("-" * 78)
    for label, cid, exp in rows:
        time.sleep(2)  # let the previous camera fully release
        cap = measure(cams[cid], exp)
        disp = min(255.0, cap * PREVIEW_GAIN)
        print(f"{label:32s} {cap:>18.1f}/255 {disp:>18.1f}/255")
    print("\nNote: 'capture' is the raw sensor mean (what the QR/DM decoder operates on).")
    print("'displayed' is capture x preview_gain 2.2, clamped at 255 (the dashboard view only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
