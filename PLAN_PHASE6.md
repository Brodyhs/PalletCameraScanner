# Phase 6 — Trial Readiness (REV2 — owner approval pending)

## Context: why this revision exists

**This REV2 supersedes the approved 2026-06-14 plan** (committed verbatim as `12df19b`;
git history preserves it). Between that plan and today, three things invalidated its
foundation:

1. **The hardware bring-up** (`4d95b67`, ~9k lines, unreviewed) moved the PassTracker
   from a single open segment to a **dict of concurrent segments** (multi-object
   tracking), added the zxing engine / idle scan / identity guard / station policy /
   injection harness, and grew the suite well past the plan's "465 tests UNMODIFIED"
   baseline. Every pass_tracker/decode_engine line number REV1 cites is stale.
2. **The 72-finding adversarial review** (`REVIEW_bringup_4d95b67.md`) confirmed 13
   runtime correctness bugs plus tooling/accounting defects, and its §"Phase 6 —
   revise, don't execute as written" (lines 346–368) mandates this exact reorder:
   recording/replay first and rederived; last-chance + playbook pulled forward;
   old 6.2 reshaped into zxing **promotion evidence**; the sensitivity sweep folded
   into `inject_grid`; **WeChat/opencv-contrib demoted to contingency backlog**.
3. **The fix commit** (`dc7c3d9`, HEAD: 47 files, +3677/−457, each fix with a
   discriminating test) repaired those findings. This plan is derived against
   `dc7c3d9`, with fresh anchors cited below.

**Rebaselined suite (measured on the factory box at `dc7c3d9`):** 644 tests collected.
Fast selection (`-m "not acceptance and not soak_short"`) = 640 run → **636 pass,
1 skip, 3 documented pre-existing Windows-env failures**
(`test_http_sink.py::test_size_cap_prunes_oldest_and_counts`,
`test_instance_lock.py::test_hard_killed_holder_leaves_no_stale_lock`,
`test_supervisor.py::test_default_spawn_injects_pid_env_without_mutating_environ`);
acceptance 3/3 green (including the replay acceptance that failed on `4d95b67`).
Those 3 env failures are the accepted baseline, **not headroom** — a sub-phase gate
tolerates exactly those and nothing else.

Two framings still apply: **(a)** close the in-spec decode-floor question with real
evidence (spec §4's optional extra tier is now *contingent*, not scheduled), and
**(b)** trial instrumentation — a time-boxed hardware trial with unknown failure
modes must be diagnosable and intervenable without code changes. What is NEW since
REV1: the injection harness family exists but **explicitly cannot certify the real
lens** (its own docstrings say so — composited codes model the optics), and
`OPTICS_SPEC.md` §2 shows the stock 3 mm lenses fall below the 5 px/module floor
beyond ~3–4 ft — i.e. we now *predict* optics-driven failures the synthetic model
will not show. That makes **recording real segments (6.1) the single most valuable
item in the phase**.

**Hard constraints (non-negotiable, every sub-phase):**
- Every behavior change ships behind a **config flag, default OFF**.
- **Default-config behavior stays bit-identical**; the rebaselined suite above passes
  **UNMODIFIED** (no assertion ever weakened; no new failure beyond the 3 documented).
- Promotion of any flag to default happens only by a **later explicit owner ruling**.
  (This includes `decode.engine`: the code default stays `legacy`; `station.yaml`'s
  `engine: zxing` remains a *documented deviation* pinned by
  `test_config.py::test_station_yaml_matches_defaults_except_documented_deviations`.)
- Per-sub-phase gate + commit, executed from this file in fresh sessions.

**Owner rulings this revision needs (approval pending):**
- **D1 — approve the reorder/rescope.** REV1's ruling 1 (WeChat via
  opencv-contrib swap) is **rescinded to the contingency backlog**; ruling 2
  (last-chance = Option A, sync-bounded in `_finalize_miss`) **survives**; ruling 3
  (zxing as import-guarded optional extra) already landed via the bring-up + dc7c3d9
  (CI now installs `.[dev,zxing]` on 3.11 + 3.13).
- **D2 — last-chance concurrency bound** (multi mode can finalize several misses on
  one clock advance): proposed = one full `time_budget_ms` per miss, overruns counted
  (`last_chance_overruns`), worst case documented (see 6.2). Alternative: a shared
  per-clock-advance allowance.
- **D3 — zxing latency guard vs ASSUMPTIONS-only**, decided by 6.3's measurement (see
  6.3; the guard, if built, is itself default-off).
- **D4 — optional `inject_grid --offline` synthetic mode** (camera-free envelopes on
  the dev box) — cheap, skippable.
- **D5 — trial recording deviation**: whether `station.yaml` sets
  `recording.enabled: true` for the trial window (added to the pinned documented-
  deviations list), or ops flips it on trial morning.

Out of this plan's scope but sequenced around it (REVIEW §"Recommended sequence"):
merging `bringup-2026-06` to main with the ASSUMPTIONS/RUNBOOK backfill (the ledger
still ends at **#60**; Phase 6 entries continue from wherever it stands at execution),
fixing OPTICS_SPEC's three finding-14 contradictions **before buying** lenses/lights,
and the hardware-validation pass (time-based debounce → A/B, ARRIVAL_CHECKLIST §6/§7/§9).
6.2's playbook *references* the purchase decision; it does not own it.

## Verified facts this plan builds on (checked at `dc7c3d9`; supersede REV1's anchors)

- **PassTracker is multi-segment**: `_open: dict[str, _SegmentState]`
  (`pass_tracker.py:123`), per-candidate `_SegmentState` with its own decode context +
  `_FrameReservoir` (`:68–76`); `on_frame` feeds EVERY open segment's reservoir
  (`:203–211`); `ctx_for(candidate_id)` (`:177`) and `has_open` (`:172`) are the
  multi-mode routing API. `_finalize_segment` (`:252`) either appends a `_PendingMiss`
  (`:79–88`, deadline `close_ts + buffer.post_s`) or emits PassEvents inline in the
  confirmed-payload loop (`:277–314`: `_recent` dedup merge, window refresh on emit,
  PassEvent construction, `passes_emitted`). Pending misses drain by deadline in
  `on_frame_ts` (`:213`), `flush_pending` (`:218`), `flush` (`:240`).
  `_finalize_miss` (`:323`) harvests the post-roll with the `have` frame-index dedup +
  `f.ts > close_ts` filter (`:330–337`) — the seam 6.1 factors out and 6.2 reuses.
- **MotionGate multi mode**: a track's `track_id` IS the segment `candidate_id`
  (`motion_gate.py:286–300`); only tracks updated this frame (`missed == 0`) surface
  as `MotionTrack`s with **per-track full-res ROIs**; candidate ids embed
  `source_id + run_token` (`:80–89`) so burst directories never collide across arms
  or restarts.
- **app.py**: multi-track decode loop shares ONE frame budget with rotation
  (`app.py:446–472`, `track.roi` per track); single-mode branch at `:473–477`
  (`result.roi`); discontinuity flush at `:426–435`; gauges registered lazily via
  `metrics.register_gauges` (`:329–345`); `run()` starts the bus at `:586` and its
  `finally` shuts down idle executor → decode executor → bus (`:608–614`).
  StationRunner builds **one PipelineRunner per camera** (`station.py:172–174`) and
  each arm already shares the same `evidence.dir` safely (candidate ids disambiguate;
  pruning is race-tolerant).
- **DecodeEngine**: `PassDecodeContext` (`decode_engine.py:47`), counters dataclass
  (`:73–80`), variant tasks `_variant_task` / `_variant_task_zxing` (`:136`/`:112` —
  zxing "has no per-call timeout knob"), `_results` payload gate application (`:232`),
  the zxing inline path (`:284–295`) which **never checks the frame deadline**, the
  legacy per-symbology loop (`:296–313`), and the step-3 fan-out with the
  `wait(FIRST_COMPLETED)` budget drain (`:315–357`). These are the seams 6.2's
  `last_chance` reuses and 6.3's guard (if D3 = guard) would slot into.
- **Flag surface that exists NOW (all default-off/dormant, playbook material)**:
  `motion.tracking: single|multi` (`config.py:204`), `motion.idle_scan_s: 0.0`
  (`:195`), `decode.engine: legacy|zxing` (`:281`), `decode.payload_pattern` (`:294`),
  `decode.dm_min_payload_len` (`:300`), `cameras[].identity.policy: strict|warn|off`
  (`:571`), `station.on_arm_failure: stop_all|continue_others` (`:718`), and the
  writer-level stop latch `palletscan.stop` (`cli.py:430–445`,
  `reliability/supervisor.py:268` `StopFileWatch` — sticky, console-free drain).
- **zxing-cpp is installed in this venv** (`import zxingcpp` verified) and CI installs
  `.[dev,zxing]` — 6.3 specifies actually *running* the bench, not just writing it.
- **The 18-crop compare is the entire current zxing evidence**:
  `tools/bench_decoders.py:42–66` `_make_hard_workload` builds 3 ppm × 3 blur × 2
  symbologies = 18 crops; `station.yaml:130–136` cites its "100% vs 78%" as the
  reason the station ships zxing. REVIEW strategic concern 2: far below the
  promotion bar, and the zxing path abandons the budget machinery.
- **inject_grid already is the single-axis envelope tool**: `tools/inject_grid.py`
  pins every axis to a decodable baseline, sweeps ONE axis, reconciles truth through
  the REAL pipeline, opens the camera once, and its docstring already carries the
  honesty caveat ("Pair with record-then-replay for the lens").
- **OPTICS_SPEC.md** (finding 14 corrections still pending): §2 stock 3 mm lens
  < 5 px/module beyond ~3–4 ft; §4/§7 the 8 mm M12 buy; §8 lighting bands where the
  2,000–4,000 lux "buy" row contradicts the ~7,800 lux the 1 ms motion-freeze row
  requires; §9 + `station.yaml:96–102` exposure operating points (cam-color −6;
  cam-mono −8 ≈ 3.9 ms unlit bench, −9/−10 once scan-zone lighting lands).
- `EvidenceWriter.write_burst(candidate_id, frames, meta)` (`events/evidence.py:48`)
  never raises on storage failure; `EvidenceConfig` (`config.py:314`) carries
  dir/stride/quality/size/age caps; `apply_overrides(data_dir=…)` (`:863–883`) is the
  established path-rebase seam. `focus_metric` (variance of Laplacian) lives at
  `calibrate.py:79`. `tools/measure_cpu.py` has `--scenario baseline|station|both`
  (`:289`) and `_write_child_config` (`:130`). The A/B report already has
  time-window filters `window_from`/`window_to` (`reporting/ab.py:83–122`).

## Out-of-scope fence (do NOT touch)

- **WeChat / opencv-contrib-python / vendored SR models** — contingency backlog only
  (section below). No base-dependency swap in this phase.
- The REVIEW_7e4c22c deferred cleanups and the 18 unverified low-priority cleanup
  candidates from REVIEW_bringup_4d95b67 (bench.py duplication, tools re-wiring,
  live_decode.py) — untouched.
- No ML training, vendor SDKs, cloud/GPU/Docker/brokers/external DBs, auth; nothing
  that alters default runtime behavior; no new always-on runtime services (the
  recorder worker is the established EventBus thread pattern, in-scope as before).
- **No physical camera is required to execute this plan** — every runtime change is
  verified via fakes/synthetic sources; live recording shakeout is a trial-day /
  ARRIVAL_CHECKLIST step.

---

## Build order (4 sub-phases, each its own gate + commit)

### 6.1 — Trial Recording Mode + Replay Harness (REDERIVED; highest value)

**Goal:** a flagged mode that persists **every** motion segment — passes too, with
post-roll padding — as capped evidence-style bursts off the hot path, plus an offline
replay harness that re-scores a recorded trial under arbitrary cascade configs.
Recording real segments is the only ground truth the injection harness cannot
provide, and OPTICS_SPEC predicts optics-driven failures (undersampled modules,
glare, wrap distortion) the synthetic compositor will not show.

**Config** (`palletscan/config.py`): `RecordingConfig(_StrictModel)` — `enabled: bool
= False`, `post_s: float = 2.0`, `queue_maxsize: int = 64`, and its own
`evidence: EvidenceConfig` (dir `data/recordings`, `max_total_mb=2000.0`, own caps,
**disjoint from miss evidence**). Slot `recording: RecordingConfig` into `AppConfig`;
extend `apply_overrides` (`config.py:882` block) to rebase `recording.evidence.dir`
under `--data-dir`. Add the keys to `config/default.yaml` (+ `station.yaml` only
under ruling D5).

**New component** `palletscan/pipeline/segment_recorder.py` — `SegmentRecorder`: own
thread, bounded `queue.Queue(maxsize)`, own `EvidenceWriter(cfg.evidence)`.
`submit()` non-blocking, drop-newest, counted (`dropped`); `_run()` drains →
`write_burst`; `start()`/`shutdown()` (sentinel + bounded join, non-fatal if
undrained). Counters `enqueued/dropped/written/write_failures`. Mirrors
`events/bus.py`'s thread pattern. (Unchanged from REV1 — this component never
touched tracker internals.)

**PassTracker tap — rederived against the multi-segment tracker**
(`palletscan/pipeline/pass_tracker.py`), all behind `if self._recorder is not None:`:
- Optional ctor param `recorder: SegmentRecorder | None = None` (default None → inert)
  + `record_post_s: float` (from `recording.post_s`).
- New `_PendingRecord` dataclass mirroring `_PendingMiss` (`:79–88`) plus `outcome`
  (`"pass"`/`"miss"`), `payloads`, `symbologies`, `roi: Roi | None`. **Per-candidate
  pending records**: `_finalize_segment` (`:252`) appends one `_PendingRecord` for its
  segment on **both** outcome branches — the miss branch alongside the `_PendingMiss`
  append (`:264–275`), the pass branch after the confirmed-payload loop — so N
  concurrent segments produce N independent records. Pass-emit timing is untouched:
  the PassEvent still emits synchronously at close; only the *record* waits for its
  post-roll (`deadline_ts = close_ts + record_post_s`).
- `_pending_records` drains by deadline in exactly the clock hooks that drain misses:
  `on_frame_ts` (`:213`), `flush_pending` (`:218`) — which also covers the
  discontinuity flush at `app.py:426–435` — and `flush` (`:240`).
- **Factor the post-roll harvest** (`:330–337`: `have` index set, `f.ts > close_ts`,
  buffer extract) into a shared `_harvest_post(close_ts, frames) -> list[Frame]`
  used by BOTH `_finalize_miss` and `_finalize_record`, so the two paths cannot
  drift (and 6.2 reuses it). Neither path may mutate the reservoir list it shares
  with the other (`frames + post` builds a new list, as today at `:338`).
- **Per-track ROI**: `_SegmentState` (`:68`) gains `last_roi: Roi | None`, updated
  via a new gated `note_roi(candidate_id, roi)` no-op-unless-recorder method called
  from app.py's two decode sites — the multi-mode per-track loop (`app.py:464`,
  `track.roi`) and the single-mode branch (`app.py:476`, `result.roi`). Recorded
  into `meta["roi"]` in full-res coordinates. Absent (never-decode-eligible segment)
  → replay uses full-frame, labeled.
- Overlap honesty: in multi mode, concurrent segments' reservoirs each sampled every
  frame while open (`:209–210`), so overlapping bursts duplicate frames **across**
  recordings by design — the recording size cap accounts for it; `frame_indices` in
  each burst's meta stay per-segment exact.

**Recorded `meta.json`** (the `meta` dict to `write_burst`): `schema: "recording/v1"`,
`outcome`, `payloads` (**ground-truth label**: decoded payloads, `[]` for a miss),
`symbologies`, `source_id`, `candidate_id`, `tracking` (`single`/`multi`), `engine`
(decode engine at record time), `segment_frames`, `segment_ts`, optional `roi`.
Explicit honesty note in the writer docstring: in a live trial the only label is the
live decode outcome; a replay "recovery" over a `[]`-labeled miss is a candidate,
never confirmed truth.

**Wiring** (`palletscan/app.py`): gated construction in `PipelineRunner.__init__`
(pattern of the idle-scan block, `:232–270`); `recorder.start()` next to
`self._bus.start()` (`:586`); `recorder.shutdown()` in `run()`'s `finally` after
`self._bus.shutdown()` (`:614`), non-fatal. Lazy gauges
`recorder_enqueued/dropped/written/write_failures` + queue depth via
`register_gauges`/`register_queue` (`:329–357`). A/B: each arm's PipelineRunner gets
its own recorder into the shared recording dir — same established pattern as the
per-arm `EvidenceWriter(cfg.evidence)` (`:291`), collision-safe via candidate-id
namespacing (`motion_gate.py:80–89`).

**Replay harness** `tools/replay_bursts.py` (house style: argparse, `load_config`,
`main(argv) -> 0/1`, stdout + `--out` markdown). Reads a recording dir; loads JPEGs
back grayscale (`cv2.imread(…, IMREAD_GRAYSCALE)` — inverse of the write path); for
each `--config` variant builds an offline `DecodeEngine(cfg.decode,
ThreadPoolExecutor(workers))` and replays the per-frame cascade with an advancing
`PassDecodeContext` (so `fallback_after_frames`/fan-out engage as live). ROI = stored
`meta["roi"]` else full-frame (labeled). Reports per variant: recovered (was-miss now
decodes), regressions (was-pass now fails), per-decoder/variant attribution
(`DecodeResult.decoder`), re-scored read rate, timing. Because zxing-cpp is
installed, **legacy-vs-zxing replay over real recorded frames is a first-class
variant pair** — this is the evidence stream that eventually supersedes 6.3's
synthetic corpora. Optional `--truth synth.truth.jsonl` cross-annotation via
frame-range overlap (reuse `reconcile_truth`'s overlap logic, `app.py:137–170`).
**Integrity rule (load-bearing):** the harness is read-only over the recording dir,
emits no events, touches no sink/DB/JSONL, writes only its own report, and prints
prominently that recoveries are hypothetical and never fold into the live read rate.

**CPU delta** (`tools/measure_cpu.py`): opt-in `recording` scenario — one conditional
in `_write_child_config` (`:130`) enabling `recording` under the child's
`--data-dir`. Run baseline vs recording; record the avg/p95 `/4-core` delta as an
ASSUMPTIONS entry. Existing scenarios untouched.

**Tests** (new files only): `tests/test_segment_recorder.py` —
disabled-is-bit-identical (recorder None; identical event stream vs baseline; no
recording dir created); pass recorded WITH post-roll padding and unchanged
PassEvent timing; miss recorded to the recording dir independently of the miss
evidence dir; **two concurrent multi-mode segments each yield their own record with
their own ROI, payload label, and frame set** (the discriminating rederivation test);
post-roll dedup via the shared `_harvest_post` (no repeated `frame_indices` in either
the miss burst or the record burst); discontinuity flush finalizes pending records;
queue overflow drops + counts without blocking (monkeypatched blocking writer, timed
`submit`); shutdown drains; write failure doesn't kill the worker.
`tests/test_replay_bursts.py` — recovers a known motion-blurred burst under a strong
config with decoder attribution; regression flagged; live JSONL/DB untouched after
replay; full-frame ROI fallback; legacy-vs-zxing variant-pair smoke; truth cross-ref.
Optional `@pytest.mark.acceptance` record→replay end-to-end ordering sanity.

**Gate:** rebaselined suite green UNMODIFIED (636+3-env-failures fast profile,
acceptance 3/3) + new tests; default-off proven bit-identical; CPU delta in
ASSUMPTIONS.

### 6.2 — Last-Chance Decode + RESPONSE_PLAYBOOK.md (pulled forward; Option A survives)

**Goal (flagged, sync-bounded per REV1 ruling 2):** when a pending miss finalizes,
run an expensive matrix (sharpest-N frames × preprocessing variants × cascade)
within a configured time budget, on the pipeline thread, **before** the MissEvent is
emitted — plus the operator playbook the trial cannot run without. Pulled ahead of
the zxing bench because it gets *cheaper* with a sub-ms zxing and the playbook must
document the bring-up's new flag surface either way.

- **Config:** `LastChanceConfig(_StrictModel)` — `enabled: bool = False`,
  `time_budget_ms: float = 250.0`, `sharpest_n: int = 5`, optional
  `symbology_priority`/`dm_timeout_ms` (None → inherit). Slot `last_chance` into
  `DecodeConfig`.
- **Focus metric:** move `focus_metric` (`calibrate.py:79`) into
  `pipeline/preprocess.py` (or a tiny `pipeline/focus.py`); re-export from
  `calibrate` so `test_calibrate.py` is unaffected. Selects the sharpest-N frames.
- **Engine:** `DecodeEngine.last_chance(frames, roi, budget_ms, …)` — reuses the
  existing seams: `task_fn` selection mirroring `decode_frame` (`decode_engine.py:328`
  — zxing engine uses `_variant_task_zxing`), `_results` (`:232`), and the
  `wait(timeout, FIRST_COMPLETED)` drain (`:333–353`); ONE global wall-clock budget
  across all (frame × variant) tasks. The budget stays *soft* exactly as documented
  for `decode_frame` (in-flight C calls can't be cancelled) — with the zxing engine
  the per-task bound is whatever 6.3 measures, which is why 6.3's latency number is
  recorded before any promotion ruling. New counters
  `last_chance_attempts/recoveries/overruns` in `_Counters` (`:73`). `decode_frame`
  itself is untouched.
- **Tracker** (`pass_tracker.py`): **extract `_emit_pass(...)`** from
  `_finalize_segment`'s confirmed-payload loop (`:277–314`: `_recent` dedup merge +
  `passes_merged`/`passes_emitted` accounting + window-refresh-on-emit + PassEvent
  construction) so a recovered pass dedups **bit-identically** to a normal pass —
  per-camera and cross-camera. Flag-gated branch at the top of `_finalize_miss`
  (`:323`): assemble `miss.frames + _harvest_post(...)` (the 6.1 shared helper),
  pick sharpest-N, run `last_chance` over the segment's `roi` (the 6.1
  `_PendingMiss.roi` when present, else full-frame; the budget is the safety
  mechanism); on a hit, route through `_emit_pass` and return without writing the
  miss; else fall through to today's exact code. Optional ctor params
  `decode_engine`/`last_chance_cfg` (default None → inert).
- **Multi-mode bound (ruling D2):** `on_frame_ts` can finalize several pending
  misses on one clock advance; each gets its own budget → worst case
  `track_max_objects × time_budget_ms` (default 8 × 250 ms = 2 s) on the pipeline
  thread while the live frame queue absorbs via drop-oldest. Proposed: accept +
  count (`last_chance_overruns`, gauge), document the bound in the playbook; misses
  are rare by definition and the budget is operator-tunable to the trial's fps.
- **Wiring** (`app.py`): pass engine + `cfg.decode.last_chance` into `PassTracker`;
  `last_chance_*` gauges (`:329` pattern).
- **Integrity distinction:** a last-chance recovery runs in-pipeline, in real time,
  before miss emit → it IS a live decode and counts toward the live read rate
  (through `_emit_pass`), separately auditable via `last_chance_recoveries`. This is
  categorically different from 6.1's replay recoveries, which never touch the live
  number.
- **`RESPONSE_PLAYBOOK.md`** — failure signature → response → cost → deploy time,
  now covering the FULL post-bring-up surface:
  - **Config interventions** (each with revert + observable gauge):
    `decode.engine: legacy` revert (incl. the `[zxing]` install caveat and
    `station.yaml:130–136` notes); `motion.tracking: multi` for co-located pallets
    (cost: per-frame association + shared budget rotation, `app.py:446–472`);
    `motion.idle_scan_s > 0` for stopped pallets/static codes (additive `idle_reads`
    only); `decode.payload_pattern` / `dm_min_payload_len` against phantom decodes
    (`spurious_rejected`); `cameras[].identity.policy: strict` once the USB topology
    is final (`station.yaml:77–80` re-stamp caveat); `station.on_arm_failure:
    continue_others` for availability-first A/B; `recording.enabled` for lunch-break
    diagnosis; `decode.last_chance.enabled` (this sub-phase).
  - **Ops channels:** the `palletscan.stop` latch (sticky, console-free drain —
    `cli.py:430–445`), supervisor restart counting via `restarts.jsonl`, dashboard
    `/stats.json` gauges to watch (`budget_overruns`, `zxing_calls`,
    `spurious_rejected`, `idle_reads`, `connect_mismatches`, `recorder_*`,
    `last_chance_*`).
  - **Physical interventions from OPTICS_SPEC:** the 8 mm M12 lens swap (§4/§7 —
    stock 3 mm fails the 5 px/module floor beyond ~3–4 ft; refocus + lock ~5 ft);
    lighting to §8's targets **with the finding-14 caveat spelled out**: freezing
    5–10 mph needs the ~1 ms row (~7,800 lux with margin), not the 2,000–4,000 lux
    band — the playbook records the owner's purchase ruling (buy to the 1 ms row or
    explicitly de-scope 5–10 mph) and the fallback if lighting is short on trial
    day (slow the lane / off-axis re-aim); exposure operating points from
    `station.yaml`: cam-color −6 @ UYVY55, cam-mono −8 (≈3.9 ms, unlit bench) → −9/−10
    (2 ms/1 ms) once scan-zone lighting lands (`station.yaml:96–102`), gain to the
    bench [80,155] brightness band.
  - **Trial-day protocol:** bench → informal shakeout → formal trial; morning
    defaults / lunch replay-diagnosis (6.1 harness) / afternoon named-config
    intervention measured via the A/B report's existing `window_from`/`window_to`
    filters (`reporting/ab.py:83–122`); multi-label semantics (true read rate =
    manifest coverage); the integrity rule that offline recoveries never pad the
    live number.
- **Tests** (additions to `test_pass_tracker.py`/`test_decode_engine.py`):
  last-chance recovers a near-miss as a single PassEvent (exactly one event —
  account invariant); disabled is bit-identical (still a miss); recovered pass
  routes through dedup (same-payload-within-window merges — discriminating: a raw
  `_emit` double-counts and fails); budget caps wall-clock; sharpest-N selection;
  **multi-mode: two pending misses finalizing on one clock advance each get their
  own attempt with overrun accounting** (the D2 bound test); ROI-from-segment vs
  full-frame fallback. Each discriminating test proven to fail on disabled/pre-fix
  code.

**Gate:** default-off bit-identical; recovered pass dedups identically to a normal
pass; rebaselined suite green UNMODIFIED + new tests; `RESPONSE_PLAYBOOK.md`
committed with the flag table complete.

### 6.3 — zxing Promotion Evidence (reshaped old 6.2)

**Goal:** replace the 18-crop compare with real promotion evidence for the engine
the station already deploys, and bound its worst-case latency. zxing-cpp is
installed in this venv and in CI — this sub-phase **runs** the benches and commits
the numbers; it adds no new decode tier.

- **Corpora** — `tools/make_corpus.py` (deterministic, seeded; reuse
  `palletscan/sources/render.py` render_qr/render_datamatrix/motion_blur and the
  synthetic difficulty axes): a **standard** corpus sampling the acceptance envelope
  (`px_per_module` 3–6, contrast 0.45–1.0, noise σ 2–8, occlusion ≤ 0.15 — the
  `station.yaml` synthetic block) and a **hard** corpus at/beyond the envelope (ppm
  2–3, blur 1–2 modules, low contrast, high noise, occlusion), plus full-res
  canvases at both sensor geometries (2064×1552 mono, 1920×1200 color). Hundreds of
  items per corpus, generated on demand with a truth JSONL (payload, symbology, axis
  params); no large binaries committed.
- **Bench** — `tools/bench_cascade.py` (or extend `bench_decoders.py`; house style):
  legacy cascade (inline tiers + variant fan-out driven through `decode_frame` with
  a realistic advancing `PassDecodeContext`, so `fallback_after_frames` engages as
  live) vs the zxing engine, on both corpora; per-tier and per-combination
  **recovery, accuracy (payload equality — misdecodes counted separately, they are
  worse than misses), and latency p50/p95/max** (the fixed interpolated `_p95`,
  `bench_decoders.py:31`); markdown report = the promotion evidence; summary in
  ASSUMPTIONS. The report explicitly supersedes the 18-crop claim cited in
  `station.yaml:130–136`.
- **Worst-case zxing latency bound** — measure `read_barcodes` on full-res
  **2064×1552 mono ROIs** (and 1920×1200 color): no-code frames at gain-10 noise
  levels, dense high-frequency texture, many-candidate scenes, and the worst case
  found; report max over repeated runs. This bounds three real paths that can hand
  zxing huge crops with no timeout: the inline branch (`decode_engine.py:284–295`,
  no deadline check), the idle scan's full-frame decode, and 6.2's last-chance
  full-frame fallback. **Ruling D3 consumes the number:** either (a) a
  budget/downscale guard in the zxing branch (skip-or-INTER_AREA-cap crops above a
  pixel-area threshold with ROI map-back — itself default-off, discriminating test,
  no default-path change since `engine` defaults `legacy`), or (b) a recorded
  ASSUMPTIONS entry stating the measured bound and why no guard is needed (e.g.
  worst case ≪ `frame_budget_ms`).
- **ASSUMPTIONS entries:** bench method + results; the measured latency bound + D3
  outcome; the promotion posture (code default stays `legacy`; any change to that
  default is a later explicit owner ruling on THIS evidence, and 6.1's real-frame
  replay evidence supersedes synthetic corpora as recordings accumulate).
- **Tests:** corpus determinism (same seed → same hashes); bench summary math; the
  guard's discriminating test if D3 = (a).

**Gate:** both reports committed; corpora reproducible; rebaselined suite green
UNMODIFIED (zxing tests run, not skipped, in this venv and CI); ASSUMPTIONS updated.

### 6.4 — Sensitivity Sweep folded into `inject_grid` (documentation, no new tool)

Old 6.3 is subsumed: `tools/inject_grid.py` already produces single-axis
read-rate envelopes through the REAL pipeline with the real camera in the loop, one
axis at a time from a pinned decodable baseline — strictly stronger than the
proposed offline sweep. Deliverables:

- **Documentation fold:** extend `inject_grid.py`'s docstring + RUNBOOK cross-ref to
  declare it THE sensitivity-envelope tool, mapping its axes to the old sweep's
  (`px_per_module`, speed/blur, contrast, noise, occlusion, exposure) and repeating
  the honesty caveat verbatim: composited codes measure decode + pipeline under a
  model; **glare and wrap distortion are out of model** and belong to physical bench
  testing + 6.1 recordings.
- **ASSUMPTIONS entry** recording the fold (old 6.3 not built, superseded by
  inject_grid + recordings) and the caveat.
- **Optional (ruling D4):** a `--offline` flag substituting a `SyntheticSource` for
  the camera so envelopes run camera-free on the dev box — cheap variant only if the
  owner wants it; zero runtime behavior change either way.

**Gate:** docs + ASSUMPTIONS committed; pure tool/doc change — zero `palletscan/`
runtime paths touched; suite green.

---

## Contingency backlog (explicitly NOT scheduled)

**WeChat superresolution QR tier (opencv-contrib swap + vendored models).** Demoted
from REV1's 6.2 per the review: spending a base-dependency-swap risk
(`opencv-python` → `opencv-contrib-python` across the whole runtime) before owning a
real miss corpus is backwards. **Entry criteria (all required):** (1) 6.1 recordings
from a real trial contain misses; (2) replay shows zxing failing on crops where a
superresolution detector plausibly helps — visible but undersampled QR near/below
the 5 px/module floor, i.e. the regime the 8 mm lens buy is supposed to fix first;
(3) an explicit owner ruling. If triggered, REV1's implementation sketch survives
as written: contrib swap verified by a full green suite first, import-guarded
decoder on `hasattr(cv2, "wechat_qrcode")`, 4 model files vendored from official
`opencv_3rdparty` with origin + license recorded, default-off flag, real decode
test. Until then: not in deps, not in code, not in CI.

---

## Cross-cutting verification (per acceptance criterion)

- **Bit-identical guarantee:** after EACH sub-phase, the rebaselined suite runs
  UNMODIFIED and green — fast profile 636 pass / 1 skip with exactly the 3
  documented Windows-env failures and no new ones; acceptance 3/3
  (`test_acceptance_synthetic` ≥ 99.5% + replay + demo smoke). Every new flag has a
  "disabled == today" test. No existing assertion weakened, ever.
- **Multi-mode parity:** 6.1/6.2 tracker changes carry discriminating tests in BOTH
  tracking modes (the rederivation exists because REV1 assumed single-segment).
- **Typing/style:** mypy at the project's level; `_StrictModel` config, small
  modules, no global mutable state; tools follow the argparse/`main(argv)->0/1`
  house pattern.
- **ASSUMPTIONS deliverables** (numbering continues from the ledger head at
  execution; #61+ as of this writing — the bring-up backfill entries belong to the
  merge-to-main step, not this plan): recording CPU delta + recording/v1 schema
  (6.1); last-chance semantics + playbook cross-ref + D2 bound (6.2); bench
  method/results + zxing latency bound + D3 outcome + promotion posture (6.3);
  inject_grid fold + out-of-model caveat (6.4).
- **New artifacts:** `palletscan/pipeline/segment_recorder.py`,
  `tools/replay_bursts.py`, `tools/make_corpus.py`, `tools/bench_cascade.py` (or
  extended `bench_decoders.py`), `RESPONSE_PLAYBOOK.md`, bench/latency reports.

## Critical files

- New: `palletscan/pipeline/segment_recorder.py`, `tools/replay_bursts.py`,
  `tools/make_corpus.py`, `tools/bench_cascade.py`, `RESPONSE_PLAYBOOK.md`,
  `palletscan/pipeline/focus.py` (or focus moved into `preprocess.py`).
- Modified: `palletscan/config.py` (`RecordingConfig`, `LastChanceConfig`,
  `apply_overrides` rebase), `palletscan/pipeline/pass_tracker.py` (recorder tap,
  `_PendingRecord`, `_harvest_post`, `note_roi`, `_emit_pass`, last-chance branch),
  `palletscan/pipeline/decode_engine.py` (`last_chance`, counters, D3 guard if
  ruled), `palletscan/app.py` (gated wiring, `note_roi` call sites, lifecycle,
  gauges), `palletscan/calibrate.py` (focus re-export), `tools/measure_cpu.py`
  (recording scenario), `tools/inject_grid.py` (docs), `config/default.yaml`,
  `ASSUMPTIONS.md`, `RUNBOOK.md` (playbook + recording cross-refs); `config/
  station.yaml` only under rulings D5/D3.

## Workflow note (Brody's phase gate)

This is `PLAN_PHASE6.md` REV2 material. On approval of rulings D1–D5: commit this
file verbatim, `/clear`, then execute 6.1 → 6.2 → 6.3 → 6.4 from the file in fresh
sessions, tests green at every gate, one commit per sub-phase. 6.1 first is
deliberate: it is the item the review calls "the single most valuable" and the only
one whose value compounds with every day of trial time — a trial run without it
produces no re-analyzable corpus of real misses. Graceful degradation if the lens/
lighting purchase slips: 6.1–6.3 deliver value with or without the 8 mm glass;
the playbook (6.2) documents operating both the current 3 mm reality and the
post-swap configuration.
