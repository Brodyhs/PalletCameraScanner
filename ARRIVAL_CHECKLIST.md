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
  zero drops beyond the expected burst behavior; note CPU%.

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
