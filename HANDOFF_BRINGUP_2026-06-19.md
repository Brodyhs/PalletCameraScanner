# PalletScan — Windows bring-up handoff (session 2)

> **Read this to resume.** Self-contained snapshot of the live-camera bring-up done on the
> Windows 11 factory PC the evening of **2026-06-19** (continuing into 2026-06-20). Written
> for the next Claude Code session and for Brody. Companion docs: `HANDOFF_WINDOWS.md`
> (original Mac→Windows handoff; has a "session 2" update block at the top now),
> `ARRIVAL_CHECKLIST.md` (the camera bring-up procedure), `RUNBOOK.md` (operator manual),
> `REVIEW_macos_arrival.md` (the 47-finding code review). Agent memory also holds
> `windows-bringup-progress` and `cam24cug-needs-msmf`.
>
> **Model:** Claude Opus 4.8 (1M).  **Box:** Windows 11 factory PC, Python 3.13.1, git 2.46.

---

## 0. TL;DR — where we are

- **Environment is up and green.** `.venv` (Py 3.13.1) with `pip install -e ".[dev]"`. Decode
  stack works (`synth` = 100% read, `selftest --skip-camera` OK).
- **The color camera (See3CAM_24CUG) is fully brought up, locked, and proven.** It runs on
  **MSMF + UYVY 1920×1200 @ 55 fps**, `calibrate` exits 0, `selftest --config config\station.yaml`
  is **green with the real camera**, and we **confirmed a live DataMatrix decode ("Hello world!")**
  end-to-end through the stack.
- **The mono camera (See3CAM_37CUGM) is NOT here yet** — it was at work; Brody is bringing it
  ~2026-06-20. **Start it on MSMF too** (almost certainly the same as the 24CUG).
- **Windows test baseline: 458 passed / 4 failed / 1 skipped.** The 4 fails are pre-existing
  first-on-Windows issues, none from this session's changes (see §6).
- Several original assumptions were **corrected on real hardware** — most importantly the camera
  needs **MSMF, not DSHOW/auto** (see §3).

---

## 1. Quick resume — commands

```powershell
# from the repo root, on the Windows box. venv already exists; if not:
#   py -3.13 -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -e ".[dev]"

.\.venv\Scripts\python.exe -m pytest -m "not acceptance and not soak_short" -q   # expect 458 passed / 4 failed
.\.venv\Scripts\python.exe -m palletscan calibrate --list                        # both See3CAMs (+ OBS virtual cam)
.\.venv\Scripts\python.exe -m palletscan selftest --config config\station.yaml --data-dir data\selftest   # GREEN
# live hand-test viewer (smooth, threaded decode, auto-exposure, focus score):
.\.venv\Scripts\python.exe tools\live_decode.py        # keys: q quit | a auto/manual | e/d exposure
```

Exit codes: 0 clean · 1 software failure · 2 usage · 3 watchdog escalation · 4 lock held.

---

## 2. Hardware facts

| id | device | VID/PID | sensor | LOCKED mode | notes |
|---|---|---|---|---|---|
| `cam-color` | **See3CAM_24CUG** | 2560 / C128 | 2.3MP color global shutter | **MSMF, UYVY 1920×1200@55** | MSMF index 0; here & working |
| `cam-mono` | **See3CAM_37CUGM** | — | mono global shutter | TBD (start MSMF) | **not here yet — ~2026-06-20** |

- Both are plain **UVC** — the datasheet says *"does not require any special camera drivers"*.
  The 24CUG runs on Microsoft's in-box `usbvideo` driver (confirmed). **No e-con driver needed.**
- An **OBS Virtual Camera** is registered (shows in DSHOW enumeration at index 1). **Keep OBS
  closed** during bring-up — it can shift MSMF/fallback indexes.
- Datasheet + firmware PDFs are in the repo folder: `e-con_See3CAM_24CUG_datasheet.pdf`,
  `e-con_See3CAM_24CUG_Lens_Datasheet.pdf`, `e-con_See3CAM_DFU_Firmware_Updater_Application_User_Manual.pdf`.
  (`pypdf` was pip-installed into the venv to read them.)

---

## 3. The critical camera findings (corrected on real hardware)

1. **The 24CUG is UNUSABLE under DSHOW/`auto` — use `backend: msmf`.** OpenCV's DirectShow
   backend can't negotiate this camera and pins it at **15 fps** (and won't switch FOURCC off
   UYVY). **MSMF works** (54–55 fps UYVY). `backend: auto` resolves to DSHOW on Windows, so it
   MUST be set to `msmf` explicitly. **The link/cable were never the problem.**

2. **MSMF requires a pinned `fallback_index`** (name enumeration is DSHOW-only; the code refuses
   MSMF without it). This **forfeits name stability** — after replug/reboot index 0 might point
   elsewhere. Mitigation: keep OBS closed. The 24CUG is MSMF index 0 → `fallback_index: 0`.

3. **Mode reality (datasheet Table 2) — there is NO UYVY 1920×1200@120.** At full 2.3MP res:
   - **UYVY → 55 fps max** (bandwidth: @120 would need 553 MB/s > USB 3.2 Gen 1).
   - **MJPEG → ~114 fps**, but our OpenCV stack decodes MJPEG inline on the read thread → caps at
     **~42 fps**. So **UYVY@55 beats MJPEG@42** through our stack and is simpler (raw, no decode CPU).
   - **Locked mode = MSMF + UYVY 1920×1200@55.** 55 fps actually *eases* review Findings 2/3
     (per-frame budget ~18 ms vs ~8 ms at 120).

4. **MSMF readback is unreliable** (contradicts HANDOFF §6 / ARRIVAL_CHECKLIST §8's "MSMF FOURCC
   is truthful"): **FOURCC reads back garbage ("????")**, and exposure/gain/auto-exposure
   readback don't match what we set — **even though the controls physically work** (the
   exposure-effect test verifies brightness changes). Fix applied (see §5):
   `QUIRKS[Backend.MSMF].controls_reliable = False` so calibrate/selftest **warn** instead of
   hard-failing, relying on the exposure-effect + achieved-fps checks. DSHOW readback IS truthful.

5. **exposure ↔ fps ↔ light are coupled.** A bright image in dim ambient light needs a long
   exposure (~30–40 ms), which physically caps fps at ~25–32. Hitting 55 fps needs a **short
   exposure (~1 ms)**, which is dark unless you **add scan-zone illumination**. **The deployment
   MUST light the scanning zone** — this is normal machine vision, not a defect. The handoff's
   "~1 ms exposure / 55 fps" target assumes lighting.

6. **The S-mount (M12) lens is manual-focus** — this camera has no software focus ("liquid lens
   only" per datasheet). Out-of-focus was what blocked the first decode attempts. The
   `tools/live_decode.py` viewer shows a focus score (variance of Laplacian) to set it by eye.

7. **LIVE DECODE CONFIRMED (2026-06-19):** a DataMatrix reading **"Hello world!"** decoded
   end-to-end off the real camera (MSMF + UYVY@55 → pylibdmtx) via `tools/live_decode.py`.

---

## 4. The locked config — `config/station.yaml`

This is the **deployment config** (RUNBOOK convention: pass `--config config\station.yaml`
everywhere). It is hand-maintained — **prefer hand-editing it; `calibrate --save` strips its YAML
comments** (`upsert_camera_yaml` re-dumps without comments). Key parts:

```yaml
watchdog:
  max_outage_s: 120        # HARDENING 5.1 (Finding 5): exit 3 after 120s no frames -> supervisor restarts
cameras:
  - id: cam-color
    name: "See3CAM_24CUG"
    backend: msmf            # REQUIRED (DSHOW = 15fps wall)
    fourcc: UYVY             # LOCKED; raw, no decode cost; 54.9fps measured
    width: 1920
    height: 1200
    fps: 55.0                # datasheet UYVY max @ this res; do NOT raise (no UYVY@120 exists)
    convert_rgb: true
    fallback_index: 0        # MSMF index of the 24CUG (OBS closed); name stability forfeited
    read_fail_limit: 5
    connect_verify_s: 1.0
    settings: {exposure_auto: false, exposure: -6, gain: 10, brightness: null}  # NEEDS real-pallet tuning
#  - id: cam-mono            # 37CUGM — uncomment when it arrives; START ON msmf; confirm its MSMF index
```

Note: `config/default.yaml` is intentionally left at code-default (`max_outage_s: null`) because
`tests/test_config.py::test_default_yaml_file_is_valid` pins it to mirror `AppConfig()` defaults.
The hardening lives in `station.yaml`, not `default.yaml`.

---

## 5. Code changes made this session (all committed to the working tree, NOT git — repo isn't a git repo)

| File | Change | Why |
|---|---|---|
| `palletscan/sources/controls.py` | **5.2:** hardened `measure_achieved_fps` (wall-clock deadline bounds warmup + `max_consecutive_failures=50` fast-bail). **MSMF fix:** `QUIRKS[Backend.MSMF].controls_reliable = False` + comments. | A wedged `cap.read()` could hang connect; MSMF readback is unreliable on real HW. |
| `palletscan/cli.py` | **UTF-8 fix:** `main()` reconfigures stdout/stderr to UTF-8 up front (guarded). | Windows cp1252 console crashed selftest/synth/calibrate report output with `UnicodeEncodeError` (invisible on macOS). |
| `palletscan/calibrate.py` | Generalized the "controls unverified" warning message + docstring (no longer AVFoundation-specific). | MSMF now also hits the warn path. |
| `config/station.yaml` | **NEW** — deployment config: 5.1 `max_outage_s: 120` + locked `cam-color` entry. | The operative config; default.yaml can't hold deployment values (test invariant). |
| `tests/test_calibrate.py` | `_lister(..., backend=MSMF)` param; `test_rejected_control_hard_fails_on_reliable_backend` repointed to **DSHOW**; AVFoundation message assertion → `"controls unverified"`. | DSHOW is now the truthful-readback exemplar; MSMF warns. |
| `tests/test_selftest.py` | `test_dead_exposure_control_hard_fails_on_reliable_backend` repointed to **DSHOW**. | Same reason. |
| `tools/live_decode.py` | **NEW** — throwaway smooth live viewer: threaded decode (off display thread), `BUFFERSIZE=1`, auto-exposure default, `a`/`e`/`d` keys, focus score overlay. NOT product code. | Hand-test tool for showing a code to the camera. |
| `HANDOFF_WINDOWS.md` | Added a "session 2" update block at the top. | So a fresh session reading it first gets the corrections. |

After every change the fast suite was re-run; final: **458 passed / 4 failed / 1 skipped**, no
regressions. The MSMF/UTF-8/5.x changes are all green.

---

## 6. The 4 Windows test failures (pre-existing; first-ever-on-Windows; NOT from this session)

| Test | Verdict |
|---|---|
| `test_devices::test_windows_enumeration_failure_returns_empty_loudly` | **Test-only artifact** — deletes `pygrabber` from `sys.modules` to fake an import failure, but on Windows it re-imports from disk. (Bonus: its failure output *proves* enumeration works.) |
| `test_supervisor::test_default_spawn_injects_pid_env_without_mutating_environ` | **Test-only artifact** — the `_FakeProc` lacks `_handle`, so the real Windows job-object path can't run against it. Validate the real path in ARRIVAL_CHECKLIST §9. |
| `test_instance_lock::test_hard_killed_holder_leaves_no_stale_lock` | **Verify in §9.** Lock records the inner pid (`os.getpid()`) but `Popen.pid` is the venv-launcher pid — they diverge. Likely the product is *correct* (records the real holder pid that `stop -Hard` should kill) and the test's assumption is wrong, but confirm the taskkill/`-Hard` paths kill the real holder. |
| `test_http_sink::test_size_cap_prunes_oldest_and_counts` | **Known Low finding** (outbox cap counts chars not bytes), platform-sensitive. HTTP sink is disabled by default. |

Two are pure test fakes; two are worth a real look (instance_lock in §9, http_sink whenever).

---

## 7. Open items / next steps (priority order)

1. **Bring up the 37CUGM (mono)** when it arrives (today, ~2026-06-20): plug into a USB-3 port,
   `calibrate --list` to confirm its name + **MSMF index**, uncomment its `station.yaml` block with
   `backend: msmf` + the right `fallback_index`, then `calibrate --camera cam-mono ...`. Watch the
   mono pixel layout (Y8/GREY) — under MSMF the FOURCC readback is garbage so the luma channel
   falls back to the configured `fourcc` (ARRIVAL_CHECKLIST §8). Expect the same DSHOW-vs-MSMF story.
2. **Real exposure/gain tuning** against actual pallets + the deployment lighting. Current
   `exposure: -6, gain: 10` give a usable image but readback is unverifiable on MSMF; tune for read
   rate under the real illumination. **Plan for scan-zone lighting** so a short exposure keeps the
   image bright at full fps (Finding in §3.5).
3. **ARRIVAL_CHECKLIST §6** (unplug/replug watchdog — reconnect <10 s, brightness unchanged,
   `source.reconnects` increments), **§7** (30-min stability + `tools\measure_cpu.py` for the spec
   §11 number), **§9** (Phase-5 Windows ops: install_service, CTRL_BREAK stop, stop-latch,
   job-object tree-kill, exit-4 contention, log rotation). §9 also covers the instance_lock and
   supervisor test questions above.
4. **A/B** (both cameras) once the mono is up — note the time-based motion-debounce concern
   (review Finding 3 / handoff §5.3): the two cameras run at different fps (55 vs ~72), so the
   frame-count debounce needs the time-based fix before trusting A/B attribution.
5. Optional cleanup: the 2 test-only fakes (devices, supervisor) could be fixed to go green on
   Windows; the http_sink byte/char cap is a real (low) fix.

---

## 8. Gotchas to remember

- **Always `backend: msmf` for these See3CAMs; always pass `--config config\station.yaml`.** Keep
  **OBS closed**.
- **`calibrate --save` strips YAML comments** — hand-edit `station.yaml` to keep its docs.
- **MSMF readback lies** (FOURCC "????", exposure/gain/auto values) but the controls work — trust
  the exposure-effect + achieved-fps checks, not the readback (that's why controls_reliable=False).
- **fps in `tools/live_decode.py` is not the camera's capability** — it's exposure-bound (and was
  decode-throttled before the threaded-decode rewrite). The authoritative 55 fps came from
  `calibrate`/`selftest` measuring capture directly. Camera hardware max = 55 fps; a 144 Hz monitor
  can't exceed that.
- **The repo is NOT a git repo** — changes are only in the working tree. Consider `git init` if you
  want history before more edits.
