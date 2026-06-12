# Phase 5 Plan — Hardening + Ops (final phase)

## Context

Phases 1–4 are complete and green at HEAD `8a315c4` (340 tests; review findings fixed; 7 cleanups deliberately deferred). Phase 5 closes the spec: log rotation (deferred from Phase 1 — `logging_setup.py` line 1 says "rotation arrives in Phase 5"), the single-instance lock (deferred from Phases 3/4), Windows service install with countable exit codes, RUNBOOK.md, the spec §11 CPU measurement under realistic burst load (deferred in ASSUMPTIONS #28), the end-to-end demo script, and (optional, recommended) adaptive soak warmup (ASSUMPTIONS #39 residue). No new pipeline features; reuse existing seams; the 7 deferred cleanups from REVIEW_7e4c22c stay untouched.

This plan follows the established workflow: owner issues numbered rulings on D1–D13, the approved plan is committed verbatim as `PLAN_PHASE5.md`, execution happens from that file in a fresh session. New ASSUMPTIONS continue from #52.

---

## 0. Approved decisions (owner rulings)

All thirteen decisions (D1–D13, including optional D11) approved as proposed, 2026-06-12.

**D1 — Lock mechanism: OS-level advisory/byte-range lock, not a PID file.**
`fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK)` on Windows, on `<data-dir>/palletscan.lock`, held open for the process lifetime. **Stale-lock behavior: structurally impossible** — the OS releases the lock when the holder dies, cleanly or not; no liveness probing, no PID-reuse races. Holder diagnostics (PID, start ts, argv) are written as JSON at file offset 0; because Windows byte-range locks are *mandatory* (a locked byte is unreadable by other handles), the actual lock byte sits at offset 0x100000 (locking past EOF is legal) so the JSON stays readable for ops. `release()` unlocks before close (MSDN: release timing after abnormal close is indeterminate — the supervisor's ≥5 s restart delay covers that window); the lock file is never unlinked (delete-while-open races; the leftover file is harmless last-holder diagnostics).
*Rejected:* PID file with liveness check (stale-PID and PID-reuse races — the exact failure mode the owner asked to define away). *Cost:* lock file persists after exit (cosmetic); rare seconds-scale release delay after a hard kill on Windows.

**D2 — Lock scope and holders: per data-dir; writer commands only; new exit code 4.**
`run`, `synth`, `replay` acquire the lock (they write sinks/evidence/logs); `dashboard`, `calibrate`, `selftest` do not (read-only/pre-flight; RUNBOOK says "stop the service before calibrating"). Two instances with different `--data-dir` may coexist by design (multi-station box, parallel dev/test). Contention → message naming the holder + **exit code 4 "another instance holds the lock"** (docstring + README updated; 0–3 keep their meanings).
*Rejected:* machine-wide mutex (kills multi-station and dev workflows). *Cost:* exit 4 from a supervised child loops with backoff while a manually-launched run holds the lock — visible in restarts.jsonl, self-healing.

**D3 — Log rotation: stdlib `RotatingFileHandler`, file handler only in lock-holding commands.**
New config `logging.file: {enabled: true, dir: data/logs, max_mb: 20, backups: 5, max_age_days: 14}`; JSONL file `data/logs/palletscan.jsonl` with the existing `JsonFormatter`; total size cap = `max_mb × (backups+1)` = 120 MB default. The stderr handler stays always (dev UX; supervisor inherits it). The file handler installs **only in `run`/`synth`/`replay` after the lock is held** — `doRollover()` renames, which on Windows fails if another process holds the file open; lock scope = file-logging scope makes single-writer rotation an invariant, not a hope. Age pruning ("prune logs by size/age", spec §4): `prune_old_logs(dir, max_age_days)` sweep at handler install, OSError-tolerant in the `EvidenceWriter.prune` style, always sparing `restarts.jsonl` (the ops audit trail). `apply_overrides(data_dir=…)` rebases `logging.file.dir` and `lock.path`.
*Rejected:* `TimedRotatingFileHandler` (no size bound — disk safety first); custom handler reusing EvidenceWriter (more code than stdlib for the same result). *Cost:* dashboard/calibrate/selftest sessions log to stderr only.

**D4 — Service shape: `palletscan supervise` (Python, in-package) + Task Scheduler as boot-starter; no NSSM.**
Task Scheduler alone cannot meet the requirements: `-RestartInterval` has a hard **1-minute minimum**, `-RestartCount` is bounded, and only the *last* run result is recorded — exit codes per restart are not countable. NSSM is a third-party exe (InfoSec posture: Python + pip only). So: a ~150-line supervisor as a new CLI subcommand (`palletscan supervise [opts] -- run [run-args]`) restarts the child on **any nonzero exit** in ~5 s and appends one JSONL line per child exit to `<data-dir>/logs/restarts.jsonl` — `{"ts", "exit_code", "runtime_s", "delay_s", "reason"}` — making escalations (code 3) vs crashes (code 1) a one-liner to count. Task Scheduler's only jobs: start the supervisor at logon, and restart the *supervisor* if it ever dies (PT1M is fine for that rare case). Being a subcommand (not a tools/ script) makes the restart/backoff/stop logic unit-testable cross-platform on macOS.
*Rejected:* Task Scheduler native restart (above); NSSM (InfoSec). *Cost:* one more long-lived process; ~150 lines to own.

**D5 — Two locks: supervisor and child each hold their own.**
Supervisor holds `<data-dir>/palletscan.supervisor.lock` (prevents two supervisors); the child `run` holds `<data-dir>/palletscan.lock` as in D2 (prevents a manual run racing the service). Lock handles do not leak into children (PEP 446: fds/handles are non-inheritable by default).
*Rejected:* a single shared lock (impossible — the child must own its own); supervisor-only (a manual `palletscan run` could then race the supervised child on the same sinks). *Cost:* none beyond D2's.

**D6 — Restart policy: any nonzero exit restarts, including 2 (usage), with crash-loop backoff.**
Per the owner's wording ("restart on any nonzero exit") and the existing cli.py docstring promise. Exit 2 (config error) logs a loud "fix the config; will pick it up on the next retry" line but still restarts — a station must come back by itself once ops fixes the file. Backoff: 5 s base; if the child ran < 60 s, double up to a 300 s cap; reset after a stable run. Clean exit 0 ends supervision (intentional stop).
*Rejected:* halt on exit 2 (leaves the station down after a typo'd config is fixed). *Cost:* a permanently bad config burns one spawn per 300 s (and rotation caps the log noise).

**D7 — Stop channel: stop-file primary, signals secondary; SIGTERM/SIGBREAK handlers added to the child (spec §5).**
`Stop-ScheduledTask` hard-terminates the tree, and console-ctrl events cannot cross Windows sessions — an operator's PowerShell cannot signal the hidden-console supervisor. So: `deploy/stop_palletscan.ps1` writes `<data-dir>/supervisor.stop`; the supervisor polls it (0.5 s), sends CTRL_BREAK (Windows; child is spawned with `CREATE_NEW_PROCESS_GROUP`, sharing the console) / SIGTERM (POSIX) to the child, waits a 15 s grace, then kills; deletes the stop-file; exits 0. Correspondingly, `_install_sigint` becomes `_install_stop_signals`: same first-signal-drains/second-forces behavior, registered for SIGINT + SIGTERM (POSIX) + SIGBREAK (Windows) — this also closes spec §5's "graceful SIGTERM/CTRL+C" gap. A stop-file found at supervisor startup is removed and ignored (friendlier than refusing to start). `Stop-ScheduledTask` stays documented as the hard-stop fallback (crash-only design tolerates it: SQLite/outbox are durable, evidence pruning is race-tolerant).
*Rejected:* signals-only (cannot cross sessions/consoles on Windows). *Cost:* ≤ 0.5 s stop latency; one magic file to document.

**D8 — Service identity: dedicated station user + AtLogOn trigger + OS auto-logon (netplwiz); not SYSTEM.**
UVC capture via OpenCV under session 0 is a known failure mode (Windows camera frame server + per-user privacy consent gate desktop-app camera access); it cannot be verified before hardware. The task runs as the logged-on station user; auto-logon configured with built-in `netplwiz` (no third-party Autologon.exe). ARRIVAL_CHECKLIST gains a step: confirm camera capture inside the scheduled task's session.
*Rejected:* SYSTEM / "run whether user is logged on or not" (cleaner ops, but risks a blind station on day one). *Cost:* the station stays logged in — kiosk posture; RUNBOOK notes physical security.

**D9 — Demo: `tools/demo.py` + `config/demo.yaml`; no Makefile.**
`SyntheticConfig.realtime: true` already exists (config.py:133; sleep-paced frames, live drop-oldest semantics) — the demo needs zero pipeline changes. `demo.yaml`: realtime synthetic, ~10–15 min of paced passes, `web.enabled: true`, console sink off. `tools/demo.py` spawns `[sys.executable, "-m", "palletscan", "synth", "--ab", "--dashboard", "--config", "config/demo.yaml", "--data-dir", "data/demo"]`, polls `/stats.json` until 200, opens the browser (`webbrowser` stdlib), waits on the child, propagates its exit code; Ctrl-C reaches the foreground process group and drains gracefully through the existing handlers. Flags `--no-browser` and `--max-seconds N` make it smoke-testable. `make` doesn't exist on the Windows target — `python tools/demo.py` is the spec's "or equivalent".
*Rejected:* Makefile (no make on target); `palletscan demo` subcommand (runtime surface for a dev/demo artifact). *Cost:* none.

**D10 — CPU method (spec §11): replay of a dense recorded burst clip at 1.0× speed, child sampled via psutil.**
Replay, not realtime synthetic: `VideoFileSource` paces on an absolute schedule (no per-frame sleep drift) and its MJPG-decode-per-frame cost is the closest proxy for live MJPEG UVC ingest, while synthetic rendering cost doesn't exist in production. Burst clip recorded via the existing `record_synthetic_clip` seam with idle gaps tightened to ~0.2–0.8 s → ~50 passes/min ≈ **7× the 7/min average** — a defensible "much higher burst". `tools/measure_cpu.py`: (a) baseline — one replay child, dashboard off; (b) realistic station — **two replay children** (A/B approximation, separate `--data-dir`s so locks don't collide) with the dashboard on and one live MJPEG client (streaming GET on `/live/<id>`), since §11 requires "dashboard functional throughout". Sample each child `psutil.Process.cpu_percent()` at 1 Hz for ≥ 5 min; report avg/p95/max, raw (sum-over-cores %) and normalized to a 4-core budget (raw/4). psutil stays a [dev] extra (tools-only). Method + dev-Mac numbers recorded in ASSUMPTIONS; the binding factory-box measurement is an ARRIVAL_CHECKLIST step (it already has a CPU step — extend it to use this tool). Production note recorded: real A/B runs one StationRunner process, marginally cheaper than two processes.
*Rejected:* synthetic realtime in-process (render cost pollutes; sleep pacing drifts); unpaced replay (that's the soak's job; 212% avg CPU at ~267 fps is not "realistic burst"). *Cost:* dev-Mac numbers are indicative; the factory box is authoritative.

**D11 — Adaptive soak warmup: include (it's cheap, ~60 lines + 4 tests).** *(owner flagged optional)*
`detect_warmup(samples, window_s=60, slope_thresh_mb_per_min=2.0, min_warmup_s=45, max_warmup_frac=0.5)` in tools/soak.py: smallest t where the least-squares slope over `[t, t+window_s]` drops below threshold, clamped to bounds. `--warmup-s` default becomes None = adaptive (explicit value still honored — the 2 h run's gates are unchanged); verdict gains `warmup_used_s` so runs stay comparable. A genuine leak never plateaus → warmup hits the max bound → the existing slope gate fails, as it should. soak_short drops its hard-coded `--warmup-s 90` and becomes portable to the Windows box (ASSUMPTIONS #39 amended, not replaced).
*Rejected:* per-OS fixed warmup table (guesswork against unknown Windows reclaim behavior). *Cost:* ~60 lines; one idle-machine soak_short re-run to validate.

**D12 — Add `palletscan/__main__.py` (4 lines).**
`python -m palletscan` fails today. Supervisor/demo/CPU tools spawn children as `[sys.executable, "-m", "palletscan", …]` — immune to PATH/venv-Scripts drift, identical on both platforms.
*Rejected:* resolving the console-script path per platform. *Cost:* none.

**D13 — Event sinks stay unbounded by design; documented, not rotated.**
`data/events.jsonl` and `palletscan.db` are the data of record (audit trail / dashboard source), unlike diagnostic logs. Spec §4's "auto-prune evidence and logs" is satisfied by evidence caps (Phase 1) + log rotation (D3); the outbox already has size/age caps. RUNBOOK documents observed growth rates and a manual archival procedure (stop service → move file → start).
*Rejected:* rotating events.jsonl (rotating the audit record defeats its purpose; SQLite can't be "rotated" without a retention feature nobody asked for — spec §12: no speculative features). *Cost:* ops owns archival on whatever cadence the trial needs.

---

## 1. Verified facts the plan builds on (checked against Phase 4 HEAD, 8a315c4)

- `logging_setup.py` (42 lines): one stderr `StreamHandler` + `JsonFormatter` (ts/level/logger/msg/exc/stats); `setup_logging(level)` idempotent; called at 6 cli.py sites + tools/soak.py + tools/record_synthetic.py. No file handler anywhere; module docstring defers rotation to Phase 5.
- `LoggingConfig` (config.py:339) has only `level`. `_StrictModel` is `extra="forbid"` — config additions need config.py + `config/default.yaml` together. `apply_overrides(data_dir=…)` (config.py:612–647) is the established path-rebase seam.
- Exit codes (cli.py:8–10): 0 clean / 1 software / 2 usage / **3 watchdog escalation**, and the docstring already promises "the supervisor must restart the process on any nonzero exit". `_exit_code_for` (cli.py:273–279) maps `exc.__cause__ is WatchdogEscalation → 3`; station.py:225–227 chains causes so escalation survives A/B mode. **4 is free.**
- `_install_sigint` (cli.py:261–270): SIGINT only; first signal → `runner.stop()`, second → default handler. No SIGTERM/SIGBREAK — spec §5 gap closed by D7. `signal.signal` calls already run on the main thread (cli command functions).
- Crash-only architecture confirmed: no in-process thread restart; any thread death → `run()` raises → process exit; WatchdogSource handles per-camera reconnect in-process; escalation valves (zombie readers > max, outage > max) raise `WatchdogEscalation`.
- **Finding 17 / ASSUMPTIONS #50:** `DashboardServer.start()` after `stop()` raises `DashboardServerError` (web/server.py:68–77) — guarded deliberately; #50 says restart machinery "belongs with Phase 5". Process-granularity restart (D4) satisfies it **without touching server.py**: every restarted process constructs a fresh DashboardServer. The guard stays.
- `_cmd_synth`/`_cmd_replay` do **not** hold the dashboard open after source exhaustion (`finally: dashboard.stop()`) — hence the demo uses a long realtime-paced synthetic run, not a hold-open hack.
- `SyntheticConfig.realtime: bool = False` exists (config.py:133; synthetic.py:100, 197) — sleep-paced frames, `live=True` drop-oldest queue semantics. Not CLI-exposed; `config/demo.yaml` sets it.
- `replay` supports `--speed` (1.0 = as-recorded pace, absolute-schedule pacing in video.py) and `--loop` (0 = forever) — the CPU tool's load generator exists.
- `palletscan/__main__.py` does **not** exist (D12).
- soak: `RssSampler` + `analyze_rss(warmup_s, …)` in tools/soak.py (least-squares slope + final/baseline ratio); soak_short (tests/test_flaky_and_soak.py:116–176) passes `--warmup-s 90` explicitly; 6 min / 8 MB/min / 1.3× gates per ASSUMPTIONS #39. ASSUMPTIONS #28 defers the §11 CPU measurement to Phase 5 verbatim.
- pyproject: entry point `palletscan = palletscan.cli:main`; [dev] already has psutil, pytest, mypy, httpx. 324 test functions; markers `acceptance`, `soak_short`; fast suite ~25 s.
- Windows specifics validated during design: msvcrt byte-range locks are mandatory (hence D1's offset trick); `subprocess` handles are non-inheritable by default (PEP 446); CTRL_BREAK needs `CREATE_NEW_PROCESS_GROUP` + shared console and is the only ctrl event deliverable to a child group (a grouped child can't reliably get CTRL_C — hence SIGBREAK handler in D7); Task Scheduler RestartInterval min = PT1M; `Get-Content -Wait` on a rotating log can make rollover's rename fail transiently (handler degrades by continuing in the same file — RUNBOOK says copy, don't tail).

The 7 deferred cleanups (REVIEW_7e4c22c lines 138–153) are out of scope and none of this plan's changes touch their sites beyond the mechanical lock-indentation in cli.py (which deliberately does *not* consolidate the quadruplicated dashboard-lifecycle blocks — cleanup #1 stays deferred).

---

## 2. Architecture

```
Task Scheduler (AtLogOn, station user, restart-supervisor-on-failure PT1M)
  └─ palletscan supervise --data-dir D -- run --config config.yaml
       • holds D/palletscan.supervisor.lock          (D5)
       • logs to stderr + D/logs/supervisor.jsonl     (own file — no rotation races)
       • appends D/logs/restarts.jsonl                (countable exit codes, D4/D6)
       • polls D/supervisor.stop                      (D7)
       └─ python -m palletscan run …                  (fresh process per restart → finding 17 respected)
            • holds D/palletscan.lock                 (D1/D2; exit 4 on contention)
            • stderr JSON + rotating D/logs/palletscan.jsonl  (D3, single writer by lock)
            • SIGINT/SIGTERM/SIGBREAK graceful drain  (D7)
            • exits 0/1/2/3 as today
```

New modules: `palletscan/reliability/instance_lock.py` (`InstanceLock`, `InstanceLockHeld`, `hold_instance_lock()` ctx manager), `palletscan/reliability/supervisor.py` (`Supervisor` with injectable spawn/clock/sleep seams, `stop_child_gracefully()`, backoff policy, restarts-JSONL appender), `palletscan/__main__.py`.
Changed: `logging_setup.py` (+`add_rotating_file_handler`, `prune_old_logs`; `setup_logging` signature untouched — all 8 call sites unchanged), `config.py` (+`LogFileConfig`, +`LockConfig`, `apply_overrides` rebases), `cli.py` (docstring exit 4; `_install_stop_signals`; lock + file-handler block in `_cmd_run`/`_cmd_synth`/`_cmd_replay`; `supervise` subcommand), `config/default.yaml`, `tools/soak.py` (D11).
New ops/dev artifacts: `deploy/install_service.ps1`, `deploy/uninstall_service.ps1`, `deploy/start_palletscan.ps1`, `deploy/stop_palletscan.ps1`, `tools/measure_cpu.py`, `tools/demo.py`, `config/demo.yaml`, `RUNBOOK.md`.

Setup order inside each writer command (run/synth/replay): config load → overrides → `setup_logging` (stderr first, so lock failures are logged) → **acquire lock (exit 4 on contention, before any camera/sink is touched)** → install rotating file handler + age prune → construct runner → install stop signals → dashboard → run → `finally:` release lock. The lock wrap is a `with hold_instance_lock(…)` — the body re-indents; that mechanical diff is isolated in its own commit (Step 3) for reviewability.

---

## 3. Build order (commit-sized steps; tests green at every gate)

### Step 1 — Log rotation plumbing (no CLI wiring yet)
`LogFileConfig` + `LoggingConfig.file` + `LockConfig` + `AppConfig.lock` + `apply_overrides` rebase + `config/default.yaml` keys; `add_rotating_file_handler(cfg, filename="palletscan.jsonl")` (idempotent, `delay=True`, utf-8, JsonFormatter) + `prune_old_logs(dir, max_age_days)` (spares `restarts.jsonl`, OSError-tolerant) in logging_setup.py.
*Tests* (`tests/test_logging_rotation.py` + extend `tests/test_config.py`): file handler writes parseable JSONL; tiny `max_mb` forces rollover and total bytes ≤ cap with backups counted; idempotent install; age prune deletes old, keeps young + restarts.jsonl; `apply_overrides` rebases `logging.file.dir` and `lock.path`; validator bounds (max_mb > 0, backups ≥ 1).

### Step 2 — Graceful-stop signals (spec §5 gap)
`_install_sigint` → `_install_stop_signals` (SIGINT + SIGTERM where defined + SIGBREAK where defined; same drain-then-force contract); rename at the three call sites.
*Tests* (extend `tests/test_cli_run.py`): registration asserted via monkeypatched `signal.signal`; POSIX end-to-end — synth subprocess receives SIGTERM → exits 0 with summary printed. SIGBREAK delivery is Windows-only → ARRIVAL_CHECKLIST.

### Step 3 — Instance lock + exit code 4 + CLI wiring
`instance_lock.py` (D1 semantics, platform branches, holder JSON, offset trick, unlock-before-close, never unlink); cli.py docstring; lock-then-file-handler block in the three writer commands (`dashboard`/`calibrate`/`selftest` untouched).
*Tests* (`tests/test_instance_lock.py`): acquire/release/re-acquire; second acquire in the same process fails (valid on both platforms — flock is per open-file-description, msvcrt per handle); holder JSON readable while locked; subprocess holder hard-killed → parent re-acquires (the stale-lock proof); `main(["run", …])` returns 4 while held, message names the holder; dashboard command takes no lock.

### Step 4 — `__main__.py` + supervisor + `supervise` subcommand
`palletscan/__main__.py`; `reliability/supervisor.py` (D4–D7 semantics; spawn `[sys.executable, "-m", "palletscan", *child_args]`, `CREATE_NEW_PROCESS_GROUP` on win32, **no pipes** — inherited stderr avoids fill-deadlock); `supervise` parser (`--data-dir`, `--grace-s 15`, `--backoff-base-s 5`, `--backoff-cap-s 300`, `--stable-after-s 60`, `argparse.REMAINDER` child args validated to start with run/synth/replay); supervisor acquires its own lock, installs stderr + `supervisor.jsonl` logging, registers stop signals.
*Tests* (`tests/test_supervisor.py`): with fake spawn/clock — restarts on nonzero; exit 0 ends supervision; backoff doubles on fast crashes, caps, resets after stable run; restarts.jsonl lines countable by exit_code (a code-3 line distinguishable from code-1); stop-file → graceful stop, `reason: "stop-requested"`, file removed, exit 0; loud log on exit-2 child. With real children (`sys.executable -c "import sys; sys.exit(3)"`): codes recorded faithfully; `python -m palletscan version` works; second supervisor on same data-dir exits 4.

### Step 5 — Adaptive soak warmup (D11; drop if owner rules it out)
`detect_warmup()` in tools/soak.py; `analyze_rss(warmup_s=None → adaptive)`; verdict + report gain `warmup_used_s`; soak_short drops `--warmup-s 90`.
*Tests* (extend `tests/test_flaky_and_soak.py`): ramp-then-plateau curve → warmup lands at the knee; monotone growth → max bound (and slope gate still fails); noisy flat → min bound; adaptive default matches explicit warmup on a plateau curve. Gate: one `pytest -m soak_short` on an idle machine (known load-flaky — full-suite-on-idle only; reproduce on HEAD before blaming new code).

### Step 6 — CPU measurement (D10) + recorded results
`tools/measure_cpu.py`: record burst clip (tight idle gaps) via `record_synthetic_clip` seam → spawn replay child(ren) at `--speed 1.0 --loop 0` → 1 Hz psutil sampling ≥ 5 min → markdown report (avg/p95/max, raw and /4-core). Scenarios: 1-cam baseline (no dashboard) and 2-cam + dashboard + 1 MJPEG client. Summarization in pure functions.
*Tests* (`tests/test_demo_and_cpu_tools.py`): summary math (avg/p95/max, 4-core normalization); burst-config idle-range tightening. Then **run it on the dev Mac and record method + numbers in ASSUMPTIONS (#52+)**; extend ARRIVAL_CHECKLIST's CPU step to re-run this tool on the factory box.

### Step 7 — Demo (D9)
`config/demo.yaml` (realtime A/B synthetic, web enabled, console sink off) + `tools/demo.py` (`--no-browser`, `--max-seconds`).
*Tests* (same module): demo.yaml loads strict-valid with realtime+web asserted; poll-until-ready logic against a fake probe; smoke test `demo.py --no-browser --max-seconds 20` end-to-end (child spawns, stats endpoint 200s, clean shutdown).

### Step 8 — RUNBOOK.md + deploy/ scripts + close-out
`deploy/*.ps1` (Register-ScheduledTask with AtLogOn trigger + station user + `ExecutionTimeLimit` zero + supervisor-restart settings, parameterized venv/config/data paths; uninstall; start; stop-file writer). `RUNBOOK.md` written for a non-author (the stand-up-from-zero test), sections: prerequisites + install from zero (python.org 3.11+, venv, `pip install .`, wheel notes for pyzbar/pylibdmtx DLLs); config bootstrap; calibrate workflow + selftest preflight (pointer to ARRIVAL_CHECKLIST); service install incl. netplwiz auto-logon (D8) + physical-security note; start/stop/restart procedures (stop-file semantics, hard-stop fallback); **exit-code table 0/1/2/3/4 + the escalation-counting one-liner** (`Get-Content data\logs\restarts.jsonl | ConvertFrom-Json | Where-Object exit_code -eq 3 | Measure-Object`); locations table (rotating logs, restarts.jsonl, evidence, events.jsonl, palletscan.db, outbox.db, lock files, demo data dir); recovery procedures (camera unplug → watchdog self-heals; repeated exit 3 → reseat cable/hub, count via restarts.jsonl; disk full; dashboard port in use; exit 4 "already running"; stale stop-file; log-tailing caveat); event-sink archival (D13); demo. README quickstart + exit-code table updates; ASSUMPTIONS #52+ (lock semantics + offset trick; rotation single-writer rule; supervisor policy incl. restart-on-2 ruling; service-as-user rationale; CPU method + dev-Mac results; #39 amendment; D13); ARRIVAL_CHECKLIST "Phase 5 ops" additions (task-session camera capture, CTRL_BREAK stop end-to-end, exit-4 message after taskkill, factory CPU run, Windows soak_short).
Final gates: full `pytest` (expect ~365+), `pytest -m soak_short` (idle machine), `mypy palletscan tools`, `python tools/demo.py --no-browser --max-seconds 30`.

---

## 4. Verification matrix (criterion → proof)

| Criterion | Proof |
|---|---|
| Structured JSON logs with rotation (spec §5) | rotation-cap + JSONL-format tests; artifact `data/logs/palletscan.jsonl(.N)` after any run |
| Logs pruned by size **and** age (spec §4) | size: rotation cap test; age: `prune_old_logs` test (spares restarts.jsonl) |
| Single-instance lock + defined stale-lock behavior | same-process contention test; **subprocess hard-kill → parent re-acquires** (stale lock impossible by construction); `run` returns 4 while held |
| Graceful SIGTERM/CTRL+C (spec §5) | POSIX SIGTERM end-to-end test (exit 0, flushed summary); SIGBREAK registration test; Windows delivery → ARRIVAL_CHECKLIST |
| Restart on any nonzero exit; escalations countable | supervisor restart/backoff tests; restarts.jsonl exit_code-countability test; real-child exit-3 recorded test; RUNBOOK one-liner |
| Finding 17 respected | supervisor restarts whole processes; server.py untouched; existing single-use guard test stays green |
| Windows service install, pip-only InfoSec | `deploy/*.ps1` (Task Scheduler, no NSSM); executed on the box per ARRIVAL_CHECKLIST |
| RUNBOOK stands a colleague up from zero | section checklist in Step 8 reviewed against the owner's test; dry-run at hardware arrival |
| §11 CPU ≤ ~50% of 4 cores under burst | `tools/measure_cpu.py` method + dev-Mac results in ASSUMPTIONS; factory re-run via ARRIVAL_CHECKLIST |
| End-to-end demo on synthetic input, dashboard opens | `python tools/demo.py`; CI-safe smoke test (`--no-browser --max-seconds 20`) |
| Adaptive warmup portable soak (optional D11) | 4 `detect_warmup` unit tests + one idle-machine soak_short run |
| Suite green at every gate | full `pytest` + `mypy` per step |

---

## 5. Out of scope (per spec §12 and owner instruction)

- The 7 deferred cleanups from REVIEW_7e4c22c (dashboard-lifecycle contextmanager, JPEG cache, SQL filtering, _ListSink gating, MissEvent revision field, reconciliation render dedup, reemits counter) — untouched, including while re-indenting cli.py.
- In-process DashboardServer restartability — D4's process-granularity restart makes it unnecessary; the finding-17 guard stays.
- NSSM, auto-selftest-on-run, auth, event-sink rotation (D13), Makefile, any new pipeline features.
- Windows-only behaviors not verifiable on macOS (CTRL_BREAK delivery, msvcrt contention messages, Task Scheduler registration, task-session camera capture, Windows soak warmup) — designed conservatively, then verified on the factory box via the new ARRIVAL_CHECKLIST section; RUNBOOK covers the operator-facing halves.
