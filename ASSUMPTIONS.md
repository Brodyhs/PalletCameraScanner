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
    baseline; `pytest -m soak_short` runs a ~6-minute variant of the
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

## Phase 3: live cameras (code-complete against fakes)

29. **Live timestamps anchor once, never re-anchor on reopen.**
    `CameraSource` samples `ts = clock() - t0` right after `read()`
    returns, with `t0` fixed at construction. A reconnect therefore shows
    up as a *real gap in source time* — which is what dedup windows,
    rolling-buffer eviction and miss deadlines should see — and ts stays
    monotonic across any number of reopens. `frame_index` likewise
    increments monotonically across reopens (allocated *before* the
    yield, so an iterator abandoned mid-yield by the watchdog can never
    reissue an index). `CAP_PROP_POS_MSEC` is not used: it is unreliable
    for live devices. `frames()` is single-use **per connection**; only
    the reliability watchdog calls it again, after `reopen()`.
30. **Exposure/gain are stored as raw backend values, beside their
    backend.** The number handed to `CAP_PROP_EXPOSURE` is what the
    config persists — no millisecond abstraction. DSHOW's log2 stops vs
    MSMF's semantics make unit conversion a guess we cannot verify
    without hardware; calibrate records what *worked* and reconnect
    replays exactly that. `cameras[].backend` is saved alongside so the
    values travel with the semantics they were calibrated under. The
    per-backend quirk knowledge (auto-exposure magic values, log2
    quantization tolerance, AVFoundation ignoring controls) is data in
    one table (`QUIRKS` in sources/controls.py), corrected on arrival
    day (ARRIVAL_CHECKLIST step 3).
31. **First connect fails fast; everything after is the watchdog's.**
    `CameraSource.__init__` must enumerate-by-name, open, and apply
    mode+settings or it raises (consistent with VideoFileSource and
    "refuse to run blind" — a station that cannot see its camera at
    startup should say so, not retry silently). At run/(re)connect,
    control-readback mismatches warn-and-continue (frames at
    slightly-wrong exposure beat no frames); calibrate/selftest are the
    strict paths (hard fail on `controls_reliable` backends, honest
    warning on AVFoundation).
32. **The watchdog never gives up, with two escalation valves.** Capped
    jittered backoff (0.5 s → 15 s, attempt counter reset only when a
    frame actually flows) retries reopen forever: process exit cannot fix
    an unplugged camera. Valves: `watchdog.max_outage_s` (default null =
    off) and `max_zombie_readers: 3` — reader threads stuck in hung
    `read()` calls that `release()` failed to unblock are the
    wedged-USB-stack signature, and only a process restart resets a
    wedged stack. Escalation raises `WatchdogEscalation` through the
    crash-only chain; the CLI maps it to **exit code 3** (vs 1 for
    software failure) so ops can distinguish "check cable/hub" from
    "check logs" at the supervisor. Zombie output can never poison the
    stream: every handoff item carries a generation token and stale
    generations are discarded. `PipelineRunner.stop()` explicitly closes
    a watchdog-wrapped source — a source mid-outage never yields, so the
    source thread cannot observe the stop flag between frames; without
    the explicit close, shutdown during an outage would hang.
33. **macOS enumeration is best-effort, by design.** `system_profiler
    SPCameraDataType` names paired with CAP_AVFOUNDATION in profiler
    order; profiler-order-vs-index-order is *not* guaranteed and is
    documented as such. When a platform yields no names at all,
    enumeration returns `[]` loudly and `cameras[].fallback_index` is the
    escape hatch. Production targets Windows, where pygrabber's
    DirectShow filter order is the supported path.
34. **DSHOW list-order == CAP_DSHOW-index-order is an assumption.**
    pygrabber returns DirectShow filter names in graph-enumeration order
    and OpenCV's CAP_DSHOW indexes devices the same way — believed but
    unverifiable without hardware; ARRIVAL_CHECKLIST step 1 verifies it
    and `_list_windows()` is the one place to fix if it is wrong.
    Name matching is case-insensitive substring and must match exactly
    one device — ambiguity is as fatal as absence, because opening "a"
    camera when two match would silently run the wrong experiment arm.
35. **`calibrate --save` loses YAML comments (key order survives).**
    The upsert is narrow (replace-or-append one `cameras[]` entry), the
    merged result is validated as a full AppConfig *before* anything
    touches disk (a corrupt save can never brick the station), and the
    write is tmp-file + `os.replace` with a timestamped `.bak`. PyYAML
    drops comments on round-trip; ruamel.yaml would preserve them but
    adds a dependency for cosmetics (spec §12 says no). The commented
    reference lives in `config/default.yaml`.
36. **`snapshot()` gained a top-level `"source"` section** —
    `{stalls, reconnects, reopen_failures, zombie_readers}` (spec §5
    requires a reconnect count). This is the approved Phase 3 API
    extension to the stats contract (`SNAPSHOT_KEYS` in
    tests/test_metrics.py was amended accordingly — the only existing
    test edited in Phase 3); Phase 4's `/stats.json` inherits the shape.
    Non-camera runs report zeros.
37. **Selftest assets are committed, not generated at runtime.**
    `palletscan/assets/selftest_qr.png` / `selftest_dm.png` (payloads
    `PALLETSCAN-SELFTEST-QR` / `-DM`) were produced once by
    `tools/make_selftest_assets.py` from the repo's own render functions
    (qrcode + pylibdmtx encode) at 8 px/module — vendored per the
    InfoSec posture (spec §3), provenance documented in the tool.
    Selftest sweeps them across a synthetic frame through the *full*
    pipeline (motion → decode → tracker → bus → evidence) and demands
    exactly the expected pass events with zero misses, so it proves the
    deployed station can decode, not merely that files exist. Guard
    tests pin both assets to their payloads.
38. **Plan rationales and deliberate deviations (post-review).** Recorded
    here so nothing diverges silently. Rationales the plan referenced:
    the watchdog is a *wrapper*, not internal to CameraSource (single
    responsibility: detection is testable against stalled synthetic
    sources, recovery against CameraSource+fakes, and the runner needs a
    `frames()` that keeps yielding across reopens — the wrapper is where
    that absorption lives); `cameras:` is a *list*, not a single block
    (the trial runs two cameras with different native modes, calibrate
    must persist per-device settings without clobbering the other, and
    Phase 4 A/B runs both); probe candidate matrices are suggestions to
    *try*, never assumptions (unknown combos fail readback or measure
    low and lose the ranking). Deviations from the plan text, all
    deliberate: (a) `cameras[].width/height/fps/fourcc` are nullable
    (null = leave the device default) — calibrate always locks concrete
    values, but a hand-written minimal entry should not be forced to
    guess; fps checks are skipped/soft when unset. (b) `to_gray` picks
    the packed-YUV luma plane by fourcc (UYVY → channel 1) instead of
    the plan's hard-coded channel 0, which is wrong for UYVY (U Y V Y
    interleave) and would have made every raw UYVY frame undecodable
    chroma; arrival checklist still verifies. (c) The `Backend` enum
    lives in config.py (the field that validates against it lives
    there); the quirk *data* stays in sources/controls.py as planned.
    (d) `calibrate --name` (+ id default `cam-main`) can create a fresh
    entry on a machine with an empty config — without it, first-ever
    calibration would require hand-writing YAML first. (e)
    `PipelineRunner.stop()` closes a watchdog-wrapped source (see #32) —
    found during integration testing; without it, graceful shutdown
    during a camera outage hangs forever. (f) BUFFERSIZE is reported
    honestly but marked informational and never fed to a hard gate:
    DSHOW/MSMF do not implement it, and gating on it would have failed
    every Windows calibrate/selftest (found by adversarial review).
    Residual known gap: a driver hang inside the `cv2.VideoCapture()`
    *constructor* (before any handle exists) is not releasable by
    `close()`; reads/sets after construction are covered. If constructor
    hangs appear on arrival day, move reopen onto a supervised worker
    thread with the zombie accounting.
39. **soak_short lengthened 2.5 → 6 minutes (owner ruling at Phase 3
    close-out).** The 2.5-minute fit window sat almost entirely inside
    macOS's lazy page-reclaim ramp: freed per-segment memory stays
    resident until the kernel reclaims it, so a fresh process measured
    +260..+800 MB/min of phantom "growth" on leak-free code — reproduced
    on pristine Phase 2 HEAD in a clean worktree, while a 6-minute run of
    the identical harness measured **−6 MB/min with final RSS below the
    post-warmup baseline** (no leak; the harness already `gc.collect()`s
    each dead runner's cyclic graph per segment). The amendment changes
    only the window (6 min, 90 s warmup); the 8 MB/min slope gate and
    1.3× final/baseline ratio are unchanged, and the 2 h `tools/soak.py`
    run remains the authoritative memory gate.

## Phase 4: dashboard + A/B trial reporting

40. **Cross-camera dedup is emit-now + merge-by-reemit with a
    revision-guarded upsert (owner-approved D1, with owner amendment).**
    In A/B mode the first sighting of a payload publishes the business
    PassEvent immediately (that event's `event_id` stays the stable
    business id); a second camera's pass within `dedup.window_s` is merged
    and re-published with the same id — min first_seen, max last_seen,
    summed decode_count, merged cameras/camera_detail, best_frame = earlier
    first decode, concatenated candidate_ids. Same-camera repeats are
    suppressed and counted; the window anchor refreshes only on first emit
    (parked-pallet rule, mirrors #16). Misses forward unchanged — the
    per-camera miss IS the A/B experiment's evidence. No held state, no
    expiry timers: report completeness never depends on dedup timing. The
    rejected hold-until-all-cameras alternative needs expiry machinery
    whose early firing systematically undercounts the slower camera — the
    exact experiment this phase runs.
    **Owner amendment — re-emit ordering.** Publish happens outside the
    deduper's lock (the business bus's blocking put must never couple the
    two runners' bus threads), so same-id re-emits can reach storage out
    of order. The guard lives at the storage boundary: a monotonically
    increasing `revision` (assigned under the deduper's lock) rides on
    every re-emitted event, and SqliteSink's upsert replaces a row only
    when `excluded.revision >= events.revision`. A stale v0 arriving after
    v1 is a no-op; a barrier-synchronized two-thread hammer test asserts
    the FINAL stored row is always the fully-merged version. Accepted
    costs: JSONL gets ≤ N_cameras lines per business pass sharing one
    event_id (append-only audit log; readers take max-revision-wins — the
    station summary and `tests` do exactly that); HTTP receivers keep the
    first version per the at-least-once dedupe-on-event_id contract (#23)
    — business fields are correct in v1, merged per-camera detail is a
    local trial-reporting concern; ConsoleSink prints the merged event
    again (harmless). The business bus deliberately has no MetricsRegistry
    (it would double-count merged passes); `/stats.json`'s business
    section serves the deduper's own counters.

41. **PassEvent gained trailing defaulted fields `first_decode_ts`,
    `camera_detail`, `revision` (D2); SQLite schema v2 adds the
    `revision` column.** `camera_detail` maps source_id →
    {first_seen_ts, first_decode_ts, last_seen_ts, decode_count};
    time-to-first-decode = same-camera `first_decode_ts − first_seen_ts`,
    so cross-camera clock skew cancels by construction. The A/B report
    falls back to the `cameras` map for pre-Phase-4 rows (ttfd
    unavailable) so old DBs stay browsable. On connect SqliteSink reads
    `PRAGMA user_version`: 0/fresh → create v2; 1 → `ALTER TABLE ... ADD
    COLUMN revision`; 2 → no-op; newer → refuse (forward-compat guard).
    Deviation from the Phase 4 plan's "only test_metrics.py edits": the
    existing `test_sqlite_sink_rows_queryable` pinned `user_version == 1`
    and necessarily moved to 2 — a second, mechanical existing-test edit.

42. **`/stats.json` envelope (D3, amends the #24/#36 wording):**
    `{"generated_utc": iso, "cameras": {source_id: snapshot() verbatim},
    "business": {deduper counters} | null}` — uniform across single-camera,
    A/B, and standalone modes (standalone serves `cameras: {}`). The
    pinned snapshot dict itself is unchanged except D4: a top-level
    `read_rate_24h` (second `_SourceTimeWindow(86400)` pair fed by the
    same hooks), computed identically to `read_rate_1h`. `SNAPSHOT_KEYS`
    amended accordingly (precedent #36).

43. **A/B evidence is rebased per camera: `<evidence.dir>/<source_id>`
    (D5).** Two runners sharing one evidence root race each other's prune
    (`rmtree` during another's `_candidate_dirs`/`_dir_size` scan) and the
    loser's exception lands in `_finalize_miss` before `_emit` — silently
    eating a MissEvent. Per-camera subdirectories eliminate the race;
    the scan/size helpers additionally tolerate vanishing entries
    (`except OSError` → smaller listing, never an aborted miss write).
    Consequence: evidence size/age caps apply per camera in A/B mode.

44. **SqliteSink sets `PRAGMA busy_timeout=5000` (D6); reviews/manifest
    live in web-owned tables in the same DB file (D7).** The dashboard's
    mark-reviewed/manifest writes open a second writer connection on the
    same WAL DB; without the timeout the bus thread's commit can raise
    SQLITE_BUSY immediately and drop an event row. `miss_reviews`
    (keyed by miss event_id — reviews survive evidence pruning) and
    `manifest` are created by the web ReadStore, never by SqliteSink.
    Manifest upload is a raw `text/csv` request body (FileReader → fetch);
    no python-multipart dependency. `report.manifest_path` is the
    config-pointed fallback when the manifest table is empty.

45. **Merged cross-camera first/last timestamps mix per-source clocks**
    anchored ~1–3 s apart at construction — display-grade, not
    measurement-grade. Time-to-first-decode is skew-free by construction
    (#41). No shared-epoch plumbing this phase; revisit only if the trial
    needs cross-camera latency comparisons.

46. **The live MJPEG stream is tested against a real uvicorn server on an
    ephemeral port, not the Starlette TestClient** (deviation from the
    plan's test sketch, found during implementation): the installed
    TestClient runs each ASGI request to completion on its portal before
    returning — `client.stream()` blocks forever on an unbounded
    multipart response (verified by faulthandler dump). `DashboardServer`
    (uvicorn on a daemon thread, `port=0` supported, `started`-flag
    startup wait, `should_exit` shutdown) was pulled forward from Step 7
    and is itself under test. uvicorn off-main-thread skips signal
    handlers, so the pipeline's SIGINT handling stays in charge; a failed
    bind sys.exit()s in the thread and is absorbed into a logged error +
    `DashboardServerError` from `start()`.

47. **`web.port: 0` means ephemeral** (the CLI prints the picked port) —
    needed so tests and parallel dev runs never collide on 8000; the
    validator allows 0–65535. `palletscan dashboard` refuses to start if
    the events DB file is absent (a typo'd path silently showing "no
    events" would misreport a finished trial) — exit 2.

48. **httpx joined `[dev]` extras only** (TestClient transport, D10);
    runtime dependencies are unchanged (fastapi/uvicorn were already
    required). The dashboard UI is vendored vanilla JS/CSS under
    `palletscan/web/static/` (packaged via package-data) — no CDN, no
    build step, offline-first.

49. **Phase 4 close-out adversarial review: five fixes landed before
    commit.** A multi-agent review of the phase diff (every finding
    independently verified by two adversarial refuters with reproductions)
    confirmed and we fixed: (a) naive `window_from`/`window_to` query
    values (e.g. `2026-06-11T08:00`) raised TypeError against the
    always-aware stored stamps and 500'd all three report endpoints —
    naive bounds now normalize to UTC; (b) Excel "CSV UTF-8" manifests
    carry a BOM that defeated the header rule, silently adding a phantom
    expected payload and deflating true read rate — `parse_manifest` now
    strips it, and unparseable CSV raises ValueError → HTTP 400 instead
    of 500; (c) `DashboardServer.stop()` stalled its full join timeout and
    leaked a still-serving thread whenever an MJPEG viewer was connected
    (uvicorn's graceful shutdown waits forever by default) —
    `timeout_graceful_shutdown=3` bounds it, verified by a
    stop-with-connected-client test; (d) SqliteSink cached its connection
    before migration succeeded, so a failed/refused migration was bypassed
    on the next write — the connection is now cached only after a
    successful migrate; (e) a dashboard port collision surfaced as a raw
    traceback — now a clean message + exit 2 on all CLI paths. The review
    also flagged that two §4 verification-matrix rows lacked their
    promised station-run end-to-end proof; the missing integration test
    (station DB → ReadStore → A/B report + truth-derived manifest
    reconciliation) was added rather than amending the matrix.
