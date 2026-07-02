r"""Throwaway diagnostic: which exposures actually decode a MOVING code in the
current room/light, and does the camera switch exposure cleanly?

Holds each exposure in a ladder for ~1.5 s (twice through), at a fixed high gain,
while YOU MOVE THE BOX. Reports, per exposure: mean brightness, frames, and how
many frames decoded the (moving) code. The exposure that both stays bright AND
decodes while moving is the usable motion setting in your light.

Run:  .\.venv\Scripts\python.exe tools\expo_probe.py        # then move the box ~20s
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from tools.bench import _MOTION_H, _MOTION_W, decode_region, motion_box  # noqa: E402

EXPOSURES = [-5, -6, -7, -8, -9, -10, -11]
GAIN = 45.0  # high, to give the short exposures the best possible shot
HOLD_S = 1.5
CYCLES = 2


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    cap = cv2.VideoCapture(0, cv2.CAP_MSMF)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1200)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"UYVY"))
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
    cap.set(cv2.CAP_PROP_FPS, 55)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
    cap.set(cv2.CAP_PROP_GAIN, GAIN)
    if not cap.isOpened():
        print("FAILED to open camera (MSMF index 0)")
        return 1

    print(f">>> MOVE THE BOX in front of the camera for ~{int(CYCLES*len(EXPOSURES)*HOLD_S)}s <<<",
          flush=True)
    bright: dict[int, list] = defaultdict(list)
    decodes: dict[int, int] = defaultdict(int)
    frames: dict[int, int] = defaultdict(int)
    prev_small = None
    for _cycle in range(CYCLES):
        for e in EXPOSURES:
            cap.set(cv2.CAP_PROP_EXPOSURE, float(e))
            for _ in range(3):  # let the new exposure settle
                cap.read()
            t1 = time.monotonic()
            while time.monotonic() - t1 < HOLD_S:
                ok, img = cap.read()
                if not ok or img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                bright[e].append(float(gray.mean()))
                frames[e] += 1
                small = cv2.resize(gray, (_MOTION_W, _MOTION_H), interpolation=cv2.INTER_AREA)
                # scale the motion box by the NEGOTIATED geometry, not the
                # requested 1920x1200 (the driver may deliver another mode)
                fh, fw = gray.shape
                roi = motion_box(prev_small, small, fw, fh)[0] if prev_small is not None else None
                prev_small = small
                region = gray if roi is None else gray[roi[1]:roi[1]+roi[3], roi[0]:roi[0]+roi[2]]
                if decode_region(region, 0, 0, 1.0, 1.0):
                    decodes[e] += 1
            print(f"  exp {e:>3}: bright {np.mean(bright[e]):5.1f}  decodes {decodes[e]}", flush=True)
    cap.release()

    print("\n== summary (gain fixed at %.0f) ==" % GAIN)
    print("exposure  ~ms      mean_bright   frames   decode_frames")
    for e in EXPOSURES:
        ms = 1000 * (2.0 ** e)  # rough 2^e-seconds UVC mapping
        b = float(np.mean(bright[e])) if bright[e] else 0.0
        print(f"  {e:>3}   {ms:7.2f}     {b:8.1f}     {frames[e]:5d}    {decodes[e]:5d}")
    print("\nThe usable motion exposure = the shortest exposure that still both")
    print("stays bright (decodable) AND racks up decode_frames while you moved the box.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
