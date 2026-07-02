r"""Phase 0: prove BOTH cameras stream simultaneously through the real
CameraSource path — color (MSMF/UYVY) + mono (pygrabber/Y8) at once, the actual
two-camera USB-coexistence test.

Each camera runs in its own thread (so COM apartment + the pygrabber graph live
on the thread that built them). Reports per-camera achieved fps + frame shape +
a live decode, while the other camera is also streaming.

Run with e-CAMView CLOSED:
  .\.venv\Scripts\python.exe tools\dual_camera_check.py [seconds]
"""
from __future__ import annotations
import sys, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.config import load_config  # noqa: E402
from palletscan.sources.camera import CameraSource  # noqa: E402


def run_cam(cam_cfg, dur, out):
    try:
        src = CameraSource(cam_cfg)
    except Exception as e:
        out["error"] = f"connect: {e!r}"
        return
    n, shapes, t0, last = 0, set(), time.monotonic(), None
    try:
        for fr in src.frames():
            n += 1
            shapes.add((fr.image.shape, str(fr.image.dtype)))
            last = fr.image
            if time.monotonic() - t0 >= dur:
                break
    except Exception as e:
        out["error"] = f"stream: {e!r}"
    finally:
        dt = time.monotonic() - t0
        try:
            src.close()
        except Exception:
            pass
        out.update(frames=n, fps=(n / dt if dt else 0.0), shapes=shapes, last=last)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    cfg = load_config("config/station.yaml")
    cams = {c.id: c for c in cfg.cameras}
    ids = [cid for cid in ("cam-color", "cam-mono") if cid in cams]
    results = {cid: {} for cid in ids}
    print(f"opening {ids} simultaneously for {dur:.0f}s each...\n")
    threads = [threading.Thread(target=run_cam, args=(cams[cid], dur, results[cid]),
                                name=cid) for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = True
    for cid in ids:
        r = results[cid]
        if r.get("error"):
            ok = False
            print(f"{cid:10s}: ERROR {r['error']}")
            continue
        line = f"{cid:10s}: {r['frames']:4d} frames @ {r['fps']:5.1f} fps  shapes={r['shapes']}"
        try:
            import numpy as np
            from palletscan.pipeline.decoders import ZxingDecoder
            last = r.get("last")
            if last is not None:
                g = last if last.ndim == 2 else last[:, :, 0]
                d = ZxingDecoder().decode(np.ascontiguousarray(g))
                line += f"  decodes={[x.payload for x in d] or '[]'}"
        except Exception as e:
            line += f"  (decode skipped: {e!r})"
        print(line)
        if r["frames"] < 5:
            ok = False
    print("\nDUAL-CAMERA:", "OK — both streamed together" if ok else "PROBLEM (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
