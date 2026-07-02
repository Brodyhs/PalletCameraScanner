# Bench Protocol — real-world testing with the actual cameras

> The hands-on drills for moving PalletScan from synthetic validation to real
> hardware: single-camera shakeout → box-wave decode + **miss-flag** validation
> → dual-camera A/B → pallet field day. Each section ends with what to record.
> Companion configs: `config/live_color.yaml`, `config/live_mono.yaml`,
> `config/bench_wave.yaml` (all derived from `config/station.yaml`;
> station.yaml itself stays synthetic until §4's A/B flip).
>
> Working-distance reality (stock 3 mm lenses, OPTICS_SPEC §2): ≥5 px/module
> on a 5 mm-module code holds only to ~3 ft (color) / ~4 ft (mono). Expect the
> smallest printed codes to need the near end. That's the glass, not the code.

## 0. Before anything

- venv: `pip install -e ".[dev,zxing]"` (zxing is the deployed engine).
- **Keep OBS Virtual Camera closed** (it shifts MSMF indexes) and e-CAMView
  closed (cameras are exclusive).
- Fast suite green: `python -m pytest -m "not acceptance and not soak_short" -q`
  (3 documented Windows-env failures are expected; nothing else).

## 1. Single-camera hardware shakeout (Phase 1)

Per camera — color first (proven path), then mono (first post-fix hardware run):

1. `palletscan calibrate --list` — both See3CAMs enumerate, identity shown.
2. `palletscan calibrate --camera cam-color --save --config config\live_color.yaml`
   then `--camera cam-mono --save --config config\live_mono.yaml`.
   Mono notes: expect *honest* fps reporting (a "set() rejected"-style WARNING
   if the Y8 capability advertises a rate range is documented behavior, not a
   failure); calibrate now refuses to lock a mode the device never streamed.
3. `palletscan selftest --config config\live_color.yaml` (then live_mono) —
   green with the real camera (controls may WARN on readback; the
   exposure-EFFECT check is the hard gate).
4. Mono stress: `python tools\mono_reconnect_check.py` (COM reconnect loop)
   and `python tools\mono_camerasource_check.py`.
5. ARRIVAL_CHECKLIST **§6** per arm: start `run`, yank the USB cable, replug —
   reconnect < 10 s, `source.reconnects` increments, brightness unchanged.
   **Tick the §1–§6 boxes this time.**
6. Focus + exposure at your bench distance:
   - `python tools\live_decode.py` — turn the M12 ring to maximize the focus
     score at the distance you'll test at; confirm each printed code size
     decodes when held there.
   - `python tools\expo_probe.py` — wave a code for ~20 s; note the shortest
     exposure that stays bright AND decodes in motion.
   - `python tools\brightness_check.py` — sanity on operating points.

**Record:** measured fps per arm, chosen focus distance, decode-able size ×
distance table from live_decode, exposure ladder result → ASSUMPTIONS (#74+).

## 2. Box-wave drill — decode + miss flagging (Phase 3, the core validation)

```
palletscan run --dashboard --config config\bench_wave.yaml --data-dir data\bench
```

Dashboard at the printed URL. The visual language: **amber box** = motion
segment open (a "pallet-like object" is passing); **green box** = decode.

| # | Drill | Expected |
|---|-------|----------|
| 1 | Wave each code size through view at 2 / 3 / 4 ft, deliberate speed | Amber → green → `[PASS]` console line, pass tile on dashboard |
| 2 | Same wave, code covered / blank side toward camera | Amber, **no green** → after the segment closes + ~2 s post-roll: `[MISS]` line → **miss in the dashboard gallery with evidence frames** |
| 3 | Hold a code static in view | Idle scan reads it every ~2 s (shown + counted as idle reads; **no** pass/miss — idle reads never touch accounting) |
| 4 | Very fast flick-through | Find where the gate stops opening (no amber): that's the debounce envelope; note the speed |
| 5 | Edge-clip pass (object barely enters frame) | Find where min_area_frac stops tripping; note the size/overlap |
| 6 | Empty-handed wave (no box) | Amber then a MISS — **correct behavior** (motion with no decode IS a miss); this is what pallet-shaped-but-unlabeled traffic will do |

Tuning loop (edit `config/bench_wave.yaml`, restart):
- Segment never opens for the box → lower `min_area_frac` (0.003 → 0.002);
  hands/shadows minting segments → raise it.
- Codes decode held but not waved → shorten exposure (settings.exposure) per
  the expo_probe result; light the zone if too dark at short exposure.
- Set `decode.payload_pattern` to your printed format to kill decoder
  false-positives during the drill.

**Caveats (both bit in real drills):**
- **Identical payloads within `dedup.window_s` (12 s) merge into ONE pass.**
  Wave *different* codes back-to-back, or space repeats > 12 s — otherwise
  your counts will look short and they aren't.
- A miss appears ~`buffer.post_s` (2 s) *after* the segment closes (post-roll
  evidence harvest) — don't call it unflagged too early.

**Record:** per-size × per-distance decode results, the miss-drill outcome
(evidence frames present?), final tuned knob values → ASSUMPTIONS + profile
comments.

## 3. Session drill (after the session interface lands — Phase 4)

1. `palletscan run --dashboard --config config\bench_wave.yaml --data-dir data\bench`
2. Dashboard → Session panel → expected count (e.g. 5) → **Start**.
3. Wave 4 decodable codes + 1 covered box.
4. Watch Expected / Decoded / Missed / Shortfall live (4 / 1 / 0 at the end).
5. **Close Out** — counts match (4+1=5): closes clean with the reconciliation.
6. Repeat with expected 6 (one short): Close Out demands an acknowledgement
   note before closing — type why ("only 5 objects presented") and confirm.

## 4. Dual-camera A/B flip (Phase 5)

1. `python tools\dual_camera_check.py` — both cameras streaming at once.
2. Flip `config/station.yaml`: `source.type: camera`,
   `source.cameras: [cam-color, cam-mono]`, add `motion.open_s: 0.055` /
   `quiet_s: 0.145`, **and update its header deviations list** (a test pins it).
3. `palletscan run --dashboard --config config\station.yaml --data-dir data\live`
4. Drills: shared-view wave → ONE business pass (cross-camera merge; A/B
   report shows per-camera detail); cover one lens → that arm misses, the
   business pass still counts once; unplug one arm mid-run → watchdog
   reconnect; session drill in A/B mode (sessions count business-level).
5. ARRIVAL_CHECKLIST §7: 30-minute stability + `python tools\measure_cpu.py`
   — record the spec §11 CPU number.

## 5. Pallet field day (Phase 6)

- `recording.enabled: true` for the test window (segment recorder — every
  pass/miss becomes a replayable burst under `data/recordings`).
- One session per pallet batch; expected count = batch size.
- Bring the printed-code box as a control object (known-good decode).
- Codes on pallets at the heights/angles the forklift actually presents.
- After each batch: Close Out → review misses in the gallery (evidence tells
  you *why*: blur, angle, size, glare).
- End of day: replay recorded misses under legacy vs zxing
  (`tools/replay_bursts.py`) before changing any config.

**Record:** per-batch session reports, miss causes from evidence review,
recording corpus location → ASSUMPTIONS + the Phase 6 promotion bench inputs.
