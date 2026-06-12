"""``palletscan run``: source honoring, --camera override, exit-code mapping,
graceful stop signals."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from palletscan.cli import _exit_code_for, _install_stop_signals, main
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


def test_install_stop_signals_registers_and_drains(monkeypatch) -> None:
    """SIGINT/SIGTERM(/SIGBREAK on Windows) share one drain handler; the
    first delivery stops the runner and restores defaults so a second
    delivery force-quits (spec §5 graceful SIGTERM/CTRL+C)."""
    import signal as signal_mod

    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        signal_mod, "signal", lambda sig, handler: calls.append((sig, handler))
    )

    class _Runner:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    runner = _Runner()
    _install_stop_signals(runner)  # type: ignore[arg-type]
    expected = {signal_mod.SIGINT, signal_mod.SIGTERM}
    if hasattr(signal_mod, "SIGBREAK"):  # Windows only
        expected.add(signal_mod.SIGBREAK)
    assert {sig for sig, _ in calls} == expected
    assert len({h for _, h in calls}) == 1, "one shared handler for all signals"
    handler = calls[0][1]
    handler(signal_mod.SIGINT, None)
    assert runner.stopped
    restored = {sig for sig, h in calls if h is signal_mod.SIG_DFL}
    assert restored == expected, "second delivery must hit the default handler"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_synth_sigterm_drains_gracefully(tmp_path: Path) -> None:
    """Spec §5 end-to-end: SIGTERM mid-run drains the pipeline — exit 0
    with the summary printed, not a traceback. (SIGBREAK delivery is
    Windows-only: ARRIVAL_CHECKLIST.)"""
    import signal
    import subprocess
    import time

    cfg = tmp_path / "synth.yaml"
    cfg.write_text(
        _SYNTH_YAML.replace("num_passes: 2", "num_passes: 500"), encoding="utf-8"
    )
    events = tmp_path / "data" / "events.jsonl"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from palletscan.cli import main; "
            "sys.exit(main(sys.argv[1:]))",
            "synth",
            "--config",
            str(cfg),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if events.exists() and events.stat().st_size > 0:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is None, "synth finished before SIGTERM could be sent"
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, f"stdout:\n{out}\nstderr:\n{err}"
    assert "run summary" in out


def test_exit_code_mapping_for_watchdog_escalation() -> None:
    wedged = RuntimeError("pipeline thread failure")
    wedged.__cause__ = WatchdogEscalation("4 zombie readers")
    assert _exit_code_for(wedged) == 3
    crashed = RuntimeError("pipeline thread failure")
    crashed.__cause__ = ValueError("bug")
    assert _exit_code_for(crashed) == 1
    assert _exit_code_for(RuntimeError("no cause")) == 1


# -- REVIEW_SYSTEM_0c30c77 findings 12, b1, b2, b13 + finding 6 wiring ---------


def test_invalid_config_exits_2_with_clean_message(tmp_path: Path, capsys) -> None:
    """REVIEW_SYSTEM_0c30c77 finding b2 (repro: a YAML typo exits 1 with a
    raw pydantic traceback): config failures must follow the documented
    exit-2 contract so the supervisor's 'fix the config' branch fires."""
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("motion: {open_frames: banana}\n", encoding="utf-8")
    rc = main(["run", "--config", str(cfg), "--data-dir", str(tmp_path / "d")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "fix the config" in err
    assert "Traceback" not in err
    # The shared helper covers every subcommand; pin one more.
    rc = main(["selftest", "--config", str(cfg)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "fix the config" in err


def test_invalid_logging_level_is_a_config_error(tmp_path: Path, capsys) -> None:
    """Finding b2, second arm: a bad logging.level used to blow up inside
    setup_logging with a traceback (exit 1)."""
    cfg = tmp_path / "level.yaml"
    cfg.write_text("logging: {level: VERBOSE}\n", encoding="utf-8")
    rc = main(["run", "--config", str(cfg), "--data-dir", str(tmp_path / "d")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "logging.level" in err


def test_synth_pins_synthetic_source_on_camera_config(
    tmp_path: Path, capsys
) -> None:
    """REVIEW_SYSTEM_0c30c77 finding b13 (repro: `synth` on a camera
    config silently opened the real cameras, ran indefinitely with
    --passes inert, and wrote real passes under the synth banner)."""
    cfg = tmp_path / "cam.yaml"
    cfg.write_text(
        "source: {type: camera}\n"
        "synthetic: {width: 640, height: 360, num_passes: 2,\n"
        "  speed_mph_range: [3.0, 5.0], contrast_range: [0.8, 1.0],\n"
        "  noise_sigma_range: [1.0, 3.0], occlusion_max_frac: 0.0}\n"
        "sinks: {console: {enabled: false}}\n"
        "cameras: [{id: cam-color, name: 'ZZZ-Nonexistent-Camera'}]\n",
        encoding="utf-8",
    )
    rc = main(
        [
            "synth",
            "--passes",
            "2",
            "--seed",
            "7",
            "--config",
            str(cfg),
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, out  # never tried to enumerate/open the configured camera
    assert "run summary" in out


def test_child_carries_data_dir_recognizes_both_spellings() -> None:
    """REVIEW_SYSTEM_0c30c77 finding b1: the `--data-dir=X` spelling
    slipped past supervise's exact-token gate; the supervisor then appended
    its own --data-dir and argparse-last-wins ran the child on the wrong
    directory."""
    from palletscan.cli import _child_carries_data_dir

    assert _child_carries_data_dir(["run", "--data-dir", "X"])
    assert _child_carries_data_dir(["run", "--data-dir=E:\\trial7"])
    assert not _child_carries_data_dir(["run", "--data", "X"])
    assert not _child_carries_data_dir(["run"])


def test_option_abbreviations_are_rejected(tmp_path: Path) -> None:
    """Finding b1, second arm: with abbreviations enabled, `--data` was a
    silent alias for --data-dir in the CHILD's parser; now it is a loud
    usage error."""
    with pytest.raises(SystemExit) as exc:
        main(["synth", "--pass", "2"])
    assert exc.value.code == 2


def test_selftest_rebases_config_under_data_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 12 (repro: the only disk gate in the
    system probed the config-file paths and stayed green while the
    deployed volume filled): selftest must apply the same --data-dir
    rebase the run path uses."""
    import palletscan.selftest as selftest_mod
    from palletscan.selftest import CheckResult, SelftestReport

    received: dict = {}

    def fake_run_selftest(cfg, **kwargs):
        received["cfg"] = cfg
        report = SelftestReport()
        report.checks.append(CheckResult("stub", True, "ok"))
        return report

    monkeypatch.setattr(selftest_mod, "run_selftest", fake_run_selftest)
    cfg = tmp_path / "station.yaml"
    cfg.write_text("synthetic: {num_passes: 1}\n", encoding="utf-8")
    deploy = tmp_path / "deploy-volume"
    rc = main(
        ["selftest", "--config", str(cfg), "--data-dir", str(deploy)]
    )
    assert rc == 0
    got = received["cfg"]
    assert got.evidence.dir == deploy / "evidence"
    assert got.sinks.http.outbox_path == deploy / "outbox.db"
    assert got.sinks.sqlite.path == deploy / "palletscan.db"


def test_parent_watch_wiring_reads_env(monkeypatch) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 6, CLI half: writer commands start the
    watch iff the supervisor's pid env var is present and sane, and the
    watch is stoppable (the in-process-main no-leak convention)."""
    import os

    from palletscan.cli import _start_parent_watch_if_supervised
    from palletscan.reliability.supervisor import SUPERVISOR_PID_ENV

    class _FakeRunner:
        def __init__(self) -> None:
            self.stops = 0

        def stop(self) -> None:
            self.stops += 1

    monkeypatch.delenv(SUPERVISOR_PID_ENV, raising=False)
    assert _start_parent_watch_if_supervised(_FakeRunner()) is None
    monkeypatch.setenv(SUPERVISOR_PID_ENV, "not-a-pid")
    assert _start_parent_watch_if_supervised(_FakeRunner()) is None
    monkeypatch.setenv(SUPERVISOR_PID_ENV, str(os.getpid()))
    watch = _start_parent_watch_if_supervised(_FakeRunner())
    assert watch is not None
    watch.stop()
