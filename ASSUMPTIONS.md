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
    *Amendment (0c30c77 fix session):* camera-backed runs seed this window
    from the previous run's stored passes (#59), so the rule holds across a
    supervisor restart too.
17. **Miss finalization waits for the post-roll** (`buffer.post_s`, 2 s of
    source time) so evidence bursts include frames after the segment closed;
    end-of-stream `flush()` finalizes with whatever exists.
    *Amendment (0c30c77 finding 2):* a source discontinuity (watchdog
    reconnect) is a third finalization trigger — pending misses finalize
    immediately with whatever post-roll exists, because frames from after
    the gap are never pallet-exit evidence. The post-roll also dedupes
    against frames already sampled into the segment reservoir (finding
    b11), so `evidence_frame_count` equals the files actually stored.
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
    *Amendments (0c30c77 fix session):* (a) the reconnect boundary is now
    an explicit signal, not just a ts gap — the watchdog marks the first
    frame of every recovered connection `Frame.discontinuity=True`, the
    motion gate hard-closes any open segment at its pre-gap last-active
    frame (finding 2: a segment spanning the gap let a decoded pallet
    swallow the pre-gap pallet's MissEvent), and the drop-oldest frame
    queue carries the mark across drops (the one non-redundant frame).
    Time itself is still never re-anchored. (b) In station mode every
    camera anchors to ONE shared `(monotonic, wall)` epoch pair sampled
    before any device opens (finding b8), so cross-camera ts comparisons
    carry no construction skew; `epoch_wall` (the wall instant of ts=0)
    also lets events be stamped with the wall time of their close ts
    (finding b12) and bridges the dedup window across restarts (#59).
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
    *Amendment (0c30c77 finding 8):* `fallback_index` has a second role —
    it is the **only** way to open a camera under an explicit backend
    that is not the enumeration backend (e.g. `backend: msmf` on Windows,
    where only DSHOW enumerates names). A name-resolved index under
    another backend silently captures whatever device sits at that slot
    after a replug shifts the order, so that combination is now a hard
    connect error without a pinned index; with one, every connect warns
    that name stability is forfeited. The run path, selftest and
    calibrate all enforce the same rule.
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
    *Amendment (Phase 5, D11):* the fixed 90 s warmup is gone — warmup is
    now detected adaptively (`detect_warmup`: the earliest 60 s window
    whose least-squares RSS slope drops under 2 MB/min, clamped to
    [45 s, half the run]), making soak_short portable to the Windows box
    whose reclaim/allocator ramp is unknown. The verdict records
    `warmup_used_s` so runs stay comparable; an explicit `--warmup-s`
    still pins it; the gates are unchanged. A genuine leak never plateaus,
    so adaptive warmup rides its max bound and the slope gate fails on the
    remaining half of the run, as it should.

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
    *Amendment (0c30c77 finding 9):* the deduper's per-payload state is a
    LIST of window entries, not one. A re-sighting beyond every entry's
    window becomes a new business pass *appended beside* the old entry —
    never replacing it (replacement destroyed state a lagging camera could
    still merge with, misattributing its backdated sighting to the wrong
    physical pass). Matching is two-sided (an event staler than every
    anchor by more than the window is its own, earlier pass) and picks the
    NEAREST anchor (ties to the older) — anchors of one payload are
    pairwise > window apart, but two can both be within the window of one
    event when their spacing is in (window, 2·window].

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
    *Amendment (7e4c22c review, finding 2):* the original fix guarded
    only the `_candidate_dirs`/`_dir_size` scan. `prune()`'s trailing
    empty-day cleanup was left unguarded, so concurrent churn (`rmdir`
    on a just-repopulated day, a day vanishing mid-scan) could still
    raise out of `write_burst` after the pending miss was popped — the
    same silent MissEvent loss this entry claimed was eliminated; the
    review's verifier reproduced it. The cleanup loop now carries the
    same `except OSError` tolerance, with regression tests racing both
    the `rmdir` and the emptiness check. External-process churn against
    the evidence tree remains possible even with per-camera roots, so
    the tolerance is load-bearing, not belt-and-braces.
    *Second amendment (0c30c77 finding 1) — the claim above was still
    wrong.* Two unguarded raisers remained on the very same path: the
    burst directory `mkdir` and the `meta.json` write — an ENOSPC there
    (the 24/7 disk-full regime, not a race) ate one MissEvent per
    finalize and aborted the shutdown drain. The invariant is no longer
    "the writer never raises" but **the miss emits regardless**:
    `write_burst` degrades every OSError to a flagged
    `EvidenceRef(error=...)`, and `_finalize_miss` catches anything else
    and still emits the MissEvent evidence-less with
    `evidence_error` set and the `evidence_failures` gauge incremented —
    disk exhaustion degrades loudly, never by silent event loss. The
    burst directory is also collision-guarded (`-rN` suffix): candidate
    ids carry a per-run token (finding 5), and even a colliding id can
    no longer byte-overwrite a stored miss's evidence.

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
    *Superseded (0c30c77 finding b8):* StationRunner now anchors every
    camera to one shared epoch sampled before any device opens, so the
    construction skew is gone and cross-camera ts comparisons (the dedup
    window above all) are aligned by construction. The ttfd computation
    keeps its same-camera form regardless.

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

50. **`DashboardServer` is single-use; restart is a Phase 5 concern
    (7e4c22c review, finding 17 — guarded, deliberately not fixed).**
    `stop()` consumes the underlying `uvicorn.Server`: `should_exit`
    stays set and its `started` flag never resets, so a second `start()`
    used to report success while the serve thread exited within a tick —
    every subsequent request got connection refused. All five CLI call
    sites build a fresh instance per run and stop exactly once, so
    nothing trips this today; rather than building restart machinery now,
    `start()` after `stop()` raises `DashboardServerError`
    ("single-use") — a loud failure beats silently serving nothing.
    Revisit in Phase 5: service-restart/supervision machinery is exactly
    what that phase builds, and a restartable server (fresh
    `uvicorn.Server` per start) belongs with it.

51. **Cross-camera dedup eviction keys on the slowest camera's high
    water, with counted/logged forced evictions (7e4c22c review,
    findings 1+16 — one family).** The original `_prune` used a single
    global high-water cutoff fed by all cameras, so one camera's
    progress could evict payload state the other, lagging camera was
    still inside the merge window for — its late sighting then
    double-counted as a new business pass (reproduced live by the
    review). Eviction now keys on `min(per-camera high water) −
    window_s` (per-camera event timestamps are monotonic, so that state
    is unreachable by every camera); `StationRunner` hands the deduper
    its camera ids so eviction waits for the slowest camera from the
    first event, and a camera's misses also advance its high water
    (decode droughts must not halt eviction). Consequence: a silent
    camera halts time-based eviction and the `_MAX_TRACKED` cap becomes
    the memory bound — hitting it force-evicts in-window state, which is
    why every forced eviction is counted (`forced_evictions`, new
    `stats()` key, additive per #41/#42) and logged per the
    counted-logged-drops convention.
    *Amendment (0c30c77 finding 9):* the prune rule is now per-ENTRY
    (state is a list per payload, #40 amendment); a payload's key is
    removed when its last entry is pruned, and the `_MAX_TRACKED` cap
    counts total entries.

## Phase 5: hardening + ops

52. **Single-instance lock: OS-level locking, not a PID file (D1/D2).**
    `fcntl.flock(LOCK_EX|LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK)` on
    Windows, on `<data-dir>/palletscan.lock`, held open for the process
    lifetime. **Stale locks are structurally impossible** — the OS
    releases the lock when the holder dies, cleanly or not (proven by the
    hard-killed-subprocess test); no liveness probing, no PID-reuse
    races. Holder diagnostics (pid/start/argv) are JSON at file offset 0;
    because Windows byte-range locks are *mandatory* (a locked byte is
    unreadable by other handles), the actual lock byte sits at offset
    0x100000 — past EOF, which is legal — so the JSON stays readable for
    ops. `release()` unlocks before close (MSDN: release timing after an
    abnormal close is indeterminate; the supervisor's ≥5 s restart delay
    covers that window); the file is never unlinked (delete-while-open
    races a fresh acquirer; the leftover is last-holder diagnostics).
    Scope: per data-dir, writer commands only (`run`/`synth`/`replay` —
    they own sinks/evidence/logs); `dashboard`/`calibrate`/`selftest`
    take no lock (read-only/pre-flight; RUNBOOK: stop the service before
    calibrating). Contention → exit code **4** naming the holder. Two
    instances with different `--data-dir` coexist by design.

53. **Log rotation: stdlib RotatingFileHandler; lock scope ==
    file-logging scope (D3).** `logging.file` config: JSONL to
    `<data-dir>/logs/palletscan.jsonl`, 20 MB × (5+1) files = 120 MB cap,
    14-day age prune at handler install (`prune_old_logs`,
    OSError-tolerant, always sparing `restarts.jsonl` — the ops audit
    trail). The handler installs **only in the writer commands, after the
    lock is held**: `doRollover()` renames, which fails on Windows if
    another process holds the file open — single-writer rotation is an
    invariant, not a hope. The stderr JSON handler stays in every command
    (dashboard/calibrate/selftest are stderr-only). A startup INFO line
    ("<cmd> started: lock ... held by pid ...") marks each run boundary
    and materializes the `delay=True` file even on quiet runs. The CLI
    releases lock + file handler together (`_WriterLease`) because main()
    runs in-process under pytest and must not leak handlers across calls.
    Event sinks are exempt from rotation by design — see #57.

54. **Supervisor: `palletscan supervise` (in-package, ~200 lines), Task
    Scheduler only boots it (D4–D7).** Task Scheduler alone cannot meet
    the requirements (-RestartInterval has a 1-minute floor,
    -RestartCount is bounded, only the last run result is recorded);
    NSSM is a third-party exe (InfoSec: Python + pip only). The
    supervisor restarts the child on **any** nonzero exit — including 2
    (usage/config error), per owner ruling: a station must come back by
    itself once ops fixes the file (loud "fix the config" log; a
    permanently bad config burns one spawn per 300 s). Backoff: 5 s base,
    doubling while runs last < 60 s, capped at 300 s, reset after a
    stable run; clean exit 0 ends supervision. Every child exit appends
    `{ts, exit_code, runtime_s, delay_s, reason}` to
    `<data-dir>/logs/restarts.jsonl` (escalation counting is a
    PowerShell one-liner, RUNBOOK §7). Two locks: the supervisor holds
    `palletscan.supervisor.lock`, the child holds `palletscan.lock`
    (lock handles don't leak into children — PEP 446). Children are
    spawned as `python -m palletscan …` (hence `__main__.py`, D12) with
    no pipes (a full pipe would deadlock a chatty child) and
    CREATE_NEW_PROCESS_GROUP on Windows. Stop channel: stop-file
    (`supervisor.stop`, polled at 0.5 s) primary — console-ctrl events
    cannot cross Windows sessions — then CTRL_BREAK/SIGTERM, 15 s grace,
    kill. Signals are the secondary channel; on POSIX the SIGINT handler
    does *not* forward to the child (terminal Ctrl-C already hit the
    foreground group; forwarding would trip the child's
    second-signal-forces path mid-drain), while directed SIGTERM and all
    Windows ctrl events do forward. Convenience ruling: `supervise
    --data-dir D` auto-appends `--data-dir D` to the child args unless
    they already carry one (either argparse spelling; abbreviations are
    disabled CLI-wide — 0c30c77 finding b1).
    *Amendments (0c30c77 findings 6/7/13/15 — the stop/lifetime protocol
    redesign; replaces the original "stale stop-file at startup is
    removed and ignored" rule).* (a) The stop-file is a STICKY LATCH:
    the supervisor never deletes it, and one found at startup is honored
    (no spawn, exit 0, a `restarts.jsonl` line with reason
    `stop-honored-at-startup` and null exit_code) — a stop request
    survives Task Scheduler revivals and reboots; only
    `start_palletscan.ps1` (or deleting the file) re-arms the station.
    (b) Child lifetime ⊆ supervisor lifetime: a Windows kill-on-close
    job object (best-effort ctypes, arrival-verified) plus a portable
    child-side parent watch (`SUPERVISOR_PID_ENV`; pid-reuse-safe
    retained handle on Windows, ppid on POSIX; injected per spawn via
    the Popen env argument, never by mutating os.environ) plus a
    stop-the-child-first guard on unexpected supervisor exceptions.
    Consequence: on Windows a supervisor death hard-kills the child
    mid-queue via the job object — within crash-only tolerance; the
    graceful self-drain applies on POSIX and when job assignment failed.
    (c) "Stopped" is VERIFIED, never assumed: `stop_palletscan.ps1`
    probes both instance locks with non-blocking lock attempts on the
    lock byte (death-proof, detects orphans it has never heard of),
    kills the writer-lock holder's pid (from the lock's diagnostics
    JSON) on the hard path, polls ~10 s through the indeterminate
    post-kill release window, and refuses to act on a directory with no
    station state; both scripts derive the data dir from the registered
    task's arguments. (d) The supervisor's own bookkeeping
    (`restarts.jsonl` append) never raises: a full disk must not kill
    the process that exists to restart the crashed child (finding 13).

55. **Service identity: interactive station user + netplwiz auto-logon,
    not SYSTEM (D8).** UVC capture via OpenCV under session 0 is a known
    failure mode (Windows camera frame server + per-user privacy consent
    gate desktop camera access) and cannot be verified before hardware;
    running blind on day one is the worse risk. Cost: kiosk posture (the
    box stays logged in) — RUNBOOK notes physical security; the
    ARRIVAL_CHECKLIST §9 verifies capture inside the task's session.

56. **Spec §11 CPU measurement: method + dev-Mac results (D10;
    supersedes the deferral in #28).** Method: a recorded **burst clip**
    (idle gaps tightened to 0.2–0.8 s → ~50 passes/min ≈ 7× the 7/min
    spec average; decodability envelope untouched — burst means cadence,
    not blur) replayed at `--speed 1.0 --loop 0`, children sampled via
    `psutil.Process.cpu_percent()` at 1 Hz for 300 s per scenario
    (`tools/measure_cpu.py`). Replay, not realtime synthetic:
    VideoFileSource paces on an absolute schedule and its
    MJPG-decode-per-frame cost is the closest proxy for live MJPEG UVC
    ingest, while synthetic rendering cost doesn't exist in production.
    Results (2026-06-12, Apple Silicon dev Mac, 10 logical cores; raw %
    = sum over cores, normalized = raw/4 for the spec's 4-core budget):
    *baseline* (1 replay child, no dashboard) avg 33.4% raw / 8.4%
    normalized, p95 44.8% / 11.2%; *station* (2 replay children +
    dashboard + 1 live MJPEG viewer) total avg 63.7% raw / **15.9%
    normalized**, p95 85.5% / 21.4%, max 110.8% / 27.7% — comfortably
    under the ≤ ~50% bar, with headroom noted for the slower factory
    box. Production note: real A/B runs one StationRunner process,
    marginally cheaper than the two processes measured here. Dev-Mac
    numbers are indicative; the binding factory-box run is
    ARRIVAL_CHECKLIST §7/§9 (same tool, report filed at
    `data/cpu/cpu_report.md`).

57. **Event sinks stay unbounded by design (D13).** `events.jsonl` and
    `palletscan.db` are the data of record (audit trail, dashboard,
    A/B report, reconciliation), unlike diagnostic logs. Spec §4's
    "auto-prune evidence and logs" is satisfied by the evidence caps
    (Phase 1, #18) plus log rotation/age-pruning (D3, #53); the HTTP
    outbox already has size/age caps. Rotating the audit record would
    defeat its purpose, and SQLite cannot be "rotated" without a
    retention feature nobody asked for (spec §12: no speculative
    features). RUNBOOK §10 documents expected growth (~5–10 MB/day per
    file at the spec's 10k passes/day) and the stop → move → start
    archival procedure; ops owns the cadence.

## 0c30c77 fix session (whole-system review at the final software gate)

58. **(Re)connect policy: what a camera may silently differ on vs what
    must fail loudly (findings 3/8 + b9).** After any (re)connect,
    control VALUES (exposure/gain/brightness readback, achieved fps,
    buffersize) may differ — warned and counted in the new
    `source.connect_mismatches` gauge; frames at slightly-wrong exposure
    beat no frames. Identity- and interpretation-bearing state may NOT:
    the packed-YUV luma channel is derived from the fourcc the device
    actually NEGOTIATED (readback; falls back to the configured value
    when readback is 0.0/garbage, warned + counted), and the FIRST
    DELIVERED FRAME of every connection is the format oracle — a
    2-channel frame with no derivable luma layout, or a geometry that
    differs from the locked width/height, raises into the watchdog's
    retry path (`reopen_failures` climbing, fps 0: visibly broken, never
    silently scanning chroma or the wrong optics envelope). Calibrate's
    live metrics read the same negotiated-format luma plane (b9). The
    cost accepted: a config with locked width/height fails loudly on a
    device that cannot deliver it (including dev Macs — omit
    width/height in dev configs or recalibrate; the error says so).
    Explicit-backend/name-resolution mixing is a hard error without a
    pinned `fallback_index` (#33 amendment); a mismatch error at
    construction exits 1 and churns under the supervisor — loud, and
    deliberately not a config-load check because production configs
    (msmf/dshow) must stay loadable on dev machines for the read-only
    commands.

59. **Restart-spanning dedup: suppress-and-count via a wall-clock bridge
    (finding 10).** Camera-backed runs (the only mode with both restarts
    and a wall anchor) seed the deduper — and each per-camera tracker,
    filtered to payloads that camera saw — from the local store's recent
    pass rows: anchor = stored `wall_time_iso` − `epoch_wall`, clamped
    ≤ 0, discarded (counted + logged) when a backward wall-clock step
    would place it after process start, newest row per payload. A
    re-sighting within the window of a seed is SUPPRESSED
    (`restart_repeats_suppressed`, new stats key), not merged: there is
    no in-memory event to merge into, and reconstructing one from
    detail_json buys little for its complexity. **Documented cost, not
    independence:** the suppressed sighting writes no `camera_detail`
    into the stored business row, so the A/B report loses the
    post-restart camera's datapoint for restart-spanning passes — and
    since the downstream camera is always the post-restart one for a
    pallet in transit, that loss is direction-biased by camera order.
    At the trial's restart rates (escalations are counted in
    restarts.jsonl) this is noise; if restarts churn, the bias is
    visible in the same file. Seeds obey the slowest-camera prune rule
    and the cap. The loader never raises and never creates the DB
    (first boot must not be blockable by its own bookkeeping).
    Replay/synth runs neither seed nor anchor — determinism. Caveat: a
    `replay` into the live data dir writes wall-stamped pass rows a
    `run` started within ~window+slack could seed from; RUNBOOK says
    don't do that.

60. **Shutdown drains are verified, not assumed (finding 11).**
    `EventBus.shutdown()` returns whether the bus actually drained; a
    timeout counts the abandoned queue depth (`events_lost`), and the
    runner fails the run (nonzero exit → supervisor restart) instead of
    printing a clean summary over a silent tail loss. Publishing after
    shutdown began (the station SENTINEL-overtake) is counted
    (`published_after_shutdown`) and logged. The supervised-stop path is
    unchanged: the 15 s grace kill preempts the join and is already a
    documented, logged loss boundary.

## Bring-up backfill (4d95b67 hardware bring-up, as fixed by dc7c3d9) — 2026-07-01

The 2026-06-20→23 bring-up commit `4d95b67` made these decisions without
ledger entries (REVIEW_bringup_4d95b67.md, strategic concern 4: "the
documentation ledger broke"). Backfilled 2026-07-01, recorded as amended
by the dc7c3d9 fix session — each entry states both the bring-up decision
and where the review corrected it.

61. **pygrabber DirectShow backend for the mono camera (37CUGM).** The
    See3CAM_37CUGM (Sony IMX900 mono) exposes ONLY Y8/Y12 formats, which
    OpenCV cannot read on Windows: MSMF negotiates Y8 but its source
    reader never starts (0 fps, MF error -1072875852) and DSHOW won't
    open the device at all. `backend: pygrabber` drives the same
    DirectShow SampleGrabber graph e-CAMView uses — pygrabber was already
    a pip dependency, so the InfoSec posture (Python + pip only, no
    vendor SDK, no driver) holds. The load-bearing design: the entire
    graph lives on ONE dedicated COM-initialized owner thread
    (DirectShow objects are apartment-bound; the watchdog reopens from
    other threads), hidden behind the existing `Capture` protocol so
    CameraSource/watchdog/settings run unchanged. Accepted constraint:
    pygrabber hardcodes the SampleGrabber media type to RGB24, so every
    mono frame takes a colour round-trip plus two copies (~13 MB/frame
    at 2064×1552); grabbing Y800/GREY directly requires forking
    pygrabber internals (its BufferCB is 3-channel-only), which the
    measured ~63–72 fps at full res does not justify today. Revisit if
    CPU headroom becomes the binding constraint.

62. **zxing engine: promoted in station.yaml on thin evidence; the
    promotion bench is still owed.** `config/station.yaml` ships
    `decode.engine: zxing` off an 18-crop synthetic compare ("100% vs
    78%", ~250× faster) — below PLAN_PHASE6's own promotion bar. What
    exists now (dc7c3d9): zxing-cpp lives in the optional `[zxing]`
    extra; the documented install is `pip install -e ".[dev,zxing]"`;
    CI runs a 3.11/3.13 matrix installing `.[dev,zxing]` so the deployed
    engine finally has automated coverage; `engine: zxing` without
    zxing-cpp installed is an actionable startup error; and
    station.yaml's deviations-from-default header is pinned by a test.
    Still owed (revised Phase 6): a real legacy-vs-zxing bench on
    generated standard + hard corpora, and a measured worst-case zxing
    latency bound on full-res mono ROIs — zxing has no per-call timeout
    knob, so the frame budget bounds work *between* calls, not within
    one. `engine: legacy` is the one-line trial-day revert (RUNBOOK §9).

63. **Camera identity guard — functional only after the
    CoInitialize-on-enumeration fix.** The guard (calibrate stamps
    `expected_vid_pid`/`expected_device_path`; every (re)connect
    compares; `identity.policy: warn|strict`) closes the MSMF
    pinned-index wrong-camera hole. As shipped it was inert on exactly
    the reconnect path it was built for: comtypes auto-initializes COM
    only on the importing (main) thread, so `list_devices()` on the
    watchdog consumer thread raised CO_E_NOTINITIALIZED — swallowed to
    `[]` — on every `reopen()`. dc7c3d9: `_list_windows` CoInitializeEx's
    its calling thread for the duration (balanced pairing;
    RPC_E_CHANGED_MODE means usable-as-is with NO paired uninit; moniker
    refs are dropped before the CoUninitialize), pid-absent identity
    (the `'2560:None'` composite-device case) is unverifiable-never-
    mismatch, and a strict-policy raise releases the capture it used to
    leak. `policy: warn` stays the default until the USB topology is
    final (device_path encodes port topology — re-stamp after any
    deliberate move).

64. **Exposure-EFFECT is a hard gate on every backend; readback
    reliability is its own quirk.** Bring-up flipped MSMF's
    `controls_reliable = False` (justified: garbage readback), but that
    single flag also demoted the exposure-EFFECT gate — a physically
    dead exposure control exited calibrate/selftest rc=0 with a warning
    claiming to trust the very check that had just reported NO EFFECT.
    dc7c3d9 splits the quirk: `readback_reliable` governs only whether
    numeric readback mismatches hard-fail (calibrate/selftest) or warn,
    while `verify_exposure_effect` — which measures delivered pixels,
    not readback — is a hard gate on EVERY backend. Production
    mis-exposure fails loudly again.

65. **pygrabber fps: capability selection only, reported honestly
    (~63 measured vs 72 configured).** pygrabber's `set_format` programs
    the capability's own media type and exposes no `AvgTimePerFrame`
    hook, so this backend can *select* a capability but never program an
    arbitrary rate. `choose_format` prefers, within each format tier
    (exact configured resolution first — dc7c3d9), a capability whose
    advertised framerate range contains the requested fps (pygrabber
    reports min/max swapped — smallest interval = highest fps — so the
    range is normalized); `set(CAP_PROP_FPS)` succeeds only when the
    negotiated capability is fixed-rate at the request, otherwise it
    returns False and the control report files fps as REJECTED — never
    the bring-up's fabricated verified=True echo of a mirror. Ground
    truth per station.yaml: Y8 2064×1552 datasheet max is 72 fps; ~63
    measured through the grab loop. That clears selftest's hard
    0.85×72 = 61.2 fps gate by only ~1.8 fps — a standing flaky-selftest
    watch item until the configured fps or the gate is retuned on
    hardware.

66. **Default payload gate: GS1/ISO-15434 separators allowed,
    `dm_min_payload_len: 4` — a deliberate default behavior change.**
    Bring-up added a default-on gate rejecting all C0 control bytes to
    kill a real pylibdmtx phantom (short printable garbage like `"F'm"`
    decoded from noisy crops), but it also rejected GS (0x1D) — the GS1
    FNC1/AI separator standard warehouse labels embed — finalizing real
    pallets as MISSes. dc7c3d9's default gate: allow GS/RS/FS/EOT (GS1
    separators and the ISO 15434 message envelope), reject other C0
    bytes, and additionally require `dm_min_payload_len` (default 4)
    for DATAMATRIX results only — the short-garbage phantom is
    printable, so the control-byte check alone cannot catch it. This IS
    a behavior change against the Phases 1–5 defaults (a 1–3 char DM
    payload is now dropped by default), made deliberately: the phantom
    is an observed decoder false positive that becomes wrong inventory.
    Escape hatches: `dm_min_payload_len: 1` restores the old
    accept-anything-printable behavior; a configured `payload_pattern`
    supersedes the heuristic gate entirely. Gate-rejected hits no longer
    short-circuit the symbology cascade or variant fan-out, and each
    rejected payload warns once, then counts.

67. **Motion-gate downscale is one full INTER_AREA resize; strided
    pre-slicing is banned.** Bring-up replaced the single INTER_AREA
    resize with a strided pre-slice (`image[::sy, ::sx]`) for speed; at
    common resolutions the follow-up resize became a no-op, so the
    "downscale" decimated instead of averaged and raw per-pixel sensor
    noise flooded the frame diff — phantom motion_frac 0.14–0.74 on
    static noisy scenes, segments that never closed, and a failing
    acceptance suite, in both single (default) and multi mode. dc7c3d9
    reverts to the single full INTER_AREA resize. Lesson recorded: the
    downscale's *averaging* is the motion gate's noise filter, not an
    optimization detail — any future fast path must first prove noise
    equivalence against the discriminating static-noisy-scene test that
    now pins this.

68. **Multi-track decode shares ONE frame budget.** In `tracking: multi`,
    the per-track `decode_frame` calls share a single `frame_budget_ms`
    deadline per frame — a fresh budget per track burned
    budget × `track_max_objects` (up to 8×) on the single pipeline
    thread — and the rotation resumes at the next frame when the budget
    is exhausted. Only tracks matched in the current frame are decoded:
    stale ROIs of unmatched tracks no longer burn budget during quiet
    frames. The budget stays soft per #12; with `engine: zxing` a single
    call still has no internal timeout — bounded in practice by
    measured sub-ms calls, with the formal worst-case bound owed by the
    promotion bench (#62).

69. **Station `continue_others` tolerates arm failures only — never
    accounting or escalation failures.** The availability-first policy
    (`station.on_arm_failure: continue_others`: a dead camera arm does
    not take down the healthy arm) has two hard exclusions restored by
    dc7c3d9: a business-bus drain failure is NEVER tolerated (bring-up's
    guard swallowed it whenever any arm error existed — silent
    business-event loss), and a `WatchdogEscalation` (checked including
    `__cause__`) is never a tolerable arm error — it re-raises so the
    CLI's exit-3 mapping and the supervisor restart engage, instead of
    the escalation quietly becoming permanent arm loss with no respawn.
    When multiple errors exist, the escalation is preferred as the
    raised error so the exit code survives.

70. **`frame_queue_size` must be ≥ 1 (validated).** `queue.Queue`
    treats `maxsize <= 0` as INFINITE, so a configured 0 silently turned
    DroppingQueue's bounded drop-oldest contract into an unbounded FIFO
    that never drops. Now `ge=1` — a config error, loud at startup.
    Sizing rule recorded with it: the default 64 absorbs decode bursts
    for account-for-everything; a live-view/demo wants it SMALL, because
    a FIFO backlog is queue_depth/fps of latency (the 72 fps mono at
    depth 64 shows ~1 s of lag).

71. **Injection-harness truth accounting: ts-space reconcile +
    discontinuity finalization.** Trial-rehearsal numbers are only as
    good as truth reconciliation, so dc7c3d9 makes the injected ground
    truth reconcilable: truth is recorded in the same ts space the
    frames are yielded in (bring-up recorded live-camera frame_index
    space against wall-clock ts, so every genuinely missed injected pass
    reported as "unaccounted" instead of MISSED); a watchdog
    discontinuity FINALIZES in-flight injected passes into truth
    (their frames were already delivered — discarding them understated
    misses); `launch_in` decrements only while a launch slot is open,
    preserving the idle-gap contract under `max_concurrent`; diagonal
    trajectories exit at the min of per-axis exit times; plan overrides
    go through `model_validate` (a camera config that legitimately
    omits its locked mode fails loudly instead of smuggling None into
    plan fields and crashing deep in rendering); and pass 0's plan is
    computed once and cached instead of rendered twice.

72. **Writer-level stop latch: `palletscan.stop` — sticky,
    console-free.** An unsupervised writer (`run`/`synth`/`replay`)
    watches `<data-dir>/palletscan.stop` next to its instance lock
    (`StopFileWatch`, 0.25 s poll) and drains gracefully when it
    appears. Why a file and not a signal: CTRL_BREAK needs a *shared
    console*, which services, captured-output CI shells, and
    tools/demo.py's smoke mode do not have — this channel grew out of
    the finding-1 fallout (the demo smoke test needed a graceful,
    console-free stop on Windows once the motion gate was fixed). Same
    latch semantics as the supervisor's `supervisor.stop` (#54): STICKY
    — the watcher never deletes the file, whoever created it clears it —
    and a file already present at start fires immediately, so a stale
    latch stops the next run at startup; clear it before restarting.
    The supervised path is unchanged (`supervisor.stop` → CTRL_BREAK →
    grace → kill); this latch is the unsupervised/tooling channel, and
    the watch is stopped with the `_WriterLease` teardown so in-process
    pytest runs leak no threads.

73. **Time-based motion debounce: `motion.open_s`/`quiet_s` (2026-07-02,
    the A/B prerequisite).** The open/quiet debounce knobs are frame
    COUNTS, but the two arms run at different rates (color 55 fps, mono
    ~63 measured), so `quiet_frames: 8` meant ~145 ms on the color arm
    and only ~127 ms on the mono arm - the mono closed segments ~13%
    sooner, skewing exactly the per-camera pass/miss attribution an A/B
    trial measures (HANDOFF 7.4 and REVIEW_bringup_4d95b67 strategic
    concern 5 both called the time-based fix required "before trusting
    A/B"). New optional `motion.open_s`/`motion.quiet_s` (validated > 0)
    override the frame counts, converted ONCE at MotionGate construction
    via the source's nominal fps: frames = max(1, round(seconds * fps)).
    Both single and multi tracking paths honor the converted counts.
    Defaults are None: frame-count behavior is byte-identical (pinned by
    tests). If seconds are set but the source has no nominal fps, the
    gate warns and falls back to the frame counts - a silent default-fps
    guess would skew the very parity these knobs exist to protect.
    Station values for the A/B flip: open_s 0.055 / quiet_s 0.145
    (matching today's 3/8 frames at 55 fps).

74. **Operator sessions are a reporting window, not a pipeline gate
    (2026-07-02).** The session interface (dashboard Session panel; POST
    /api/session/start with expected_count, GET /api/session, POST
    /api/session/close, /report/session/<id>.csv) never starts or stops
    scanning - it timestamps a window over the always-running pipeline
    and reconciles inside it. Counts are BUSINESS-level via the existing
    A/B window filter (compute_ab_report business passes/misses:
    cross-camera deduped, so one pallet seen by both arms counts once);
    shortfall = expected - (decoded + missed), negative when more objects
    passed than declared. Close is acknowledge-to-close (owner choice):
    a nonzero shortfall returns 409 requires_ack and the session only
    closes with an operator note, stored on the row. The close stamp is
    taken ONCE and used for both closed_utc and the persisted summary
    window so they cannot drift. Sessions live in a web-owned `sessions`
    table in the events SQLite DB (same pattern as miss_reviews/manifest),
    with BEGIN IMMEDIATE serializing concurrent starts (one open session
    at a time). When a manifest is loaded, the closeout summary stores the
    payload-level reconcile() windowed to the session (rows_in_window) -
    a whole-DB reconciliation would hide a payload scanned before the
    session began.
