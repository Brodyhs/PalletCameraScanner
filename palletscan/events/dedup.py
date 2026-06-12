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

Threading: ``submit`` is called from each runner's bus thread. The merged
event and its revision are computed under the lock; ``publish`` happens
outside it, because the business bus's blocking put must never couple one
runner's bus thread to the other's. No held state needs expiry timers, so
idle periods and accelerated replay have no failure modes here.

Eviction keys on the SLOWEST camera's progress, not a global high water:
per-camera event timestamps are monotonic, so state older than
``min(per-camera high water) - window_s`` is unreachable by every camera,
while a lagging camera's still-mergeable state survives until that camera
itself moves past it. A camera that goes silent therefore halts time-based
eviction; the ``_MAX_TRACKED`` cap bounds memory instead, and every forced
cap eviction is counted (``forced_evictions``) and logged because the
evicted payload's next sighting double-counts as a new business pass.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from palletscan.events.sinks import Sink
from palletscan.types import Event, PassEvent

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
    event: PassEvent  # latest merged version (carries the stable event_id)
    anchor_ts: float  # first-emit close ts; never refreshed by merges


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
        self._state: dict[str, _PayloadState] = {}
        self._high_waters: dict[str, float] = {
            camera: float("-inf") for camera in cameras or ()
        }
        self.passes_emitted = 0
        self.cross_camera_merges = 0
        self.repeats_suppressed = 0
        self.reemits = 0
        self.misses_forwarded = 0
        self.forced_evictions = 0

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
        """Merge ``event`` into the payload map; returns what to publish."""
        ts = event.last_seen_ts
        camera = next(iter(event.cameras), None)
        if camera is not None:
            self._note_progress(camera, ts)
        self._prune()
        state = self._state.get(event.payload)
        if state is None or ts - state.anchor_ts > self._window_s:
            self._state[event.payload] = _PayloadState(event=event, anchor_ts=ts)
            self.passes_emitted += 1
            return event
        if camera in state.event.cameras:
            # Same camera re-sighting (e.g. its tracker window expired
            # first): suppress, and do NOT extend the anchor — a parked
            # pallet must eventually become a new business pass.
            self.repeats_suppressed += 1
            return None
        merged = self._merge(state.event, event)
        state.event = merged
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
        camera's lagging sighting arrives."""
        if self._high_waters:
            cutoff = min(self._high_waters.values()) - self._window_s
            if any(s.anchor_ts < cutoff for s in self._state.values()):
                self._state = {
                    p: s for p, s in self._state.items() if s.anchor_ts >= cutoff
                }
        over = len(self._state) - _MAX_TRACKED
        if over > 0:
            # Forced eviction: everything left is still inside the merge
            # window for at least one camera, so each evicted payload's
            # next sighting becomes a second business pass. Counted and
            # logged, never silent (the project's counted-logged-drops
            # convention).
            for payload in sorted(
                self._state, key=lambda p: self._state[p].anchor_ts
            )[:over]:
                del self._state[payload]
            self.forced_evictions += over
            log.warning(
                "dedup tracking cap (%d) exceeded: force-evicted %d "
                "in-window payload(s); the next sighting of each will "
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
                "reemits": self.reemits,
                "misses_forwarded": self.misses_forwarded,
                "forced_evictions": self.forced_evictions,
            }
