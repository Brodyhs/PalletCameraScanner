You are building **PalletScan**, a production-grade, 24/7 fixed-camera scanning system that reads QR codes and Data Matrix codes on pallets as forklifts carry them past stationary cameras in a factory. Build the complete, runnable system. Work in phases (defined below), run and test your code as you go, and keep the repo in a working state at the end of every phase.

## 1. Mission and physical context

- Two USB cameras will be mounted at a palletizer line (initial trial) and later at warehouse dock doors. Pallets pass at up to 5 mph (design margin: 10 mph ≈ 4.5 m/s).
- Codes are large: QR or Data Matrix printed at roughly letter size (~6–8 inch symbol, ~5 mm modules), applied to at least two faces of each pallet.
- Read distance: ~3–15 ft. Indoor, fairly constant ambient lighting. No supplemental lighting initially.
- Volume: up to ~10,000 pallets/day across multiple stations (≈7/min average, with much higher bursts). Each pass keeps the code in frame ~0.5–0.7 s → dozens of candidate frames per pass.
- Operational goal: **every pallet pass is accounted for** — it either produces a decode event or a flagged "miss" exception with saved frame evidence for human review. Target ≥99.5% automated decode rate; the exception path covers the remainder. Never silently drop a pass.

## 2. Hardware (in transit — do NOT require it to develop)

Both cameras are **UVC plug-and-play** (standard OS camera stack, no vendor drivers):

1. **e-con See3CAM_24CUG** — color, onsemi AR0234, 1920×1200, up to 120 fps, global shutter, enclosed, M12 lens.
2. **e-con See3CAM_37CUGM** — monochrome, Sony IMX900, 3.2 MP (~2064×1552), up to 72 fps full-res, global shutter, HDR-capable, S-mount lens, board-level (no enclosure).

Camera notes you must design around:
- Treat both as generic UVC devices. Control only what standard UVC exposes (exposure, gain, brightness, etc.). Do NOT depend on vendor extension-unit features (Quad HDR, self-trigger ROI) or vendor apps at runtime.
- At startup and during calibration, **probe supported (pixel format, resolution, fps) combinations empirically** and measure *achieved* fps — never trust requested values. Prefer the highest real fps at full resolution; accept MJPEG if uncompressed can't sustain the rate, but prefer uncompressed (YUY2/UYVY/Y8) when bandwidth allows and log the choice.
- On Windows, OpenCV exposure control is quirky (backend-dependent `CAP_PROP_AUTO_EXPOSURE` semantics, log2-scaled exposure values under DirectShow vs MSMF). Implement a small camera-control layer that sets a value, reads it back, and verifies the effect on actual frames. UVC settings can reset on re-enumeration — **persist all camera settings in config and re-apply on every (re)connect.**
- The mono camera may deliver Y8/Y16 or YUV-wrapped luma; normalize to single-channel grayscale once at ingest. Convert the color camera BGR→gray once at ingest. The whole pipeline downstream is grayscale.
- Enumerate cameras **by stable identity (device name/path), not bare index** — indexes shuffle on reboot/replug. On Windows use a lightweight pip-installable approach (e.g., pygrabber for DirectShow names) and map names→config. Best-effort equivalent on macOS.

## 3. Hard constraints

- **Stack:** Python 3.11+, pip-installable packages only. Core: opencv-python (or opencv-contrib-python), pyzbar (QR/1D), pylibdmtx (Data Matrix), FastAPI + uvicorn (dashboard), SQLite (stdlib sqlite3), pytest. No vendor SDKs, no cloud services, no GPU/CUDA, no Docker requirement, no message brokers, no heavyweight databases. Everything must run on a normal Windows 10/11 desktop; keep it macOS-compatible for development.
- **InfoSec posture:** zero third-party drivers; the only installs are Python + pip packages. If you use OpenCV's WeChat QR detector as a fallback decoder, vendor its model files into the repo (document their origin) — no runtime downloads.
- **Offline-first:** the station must function with no network. Any outbound integration is store-and-forward.

## 4. Architecture (required shape; you own the details)

Pipeline per camera, communicating via bounded queues:

```
FrameSource → MotionGate → DecodeEngine → Dedup/PassTracker → EventBus → Sinks
                   ↘ RollingFrameBuffer (pre/post evidence) ↗
```

- **FrameSource (abstract):** three interchangeable implementations — `CameraSource` (live UVC via OpenCV), `VideoFileSource` (replay recorded clips at native or accelerated speed), `SyntheticSource` (generated pallet passes; see §7). All downstream code is source-agnostic. The system must run fully without hardware.
- **MotionGate:** cheap motion detection on downscaled grayscale (frame differencing or MOG2) so idle hours cost near-zero CPU. Emits "pass candidate" segments with ROIs. Tunable via config; expose its state on the dashboard.
- **DecodeEngine:** budget-aware cascade per frame/ROI:
  1. Fast path: pyzbar on the motion ROI.
  2. Data Matrix: pylibdmtx — it's slow, so run it on candidate regions, and make symbology priorities configurable (QR-only / DM-only / both).
  3. Fallbacks (only while a pass remains undecoded): preprocessing variants (CLAHE, adaptive threshold, unsharp, small rotations/perspective), optional WeChat QR detector.
  - Early-exit: first confirmed decode for a pass cancels remaining work for that pass. Enforce a per-frame decode time budget. Benchmark ThreadPool vs ProcessPool for decoders (pyzbar/libdmtx release the GIL in C calls — measure, choose, document).
- **Dedup/PassTracker:** one pallet → many decodes across frames (and both cameras in A/B mode). Collapse by payload within a configurable time window (default ~12 s) into a single **pass event** carrying: payload, symbology, first/last seen, decode count, per-camera attribution, best frame reference. In A/B mode, business events dedupe across cameras, but per-camera stats must NOT (each camera's independent performance is the experiment).
- **Miss handling (the 100% mechanism):** keep a rolling pre/post buffer (~2 s) per camera. A motion pass that ends with no decode persists a JPEG burst (or short MP4) + metadata to a capped evidence directory and emits a `miss` event. Auto-prune evidence and logs by size/age.
- **EventBus → Sinks (pluggable):** console, JSONL file, SQLite, CSV export, and an HTTP POST sink with an on-disk store-and-forward queue and retry/backoff (target internal REST endpoint TBD — make URL/headers config-driven, ship it pointing at a localhost echo stub).

## 5. Reliability requirements (24/7 duty)

- **Watchdog:** detect stalled sources (no frame for N s) → close/reopen; on failure, exponential backoff + re-enumeration by device name; re-apply persisted camera settings on every reconnect. Unplug/replug recovery must be automatic and logged.
- **Crash-only design:** any thread death is detected and restarted or escalates to clean process exit (supervisor restarts it). Single-instance lock. Graceful SIGTERM/CTRL+C shutdown that flushes queues.
- **Startup self-test:** enumerate cameras, verify achieved fps vs config, decode a bundled known-good test image through the full pipeline, check disk space — refuse to "run blind" and surface failures on console + dashboard + log.
- **Observability:** structured JSON logs with rotation; metrics (fps per camera, queue depths, decode latency p50/p95, passes/hour, read rate, miss count, uptime, reconnect count) exposed at `/stats.json` and on the dashboard.
- **Ops packaging:** RUNBOOK.md covering install (venv + requirements), Windows auto-start + restart-on-crash (Task Scheduler or NSSM — provide exact steps/config), log/evidence locations, and recovery procedures.

## 6. Dashboard (FastAPI, localhost-bound by default)

- Live view per camera (MJPEG endpoint) with decode/motion overlay boxes — this is the demo that sells the project.
- Tiles: read rate (rolling 1h/24h), passes/hour, per-camera fps and health, last 50 events table, miss gallery (evidence images, mark-reviewed).
- **A/B trial report page + export:** per-camera passes seen, passes decoded, read rate, time-to-first-decode, decodes/pass over the trial window; downloadable CSV + auto-generated markdown summary comparing 24CUG vs 37CUGM.
- **Manifest reconciliation:** accept an uploaded/config-pointed CSV of expected pallet IDs (known outbound pallets from the line) and report scanned vs expected vs unexpected — this computes *true* read rate for the trial.

## 7. Simulation-first development (cameras are in transit)

Build these before any live-camera code:
- **SyntheticSource:** renders QR/Data Matrix payloads (use `qrcode` + generate DM via pylibdmtx encode) onto frames and animates them across the FOV with configurable: speed (calibrated to px/frame equivalents of 2–10 mph at 3–15 ft), code pixel size, approach angle/perspective skew (0–35°), motion blur (directional kernel matched to speed/exposure), noise, contrast, lighting gradient, occlusion fraction, and "empty" idle periods. Deterministic via seed.
- **VideoFileSource:** replay any .mp4/.avi as if live; support faster-than-realtime for soak tests.
- **Test suite (pytest):** unit tests per stage + an end-to-end synthetic acceptance test: across a randomized matrix of speed/angle/blur/contrast within spec, pass-level read rate must be **≥99.5%**, and every undecoded pass must produce miss evidence (the account-for-everything invariant). Include a soak script (run synthetic/replay load for hours; assert stable memory, zero unhandled exceptions, recovery from injected source failures).

## 8. Configuration & calibration

- Single YAML config: cameras (by name, format/res/fps, exposure/gain), zones, motion thresholds, decode budgets/symbology priorities, dedup window, sinks, evidence caps, dashboard bind.
- `palletscan calibrate` CLI: list devices by name; live preview with focus metric (variance of Laplacian), measured fps, exposure readback, and live decode test; lock-and-save settings to config. `palletscan selftest`, `palletscan run`, `palletscan replay <file>`, `palletscan synth` subcommands.

## 9. Code quality

Typed Python (mypy-clean or close), pydantic (or dataclass) config models, small modules with single responsibilities, no global mutable state, docstrings on public APIs, README (quickstart in <10 commands), ASSUMPTIONS.md for anything you had to decide. Package layout: `palletscan/` (sources/, pipeline/, events/, reliability/, web/, cli.py), `tests/`, `tools/`.

## 10. Build phases (keep tests green at each gate)

1. **Core pipeline, no hardware:** scaffold, config, FrameSource abstraction, SyntheticSource, MotionGate, DecodeEngine (pyzbar+pylibdmtx), PassTracker/dedup, JSONL+SQLite sinks, miss evidence, pytest suite incl. the ≥99.5% synthetic acceptance test.
2. **Replay + metrics:** VideoFileSource, metrics module, store-and-forward HTTP sink + echo stub, soak script.
3. **Live cameras:** CameraSource with format/fps probing, settings persistence/re-apply, device-by-name, watchdog/reconnect, calibrate + selftest CLIs. (Code complete and unit-tested against mocks even though hardware is absent; document exactly what to verify when cameras arrive.)
4. **Dashboard + A/B:** live MJPEG w/ overlays, stats, miss gallery, A/B report, manifest reconciliation.
5. **Hardening + ops:** supervisor/service install docs, log/evidence rotation, RUNBOOK.md, final end-to-end demo script (`make demo` or equivalent) that runs the full system on synthetic input and opens the dashboard.

## 11. Acceptance criteria (verify and show results)

- `pytest` fully green; synthetic acceptance: ≥99.5% pass read rate within spec envelope; 100% of passes produce decode OR miss evidence.
- Replay of a synthetic-generated "recorded" clip decodes all expected payloads and the manifest reconciliation report matches.
- Injected source failure (kill/stall a source mid-run) → automatic recovery <10 s, logged, no crash, no event loss.
- 2h+ accelerated soak: flat memory profile, zero unhandled exceptions.
- Dashboard functional throughout; sustained CPU on a typical 4-core desktop ≤~50% under burst load (document measurements).

## 12. Non-goals / do NOT

No vendor SDKs or e-CAMView dependence at runtime; no cloud, GPU, Docker, brokers, or external DBs; no auth system (localhost bind + note for future); no HDR/self-trigger extension-unit control; no speculative features beyond this spec. Prefer boring, debuggable code over cleverness.

Ask me questions only if truly blocking; otherwise choose sensible defaults and record them in ASSUMPTIONS.md. Begin with a brief written plan, then start Phase 1.