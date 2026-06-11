# ASSUMPTIONS

Decisions made during Phase 1 where the spec left room. Numbered for
reference; revisit any of these when real hardware/data arrives.

## Environment

1. **Python 3.13 venv** for development (3.14 risked missing `opencv-python`
   wheels at setup time). All code stays 3.11-syntax-compatible per the spec
   floor.
2. **`brew install zbar libdmtx` is a macOS dev prerequisite.** These are
   native libraries, not pip packages; on the Windows target the pip wheels
   bundle the DLLs, so the pip-only InfoSec posture holds where it matters.
3. **`setuptools` is a runtime dependency** because pylibdmtx 0.1.10 imports
   `distutils` (removed in Python 3.12). `palletscan/_compat.py` installs
   setuptools' distutils shim programmatically before importing pylibdmtx —
   robust even where `.pth` processing is disabled.
4. **This dev Mac intermittently flags venv `.pth` files `UF_HIDDEN`**
   (macOS provenance tagging), which Python ≥3.12 `site.py` silently skips,
   breaking editable installs and the setuptools shim. Mitigations: the
   `_compat` shim (3.), tests run via `python -m pytest` from the repo root,
   and `tools/` scripts bootstrap `sys.path` themselves. Not expected on
   the Windows target.

## Synthetic envelope (the heart of the acceptance gate)

5. **Calibration is dimensionless.** Two ratios govern decodability and are
   drawn directly: px/module ∈ [3, 6] (the optics envelope at 3–15 ft) and
   blur-in-modules = speed × exposure / module size (5 mm modules, spec §1).
   Pixel scale is *derived* (`px_per_meter = px_per_module / module_size_m`),
   so the envelope is invariant to frame size — the 960×540 acceptance config
   tests the same physics as 1280×720.
6. **`exposure_fraction: 0.03` (~1 ms at 30 fps) is the operating point**,
   matching a locked global-shutter exposure; it yields ≤ ~0.9 module of
   blur at the 10 mph design margin. It remains a config stress knob; the
   acceptance test runs at the operating point.
7. **Occlusion is modeled as a static foreground occluder** (post/pole) that
   the pallet passes behind — transient per frame, so some frames are clean.
   Measured during development: an occluder bar *attached to the pallet face*
   permanently covering the Data Matrix finder/timing patterns makes the
   whole pass undecodable (libdmtx exhausts its search), which would violate
   the decodable-by-design envelope. A real strap-over-the-code produces a
   legitimate miss + evidence, which is the exception path, not the envelope.
8. **QR rendered at ECC level Q**; payload placeholder format `PLT-NNNNNN`
   until the real pallet ID scheme is known.
9. **Synthetic scene**: frozen background texture + one fixed lighting
   gradient per run ("fairly constant ambient lighting", spec §1); contrast
   is applied to the pallet face (print/material property); Gaussian sensor
   noise per frame.

## Pipeline design

10. **Single inline pipeline thread** (MotionGate → DecodeEngine →
    PassTracker): strict per-frame ordering and shared per-segment state make
    separate stage threads pure overhead. The spec's bounded-queue seams sit
    where they matter — source→pipeline (`DroppingQueue`, drop-oldest with a
    counter) and pipeline→sinks (blocking, never drops events). Decode
    parallelism lives inside the Executor.
11. **ThreadPoolExecutor is the measured default** (`decode.executor`).
    `tools/bench_decoders.py` on this machine (Apple Silicon, 2026-06-10):
    thread pool 0.02–0.06 s wall for 60 mixed decodes vs 0.31–0.52 s for
    processes (ndarray pickling dominates); 8-way concurrent decode results
    were bit-identical to serial — pyzbar/pylibdmtx are thread-safe per call
    (each call builds its own native scanner state). Re-run on the Windows
    target and flip the config key if it disagrees.
12. **The per-frame decode budget is soft.** In-flight C calls can't be
    cancelled from Python; the worst case is bounded by ROI-only decoding and
    libdmtx's native timeout. Overshoots are counted
    (`DecodeEngine.counters.budget_overruns`).
13. **pylibdmtx never sees a full frame** — motion-ROI crops only, with
    `timeout=min(dm_timeout_ms, remaining budget)` and `max_count=1`.
14. **Motion debounce is 3 frames** (`motion.open_frames`), not 2: a
    single-frame flash produces *two* consecutive active diffs (appear +
    disappear) under frame differencing. Segment opens are backdated to the
    first active frame, so no timing information is lost.
15. **All time logic keys off the frame source clock** (`Frame.ts`), never
    wall clock — dedup windows, rolling-buffer eviction, miss post-roll
    deadlines. Accelerated/non-realtime runs are behaviorally identical to
    realtime; this is also what makes the acceptance test fast.
16. **Dedup window 12 s** (spec default), keyed by payload. A repeat sighting
    within the window merges into the recent pass (counted in
    `passes_merged`, logged) and does not emit a second business event.
17. **Miss finalization waits for the post-roll** (`buffer.post_s`, 2 s of
    source time) so evidence bursts include frames after the segment closed;
    end-of-stream `flush()` finalizes with whatever exists.
18. **Evidence caps**: 500 MB total / 14 days, pruned oldest-first on every
    write; JPEG quality 85, every 3rd frame.
19. **Per-camera A/B readiness**: `PassEvent.cameras` is a per-source decode
    count map from day one, so Phase 4's rule (business dedup across cameras,
    per-camera stats kept separate) needs no schema change.

## Acceptance test

20. **400 passes, fixed seed (20260610), ≥99.5% ⇒ at most 2 misses.** The
    failure message prints each missed pass's px/module and blur-in-modules
    (also logged per pass in `truth.jsonl`) so a regression points at the
    exact corner of the envelope that broke.
