r"""Smooth live camera viewer + QR/DataMatrix decode overlay (bring-up test).

Throwaway hand-test tool (not product). Decoding runs on a BACKGROUND thread so
the display loop only reads + shows frames — the window then refreshes at the
camera's true delivery rate instead of being throttled by pyzbar/pylibdmtx.
(This mirrors the production pipeline: capture never blocks on decode.) A native
cv2 window at the full frame rate, so it is far smoother than the dashboard's
10 fps MJPEG stream.

Works for BOTH cameras:
  --camera color  (default)  the See3CAM_24CUG on MSMF (OpenCV)
  --camera mono              the See3CAM_37CUGM on the pygrabber backend
                             (OpenCV cannot read its Y8 stream)

Auto-exposure/focus: the manual-focus S-mount lens has no software focus, so the
overlay shows a focus score (variance of Laplacian) — turn the lens ring to
MAXIMIZE it. A bright image in dim light needs a long exposure, which lowers fps;
press d (shorter) or add light to push the rate back up.

Keys:  q quit · a auto/manual exposure · e brighter · d darker (e/d: MANUAL only)
       · f focus-zoom (1:1 center crop)
Run:  .\.venv\Scripts\python.exe tools\live_decode.py --camera mono
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import cv2
from pylibdmtx.pylibdmtx import decode as dm_decode
from pyzbar.pyzbar import ZBarSymbol
from pyzbar.pyzbar import decode as qr_decode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_AE_AUTO = 0.75      # DSHOW/MSMF auto-exposure sentinel (SET works; readback lies)
_AE_MANUAL = 0.25

#: Per-camera defaults. Color: MSMF UYVY 1920x1200@55, exposure -6. Mono:
#: pygrabber Y8 2064x1552@72, exposure -8 (both from config/station.yaml).
_CAMERAS = {
    "color": dict(index=0, width=1920, height=1200, fps=55.0, expo=-6.0),
    "mono": dict(index=1, width=2064, height=1552, fps=72.0, expo=-8.0),
}


def _apply_exposure(cap, auto: bool, expo: float) -> None:
    if auto:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, _AE_AUTO)
    else:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, _AE_MANUAL)
        cap.set(cv2.CAP_PROP_EXPOSURE, expo)


def _open_color(cfg):
    cap = cv2.VideoCapture(cfg["index"], cv2.CAP_MSMF)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"UYVY"))
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
    cap.set(cv2.CAP_PROP_FPS, cfg["fps"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # freshest frame, low latency
    return cap


def _open_mono(cfg):
    # The mono streams Y8/packed-mono that OpenCV cannot read; the product's
    # pygrabber DirectShow backend delivers a 2-D grayscale frame via read().
    from palletscan.sources.pygrabber_capture import PyGrabberCapture

    return PyGrabberCapture(
        cfg["index"], width=cfg["width"], height=cfg["height"], fps=cfg["fps"]
    )


class Decoder(threading.Thread):
    """Decodes the most-recent gray frame off the display thread."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._gray = None
        # NOT "_stop": that shadows threading.Thread's private _stop()
        # method (called by join() on 3.11) — see tools/soak.py.
        self._stop_evt = threading.Event()
        self.payload = ""
        self.hit_t = 0.0

    def submit(self, gray) -> None:
        with self._lock:
            self._gray = gray

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            with self._lock:
                gray, self._gray = self._gray, None
            if gray is None:
                time.sleep(0.003)
                continue
            out = []
            # QR only (no PDF417 etc. -> kills the zbar pdf417 assertion noise);
            # full-res, with an Otsu black/white fallback for low-contrast/glare.
            qrs = qr_decode(gray, symbols=[ZBarSymbol.QRCODE])
            if not qrs:
                _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                qrs = qr_decode(bw, symbols=[ZBarSymbol.QRCODE])
            for r in qrs:
                out.append("QR: " + r.data.decode("utf-8", "replace"))
            # DM is downscale-tolerant and faster small; QR needs the pixels.
            dm_gray = cv2.resize(gray, (960, 600))
            for r in dm_decode(dm_gray, timeout=40, max_count=1):
                out.append("DM: " + r.data.decode("utf-8", "replace"))
            if out:
                new = " | ".join(out)
                now = time.monotonic()
                if new != self.payload or now - self.hit_t > 1.0:
                    print(f"DECODED  {new}", flush=True)
                self.payload, self.hit_t = new, now


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", choices=("color", "mono"), default="color")
    ap.add_argument("--index", type=int, default=None, help="override the device index")
    args = ap.parse_args(argv)
    cfg = dict(_CAMERAS[args.camera])
    if args.index is not None:
        cfg["index"] = args.index
    is_mono = args.camera == "mono"

    cap = _open_mono(cfg) if is_mono else _open_color(cfg)
    if not cap.isOpened():
        print(f"FAILED to open {args.camera} camera at index {cfg['index']}", flush=True)
        return 1

    auto = False          # start MANUAL short exposure so fps is full (press a for AUTO)
    expo = cfg["expo"]
    _apply_exposure(cap, auto, expo)

    dec = Decoder()
    dec.start()

    win = f"palletscan live [{args.camera}]  (q quit | a auto/manual | e/d exposure | f focus-zoom)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1600, 1000)

    last_t = time.monotonic()
    fps = 0.0
    count = 0
    focus_zoom = False     # f toggles a magnified 1:1 center crop for fine focusing
    focus_peak = 0.0       # running best focus score so you know when you've maximized it
    print(f"live_decode [{args.camera}]: streaming FULL-RES (threaded decode) - hold a code up",
          flush=True)
    while True:
        ok, img = cap.read()
        if not ok or img is None:
            continue
        count += 1
        now = time.monotonic()
        if now - last_t >= 0.5:
            fps = count / (now - last_t)
            count = 0
            last_t = now

        # The mono backend already delivers 2-D grayscale; the color cam is BGR.
        if img.ndim == 2:
            gray_full = img
        else:
            gray_full = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        dec.submit(gray_full)  # QR decodes at full res; DM downscales internally

        h, w = gray_full.shape
        rw, rh = min(800, w), min(600, h)                      # center focus ROI (FULL-res px)
        x0, y0 = (w - rw) // 2, (h - rh) // 2
        roi = gray_full[y0:y0 + rh, x0:x0 + rw]
        focus = cv2.Laplacian(roi, cv2.CV_64F).var()           # full-res ROI => true focus signal
        focus_peak = max(focus_peak, focus)

        if focus_zoom:                                          # 1:1 center crop, max detail
            view = cv2.cvtColor(roi, cv2.COLOR_GRAY2BGR)
        elif img.ndim == 2:
            view = cv2.cvtColor(gray_full, cv2.COLOR_GRAY2BGR)  # mono: colorize for overlays
            cv2.rectangle(view, (x0, y0), (x0 + rw, y0 + rh), (0, 200, 255), 2)
        else:
            view = img                                          # color: FULL-res BGR
            cv2.rectangle(view, (x0, y0), (x0 + rw, y0 + rh), (0, 200, 255), 2)

        fresh = (now - dec.hit_t) < 1.5
        mode = "AUTO" if auto else f"MANUAL {expo:+.0f}"
        sc = 0.9 if focus_zoom else 1.1
        cv2.putText(
            view,
            f"{mode}  fps {fps:4.1f}  bright {int(gray_full.mean())}  "
            f"focus {focus:6.0f}  peak {focus_peak:6.0f}",
            (16, 40), cv2.FONT_HERSHEY_SIMPLEX, sc, (0, 255, 0), 2,
        )
        if dec.payload:
            color = (0, 255, 0) if fresh else (60, 160, 160)
            for i, part in enumerate(dec.payload.split(" | ")):  # QR / DM on own lines
                cv2.putText(
                    view, part[:60], (16, 84 + i * 40),
                    cv2.FONT_HERSHEY_SIMPLEX, sc, color, 2,
                )
        cv2.imshow(win, view)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord("a"):
            auto = not auto
            _apply_exposure(cap, auto, expo)
        if k == ord("f"):
            focus_zoom = not focus_zoom
            focus_peak = 0.0                                    # reset peak when switching view
        if k == ord("e") and not auto:
            expo += 1
            cap.set(cv2.CAP_PROP_EXPOSURE, expo)
        if k == ord("d") and not auto:
            expo -= 1
            cap.set(cv2.CAP_PROP_EXPOSURE, expo)

    dec.stop()
    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
