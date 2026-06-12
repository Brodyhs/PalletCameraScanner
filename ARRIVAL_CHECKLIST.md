# Camera arrival checklist

Phase 3 is code-complete and unit-tested **against fakes** — no camera ever
touched this code. This checklist is the honest list of every claim that
needs hardware to verify, in dependency order, per camera
(**See3CAM_24CUG** color 1920×1200@120 and **See3CAM_37CUGM** mono
2064×1552@72; both UVC global shutter). Run it on the Windows factory PC
(the supported target); a macOS pass is informative only.

Setup: `pip install -e .` (pulls `pygrabber` on Windows automatically),
then work through the steps. Record findings inline in this file and
correct the code where reality disagrees — each step names the constant
to fix.

## 1. Enumeration by name

```
palletscan calibrate --list
```

- [ ] Both cameras appear. **Record the exact device-name strings** and
  adjust `cameras[].name` in your config if they differ from the
  datasheet names used in `config/default.yaml`'s commented examples.
- [ ] Verify the *list-order == CAP_DSHOW-index-order* assumption
  (`palletscan/sources/devices.py`): open each printed index with a
  trivial cv2 snippet and confirm the picture matches the name
  (point one camera at the ceiling). If order disagrees, fix
  `_list_windows()`.

## 2. Probe matrix (both backends)

```
palletscan calibrate --camera cam-color --seconds 3
palletscan calibrate --camera cam-mono  --seconds 3
```

- [ ] Run with `backend: dshow` and again with `backend: msmf` in the
  config; **paste both probe tables here**.
- [ ] Confirm the headline modes: 1920×1200@120 (UYVY or MJPG) on the
  24CUG; 2064×1552@72 (GREY/Y8) on the 37CUGM. If a mode is missing,
  extend `candidates_for()` in `palletscan/sources/probe.py`.
- [ ] Pick the backend per camera (better achieved fps / fewer quirks)
  and lock it via `calibrate --save`.

## 3. Exposure semantics (the QUIRKS table)

- [ ] With auto-exposure off, set two exposure values 2 stops apart;
  confirm **readback** matches *and* mean frame brightness moves
  (calibrate prints both — the `exposure effect` line).
- [ ] Record the working auto-exposure on/off magic values per backend.
  If they differ from the table (MSMF 0.75/0.25, DSHOW 1/0), correct
  `QUIRKS` in `palletscan/sources/controls.py`.
- [ ] Confirm whether DSHOW exposure is integer log2 stops on these
  cameras (`exposure_is_log2`, the 0.5-stop readback tolerance).

## 4. Settings round-trip

- [ ] `palletscan calibrate --camera <id> --no-auto-exposure --exposure <v> --gain <v> --save --config <file>`
- [ ] Re-run calibrate: the saved mode and settings load, apply, and
  verify (no MISMATCH lines).

## 5. Selftest gate

- [ ] `palletscan selftest --config <file>` is fully green, including the
  achieved-fps ≥ 0.85× check on both cameras.

## 6. Unplug/replug recovery (the watchdog's reason to exist)

- [ ] `palletscan run --config <file> --stats-interval 5`, then pull the
  USB cable mid-run and replug after ~10 s.
- [ ] Reconnect completes in **< 10 s after replug**; the log shows the
  stall/error detection, the backoff attempts, and the reconnect.
- [ ] `source.reconnects` increments in the stats line.
- [ ] **Brightness is unchanged after replug** — the UVC
  settings-reset-on-re-enumeration trap; if the image comes back washed
  out, settings re-apply in `CameraSource.reopen()` is not reaching the
  device.
- [ ] Repeat with the cable left out for 2+ minutes: backoff holds at
  15 s, memory flat, and a single replug still recovers.

## 7. 30-minute stability run

- [ ] fps stable at the locked mode (watch the stats line), zero stalls,
  zero drops beyond the expected burst behavior.
- [ ] CPU: run `python tools\measure_cpu.py` (the spec §11 method — burst
  replay + dashboard + MJPEG viewer, 5 min per scenario) and file
  `data\cpu\cpu_report.md`. **This factory-box run is the authoritative
  §11 number** (the dev-Mac figures in ASSUMPTIONS #56 are indicative);
  the *station total* row normalized to 4 cores must be ≤ ~50%.

## 8. Quirk watch-list (record anything observed)

- [ ] MSMF hung-read-on-release: does the zombie counter ever increment?
  (If the USB stack wedges hard, the process exits with code 3 — the
  supervisor must restart it.)
- [ ] MJPG compression artifacts vs decode rate at speed (compare decode
  counts per pass between UYVY and MJPG at the same fps).
- [ ] The 37CUGM's mono pixel layout: confirm which of Y8/Y16/YUV-wrapped
  arrives with `convert_rgb: false`, and that the luma plane is right —
  `to_gray` picks channel 1 for UYVY and channel 0 otherwise, derived
  from `cameras[].fourcc` (see `CameraSource._luma_channel`); if grays
  look like flat static, the plane choice is wrong for this device.
- [ ] Mode changes resetting controls: after `apply_mode`, do the
  settings still read back? (`apply_settings` runs after it on purpose.)

## 9. Phase 5 ops (Windows-only behaviors designed blind)

These were designed conservatively on macOS and need one Windows pass
(RUNBOOK.md is the operator-facing manual for all of them):

- [ ] **Camera capture inside the scheduled task's session.** Install the
  service (`deploy\install_service.ps1`, RUNBOOK §5 incl. netplwiz
  auto-logon), reboot, and confirm frames flow (dashboard live view, fps
  in `/stats.json`). This validates the run-as-interactive-user decision
  (D8) — capture under session 0/SYSTEM is the failure mode it avoids.
- [ ] **CTRL_BREAK stop end-to-end.** `deploy\stop_palletscan.ps1` while
  scanning: the child must *drain* (run summary in
  `logs\palletscan.jsonl`, `reason: "stop-requested"` in
  `logs\restarts.jsonl`), the stop-file must vanish, and the supervisor
  exit 0 — within ~20 s. This validates CTRL_BREAK delivery to the
  `CREATE_NEW_PROCESS_GROUP` child and the SIGBREAK handler.
- [ ] **Exit-4 contention message after `taskkill`.** With the service
  running, try `palletscan run` on the same data dir → exit 4 naming the
  holder. Then `taskkill /F` the child: the supervised replacement must
  re-acquire the lock (msvcrt releases on process death — the stale-lock
  proof on Windows), and a few seconds of "exit 4 + backoff" churn in
  restarts.jsonl during the race window is expected and self-healing.
- [ ] **Log rotation under load.** Set `logging.file.max_mb: 1` briefly;
  confirm `palletscan.jsonl.1..5` appear and total size respects the cap
  (doRollover's rename is the Windows-fragile bit the single-writer lock
  protects).
- [ ] **Factory CPU run** — §7 above (`tools\measure_cpu.py`).
- [ ] **Windows soak_short.** `pytest -m soak_short` on the idle factory
  box: adaptive warmup (D11) should absorb whatever Windows' allocator
  ramp looks like; record the reported `warmup` value here.
