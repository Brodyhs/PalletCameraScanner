# Phase 6 — Trial Readiness

## Context

Phases 1–5 are complete at HEAD: 465 tests green, ASSUMPTIONS #1–60, 7 review-deferred
cleanups left untouched. Phase 6 prepares the system for a **time-boxed hardware trial with
unknown failure modes**, and closes one **in-spec** gap. Two framings apply:

- **(a) In-spec decode work.** Spec §4 mandates DecodeEngine fallback tiers — preprocessing
  variants *and* an optional WeChat QR detector — running only while a pass remains undecoded.
  HEAD audit (below) shows preprocessing is done; **WeChat is entirely absent**. Auditing and
  closing that gap is in-spec.
- **(b) Trial instrumentation.** Recording/replay, a decode floor with promotion evidence, a
  sensitivity sweep, a last-chance decode, and an operator playbook — so a trial with unknown
  failure modes can be diagnosed and intervened on without code changes.

**Hard constraints (non-negotiable, every sub-phase):**
- Every behavior change ships behind a **config flag, default OFF**.
- **Default-config behavior stays bit-identical**; the existing **465 tests pass UNMODIFIED**.
- Promotion of any flag to default happens only by a **later explicit owner ruling**.

**Owner rulings this session (resolved before planning):**
1. **WeChat tier** → switch the base dependency `opencv-python` → `opencv-contrib-python`
   (pip superset, InfoSec-compliant). Implement WeChat as a default-off flagged tier, **vendor**
   the model files (documented origin, no runtime download), add a **real** test.
2. **Last-chance decode** → **sync-bounded in `_finalize_miss`** (Option A), not emit-then-upgrade.
3. **zxing-cpp tier** → new **`[trial]` optional extra**, import-guarded, default-off, tests skipif.

## DecodeEngine audit at HEAD (item 2's first deliverable — already performed)

| Tier | Component | Exists | Wired | Tested | Notes |
|---|---|---|---|---|---|
| 1 | pyzbar (QR/1D) on motion ROI | ✓ | ✓ | ✓ | `pipeline/decoders.py` `PyzbarDecoder` |
| 2 | pylibdmtx (Data Matrix), configurable symbology priority (QR-only/DM-only/both) | ✓ | ✓ | ✓ | ROI-only, budget-capped timeout |
| 3a | preprocessing variants: unsharp, adaptive-threshold, CLAHE, ±10° rotation | ✓ | ✓ | ✓ | `pipeline/preprocess.py` `VARIANTS`; fan-out gated by `fallback_after_frames`, off once confirmed |
| 3b | **WeChat QR detector** | ✗ | ✗ | ✗ | base dep is `opencv-python` (cv2 has **no** `wechat_qrcode`); no models vendored; no test |
| — | **zxing-cpp** candidate tier | ✗ | — | — | not in deps (deliberate stack addition for this phase) |
| — | **hard corpus** for bench | ✗ | — | — | no persistent corpus exists; bench generates synthetic on demand |

Executor (ThreadPool vs ProcessPool) is config-driven (`decode.executor`, default THREAD), with
`tools/bench_decoders.py` measuring throughput (ASSUMPTIONS #11). Early-exit on confirmed decode
and a soft per-frame budget (`decode_engine.py:194`, `:233`) are in place.

## Out-of-scope fence (do NOT touch)

- **The 7 deferred cleanups** in `REVIEW_7e4c22c.md` §"Deferred cleanups" (lines 138–152):
  dashboard-lifecycle quadruplication; per-client JPEG re-encode; unfiltered store scan;
  `_ListSink` on live runs; **`MissEvent.revision` special-casing (#5)**; reconciliation render
  duplication; `reemits == cross_camera_merges`. Also the 2 refuted items (lines 156–159) and the
  Windows-only behaviors in `ARRIVAL_CHECKLIST.md` §9. Note: deferred-cleanup #5 independently
  rules out the emit-then-upgrade last-chance design (it would need exactly that untouchable field).
- No ML training, no vendor SDKs, no cloud/GPU/Docker/brokers/external DBs, no auth.
- Nothing that alters default runtime behavior.
- **Clarification:** an internal worker thread (the recorder) is **not** a "new runtime service" —
  it is the established `EventBus` pattern, and item 1 explicitly requires reusing the
  EvidenceWriter path "off the hot path." That is in-scope.

---

## Build order (owner's priority — 4 sub-phases, each its own gate + commit)

### 6.1 — Trial Recording Mode + Replay Harness

**Goal:** a flagged mode that persists **every** motion segment (passes too, with pre/post
padding) as capped evidence-style bursts, reusing `EvidenceWriter` **off the hot path**; plus an
offline replay harness that re-scores a recorded trial under arbitrary cascade configs.

**Config** (`palletscan/config.py`): new `RecordingConfig(_StrictModel)` — `enabled: bool = False`,
`post_s: float = 2.0`, `queue_maxsize: int = 64`, and an own
`evidence: EvidenceConfig = EvidenceConfig(dir=Path("data/recordings"), max_total_mb=2000.0, …)`
(its own size cap, disjoint dir from miss evidence). Slot `recording: RecordingConfig` into
`AppConfig`. Extend `apply_overrides` to rebase `recording.evidence.dir` under `--data-dir` (inside
the existing `data_dir` block only).

**New component** `palletscan/pipeline/segment_recorder.py` — `SegmentRecorder`: owns its **own
thread**, a **bounded `queue.Queue(maxsize)`**, and its own `EvidenceWriter(cfg.evidence)`.
`submit()` is **non-blocking, drop-newest, count `dropped`** (best-effort diagnostic — must never
block the pipeline or grow unbounded); `_run()` drains → `write_burst`; `start()`/`shutdown()`
(sentinel + bounded join, **non-fatal** if undrained). Counters: `enqueued/dropped/written/write_failures`.
Mirror `events/bus.py`'s thread pattern.

**PassTracker tap** (`palletscan/pipeline/pass_tracker.py`), all behind `if self._recorder is not None:`:
- New optional ctor param `recorder: SegmentRecorder | None = None` (default None → inert).
- New `_PendingRecord` list + `_finalize_record`, mirroring `_PendingMiss`/`_finalize_miss`.
- In `_finalize_segment` (line 218): after the pass/miss decision, append a `_PendingRecord`
  (`deadline_ts = close_ts + recording.post_s`, `outcome`, `payloads`) for **both** outcomes — so
  decoded passes get post-padding **without changing pass-emit timing** (the `PassEvent` still emits
  synchronously at close).
- In `on_frame_ts` (line 179) / `flush_pending` / `flush`: drain `_pending_records` by deadline,
  same as misses. Factor the post-roll harvest+dedup (`have`/`f.ts > close_ts`, lines 296–303) into a
  shared `_harvest_post(close_ts, have)` so the miss and record paths cannot drift.
- ROI capture: store the segment motion ROI on `_SegmentState` **only when recorder present**
  (gated tap); recorded into `meta["roi"]`. Fallback: absent → replay uses full-frame ROI.

**Recorded `meta.json`** (passed as the `meta` dict to `write_burst`): `outcome` (`pass`/`miss`),
`payloads` (**ground-truth label** — decoded payloads, or `[]` for a miss), `symbologies`,
`source_id`, `segment_frames`, `segment_ts`, `frame_indices` (already written), `roi` (optional),
`schema: "recording/v1"`. Explicit honesty: in a **live** trial the only label is the original live
decode outcome; a replay "recovery" over a `[]`-labeled miss is a candidate, not confirmed truth.

**Wiring** (`palletscan/app.py` `PipelineRunner`): gated construction (like `build_sinks`);
`recorder.start()` after `bus.start()`; in `run()`'s `finally`, after `tracker.flush()` and
`bus.shutdown()`, call `recorder.shutdown()` (non-fatal). Register lazy gauges
`recorder_enqueued/dropped/written/write_failures` + queue depth.

**Replay harness** `tools/replay_bursts.py` (house style: argparse, `load_config`, `main(argv)->0/1`,
stdout + `--out` markdown). Reads a recording dir; loads JPEGs back to grayscale (`cv2.imread(…,
IMREAD_GRAYSCALE)` — inverse of the write path); for each `--config` variant builds an offline
`DecodeEngine(cfg.decode, ThreadPoolExecutor(workers))` and runs the per-frame cascade with an
advancing `PassDecodeContext` (so `fallback_after_frames`/variant fan-out engage as live). Reports
**per variant**: recovered (was-miss now decodes), regressions (was-pass now fails), per-variant/
decoder attribution (`DecodeResult.decoder`, e.g. `pyzbar+unsharp`), re-scored read rate, timing.
ROI = stored `meta["roi"]` else full-frame (labeled). Optional `--truth synth.truth.jsonl` to
annotate recoveries with `blur_modules`/`px_per_module`/… via frame-range overlap (reuse
`reconcile_truth`'s overlap logic).
**Integrity rule (load-bearing):** the harness is read-only over the recording dir, emits **no**
events, touches **no** sink/DB/JSONL, writes only its own report, and prints prominently that
recoveries are hypothetical and never fold into the live read rate.

**CPU delta** (`tools/measure_cpu.py`): add an opt-in `recording`-on scenario (one conditional line
in `_write_child_config` enabling `recording`, dir under the child's `--data-dir`). Owner re-runs
`--scenario baseline` vs `--scenario recording`; record the avg/p95 `/4-core` delta as a new
ASSUMPTIONS entry. Existing scenarios untouched.

**Tests** (new files only): `tests/test_segment_recorder.py` — disabled-is-bit-identical
(no dir, `_recorder is None`, identical events vs baseline); records pass-with-post-padding; records
miss (to the recording dir, **independently** of the miss-evidence dir); post-roll dedup (no repeat
`frame_indices`); **queue overflow drops+counts without blocking** (monkeypatch a blocking writer,
time `submit`); shutdown drains; write-failure doesn't kill the worker. `tests/test_replay_bursts.py`
— recovers a known motion-blurred burst under a strong config (attributed to `unsharp`); regression
flagged; **live numbers untouched** after replay; full-frame ROI fallback; truth cross-ref.
Optional `@pytest.mark.acceptance` end-to-end record→replay ordering sanity.

**Gate:** 465 unchanged + new tests green; default-off proven bit-identical; CPU delta in ASSUMPTIONS.

### 6.2 — Decode Cascade Audit + Floor

**Goal:** land the audit, close the WeChat gap, add the zxing-cpp tier, and produce bench evidence
on a standard **and** a new hard corpus (the bench report is the promotion evidence).

- **Audit doc:** commit the HEAD audit (table above) as ASSUMPTIONS entries (what exists, what's
  wired, what's missing).
- **Dependency switch:** `pyproject.toml` `opencv-python>=4.9` → `opencv-contrib-python>=4.9`.
  **First action: run the full 465 suite under contrib** — this is an environment change, not a
  behavior change; verify green before anything else. Add `assets/wechat/*` to
  `[tool.setuptools.package-data]`.
- **WeChat tier** (`palletscan/pipeline/decoders.py` + `decode_engine.py`): new decoder class
  **import-guarded** on `hasattr(cv2, "wechat_qrcode")`; vendor the 4 model files
  (`detect.prototxt`, `detect.caffemodel`, `sr.prototxt`, `sr.caffemodel`) under
  `palletscan/assets/wechat/` from the official `opencv_3rdparty` (`wechat_qrcode` branch), with a
  `README`/ASSUMPTIONS entry recording **origin + license** (no runtime download). Default-off flag
  in `DecodeConfig` (e.g. `wechat_enabled: bool = False`); wire as a fallback tier (alongside the
  preprocessing fan-out, only while undecoded). Real test (contrib now in base) decoding a rendered QR.
- **zxing-cpp tier:** new `[project.optional-dependencies] trial = ["zxing-cpp>=2.2"]`; decoder
  **import-guarded** on `zxingcpp`; default-off flag; test `skipif` when absent. Record in ASSUMPTIONS
  as a deliberate stack addition beyond spec §3.
- **Corpora:** `tools/make_corpus.py` (deterministic, seeded) emits a **standard** corpus (existing
  synthetic acceptance envelope) and a **hard** corpus (near/beyond the decodability envelope: high
  `blur_modules`, low `px_per_module`, low `contrast`, higher `noise_sigma`, occlusion). Generated
  deterministically (small/optional vendored sample), not large binaries committed wholesale.
- **Bench:** extend `tools/bench_decoders.py` (or new `tools/bench_cascade.py`) to measure **per-tier
  and per-combination recovery/accuracy + latency** on both corpora; emit a markdown report = the
  promotion evidence; summarize in ASSUMPTIONS.

**Gate:** new tiers default-off and import-guarded; 465 unchanged (under contrib); bench report
produced; ASSUMPTIONS updated (audit, WeChat origin, zxing-cpp addition, bench results).

### 6.3 — Sensitivity Sweep

**Goal:** a tool that sweeps the synthetic generator's difficulty axes one at a time, producing a
read-rate-vs-parameter failure envelope per cascade config.

- `tools/sweep_sensitivity.py` (house style): for each axis in {`blur_modules`, `contrast`,
  `noise_sigma`, `occlusion_frac`, `px_per_module`}, pin the other axes at easy defaults, step the
  swept axis across buckets, run **N passes per bucket** via `PipelineRunner` over `SyntheticSource`,
  measure read rate (reuse `reconcile_truth`). Emit a per-cascade-config envelope report (stdout +
  markdown). Reuse `SyntheticConfig` ranges and the per-pass truth params already recorded by the
  generator.
- **Honesty documentation:** the report and ASSUMPTIONS entry state plainly that this characterizes
  the decoder on **modeled** axes; **glare and wrap distortion are out of model** and must be covered
  by physical bench testing.

**Gate:** tool runs and produces the envelope report; results in ASSUMPTIONS; **pure tool — zero
runtime behavior change** (touches no `palletscan/` runtime path).

### 6.4 — Last-Chance Decode + RESPONSE_PLAYBOOK.md

**Goal (flagged, Option A — sync-bounded):** when a pass is about to finalize as a miss, run an
expensive matrix (sharpest-N frames × preprocessing variants × full cascade) within a configured
time budget, on the pipeline thread, **before** the miss is emitted.

- **Config:** new `LastChanceConfig(_StrictModel)` — `enabled: bool = False`,
  `time_budget_ms: float = 250.0`, `sharpest_n: int = 5`, optional `symbology_priority`/`dm_timeout_ms`
  (None → inherit `DecodeConfig`). Slot `last_chance` into `DecodeConfig`.
- **Focus metric:** **move** `focus_metric` (variance of Laplacian) out of `calibrate.py` into a
  shared low-level module (`pipeline/preprocess.py` or `pipeline/focus.py`); **re-export** from
  `calibrate` so `test_calibrate.py` is unaffected. Used to pick the sharpest-N frames.
- **Engine:** new `DecodeEngine.last_chance(frames, roi, budget_ms, …)` — reuses `_variant_task`,
  `_decode_sym`, `_results`, and the `wait(timeout, FIRST_COMPLETED)` drain (decode_engine.py
  205–229); one **global wall-clock budget** across all (frame × variant) tasks. New counters
  `last_chance_attempts/recoveries/overruns`. `decode_frame` is untouched (decode-engine tests stay green).
- **Tracker** (`pass_tracker.py`): **extract `_emit_pass(...)`** from `_finalize_segment` (the
  `_recent` dedup window + `passes_merged`/`passes_emitted` accounting + `PassEvent` construction).
  Add a flag-gated branch at the top of `_finalize_miss`: on a last-chance hit, route the recovered
  pass through `_emit_pass` (so per-camera **and** cross-camera dedup are **bit-identical** to a
  normal pass) and return without writing the miss; else fall through to today's exact code. New
  optional ctor params `decode_engine`/`last_chance_cfg` (default None → inert). ROI: full-frame first
  (simplest, budget is the safety mechanism); optional ROI retention as a follow-up knob.
- **Wiring** (`app.py`): pass the `DecodeEngine` + `cfg.decode.last_chance` into `PassTracker`;
  register `last_chance_*` gauges in `/stats.json`.
- **Integrity distinction:** a last-chance recovery runs **in-pipeline, in real time, before miss
  emit** → it **is** a legitimate live decode and **counts** toward the live read rate (flows through
  `_emit_pass`), exposed via a separate `last_chance_recoveries` gauge for auditability. This is
  categorically different from the replay harness (offline) whose recoveries **never** pad the live
  number.
- **`RESPONSE_PLAYBOOK.md`:** failure signature → flag/physical response → cost → deploy time;
  trial-day protocol (bench → informal shakeout → formal trial; **morning** defaults / **lunch**
  replay-diagnosis / **afternoon** named-config intervention measured via the A/B report's **existing
  time-window filters** `window_from`/`window_to` in `reporting/ab.py`); multi-label semantics
  (unexpected decodes are normal; **true read rate = manifest coverage**); the integrity rule that
  offline recoveries never pad the live number.
- **Tests** (additions to `test_pass_tracker.py`/`test_decode_engine.py`): last-chance recovers a
  near-miss as a single `PassEvent` (not a miss); account invariant (exactly one event); **disabled
  is bit-identical** (still a miss); recovered pass **routes through dedup** (same-payload within
  window merges — discriminating regression test: raw-`_emit` makes it double-count and fail); budget
  caps wall-clock; focus-metric selects sharpest. Each discriminating test proven to fail on
  disabled/pre-fix code.

**Gate:** default-off bit-identical; recovered pass dedups identically to a normal pass; 465 unchanged
+ new tests green; playbook committed.

---

## Cross-cutting verification (per acceptance criterion)

- **Bit-identical guarantee:** after **each** sub-phase, `pytest` runs the existing 465 tests
  **unmodified** and green with default config; each new flag has a "disabled == today" test.
- **Synthetic acceptance ≥99.5%** (`test_acceptance_synthetic.py`) stays green (default config
  unchanged).
- **6.2 env change:** full suite green under `opencv-contrib-python` is the first check of 6.2.
- **Typing:** mypy at the project's existing level; match house style (pydantic `_StrictModel`,
  small modules, no global mutable state).
- **ASSUMPTIONS deliverables:** audit + WeChat origin/license + zxing-cpp addition + bench results
  (6.2), recording CPU delta (6.1), sweep envelope with the out-of-model caveat (6.3), last-chance +
  playbook cross-reference (6.4).
- **New artifacts:** `tools/replay_bursts.py`, `tools/make_corpus.py`, `tools/bench_cascade.py` (or
  extended `bench_decoders.py`), `tools/sweep_sensitivity.py`, `RESPONSE_PLAYBOOK.md`,
  `palletscan/assets/wechat/*`.

## Critical files

- New: `palletscan/pipeline/segment_recorder.py`, `tools/replay_bursts.py`, `tools/make_corpus.py`,
  `tools/sweep_sensitivity.py`, `RESPONSE_PLAYBOOK.md`, `palletscan/assets/wechat/*`,
  `palletscan/pipeline/focus.py` (or focus moved into `preprocess.py`).
- Modified: `palletscan/config.py` (`RecordingConfig`, `LastChanceConfig`, `DecodeConfig` flags),
  `palletscan/pipeline/pass_tracker.py` (recorder tap, `_emit_pass`, last-chance branch),
  `palletscan/pipeline/decode_engine.py` (`last_chance`, WeChat/zxing wiring, counters),
  `palletscan/pipeline/decoders.py` (WeChat + zxing decoders, import-guarded),
  `palletscan/app.py` (gated wiring, lifecycle, gauges), `palletscan/calibrate.py` (focus re-export),
  `tools/measure_cpu.py` + `tools/bench_decoders.py`, `pyproject.toml` (contrib swap, `[trial]` extra,
  package-data), `ASSUMPTIONS.md`.

## Workflow note (Brody's phase gate)

This is `PLAN_PHASE6.md` material. On approval: commit the plan **verbatim** as `PLAN_PHASE6.md`,
`/clear`, then execute each sub-phase (6.1 → 6.4, in order) from the file in fresh sessions, keeping
tests green at every gate and committing per sub-phase. Dev/trial-only deps go in extras
(`[trial]` for `zxing-cpp`). Graceful degradation if hardware arrives mid-phase: the priority order
front-loads recording + the decode floor, which deliver value with or without cameras.
