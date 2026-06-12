"""``palletscan run``: source honoring, --camera override, exit-code mapping."""

from __future__ import annotations

import json
from pathlib import Path

from palletscan.cli import _exit_code_for, main
from palletscan.reliability.watchdog import WatchdogEscalation

_SYNTH_YAML = """
synthetic:
  width: 640
  height: 360
  num_passes: 2
  seed: 1234
  speed_mph_range: [3.0, 5.0]
  angle_deg_range: [0.0, 10.0]
  contrast_range: [0.8, 1.0]
  noise_sigma_range: [1.0, 3.0]
  occlusion_max_frac: 0.0
  idle_s_range: [0.4, 0.6]
sinks:
  console: {enabled: false}
"""


def test_run_honors_configured_source(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "synth.yaml"
    cfg.write_text(_SYNTH_YAML, encoding="utf-8")
    rc = main(
        ["run", "--config", str(cfg), "--data-dir", str(tmp_path / "data")]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "run summary" in out
    events = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert len([json.loads(line) for line in events.splitlines()]) >= 2


def test_run_camera_override_unknown_id_fails_fast(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cam.yaml"
    cfg.write_text(
        "source: {type: camera}\n"
        "cameras: [{id: cam-color, name: 'ZZZ-Nonexistent-Camera'}]\n",
        encoding="utf-8",
    )
    rc = main(["run", "--config", str(cfg), "--camera", "nope"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "'nope'" in err and "cam-color" in err  # lists configured ids


def test_run_missing_device_fails_fast_not_blind(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cam.yaml"
    cfg.write_text(
        "source: {type: camera}\n"
        "cameras: [{id: cam-color, name: 'ZZZ-Nonexistent-Camera-XYZ'}]\n",
        encoding="utf-8",
    )
    rc = main(["run", "--config", str(cfg)])
    assert rc == 1
    assert "run:" in capsys.readouterr().err


def test_run_exits_3_end_to_end_on_watchdog_escalation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Full wiring: camera dies, every reopen fails, max_outage_s trips ->
    WatchdogEscalation -> RuntimeError chain -> exit code 3 from main()."""
    import palletscan.sources.camera as camera_mod
    from palletscan.sources.devices import devices_from_names
    from tests.camera_fakes import FakeCapture, FakeCaptureFactory

    factory = FakeCaptureFactory(
        captures=[FakeCapture(read_script=["ok"] * 3 + [RuntimeError("usb died")])],
        default=lambda i, b: FakeCapture(opened=False),  # never recovers
    )
    monkeypatch.setattr(camera_mod, "default_capture_factory", factory)
    monkeypatch.setattr(
        camera_mod, "list_devices", lambda: devices_from_names(["FakeCam"], 0)
    )
    cfg = tmp_path / "cam.yaml"
    cfg.write_text(
        "source: {type: camera}\n"
        "cameras: [{id: c, name: FakeCam, connect_verify_s: 0.0}]\n"
        "watchdog:\n"
        "  stall_timeout_s: 0.5\n"
        "  retry: {base_s: 0.05, cap_s: 0.1}\n"
        "  max_outage_s: 0.3\n"
        f"sinks: {{console: {{enabled: false}}, sqlite: {{enabled: false}}, "
        f"jsonl: {{enabled: true, path: {tmp_path / 'e.jsonl'}}}}}\n",
        encoding="utf-8",
    )
    rc = main(["run", "--config", str(cfg), "--data-dir", str(tmp_path / "d")])
    err = capsys.readouterr().err
    assert rc == 3, err
    assert "max_outage_s" in err  # ops sees WHY, not just the code


def test_exit_code_mapping_for_watchdog_escalation() -> None:
    wedged = RuntimeError("pipeline thread failure")
    wedged.__cause__ = WatchdogEscalation("4 zombie readers")
    assert _exit_code_for(wedged) == 3
    crashed = RuntimeError("pipeline thread failure")
    crashed.__cause__ = ValueError("bug")
    assert _exit_code_for(crashed) == 1
    assert _exit_code_for(RuntimeError("no cause")) == 1
