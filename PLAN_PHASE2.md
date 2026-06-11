# Phase 2 Plan — Replay, Metrics, HTTP Sink, Soak

Status: **approved 2026-06-11**. Execute in order (§4), starting with the
metrics module. Phase 1 is complete and green (69 tests incl. the 400-pass
acceptance gate); recorded decisions live in ASSUMPTIONS.md.

## 0. Approved decisions (owner rulings)

1. **One-event-per-POST: approved.** ~7 events/min average makes batching
   premature for a TBD endpoint; keep batching as a future config knob and
   document the at-least-once + `event_id` dedupe contract in the README.
2. **psutil: approved, in the `[dev]` extras only** — only `tools/soak.py`
   needs it, and the runtime dependency list stays minimal for security
   review.
3. **Both refactors approved** (source factory, lazy pass planning).
   ASSUMPTIONS.md amendments approved.

## 1. Where each piece plugs into Phase 1

| Phase 2 piece | Existing seam it reuses |
|---|---|
| VideoFileSource | `FrameSource` ABC (`source_id`, `frames()`, `close()`, `nominal_fps`, `live`); source-clock `Frame.ts` convention (ASSUMPTIONS #15) |
| Metrics | Counters that already exist: `DroppingQueue.dropped`, `EventBus.events_handled/sink_errors`, `PassTracker.passes_emitted/merged/misses_emitted`, `DecodeEngine.counters`, `runner.frame_errors`, `DecodeResult.latency_ms` |
| HTTP sink | `Sink` interface on the `EventBus` (per-sink error isolation already exists); SQLite via stdlib |
| Soak | `PipelineRunner.run()` + reconcile machinery; blocking replay queue semantics (non-live sources use `put_blocking`, never drop) |

## 2. Design decisions

### VideoFileSource (`palletscan/sources/video.py`)

- **cv2.VideoCapture**, fps from `CAP_PROP_FPS` with a config override for
  files with broken metadata. `ts = frame_index / fps` — the *native* clock
  regardless of playback speed, so dedup/buffer/miss logic is bit-identical
  at any acceleration (this is exactly why Phase 1 keyed everything off
  source clock).
- **Speed**: `video.speed: float` — `1.0` paces delivery with `time.sleep`
  (as-if-live), `>1` accelerates, `0` = unpaced (max speed, for soak).
  Pacing affects only wall-clock delivery, never `ts`.
- **`live = False` always**: a file's frames are all available, so
  backpressure blocks rather than drops (matches the queue-policy fix).
  "As-if-live" means paced, not lossy.
- **Grayscale at ingest** (spec §2): 3-channel → `cvtColor` once;
  already-gray passthrough.
- **Looping**: `video.loop: int` (0 = infinite) for soak; `frame_index`
  keeps incrementing across loops so `ts` stays monotonic.
- **Config/CLI**: `source.type` Literal gains `"video"`; new `video:`
  config block; `palletscan replay <file> [--speed N]` subcommand (spec §8).
- **Recording tool** (`tools/record_synthetic.py`): renders a
  SyntheticSource run to a clip + `truth.jsonl`. Default container
  **.avi/MJPG**, not mp4/H.264 — OpenCV's bundled mp4 encoders are lossy
  enough to perturb the decodability envelope and H.264 availability varies
  by platform; MJPG is pip-only-safe and high-fidelity. (Replay itself
  accepts any .mp4/.avi the OS can decode, per spec.)

### Metrics (`palletscan/metrics.py`)

- A **`MetricsRegistry` owned by `PipelineRunner`** — no globals (Phase 1
  principle). Components get narrow hooks (e.g. the engine records
  per-frame decode wall time including failed attempts, not just successful
  `latency_ms`).
- **Latency p50/p95**: bounded deque reservoir (~2k samples), percentiles
  computed at snapshot time. `deque.append` is GIL-atomic; snapshot reads
  are approximate by design (documented).
- **Rates**: fps = windowed count at pipeline ingest; passes/hour + read
  rate over rolling 1h windows of event source-timestamps; queue depths
  sampled at snapshot.
- **One stable `snapshot() -> dict` shape** is the contract: consumed now
  by `RunSummary` + an optional periodic structured-log line
  (`--stats-interval`), and unchanged later by Phase 4's `/stats.json`.
  Keys: per the spec list — fps, queue depths, decode p50/p95, passes/hour,
  read rate, miss count, drop counters, frame/sink errors, uptime; outbox
  depth + oldest-age from the HTTP sink.

### HTTP sink with store-and-forward (`palletscan/events/http_sink.py`)

- **Outbox pattern, two stages**: the bus thread's `handle()` does one fast
  local thing — insert the event JSON into a **SQLite outbox**
  (`data/outbox.db`, WAL mode, connection per thread). A dedicated
  **uploader thread** drains it: POST → 2xx → delete row; failure →
  exponential backoff 1s→60s (jittered, per-queue, success resets). The bus
  thread never touches the network, so a dead endpoint can't stall event
  flow — offline-first by construction.
- **Why SQLite over JSONL segments**: transactional ack (no partial-line
  corruption on crash), trivial size/age caps, stdlib-only.
- **Delivery contract**: one event per POST, body = event JSON, `2xx` =
  ack; **at-least-once** (a crash between POST and delete re-sends;
  `event_id` lets the receiver dedupe). URL/headers/timeout config-driven;
  endpoint TBD per spec, so the simplest possible contract wins. Batching
  stays a future config knob. Document the at-least-once + `event_id`
  dedupe contract in the README.
- **Client**: stdlib `urllib.request` with timeout — no new runtime
  dependency for the sender.
- **Caps**: outbox max-MB/max-age, pruned oldest-first with a *counted and
  logged* drop (never silent — account-for-everything applies to the outbox
  too).
- **Crash persistence**: `close()` stops the uploader after the in-flight
  attempt; pending rows survive and drain on next start. That *is* the
  store-and-forward guarantee, and it gets a dedicated test.
- **Echo stub** (`tools/echo_server.py`): FastAPI + uvicorn (already in the
  spec stack), `POST /events` → `{"ok": true}`, with optional
  `?fail_rate=`/`?latency_ms=` knobs for manual chaos testing. Pytest does
  **not** depend on uvicorn — tests use a stdlib `http.server` thread
  fixture with scriptable responses (200s, 500s, connection-refused
  phases).

### Soak (`tools/soak.py`)

- **Duration-driven** (`--minutes/--hours`), two load modes: `replay`
  (loop a recorded clip — naturally constant memory, exercises the new
  source) and `synthetic` (long generated run).
- **Memory flatness**: sample RSS every few seconds via **psutil** (in
  `[dev]` extras; `resource.getrusage` doesn't exist on the Windows
  target). Assertion: after a warmup window, linear-fit slope below
  threshold (~1 MB/min) and final RSS within ~1.3× post-warmup baseline.
  Text report of the curve.
- **Zero unhandled exceptions**: assert `thread_errors == []`,
  `frame_errors == 0`, `sink_errors == 0` — the runner already records all
  three.
- **Injected source failures**: a `FlakySource` wrapper
  (`palletscan/reliability/flaky.py`) that delegates to any `FrameSource`
  and injects stalls or raises at configured frame counts. **Explicit
  scoping decision**: the in-process watchdog (stall detection → reopen,
  <10 s) is Phase 3 per spec §10. Phase 2 verifies the *crash-only* half of
  the contract: injected failure → pipeline flushes (flush-in-`finally`
  guarantees pending misses become events) → soak harness restarts the run
  → no event loss across the restart, proven by outbox persistence + truth
  reconciliation over the combined run, restart gap measured <10 s. Phase
  3's watchdog then upgrades recovery to in-process. Record in
  ASSUMPTIONS.md.

## 3. Approved refactors (everything else is purely additive)

1. **Source factory** — `palletscan/sources/create(cfg) -> FrameSource`,
   replacing the hardcoded `if cfg.source.type != "synthetic": raise` in
   `app.from_config`. Justification: the second source type is what that
   special case was waiting for; the prior review flagged it (altitude) and
   it was deferred precisely until now.
2. **Lazy pass planning in SyntheticSource** — `_plan_pass` currently runs
   for *all* passes at construction, holding every rendered patch in memory
   (~100 KB each → a multi-hour soak at thousands of passes would hold
   GBs). Plans become generated per-pass from the same pre-spawned
   `SeedSequence` children, so determinism ("pass *i* reproducible
   regardless of consumption") is preserved by construction and the
   existing determinism tests stay green. Justification: required for long
   synthetic soaks; no behavior change.

Hygiene item: ASSUMPTIONS.md entries #10/#13/#16 still describe
pre-fix-pass behavior (drop-oldest unconditionally, `max_count=1`, dedup
refresh semantics) — amend them alongside the new Phase 2 entries.

## 4. Build order

1. **Metrics** — registry, reservoir, rolling windows, snapshot; wire
   existing counters; unit tests. (First because sources/sink/soak all
   report into it.)
2. **VideoFileSource** — source factory, video source, record tool,
   `replay` CLI; tests: gray normalization, ts monotonicity across loops,
   pacing vs speed=0, fps-metadata fallback, **replay-of-synthetic
   acceptance** (record ~40 passes → replay → decoded payloads == truth).
3. **HTTP sink** — outbox + uploader + backoff; echo stub; tests: happy
   path, 500s/refused → backoff → recovery, crash-restart persistence, cap
   pruning counted, offline accumulation then drain.
4. **Soak** — FlakySource, RSS sampling, lazy synthetic plans,
   `tools/soak.py`; a ~3-minute `@pytest.mark.soak_short` variant asserting
   the same invariants; full 2h run executed once manually.
5. **Gate** — full suite green (existing 69 tests untouched and passing),
   2h+ soak results recorded in ASSUMPTIONS.md/README, default config
   bit-identical behavior for Phase 1 paths.

## 5. Acceptance criteria → verification

| Criterion (spec §11) | Verified by |
|---|---|
| `pytest` fully green; existing acceptance gate intact | Existing 400-pass gate unchanged; ~25–30 new tests |
| Replay of a synthetic "recorded" clip decodes all expected payloads | New acceptance test: record → replay → decoded payload set equals truth set, decode-XOR-miss accounted (manifest reconciliation *report* is Phase 4; the payload-level equivalence is the Phase 2 half) |
| Injected source failure → recovery <10 s, logged, no crash, no event loss | FlakySource test: stall + raise modes; harness restart gap timed <10 s; truth reconciliation across combined run = zero unaccounted; outbox proves no event loss (in-process watchdog lands in Phase 3) |
| 2h+ accelerated soak: flat memory, zero unhandled exceptions | `tools/soak.py` slope/ceiling assertions + error counters; short-soak pytest variant for CI; one real 2h run with results pasted into ASSUMPTIONS.md |
| Metrics exposed | Unit tests on percentiles/windows + smoke test asserting a sane snapshot after a full run; dict shape documented as the Phase 4 `/stats.json` contract |
| Store-and-forward HTTP sink | Echo-stub end-to-end (delivered == emitted), outage-then-drain test, restart-persistence test |

## 6. New config keys and dependencies

**Config** (all additive, all defaulted so Phase 1 behavior is unchanged):

- `source.type: video`
- `video: {path, fps_override, speed, loop}`
- `sinks.http: {enabled: false, url, headers, timeout_s, outbox_path, retry: {base_s, cap_s}, max_mb, max_age_days}`
- `metrics: {window_s, latency_samples}`

**Dependencies**: psutil in `[dev]` extras (soak only). FastAPI/uvicorn
enter `pyproject` now for the echo stub — already mandated by the spec
stack.

**New modules**: `palletscan/metrics.py`, `palletscan/sources/video.py`,
`palletscan/sources` factory, `palletscan/events/http_sink.py`,
`palletscan/reliability/flaky.py`, `tools/record_synthetic.py`,
`tools/echo_server.py`, `tools/soak.py`.
