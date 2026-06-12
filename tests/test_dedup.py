"""CrossCameraDeduper: emit-now/merge-by-reemit semantics + the revision
hammer (owner amendment to D1)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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


def test_lagging_camera_pass_still_merges_after_other_advances() -> None:
    """7e4c22c review, finding 1 (reproduced there): eviction by a single
    global high-water cutoff let one camera's progress evict state the
    OTHER, lagging camera was still inside the merge window for, turning
    its late sighting into a double-counted business pass."""
    published: list[Event] = []
    d = CrossCameraDeduper(published.append, WINDOW_S, cameras=("camA", "camB"))
    d.submit(_pass("PLT-P", "camA", first_seen=99.5, last_seen=100.0))
    # camA moves on 13 s; a global cutoff (113 - 12) would evict PLT-P.
    d.submit(_pass("PLT-Q", "camA", first_seen=112.5, last_seen=113.0))
    # camB's pass for PLT-P arrives late (decode backlog / watchdog
    # reconnect) but within the window of PLT-P's anchor: it MUST merge.
    d.submit(_pass("PLT-P", "camB", first_seen=100.0, last_seen=100.5))
    assert len(published) == 3
    merged = published[2]
    assert merged.event_id == published[0].event_id  # same business pass
    assert merged.revision == 1
    assert merged.cameras == {"camA": 3, "camB": 3}
    stats = d.stats()
    assert stats["passes_emitted"] == 2  # PLT-P and PLT-Q, not 3
    assert stats["cross_camera_merges"] == 1
    assert stats["forced_evictions"] == 0


def test_eviction_waits_for_slowest_seen_camera_without_camera_list() -> None:
    """Without a construction-time camera list the set is learned from the
    events: a camera that has spoken pins the cutoff at ITS high water."""
    published: list[Event] = []
    d = CrossCameraDeduper(published.append, WINDOW_S)
    d.submit(_pass("PLT-R", "camB", first_seen=94.5, last_seen=95.0))  # camB known
    d.submit(_pass("PLT-P", "camA", first_seen=99.5, last_seen=100.0))
    d.submit(_pass("PLT-Q", "camA", first_seen=112.5, last_seen=113.0))
    # cutoff = min(camB 95, camA 113) - 12 = 83: PLT-P survives.
    d.submit(_pass("PLT-P", "camB", first_seen=100.0, last_seen=100.5))
    assert published[3].event_id == published[1].event_id
    assert d.stats()["cross_camera_merges"] == 1


def test_miss_advances_its_cameras_high_water(deduper) -> None:
    """A decode drought (misses only) on one camera must not halt eviction:
    the miss's end_ts proves that camera's clock progressed."""
    d, _ = deduper
    d.submit(_pass("PLT-OLD", "camA", first_seen=1.0, last_seen=2.0))
    d.submit(_pass("PLT-OLD2", "camB", first_seen=1.0, last_seen=2.0))
    miss = MissEvent(
        candidate_id="camB-000050",
        source_id="camB",
        start_ts=98.0,
        end_ts=99.0,
        first_frame=2900,
        last_frame=2950,
        evidence_dir="/tmp/ev/y",
        evidence_frame_count=4,
        event_id=str(uuid.uuid4()),
        wall_time_iso="2026-06-11T00:00:02+00:00",
    )
    d.submit(miss)
    d.submit(_pass("PLT-NEW", "camA", first_seen=99.0, last_seen=100.0))
    # min(camA 100, camB 99) - 12 = 87: both old states are evictable.
    assert "PLT-OLD" not in d._state and "PLT-OLD2" not in d._state


def test_forced_cap_eviction_is_counted_and_logged(
    deduper, monkeypatch, caplog
) -> None:
    """7e4c22c review, finding 16 (the cut sibling of finding 1): size-cap
    eviction necessarily removes in-window state, so it must be counted
    and logged per the counted-logged-drops convention — and the evicted
    payload's next sighting double-counts, which is why it matters."""
    import logging

    import palletscan.events.dedup as dedup_mod

    d, published = deduper
    monkeypatch.setattr(dedup_mod, "_MAX_TRACKED", 4)
    with caplog.at_level(logging.WARNING, logger="palletscan.events.dedup"):
        for i in range(5):
            d.submit(
                _pass(f"PLT-C{i}", "camA", first_seen=float(i), last_seen=float(i))
            )
        # The 6th submit prunes over the cap first: PLT-C0 (oldest anchor,
        # still in-window) is force-evicted — and this very event is its
        # other-camera sighting, so it lands as a NEW business pass: the
        # double-count the counter and log make visible.
        d.submit(_pass("PLT-C0", "camB", first_seen=0.2, last_seen=0.4))
    stats = d.stats()
    assert stats["forced_evictions"] == 1
    assert any("force-evicted" in r.message for r in caplog.records)
    assert stats["passes_emitted"] == 6
    assert stats["cross_camera_merges"] == 0


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


def _hammer_submitter(
    deduper: CrossCameraDeduper,
    barrier: threading.Barrier,
    camera: str,
    decode_count: int,
    rounds: int,
) -> None:
    """One camera's submit loop, two barrier phases per round. Exits via
    BrokenBarrierError when the main thread aborts the barrier."""
    try:
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
    except threading.BrokenBarrierError:
        return  # main thread failed mid-round and released us
    except BaseException:
        # A submitter-side failure (a future deduper regression raising
        # under contention) must release the OTHER waiters too, or the
        # main thread parks forever on its timeout-less wait.
        barrier.abort()
        raise


@contextmanager
def _released_submitters(
    barrier: threading.Barrier, threads: list[threading.Thread]
) -> Iterator[None]:
    """Start the submitters and guarantee they exit even when the body
    fails between barrier phases (7e4c22c review, finding 8): without the
    abort, a mid-round assertion left both non-daemon threads parked on
    the barrier and pytest wedged at interpreter shutdown, burying the
    real failure under the CI timeout."""
    try:
        # Starts inside the try: a failed second start must still abort the
        # barrier so the first thread is not left parked forever.
        for t in threads:
            t.start()
        yield
    finally:
        barrier.abort()
        for t in threads:
            if t.ident is not None:  # join only what actually started
                t.join(timeout=10)


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
    threads = [
        threading.Thread(target=_hammer_submitter, args=(deduper, barrier, "camA", 3, rounds)),
        threading.Thread(target=_hammer_submitter, args=(deduper, barrier, "camB", 2, rounds)),
    ]

    conn: sqlite3.Connection | None = None
    try:
        with _released_submitters(barrier, threads):
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


def test_hammer_scaffold_releases_threads_on_midround_failure() -> None:
    """7e4c22c review, finding 8 (structural repro): an assertion firing
    between barrier phases must release the submitter threads instead of
    leaving them parked on the 3-party barrier forever. Daemon threads
    here so a regression fails red instead of wedging the suite."""
    deduper = CrossCameraDeduper(lambda ev: None, WINDOW_S)
    barrier = threading.Barrier(3)
    threads = [
        threading.Thread(
            target=_hammer_submitter,
            args=(deduper, barrier, camera, 1, 5),
            daemon=True,
        )
        for camera in ("camA", "camB")
    ]
    with pytest.raises(AssertionError, match="injected mid-round"):
        with _released_submitters(barrier, threads):
            barrier.wait(timeout=10)  # round 0, phase 1: submitters proceed
            assert False, "injected mid-round failure"
    for t in threads:
        assert not t.is_alive(), "submitter thread leaked past the scaffold"


def test_hammer_scaffold_releases_main_thread_on_submitter_failure() -> None:
    """The other arm of finding 8: a submitter-side exception (a future
    deduper regression raising under contention) must abort the barrier
    too, or the MAIN thread parks forever on its timeout-less wait and the
    scaffold's releasing finally is never reached."""
    deduper = CrossCameraDeduper(lambda ev: None, WINDOW_S)

    def explode(event: Event) -> None:  # the hypothetical regression
        raise KeyError("injected submit failure")

    deduper.submit = explode  # type: ignore[method-assign]
    barrier = threading.Barrier(3)
    threads = [
        threading.Thread(
            target=_hammer_submitter,
            args=(deduper, barrier, camera, 1, 5),
            daemon=True,
        )
        for camera in ("camA", "camB")
    ]
    started = time.monotonic()
    with pytest.raises(threading.BrokenBarrierError):
        with _released_submitters(barrier, threads):
            barrier.wait(timeout=10)  # phase 1: submitters call submit and die
            barrier.wait(timeout=20)  # pre-fix this only breaks via timeout
    # The abort must come from the dying submitter (immediate), not from
    # this thread's own wait timing out after parking for the full 20 s.
    assert time.monotonic() - started < 10
    for t in threads:
        assert not t.is_alive(), "submitter thread leaked past the scaffold"


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
        "forced_evictions": 0,
    }
