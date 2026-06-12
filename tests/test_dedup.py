"""CrossCameraDeduper: emit-now/merge-by-reemit semantics + the revision
hammer (owner amendment to D1)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from palletscan.events.bus import EventBus
from palletscan.events.dedup import CrossCameraDeduper, ForwardingSink
from palletscan.events.sinks import SqliteSink
from palletscan.types import Event, MissEvent, PassEvent, Symbology

WINDOW_S = 12.0


def _pass(
    payload: str,
    camera: str,
    *,
    first_seen: float,
    last_seen: float,
    decode_count: int = 3,
    first_decode: float | None = None,
    best_frame_index: int = 42,
) -> PassEvent:
    first_decode = first_decode if first_decode is not None else first_seen + 0.1
    return PassEvent(
        payload=payload,
        symbology=Symbology.QR,
        first_seen_ts=first_seen,
        last_seen_ts=last_seen,
        decode_count=decode_count,
        cameras={camera: decode_count},
        best_frame=(camera, best_frame_index),
        candidate_ids=[f"{camera}-000001"],
        event_id=str(uuid.uuid4()),
        wall_time_iso="2026-06-11T00:00:00+00:00",
        first_decode_ts=first_decode,
        camera_detail={
            camera: {
                "first_seen_ts": first_seen,
                "first_decode_ts": first_decode,
                "last_seen_ts": last_seen,
                "decode_count": decode_count,
            }
        },
    )


def _miss(camera: str = "camA") -> MissEvent:
    return MissEvent(
        candidate_id=f"{camera}-000099",
        source_id=camera,
        start_ts=5.0,
        end_ts=6.0,
        first_frame=150,
        last_frame=180,
        evidence_dir="/tmp/ev/x",
        evidence_frame_count=10,
        event_id=str(uuid.uuid4()),
        wall_time_iso="2026-06-11T00:00:01+00:00",
    )


@pytest.fixture()
def deduper():
    published: list[Event] = []
    return CrossCameraDeduper(published.append, WINDOW_S), published


def test_single_camera_pass_through_verbatim(deduper) -> None:
    d, published = deduper
    ev = _pass("PLT-1", "camA", first_seen=1.0, last_seen=2.0)
    d.submit(ev)
    assert published == [ev]  # the very same event: id stable, revision 0
    assert published[0].revision == 0
    assert d.stats()["passes_emitted"] == 1
    assert d.stats()["reemits"] == 0


def test_two_cameras_merge_and_reemit(deduper) -> None:
    d, published = deduper
    a = _pass(
        "PLT-2", "camA",
        first_seen=1.0, last_seen=2.0,
        decode_count=3, first_decode=1.5,
        best_frame_index=42,
    )
    b = _pass(
        "PLT-2", "camB",
        first_seen=0.5, last_seen=2.5,
        decode_count=2, first_decode=1.2,
        best_frame_index=17,
    )
    d.submit(a)
    d.submit(b)
    assert len(published) == 2
    first, merged = published
    assert first is a
    assert merged.event_id == a.event_id  # stable business id
    assert merged.revision == 1
    assert merged.first_seen_ts == 0.5  # min
    assert merged.last_seen_ts == 2.5  # max
    assert merged.decode_count == 5  # summed
    assert merged.cameras == {"camA": 3, "camB": 2}
    assert merged.best_frame == ("camB", 17)  # camB decoded first (1.2 < 1.5)
    assert merged.first_decode_ts == 1.2
    assert set(merged.camera_detail) == {"camA", "camB"}
    assert merged.camera_detail["camA"]["decode_count"] == 3
    assert merged.camera_detail["camB"]["decode_count"] == 2
    assert merged.candidate_ids == ["camA-000001", "camB-000001"]
    stats = d.stats()
    assert stats["passes_emitted"] == 1
    assert stats["cross_camera_merges"] == 1
    assert stats["reemits"] == 1


def test_beyond_window_is_new_business_pass(deduper) -> None:
    d, published = deduper
    d.submit(_pass("PLT-3", "camA", first_seen=1.0, last_seen=2.0))
    d.submit(
        _pass("PLT-3", "camB", first_seen=19.0, last_seen=20.0)
    )  # 18 s after anchor > 12 s window
    assert len(published) == 2
    assert published[0].event_id != published[1].event_id
    assert published[1].revision == 0
    assert d.stats()["passes_emitted"] == 2
    assert d.stats()["cross_camera_merges"] == 0


def test_misses_forward_unchanged(deduper) -> None:
    d, published = deduper
    m = _miss()
    d.submit(m)
    assert published == [m]
    assert d.stats()["misses_forwarded"] == 1
    assert d.stats()["passes_emitted"] == 0


def test_same_camera_repeat_suppressed_anchor_not_extended(deduper) -> None:
    d, published = deduper
    d.submit(_pass("PLT-4", "camA", first_seen=1.0, last_seen=2.0))  # anchor 2
    d.submit(_pass("PLT-4", "camA", first_seen=9.0, last_seen=10.0))  # repeat
    assert len(published) == 1
    assert d.stats()["repeats_suppressed"] == 1
    # 15 is within 12 s of the repeat (10) but beyond the anchor (2): the
    # parked-pallet rule means this is a NEW business pass.
    d.submit(_pass("PLT-4", "camA", first_seen=14.0, last_seen=15.0))
    assert len(published) == 2
    assert published[1].event_id != published[0].event_id
    assert published[1].revision == 0
    assert d.stats()["passes_emitted"] == 2


def test_payload_map_pruned_by_high_water(deduper) -> None:
    d, _ = deduper
    for i in range(200):
        d.submit(
            _pass(f"PLT-P{i:04d}", "camA", first_seen=float(i), last_seen=float(i))
        )
    # window is 12 s and timestamps advance 1 s per payload: only ~window's
    # worth of payloads may remain tracked.
    assert len(d._state) <= int(WINDOW_S) + 2


def test_merge_with_legacy_event_missing_detail(deduper) -> None:
    """Pre-Phase-4 events (no first_decode_ts/camera_detail) still merge."""
    d, published = deduper
    legacy = PassEvent(
        payload="PLT-5",
        symbology=Symbology.QR,
        first_seen_ts=1.0,
        last_seen_ts=2.0,
        decode_count=2,
        cameras={"camA": 2},
        best_frame=("camA", 7),
        candidate_ids=["camA-000001"],
        event_id="ev-legacy",
        wall_time_iso="2026-06-11T00:00:00+00:00",
    )
    d.submit(legacy)
    d.submit(_pass("PLT-5", "camB", first_seen=1.5, last_seen=2.5, first_decode=1.9))
    merged = published[1]
    assert merged.revision == 1
    assert merged.first_decode_ts == 1.9
    assert merged.best_frame == ("camB", 42)  # only camB has a decode ts
    assert set(merged.camera_detail) == {"camB"}


def test_hammer_one_payload_two_threads_final_row_fully_merged(
    tmp_path: Path,
) -> None:
    """Owner-amendment proof: under racing re-emits the FINAL stored row is
    the fully-merged version — not merely that no ids were lost."""
    db = tmp_path / "hammer.db"
    bus = EventBus([SqliteSink(db)])
    bus.start()
    published: list[Event] = []

    def publish(ev: Event) -> None:
        published.append(ev)  # GIL-atomic append from both threads
        bus.publish(ev)

    deduper = CrossCameraDeduper(publish, WINDOW_S)
    rounds = 30
    barrier = threading.Barrier(3)

    def submitter(camera: str, decode_count: int) -> None:
        for i in range(rounds):
            ev = _pass(
                f"PLT-H{i:04d}",
                camera,
                first_seen=float(i),
                last_seen=float(i) + 0.5,
                decode_count=decode_count,
            )
            barrier.wait()  # both threads submit as simultaneously as possible
            deduper.submit(ev)
            barrier.wait()  # round complete; main thread asserts

    threads = [
        threading.Thread(target=submitter, args=("camA", 3)),
        threading.Thread(target=submitter, args=("camB", 2)),
    ]
    for t in threads:
        t.start()

    conn: sqlite3.Connection | None = None
    try:
        for i in range(rounds):
            barrier.wait()  # release the submitters
            barrier.wait()  # both submitted; 2 publishes for this round
            deadline = time.monotonic() + 10.0
            while bus.events_handled < 2 * (i + 1):
                assert time.monotonic() < deadline, "bus did not drain in time"
                time.sleep(0.005)
            if conn is None:
                conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT decode_count, revision, detail_json FROM events "
                "WHERE payload = ?",
                (f"PLT-H{i:04d}",),
            ).fetchone()
            assert row is not None
            decode_count, revision, detail_json = row
            detail = json.loads(detail_json)
            assert decode_count == 5, f"round {i}: row regressed to pre-merge"
            assert revision == 1
            assert set(detail["camera_detail"]) == {"camA", "camB"}
            assert detail["cameras"] == {"camA": 3, "camB": 2}
    finally:
        for t in threads:
            t.join(timeout=10)
        bus.shutdown()
        if conn is not None:
            conn.close()

    assert bus.sink_errors == 0
    # Each round emitted exactly one business id at revisions 0 and 1.
    by_payload: dict[str, list[int]] = {}
    for ev in published:
        assert isinstance(ev, PassEvent)
        by_payload.setdefault(ev.payload, []).append(ev.revision)
    assert all(sorted(revs) == [0, 1] for revs in by_payload.values())


def test_forwarding_sink_routes_and_close_is_noop(deduper) -> None:
    d, published = deduper
    sink = ForwardingSink(d)
    sink.handle(_pass("PLT-6", "camA", first_seen=1.0, last_seen=2.0))
    sink.handle(_miss())
    sink.close()  # must not propagate to the deduper or business bus
    assert len(published) == 2
    assert d.stats() == {
        "passes_emitted": 1,
        "cross_camera_merges": 0,
        "repeats_suppressed": 0,
        "reemits": 0,
        "misses_forwarded": 1,
    }
