# Phase 4 Plan — Dashboard + A/B Trial Reporting

Status: **approved 2026-06-11** (D1 approved with owner amendment; D2–D11
approved as proposed). Execute in order (§3), starting with the event/metric
extensions. Phases 1–3 are complete and green (235 tests); recorded decisions
live in ASSUMPTIONS.md #1–39. Phase 4 builds spec §6 only: live MJPEG view
per camera with decode/motion overlays, stats tiles backed by the pinned
`snapshot()` contract at `/stats.json`, last-N events table, miss-evidence
gallery with mark-reviewed, the A/B comparison report (per-camera passes
seen/decoded, read rate, time-to-first-decode, decodes/pass; markdown + CSV
export), and manifest reconciliation. Localhost bind, no auth (documented).

The phase's central design problem: the runner is single-source today, and
A/B requires both cameras live simultaneously into one zone, with business
events deduped across cameras while per-camera stats must NOT dedupe
(spec §4).

## 0. Approved decisions (owner rulings)

1. **D1 — Cross-camera dedup = emit-now + merge-by-reemit: approved WITH
   OWNER AMENDMENT (revision-guarded upsert).** On the first sighting of a
   payload the deduper publishes the business PassEvent immediately (keeping
   that camera event's `event_id` as the stable business id). A second
   camera's pass for the same payload within `dedup.window_s` (12 s default,
   the spec's number) is merged and **re-published with the same event_id**:
   min first_seen, max last_seen, summed decode_count, merged
   `cameras`/`camera_detail`, best_frame = earlier first decode, concatenated
   candidate_ids. Same-camera repeats within the window are suppressed +
   counted; the window anchor refreshes only on first emit (mirrors
   ASSUMPTIONS #16). Misses forward unchanged (no payload to key on; the
   per-camera miss IS the experiment's evidence). No held state, no expiry
   timers, no idle/accelerated-replay failure modes; report completeness
   does not depend on dedup timing. The rejected alternative
   (hold-until-all-cameras) needs expiry machinery whose early firing
   attributes the slower camera's data nowhere — systematically
   undercounting the slower camera and biasing the exact experiment this
   phase exists to run.
   **Owner amendment — re-emit ordering.** Publish stays outside the
   deduper's lock (the deduper must never couple the two runners' bus
   threads), so out-of-order re-emits of the same event_id are possible and
   a plain `INSERT OR REPLACE` could regress the SQLite row to a pre-merge
   version — and the dashboard/report read that row. Close it at the
   storage boundary: a monotonically increasing **`revision` (int, assigned
   under the deduper's lock)** rides on re-emitted events, and SqliteSink's
   upsert becomes conditional — replace only if the incoming revision is
   `>=` the stored one (`INSERT ... ON CONFLICT(event_id) DO UPDATE SET ...
   WHERE excluded.revision >= events.revision`). A stale v1 arriving after
   v2 is a no-op. A concurrency test hammers ONE payload from two threads
   and asserts the FINAL stored row is the fully-merged version — not
   merely that no ids were lost.
   *Accepted costs (documented in ASSUMPTIONS):* JSONL gets ≤N_cameras
   lines per business pass sharing one event_id (append-only audit log;
   readers take max-revision-wins). HTTP receivers keep the first
   (one-camera) version per the existing at-least-once dedupe-on-event_id
   contract (ASSUMPTIONS #23) — business fields are correct in v1; merged
   per-camera detail is a local trial-reporting concern. ConsoleSink prints
   the merged event again (harmless). The business bus deliberately gets
   **no MetricsRegistry** (a `_MetricsSink` there would double-count merged
   passes); the `/stats.json` business section serves the deduper's own
   counters.
2. **D2 — PassEvent additive fields: approved.** Trailing, defaulted
   (frozen/slots-safe; all constructions are keyword-arg):
   `first_decode_ts: float | None = None`, `camera_detail: dict[str, dict]
   | None = None` (source_id → `{first_seen_ts, first_decode_ts,
   last_seen_ts, decode_count}`), and — per the D1 amendment —
   `revision: int = 0`. The tracker always populates the first two (single
   entry, revision 0); the deduper merges `camera_detail` and bumps
   `revision`. `cameras` stays unchanged (ASSUMPTIONS #19 back-compat).
   Time-to-first-decode = `camera_detail[cam].first_decode_ts −
   camera_detail[cam].first_seen_ts` — same-camera timestamps, so
   cross-camera clock skew cancels. The report falls back to the `cameras`
   map (ttfd = None) for pre-Phase-4 rows so old DBs stay browsable.
3. **D3 — `/stats.json` envelope, uniform across modes: approved.**
   `{"generated_utc": iso, "cameras": {source_id: <snapshot() verbatim>},
   "business": {deduper counters} | null}` — single-camera runs have one
   `cameras` entry and `business: null`. The pinned snapshot IS served
   verbatim, nested under one stable key; a mode-dependent shape would
   force clients to sniff. Amends the ASSUMPTIONS #24 wording the way #36
   amended the key set — recorded as a new numbered assumption.
4. **D4 — `snapshot()` gains top-level `read_rate_24h`: approved.** Spec §6
   tile ("read rate (rolling 1h/24h)"): a second `_SourceTimeWindow(86400)`
   pair fed by the same `record_pass`/`record_miss` hooks, computed
   identically to `read_rate_1h`. `SNAPSHOT_KEYS` in tests/test_metrics.py
   amended — **the only edit to an existing test file** (precedent: #36).
5. **D5 — Per-camera evidence subdirectories in A/B mode: approved.**
   StationRunner rebases each runner's evidence dir to
   `<evidence.dir>/<source_id>` (per-runner config copy), eliminating a
   verified concurrent-prune race that can silently eat a MissEvent (two
   runners sharing one root race each other's `rmtree` during
   `_candidate_dirs`/`_dir_size`; the exception lands in `_finalize_miss`
   before `_emit`). Plus defensive `try/except (FileNotFoundError, OSError)`
   in those two helpers as cheap hardening. Caps become per-camera in A/B
   mode — documented.
6. **D6 — SqliteSink gains `PRAGMA busy_timeout=5000`: approved.** The
   dashboard's mark-reviewed/manifest writes open a second writer
   connection on the same WAL DB; without a busy timeout the bus thread's
   commit can raise SQLITE_BUSY immediately and drop an event row
   (http_sink already sets it).
7. **D7 — Reviews/manifest in the same SQLite file, web-owned tables:
   approved.** `miss_reviews(event_id PK, reviewed, note, reviewed_utc)`,
   `manifest(payload PK)`, created by the web read-store (`CREATE TABLE IF
   NOT EXISTS`), not by SqliteSink. Reviews key on the miss `event_id`, so
   they survive evidence pruning. Manifest also accepts a config-pointed
   CSV (`report.manifest_path`) as fallback when the table is empty.
   Manifest upload is a **raw `text/csv` request body** (JS FileReader →
   fetch) — no python-multipart dependency.
8. **D8 — A/B run configuration: approved.** New optional `source.cameras:
   list[str]` (mutually exclusive with `source.camera`, ≥2 entries, each
   must resolve) routes `palletscan run` through the new StationRunner.
   `palletscan synth --ab` runs two same-seed SyntheticSources
   (`source_id="synthA"/"synthB"`) — bit-identical pass schedules model two
   cameras on one zone and exercise the full merge path without hardware.
9. **D9 — Standalone `palletscan dashboard` subcommand: approved.** Same
   app factory with no runners — live view 503, `cameras: {}` in stats,
   but events/misses/report/manifest work read-only against the configured
   DB (refuses to start if the DB file is absent). This is how the trial
   gets reviewed after the station stops.
10. **D10 — Dependencies: approved.** `httpx` into `[dev]` extras only
    (TestClient transport). Runtime deps unchanged (fastapi/uvicorn already
    present, used by tools/echo_server.py). Dashboard HTML/JS/CSS is
    vendored static (vanilla JS polling, no CDN, offline-first), packaged
    like `palletscan/assets/` (verify packaging includes
    `palletscan/web/static/*`).
11. **D11 — One justified refactor: approved.** Extract the
    sink-construction block of `PipelineRunner.from_config` (app.py:272–280)
    into module-level `build_sinks(cfg) -> list[Sink]` so StationRunner
    builds the business sink set without duplication. `from_config` calls
    it; behavior identical.

## 1. Verified facts the plan builds on (checked against Phase 3 HEAD)

- `PipelineRunner` (app.py:185) is single-source but **fully
  multi-instantiable**: no globals, per-instance
  MetricsRegistry/EventBus/threads/queues. `from_config(cfg, source=None)`;
  sinks injected via constructor. `_process_frame` (app.py:324) has frame +
  MotionResult + decode results in hand — the natural overlay-tap hook
  (`decodes` currently only exists inside the `if result.active` branch).
- `PassEvent.first_seen_ts` is the **segment-open** ts; first-decode time
  is available at `PassTracker._finalize_segment` as `decodes[0].ts`
  (pass_tracker.py:227) but is **not persisted anywhere** →
  time-to-first-decode currently underivable from stored events.
- `event_to_dict` (events/sinks.py:25) = `dataclasses.asdict` + kind →
  additive PassEvent fields flow into JSONL and SQLite `detail_json`
  automatically. `SqliteSink` keys on `event_id` PK with `INSERT OR
  REPLACE` (sinks.py:148) — the seam D1 exploits; the D1 amendment replaces
  the statement with the revision-guarded conditional upsert. No
  `busy_timeout` is set today (sinks.py:107).
- HTTP outbox schema is `seq AUTOINCREMENT, event_id TEXT` (no unique
  constraint, http_sink.py:50) → re-emitting an event_id inserts a second
  outbox row, both POST, receivers dedupe on `event_id` per the
  at-least-once contract (ASSUMPTIONS #23). No constraint error.
- `EvidenceWriter.prune()` runs on the **pipeline thread** on every miss
  write; `_candidate_dirs()`/`_dir_size()` (evidence.py:85–98) stat/rglob
  without exception handling → the D5 race is real in any two-runner
  design sharing one evidence root.
- `MetricsRegistry` is per-runner; `snapshot()` shape pinned by
  `SNAPSHOT_KEYS` (tests/test_metrics.py:23). Only `read_rate_1h` exists.
  `_SourceTimeWindow` holds pass/miss timestamps (not frames) — a 24 h
  window at ~10k pallets/day is trivially cheap under the 100k cap.
- `_exit_code_for` (cli.py:182) maps `isinstance(exc.__cause__,
  WatchdogEscalation)` → 3. Any station-level re-raise must chain from the
  runner error's **cause**, or escalation degrades to exit 1.
- `SyntheticSource` already takes `source_id: str = "synth0"`
  (synthetic.py:57) and has a `realtime` pacing flag — `synth --ab` needs
  no source changes; the MJPEG live test uses `realtime: true`. MotionGate
  candidate_ids are `f"{source_id}-{seq:06d}"` → no cross-camera
  collisions.
- `PassEvent(` constructions exist only at pass_tracker.py:229,
  tests/test_sinks.py:15, tests/test_http_sink.py:100 — all keyword-arg;
  trailing defaulted fields are safe. test_sinks JSONL/detail assertions
  are subset-based. test_http_sink's outbox-cap sizing derives from
  `asdict` of the same dataclass (self-consistent). The **only existing
  test needing an edit** is tests/test_metrics.py (`SNAPSHOT_KEYS`).

## 2. Architecture

**One process, one `PipelineRunner` per camera, plus an event-layer
cross-camera deduper — NOT a multi-source runner.** Spec §4's required
shape is literally "Pipeline per camera". Per-camera independence — own
MotionGate (different native modes), own decode budget, own MetricsRegistry
(per-camera fps/health/read-rate for free), own watchdog (one unplugged
camera can't pollute the other arm's stats) — **is** the A/B experiment.
The runner is already multi-instantiable with zero shared state; this is
pure reuse. A multi-source runner would need per-source
gates/trackers/buffers anyway, would couple the arms through a shared
decode budget, and would force a per-source dimension into the pinned
metrics contract. The only genuinely cross-camera concern is business-event
dedup, which is an event-layer concern, placed there
(`palletscan/events/dedup.py`).

```
            ┌─ PipelineRunner(camA cfg) ── bus A ── ForwardingSink ─┐
 sources ───┤   (own gate/engine/tracker/metrics/watchdog)          ├─► CrossCameraDeduper ─► business EventBus ─► console/jsonl/sqlite/http (+ station collector)
            └─ PipelineRunner(camB cfg) ── bus B ── ForwardingSink ─┘     (lock; emit-now, merge-by-reemit,
                     │ LivePreview tap per runner (optional)               revision assigned under lock)
                     ▼
              FastAPI app on a uvicorn background thread:
              /  /stats.json  /live/{cam}  /api/events  /api/misses(+review)
              /evidence/*  /api/report/ab(+.md/.csv)  /api/manifest(+reconciliation)
              reads: runner.metrics.snapshot() + previews (in-process), SQLite events DB (per-request connections)
```

Single-camera mode is wired **exactly as today** (sinks directly on the
runner's bus, no deduper) — zero behavior change to the existing 235 tests.
Revision stays 0 everywhere in single-camera mode; the conditional upsert
degrades to today's semantics.

## 3. Build order (commit-sized steps; tests green at every gate)

### Step 1 — Event/metric extensions + sink hardening (additive)

Modify:
- `palletscan/types.py`: PassEvent += `first_decode_ts`, `camera_detail`,
  `revision` (D2; trailing defaults).
- `palletscan/pipeline/pass_tracker.py` (~line 228): emit with
  `first_decode_ts=decodes[0].ts`,
  `camera_detail={self._source_id: {first_seen_ts, first_decode_ts,
  last_seen_ts, decode_count}}` (revision stays default 0).
- `palletscan/metrics.py`: `_DAY_S = 86400.0`; second window pair;
  `read_rate_24h` in snapshot (D4).
- `palletscan/events/sinks.py`: busy_timeout (D6); **schema v2** — `events`
  gains `revision INTEGER NOT NULL DEFAULT 0`; on connect read
  `PRAGMA user_version`: 0/fresh → create v2 schema, set user_version=2;
  1 → `ALTER TABLE events ADD COLUMN revision INTEGER NOT NULL DEFAULT 0`,
  set user_version=2; 2 → no-op. Replace `INSERT OR REPLACE` with the
  conditional upsert (D1 amendment):
  `INSERT INTO events VALUES (...) ON CONFLICT(event_id) DO UPDATE SET
  <all columns>=excluded.<col> WHERE excluded.revision >= events.revision`.
  Miss rows write revision 0 (misses are never re-emitted).
- `palletscan/events/evidence.py`: defensive stat/size handling (D5
  hardening half).
- `tests/test_metrics.py`: SNAPSHOT_KEYS += `read_rate_24h` (the only
  existing-test edit).

Tests: extend test_pass_tracker (first_decode_ts == first decode's ts;
camera_detail mirrors cameras), test_metrics (24 h window counts; an event
>1 h but <24 h old appears in the 24 h rate only), test_sinks (new fields
round-trip detail_json; v1→v2 migration on an existing Phase-3 DB file;
**deterministic stale-write test**: write revision 1 then revision 0 for
the same event_id → row stays at revision 1). Full suite green.

### Step 2 — CrossCameraDeduper (new `palletscan/events/dedup.py`)

`ForwardingSink(Sink)` (handle → deduper; `close()` no-op — the
double-close firewall) and `CrossCameraDeduper(publish, window_s)` per D1.
Lock around the payload map; merged event + its revision (prev+1, via
`dataclasses.replace`) computed **under** the lock; **publish outside the
lock** (business `EventBus.publish` is a blocking put; don't stall the
other runner's bus thread on a full queue — the revision guard at the sink
absorbs the resulting reorder risk). Lazy pruning of the payload map by
high-water ts + size cap. Counters: passes_emitted, cross_camera_merges,
repeats_suppressed, reemits, misses_forwarded; `stats() -> dict`.

Tests (new tests/test_dedup.py): single-camera pass-through verbatim with
same event_id and revision 0; two cameras same payload → 2 publishes, one
event_id, revisions 0 then 1, merged camera_detail/summed counts/min-max
ts/earlier best_frame; beyond window → new event_id; misses pass through;
same-camera repeat suppressed + anchor not extended (parked-pallet rule);
map pruning bounded; **the owner-amendment hammer test** — repeated rounds
of two barrier-synchronized threads emitting the same payload from
different cameras through deduper → real SqliteSink; after every round the
FINAL stored row contains both cameras' camera_detail, the summed
decode_count, and revision == max emitted (not merely that no ids were
lost).

### Step 3 — StationRunner + config + `synth --ab` + integration test

Create `palletscan/station.py`: `StationRunner(cfg, sources=None)` —
business sinks via `build_sinks(cfg)` (D11) + station `_ListSink`
collector; `EventBus` + deduper; per camera: config copy with
`source.camera=<id>` and `evidence.dir=<root>/<source_id>` (D5);
`PipelineRunner(cfg_i, source_i, [ForwardingSink(deduper)])` (runner's
internal `_ListSink`/`_MetricsSink` untouched → per-camera metrics never
dedupe). `run()`: start business bus; thread per runner.run();
**error-completion stops the others** (a half-running trial silently
biases the experiment); normal exhaustion does not; join all →
`business_bus.shutdown()`; re-raise chaining from the runner error's
`__cause__` so exit code 3 survives (cli.py:182 inspects `exc.__cause__`);
return StationSummary (per-camera RunSummaries + deduper counters +
business reconciliation via `reconcile_truth` against **distinct** business
event_ids, max-revision rows). Expose `runners: dict[source_id, runner]`,
`stop()`.

Modify: config.py (`source.cameras` + validator, D8); app.py
(`build_sinks` extraction, D11); cli.py (`synth --ab`; `run` routes to
station when `source.cameras` set).

Tests (new tests/test_station.py): two same-seed fast synthetic sources →
distinct business pass events == truth count (NOT 2×); every business
event's stored row carries both source_ids in camera_detail (revision-
guarded upsert proven end-to-end in SQLite); each runner's
`passes_emitted` == truth count (per-camera stats not deduped — spec §4);
zero collector drops/sink errors; evidence under per-camera subdirs;
escalation cause-chain → exit 3 (unit test on the chain).

### Step 4 — `web:` config + LivePreview tap + read store

Modify: config.py — `WebConfig{enabled=False, host="127.0.0.1", port=8000,
preview_fps=10.0, preview_quality=80, preview_width=640}`,
`ReportConfig{manifest_path: Path|None}`; app.py — `self.preview:
LivePreview|None = None`; `_process_frame` restructure: `decodes:
list[DecodeResult] = []` before the active branch, then `if self.preview
is not None: self.preview.update(frame, result, decodes)` (~6-line diff;
assigned before `run()`, no signature churn; the None-check is the only
cost when disabled).

Create: `palletscan/web/preview.py` — `LivePreview`: lock-guarded latest
(frame ref, MotionResult, decode-overlay deque with ~1 s linger by source
ts); `render_jpeg()` copies under lock, draws outside it (gray→BGR, motion
box, decode boxes + payload text, header line), downscales to
preview_width, imencode at preview_quality → `(bytes|None, stamp)`.
Bounded by construction (one frame ref + small deque).

Create: `palletscan/web/store.py` — `ReadStore(db_path)`: ensures
miss_reviews/manifest tables (D7), busy_timeout, **fresh connection per
call** (sync routes run in Starlette's threadpool; satisfies sqlite3's
same-thread rule by construction); queries: `recent_events(limit, kind)`
(detail_json parsed), `misses(limit, unreviewed_only)` (LEFT JOIN
miss_reviews), `mark_reviewed`, `replace_manifest`/`manifest_payloads()`
(config-path fallback), `pass_and_miss_rows(window)`.

Tests: preview update/render round-trip (boxes change pixels, stamp
advances, None pre-first-frame); ReadStore against a SqliteSink-written
tmp DB (review upsert + join + persistence; busy-timeout smoke).

### Step 5 — FastAPI app + MJPEG (`palletscan/web/app.py`, `web/static/`)

`DashboardContext` dataclass `{snapshots: dict[str, Callable[[], dict]],
previews, business: Callable|None, store, evidence_root, web_cfg,
manifest_path}`; `create_app(ctx) -> FastAPI` factory (no globals,
TestClient-friendly). Routes:
- `GET /` → vendored index.html/app.js/style.css (vanilla JS, ~2 s
  polling; `<img src="/live/{id}">` per camera; tiles: read rate 1h/24h,
  passes/hour, fps, queue depths, decode p50/p95, source health
  (stalls/reconnects), miss count, uptime, business counters).
- `GET /stats.json` → D3 envelope.
- `GET /live/{camera_id}` → **async-generator** MJPEG: loop `await
  request.is_disconnected()` → `await
  run_in_threadpool(preview.render_jpeg)` → yield multipart JPEG on new
  stamp (keepalive re-yield ~1 s on idle) → `await asyncio.sleep(
  1/preview_fps)`. Async generator, not sync-in-threadpool: a sync
  generator parks an AnyIO worker token per client across its pacing
  sleeps and tears down nondeterministically on disconnect; async cancels
  at the next await and occupies the pool only for the milliseconds of an
  actual encode. 404 unknown id; 503 when no previews (standalone).
- `GET /api/events?limit=50&kind=` (clamped ≤500, newest-first),
  `GET /api/misses?limit&unreviewed_only` (evidence_dir relativized into
  `/evidence/...` URLs; omit images if pruned/outside root — never 500),
  `POST /api/misses/{event_id}/review`, `/evidence` StaticFiles read-only
  mount, `POST /api/manifest` (raw text/csv body, D7).

Modify: pyproject.toml — httpx → `[dev]` (D10); ensure static packaged.

Tests (new tests/test_web_api.py, TestClient): stats envelope pinned AND
`cameras[id]` key-set == SNAPSHOT_KEYS (ties the endpoint to the pinned
contract); events limit/order; miss → review → persists across a fresh
ReadStore; evidence static fetch; pruned-evidence degradation. New
tests/test_web_live.py: realtime fast synthetic runner + tap in a thread;
`client.stream` reads ≥2 multipart JPEG parts (magic bytes); unknown
camera 404; app still serves /stats.json after stream close.

### Step 6 — Reporting (`palletscan/reporting/`)

- `ab.py`: pure `compute_ab_report(pass_rows, miss_rows, window) ->
  ABReport` — per camera: passes_seen (business passes whose camera_detail
  contains cam + cam's misses), passes_decoded, read_rate, ttfd median/p95
  (same-camera deltas; reuse `metrics.percentile`), decodes_per_pass,
  misses; business totals; `from`/`to` ISO filters via
  `datetime.fromisoformat` (not string compare); legacy-row fallback (D2).
- `manifest.py`: `parse_manifest(text)` (stdlib csv; first column; skip
  row 1 iff its first cell lowercased ∈ {"payload","pallet_id","pallet",
  "id","code"}; dedupe preserving order); `reconcile(expected, scanned) ->
  {matched, missing, unexpected, true_read_rate}`.
- `render.py`: `ab_markdown` (camera-vs-camera comparison table, generic
  over source_ids), `ab_csv`, `reconciliation_csv`.

Wire endpoints: `GET /api/report/ab`, `GET /report/ab.md`,
`GET /report/ab.csv` (Content-Disposition), `GET /api/report/
reconciliation`, `GET /report/reconciliation.csv`.

Tests: report math on handcrafted rows (e.g. cam in 8/10 passes + 2 misses
→ seen 10, decoded 8, rate 0.8; ttfd against known values; window filters;
legacy fallback); md/csv structural assertions (headers + key figures, not
byte-pinned); manifest header rule/CRLF/dupes; reconciliation buckets +
true read rate; endpoint round-trips including raw-body upload.

### Step 7 — Server lifecycle + CLI wiring

Create `palletscan/web/server.py`: `DashboardServer` wrapping
`uvicorn.Server` (our logging kept; `log_level="warning"`); `start()`
spawns thread, waits on `server.started` with timeout; `stop()` sets
`should_exit`, joins. (uvicorn off-main-thread skips signal handlers —
verified against the installed version.)

Modify cli.py: `--dashboard` on run/synth/replay (also honors
`web.enabled`): build previews (assign `runner.preview`),
DashboardContext (snapshots from `runner.metrics.snapshot`;
`business=deduper.stats` when stationed), start server before `run()`,
stop in `finally`; fail fast with a clear message if
`sinks.sqlite.enabled` is false. New `palletscan dashboard` subcommand
(D9; refuses to start if the DB file is absent).

Tests: lifecycle smoke (ephemeral port, real GET /stats.json +
/api/events, clean stop, thread joined); CLI plumbing in test_cli_run.py
style.

### Step 8 — Docs + close-out

`config/default.yaml`: commented `web:`/`report:`/`source.cameras`
sections. README: dashboard quickstart (`palletscan synth --ab
--dashboard`, `palletscan dashboard`), **no-auth/localhost-bind note**
(spec §12: auth is future work; bind 127.0.0.1 by default).
ASSUMPTIONS.md: numbered entries for D1 as amended (reemit + revision
semantics, JSONL max-revision-wins, HTTP first-version note), D3/D4 (stats
contract amendments), D5 (per-camera evidence caps), D6, D7, the
clock-skew caveat (cross-camera merged first/last ts mix per-source clocks
anchored ~1–3 s apart at construction; display-grade, not
measurement-grade; ttfd is skew-free by construction; no shared-epoch
plumbing this phase), D10. Full suite + `pytest -m soak_short` on an idle
machine (if soak_short misbehaves, reproduce on HEAD before blaming
Phase 4). mypy clean.

## 4. Verification matrix (criterion → proof)

| Phase 4 criterion | Proven by |
|---|---|
| Live MJPEG per camera with decode/motion overlays | test_web_live (≥2 multipart JPEG parts from a realtime synthetic run); preview unit test pins boxes actually drawn (pixel deltas); manual `palletscan synth --ab --dashboard` |
| Stats tiles backed by pinned snapshot contract at /stats.json | test_web_api pins the D3 envelope AND asserts `cameras[id]` keys == SNAPSHOT_KEYS; amended pin covers read_rate_24h; per-camera health from existing `source` section |
| Last-N events table | /api/events tests (order, limit clamp, parsed detail); dashboard JS renders (manual) |
| Miss gallery + mark-reviewed | /api/misses join + /evidence/ image fetch; review persists across fresh ReadStore; reviews survive evidence pruning (row remains, images degrade gracefully) |
| A/B: business dedupe across cameras; per-camera stats independent (spec §4) | test_station: distinct business passes == truth (not 2×); stored rows carry both cameras' detail; each runner's passes_emitted == truth |
| Merged row can never regress to a pre-merge version (owner amendment) | deterministic stale-write test (v2 then v1 → row stays v2) + the two-thread one-payload hammer asserting the FINAL stored row is fully merged |
| A/B report (seen/decoded/read rate/ttfd/decodes-per-pass) + md + CSV export | reporting unit math vs handcrafted rows; station end-to-end numbers vs truth; download endpoints tested |
| Manifest reconciliation → true read rate | manifest unit tests; end-to-end: manifest built from truth.jsonl payloads (+1 extra, −1 missing) → buckets exact, true read rate == reconciliation read rate |
| Account-for-everything preserved under A/B | station test: zero collector drops/sink errors; SQLite rows == business ids; misses forwarded per camera; D5 race eliminated + hardened |
| Existing 235 tests green | only SNAPSHOT_KEYS edited (D4); PassEvent constructions/pins audited additive-safe; revision 0 + conditional upsert degrade to today's semantics single-camera |
| Exit-code 3 chain through station | unit test on the re-raise cause chain |

## 5. Out of scope (Phase 5 or never, per spec §12)

Auth/HTTPS, websockets, historical charts, RUNBOOK.md, Windows service
install, CPU measurement under burst load (spec §11, Phase 5),
shared-epoch camera clock alignment, python-multipart, any JS
framework/build step.
