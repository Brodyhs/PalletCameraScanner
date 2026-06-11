# ASSUMPTIONS

Decisions made where the spec left room (Phase 1: #1–20, Phase 2: #21–28).
Numbered for reference; revisit any of these when real hardware/data
arrives.

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
    where they matter; the source→pipeline queue's policy follows
    `FrameSource.live` — live capture drop-oldest with a counter (stalling
    the device is worse than losing one of a pass's dozens of frames),
    finite sources (synthetic, replay) block and never drop (their frames
    are all available; dropping would fabricate data loss). Pipeline→sinks
    always blocks. Decode parallelism lives inside the Executor.
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
    `timeout=min(dm_timeout_ms, remaining budget)` and `max_count=4`
    (more than one pallet face can share a motion segment, so never cap at
    1; the cap plus the native timeout still bounds worst-case scan time).
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
    `passes_merged`, logged) and does not emit a second business event. The
    window refreshes **only on emit**: merged sightings do not extend the
    suppression, so a pallet parked in view cannot be deduped forever.
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

## Phase 2: replay, metrics, HTTP sink, soak

21. **Replay keeps the file's native clock.** `Frame.ts = frame_index /
    fps` regardless of playback speed; `video.speed` shapes only wall-clock
    delivery (1.0 as-if-live, >1 accelerated, 0 unpaced). With #15 this
    makes dedup/buffer/miss behavior bit-identical at any acceleration.
    `VideoFileSource.live` is always False — every frame of a file is
    available, so backpressure blocks rather than drops ("as-if-live"
    means paced, not lossy). Looping reopens the capture (not every codec
    seeks cleanly) and `frame_index` keeps incrementing so ts stays
    monotonic.
22. **Recording is .avi/MJPG only** (`tools/record_synthetic.py` /
    `palletscan/sources/record.py`): OpenCV's bundled mp4 encoders are
    lossy enough to perturb the decodability envelope and H.264 encoder
    availability varies by platform; MJPG (per-frame JPEG) is pip-only-safe
    and high-fidelity. Frames are written as replicated-channel BGR
    (single-channel VideoWriter support varies by backend); the round trip
    back to gray at replay ingest is lossless for achromatic frames.
    *Replay* accepts any .mp4/.avi the OS can decode. The replay acceptance
    test (40 recorded passes, moderate in-spec envelope) demands decoded ==
    truth exactly, proving the codec round trip does not eat passes.
23. **HTTP sink = SQLite outbox + uploader thread.** The bus thread only
    INSERTs (never touches the network); delivery is one event per POST,
    2xx acks, **at-least-once** (crash between POST and DELETE re-sends) —
    receivers dedupe on `event_id`. All non-2xx responses are retried with
    jittered exponential backoff capped at 60 s (even 4xx: with the
    endpoint TBD, a misconfiguration must not silently discard events).
    Redirects are failures, not followed — urllib would turn a redirected
    POST into a body-less GET and ack an event the receiver never saw;
    `url` must point at the final endpoint. The outbox caps
    (200 MB / 14 d) bound the backlog, pruning oldest-first with counted
    and logged drops. `close()` stops the uploader after the in-flight
    attempt; pending rows persist and drain on next start — that is the
    store-and-forward guarantee. Batching stays a future config knob
    (~7 events/min makes it premature). The WAL switch happens once,
    single-threaded, before worker threads connect (the rollback→WAL
    transition returns SQLITE_BUSY immediately, bypassing busy_timeout).
24. **Metrics are per-runner, lock-free, approximate by design.** One
    `MetricsRegistry` per PipelineRunner (no globals); existing component
    counters stay the source of truth, read lazily at snapshot. fps uses a
    wall-clock window (`metrics.window_s`); passes/hour and 1h read rate
    use **source-time** windows anchored at the latest frame ts (so rates
    decay during idle and accelerated replay reports source-time rates).
    Decode latency p50/p95 comes from a bounded reservoir of per-frame
    decode **wall time including failed attempts** (success-only latency
    would flatter the p95). `snapshot()`'s key structure is the contract
    Phase 4's `/stats.json` serves verbatim (`tests/test_metrics.py`
    pins it).
25. **Lazy pass planning.** Pass *i* is planned on demand from
    `SeedSequence(seed, spawn_key=(i+2,))` — verified bit-identical to the
    eager `spawn()` version — so only the current and previous plans
    (~100 KB of rendered patch each) are alive during iteration. Required
    for multi-hour synthetic soaks; eager planning held every patch for
    the whole run.
26. **Phase 2 verifies the crash-only half of recovery.** An injected
    source failure ends the run through the flush path (pending misses
    become events), the soak harness restarts the run (gap measured,
    asserted <10 s), and zero event loss is proven by per-segment truth
    reconciliation plus a dead-endpoint outbox whose rows must equal every
    emitted event id across restarts. The in-process watchdog
    (stall→reopen) is Phase 3 per spec §10; `FlakySource.stall_at` exists
    as its ready-made test fixture.
27. **psutil lives in `[dev]` extras only** — `tools/soak.py` samples RSS
    with it (`resource.getrusage` doesn't exist on the Windows target);
    the runtime package never imports it. Soak thresholds: post-warmup
    least-squares slope < 1 MB/min and final RSS < 1.3× the post-warmup
    baseline; `pytest -m soak_short` runs a ~2.5-minute variant of the
    same invariants.
28. **2h soak results** (2026-06-11, Apple Silicon dev Mac,
    `python tools/soak.py --hours 2 --mode replay --stats-interval 300`,
    40-pass auto-recorded clip looped unpaced):

    ```
    ── soak report ──
    duration         : 120.0 min (1 segment(s), 0 injected restarts, max restart gap 0.00s)
    frames / events  : 1921349 / 34067
    truth accounting : 0/0 decoded, 0 missed-with-evidence   (replay mode: no truth)
    cpu              : avg 212% max 224%
    outbox           : skipped
    rss              : baseline 369.3 MB -> final 344.0 MB (peak 425.4), slope -0.414 MB/min
    verdict          : OK
    ```

    ~267 fps sustained for two hours; RSS slope negative (final below the
    post-warmup baseline), zero frame/sink/thread errors. CPU% is
    process-total across cores under *unpaced* load — a paced 30 fps
    camera feed is ~9x lighter; the spec §11 ≤50%-of-4-cores measurement
    under realistic burst load is re-verified in Phase 5 ops hardening.
