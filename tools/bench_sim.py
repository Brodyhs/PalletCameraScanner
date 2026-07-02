r"""Offline synthetic harness for the bench's decode + motion logic.

Simulates a code panning across the frame (using the project's own render +
motion-blur model) and runs the SAME motion_box + zxing decode the live bench
uses — so the decode-vs-speed-vs-exposure envelope is measurable in *seconds*
with no camera. It separates SOFTWARE (decode/motion) from PHYSICS (lighting):
if a clean synthetic moving code decodes here but the live camera misses it,
the problem is the image (exposure / light / focus), not the code path.

Run:
  .\.venv\Scripts\python.exe tools\bench_sim.py                       # default sweep
  .\.venv\Scripts\python.exe tools\bench_sim.py --sym datamatrix --ppm 4 --module 5
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palletscan.sources.render import motion_blur, render_datamatrix, render_qr  # noqa: E402
from tools.bench import _MOTION_H, _MOTION_W, decode_region, motion_box  # noqa: E402

# The decode envelope depends only on px/module and blur (both in module units),
# not on absolute frame size — so a small canvas is faithful and ~4x faster.
# The narrower canvas DOES mean fewer in-view frames per crossing than the
# 1920 px camera, so the per-speed chance count is measured and reported
# (run_pass "chances", printed per cell) rather than assumed equal.
FRAME_W, FRAME_H = 960, 600
MPH_TO_MPS = 0.44704

#: Cap on in-view decode chances per pass: slow passes are truncated here so
#: the sweep stays fast; faster passes get min(MAX_CHANCES, full geometric
#: crossing) — a fixed frame count would silently cut fast passes mid-crossing.
MAX_CHANCES = 40


def ppm_from_optics(module_mm, lens_mm, dist_in, pixel_um=3.0):
    """Pixels-per-module a code projects to: module_mm / GSD, GSD = pixel*D/f."""
    gsd_mm = (pixel_um / 1000.0) * (dist_in * 25.4) / lens_mm
    return module_mm / gsd_mm, gsd_mm


def make_pass(payload, sym, ppm, speed_pxps, blur_px, fps, noise_sigma=4.0, bg=120, seed=0):
    """Frames of `payload` panning L->R at speed_pxps, motion-blurred by blur_px
    (the displacement during the exposure).

    The code starts FULLY OFF-CANVAS: frame 0 is code-free (motion_box needs a
    prior frame, so a code already mid-canvas on frame 0 was an unboxable
    wasted chance) and every subsequent frame overlaps the canvas until the
    patch fully exits or MAX_CHANCES in-view frames exist. Chances per speed =
    ``len(frames) - 1`` = min(MAX_CHANCES, geometric crossing capacity) — the
    old fixed 40-frame run entered at x=0 and, at higher speeds, burned most
    of its frames after the code had already left the canvas, so "every speed
    gets the same number of decode chances" was false exactly where it
    mattered."""
    rng = np.random.default_rng(seed)
    rend = render_qr(payload, ppm) if sym == "qr" else render_datamatrix(payload, ppm)
    patch = motion_blur(rend.image, blur_px)
    ph, pw = patch.shape
    y = (FRAME_H - ph) // 2
    pxpf = max(1.0, speed_pxps / fps)

    def _canvas(x):
        canvas = np.full((FRAME_H, FRAME_W), bg, np.uint8)
        if x is not None:
            x0, x1 = max(0, x), min(FRAME_W, x + pw)
            if x1 > x0:
                canvas[y : y + ph, x0:x1] = patch[:, x0 - x : x1 - x]
        if noise_sigma > 0:
            canvas = np.clip(
                canvas.astype(np.int16)
                + rng.normal(0, noise_sigma, canvas.shape).astype(np.int16),
                0, 255,
            ).astype(np.uint8)
        return canvas

    frames = [_canvas(None)]  # the code-free motion prior
    i = 1
    while len(frames) - 1 < MAX_CHANCES:
        x = int(round(-pw + i * pxpf))
        if x >= FRAME_W:
            break  # fully exited: the crossing is over
        frames.append(_canvas(x))
        i += 1
    return frames


def run_pass(frames, payload):
    """Run the bench's motion_box + decode over the frames; return diagnostics."""
    prev_small = None
    decode_frames = box_frames = 0
    first = None
    centroids = []
    for idx, gray in enumerate(frames):
        small = cv2.resize(gray, (_MOTION_W, _MOTION_H), interpolation=cv2.INTER_AREA)
        roi = motion_box(prev_small, small, FRAME_W, FRAME_H)[0] if prev_small is not None else None
        prev_small = small
        if roi is None:
            continue
        box_frames += 1
        x, y, w, h = roi
        for pts, pl, _sym in decode_region(gray[y : y + h, x : x + w], x, y, 1.0, 1.0):
            if pl == payload:
                decode_frames += 1
                if first is None:
                    first = idx
                centroids.append((idx, sum(p[0] for p in pts) / 4.0))
    pxpf = None
    if len(centroids) >= 2:
        deltas = [
            (centroids[k][1] - centroids[k - 1][1]) / (centroids[k][0] - centroids[k - 1][0])
            for k in range(1, len(centroids))
            if centroids[k][0] != centroids[k - 1][0]
        ]
        if deltas:
            pxpf = float(np.median(deltas))
    return {
        "decoded": decode_frames > 0,
        "decode_frames": decode_frames,
        # in-view decode chances: make_pass's contract is that every frame
        # after the code-free prior overlaps the canvas
        "chances": len(frames) - 1,
        "box_frames": box_frames,
        "first": first,
        "measured_pxpf": pxpf,
    }


def sweep(sym, ppm, module_mm, fps, payload):
    px_per_m = ppm / (module_mm / 1000.0)
    mphs = [0.5, 1, 2, 3, 5, 8]
    exps = [1, 2, 4, 8, 16, 33]
    print(f"\nDecode envelope — {sym.upper()}, {ppm:g} px/module, {module_mm:g} mm modules, {fps:g} fps")
    print("cell = frames decoded / in-view chances at that speed; '.' = MISS")
    print("(faster passes cross the canvas in fewer frames — chances are per-speed, printed, not assumed equal)\n")
    print("exp \\ mph " + "".join(f"{m:>9}" for m in mphs))
    for e in exps:
        cells = []
        for m in mphs:
            speed_pxps = m * MPH_TO_MPS * px_per_m
            blur_px = speed_pxps * (e / 1000.0)
            r = run_pass(make_pass(payload, sym, ppm, speed_pxps, blur_px, fps), payload)
            cells.append((r["decode_frames"], r["chances"]))
        print(f"{e:>4}ms   " + "".join(
            f"{(f'{d}/{c}' if d else f'./{c}'):>9}" for d, c in cells
        ))
    print("\nblur in modules at a cell = mph * 0.447 * exp_ms / module_mm")
    print("(decode falls off where blur exceeds ~1 module — short exposure freezes faster motion)")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Offline bench decode-envelope sweep")
    ap.add_argument("--sym", default="qr", choices=["qr", "datamatrix"])
    ap.add_argument("--ppm", type=float, default=5.0, help="pixels per module (resolution)")
    ap.add_argument("--module", type=float, default=5.0, help="module size mm")
    ap.add_argument("--fps", type=float, default=55.0)
    ap.add_argument("--payload", default="PLT-000042")
    ap.add_argument("--lens", type=float, help="lens focal length mm (with --dist, computes ppm)")
    ap.add_argument("--dist", type=float, help="working distance INCHES (with --lens)")
    ap.add_argument("--pixel", type=float, default=3.0, help="sensor pixel pitch um (24CUG=3.0)")
    a = ap.parse_args()
    ppm = a.ppm
    if a.lens and a.dist:
        ppm, gsd = ppm_from_optics(a.module, a.lens, a.dist, a.pixel)
        print(f"\nOptics: {a.lens:g} mm lens @ {a.dist:g} in, {a.pixel:g} um pixels "
              f"-> GSD {gsd:.2f} mm/px -> {ppm:.1f} px/module for {a.module:g} mm modules")
    sweep(a.sym, ppm, a.module, a.fps, a.payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
