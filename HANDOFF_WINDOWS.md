# PalletScan — Session Handoff (Mac dev → Windows 11 bring-up)

> **Read this first.** These are complete notes from a working session done on a
> macOS arm64 dev machine, written so the work can continue on the **Windows 11
> factory PC** — which is the real production target and where the cameras will
> actually run. Addressed to both the human (Brody) and the next Claude Code
> session that opens this repo on Windows.
>
> **Date of session:** 2026-06-19 · **Reviewer model:** Claude Opus 4.8
> **Companion file:** `REVIEW_macos_arrival.md` (the full 47-finding code review)

---

## ⚡ UPDATE — 2026-06-19 evening (Windows bring-up session 2, Claude Opus 4.8)

The Windows 11 bring-up actually started this evening on the factory PC. **Read this
before the original notes below** — several original assumptions were corrected on real
hardware. Full state also lives in agent memory (`windows-bringup-progress`,
`cam24cug-needs-msmf`).

**Environment is up and green.** Fresh `.venv` (Py 3.13.1), `pip install -e ".[dev]"`
clean (all native Windows wheels incl. pygrabber+comtypes). `synth` = 100% read,
`selftest --skip-camera` OK. Windows fast-suite baseline: **458 passed / 4 failed / 1 skipped**.

**Patches applied this session:**
- **5.1** `watchdog.max_outage_s: 120` — placed in **`config/station.yaml`** (NOT `default.yaml`,
  which `test_config.py` pins to mirror code defaults; station.yaml is the deployment config per RUNBOOK).
- **5.2** hardened `measure_achieved_fps` (wall-clock deadline + consecutive-failure cap). Verified on hardware.
- **NEW — UTF-8 console fix** in `cli.py main()`: Windows cp1252 was crashing selftest/synth/calibrate
  report output with `UnicodeEncodeError` (invisible on macOS). All CLI output now forces UTF-8.

**CRITICAL camera finding — the 24CUG needs MSMF, not DSHOW/auto:**
- OpenCV's DirectShow backend can't negotiate this camera and pins it at **15 fps**. **MSMF works**
  (54 fps UYVY). `backend: auto` resolves to DSHOW on Windows, so it MUST be set to `msmf` explicitly.
- MSMF requires `fallback_index` (name enum is DSHOW-only) → forfeits name stability → **keep OBS
  Virtual Camera closed** (it takes a DSHOW index). The 24CUG is MSMF index 0.
- **The datasheet (Table 2) corrects §1 below: there is NO UYVY 1920×1200@120.** UYVY @ 1920×1200
  is **55 fps max** (bandwidth); the only >55 path is **MJPEG@114**, which our OpenCV stack can't
  realize (`cv2.read()` decodes MJPEG inline → ~42 fps). **Locked mode: `MSMF + UYVY 1920×1200@55`.**
  55 fps actually *eases* Findings 2/3 (per-frame budget ~18 ms vs ~8 ms at 120).
- **MSMF FOURCC readback is GARBAGE ("????")** — contradicts §6 / ARRIVAL_CHECKLIST §8's "MSMF FOURCC
  is truthful". Exposure/gain/auto-exposure readback are also unreliable, though the controls physically
  work (exposure-effect verifies). Fix applied: `QUIRKS[Backend.MSMF].controls_reliable=False` (warns
  instead of hard-failing, like AVFoundation; exposure-effect + achieved-fps remain the real gates);
  the calibrate/selftest "reliable backend" tests were repointed to DSHOW (whose readback IS truthful).
- Result: `calibrate cam-color` exits 0 and **`selftest --config config\station.yaml` is GREEN with the
  real camera** (controls WARN, exposure_effect + fps PASS). `config/station.yaml` holds the locked entry.

**The 4 Windows test failures** (none from these changes; first-ever-on-Windows): `test_devices`
(re-imports pygrabber — test-only, and proves enumeration works), `test_supervisor` (`_FakeProc` lacks
`_handle` — test-only), `test_instance_lock` (lock records inner pid ≠ `Popen.pid` on a venv — verify in
§9 taskkill/`-Hard`), `test_http_sink` (outbox byte-vs-char cap — the known Low finding).

**Still pending:** 37CUGM (mono) arrives ~2026-06-20 — start it on MSMF too. Real exposure/gain tuning
vs actual pallets. ARRIVAL_CHECKLIST §6/§7/§9. NOTE: `calibrate --save` strips YAML comments
(`upsert_camera_yaml`) — prefer hand-editing `station.yaml` to keep its documentation.

---

## 0. TL;DR — where we are

- The repo is **code-complete through Phases 1–5** and very high quality. Fast
  test suite is **green (463 passed)** on macOS / Python 3.13.
- The **entire live-camera stack has never touched real hardware.** Cameras have
  now arrived. Bring-up moves to **Windows 11** (the supported target; one of the
  two cameras effectively only runs there).
- A deep multi-agent code review ran this session: **104 raw findings → 47
  confirmed**, **0 Critical**. Full report in `REVIEW_macos_arrival.md`. That
  review was *weighted toward macOS*; **§4 below re-prioritizes every finding for
  Windows** — read that, not just the raw report.
- Nothing in the code was changed this session. Two small **pre-live hardening
  patches are recommended but NOT yet applied** (§5). Decide whether to apply
  them before first connect.
- **Primary bring-up procedure = `ARRIVAL_CHECKLIST.md`** (already in the repo,
  written for Windows). This handoff layers the review findings on top of it.

---

## 1. The hardware

| id (suggested) | device | sensor | headline mode | notes |
|---|---|---|---|---|
| `cam-color` | **See3CAM_24CUG** | color | 1920×1200 @ 120 fps (UYVY or MJPG) | UVC global shutter |
| `cam-mono`  | **See3CAM_37CUGM** | mono | 2064×1552 @ 72 fps (GREY/Y8) | UVC global shutter; **Windows-only in practice** |

Cameras are configured by **device-name substring** (`cameras[].name`), never by
bare index (indexes shuffle on replug). Name enumeration on Windows is
DirectShow via `pygrabber` (auto-installed by `pip install -e .` on Windows).

---

## 2. What this session did (on the Mac — for context only)

The Mac setup below is **informational**; the Windows steps are in §3. On macOS:

1. `brew install libdmtx` (zbar was already installed). These are the native
   decoder libs that `pyzbar` / `pylibdmtx` dlopen. **On Windows you do NOT need
   this** — the Windows wheels bundle the DLLs.
2. Created `.venv` with **Python 3.13.2**, `pip install -e ".[dev]"`.
3. Verified decoder dylibs load, then ran:
   - `pytest -m "not acceptance and not soak_short"` → **463 passed** (~76 s).
     (Two warnings are benign: a Starlette/httpx deprecation, and a *deliberate*
     injected thread failure inside a dedup stress test that the test asserts on.)
   - `palletscan selftest --skip-camera` → green.
   - `palletscan synth --passes 10 --seed 7` → **100% read rate, 0 unaccounted**
     (full pipeline works end-to-end through real zbar + libdmtx).
   - `palletscan calibrate --list` → enumerated the Mac's webcam + iPhone on
     AVFoundation (proves the macOS enumeration path works; not the prod path).

**Versions resolved on Mac (Windows will resolve similar):** opencv-python
4.13.0, numpy 2.4.6, pydantic 2.13.4, fastapi 0.138.0, uvicorn 0.49.0,
pyzbar 0.1.9, pylibdmtx 0.1.10, pytest 9.1.1.

---

## 3. Windows 11 setup — hit the ground running

> Run these in **PowerShell**, from the repo root, on the Windows 11 box.

### 3.1 Do NOT reuse the Mac artifacts
- **Delete any copied `.venv\`** — it contains macOS arm64 binaries and will not
  work on Windows. Recreate it fresh (below).
- `data\` is throwaway scratch (events, evidence, logs from the Mac synth run).
  Safe to delete; it's gitignored.
- This folder is **not a git repo** currently. Optional: `git init` for history.

### 3.2 Install Python 3.13
Install Python **3.13** from python.org (check "Add to PATH"). Match the version
used in dev to avoid wheel surprises. Confirm: `py -3.13 --version`.

### 3.3 Create the venv and install
```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
python -m pip install --upgrade pip
pip install -e ".[dev]"               # pulls pygrabber automatically on Windows
```
- **No `brew` / no native lib install needed** — `pyzbar`/`pylibdmtx` Windows
  wheels bundle the DLLs. `pygrabber` (DirectShow device-name enumeration)
  installs automatically because of the `sys_platform == 'win32'` marker in
  `pyproject.toml`.

### 3.4 Smoke-test (no camera yet)
```powershell
palletscan version
pytest -m "not acceptance and not soak_short" -q        # expect ~463 passed
palletscan selftest --skip-camera
palletscan synth --passes 10 --seed 7                   # expect 100% read rate, 0 unaccounted
palletscan synth --ab --dashboard                       # open http://127.0.0.1:8000 — full UI, no hardware
```
If decoders fail to import on Windows (rare): the wheels bundle the DLLs, so this
usually means a corrupt wheel — `pip install --force-reinstall pyzbar pylibdmtx`.

### 3.5 Then: cameras
Plug in, then follow **`ARRIVAL_CHECKLIST.md`** start to finish (it's the
authoritative, Windows-oriented bring-up procedure). §6 below adds review-driven
watch-items on top of it.

---

## 4. The code review, RE-PRIORITIZED for Windows

The full review (`REVIEW_macos_arrival.md`) was weighted toward macOS/AVFoundation.
Here is what actually matters **on Windows**, in priority order. "Confirmed"
means I read the code and verified the defect this session.

### Applies fully on Windows — worth fixing / watching
| # | Finding | File | Windows relevance |
|---|---|---|---|
| **2** | **QR (pyzbar) decode has no time cap** on full-res motion ROIs; only the DataMatrix branch respects the budget. At 120/72 fps the per-frame budget is ~8–14 ms; a big-ROI QR scan blows it, stalls the single pipeline thread, and the dropping queue silently drops frames — possibly the only decodable frame of a pass. | `pipeline/decode_engine.py:178-190` | **HIGH — this is the real production target.** Never seen because tests used tiny synthetic crops. |
| **3** | **Motion debounce is frame-counts, not time.** `open_frames=3`/`quiet_frames=8` were tuned at 30 fps. At 120 fps, "8 quiet frames" = 67 ms → one pass splits into several; spurious misses; **breaks A/B since the two cameras run at different fps** (120 vs 72) and debounce differently. | `pipeline/motion_gate.py:109,126` | **HIGH.** Directly corrupts the "every pass accounted for" invariant and A/B attribution. |
| **5** | **`watchdog.max_outage_s` defaults to `null` (off).** A camera that opens but never delivers frames scans nothing forever while the supervisor reports "healthy." | `config.py:523`, `config/default.yaml:74` | **HIGH.** Platform-independent. Set this before any 24/7 run. |
| **1** | **`measure_achieved_fps` can hang the connect/reopen path** on a blocking `cap.read()` (warmup loop is unbounded; sample loop only checks the clock between reads). | `sources/controls.py:271-292` | **MEDIUM-HIGH.** DSHOW/MSMF reads can also block on a wedged device. A native blocked read can't be interrupted by a Python signal handler → may need to kill the process. |
| **14** | **A hung native `VideoCapture` open during reopen can hang the whole station**; joins in `app.run()` have no timeout, and `reopen()` nulls `_cap` before the constructor returns so `close()` can't release it. | `app.py:326-335,444-483` + camera reopen | **MEDIUM.** ARRIVAL_CHECKLIST §8 already flags **MSMF hung-read-on-release** — same family. Real on Windows. |
| **8** | **Variant fan-out tasks keep running after a winner is found** (cancel can't stop in-flight C calls), occupying workers and degrading fallback latency on stubborn frames. | `pipeline/decode_engine.py:205-229` | MEDIUM. Worse if you set `decode.executor: process`. |
| **13** | **`close()` can return while a watchdog reopen is still driving the device** (consumer thread not joined; connect-verify not abort-aware) → shutdown re-acquires a stopping camera for ~1 s. | `reliability/watchdog.py:179-195,237-294` | MEDIUM. Bounded latency, no leak; same root cause as #1. |
| **9** | **Rolling buffer holds full-res frames: ~1.7 GB/camera, ~2.5–3.4 GB in A/B.** | `pipeline/rolling_buffer.py:18-26` | MEDIUM on a dev box; **likely fine on a provisioned factory PC** — but the bound is large and was never measured at real resolution/fps (the "flat memory" soak used small synthetic frames). Watch RSS during the §7 stability run. |
| **12** | **CSV formula injection**: a scanned payload starting with `= + - @` becomes a live formula when an operator opens the report in Excel/LibreOffice on the factory PC. | `reporting/render.py:109-119` | LOW-MED. Real on Windows since operators open the CSV there. One-line fix. |

### Mostly macOS-specific — LOWER priority on Windows (verify, don't assume)
| # | Finding | Why it's lower on Windows |
|---|---|---|
| 4 | Geometry-snap → infinite reopen loop (`camera.py:327-362`) | AVFoundation snapping is the worry; DSHOW/MSMF *usually* honor the locked mode, but still **watch `source.connect_mismatches`** on first connect (ARRIVAL_CHECKLIST §8). The no-escalation defect is platform-independent if it does snap. |
| 11 | Calibrate may lock MJPG over a raw mode because FOURCC readback is garbage (`probe.py:101-108`) | AVFoundation returns garbage FOURCC; **Windows DSHOW/MSMF report truthful FOURCC** (per ARRIVAL_CHECKLIST §8). Verify the readback is truthful per that step; if so, this doesn't bite you. |
| 6 | `flock` OSError masking (`instance_lock.py:116-120`) | **macOS-only** — Windows uses the `msvcrt` byte-range lock path, which is the path the whole design targets. Confirmed N/A on Windows. |
| 7 | ProcessPool spawn cold-load (`decode_engine.py`) | **Windows also uses `spawn`**, so if you switch `decode.executor: process` this applies; default `thread` avoids it. |

### Low tier (~30 items)
All in `REVIEW_macos_arrival.md` under "## Low". Genuine but low-impact:
JSONL not fsync'd, outbox cap counts chars not bytes, a few config fields lack
lower-bound validation (e.g. `decode.workers=0` crashes deep instead of a clean
exit-2), dashboard JS freezes the miss gallery while a note field is focused,
`import_pylibdmtx` at module import in `sources/render.py` can crash synth/replay
if the dylib is missing, etc.

---

## 5. Recommended pre-live hardening patches (NOT yet applied)

These protect the **first live connect** specifically. Decide whether to apply
before plugging in. The first is a config-only change (zero risk); the others are
small code changes to a deliberately-designed codebase, so review before applying.

### 5.1 Finding 5 — set `max_outage_s` (config only, do this)
In your camera config YAML:
```yaml
watchdog:
  max_outage_s: 120     # escalate to exit 3 (supervisor restart) after 2 min of no frames
```
This alone closes the worst "looks healthy, scans nothing" failure mode. No code
change. Optionally also change the default in `config.py:523` to `120.0`.

### 5.2 Finding 1 — time-bound `measure_achieved_fps` (small code change)
`sources/controls.py:261-292`. Add a hard wall-clock deadline that also bounds
the warmup loop, and a consecutive-failure cap so a glitching device returns
`frames=0` fast (a failed verify the watchdog can act on) instead of looping:
```python
def measure_achieved_fps(cap, *, sample_s, warmup_frames=5,
                         clock=time.monotonic, max_consecutive_failures=50):
    deadline = clock() + sample_s + 2.0          # hard cap incl. warmup
    for _ in range(warmup_frames):
        if clock() >= deadline:
            break
        cap.read()
    t0 = clock(); frames = failures = consecutive = 0
    while True:
        now = clock()
        if now - t0 >= sample_s or now >= deadline:
            break
        ok, _ = cap.read()
        if ok:
            frames += 1; consecutive = 0
        else:
            failures += 1; consecutive += 1
            if consecutive >= max_consecutive_failures:
                break
            time.sleep(0.005)
    elapsed = max(clock() - t0, 1e-9)
    return FpsMeasurement(fps=frames/elapsed, frames=frames,
                          read_failures=failures, elapsed_s=elapsed)
```
**Honest limitation:** this still cannot interrupt a *single* `cap.read()` that
blocks forever — only `cap.release()` from another thread can. The real fix for
that is to make connect-verify abort-aware (Finding 13) or skip connect-verify on
the watchdog reopen path. The deadline+fail-fast above covers the common
slow/glitching cases; document that a truly-wedged open still needs a process kill
(which is what `max_outage_s` + the supervisor are for).

### 5.3 Finding 3 — make motion debounce time-based (small code change, do before trusting A/B)
Add `open_s`/`quiet_s` to `MotionConfig` and convert to frame counts per source
inside `MotionGate` using `source.nominal_fps`
(`open_frames = max(1, round(open_s * fps))`). **Stopgap if you don't patch:** set
per-run motion overrides sized to the real fps, but note a single global
`motion` block can't be right for both 120 fps and 72 fps at once — which is
exactly why the time-based fix matters for A/B.

### 5.4 Finding 2 — cap QR decode cost (small code change, matters at speed)
In `pipeline/decode_engine.py:178-190`, downscale large ROIs for the pyzbar QR
fast path (cap the longer side to ~1000 px, map the rect back) and/or skip QR
when remaining budget is below a measured per-crop cost, and feed measured pyzbar
wall time into the deadline math so DM/fan-out see the real remaining budget.

> Suggested apply order if doing a pre-live pass: **5.1 (free) → 5.2 → 5.3 → 5.4**,
> running `pytest -m "not acceptance and not soak_short"` after each.

---

## 6. Camera bring-up on Windows — review-driven watch-items

Follow `ARRIVAL_CHECKLIST.md` step by step. Extra things to watch, mapped to it:

- **Step 1 (enumeration):** `palletscan calibrate --list` must show both See3CAMs
  by their exact device-name strings. Put those substrings in `cameras[].name`.
  Verify list-order == CAP_DSHOW-index-order (the checklist's open-each-index test).
- **Step 2 (probe):** capture both `dshow` and `msmf` probe tables. Prefer
  `dshow`/`auto` for name stability. If MSMF, you must pin `fallback_index`
  (name enumeration is DirectShow-only). Tip: `OPENCV_VIDEOIO_PRIORITY_MSMF=0`
  env var makes OpenCV prefer DSHOW if MSMF is noisy.
- **Step 2/8 (Finding 11/4):** confirm `CAP_PROP_FOURCC` readback is **truthful**
  on these devices (it is on Windows, unlike macOS) and that the device negotiates
  the locked geometry — **watch `source.connect_mismatches` in `/stats.json`**.
  Nonzero = the device gave you something other than the locked mode.
- **Step 3 (exposure):** the QUIRKS table (`sources/controls.py`) assumes MSMF
  0.75/0.25, DSHOW 1/0 auto-exposure magic values and DSHOW log2-stop exposure.
  Confirm readback *and* the `exposure effect` brightness line both move; correct
  `QUIRKS` if reality differs. Global-shutter blur budget depends on a pinned
  exposure, so this is the one that actually affects read rate.
- **Step 6 (watchdog):** unplug/replug mid-run; reconnect < 10 s; brightness
  unchanged after replug (UVC settings-reset-on-re-enumeration trap);
  `source.reconnects` increments. **Set `max_outage_s` first (§5.1)** so a
  no-frames-after-reopen loop actually escalates instead of spinning silently
  (Finding 4/5).
- **Step 7 (stability + CPU):** watch RSS during the 30-min run for Finding 9
  (full-res rolling buffer ~1.7 GB/camera). Run `python tools\measure_cpu.py` —
  that factory-box number is the authoritative spec §11 figure.
- **Step 8 (quirks):** MSMF hung-read-on-release ↔ Finding 14 (hung native open
  can hang the station). If the zombie counter climbs or the process needs a kill,
  that's the watchdog escalation (exit 3) doing its job — make sure the supervisor
  restarts it.

---

## 7. Phase 5 / 24-7 ops (Windows-specific, designed blind on Mac)

`ARRIVAL_CHECKLIST.md` §9 is the full list (install_service, CTRL_BREAK stop,
stop-latch, job-object tree-kill, exit-4 contention, log rotation). `RUNBOOK.md`
is the operator manual. None of this was exercisable on the Mac — it all needs a
Windows pass. The single-instance lock's Windows path (`msvcrt` byte-range lock
at offset 0x100000) is confirmed present and is the design's intended path.

---

## 8. Quick reference

```powershell
# env
py -3.13 -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -e ".[dev]"

# verify stack (no camera)
pytest -m "not acceptance and not soak_short" -q
palletscan synth --ab --dashboard      # http://127.0.0.1:8000

# cameras (after editing your config YAML with the See3CAM names)
palletscan calibrate --list
palletscan calibrate --camera cam-color --seconds 3
palletscan calibrate --camera cam-color --no-auto-exposure --exposure <v> --gain <v> --save --config <file>
palletscan selftest --config <file> --data-dir <service-data-dir>
palletscan run --config <file> --stats-interval 5 --dashboard
```

Exit codes: 0 clean · 1 software failure · 2 usage · 3 watchdog escalation
(USB wedged — restart) · 4 another instance holds the lock.

## 9. Open decision for the next session
Brody had not yet decided whether to **apply the §5 pre-live patches** or **plug
in and fix reactively**. Recommendation: apply **§5.1 (free config change)** at
minimum, and ideally **§5.2** before first connect. Ask Brody which scope he wants
(`pre-live patches only` vs `full High+Medium hardening pass` vs `bring up camera
first`) and proceed from there.
