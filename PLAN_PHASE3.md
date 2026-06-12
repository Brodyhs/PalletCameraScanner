# Phase 3 Plan — Live Cameras (code-complete against fakes)

Status: **approved 2026-06-11**. Execute in order (§4), starting with config.
Phases 1–2 are complete and green (117 tests incl. the 400-pass acceptance
gate and short soak); recorded decisions live in ASSUMPTIONS.md. No hardware
is present: everything is built and unit-tested against injected fakes;
ARRIVAL_CHECKLIST.md captures what to verify when the two cameras land
(See3CAM_24CUG color 1920×1200@120, See3CAM_37CUGM mono 2064×1552@72, both
UVC global shutter).

## 0. Approved decisions (owner rulings)

1. **Minimal `run` subcommand: approved.** Spec §8 lists it and CameraSource
   is otherwise unreachable from the CLI (synth/replay hardcode their
   sources); the arrival checklist needs it for the physical replug test.
   Minimal = load config, optional `--camera ID` override, `--data-dir`,
   `--stats-interval`, `PipelineRunner.from_config`, sigint handler, exit 0
   clean / 1 on pipeline failure. No dashboard, no single-instance lock
   (Phase 4/5).
2. **Metrics snapshot API extension: approved.** `snapshot()` gains a
   top-level `"source": {stalls, reconnects, reopen_failures,
   zombie_readers}` section (spec §5 requires reconnect count). This amends
   `SNAPSHOT_KEYS` in `tests/test_metrics.py` — the **only existing-test
   edit in Phase 3** — which that test's own comment classifies as an API
   change, not a refactor. Phase 4's `/stats.json` inherits the shape.
3. **pygrabber as a win32-marked main dependency: approved.**
   `pygrabber>=0.2 ; sys_platform == 'win32'` — runtime-required on the
   factory PC for DirectShow name enumeration (spec §2), never installed on
   macOS dev machines. Pure Python + comtypes, no native code.
4. **Watchdog give-up policy — never give up, with zombie cap: approved.**
   Capped jittered backoff forever (0.5 s → 15 s) with loud logs + counters;
   process exit can't fix an unplugged camera. Two escalation valves:
   `watchdog.max_outage_s` (default null = off) and a hard
   `max_zombie_readers: 3` — three abandoned reader threads stuck in hung
   `read()` calls is the wedged-driver signature, and a process restart is
   the only thing that resets a wedged USB stack. Escalation raises through
   the existing crash-only chain (source thread → `_thread_errors` →
   `run()` raises → supervisor restart).
5. **Distinct exit code for watchdog escalation: owner addition at
   approval.** `WatchdogEscalation` exits the process with **exit code 3**,
   not the generic 1 — so ops can distinguish "USB stack wedged, check
   cable/hub" from "software crashed, check logs" at the supervisor level
   without log diving. Documented in README's exit-code table; Phase 5's
   RUNBOOK will note the supervisor must restart on any nonzero exit.

Everything else below is decided and recorded in ASSUMPTIONS.md (#29–#37):
watchdog-as-wrapper, `cameras:` list config shape, live-ts anchoring, raw
exposure units, fail-fast first connect, YAML upsert semantics, the
`frames()` contract amendment, candidate matrices, selftest asset approach.

## 1. Where each piece plugs into Phases 1–2

| Phase 3 piece | Existing seam it reuses |
|---|---|
| CameraSource | `FrameSource` ABC (`source_id`, `nominal_fps`, `live`, `frames()`, `close()`); `live=True` ⇒ runner's existing drop-oldest `DroppingQueue.put`; `to_gray` in `sources/video.py` (extended) |
| Watchdog | `palletscan/reliability/` home beside `flaky.py`; `FlakySource.stall_at` as the promised ready-made fault (its docstring names Phase 3); `RetryConfig` shape + jittered-backoff pattern from the HTTP sink; crash-only escalation through `_source_loop` → `_thread_errors` |
| Wiring | `create_source()` factory branch on `source.type`; `PipelineRunner.from_config(cfg, source=...)` unchanged |
| Metrics | `MetricsRegistry.register_gauges` lazy-gauge pattern; `_KNOWN_GAUGES` whitelist extended (metrics.py:43) |
| Control/probe reports | JSON-lines structured logging (`extra=` dict, like the stats line) |
| calibrate decode test | `PyzbarDecoder.decode(gray)` / `PylibdmtxDecoder.decode(gray, timeout_ms)` from `pipeline/decoders.py`, used directly |
| selftest "full pipeline" | `PipelineRunner` + a tiny in-module asset-sweep source; `apply_overrides(data_dir=...)` sandboxes outputs |
| Config | `_StrictModel` conventions, `load_config`; CLI subparser pattern (`_add_X_parser`/`_cmd_X`, lazy imports, `_install_sigint`) |
| Tests | Constructor-injected fakes (`FakeCapture` via a `Capture` protocol) honor the no-wholesale-cv2-mocking rule; injected clocks like `MetricsRegistry(clock=)` |

## 2. Design decisions

### Device enumeration (`palletscan/sources/devices.py`)

- `DeviceInfo(name, index, backend)` frozen dataclass — `backend` is the
  cv2 `CAP_*` flag under which that index is valid (never mix a
  DSHOW-derived index with MSMF).
- `list_devices() -> list[DeviceInfo]` platform dispatch. **Windows**:
  `pygrabber.dshow_graph.FilterGraph().get_input_devices()` names in
  DirectShow filter order paired with `cv2.CAP_DSHOW` (list-order ==
  CAP_DSHOW-index-order assumption is recorded and is an arrival-checklist
  item). **macOS best-effort**: `system_profiler SPCameraDataType -json`
  (subprocess, 2 s timeout) paired with `cv2.CAP_AVFOUNDATION`;
  profiler-order-vs-index-order not guaranteed (documented); on failure
  return unnamed indices and fall back to `cameras[].fallback_index` with a
  loud warning.
- `find_device(devices, name)`: case-insensitive substring, must match
  **exactly one**; ambiguity/no-match raises listing what was found.
  ("See3CAM_24CUG"/"See3CAM_37CUGM" are mutually non-substrings.)
- Injectable everywhere as `device_lister: Callable[[], list[DeviceInfo]]`;
  the pygrabber import lives inside the Windows branch.

### Camera control layer (`palletscan/sources/controls.py`)

- `Backend` StrEnum (auto/dshow/msmf/avfoundation) + `BackendQuirks`
  frozen dataclass (`auto_exposure_on/off` values, `exposure_is_log2`,
  `controls_reliable`) + a module-constant `QUIRKS` table — backend quirk
  knowledge is *data* in one place, corrected on arrival day. Known values:
  MSMF 0.25 manual / 0.75 auto, DSHOW log2-scaled exposure, AVFoundation
  mostly ignores control props (`controls_reliable=False`).
- `ControlReport(prop, requested, accepted, readback, verified, note)` —
  per-property honest report; lists emitted as one structured log line.
- `apply_mode(cap, cam_cfg)`: FOURCC → width/height → fps → CONVERT_RGB →
  BUFFERSIZE=1, **in that order** (a UVC mode change can reset controls);
  then `apply_settings(cap, settings, quirks)`: auto-exposure off →
  exposure → gain → brightness → read back everything.
- `measure_achieved_fps(cap, *, sample_s, warmup_frames=5, clock=...)` —
  empirical, never trusts requested values.
- `verify_exposure_effect(...)`: sample mean frame brightness at configured
  exposure vs a stepped value; expect a delta beyond a margin. Runs in
  **calibrate/selftest only** (it perturbs the camera).
- **Fail semantics**: at run/(re)connect → warn-and-continue on readback
  mismatch (frames at slightly-wrong exposure beat no frames). At
  calibrate/selftest → hard failure on `controls_reliable` backends, honest
  warning on AVFoundation.

### CameraSource (`palletscan/sources/camera.py`)

- `Capture` Protocol (isOpened/read/set/get/release) — structural, real
  `cv2.VideoCapture` satisfies it. Constructor injection:
  `capture_factory: Callable[[int, int], Capture]` and `device_lister`,
  defaulting to real cv2 / real enumeration — the only place a real capture
  is born. No globals, no cv2 monkeypatching in tests.
- `CameraSource(cfg, *, capture_factory=..., device_lister=...,
  clock=time.monotonic)`; `source_id = cfg.id`; `nominal_fps = cfg.fps`;
  `live = True`. **Fail-fast at construction** (consistent with
  VideoFileSource and "refuse to run blind"); the watchdog owns all
  post-start failures.
- `_connect()` (shared by `__init__` and `reopen()`): enumerate →
  `find_device(cfg.name)` → `capture_factory(index, backend_flag)` →
  `isOpened()` → `apply_mode` → `apply_settings` → log reports → optional
  achieved-fps sample (`connect_verify_s`, default 1.0 s; warn-only below
  0.8× configured). This is the light startup/reconnect health check; full
  matrix probing never runs in `run`.
- **`reopen()`**: the watchdog's recovery hook — tear down, re-enumerate by
  name, re-apply persisted settings (spec §5), every attempt.
- **ts semantics**: `ts = clock() - t0`, sampled right after `read()`
  returns; `t0` anchored **once at construction, never re-anchored on
  reopen** — ts stays monotonic across reconnects and an outage appears as
  a real gap in source time (what dedup windows and miss deadlines should
  see). `CAP_PROP_POS_MSEC` is not used (unreliable for live devices).
  `frame_index` increments monotonically across reopens (same convention as
  video looping).
- Read loop: `ok=False` → 5 ms sleep, retry; `read_fail_limit` (default 5)
  consecutive failures → raise `CameraReadError` so the watchdog recovers
  immediately instead of waiting out the stall timeout.
- **Grayscale**: extend `to_gray` (additive): 2-D uint8 passthrough; 2-D
  uint16 `>>8` (Y16); HxWx1 squeeze; HxWx2 → channel 0 (YUY2/UYVY-wrapped
  luma when CONVERT_RGB=0); HxWx3 BGR → cvtColor. Default
  `convert_rgb: true` so both cameras arrive BGR and the existing path just
  works; raw paths exist so probing may pick CONVERT_RGB=0 for the mono cam.
- `close()` idempotent, callable from another thread (`cap.release()` is
  the watchdog's read-unblocker).
- `build_camera_source(cfg, *, capture_factory=..., device_lister=...)
  -> FrameSource` resolves the `source.camera` selector, constructs
  CameraSource, wraps in WatchdogSource. `create_source` calls it with
  defaults; tests call it with fakes.

### Watchdog (`palletscan/reliability/watchdog.py`)

- **Generic wrapper, not internal to CameraSource**: `WatchdogSource(inner,
  cfg, *, clock=..., rng=...)` with a `Reopenable` Protocol
  (`reopen() -> None`) that only CameraSource implements; constructor
  asserts the inner is Reopenable (fail at wiring, not mid-outage).
  Rationale: single responsibility; detection is testable against
  FlakySource-stalled synthetic sources exactly as flaky.py's docstring
  promises; recovery is testable against CameraSource+FakeCapture; and the
  runner's crash-only `_source_loop` needs a `frames()` that keeps yielding
  across reopens — the wrapper is where that absorption lives.
- **Mechanics**: a daemon reader thread runs `inner.frames()` into a small
  handoff `queue.Queue(maxsize=4)` tagged with a generation token;
  `frames()` (on the runner's source thread) loops
  `handoff.get(timeout=stall_timeout_s)`. Timeout = stall detected; an
  exception/end marker from the reader = device failure detected
  immediately (fast path, no timeout wait).
- **Recovery sequence** (inside `frames()`, runner never sees it):
  `inner.close()` first (release usually unblocks a hung read) → join old
  reader 5 s → still alive ⇒ `zombie_readers += 1` (it can never poison the
  stream: stale generation tokens are discarded) → jittered backoff wait
  via interruptible `stop_event.wait(delay)` → `inner.reopen()`
  (re-enumeration + settings re-apply every attempt). Success: bump
  generation, spawn fresh reader on a fresh `inner.frames()` iterator,
  `reconnects += 1`. The backoff attempt counter resets only when a frame
  is actually yielded.
- **Escalation** (rulings #4/#5): `zombie_readers > max_zombie_readers` or
  outage > `max_outage_s` (if set) → raise `WatchdogEscalation` → existing
  crash-only chain → CLI detects `isinstance(exc.__cause__,
  WatchdogEscalation)` (the same `__cause__` pattern soak uses for
  `InjectedFailure`) → **exit code 3** → supervisor restarts the process.
- Backoff reuses `RetryConfig` (jittered, success resets — same shape as
  the HTTP sink), defaults base 0.5 s / cap 15 s. Recovery budget vs the
  <10 s spec gate: ~2.0 s detect + 0.5–1 s backoff + ~1 s reopen + 1 s
  connect-verify ≈ 4–5 s.
- Counters (`stalls_detected`, `reconnects`, `reopen_failures`,
  `zombie_readers`) are plain ints, single-writer (the consumer thread).
- Only camera sources get wrapped; synthetic/video paths bit-identical.

### Probing (`palletscan/sources/probe.py`)

- `ModeCandidate(fourcc, width, height, fps)`; `ProbeResult(candidate,
  opened, actual_fourcc/width/height, achieved_fps, frames_sampled, error)`.
- `candidates_for(device_name)` (pure): See3CAM_24CUG → 1920×1200 @
  {120, 60, 30} × {UYVY, YUY2, MJPG}; See3CAM_37CUGM → 2064×1552 @
  {72, 60, 30} × {GREY(Y8), UYVY, MJPG}; generic fallback →
  device-reported current mode plus 1920×1080/1280×720/640×480 @ {60, 30}
  × {YUY2, MJPG}. Candidates to *try*, not assumptions — unknown combos
  fail readback or measure low.
- `probe_modes(make_cap, candidates, *, sample_s=1.0, warmup_frames=5,
  clock=...)`: fresh capture per candidate (UVC mode-switch on a live
  handle is flaky); set → read back actual → sample achieved fps.
- `choose_mode(results, *, min_fps_fraction=0.9)` (pure, cv2-free): filter
  to ≥ fraction of requested fps; rank by resolution area desc then
  achieved fps desc; among near-equals (within 5% fps) **prefer
  uncompressed (Y8/UYVY/YUY2) over MJPG**; the full table + choice is
  logged (spec §2).
- **Where it runs**: full matrix in calibrate; configured-mode verification
  in selftest; never in `run` (which does the 1 s connect-verify).

### Config (`palletscan/config.py`)

```yaml
source:
  type: synthetic        # synthetic | video | camera
  camera: null           # cameras[].id to run; optional when exactly one entry
cameras: []              # default empty; commented See3CAM examples in default.yaml
#  - id: cam-color
#    name: "See3CAM_24CUG"      # device-name substring, case-insensitive
#    backend: auto              # auto | dshow | msmf | avfoundation
#    fourcc: UYVY               # null = leave device default
#    width: 1920
#    height: 1200
#    fps: 120.0
#    convert_rgb: true
#    fallback_index: null       # only when the platform gives no names
#    read_fail_limit: 5
#    connect_verify_s: 1.0      # achieved-fps sample per (re)connect; 0 disables
#    settings: {exposure_auto: false, exposure: -6, gain: 10, brightness: null}
watchdog:
  stall_timeout_s: 2.0
  retry: {base_s: 0.5, cap_s: 15.0}    # reuses RetryConfig
  max_outage_s: null                   # null = never give up (ruling #4)
  max_zombie_readers: 3
```

- Models: `CameraSettings`, `CameraConfig` (required `id` + `name`;
  validators: 4-char fourcc, fps finite > 0, width/height > 0),
  `WatchdogConfig`; `AppConfig` gains `cameras` + `watchdog`;
  model-validator rejects duplicate `cameras[].id`. All additive, all
  defaulted — existing behavior bit-identical.
- **Why a `cameras:` list, not a single block**: the trial has two cameras
  with different native modes; calibrate must persist per-device settings
  without clobbering the other; Phase 4 A/B runs both. Spec §8 says
  "cameras (by name, …)" — plural. `resolve_camera(cfg)` implements the
  selector (single entry → default; multiple → `source.camera` required;
  unknown id → error listing ids).
- **Exposure/gain stored as raw backend values** (the number handed to
  `CAP_PROP_EXPOSURE`) plus `exposure_auto: bool` — no ms abstraction.
  DSHOW-log2 vs MSMF semantics make unit conversion a guess we cannot
  verify without hardware; calibrate records what *worked* and reconnect
  replays exactly that. `backend` is stored alongside so values and backend
  travel together.
- **Calibrate save** = `upsert_camera_yaml(path, camera)` — narrow,
  targeted: raw `yaml.safe_load` of the existing file (or `{}`), replace or
  append only the matching `cameras[]` entry, validate the merged result
  via `AppConfig.model_validate` **before** writing (a corrupt save can
  never brick the station), `safe_dump(sort_keys=False)` with an
  `# updated by palletscan calibrate <iso-ts>` header, tmp-file +
  `os.replace`, timestamped `.bak` of the original. Tradeoff: comments in
  the user's config file are lost on save (key order survives);
  `config/default.yaml` remains the commented reference. ruamel.yaml would
  preserve comments but adds a dependency for cosmetics — not justified
  under spec §12.

### calibrate (`palletscan/calibrate.py` + CLI)

- Orchestration in `calibrate.py` (`run_calibration(cfg, opts, *,
  capture_factory=..., device_lister=..., out=sys.stdout) -> int`);
  `cli.py::_cmd_calibrate` is a thin shell. **Non-interactive, flag-driven
  by default** (testable, SSH-able, headless factory PC).
- Flow: `--list` → device table, exit. Otherwise: resolve device → probe
  matrix (or `--fourcc/--width/--height/--fps` to pin a mode) → print
  table + chosen mode → apply settings (`--exposure/--gain/
  --auto-exposure` overrides) → ControlReport table incl.
  `verify_exposure_effect` → metrics loop for `--seconds N` (default 5):
  one line/second with measured fps, focus metric
  (`cv2.Laplacian(gray, CV_64F).var()`), mean brightness, and live decode
  results (sampled frames through PyzbarDecoder/PylibdmtxDecoder) →
  `--save` upserts the locked entry into the config (lock-and-save,
  spec §8).
- `--preview` adds a cv2.imshow window with overlay text and `q`/`s` keys;
  main-thread only (macOS constraint); the one path pytest doesn't cover.

### selftest (`palletscan/selftest.py` + CLI)

- `run_selftest(cfg, *, capture_factory=..., device_lister=...,
  disk_usage=shutil.disk_usage, data_dir=None) -> SelftestReport`
  (`CheckResult(name, ok, detail, hard)` list; `format()` for console).
- Checks in order:
  1. **Cameras** (runs iff `cfg.cameras` non-empty; `--skip-camera`
     bypasses; skipped-with-notice when empty so pre-hardware selftest
     passes): enumerate; each configured camera resolves by name; opens;
     mode + settings apply; achieved fps over ~2 s ≥ **0.85×** configured
     (hard fail — spec §5 "verify achieved fps vs config"); controls
     readback hard on reliable backends, warn on AVFoundation.
  2. **Decode through the full pipeline**: bundled assets
     `palletscan/assets/selftest_qr.png` + `selftest_dm.png` (payloads
     `PALLETSCAN-SELFTEST-QR/-DM`), generated once by
     `tools/make_selftest_assets.py` (qrcode + pylibdmtx encode) and
     **committed** — vendored, no runtime generation/downloads (spec §3).
     A ~30-line in-module `_AssetSweepSource(FrameSource)` translates each
     symbol across a gray background (~40 frames @ 30 fps, idle head/tail)
     → `PipelineRunner.from_config` with outputs rebased to a temp dir →
     assert exactly the two expected pass events and zero misses. This
     exercises MotionGate + DecodeEngine + PassTracker + bus + evidence
     wiring, not just the decoders.
  3. **Disk space**: `shutil.disk_usage` (injectable) on evidence and
     outbox dirs: hard fail if free < 2× (`evidence.max_total_mb` +
     `http.max_mb`), warn under 4×.
- Exit codes: 0 all hard checks pass; 1 any hard failure (refuse to run
  blind); 2 usage error. `pyproject.toml` gains
  `[tool.setuptools.package-data] palletscan = ["assets/*.png"]`.

### run (CLI only, ruling #1)

- `_add_run_parser` / `_cmd_run` following the synth/replay pattern: load
  config, `apply_overrides(data_dir=...)`, optional `--camera` override of
  `source.camera`, require the resulting source type to be valid (honors
  whatever `source.type` says — `run` on a synthetic config is allowed and
  useful for demos), `_install_sigint`, `--stats-interval`. Exit codes: 0 clean,
  1 pipeline failure, **3 watchdog escalation** (ruling #5), 2 usage error
  (argparse default).

### Metrics wiring (ruling #2)

- Extend `_KNOWN_GAUGES` with `source_stalls`, `source_reconnects`,
  `source_reopen_failures`, `source_zombie_readers`; `snapshot()` gains the
  `"source"` section. Wiring in `PipelineRunner.__init__`:
  `if isinstance(source, WatchdogSource): metrics.register_gauges(
  source_stalls=lambda: source.stalls_detected, ...)` — watchdog counters
  stay the single source of truth (existing lazy-gauge pattern); non-camera
  runs report zeros. Single-writer invariant holds (counters written only
  on the runner's source thread).

## 3. Approved refactors (everything else is purely additive)

1. **`FrameSource.frames()` docstring amendment** (`sources/base.py`):
   "single-use **per connection**" — after `Reopenable.reopen()` the
   reliability watchdog may call `frames()` again for a fresh stream; all
   other callers still call it once. No code change to the ABC.
   `WatchdogSource.frames()` itself remains strictly single-use toward the
   runner.
2. **`to_gray` extension** in `sources/video.py` for uint16 and 2-channel
   layouts — additive; existing tests untouched.
3. **`SNAPSHOT_KEYS` amendment** in `tests/test_metrics.py` (ruling #2) —
   the only existing-test edit in Phase 3.

## 4. Build order

1. **Config** — models, selector, `upsert_camera_yaml`; tests: parse/
   validate/duplicate-id, selector rules, upsert round-trip (other entries
   + sections preserved, atomic, `.bak`, pre-write validation). (~10 tests)
2. **FakeCapture + devices** — `tests/camera_fakes.py`: scriptable
   `FakeCapture` (property store with per-prop accept/quantize hooks;
   scripted read sequences: frame/False/stall/raise;
   brightness-tracks-exposure frame generator; fake-clock fps pacing;
   backend profiles: dshow log2-quantized exposure, msmf 0.25/0.75 auto
   semantics, avfoundation ignores sets) + recording `FakeCaptureFactory`
   (fail-then-succeed scripts, index shuffles). `devices.py` + tests via
   injected listers / canned pygrabber + system_profiler JSON. (~8)
3. **Controls** — quirks table, apply order, readback reports,
   verify-effect, fail-vs-warn mapping. (~10)
4. **Probe** — set/readback against FakeCapture, achieved-fps with injected
   clock, `choose_mode` pure-logic matrix (uncompressed preference, MJPG
   fallback, full-res preference), `candidates_for`. (~10)
5. **CameraSource** (+ `to_gray` extension, `build_camera_source`, factory
   branch) — connect/reopen apply mode+settings; ts anchoring + monotonic
   across reopen; frame_index continuity; gray paths ×5; read-fail limit;
   fail-fast missing device; close idempotent/unblocking;
   connect-verify warn. (~13)
6. **Watchdog + metrics + runner integration** — detection via FlakySource
   stall behind a 5-line reopenable shim (fulfilling the flaky.py docstring
   promise); reader-exception fast path; backoff sequence with injected
   clock (jitter bounds, cap, reset-on-frame); re-enumeration per attempt
   honoring an index shuffle; **settings re-applied on reconnect**
   (FakeCapture records per-instance set calls); stale-generation frames
   discarded; zombie-cap and max_outage_s escalation (incl. CLI mapping
   `WatchdogEscalation` → exit code 3); close-during-backoff
   exits promptly; counters. Runner integration: mid-run FakeCapture death
   → recovery, events on both sides of the outage, no unaccounted segments,
   `"source"` metrics in snapshot; one real-clock test
   (`stall_timeout_s=0.2, base_s=0.05`) asserting recovery wall time well
   under the 10 s gate. (~16)
7. **CLI: run, calibrate, selftest + assets** —
   `tools/make_selftest_assets.py` + committed PNGs (+2 asset-decode guard
   tests); calibrate non-interactive paths (probe table, focus-metric
   ordering sharp>blurred, decode line, `--save`, `--list`, exit codes)
   (~7); selftest (all-green, fps-below-tolerance, missing device,
   disk-fail via injected `disk_usage`, pipeline-decode pass/miss, skip and
   empty-cameras notice, exit codes) (~8); run (honors configured source,
   `--camera` override, exit codes; camera path exercised through the
   `build_camera_source` seam with fakes) (~4).
8. **Gate + docs** — full suite green (117 existing untouched except the
   ruling-#2 edit), mypy, `config/default.yaml` additions, README,
   ASSUMPTIONS #29–#37, ARRIVAL_CHECKLIST.md.

Estimated new tests: **~75** (total ≈ 190). No new pytest marker — injected
clocks keep everything fast; the single real-clock recovery test runs <2 s.

## 5. Acceptance criteria → verification

| Criterion | Verified by |
|---|---|
| Stalled source → reopen, recovery <10 s, logged, no crash, no event loss (spec §5/§11) | Watchdog runner-integration tests (step 6): real-clock recovery timing; event accounting across the outage; structured-log assertions |
| Exponential backoff + re-enumeration by name; settings re-applied on every reconnect (spec §5/§2) | Backoff-sequence tests with injected clock; FakeCaptureFactory index-shuffle test; per-instance recorded `set()` calls on reconnect |
| Probe (format, res, fps) empirically, measure achieved fps, prefer uncompressed, log the choice (spec §2) | `choose_mode` unit matrix + `probe_modes` readback tests + calibrate output test |
| Set/readback/verify control layer incl. Windows backend quirks (spec §2) | Controls tests against dshow/msmf/avfoundation FakeCapture profiles |
| Enumerate by stable name, not index (spec §2) | devices tests with injected/canned platform data; ambiguity + fallback-index paths |
| selftest: enumerate, achieved-fps vs config, bundled image through the full pipeline, disk check, refuse to run blind (spec §5) | Selftest tests incl. exit-code-1 paths; asset guard tests |
| calibrate: list devices, focus metric, measured fps, exposure readback, live decode test, lock-and-save (spec §8) | Calibrate non-interactive tests + upsert round-trip |
| Existing 117 tests green; default-config behavior bit-identical | cameras=[] default; synthetic/video paths never wrapped; only the ruling-#2 test edit |
| Hardware-dependent claims honestly deferred | ARRIVAL_CHECKLIST.md (below) |

## 6. New config keys, dependencies, modules, docs

**Config** (all additive, all defaulted): `source.type: camera`,
`source.camera`, `cameras: []`, `watchdog: {stall_timeout_s, retry,
max_outage_s, max_zombie_readers}`.

**Dependencies**: `pygrabber>=0.2 ; sys_platform == 'win32'` (runtime,
ruling #3). Nothing else.

**New modules**: `palletscan/sources/devices.py`, `controls.py`,
`camera.py`, `probe.py`; `palletscan/reliability/watchdog.py`;
`palletscan/calibrate.py`, `palletscan/selftest.py`;
`palletscan/assets/selftest_qr.png` + `selftest_dm.png`;
`tools/make_selftest_assets.py`; `tests/camera_fakes.py` + test files per
§4.

**ASSUMPTIONS.md #29–#37**: live-ts anchoring (no re-anchor on reopen); raw
exposure units + backend stored together; fail-fast first connect;
never-give-up watchdog + zombie escalation; macOS enumeration best-effort
(order caveat, fallback_index); DSHOW list-order==index-order assumption;
comment loss on calibrate save; snapshot `"source"` API extension; selftest
asset provenance (generated by tools/make_selftest_assets.py, committed).

**README**: run/calibrate/selftest quickstart lines, Windows pygrabber
note, ARRIVAL_CHECKLIST pointer, and an **exit-code table** (0 clean /
1 software failure / 2 usage / 3 watchdog escalation — "USB stack wedged,
check cable/hub" vs "software crashed, check logs"); Phase 5's RUNBOOK
will note the supervisor restarts on any nonzero exit.

**ARRIVAL_CHECKLIST.md** — per camera (24CUG, 37CUGM), in order:
1. `palletscan calibrate --list` → exact device-name strings appear
   (record them; adjust `cameras[].name` if they differ from the
   datasheet); verify the CAP_DSHOW index-order assumption by opening each
   index.
2. Full probe matrix on both DSHOW and MSMF → paste tables; confirm
   headline modes (1920×1200@120 UYVY/MJPG; 2064×1552@72 Y8); pick backend
   per camera.
3. Exposure: set two values, confirm readback **and** frame-brightness
   change; record working auto-exposure on/off values per backend (correct
   the QUIRKS table if reality disagrees).
4. `calibrate --save` → re-run calibrate → settings round-trip intact.
5. `selftest` green incl. the achieved-fps gate.
6. `palletscan run` → physical unplug/replug → reconnect <10 s,
   `source.reconnects` increments, brightness unchanged after replug
   (the UVC settings-reset-on-re-enumeration trap).
7. 30-minute run: fps stable, zero stalls, CPU noted.
8. Quirk watch-list: MSMF hung-read-on-release (zombie counter), MJPG
   artifacts vs decode rate, Y16 layout of the mono cam, mode changes
   resetting controls.

## Risks and mitigations

- **Hung `read()` / zombie readers**: release-first unblocking,
  generation-token frame discarding, bounded zombie count with crash-only
  escalation. Worst case (every reopen hangs) converges to a process
  restart within ~4 cycles, not unbounded thread growth.
- **Backend quirk table unverifiable until hardware**: quirks are data in
  one constant; every set is paired with honest readback reporting;
  checklist step 3 exists to correct the table.
- **macOS name/index order mismatch**: documented best-effort +
  `fallback_index`; production target is Windows, where pygrabber order is
  the supported path.
- **Connect-verify adds ~1 s to reconnect**: inside the <10 s budget;
  `connect_verify_s: 0` disables.
- **cv2.imshow threading on macOS**: preview is main-thread-only and
  optional; all tested paths are headless.
