# PalletScan

Production-grade, 24/7 fixed-camera scanning that reads QR / Data Matrix
codes on pallets as forklifts carry them past stationary USB cameras.
**Phases 1–5** (this state of the repo): the complete core pipeline on
synthetic input; video replay, metrics, a store-and-forward HTTP sink and
the soak harness; the live-camera stack — device enumeration by name,
empirical mode probing, a verified control layer, the reconnect watchdog,
and the `run`/`calibrate`/`selftest` CLIs — code-complete and tested
against fakes (cameras are in transit; see `ARRIVAL_CHECKLIST.md` for
exactly what to verify when they land); the dashboard + A/B trial
stack — live MJPEG views with decode/motion overlays, stats tiles, a miss
gallery with mark-reviewed, cross-camera business dedup, the A/B
comparison report (markdown/CSV export) and manifest reconciliation; and
the hardening/ops layer — rotating JSONL file logs, per-data-dir
single-instance locks, graceful SIGTERM/CTRL_BREAK shutdown, the
`supervise` restart-on-any-nonzero-exit supervisor with countable exit
codes, Windows Task Scheduler install scripts (`deploy/`), the CPU
measurement and demo tools, and `RUNBOOK.md` for operators.

```
FrameSource → MotionGate → DecodeEngine → Dedup/PassTracker → EventBus → Sinks
                   ↘ RollingFrameBuffer (pre/post evidence) ↗
```

The operational invariant: **every pallet pass is accounted for** — it
either produces a decode event or a flagged miss with saved frame evidence.

## Quickstart (macOS dev)

```bash
brew install zbar libdmtx                  # native decoder libraries
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not acceptance and not soak_short"   # fast suite (~25 s)
pytest                                     # full suite incl. acceptance gates + short soak
palletscan synth --passes 20 --seed 7      # run the pipeline on synthetic passes
python tools/record_synthetic.py --out data/clips/demo.avi --passes 10
palletscan replay data/clips/demo.avi --speed 0 --truth data/clips/demo.truth.jsonl
palletscan selftest                        # refuse-to-run-blind startup checks
palletscan calibrate --list                # enumerate cameras by name
palletscan run                             # run the configured source (cameras when present)
palletscan synth --ab --dashboard          # A/B demo: two synthetic cameras + live dashboard
palletscan dashboard                       # review a finished trial read-only
python tools/demo.py                       # the full demo: realtime A/B + dashboard in a browser
```

On Windows, skip the `brew` line — the `pyzbar`/`pylibdmtx` wheels bundle
the DLLs, and `pip install -e .` automatically pulls in `pygrabber`
(pure-Python DirectShow device-name enumeration; never installed on
macOS).

Outputs land under `./data/`: `events.jsonl`, `palletscan.db` (SQLite),
`evidence/` (JPEG bursts for missed passes), `truth.jsonl` (synthetic
ground truth).

## CLI

| command | what it does |
|---|---|
| `palletscan run [--config Y] [--camera ID] [--data-dir D] [--stats-interval S] [--dashboard]` | run the configured source — live cameras in production; `source.cameras: [a, b]` routes through the A/B station (one pipeline per camera, cross-camera business dedup) |
| `palletscan synth [--passes N] [--seed S] [--ab] [--config Y] [--data-dir D] [--stats-interval S] [--dashboard]` | run the full pipeline on generated pallet passes; `--ab` runs two same-seed synthetic cameras through the station |
| `palletscan replay <file> [--speed N] [--loop N] [--truth T.jsonl] [--fps-override F] [--dashboard]` | replay a recorded clip as if live (`--speed 0` = unpaced, `--truth` reconciles decoded payloads) |
| `palletscan supervise [--data-dir D] [--grace-s N] [--backoff-base-s N] [--backoff-cap-s N] [--stable-after-s N] -- run [args]` | restart the child on **any** nonzero exit (~5 s, crash-loop backoff to 300 s), append one JSONL line per child exit to `logs/restarts.jsonl`, stop gracefully via the `supervisor.stop` file — what the Windows scheduled task runs (see `RUNBOOK.md`) |
| `palletscan dashboard [--config Y] [--data-dir D]` | serve the dashboard read-only against an existing events DB (no runners) — how a finished trial gets reviewed |
| `palletscan calibrate [--list] [--camera ID] [--name SUBSTR] [--fourcc/--width/--height/--fps pins] [--exposure E] [--gain G] [--no-auto-exposure] [--seconds N] [--save] [--preview]` | probe modes empirically, verify controls (readback + exposure effect), stream fps/focus/decode lines, lock-and-save to the config |
| `palletscan selftest [--skip-camera] [--data-dir D]` | startup checks: enumerate + achieved-fps gate, bundled symbols through the full pipeline, disk space |
| `palletscan version` | print version |

Exit codes (the supervisor must restart on any nonzero exit):

| code | meaning |
|---|---|
| 0 | clean exit |
| 1 | software failure — check logs |
| 2 | usage error |
| 3 | watchdog escalation — USB stack wedged; check cable/hub, then logs |
| 4 | another instance holds the lock — `run`/`synth`/`replay` are single-instance per data-dir |

Tools: `tools/demo.py` (the end-to-end demo: realtime A/B synthetic +
dashboard opened in a browser), `tools/measure_cpu.py` (the spec §11 CPU
measurement under burst replay load), `tools/record_synthetic.py` (render
a synthetic run to .avi/MJPG + truth JSONL), `tools/echo_server.py`
(localhost endpoint for the HTTP sink, with `?fail_rate=`/`?latency_ms=`
chaos knobs), `tools/soak.py` (long-run memory/recovery soak),
`tools/bench_decoders.py`, `tools/make_selftest_assets.py` (regenerates
the committed selftest PNGs).

## Running 24/7 (Phase 5)

`RUNBOOK.md` is the operator manual: install from zero, Task Scheduler +
`palletscan supervise` service install (`deploy/*.ps1`), graceful
stop-file semantics, the exit-code table, file locations and caps, and
recovery procedures. The writer commands hold a per-data-dir instance
lock (a second writer exits 4 naming the holder; stale locks are
structurally impossible — the OS releases the lock when the holder dies)
and write rotating JSONL diagnostics to `<data-dir>/logs/` (20 MB × 6
cap, 14-day age prune; `restarts.jsonl` — the supervisor's per-child-exit
audit trail — is never pruned). Event sinks (`events.jsonl`,
`palletscan.db`) are the data of record and are deliberately not rotated;
the RUNBOOK documents archival.

## Live cameras (Phase 3)

Cameras are configured by **device-name substring** (`cameras[].name`) —
never bare index, which shuffles on replug. `palletscan calibrate` probes
the (format, resolution, fps) matrix on a fresh capture per candidate,
measures *achieved* fps (requested values are never trusted), prefers
uncompressed formats over MJPG among near-equals, verifies every control
write by readback plus a frame-brightness exposure check, and
`--save` upserts the locked entry (raw backend exposure/gain values +
the backend they were calibrated under) into your YAML atomically with a
`.bak`. On every (re)connect those settings are re-applied — UVC
controls reset on re-enumeration.

The reconnect watchdog detects stalls (`watchdog.stall_timeout_s`) and
reader failures, then closes/reopens with jittered backoff forever
(re-enumerating by name each attempt). It never gives up by default;
`max_outage_s` and `max_zombie_readers` escalate to exit code 3 when
only a process restart (wedged USB stack) can help. Reconnects, stalls,
reopen failures and zombie readers are in `snapshot()["source"]`.

Hardware-dependent behavior is honestly deferred: `ARRIVAL_CHECKLIST.md`
lists the verification pass for the day the cameras arrive.

## Configuration

Single YAML file (see `config/default.yaml`, every key optional). The
synthetic envelope is calibrated by the two dimensionless ratios that govern
decodability:

- **px/module** (`px_per_module_range`, default 3–6): the optics envelope at
  the real 3–15 ft read distance.
- **blur in modules**: derived from speed × exposure / module size.
  `exposure_fraction: 0.03` (~1 ms at 30 fps) is the locked global-shutter
  operating point; raise it to stress-test.

Both ratios are logged per pass in `truth.jsonl`, so any read-rate failure
shows exactly where in the envelope it broke.

## HTTP sink (store-and-forward)

`sinks.http` POSTs every event to a config-driven URL with an on-disk
SQLite outbox between the pipeline and the network, so the station keeps
working with no network and drains the backlog when the endpoint returns
(offline-first). **Delivery contract:** one event per POST, body = the
event JSON, any 2xx is the ack, **at-least-once** semantics — a crash
between POST and ack re-sends, so the receiver must dedupe on the event's
unique `event_id`. Failures retry with jittered exponential backoff
(capped at 60 s), and redirects count as failures — point the URL at the
final endpoint. The outbox is capped by size/age and prunes oldest-first
with counted, logged drops. Batching is deliberately deferred until the real
endpoint exists (`~7 events/min` makes one-per-POST fine).

Test it locally: `python tools/echo_server.py`, then enable `sinks.http`
in your config.

## Dashboard + A/B trial (Phase 4)

```bash
palletscan synth --ab --dashboard     # the full demo, no hardware needed
open http://127.0.0.1:8000            # live views, tiles, events, miss gallery
```

`--dashboard` (or `web.enabled: true`) serves, while the pipeline runs:
live MJPEG per camera with motion/decode overlay boxes (`/live/<id>`),
stats tiles backed by `/stats.json`, the last-events table, and the miss
gallery (evidence thumbnails, mark-reviewed with notes — reviews live in
the events DB keyed by event id, so they survive evidence pruning).
`palletscan dashboard` serves the same UI read-only against an existing DB
after the station stops.

**Security note:** the dashboard binds `127.0.0.1` and has **no
authentication** — auth is explicitly future work (spec §12). Do not bind
it beyond the host.

**A/B mode** (`synth --ab`, or `source.cameras: [idA, idB]` for live
cameras) runs one full pipeline per camera — own motion gate, decode
budget, metrics, watchdog — so each camera's stats are its independent
performance. Business events dedupe across cameras at the event layer: the
first sighting of a payload publishes immediately and a second camera's
sighting within `dedup.window_s` merges into the same `event_id` with a
bumped `revision` (storage keeps the highest revision, so out-of-order
re-emits can never regress a merged row). The report compares the arms:

- `GET /api/report/ab` — per camera: passes seen, passes decoded, read
  rate, time-to-first-decode median/p95 (same-camera timestamps, immune to
  cross-camera clock skew), decodes/pass, misses.
- `/report/ab.md`, `/report/ab.csv` — downloadable comparison.
- **Manifest reconciliation**: upload a CSV of expected pallet IDs (or set
  `report.manifest_path`) → `/api/report/reconciliation` buckets
  matched/missing/unexpected and computes the *true* read rate;
  `/report/reconciliation.csv` exports it.

## Metrics

Every runner owns a `MetricsRegistry`; its `snapshot()` dict (fps, queue
depths, decode wall-time p50/p95, passes/hour, rolling 1h/24h read rates,
miss count, drop/error counters, uptime, outbox depth/age) is the stable
contract that `/stats.json` serves verbatim per camera under
`cameras.<source_id>` (plus a `business` section in A/B mode).
`--stats-interval N` on `synth`/`replay` logs a snapshot line every N
seconds; the final snapshot is part of the run summary.

## Soak

```bash
python tools/soak.py --hours 2 --mode replay          # loop a recorded clip, unpaced
python tools/soak.py --minutes 5 --mode synthetic --inject-every-s 30
```

Asserts a flat memory profile (post-warmup RSS slope < 1 MB/min, final
< 1.3× baseline; warmup is detected adaptively per machine/OS — the
plateau knee of the RSS curve — unless `--warmup-s` pins it), zero
unhandled exceptions, and — with `--inject-every-s` —
crash/restart recovery with zero event loss (per-segment truth
reconciliation plus an outbox accounting check; restart gap must stay
under 10 s). `pytest -m soak_short` runs a ~6-minute variant of the same
invariants. The Phase 2 gate run — 2 h unpaced replay, 1.92 M frames,
34,067 events — held a *negative* RSS slope (−0.41 MB/min, final below
baseline) with zero errors; full report in ASSUMPTIONS.md #28.

## Tests

- `pytest -m "not acceptance and not soak_short"` — unit tests per stage
  (fast dev loop).
- `pytest` — adds the acceptance gates and the short soak: 400 synthetic
  passes across the full spec envelope must reach **≥99.5% pass-level read
  rate** with 100% of passes producing a decode event XOR a miss event with
  on-disk evidence; 40 recorded passes must replay to exactly their truth
  payload set; the ~6-minute soak must hold the memory/recovery
  invariants.

## Troubleshooting (macOS)

If `pyzbar`/`pylibdmtx` fail to load their dylibs:

```bash
export DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib
```

If a venv on macOS mysteriously stops finding installed packages, check for
hidden `.pth` files (`ls -lO .venv/lib/python*/site-packages/*.pth`) —
Python ≥3.12 silently skips `.pth` files carrying the `UF_HIDDEN` flag
(`chflags nohidden <file>` clears it). The code works around the known cases
(see `palletscan/_compat.py`).

## Layout

```
palletscan/
  cli.py            entry points (run/synth/replay/supervise/calibrate/selftest)
  __main__.py       python -m palletscan (how supervisor/tools spawn children)
  config.py         pydantic models + YAML loading + calibrate upsert
  types.py          Frame/Roi/events/ground-truth dataclasses
  app.py            PipelineRunner: threads, queues, shutdown drain
  metrics.py        MetricsRegistry: snapshot() is the /stats.json contract
  calibrate.py      probe/verify/lock-and-save orchestration
  selftest.py       refuse-to-run-blind startup checks
  logging_setup.py  JSON stderr + rotating, age-pruned JSONL file logs
  assets/           committed selftest symbols (tools/make_selftest_assets.py)
  sources/          FrameSource ABC, factory, SyntheticSource,
                    VideoFileSource, CameraSource, device enumeration,
                    control layer (QUIRKS), mode probing, render functions
  pipeline/         MotionGate, DecodeEngine, decoders, preprocess,
                    RollingFrameBuffer, PassTracker
  station.py        StationRunner: one pipeline per camera + business bus
  events/           EventBus, sinks (console/JSONL/SQLite/HTTP outbox),
                    EvidenceWriter, cross-camera deduper
  reliability/      bounded queues, FlakySource, reconnect WatchdogSource,
                    InstanceLock (per-data-dir), restart Supervisor
  reporting/        A/B report math, manifest reconciliation, md/csv render
  web/              FastAPI app, MJPEG LivePreview, SQLite ReadStore,
                    uvicorn DashboardServer, vendored static UI (no CDN)
deploy/             Task Scheduler install/uninstall/start/stop (PowerShell)
tools/              demo, measure_cpu, record_synthetic, echo_server, soak,
                    bench_decoders, make_selftest_assets
tests/
RUNBOOK.md          operator manual (install, service, recovery)
```

See `ASSUMPTIONS.md` for every decision made without a spec answer.
