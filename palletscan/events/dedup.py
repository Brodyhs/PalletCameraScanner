"""Cross-camera business-event dedup for A/B mode (Phase 4, D1).

Emit-now + merge-by-reemit: the first sighting of a payload publishes the
business PassEvent immediately, keeping that camera event's ``event_id`` as
the stable business id. Another camera's pass for the same payload within
the dedup window is merged and re-published with the same event_id and a
bumped ``revision``; the revision-guarded upsert in SqliteSink absorbs
out-of-order re-emits. Same-camera repeats within the window are suppressed
and counted; the window anchor refreshes only on first emit (the
parked-pallet rule, mirroring the tracker's ASSUMPTIONS #16 semantics).
Misses forward unchanged — there is no payload to key on, and the
per-camera miss IS the A/B experiment's evidence.

A payload tracks a LIST of window entries, not one (REVIEW_SYSTEM_0c30c77
finding 9): a re-sighting beyond every entry's window becomes a new
business pass *appended* beside the old entry — never replacing it — so a
lagging camera's backdated sighting still finds the pass it belongs to.
Anchors of one payload are pairwise more than ``window_s`` apart by
construction; an event is matched to the NEAREST anchor (ties to the
older), and only a match within the window merges/suppresses. The window
is two-sided: an event staler than every anchor by more than the window is
its own (earlier) business pass, never absorbed into a newer one.

``seed()`` adds suppress-only entries bridged from the previous process's
stored passes (REVIEW finding 10): a pallet pass spanning a supervisor
restart suppresses instead of double-counting. Seeds obey the same
matching, pruning and never-refresh rules.

Threading: ``submit`` is called from each runner's bus thread. The merged
event and its revision are computed under the lock; ``publish`` happens
outside it, because the business bus's blocking put must never couple one
runner's bus thread to the other's. No held state needs expiry timers, so
idle periods and accelerated replay have no failure modes here.

Eviction keys on the SLOWEST camera's progress, not a global high water:
per-camera event timestamps are monotonic, so entries older than
``min(per-camera high water) - window_s`` are unreachable by every camera,
while a lagging camera's still-mergeable entry survives until that camera
itself moves past it. A camera that goes silent therefore halts time-based
eviction; the ``_MAX_TRACKED`` cap bounds memory instead, and every forced
cap eviction is counted (``forced_evictions``) and logged because the
evicted payload's next sighting double-counts as a new business pass.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from palletscan.events.sinks import Sink
from palletscan.types import Event, PassEvent, iso_at

log = logging.getLogger(__name__)

#: Hard cap on tracked payloads; lazy slowest-camera pruning keeps the map
#: far smaller in practice (~window_s worth of distinct payloads). The cap
#: only binds when a camera goes silent (its lag becomes unknowable, so
#: time-based eviction stops); hitting it force-evicts in-window state —
#: counted in ``forced_evictions`` and logged.
_MAX_TRACKED = 4096


class ForwardingSink(Sink):
    """Bridges one runner's per-camera bus into the shared deduper.

    ``close()`` is a deliberate no-op: each runner's bus closes its sinks
    on shutdown, but the deduper and the business bus outlive any single
    runner — the double-close firewall.
    """

    def __init__(self, deduper: "CrossCameraDeduper") -> None:
        self._deduper = deduper

    def handle(self, event: Event) -> None:
        self._deduper.submit(event)


@dataclass(slots=True)
class _PayloadState:
    """One business-pass window entry for a payload.

    ``event is None`` marks a restart seed (REVIEW finding 10): the pass
    was emitted and stored by the *previous* process, so a sighting
    matching it is suppressed — there is nothing in-memory to merge into.
    """

    anchor_ts: float  # first-emit close ts; never refreshed by merges
    event: PassEvent | None = None  # latest merged version (stable event_id)


class CrossCameraDeduper:
    """Collapses per-camera PassEvents into business events by payload.

    ``cameras`` names the camera set feeding this deduper (StationRunner
    passes its source ids). Until every named camera has reported at least
    one event, no time-based eviction happens — a camera's lag is unknown
    until it speaks, and evicting state another camera can still merge
    with double-counts that pallet (finding 1 of the 7e4c22c review).
    Without ``cameras`` the set is learned from the events themselves.
    """

    def __init__(
        self,
        publish: Callable[[Event], None],
        window_s: float,
        cameras: Sequence[str] | None = None,
    ) -> None:
        self._publish = publish
        self._window_s = window_s
        self._lock = threading.Lock()
        self._state: dict[str, list[_PayloadState]] = {}
        self._high_waters: dict[str, float] = {
            camera: float("-inf") for camera in cameras or ()
        }
        self.passes_emitted = 0
        self.cross_camera_merges = 0
        self.repeats_suppressed = 0
        self.restart_repeats_suppressed = 0
        self.reemits = 0
        self.misses_forwarded = 0
        self.forced_evictions = 0

    def seed(self, anchors: dict[str, float]) -> None:
        """Install suppress-only window entries bridged from the previous
        process's stored passes (anchors already mapped into THIS process's
        source clock; they are <= 0, before process start).

        A pallet pass spanning a supervisor restart then suppresses instead
        of double-counting as a second business pass (REVIEW finding 10).
        Seeds obey the same nearest-anchor matching, the parked-pallet
        never-refresh rule, and the slowest-camera pruning. Documented
        cost: a suppressed sighting writes no camera_detail into the stored
        business row, so the A/B report loses the post-restart camera's
        datapoint for restart-spanning passes — and because the downstream
        camera is always the post-restart one for a pallet in transit, the
        loss is direction-biased (see ASSUMPTIONS).
        """
        with self._lock:
            for payload, anchor in anchors.items():
                self._state.setdefault(payload, []).append(
                    _PayloadState(anchor_ts=anchor, event=None)
                )

    def submit(self, event: Event) -> None:
        """Route one per-camera event; called from any runner's bus thread."""
        if not isinstance(event, PassEvent):
            with self._lock:
                self.misses_forwarded += 1
                # A miss still proves its camera's clock reached end_ts:
                # per-camera emission is in source-clock order, so eviction
                # keeps progressing through decode droughts.
                self._note_progress(event.source_id, event.end_ts)
            self._publish(event)
            return
        with self._lock:
            out = self._absorb(event)
        # Publish outside the lock: a full business queue must stall only
        # the submitting runner's bus thread, never the other runner's.
        if out is not None:
            self._publish(out)

    def _note_progress(self, camera: str, ts: float) -> None:
        """Advance one camera's high-water mark (under the lock)."""
        if ts > self._high_waters.get(camera, float("-inf")):
            self._high_waters[camera] = ts

    def _absorb(self, event: PassEvent) -> PassEvent | None:
        """Match ``event`` against the payload's window entries; returns
        what to publish (None = suppressed)."""
        ts = event.last_seen_ts
        camera = next(iter(event.cameras), None)
        if camera is not None:
            self._note_progress(camera, ts)
        self._prune()
        entries = self._state.get(event.payload, [])
        # Nearest anchor wins, ties to the older. Anchors of one payload
        # are pairwise > window apart, but two can still BOTH be within the
        # window of one event (spacing in (window, 2*window], reachable via
        # a lagging camera's backdated close): an unspecified pick here
        # would merge the sighting into the wrong physical pass — the exact
        # misattribution finding 9 closes.
        best: _PayloadState | None = None
        if entries:
            best = min(
                entries, key=lambda s: (abs(ts - s.anchor_ts), s.anchor_ts)
            )
            if abs(ts - best.anchor_ts) > self._window_s:
                best = None
        if best is None:
            # No entry within the window on EITHER side: a new business
            # pass. Two-sided on purpose — an event staler than every
            # anchor by more than the window is an earlier physical pass,
            # never absorbed into a newer one. Appended, never replacing:
            # the old entry stays mergeable for cameras still behind it.
            self._state.setdefault(event.payload, []).append(
                _PayloadState(anchor_ts=ts, event=event)
            )
            self.passes_emitted += 1
            return event
        if best.event is None:
            # Restart seed: the previous process already emitted and stored
            # this pass within the window (REVIEW finding 10). Suppress —
            # and do NOT refresh the anchor (parked-pallet rule), so the
            # pallet eventually becomes a genuine new business pass.
            self.restart_repeats_suppressed += 1
            log.info(
                "pass %s suppressed: emitted by the previous run %.1fs "
                "before this process started (restart-spanning dedup)",
                event.payload,
                -best.anchor_ts,
            )
            return None
        if camera in best.event.cameras:
            # Same camera re-sighting (e.g. its tracker window expired
            # first): suppress, and do NOT extend the anchor — a parked
            # pallet must eventually become a new business pass.
            self.repeats_suppressed += 1
            return None
        merged = self._merge(best.event, event)
        best.event = merged
        self.cross_camera_merges += 1
        self.reemits += 1
        return merged

    @staticmethod
    def _merge(base: PassEvent, new: PassEvent) -> PassEvent:
        """Fold ``new`` (a single-camera pass) into the business event."""
        first_decodes = [
            t for t in (base.first_decode_ts, new.first_decode_ts) if t is not None
        ]
        # Earlier first decode owns best_frame (cross-source ts compare:
        # display-grade under clock skew, exact for same-schedule synth).
        best = base.best_frame
        if (
            base.first_decode_ts is None
            or (
                new.first_decode_ts is not None
                and new.first_decode_ts < base.first_decode_ts
            )
        ):
            best = new.best_frame
        cameras = dict(base.cameras)
        for cam, n in new.cameras.items():
            cameras[cam] = cameras.get(cam, 0) + n
        detail = dict(base.camera_detail or {})
        detail.update(new.camera_detail or {})
        return dataclasses.replace(
            base,
            first_seen_ts=min(base.first_seen_ts, new.first_seen_ts),
            last_seen_ts=max(base.last_seen_ts, new.last_seen_ts),
            decode_count=base.decode_count + new.decode_count,
            cameras=cameras,
            best_frame=best,
            candidate_ids=base.candidate_ids + new.candidate_ids,
            first_decode_ts=min(first_decodes) if first_decodes else None,
            camera_detail=detail or None,
            revision=base.revision + 1,
        )

    def _prune(self) -> None:
        """Drop entries no camera can merge with any more (lazy, under the
        lock). Anything newer than the slowest camera's high water minus the
        window stays: evicting it would double-count the pallet when that
        camera's lagging sighting arrives. Payload keys whose entry lists
        empty out are removed entirely."""
        if self._high_waters:
            cutoff = min(self._high_waters.values()) - self._window_s
            if any(
                s.anchor_ts < cutoff
                for entries in self._state.values()
                for s in entries
            ):
                pruned: dict[str, list[_PayloadState]] = {}
                for p, entries in self._state.items():
                    kept = [s for s in entries if s.anchor_ts >= cutoff]
                    if kept:
                        pruned[p] = kept
                self._state = pruned
        total = sum(len(entries) for entries in self._state.values())
        over = total - _MAX_TRACKED
        if over > 0:
            # Forced eviction: everything left is still inside the merge
            # window for at least one camera, so each evicted entry's
            # next sighting becomes a second business pass. Counted and
            # logged, never silent (the project's counted-logged-drops
            # convention).
            oldest = sorted(
                (
                    (s.anchor_ts, payload, s)
                    for payload, entries in self._state.items()
                    for s in entries
                ),
                key=lambda item: item[0],
            )[:over]
            for _, payload, state in oldest:
                entries = self._state[payload]
                entries.remove(state)
                if not entries:
                    del self._state[payload]
            self.forced_evictions += over
            log.warning(
                "dedup tracking cap (%d) exceeded: force-evicted %d "
                "in-window entr(ies); the next sighting of each will "
                "double-count as a new business pass",
                _MAX_TRACKED,
                over,
            )

    def stats(self) -> dict[str, int]:
        """Business counters for /stats.json and the station summary."""
        with self._lock:
            return {
                "passes_emitted": self.passes_emitted,
                "cross_camera_merges": self.cross_camera_merges,
                "repeats_suppressed": self.repeats_suppressed,
                "restart_repeats_suppressed": self.restart_repeats_suppressed,
                "reemits": self.reemits,
                "misses_forwarded": self.misses_forwarded,
                "forced_evictions": self.forced_evictions,
            }


#: Anchors later than this past process start are clock-step artifacts: a
#: stored pass can never legitimately have happened after the new process's
#: ts=0, so a positive anchor means the wall clock stepped backward between
#: runs (Windows w32time steps, it does not slew). Kept, it would suppress
#: a GENUINE first sighting — discard and log instead.
_SEED_FUTURE_TOLERANCE_S = 0.5


def load_restart_seeds(
    db_path: Path | str,
    window_s: float,
    epoch_wall: float,
    *,
    camera: str | None = None,
    slack_s: float = 60.0,
) -> dict[str, float]:
    """payload -> dedup anchor (this process's source clock, <= 0) from the
    previous run's stored passes (REVIEW finding 10).

    The bridge: stored ``wall_time_iso`` stamps minus this process's
    ``epoch_wall`` (the wall instant of ts=0). Only rows within
    ``window_s + slack_s`` of process start can still matter. ``camera``
    filters to rows whose merged ``cameras`` map includes that source —
    per-camera tracker seeding must mirror per-camera dedup semantics.

    Never raises and never creates the database: a fresh data dir (no DB,
    or a DB whose lazy migration has not run) yields no seeds — startup
    must not be blockable by its own bookkeeping.
    """
    path = Path(db_path)
    if not path.is_file():
        return {}
    cutoff_iso = iso_at(epoch_wall - window_s - slack_s)
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT payload, wall_time_iso, detail_json FROM events "
                "WHERE kind='pass' AND payload IS NOT NULL "
                "AND wall_time_iso >= ?",
                (cutoff_iso,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        log.debug("no restart dedup seeds: %s unreadable or unmigrated", path)
        return {}
    latest: dict[str, float] = {}
    skipped_future = 0
    for payload, wall_iso, detail_json in rows:
        if camera is not None:
            try:
                cameras = json.loads(detail_json).get("cameras", {})
            except (ValueError, TypeError):
                cameras = {}
            if camera not in cameras:
                continue
        try:
            wall = datetime.fromisoformat(wall_iso).timestamp()
        except (ValueError, TypeError):
            continue
        anchor = wall - epoch_wall
        if anchor > _SEED_FUTURE_TOLERANCE_S:
            skipped_future += 1
            continue
        anchor = min(anchor, 0.0)
        if payload not in latest or anchor > latest[payload]:
            latest[payload] = anchor
    if skipped_future:
        log.warning(
            "discarded %d restart dedup seed(s) stamped after this "
            "process's start — wall clock stepped backward between runs?",
            skipped_future,
        )
    return latest
