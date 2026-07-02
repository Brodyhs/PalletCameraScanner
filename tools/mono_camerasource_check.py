r"""Verify the 37CUGM via the REAL CameraSource path on the pygrabber backend.

Builds CameraSource from the cam-mono config (backend: pygrabber), pulls frames
through the production connect->mode->read path, reports achieved fps + frame
shape, and tries a zxing decode on the last frame as an end-to-end sanity check.

Run with e-CAMView CLOSED:
  .\.venv\Scripts\python.exe tools\mono_camerasource_check.py
"""
from __future__ import annotations
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import load_config  # noqa: E402
from palletscan.sources.camera import CameraSource  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config("config/station.yaml")
    cam = next(c for c in cfg.cameras if c.id == "cam-mono")
    print(f"building CameraSource for {cam.id} (backend={cam.backend}, name={cam.name!r})")
    src = CameraSource(cam)
    n, shapes = 0, set()
    t0 = time.monotonic()
    last = None
    try:
        for fr in src.frames():
            n += 1
            shapes.add((fr.image.shape, str(fr.image.dtype)))
            last = fr.image
            if n >= 200:
                break
    finally:
        dt = time.monotonic() - t0
        src.close()
    print(f"frames: {n} in {dt:.2f}s -> {n/dt if dt else 0:.1f} fps")
    print(f"shapes/dtypes: {shapes}")
    if last is not None:
        print(f"last frame min/max: {int(last.min())}/{int(last.max())}")
        try:
            from palletscan.pipeline.decoders import ZxingDecoder
            import numpy as np
            gray = last if last.ndim == 2 else last[:, :, 0]
            dec = ZxingDecoder()
            res = dec.decode(np.ascontiguousarray(gray))
            print(f"zxing decode on live mono frame: {[r.payload for r in res] or 'none in view'}")
        except Exception as e:
            print("decode attempt skipped:", repr(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
