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
    SyntheticConfig,
    apply_overrides,
)
from palletscan.reliability.watchdog import WatchdogEscalation
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
