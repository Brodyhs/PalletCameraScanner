r"""PalletScan Bench — live camera test UI (web), motion-capable + smart.

Standalone FastAPI/zxing camera bench (no pipeline). Replaces tools/live_decode.py.
Four "smart" features layered on the motion-capable capture path:

  1. MULTI-OBJECT MOTION — connected-components on the motion mask; decode the
     top-N largest moving blobs (the pallet's code is in one of them), filtering
     noise/thin streaks. Other movers don't pollute one giant ROI.
  2. MOTION-SAFE AUTO (auto-gain) — camera auto-exposure brightens by lengthening
     the shutter, which BLURS moving codes. "Auto" here instead holds a fixed SHORT
     exposure and auto-adjusts GAIN to a brightness target (shutter-priority AE), so
     auto works for motion too.
  3. CODE SPEED — track a decoded code's centroid frame-to-frame for px/s; with a
     known code size (mm) it self-calibrates mm/px from the code's pixel size and
     reports real mph that auto-corrects for distance.
  4. MISS TRACKING — mirrors the product's account-for-everything rule: a motion
     "pass" (sustained segment) that closes with zero decodes is a MISS. Live
     passes/reads/misses/read-rate + saved thumbnails. A verdict-grace window keeps
     a late decode from ever scoring a false miss. NB: misses are "unread MOTION
     passes" (a hand/person also counts) — a tuning signal, not ground truth.

Run:  .\.venv\Scripts\python.exe tools\bench.py        # opens http://127.0.0.1:8009
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

try:
    import zxingcpp

    _ZX_FMTS = (zxingcpp.BarcodeFormat.QRCode, zxingcpp.BarcodeFormat.DataMatrix)
except Exception:  # pragma: no cover - zxing optional
    zxingcpp = None
    _ZX_FMTS = ()

INDEX = 0  # MSMF index of the See3CAM_24CUG (OBS closed)
HOST, PORT = "127.0.0.1", 8009
_AE_MANUAL = 0.25  # MSMF manual-exposure magic value
_LINGER_S = 1.2
_FOCUS_W, _FOCUS_H = 800, 600

# --- motion params (mask is _MOTION_W x _MOTION_H) ---
_MOTION_W, _MOTION_H = 240, 150
_DIFF_THRESH = 18
_MIN_MOVING = 25  # min moving mask px to count as motion (noise floor)
_MOTION_PAD = 80
_GLOBAL_FRAC = 0.85  # whole-frame motion (AE hunt / camera bump) -> ignore as a pass

# --- segment debounce (mirror motion_gate defaults) ---
_OPEN_FRAMES = 3
_QUIET_FRAMES = 8
_MIN_PASS_FRAMES = 5  # shorter passes are "rejected", not counted
_VERDICT_GRACE = 30  # frames a closed pass waits for a late decode before verdict

# --- speed ---
_SPEED_ALPHA = 0.35
_CODE_PX_ALPHA = 0.2
_MPH_MM_S = 447.04  # 1 mph = 0.44704 m/s
_TRACK_STALE_S = 2.0

# --- auto-exposure (shutter-priority WITH a brightness floor) ---
# Probe finding: a too-short exposure is dark and stops decoding; the winning
# strategy is "shortest exposure that is still DECODABLY BRIGHT". So: brighten
# with GAIN first (keep the shutter short for motion); only LENGTHEN exposure
# when gain is maxed and still dark; only SHORTEN when gain floors out (light to
# spare). This parks on -6/-7 in a dim room and goes shorter when lit.
_AG_EXPOSURE = -7.0       # exposure Auto starts from
_AG_LO, _AG_HI = 80.0, 155.0   # wide decodable band -> stays put, no flicker
_AG_GAIN_CAP = 50.0       # raise gain to here before lengthening the exposure
_AG_EXP_LONG = -5.0       # longest exposure Auto will use (caps motion blur)
_AG_EXP_SHORT = -12.0     # shortest exposure (only reached with ample light)
_AG_KP, _AG_MAX_STEP, _AG_MIN_STEP = 0.2, 5.0, 0.5
_AG_PERIOD_S = 0.15

_RATE_WINDOW_S = 120.0
_MISS_DIR = Path("data/bench_misses")
_MISS_KEEP = 200


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _payload(b) -> str:
    raw = bytes(b.bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _code_px(pts) -> float:
    """Rotation-robust pixel size of a code quad (tl, tr, br, bl)."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    sides = [
        math.hypot(x1 - x0, y1 - y0),
        math.hypot(x2 - x1, y2 - y1),
        math.hypot(x3 - x2, y3 - y2),
        math.hypot(x0 - x3, y0 - y3),
    ]
    area = abs(
        (x0 * y1 - x1 * y0) + (x1 * y2 - x2 * y1)
        + (x2 * y3 - x3 * y2) + (x3 * y0 - x0 * y3)
    ) / 2.0
    return 0.5 * (max(sides) + math.sqrt(max(area, 1.0)))


def decode_region(img, ox: int, oy: int, sx: float, sy: float) -> list:
    """zxing-decode an image/crop; map the 4 position points to full-res coords.

    Module-level (not a method) so the offline harness tools/bench_sim.py runs
    the EXACT same decode the live bench does.
    """
    if zxingcpp is None:
        return []
    try:
        results = zxingcpp.read_barcodes(np.ascontiguousarray(img), formats=_ZX_FMTS)
    except Exception:
        return []
    out = []
    for b in results:
        if not b.valid:
            continue
        sym = "qr" if b.format == zxingcpp.BarcodeFormat.QRCode else "datamatrix"
        p = b.position
        pts = [
            (ox + int(pt.x * sx), oy + int(pt.y * sy))
            for pt in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)
        ]
        out.append((pts, _payload(b), sym))
    return out


def motion_box(prev_small, small, w: int, h: int):
    """Returns (roi, moving_px). roi = one padded box of motion, or None when
    there's no motion OR whole-frame motion (too big to be a single object).
    moving_px = raw changed-pixel count — used to freeze auto-exposure on ANY
    motion (a tidy box AND a whole-frame swing), so it never chases the scene."""
    th = cv2.threshold(
        cv2.absdiff(small, prev_small), _DIFF_THRESH, 255, cv2.THRESH_BINARY
    )[1]
    moving = int(np.count_nonzero(th))
    if not (_MIN_MOVING < moving < _GLOBAL_FRAC * th.size):
        return None, moving
    ys, xs = np.nonzero(th)
    sxf, syf = w / _MOTION_W, h / _MOTION_H
    x0 = max(0, int(xs.min() * sxf) - _MOTION_PAD)
    y0 = max(0, int(ys.min() * syf) - _MOTION_PAD)
    x1 = min(w, int(xs.max() * sxf) + _MOTION_PAD)
    y1 = min(h, int(ys.max() * syf) + _MOTION_PAD)
    roi = (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None
    return roi, moving


@dataclass
class Track:
    last_t: float
    last_cx: float
    last_cy: float
    ema_vx: float = 0.0
    ema_vy: float = 0.0
    ema_pps: float = 0.0
    ema_code_px: float = 0.0
    samples: int = 0


class Bench:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gray: np.ndarray | None = None
        self.frame_q: queue.Queue = queue.Queue(maxsize=6)  # (gray, roi, t, seg_id)
        self.fps = 0.0
        self.attempts_per_sec = 0.0
        self.brightness = 0.0
        self.focus = 0.0
        self.focus_peak = 0.0
        self.exposure = -7.0  # Auto adapts this; -7 is a good dim-room start point
        self.gain = 40.0
        self.autogain = True  # Auto-exposure is the DEFAULT — finds a bright, decodable setting
        self.track_misses = False  # miss tracking is opt-in (default off)
        self.code_size_mm = 0.0  # 0 = uncalibrated -> px/s only
        self.camera = "connecting…"
        self.note: str | None = None
        self.motion_rois: list = []
        self.overlays: deque = deque(maxlen=48)  # (pts, payload, sym, t)
        self.log: deque = deque(maxlen=150)
        self._last_logged: dict[str, float] = {}
        self._tracks: dict[str, Track] = {}
        # pass / miss tracking
        self._live_segs: dict[str, dict] = {}  # open or awaiting verdict
        self._open_id: str | None = None
        self._pending: deque = deque()  # (deadline_frame, seg_id)
        self._active_streak = 0
        self._quiet_streak = 0
        self._open_backdate = (0, 0.0)
        self._seg_count = 0
        self._run_token = time.strftime("%H%M%S")
        self.passes = self.reads = self.misses = self.rejected = 0
        self._rate_events: deque = deque(maxlen=4000)  # (t, kind)
        self.recent_misses: deque = deque(maxlen=12)
        self._to_save: list = []
        self._cmds: deque = deque()  # (cmd, value)
        self._att = 0
        self._gain_t = 0.0
        self._stop = threading.Event()

    def start(self) -> None:
        _MISS_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._capture_loop, daemon=True).start()
        for _ in range(2):
            threading.Thread(target=self._decode_loop, daemon=True).start()

    # ----- camera --------------------------------------------------------
    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(INDEX, cv2.CAP_MSMF)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1200)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"UYVY"))
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        cap.set(cv2.CAP_PROP_FPS, 55)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._apply_camera(cap)
        return cap

    def _apply_camera(self, cap: cv2.VideoCapture) -> None:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, _AE_MANUAL)  # always shutter-priority
        cap.set(cv2.CAP_PROP_EXPOSURE, self.exposure)
        cap.set(cv2.CAP_PROP_GAIN, self.gain)

    def _autogain_step(self, cap: cv2.VideoCapture, bright: float, now: float) -> None:
        """Auto-exposure. GAIN is the fine knob and seeks the brightness band;
        EXPOSURE is the coarse knob and only moves when gain rails — LONGER when
        gain is maxed and still dark, SHORTER when gain floors out and still
        bright. Result: it parks on the SHORTEST exposure that is still brightly
        decodable for the current light (-6/-7 in a dim room; shorter when lit)."""
        if not self.autogain or now - self._gain_t < _AG_PERIOD_S:
            return
        self._gain_t = now
        mid = (_AG_LO + _AG_HI) / 2
        step = _clamp(_AG_KP * abs(bright - mid), _AG_MIN_STEP, _AG_MAX_STEP)
        before = (self.exposure, self.gain)
        if bright < _AG_LO:
            if self.gain < _AG_GAIN_CAP:
                self.gain = min(_AG_GAIN_CAP, self.gain + step)
            elif self.exposure < _AG_EXP_LONG:  # gain maxed & dark -> longer exposure
                self.exposure, self.gain = self.exposure + 1, _AG_GAIN_CAP * 0.5
        elif bright > _AG_HI:
            if self.gain > 0.0:
                self.gain = max(0.0, self.gain - step)
            elif self.exposure > _AG_EXP_SHORT:  # gain floored & bright -> shorter
                self.exposure, self.gain = self.exposure - 1, _AG_GAIN_CAP * 0.5
        if (self.exposure, self.gain) != before:  # only touch the camera on a real change
            self._apply_camera(cap)
        if bright < _AG_LO and self.gain >= _AG_GAIN_CAP and self.exposure >= _AG_EXP_LONG:
            self.note = "Underlit — add light to read faster motion (or get closer)"
        elif bright > _AG_HI and self.gain <= 0.0 and self.exposure <= _AG_EXP_SHORT:
            self.note = "Overlit"
        else:
            self.note = None

    def _run_cmd(self, cap: cv2.VideoCapture, cmd: str, value: float) -> None:
        if cmd == "auto":  # motion-safe auto-gain
            self.autogain, self.exposure = True, _AG_EXPOSURE
        elif cmd == "manual":
            self.autogain = False
        elif cmd == "brighter" and not self.autogain:
            self.exposure = min(0.0, self.exposure + 1)
        elif cmd == "darker" and not self.autogain:
            self.exposure = max(-13.0, self.exposure - 1)
        elif cmd == "gain_up":
            self.autogain = False
            self.gain = min(63.0, self.gain + 5)
        elif cmd == "gain_down":
            self.autogain = False
            self.gain = max(0.0, self.gain - 5)
        elif cmd == "motion_freeze":
            self.autogain, self.exposure, self.gain = False, -10.0, 40.0
        elif cmd == "bright_static":
            self.autogain, self.exposure, self.gain = False, -6.0, 10.0
        elif cmd == "code_mm":
            self.code_size_mm = max(0.0, float(value))
            return
        elif cmd == "reset_peak":
            self.focus_peak = 0.0
            return
        elif cmd == "toggle_misses":
            self.track_misses = not self.track_misses
            self._active_streak = self._quiet_streak = 0
            self._open_id = None
            self._live_segs.clear()
            self._pending.clear()
            return
        elif cmd == "reset_rate":
            self.passes = self.reads = self.misses = self.rejected = 0
            self._rate_events.clear()
            self.recent_misses.clear()
            return
        else:
            return
        self._apply_camera(cap)

    # ----- capture + segment bookkeeping ---------------------------------
    def _capture_loop(self) -> None:
        cap = self._open()
        if not cap.isOpened():
            with self.lock:
                self.camera = f"camera[{INDEX}] FAILED to open (MSMF)"
            return
        with self.lock:
            self.camera = f"See3CAM @ MSMF[{INDEX}] · UYVY 1920×1200"
        prev_small = None
        last_t = att_t = last_static = time.monotonic()
        count, fps, att0, frame_idx = 0, 0.0, 0, 0
        while not self._stop.is_set():
            ok, img = cap.read()
            if not ok or img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            fx0, fy0 = (w - _FOCUS_W) // 2, (h - _FOCUS_H) // 2
            froi = gray[fy0 : fy0 + _FOCUS_H, fx0 : fx0 + _FOCUS_W]
            focus = float(cv2.Laplacian(froi, cv2.CV_64F).var())
            bright = float(gray.mean())
            now = time.monotonic()
            frame_idx += 1

            # ---- motion: ONE generously-padded box around all movement ----
            # A moving code shows up as its edges, which fragment into blobs; a
            # single padded box reliably CONTAINS the whole code (and zxing finds
            # it amid other motion), so this beats per-blob ROIs for capture.
            small = cv2.resize(gray, (_MOTION_W, _MOTION_H), interpolation=cv2.INTER_AREA)
            if prev_small is not None:
                roi, moving = motion_box(prev_small, small, w, h)
            else:
                roi, moving = None, 0
            prev_small = small
            rois = [roi] if roi is not None else []
            active = roi is not None

            # Auto-exposure adapts to AMBIENT light only and is FROZEN on ANY
            # motion (tidy box OR whole-frame swing) so it never chases the scene
            # and flickers. Whole-frame brightness, steadier than the center patch.
            if moving <= _MIN_MOVING:
                self._autogain_step(cap, bright, now)

            count += 1
            if now - last_t >= 0.5:
                fps, count, last_t = count / (now - last_t), 0, now
            if now - att_t >= 0.5:
                with self.lock:
                    att = self._att
                self.attempts_per_sec, att0, att_t = (att - att0) / (now - att_t), att, now

            # ---- pass/miss bookkeeping (OPT-IN; capture thread owns it) ----
            with self.lock:
                seg_id = None
                if self.track_misses:
                    if active:
                        self._quiet_streak = 0
                        self._active_streak += 1
                        if self._active_streak == 1:
                            self._open_backdate = (frame_idx, now)
                        if self._open_id is None and self._active_streak >= _OPEN_FRAMES:
                            self._seg_count += 1
                            sid = f"bench-{self._run_token}-{self._seg_count:06d}"
                            of, ots = self._open_backdate
                            self._live_segs[sid] = {
                                "id": sid, "open_frame": of, "open_ts": ots,
                                "decodes": 0, "last_active": frame_idx,
                                "best_focus": -1.0, "best_gray": None, "boxes": [],
                            }
                            self._open_id = sid
                        if self._open_id is not None:
                            seg = self._live_segs[self._open_id]
                            seg["last_active"] = frame_idx
                            if focus > seg["best_focus"]:
                                seg["best_focus"], seg["best_gray"], seg["boxes"] = (
                                    focus, gray, list(rois),
                                )
                    else:
                        self._active_streak = 0
                        if self._open_id is not None:
                            self._quiet_streak += 1
                            if self._quiet_streak >= _QUIET_FRAMES:
                                self._pending.append(
                                    (frame_idx + _VERDICT_GRACE, self._open_id)
                                )
                                self._open_id, self._quiet_streak = None, 0
                    while self._pending and self._pending[0][0] <= frame_idx:
                        self._finalize(self._pending.popleft()[1], now)
                    seg_id = self._open_id
                self.gray, self.motion_rois = gray, rois
                self.focus, self.brightness = focus, bright
                self.focus_peak = max(self.focus_peak, focus)
                self.fps = fps
                cmds = list(self._cmds)
                self._cmds.clear()
                to_save = self._to_save
                self._to_save = []

            for seg in to_save:  # JPEG encode outside the lock
                self._save_miss(seg)
            # one decode work item: the single motion box, else a throttled static scan
            if roi is not None:
                self._enqueue((gray, roi, now, seg_id))
            elif now - last_static > 0.2:
                last_static = now
                self._enqueue((gray, None, now, None))
            for cmd, value in cmds:
                self._run_cmd(cap, cmd, value)

        with self.lock:  # flush the last open pass on shutdown
            if self._open_id is not None:
                self._finalize(self._open_id, time.monotonic())
        cap.release()

    def _enqueue(self, item) -> None:
        try:
            self.frame_q.put_nowait(item)
        except queue.Full:
            try:
                self.frame_q.get_nowait()
                self.frame_q.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    def _finalize(self, sid: str, now: float) -> None:
        """Verdict a closed pass. Caller holds self.lock."""
        seg = self._live_segs.pop(sid, None)
        if seg is None:
            return
        pass_frames = seg["last_active"] - seg["open_frame"] + 1
        if pass_frames < _MIN_PASS_FRAMES:
            self.rejected += 1
            self._rate_events.append((now, "rejected"))
            return
        self.passes += 1
        if seg["decodes"] > 0:
            self.reads += 1
            self._rate_events.append((now, "read"))
        else:
            self.misses += 1
            self._rate_events.append((now, "miss"))
            if seg["best_gray"] is not None:
                self._to_save.append(seg)

    def _save_miss(self, seg: dict) -> None:
        gray = seg["best_gray"]
        if gray is None:
            return
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        for (x, y, bw, bh) in seg["boxes"]:
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (80, 200, 255), 3)
        cv2.putText(img, f"MISS {seg['id']}", (16, 40), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (80, 200, 255), 2)
        img = cv2.resize(img, (1280, 800), interpolation=cv2.INTER_AREA)
        name = f"{seg['id']}.jpg"
        cv2.imwrite(str(_MISS_DIR / name), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        with self.lock:
            self.recent_misses.appendleft(
                {"id": seg["id"], "t": time.strftime("%H:%M:%S"), "url": f"/miss/{name}"}
            )
        files = sorted(_MISS_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in files[:-_MISS_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass

    # ----- decode --------------------------------------------------------
    def _decode_loop(self) -> None:
        while not self._stop.is_set():
            try:
                gray, roi, t, seg_id = self.frame_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if roi is not None:
                x, y, w, h = roi
                found = decode_region(gray[y : y + h, x : x + w], x, y, 1.0, 1.0)
            else:
                found = decode_region(gray, 0, 0, 1.0, 1.0)
            now = time.monotonic()
            with self.lock:
                self._att += 1
                if found and seg_id is not None and seg_id in self._live_segs:
                    self._live_segs[seg_id]["decodes"] += 1
                for pts, payload, sym in found:
                    self.overlays.append((pts, payload, sym, now))
                    if now - self._last_logged.get(payload, -9) > 2.0:
                        self.log.appendleft((time.strftime("%H:%M:%S"), sym, payload))
                    self._last_logged[payload] = now
                    self._update_track(payload, pts, t)
                # prune stale tracks
                for p in [p for p, tr in self._tracks.items() if t - tr.last_t > _TRACK_STALE_S]:
                    del self._tracks[p]

    def _update_track(self, payload: str, pts, t: float) -> None:
        cx = sum(p[0] for p in pts) / 4.0
        cy = sum(p[1] for p in pts) / 4.0
        wpx = _code_px(pts)
        tr = self._tracks.get(payload)
        if tr is None:
            self._tracks[payload] = Track(t, cx, cy, ema_code_px=wpx)
            return
        if t <= tr.last_t:  # out-of-order worker; don't advance velocity
            return
        dt = t - tr.last_t
        if 1e-3 < dt < 0.5:
            vx, vy = (cx - tr.last_cx) / dt, (cy - tr.last_cy) / dt
            inst = math.hypot(vx, vy)
            if not (tr.samples >= 1 and inst > 4 * max(tr.ema_pps, 1e-6)):
                tr.ema_vx = _SPEED_ALPHA * vx + (1 - _SPEED_ALPHA) * tr.ema_vx
                tr.ema_vy = _SPEED_ALPHA * vy + (1 - _SPEED_ALPHA) * tr.ema_vy
                tr.ema_pps = math.hypot(tr.ema_vx, tr.ema_vy)
                tr.samples += 1
        tr.last_t, tr.last_cx, tr.last_cy = t, cx, cy
        tr.ema_code_px = _CODE_PX_ALPHA * wpx + (1 - _CODE_PX_ALPHA) * tr.ema_code_px

    # ----- views ---------------------------------------------------------
    def render_jpeg(self) -> bytes | None:
        with self.lock:
            gray = self.gray
            t = time.monotonic()
            overlays = [o for o in self.overlays if t - o[3] < _LINGER_S]
            rois = list(self.motion_rois)
        if gray is None:
            return None
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        h, w = gray.shape
        fx0, fy0 = (w - _FOCUS_W) // 2, (h - _FOCUS_H) // 2
        cv2.rectangle(img, (fx0, fy0), (fx0 + _FOCUS_W, fy0 + _FOCUS_H), (70, 90, 110), 1)
        for (x, y, bw, bh) in rois:
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (80, 200, 255), 2)
        for pts, payload, sym, _ in overlays:
            arr = np.array(pts, np.int32)
            cv2.polylines(img, [arr], True, (80, 255, 80), 4)
            cv2.putText(img, f"{sym.upper()}: {payload}",
                        (int(arr[:, 0].min()), max(30, int(arr[:, 1].min()) - 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.95, (80, 255, 80), 2)
        img = cv2.resize(img, (1280, 800), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def status(self) -> dict:
        with self.lock:
            t = time.monotonic()
            seen, now = set(), []
            for pts, pl, s, ot in self.overlays:
                if t - ot < _LINGER_S and pl not in seen:
                    seen.add(pl)
                    tr = self._tracks.get(pl)
                    pxs = round(tr.ema_pps) if tr and tr.samples >= 2 else None
                    mph = None
                    if (tr and tr.samples >= 2 and self.code_size_mm > 0
                            and tr.ema_code_px > 0):
                        mph = round(tr.ema_pps * (self.code_size_mm / tr.ema_code_px)
                                    / _MPH_MM_S, 1)
                    now.append({"sym": s, "payload": pl, "px_s": pxs, "mph": mph})
            cutoff = t - _RATE_WINDOW_S
            while self._rate_events and self._rate_events[0][0] < cutoff:
                self._rate_events.popleft()
            kinds = [k for _, k in self._rate_events]
            wr_reads, wr_miss = kinds.count("read"), kinds.count("miss")
            wr_pass = wr_reads + wr_miss
            return {
                "camera": self.camera,
                "fps": round(self.fps, 1),
                "attempts_per_sec": round(self.attempts_per_sec),
                "focus": round(self.focus),
                "focus_peak": round(self.focus_peak),
                "brightness": round(self.brightness),
                "gain": round(self.gain),
                "autogain": self.autogain,
                "track_misses": self.track_misses,
                "exposure_mode": (f"AUTO-GAIN {self.exposure:+.0f}" if self.autogain
                                  else f"MANUAL {self.exposure:+.0f}"),
                "code_size_mm": round(self.code_size_mm),
                "note": self.note,
                "passes": wr_pass,
                "reads": wr_reads,
                "misses": wr_miss,
                "rejected": kinds.count("rejected"),
                "read_rate": round(100 * wr_reads / wr_pass) if wr_pass else None,
                "window_s": round(_RATE_WINDOW_S),
                "recent_misses": list(self.recent_misses),
                "reading_now": now,
                "log": [{"t": a, "sym": b, "payload": c} for a, b, c in self.log],
            }


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>PalletScan Bench</title>
<style>
:root{--bg:#14171c;--panel:#1d222b;--line:#2c3340;--text:#dde3ec;--muted:#8a94a6;--accent:#4cc38a;--warn:#e5a50a;--miss:#e05c5c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:baseline;gap:14px;padding:12px 20px;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:20px;color:var(--accent)}
.cam{color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--miss);display:inline-block;margin-right:6px;vertical-align:middle}
.dot.live{background:var(--accent)}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 350px;gap:16px;padding:16px 20px;max-width:1560px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.card+.card{margin-top:12px}
.feed img{display:block;width:100%;border-radius:6px;background:#000}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.banner{display:none;background:rgba(229,165,10,.14);border:1px solid var(--warn);color:var(--warn);border-radius:6px;padding:8px 12px;margin:0 20px}
.focus-num{font-size:40px;font-weight:700;line-height:1}
.focus-num small{font-size:14px;color:var(--muted);font-weight:400}
.bar{height:16px;background:var(--bg);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:10px 0 6px;position:relative}
.bar>span{display:block;height:100%;background:var(--accent);width:0%;transition:width .12s}
.peakmark{position:absolute;top:0;bottom:0;width:2px;background:var(--warn)}
.hint{color:var(--muted);font-size:12px}
.tiles{display:flex;gap:8px;flex-wrap:wrap}
.tile{flex:1;min-width:64px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;text-align:center}
.tile .v{font-size:18px;font-weight:600}.tile .l{font-size:10px;color:var(--muted);text-transform:uppercase}
.tile.good .v{color:var(--accent)}.tile.warn .v{color:var(--warn)}.tile.bad .v{color:var(--miss)}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
button{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 11px;font-weight:600;cursor:pointer}
button:hover{border-color:var(--accent)}
button.on{background:var(--accent);color:#0c2018;border-color:var(--accent)}
button.preset{flex:1;background:#222a35}
input{background:var(--bg);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:6px 8px;width:70px}
table{border-collapse:collapse;width:100%}
th,td{padding:5px 9px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
th{color:var(--muted);font-size:11px;text-transform:uppercase}
.sym{font-weight:700}.sym.qr{color:var(--accent)}.sym.datamatrix{color:var(--warn)}
.spd{color:var(--muted)}
.full{grid-column:1/-1}
code{color:var(--text);word-break:break-all}
.misses{display:flex;gap:8px;overflow-x:auto}
.misses figure{margin:0;flex:0 0 auto}
.misses img{height:96px;border-radius:4px;border:1px solid var(--miss)}
.misses figcaption{font-size:10px;color:var(--muted)}
</style></head>
<body>
<header><h1>PalletScan Bench</h1><span class="cam"><span id="dot" class="dot"></span><span id="cam">connecting…</span></span></header>
<div id="banner" class="banner"></div>
<div class="wrap">
  <div class="card feed"><img id="feed" src="/stream.mjpg" alt="live feed"></div>
  <div>
    <div class="card">
      <h2>Status <span class="hint">— fully automatic</span></h2>
      <div class="tiles">
        <div class="tile"><div class="v" id="fps">–</div><div class="l">fps</div></div>
        <div class="tile"><div class="v" id="att">–</div><div class="l">decodes/s</div></div>
        <div class="tile"><div class="v" id="bright">–</div><div class="l">bright</div></div>
      </div>
      <div class="tiles" style="margin-top:8px">
        <div class="tile"><div class="v" id="expo">–</div><div class="l">exposure</div></div>
        <div class="tile"><div class="v" id="gain">–</div><div class="l">gain</div></div>
      </div>
      <div class="hint" style="margin-top:8px">Exposure and gain are set automatically for the brightest decodable image — nothing to adjust.</div>
    </div>
    <div class="card">
      <h2>Focus — turn lens to maximize</h2>
      <div class="focus-num"><span id="focus">0</span> <small>peak <span id="peak">0</span></small></div>
      <div class="bar"><span id="focusbar"></span><div class="peakmark" style="left:100%"></div></div>
      <div class="hint" id="focushint">put a code in the box and turn the focus ring</div>
    </div>
    <div class="card">
      <h2>Reading now</h2>
      <div id="now" class="hint">—</div>
    </div>
  </div>
  <div class="card full">
    <h2>Scan log</h2>
    <table><thead><tr><th style="width:90px">time</th><th style="width:90px">type</th><th>payload</th></tr></thead><tbody id="log"></tbody></table>
  </div>
</div>
<script>
function esc(s){return (''+s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function spd(d){ return d.px_s!=null ? ` <span class="spd">— ${d.px_s} px/s</span>` : ''; }
async function poll(){
 try{
  const s = await (await fetch('/status.json')).json();
  cam.textContent = s.camera; dot.className = 'dot' + (s.fps>0?' live':'');
  const b=document.getElementById('banner'); if(s.note){b.textContent=s.note;b.style.display='block';}else{b.style.display='none';}
  focus.textContent=s.focus; peak.textContent=s.focus_peak;
  const pct=s.focus_peak>0?Math.min(100,100*s.focus/s.focus_peak):0;
  focusbar.style.width=pct+'%'; const near=pct>92;
  focusbar.style.background=near?'var(--accent)':'var(--warn)';
  focushint.textContent=near?'sharp — lock the lens here':'turn the focus ring to push the bar right';
  fps.textContent=s.fps; att.textContent=s.attempts_per_sec; bright.textContent=s.brightness;
  expo.textContent=s.exposure_mode; gain.textContent=s.gain;
  now.innerHTML=s.reading_now.length?s.reading_now.map(d=>`<span class="sym ${d.sym}">${d.sym.toUpperCase()}</span> <code>${esc(d.payload)}</code>${spd(d)}`).join('<br>'):'—';
  log.innerHTML=s.log.map(e=>`<tr><td>${e.t}</td><td><span class="sym ${e.sym}">${e.sym.toUpperCase()}</span></td><td><code>${esc(e.payload)}</code></td></tr>`).join('');
 }catch(e){ dot.className='dot'; }
}
setInterval(poll,250); poll();
</script>
</body></html>"""


def build_app(bench: Bench) -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/status.json")
    def status() -> JSONResponse:
        return JSONResponse(bench.status())

    @app.post("/control")
    def control(action: str, value: float = 0.0) -> JSONResponse:
        bench._cmds.append((action, value))
        return JSONResponse({"ok": True})

    @app.get("/miss/{name}")
    def miss(name: str):
        if "/" in name or "\\" in name or ".." in name:
            return JSONResponse({"error": "bad name"}, status_code=400)
        path = _MISS_DIR / name
        if not path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(path), media_type="image/jpeg")

    @app.get("/stream.mjpg")
    def stream(_request: Request) -> StreamingResponse:
        def gen():
            while True:
                jpg = bench.render_jpeg()
                if jpg is not None:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                time.sleep(1 / 20)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    if zxingcpp is None:
        print("WARNING: zxing-cpp not installed — `pip install zxing-cpp`.")
    bench = Bench()
    bench.start()
    url = f"http://{HOST}:{PORT}"
    print(f"PalletScan Bench -> open {url}  (Ctrl+C to stop)")
    threading.Timer(1.0, lambda: __import__("webbrowser").open(url)).start()
    uvicorn.run(build_app(bench), host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
