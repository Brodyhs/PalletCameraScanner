# PalletScan RUNBOOK

Operating manual for the PalletScan station: install from zero on the
Windows factory PC, run it as an auto-starting, self-restarting service,
and recover it when something goes wrong. Written for an operator who did
not build the system. (macOS works for development; this runbook targets
the Windows 10/11 station.)

The process tree in production:

```
Task Scheduler task "PalletScan"  (AtLogOn, station user; restarts the
  │                                supervisor itself if it dies, PT1M)
  └─ palletscan supervise --data-dir D -- run --config station.yaml
       • holds D\palletscan.supervisor.lock      (one supervisor per data dir)
       • writes D\logs\supervisor.jsonl           (its own rotating log)
       • appends D\logs\restarts.jsonl            (one line per child exit)
       • polls D\supervisor.stop                  (the graceful stop channel)
       └─ python -m palletscan run …              (restarted in ~5 s on any
            • holds D\palletscan.lock              nonzero exit, with
            • writes D\logs\palletscan.jsonl       crash-loop backoff)
```

---

## 1. Prerequisites

- Windows 10/11 desktop, ≥ 4 cores, ≥ 8 GB RAM, ≥ 20 GB free disk.
- Python **3.11+** from [python.org](https://www.python.org/downloads/)
  (check *"Add python.exe to PATH"* in the installer). No other software:
  the InfoSec posture is Python + pip packages only — no vendor SDKs, no
  drivers, no NSSM.
- The two USB cameras are standard UVC devices; plug them in, no driver
  install. Prefer separate USB 3 root hubs/ports (bandwidth).
- This repository, e.g. at `C:\palletscan`.

## 2. Install from zero

In PowerShell:

```powershell
cd C:\palletscan
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,zxing]"
palletscan version        # prints 0.1.0
```

Notes:

- **The `[zxing]` extra is required for the station config**:
  `config\station.yaml` runs `decode.engine: zxing` (zxing-cpp), which a
  plain `pip install .` does NOT pull in — without it the station fails
  at startup with a message pointing here. `[dev]` adds the test/dev
  extras (`pytest`, `mypy`, `psutil`, `httpx`); install both as shown.
- On Windows the `pyzbar` and `pylibdmtx` wheels **bundle their native
  DLLs** — nothing extra to install. If `pyzbar` fails to import with a
  DLL error on an unusual box, install the
  [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
  and retry.
- The install pulls `pygrabber` automatically on Windows (DirectShow
  device names, and the capture backend for the mono camera — §4).

Sanity check with zero hardware:

```powershell
palletscan synth --passes 5 --seed 7
```

You should see a run summary with 5 passes accounted for.

## 3. Configuration bootstrap

Copy the commented reference and edit:

```powershell
copy config\default.yaml config\station.yaml
```

The keys you will actually touch on day one: `cameras[]` (filled by
calibrate below), `source.type: camera`, `source.cameras: [cam-color,
cam-mono]` for the A/B trial (or `source.camera` for one), `web.enabled:
true` for the dashboard, and `report.manifest_path` if a manifest CSV is
used. Everything else has working defaults. Unknown keys are rejected —
typos fail loudly at startup (exit 2).

## 4. Calibrate, then selftest (before first service start)

**Stop the service before calibrating** (calibrate needs the cameras;
the running pipeline owns them).

```powershell
palletscan calibrate --list                       # device names
# First-ever calibration of each camera: --name creates the entry
# (--camera alone only selects an EXISTING cameras[] entry).
palletscan calibrate --camera cam-color --name "<device-name substring>" --save --config config\station.yaml
palletscan calibrate --camera cam-mono  --name "<device-name substring>" --save --config config\station.yaml
# Re-calibration later (the entries exist now):
palletscan calibrate --camera cam-color --save --config config\station.yaml
palletscan selftest --config config\station.yaml --data-dir C:\palletscan\data  # must be fully green
```

Pass the same `--data-dir` the service uses: the disk-space gate probes
the volumes the station actually writes to, and the decode check's
scratch outputs land in an isolated `<data-dir>\selftest\` subtree (never
the production evidence).

Work through **ARRIVAL_CHECKLIST.md** the day the cameras arrive — it is
the authoritative list of hardware claims to verify, in dependency order.
`selftest` is the refuse-to-run-blind gate: it enumerates the cameras,
verifies achieved fps, decodes bundled known-good symbols through the full
pipeline, and checks disk space.

### The mono camera (37CUGM) is a pygrabber device

The See3CAM_37CUGM exposes only Y8/Y12 mono formats, which OpenCV cannot
read on Windows (MSMF opens it but never delivers a frame; DSHOW won't
open it at all). Its `cameras[]` entry therefore uses
`backend: pygrabber` — a DirectShow SampleGrabber graph driven by the
already-installed `pygrabber` pip package; no vendor SDK, no driver.
`calibrate` and `selftest` dispatch by backend, so both commands work
for the mono exactly like the color camera (earlier builds could not
calibrate it — fixed in the dc7c3d9 session). Expect **~63 fps
measured** at Y8 2064×1552 against the 72 fps datasheet maximum; that
is normal for this grab path, not a fault.

**Keep OBS Virtual Camera (and any other virtual camera) closed** on the
station: virtual cameras enumerate alongside the real ones and can shift
the 24CUG's pinned MSMF `fallback_index`, capturing the wrong device.
The identity guard (`cameras[].identity`, `policy: warn`) logs such
drift and bumps `source.connect_mismatches`; flip to `strict` to refuse
the wrong device once the USB topology is final (re-stamp the
fingerprint after any deliberate port move).

## 5. Install as a service (Task Scheduler + supervisor)

```powershell
# elevated PowerShell
cd C:\palletscan\deploy
.\install_service.ps1 -RepoDir C:\palletscan `
    -ConfigPath C:\palletscan\config\station.yaml `
    -DataDir C:\palletscan\data
```

What this registers: an **AtLogOn** task running `palletscan supervise`
as the **interactive station user**. The supervisor restarts the pipeline
child on any nonzero exit in ~5 s (crash-loop backoff up to 300 s) and
logs every child exit; Task Scheduler's only jobs are starting the
supervisor at logon and restarting the *supervisor* if it ever dies.

**Auto-logon (required for unattended reboot recovery).** The task needs
the station user's interactive session because Windows gates desktop
camera access per user (capture under session 0/SYSTEM is a known failure
mode). Configure OS auto-logon with the built-in tool:

1. `Win+R` → `netplwiz`
2. Select the station user, untick *"Users must enter a user name and
   password to use this computer"*, Apply, enter the password.

> **Physical security note:** the station stays logged in (kiosk
> posture). Keep the PC in a locked cabinet/room; the dashboard binds
> 127.0.0.1 and has no auth by design.

After install: reboot once and confirm the station comes up scanning
(dashboard reachable, `restarts.jsonl` shows no churn).

## 6. Start / stop / restart

```powershell
deploy\start_palletscan.ps1                          # start now (re-arms a stopped station)
deploy\stop_palletscan.ps1                           # verified stop
deploy\stop_palletscan.ps1; deploy\start_palletscan.ps1   # restart
```

Both scripts derive the data dir from the registered task's `--data-dir`
argument, so the bare invocations above always act on the right
directory (pass `-DataDir` only to override).

How the stop works: the script writes `<data-dir>\supervisor.stop`; the
supervisor notices within 0.5 s, sends CTRL_BREAK to the child, gives it
15 s to drain its queues (events are flushed, open motion segments
become misses — nothing is silently dropped), and exits 0. The script
then **verifies** the station is dead by probing both instance locks
(the OS releases them when their holders die, however they die) and
prints "stopped" only when both are free — it never infers success from
the stop-file alone.

The stop-file is a **sticky latch**: the supervisor never deletes it,
and any supervisor starting while it exists honors it (exits without
scanning). The station therefore STAYS stopped through Task Scheduler
restarts and reboots until `start_palletscan.ps1` removes the latch —
safe for maintenance windows. Don't delete the file by hand mid-stop;
start the station with the start script.

Hard stop fallback (`-Hard`, or automatic after the graceful timeout):
stops the scheduled task (the supervisor's kill-on-close job object
takes the pipeline child with it — best-effort) AND explicitly kills the
writer-lock holder, covering orphans; then re-verifies via the lock
probes. Durable state survives (SQLite and the outbox are crash-safe;
evidence pruning tolerates races), but in-flight queue contents are
dropped. If the script reports the station is STILL RUNNING, believe it:
do not archive or edit the data dir until the named holder pid is dead.

A manual foreground run (e.g. for debugging) is
`palletscan run --config config\station.yaml` — but note the instance
lock: if the service is running on the same data dir, the manual run
exits **4** and tells you who holds the lock. Stop the service first, or
use a different `--data-dir`.

**Stopping an unsupervised writer (`run`/`synth`/`replay`) without a
console**: create `<data-dir>\palletscan.stop` (next to the instance
lock), e.g.

```powershell
New-Item C:\palletscan\data\palletscan.stop
```

The writer notices within ~0.25 s and drains gracefully — queues
flushed, open segments finalized, the same run summary as Ctrl-C — no
console required (CTRL_BREAK needs a shared console, which services and
captured-output shells often lack). Like `supervisor.stop`, this latch
is **sticky**: nothing deletes it for you, and a writer started while it
exists drains immediately at startup. **Delete the file before the next
run.** The supervised service uses `supervisor.stop` via the scripts
above; `palletscan.stop` is the channel for manual runs and tools.

## 7. Exit codes and counting failures

| code | meaning | supervisor reaction |
|---|---|---|
| 0 | clean exit (intentional stop) | supervision ends |
| 1 | software failure — check logs | restart |
| 2 | usage/config error — fix the config file | restart (loud log; picks up the fixed file on retry) |
| 3 | watchdog escalation — USB stack wedged; check cable/hub | restart (a fresh process resets the stack) |
| 4 | another instance holds the lock | restart with backoff until the other instance stops |

Every child exit appends one JSON line to `logs\restarts.jsonl`:
`{"ts", "exit_code", "runtime_s", "delay_s", "reason"}`. A line with
`exit_code: null` and reason `stop-honored-at-startup` records a
supervisor that started while the stop latch was present and honored it
(no child ran). Count watchdog escalations (how often the USB stack
wedged) without log diving:

```powershell
Get-Content C:\palletscan\data\logs\restarts.jsonl |
  ConvertFrom-Json | Where-Object exit_code -eq 3 | Measure-Object
```

A healthy station has a near-empty `restarts.jsonl`. Repeated exit-3
lines → reseat the camera cable / move to another USB port or hub.
Repeated exit-2 lines → the config file is broken; the loud
`fix the config` line in `supervisor.jsonl` says so.

## 8. Where everything lives

Default data dir `C:\palletscan\data` (everything below is per
`--data-dir`, so a second station on the same box just uses another dir):

| path | what | growth/caps |
|---|---|---|
| `logs\palletscan.jsonl(.1-.5)` | pipeline diagnostics (rotating) | capped: 20 MB × 6 files; > 14-day files pruned at startup |
| `logs\supervisor.jsonl(.N)` | supervisor diagnostics (rotating) | same caps |
| `logs\restarts.jsonl` | one line per child exit (audit trail) | tiny; **never auto-pruned** |
| `events.jsonl` | every pass/miss event (data of record) | unbounded by design — see §10 |
| `palletscan.db` | events in SQLite (dashboard + reports read this) | unbounded by design — see §10 |
| `evidence\<camera>\<day>\...` | JPEG bursts for missed passes | capped: 500 MB / 14 days, auto-pruned |
| `outbox.db` | store-and-forward queue for the HTTP sink | capped: 200 MB / 14 days |
| `palletscan.lock`, `palletscan.supervisor.lock` | instance locks; content = holder diagnostics JSON (pid/start/argv) | persist after exit (harmless last-holder info; never delete while running) |
| `supervisor.stop` | stop latch ("this station should be stopped") | sticky: never deleted by the supervisor; removed by `start_palletscan.ps1` |
| `palletscan.stop` | writer-level stop latch (drains an unsupervised `run`/`synth`/`replay` — §6) | sticky: never deleted by the writer; delete it by hand before restarting |
| `data\demo\` | demo runs (`tools/demo.py`) | disposable |

**Log-tailing caveat:** don't hold rotating logs open —
`Get-Content -Wait` on `palletscan.jsonl` can make the rollover rename
fail (the handler then keeps writing to the same file past its size cap
until the reader lets go). Copy the file and read the copy.

## 9. Recovery procedures

**Camera unplugged / stalled** — no action needed: the watchdog detects
the stall (`watchdog.stall_timeout_s`, default 2 s), closes and reopens
by device *name* with backoff forever, and re-applies the calibrated
settings on every reconnect. Replug the cable; recovery is < 10 s and
logged (`source.reconnects` in the stats). If the image comes back washed
out, see ARRIVAL_CHECKLIST §6.

**Repeated exit 3 (escalations)** — the USB stack wedged hard enough
that only a process restart clears it, and it keeps happening: count via
the §7 one-liner, then reseat the cable, try a different port/hub, check
the hub's power. The station keeps self-healing meanwhile.

**Dashboard unreachable** — is the run alive? (`restarts.jsonl` churn?)
Port in use by something else exits 2 with a clean message in
`supervisor.jsonl`/stderr; change `web.port`. The dashboard binds
127.0.0.1 only — reach it from the station itself.

**Exit 4 "another instance holds the lock"** — a manual run and the
service are fighting over one data dir. The message names the holder
(pid, start time, argv). Stop one of them; the supervised child keeps
retrying with backoff and wins the lock as soon as it's free.

**`supervisor.stop` present, station won't start** — that is the stop
latch doing its job: a stop request stays honored through Task Scheduler
restarts and reboots (each honor leaves a `stop-honored-at-startup` line
in `restarts.jsonl`). Start the station with
`deploy\start_palletscan.ps1`, which removes the latch.

**Disk full / filling** — evidence and logs are capped and self-pruning;
the growers are `events.jsonl` and `palletscan.db` (§10). Archive them.
`selftest --data-dir <the service's data dir>` gates on the volumes the
station actually writes to. If the disk does fill anyway, the station
degrades loudly, not silently: misses are still emitted (flagged
`evidence_error`, counted in the dashboard's `misses.evidence_failures`)
even when their JPEG bursts cannot be stored, and the supervisor keeps
restarting the child even when its own `restarts.jsonl` is unwritable.

**Decode engine misbehaving (trial day) — revert to legacy** — the
station runs the zxing-cpp engine (`decode.engine: zxing` in
`station.yaml`). If it misreads, phantom-decodes, or stalls, fall back
to the Phases 1–5 certified pyzbar+pylibdmtx cascade:

```powershell
# edit config\station.yaml:  decode.engine: zxing  ->  decode.engine: legacy
deploy\stop_palletscan.ps1; deploy\start_palletscan.ps1
```

`legacy` needs no extra packages (safe even on a box installed without
the `[zxing]` extra) and is budget-bounded per decode call. Restore
`engine: zxing` the same way to undo. Note the read-rate/latency
difference in the trial log either way — the numbers are not comparable
across engines.

**Config typo after an edit** — the child exits 2, the supervisor logs
`fix the config` and retries forever; fix `station.yaml` and the next
retry (≤ 300 s) picks it up. Nothing to restart by hand.

**Box replaced / restored from backup** — repeat §2–§5; copy the old
data dir if the trial history matters. Lock files carry over harmlessly
(last-holder diagnostics only). A restored `supervisor.stop` is a live
stop latch: the station will not scan until `start_palletscan.ps1`
removes it — which is the right default for a box whose cameras may not
be plugged in yet.

## 10. Event sinks: growth and archival

`events.jsonl` and `palletscan.db` are the **data of record** (audit
trail, dashboard, A/B report, reconciliation) and are deliberately never
rotated — rotating the audit record would defeat its purpose. Expected
growth is modest: at the spec's 10k passes/day, roughly 5–10 MB/day for
`events.jsonl` and similar for the DB (events are ~0.5–1 KB each).

Archival procedure (monthly, or whatever the trial needs):

```powershell
deploy\stop_palletscan.ps1 -DataDir C:\palletscan\data
move C:\palletscan\data\events.jsonl C:\archive\events-2026-06.jsonl
move C:\palletscan\data\palletscan.db C:\archive\palletscan-2026-06.db
deploy\start_palletscan.ps1
```

Both files are recreated empty on the next start. To review an archived
trial later: `palletscan dashboard --data-dir C:\archive\...` (point a
copy of the layout at it).

Do not run `replay` (or `synth`) against the live station's data dir:
besides the instance lock making the loser exit 4, replayed pass rows
carry real wall stamps that a live `run` started within the dedup window
(~1 minute) could pick up as restart-dedup seeds and suppress a genuine
first sighting. Tools get their own `--data-dir`.

## 11. Demo

Full system on synthetic input — no hardware, opens the dashboard in a
browser, ~10–15 min of realtime-paced A/B passes:

```powershell
python tools\demo.py
```

`Ctrl-C` stops it gracefully (the run summary still prints). Smoke mode:
`python tools\demo.py --no-browser --max-seconds 30`.

## 12. Measurements (spec §11)

- CPU under burst: `python tools\measure_cpu.py` (5 min per scenario;
  report lands in `data\cpu\cpu_report.md`). Run it once on the factory
  box and file the report — the acceptance number is the *station*
  scenario's total normalized to 4 cores.
- Soak: `python tools\soak.py --hours 2 --mode replay` (memory flatness),
  or `pytest -m soak_short` for the 6-minute variant on an idle machine.
