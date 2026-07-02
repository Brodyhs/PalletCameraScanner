"""StationRunner: A/B end-to-end — business dedup across cameras, per-camera
stats independence, per-camera evidence, error propagation."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from palletscan.app import RunSummary
from palletscan.cli import _exit_code_for, main
from palletscan.config import (
    AppConfig,
    CameraConfig,
    ConsoleSinkConfig,
    SourceConfig,
    StationPolicy,
    SyntheticConfig,
    apply_overrides,
)
from palletscan.reliability.watchdog import WatchdogEscalation
from palletscan.sources.base import FrameSource
from palletscan.sources.factory import synthetic_tail_s
from palletscan.sources.synthetic import SyntheticSource
from palletscan.station import StationRunner, StationSummary
from palletscan.types import PassEvent


def _station_cfg(base: Path) -> AppConfig:
    cfg = AppConfig().model_copy(
        update={
            "synthetic": SyntheticConfig(
                width=640,
                height=360,
                fps=30.0,
                seed=1234,
                num_passes=3,
                speed_mph_range=(3.0, 5.0),
                angle_deg_range=(0.0, 10.0),
                contrast_range=(0.8, 1.0),
                noise_sigma_range=(1.0, 3.0),
                occlusion_max_frac=0.0,
                idle_s_range=(0.4, 0.6),
            ),
        }
    )
    cfg = apply_overrides(cfg, data_dir=base)
    return cfg.model_copy(
        update={
            "sinks": cfg.sinks.model_copy(
                update={"console": ConsoleSinkConfig(enabled=False)}
            )
        }
    )


def _build_station(cfg: AppConfig) -> tuple[StationRunner, list[SyntheticSource]]:
    tail = synthetic_tail_s(cfg)
    sources = [
        SyntheticSource(cfg.synthetic, source_id=source_id, tail_s=tail)
        for source_id in ("synthA", "synthB")
    ]
    return StationRunner(cfg, sources=list(sources)), sources


@pytest.fixture(scope="module")
def station_run(tmp_path_factory: pytest.TempPathFactory):
    """One real A/B run shared by the assertion tests below."""
    base = tmp_path_factory.mktemp("station")
    cfg = _station_cfg(base)
    station, sources = _build_station(cfg)
    summary = station.run()
    return base, cfg, station, sources, summary


def test_business_passes_equal_truth_not_doubled(station_run) -> None:
    _, _, station, sources, summary = station_run
    truth_count = len(sources[0].truth)
    assert truth_count == 3
    # Same seed -> bit-identical schedules.
    assert [r.payload for r in sources[0].truth] == [
        r.payload for r in sources[1].truth
    ]
    assert summary.business["passes_emitted"] == truth_count  # NOT 2x
    assert summary.business["cross_camera_merges"] == truth_count
    assert summary.business["misses_forwarded"] == 0
    assert summary.reconciliation is not None
    assert summary.reconciliation.read_rate == 1.0
    assert summary.unaccounted == 0


def test_stored_rows_carry_both_cameras_detail(station_run) -> None:
    """The revision-guarded upsert proven end-to-end in SQLite."""
    _, cfg, _, sources, _ = station_run
    conn = sqlite3.connect(cfg.sinks.sqlite.path)
    rows = conn.execute(
        "SELECT payload, decode_count, revision, detail_json FROM events "
        "WHERE kind='pass'"
    ).fetchall()
    conn.close()
    assert len(rows) == 3  # one row per business pass, not per camera
    assert {r[0] for r in rows} == {t.payload for t in sources[0].truth}
    for payload, decode_count, revision, detail_json in rows:
        detail = json.loads(detail_json)
        assert revision == 1, f"{payload}: stored row is not the merged version"
        assert set(detail["camera_detail"]) == {"synthA", "synthB"}
        assert decode_count == sum(
            d["decode_count"] for d in detail["camera_detail"].values()
        )


def test_per_camera_stats_do_not_dedupe(station_run) -> None:
    """Spec §4: each camera's independent performance is the experiment."""
    _, _, station, sources, summary = station_run
    truth_count = len(sources[0].truth)
    for source_id in ("synthA", "synthB"):
        run = summary.per_camera[source_id]
        assert run.passes == truth_count
        assert run.misses == 0
        snap = station.runners[source_id].metrics.snapshot()
        assert snap["passes"]["emitted"] == truth_count


def test_no_event_loss_under_ab(station_run) -> None:
    _, _, station, _, summary = station_run
    assert summary.business_sink_errors == 0
    assert summary.collector_dropped == 0
    # Every business event (3 emits + 3 re-emits) was handled by the bus.
    assert summary.business_events_handled == 6
    for run in summary.per_camera.values():
        assert run.sink_errors == 0


def test_jsonl_audit_log_max_revision_wins(station_run) -> None:
    """JSONL gets <= N_cameras lines per business pass sharing one event_id;
    readers take max-revision-wins (accepted D1 cost)."""
    _, cfg, _, _, _ = station_run
    lines = [
        json.loads(line)
        for line in cfg.sinks.jsonl.path.read_text().splitlines()
    ]
    by_id: dict[str, list[dict]] = {}
    for d in lines:
        by_id.setdefault(d["event_id"], []).append(d)
    assert len(by_id) == 3
    for versions in by_id.values():
        assert sorted(v["revision"] for v in versions) == [0, 1]
        newest = max(versions, key=lambda v: v["revision"])
        assert set(newest["camera_detail"]) == {"synthA", "synthB"}


def test_evidence_rebased_per_camera(station_run) -> None:
    base, _, station, _, _ = station_run
    for source_id in ("synthA", "synthB"):
        assert (base / "evidence" / source_id).is_dir()
        writer_root = station.runners[source_id]._tracker._evidence._root
        assert writer_root == base / "evidence" / source_id


def test_ab_report_and_reconciliation_from_station_db(station_run) -> None:
    """Verification-matrix end-to-end: station merge -> SQLite camera_detail
    rows -> ReadStore -> report math; manifest from truth payloads (+1
    ghost, -1 dropped) -> buckets exact, true read rate matches."""
    from palletscan.reporting.ab import compute_ab_report
    from palletscan.reporting.manifest import reconcile
    from palletscan.web.store import ReadStore

    _, cfg, _, sources, _ = station_run
    passes, miss_rows = ReadStore(cfg.sinks.sqlite.path).pass_and_miss_rows()
    report = compute_ab_report(passes, miss_rows)
    truth_payloads = [r.payload for r in sources[0].truth]
    for source_id in ("synthA", "synthB"):
        cam = report.cameras[source_id]
        assert cam.passes_seen == len(truth_payloads)
        assert cam.passes_decoded == len(truth_payloads)
        assert cam.read_rate == 1.0
        assert len(cam.ttfd_samples) == len(truth_payloads)
        assert all(t >= 0.0 for t in cam.ttfd_samples)
    assert report.business_passes == len(truth_payloads)
    assert report.business_misses == 0

    expected = truth_payloads[1:] + ["PLT-GHOST"]  # -1 real, +1 ghost
    rec = reconcile(expected, [p["payload"] for p in passes])
    assert rec.matched == truth_payloads[1:]
    assert rec.missing == ["PLT-GHOST"]
    assert rec.unexpected == [truth_payloads[0]]
    assert rec.true_read_rate == pytest.approx(
        len(truth_payloads[1:]) / len(expected)
    )


def test_station_summary_format_smoke(station_run) -> None:
    _, _, _, _, summary = station_run
    text = summary.format()
    assert "[synthA]" in text and "[synthB]" in text
    assert "passes (deduped) : 3" in text
    assert "100.0% read rate" in text


# -- error propagation (unit-level, no real pipeline run) ---------------------


def _fake_summary() -> RunSummary:
    return RunSummary(
        frames=0, frames_dropped=0, passes=0, passes_merged=0,
        misses=0, events_handled=0, sink_errors=0,
    )


def test_runner_error_stops_others_and_chains_escalation(tmp_path: Path) -> None:
    cfg = _station_cfg(tmp_path)
    station, _ = _build_station(cfg)
    stopped = threading.Event()
    escalation = WatchdogEscalation("zombie reader limit")

    def fail_run(stats_interval_s=None):
        try:
            raise escalation
        except WatchdogEscalation as exc:
            raise RuntimeError("pipeline thread failure") from exc

    healthy = station.runners["synthB"]
    original_stop = healthy.stop

    def wait_run(stats_interval_s=None):
        assert stopped.wait(timeout=10), "station never stopped the healthy runner"
        return _fake_summary()

    def observed_stop():
        stopped.set()
        original_stop()

    station.runners["synthA"].run = fail_run  # type: ignore[method-assign]
    healthy.run = wait_run  # type: ignore[method-assign]
    healthy.stop = observed_stop  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as excinfo:
        station.run()
    assert stopped.is_set()
    # The cause chain survives the station re-raise -> supervisor exit 3.
    assert isinstance(excinfo.value.__cause__, WatchdogEscalation)
    assert _exit_code_for(excinfo.value) == 3


def test_plain_runner_error_maps_to_exit_1(tmp_path: Path) -> None:
    cfg = _station_cfg(tmp_path)
    station, _ = _build_station(cfg)

    def fail_run(stats_interval_s=None):
        raise RuntimeError("plain software failure")  # no cause chain

    def quick_run(stats_interval_s=None):
        return _fake_summary()

    station.runners["synthA"].run = fail_run  # type: ignore[method-assign]
    station.runners["synthB"].run = quick_run  # type: ignore[method-assign]
    with pytest.raises(RuntimeError) as excinfo:
        station.run()
    assert _exit_code_for(excinfo.value) == 1


# -- continue_others limits (REVIEW bringup-4d95b67, station findings) ----------


def _continue_others_cfg(base: Path) -> AppConfig:
    cfg = _station_cfg(base)
    return cfg.model_copy(
        update={
            "station": cfg.station.model_copy(
                update={"on_arm_failure": StationPolicy.CONTINUE_OTHERS}
            )
        }
    )


def test_continue_others_tolerates_plain_arm_error_with_healthy_summary(
    tmp_path: Path,
) -> None:
    """The sanctioned tolerance: a plain arm failure with a healthy arm's
    summary present returns a StationSummary instead of raising (one camera
    beats none)."""
    station, _ = _build_station(_continue_others_cfg(tmp_path))

    def fail_run(stats_interval_s=None):
        raise RuntimeError("plain arm failure")

    def quick_run(stats_interval_s=None):
        return _fake_summary()

    station.runners["synthA"].run = fail_run  # type: ignore[method-assign]
    station.runners["synthB"].run = quick_run  # type: ignore[method-assign]
    summary = station.run()
    assert set(summary.per_camera) == {"synthB"}


def test_continue_others_never_tolerates_business_drain_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """REVIEW bringup-4d95b67: the drain failure was recorded only `if not
    self._errors`, so ANY arm error hid it from the bus_errors check and
    continue_others returned a clean summary over a silent business-event
    loss — contradicting the code's own 'a business-bus drain failure is
    never tolerated'. Arm error + drain failure must fail the station."""
    station, _ = _build_station(_continue_others_cfg(tmp_path))

    def fail_run(stats_interval_s=None):
        raise RuntimeError("arm failure")

    def quick_run(stats_interval_s=None):
        return _fake_summary()

    station.runners["synthA"].run = fail_run  # type: ignore[method-assign]
    station.runners["synthB"].run = quick_run  # type: ignore[method-assign]
    real_shutdown = station.business_bus.shutdown
    monkeypatch.setattr(station.business_bus, "shutdown", lambda: False)
    try:
        with pytest.raises(RuntimeError):
            station.run()
    finally:
        real_shutdown()  # drain the real bus so its thread exits


def test_continue_others_never_tolerates_watchdog_escalation(
    tmp_path: Path,
) -> None:
    """REVIEW bringup-4d95b67: CONTINUE_OTHERS tolerated WatchdogEscalation,
    converting the watchdog's never-give-up semantics into permanent silent
    arm loss (no respawn, no exit-3 restart). An escalated arm must fail
    the station through the escalation exit path so the supervisor
    restarts it."""
    station, _ = _build_station(_continue_others_cfg(tmp_path))

    def fail_escalated(stats_interval_s=None):
        raise WatchdogEscalation("camera offline longer than max_outage_s")

    def quick_run(stats_interval_s=None):
        return _fake_summary()

    station.runners["synthA"].run = fail_escalated  # type: ignore[method-assign]
    station.runners["synthB"].run = quick_run  # type: ignore[method-assign]
    with pytest.raises(RuntimeError) as excinfo:
        station.run()
    assert isinstance(excinfo.value.__cause__, WatchdogEscalation)
    assert _exit_code_for(excinfo.value) == 3  # supervisor restart path


def test_continue_others_prefers_escalation_over_earlier_plain_error(
    tmp_path: Path,
) -> None:
    """When both a plain arm error and an escalation land, the raised error
    chains from the ESCALATION so the exit-3 supervisor mapping engages
    regardless of arrival order."""
    station, _ = _build_station(_continue_others_cfg(tmp_path))
    plain_failed = threading.Event()

    def fail_plain(stats_interval_s=None):
        try:
            raise RuntimeError("plain arm failure (lands first)")
        finally:
            plain_failed.set()

    def fail_escalated(stats_interval_s=None):
        assert plain_failed.wait(timeout=10)
        raise WatchdogEscalation("zombie reader limit")

    station.runners["synthA"].run = fail_plain  # type: ignore[method-assign]
    station.runners["synthB"].run = fail_escalated  # type: ignore[method-assign]
    with pytest.raises(RuntimeError) as excinfo:
        station.run()
    assert _exit_code_for(excinfo.value) == 3


# -- construction: leaks, probes, deduper wiring (7e4c22c review) ---------------


class _RecordingSource(FrameSource):
    """Minimal source that records close() — stands in for an eagerly
    opened CameraSource capture."""

    def __init__(self, source_id: str) -> None:
        self._id = source_id
        self.closed = False

    @property
    def source_id(self) -> str:
        return self._id

    def frames(self):
        return iter(())

    def close(self) -> None:
        self.closed = True


def test_duplicate_ids_close_already_opened_sources(tmp_path: Path) -> None:
    """Finding 15: constructor failure must release every source already
    opened — CameraSource opens its device eagerly, and there is no other
    close path for a half-built station."""
    cfg = _station_cfg(tmp_path)
    sources = [_RecordingSource("camA"), _RecordingSource("camA")]
    with pytest.raises(ValueError, match="duplicate source ids"):
        StationRunner(cfg, sources=sources)
    assert all(s.closed for s in sources)


def test_failed_second_camera_closes_the_first(
    tmp_path: Path, monkeypatch
) -> None:
    """Finding 15, the review's scenario: camB unplugged -> create_source
    raises after camA's capture is already open; camA must be closed."""
    import palletscan.station as station_mod

    cfg = _station_cfg(tmp_path).model_copy(
        update={
            "source": SourceConfig(type="camera", cameras=["camA", "camB"]),
            "cameras": [_cam("camA"), _cam("camB")],
        }
    )
    opened: list[_RecordingSource] = []

    def fake_create_source(per_cam_cfg, *, epoch=None, epoch_wall=None):
        if per_cam_cfg.source.camera == "camB":
            raise RuntimeError("camB: no matching device (unplugged)")
        source = _RecordingSource(per_cam_cfg.source.camera)
        opened.append(source)
        return source

    monkeypatch.setattr(station_mod, "create_source", fake_create_source)
    with pytest.raises(RuntimeError, match="camB"):
        StationRunner(cfg)
    assert len(opened) == 1
    assert opened[0].closed


def test_ab_outbox_metric_visible_on_every_camera(tmp_path: Path) -> None:
    """Finding 5: with sinks.http enabled the outbox hangs off the business
    bus, so per-camera snapshots reported outbox=null and the backlog
    signal was invisible in exactly the A/B trial mode."""
    from palletscan.config import HttpSinkConfig

    cfg = _station_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "sinks": cfg.sinks.model_copy(
                update={
                    "http": HttpSinkConfig(
                        enabled=True,
                        url="http://127.0.0.1:9/events",  # never dialed here
                        outbox_path=tmp_path / "outbox.db",
                    )
                }
            )
        }
    )
    station, _ = _build_station(cfg)
    try:
        for source_id, runner in station.runners.items():
            outbox = runner.metrics.snapshot()["outbox"]
            assert outbox is not None, f"{source_id}: outbox probe not wired"
            assert outbox["depth"] == 0
    finally:
        # Drain-and-close the never-used bus so the HttpSink uploader stops.
        station.business_bus.start()
        station.business_bus.shutdown()


def test_station_deduper_knows_camera_set(tmp_path: Path) -> None:
    """Finding 1 wiring: the station hands its source ids to the deduper so
    eviction waits for the slowest camera from the very first event."""
    station, _ = _build_station(_station_cfg(tmp_path))
    assert set(station.deduper._high_waters) == {"synthA", "synthB"}


# -- config validation (D8) ----------------------------------------------------


def _cam(camera_id: str) -> CameraConfig:
    return CameraConfig(id=camera_id, name=f"name-{camera_id}")


def test_source_cameras_validation() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        SourceConfig(type="camera", camera="a", cameras=["a", "b"])
    with pytest.raises(ValidationError, match=">= 2 entries"):
        SourceConfig(type="camera", cameras=["a"])
    with pytest.raises(ValidationError, match="duplicate ids"):
        SourceConfig(type="camera", cameras=["a", "a"])
    with pytest.raises(ValidationError, match="source.type=camera"):
        SourceConfig(type="synthetic", cameras=["a", "b"])
    assert SourceConfig(type="camera", cameras=["a", "b"]).cameras == ["a", "b"]


def test_source_cameras_must_resolve_against_cameras_list() -> None:
    with pytest.raises(ValidationError, match="not in cameras"):
        AppConfig(
            source=SourceConfig(type="camera", cameras=["a", "ghost"]),
            cameras=[_cam("a"), _cam("b")],
        )
    cfg = AppConfig(
        source=SourceConfig(type="camera", cameras=["a", "b"]),
        cameras=[_cam("a"), _cam("b")],
    )
    assert cfg.source.cameras == ["a", "b"]


def test_station_requires_sources_or_config_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source.cameras or sources"):
        StationRunner(_station_cfg(tmp_path))


# -- CLI plumbing ---------------------------------------------------------------


def test_cli_synth_ab_end_to_end(tmp_path: Path, capsys) -> None:
    code = main(
        ["synth", "--ab", "--passes", "1", "--data-dir", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "station summary" in out
    assert "passes (deduped) : 1" in out
    assert (tmp_path / "truth.jsonl").exists()
    conn = sqlite3.connect(tmp_path / "palletscan.db")
    (passes,) = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='pass'"
    ).fetchone()
    conn.close()
    assert passes == 1


def test_cli_synth_ab_runner_failure_keeps_exit_contract(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Finding 12: synth --ab must map a RuntimeError out of station.run()
    to the same clean message + exit code _cmd_run guarantees (station.py
    chains the cause precisely so the CLI can inspect it), instead of a
    raw traceback."""
    import palletscan.station as station_mod

    def fail_plain(self, stats_interval_s=None):
        try:
            raise OSError(28, "No space left on device")
        except OSError as exc:
            raise RuntimeError("station runner 'synthA' failed") from exc

    monkeypatch.setattr(station_mod.StationRunner, "run", fail_plain)
    code = main(["synth", "--ab", "--passes", "1", "--data-dir", str(tmp_path)])
    assert code == 1
    err = capsys.readouterr().err
    assert "synth:" in err and "No space left on device" in err

    def fail_escalated(self, stats_interval_s=None):
        try:
            raise WatchdogEscalation("zombie reader limit")
        except WatchdogEscalation as exc:
            raise RuntimeError("station runner 'synthA' failed") from exc

    monkeypatch.setattr(station_mod.StationRunner, "run", fail_escalated)
    code = main(
        ["synth", "--ab", "--passes", "1", "--data-dir", str(tmp_path / "b")]
    )
    assert code == 3  # watchdog escalation survives to the supervisor
    assert "zombie reader limit" in capsys.readouterr().err


def test_business_view_takes_max_revision() -> None:
    from palletscan.station import _business_view

    base = PassEvent(
        payload="PLT-X",
        symbology="qr",  # type: ignore[arg-type]
        first_seen_ts=1.0,
        last_seen_ts=2.0,
        decode_count=3,
        cameras={"a": 3},
        best_frame=("a", 1),
        candidate_ids=["a-1"],
        event_id="ev-1",
        wall_time_iso="2026-06-11T00:00:00+00:00",
    )
    import dataclasses

    merged = dataclasses.replace(base, decode_count=5, revision=1)
    # Stale v0 arriving after v1 must not displace it.
    view = _business_view([base, merged, base])
    assert len(view) == 1
    assert view[0].decode_count == 5


# -- REVIEW_SYSTEM_0c30c77 findings b8 and 10 (station wiring) ----------------


def test_station_passes_one_shared_epoch_to_every_camera(
    tmp_path: Path, monkeypatch
) -> None:
    """REVIEW_SYSTEM_0c30c77 finding b8: per-camera ts epochs were anchored
    at each source's construction, so sequential connects (seconds apart)
    skewed every cross-camera ts comparison. StationRunner must sample ONE
    (monotonic, wall) pair before any device opens and hand it to all."""
    import palletscan.station as station_mod

    cfg = _station_cfg(tmp_path).model_copy(
        update={
            "source": SourceConfig(type="camera", cameras=["camA", "camB"]),
            "cameras": [
                CameraConfig(id="camA", name="a"),
                CameraConfig(id="camB", name="b"),
            ],
        }
    )
    received: list[tuple] = []

    def fake_create_source(per_cam_cfg, *, epoch=None, epoch_wall=None):
        received.append((per_cam_cfg.source.camera, epoch, epoch_wall))
        return _RecordingSource(per_cam_cfg.source.camera)

    monkeypatch.setattr(station_mod, "create_source", fake_create_source)
    StationRunner(cfg)
    assert [r[0] for r in received] == ["camA", "camB"]
    epochs = {r[1] for r in received}
    walls = {r[2] for r in received}
    assert len(epochs) == 1 and None not in epochs
    assert len(walls) == 1 and None not in walls


def test_station_seeds_deduper_from_previous_runs_store(tmp_path: Path) -> None:
    """REVIEW_SYSTEM_0c30c77 finding 10 (repro: camB decodes the pallet
    8 s after camA's pre-restart emit -> second business PassEvent) +
    design-review fix: the seeding must engage THROUGH StationRunner's
    construction gate (sources carrying epoch_wall + sqlite sink), not
    just in a bare deduper."""
    import dataclasses
    import time
    import uuid

    from palletscan.events.sinks import SqliteSink
    from palletscan.types import Symbology, iso_at

    cfg = _station_cfg(tmp_path)
    epoch_wall = time.time()
    # The previous run stored a pass ~5 s before this process's ts=0.
    prev = PassEvent(
        payload="PLT-RESTART",
        symbology=Symbology.QR,
        first_seen_ts=100.0,
        last_seen_ts=101.0,
        decode_count=2,
        cameras={"camA": 2},
        best_frame=("camA", 7),
        candidate_ids=["camA-x-000001"],
        event_id=str(uuid.uuid4()),
        wall_time_iso=iso_at(epoch_wall - 5.0),
    )
    sink = SqliteSink(cfg.sinks.sqlite.path)
    sink.handle(prev)
    sink.close()

    class _WallSource(_RecordingSource):
        def __init__(self, source_id: str, wall: float) -> None:
            super().__init__(source_id)
            self.epoch_wall = wall

    station = StationRunner(
        cfg,
        sources=[_WallSource("camA", epoch_wall), _WallSource("camB", epoch_wall)],
    )
    # camB re-sights the pallet 3 s into the new run (8 s after the stored
    # emit, inside the 12 s window): suppressed, counted, no publish.
    resight = dataclasses.replace(
        prev,
        cameras={"camB": 1},
        event_id=str(uuid.uuid4()),
        first_seen_ts=2.5,
        last_seen_ts=3.0,
    )
    station.deduper.submit(resight)
    stats = station.deduper.stats()
    assert stats["restart_repeats_suppressed"] == 1
    assert stats["passes_emitted"] == 0
    for runner in station.runners.values():
        runner.stop()


def test_station_does_not_seed_without_wall_anchor(tmp_path: Path) -> None:
    """Finding 10 scope guard: synthetic/replay sources carry no
    epoch_wall — their determinism must never depend on a previous run's
    store."""
    cfg = _station_cfg(tmp_path)
    station, _ = _build_station(cfg)
    assert station.deduper.stats()["restart_repeats_suppressed"] == 0
    assert station.deduper._state == {}
    station.stop()
