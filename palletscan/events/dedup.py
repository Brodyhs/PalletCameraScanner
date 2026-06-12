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
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from palletscan.events.sinks import Sink
from palletscan.types import Event, PassEvent

log = logging.getLogger(__name__)

#: Hard cap on tracked payloads; lazy high-water pruning keeps the map far
#: smaller in practice (~window_s worth of distinct payloads).
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
    """Collapses per-camera PassEvents into business events by payload."""

    def __init__(
        self, publish: Callable[[Event], None], window_s: float
    ) -> None:
        self._publish = publish
        self._window_s = window_s
        self._lock = threading.Lock()
        self._state: dict[str, _PayloadState] = {}
        self._high_water = float("-inf")
        self.passes_emitted = 0
        self.cross_camera_merges = 0
        self.repeats_suppressed = 0
        self.reemits = 0
        self.misses_forwarded = 0

    def submit(self, event: Event) -> None:
        """Route one per-camera event; called from any runner's bus thread."""
        if not isinstance(event, PassEvent):
            with self._lock:
                self.misses_forwarded += 1
            self._publish(event)
            return
        with self._lock:
            out = self._absorb(event)
        # Publish outside the lock: a full business queue must stall only
        # the submitting runner's bus thread, never the other runner's.
        if out is not None:
            self._publish(out)

    def _absorb(self, event: PassEvent) -> PassEvent | None:
        """Merge ``event`` into the payload map; returns what to publish."""
        ts = event.last_seen_ts
        self._prune(ts)
        state = self._state.get(event.payload)
        if state is None or ts - state.anchor_ts > self._window_s:
            self._state[event.payload] = _PayloadState(event=event, anchor_ts=ts)
            self.passes_emitted += 1
            return event
        camera = next(iter(event.cameras), None)
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

    def _prune(self, ts: float) -> None:
        """Drop entries that can no longer merge (lazy, under the lock)."""
        self._high_water = max(self._high_water, ts)
        cutoff = self._high_water - self._window_s
        if len(self._state) > _MAX_TRACKED or any(
            s.anchor_ts < cutoff for s in self._state.values()
        ):
            self._state = {
                p: s for p, s in self._state.items() if s.anchor_ts >= cutoff
            }
        while len(self._state) > _MAX_TRACKED:
            oldest = min(self._state, key=lambda p: self._state[p].anchor_ts)
            del self._state[oldest]

    def stats(self) -> dict[str, int]:
        """Business counters for /stats.json and the station summary."""
        with self._lock:
            return {
                "passes_emitted": self.passes_emitted,
                "cross_camera_merges": self.cross_camera_merges,
                "repeats_suppressed": self.repeats_suppressed,
                "reemits": self.reemits,
                "misses_forwarded": self.misses_forwarded,
            }
