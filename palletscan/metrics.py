"""MetricsRegistry: per-pipeline metrics with one stable snapshot contract.

One registry per :class:`~palletscan.app.PipelineRunner` — no globals.
Components report through narrow hooks (``record_frame``,
``record_decode_wall_ms``, ``record_pass``/``record_miss``) or are read
lazily at snapshot time via registered gauge callables, so the counters
Phase 1 already maintains stay the single source of truth.

Thread-safety model: every recording hook has exactly one writer thread
(frames + decode wall time: pipeline thread; pass/miss: bus thread), and
``snapshot()`` may be called from any thread. Reads rely on the GIL's
atomicity for ``deque.append``/``list(deque)`` and are *approximate by
design* — a snapshot taken mid-frame may be one count off, which is fine
for operational stats and avoids locks on the hot path.

Clocks: fps and uptime use the wall clock (``time.monotonic``); pass/miss
rates use the frame **source clock** (``Frame.ts``), so accelerated replay
reports rates in source time, consistent with the rest of the pipeline
(ASSUMPTIONS #15).

``snapshot()`` returns the dict that Phase 4's ``/stats.json`` will serve
verbatim; treat its key structure as a contract.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from palletscan.config import MetricsConfig

#: Source-time horizons for pass/miss rates (spec §6: "rolling 1h/24h").
_HOUR_S = 3600.0
_DAY_S = 86400.0

#: Minimum source-time span used when extrapolating passes/hour, so a few
#: seconds of data cannot extrapolate to absurd hourly rates.
_MIN_RATE_SPAN_S = 60.0

#: Gauge names the snapshot consumes. ``register_gauges`` rejects anything
#: else so a typo cannot silently produce a dead metric.
_KNOWN_GAUGES = frozenset(
    {
        "frames_processed",
        "frames_dropped",
        "frame_errors",
        "passes_emitted",
        "passes_merged",
        "misses_emitted",
        "events_handled",
        "sink_errors",
        "pyzbar_calls",
        "dmtx_calls",
        "fallback_calls",
        "budget_overruns",
        "source_stalls",
        "source_reconnects",
        "source_reopen_failures",
        "source_zombie_readers",
    }
)


def percentile(sorted_samples: list[float], q: float) -> float | None:
    """Nearest-rank percentile of pre-sorted samples; None when empty."""
    if not sorted_samples:
        return None
    idx = round(q * (len(sorted_samples) - 1))
    return sorted_samples[idx]


class _SecondBuckets:
    """Per-second count buckets over a sliding wall-clock window.

    Bounded memory at any input rate (unlike a timestamp deque). Single
    writer; readers may see a bucket mid-reset, which is at most one
    second of miscount in a >=1 s window.
    """

    def __init__(self, window_s: float) -> None:
        self.window_s = max(1, int(round(window_s)))
        # +1 so the current partial second never aliases the oldest bucket.
        self._counts = [0] * (self.window_s + 1)
        self._stamps = [-1] * (self.window_s + 1)

    def add(self, now: float) -> None:
        sec = int(now)
        i = sec % len(self._counts)
        if self._stamps[i] != sec:
            self._stamps[i] = sec
            self._counts[i] = 0
        self._counts[i] += 1

    def rate(self, now: float, elapsed_s: float) -> float:
        """Events/second over the window (clipped to ``elapsed_s`` early on).

        The counted buckets span ``window_s - 1`` complete seconds plus the
        current partial one, so divide by that actual coverage — dividing
        by the full window would bias a steady rate low by up to
        ``1/window_s``, sawtoothing once per wall-clock second.
        """
        sec = int(now)
        floor = sec - self.window_s
        total = sum(
            c for c, s in zip(self._counts, self._stamps) if floor < s <= sec
        )
        coverage = self.window_s - 1 + (now - sec)
        span = max(min(elapsed_s, coverage), 1e-9)
        return total / span


class _SourceTimeWindow:
    """Source-timestamp ring for rolling-window event counts.

    Single writer appends monotonically-ish increasing timestamps; pruning
    on append bounds memory, ``maxlen`` is the hard safety net.
    """

    def __init__(self, window_s: float, cap: int = 100_000) -> None:
        self._window_s = window_s
        self._ts: deque[float] = deque(maxlen=cap)

    def add(self, ts: float) -> None:
        self._ts.append(ts)
        while self._ts and self._ts[0] < ts - self._window_s:
            self._ts.popleft()

    def count_since(self, anchor_ts: float) -> int:
        """Events with ``ts >= anchor_ts - window``; copy-first (GIL-atomic)
        so a concurrent append cannot break iteration."""
        cutoff = anchor_ts - self._window_s
        return sum(1 for t in list(self._ts) if t >= cutoff)


class MetricsRegistry:
    """Per-pipeline metrics: recording hooks in, one ``snapshot()`` out."""

    def __init__(
        self, cfg: MetricsConfig, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._started = clock()
        self._fps = _SecondBuckets(cfg.window_s)
        self._latency_ms: deque[float] = deque(maxlen=cfg.latency_samples)
        self._passes = _SourceTimeWindow(_HOUR_S)
        self._misses = _SourceTimeWindow(_HOUR_S)
        # 24 h pair: ~10k pallets/day of bare timestamps is trivially cheap
        # under the 100k ring cap.
        self._passes_24h = _SourceTimeWindow(_DAY_S)
        self._misses_24h = _SourceTimeWindow(_DAY_S)
        self._gauges: dict[str, Callable[[], int]] = {}
        self._queues: dict[str, Callable[[], int]] = {}
        self._outbox_probe: Callable[[], dict[str, Any]] | None = None
        # Source-clock span observed at pipeline ingest (anchors the 1h
        # windows, so rates decay during idle instead of freezing at the
        # last event). Plain float writes are GIL-atomic.
        self._first_frame_ts: float | None = None
        self._last_frame_ts: float | None = None
        #: Wall clock of the first ingested frame (None until then) — the
        #: "pipeline is processing again" edge for restart-gap measurement.
        self.first_frame_wall: float | None = None

    # -- recording hooks (single writer each) --------------------------------

    def record_frame(self, source_ts: float) -> None:
        """Pipeline-ingest hook: one call per frame entering the pipeline."""
        now = self._clock()
        self._fps.add(now)
        if self._first_frame_ts is None:
            self._first_frame_ts = source_ts
            self.first_frame_wall = now
        self._last_frame_ts = source_ts

    def record_decode_wall_ms(self, ms: float) -> None:
        """Per-frame decode wall time, including frames that decoded nothing."""
        self._latency_ms.append(ms)

    def record_pass(self, source_ts: float) -> None:
        self._passes.add(source_ts)
        self._passes_24h.add(source_ts)

    def record_miss(self, source_ts: float) -> None:
        self._misses.add(source_ts)
        self._misses_24h.add(source_ts)

    # -- wiring ---------------------------------------------------------------

    def register_gauges(self, **fns: Callable[[], int]) -> None:
        """Register lazy counter reads by canonical name (see module doc)."""
        unknown = set(fns) - _KNOWN_GAUGES
        if unknown:
            raise ValueError(f"unknown gauge name(s): {sorted(unknown)}")
        self._gauges.update(fns)

    def register_queue(self, name: str, depth_fn: Callable[[], int]) -> None:
        """Register a queue whose depth is sampled at snapshot time."""
        self._queues[name] = depth_fn

    def set_outbox_probe(self, fn: Callable[[], dict[str, Any]]) -> None:
        """Install the HTTP sink's outbox stats callable (depth, oldest age)."""
        self._outbox_probe = fn

    # -- snapshot ---------------------------------------------------------------

    def _gauge(self, name: str) -> int:
        fn = self._gauges.get(name)
        return fn() if fn is not None else 0

    def snapshot(self) -> dict[str, Any]:
        """The stable stats contract (Phase 4 serves this as /stats.json)."""
        now = self._clock()
        uptime_s = now - self._started
        lat = sorted(self._latency_ms)

        first, last = self._first_frame_ts, self._last_frame_ts
        per_hour = 0.0
        read_rate: float | None = None
        read_rate_24h: float | None = None
        if first is not None and last is not None:
            span = min(_HOUR_S, max(last - first, _MIN_RATE_SPAN_S))
            p = self._passes.count_since(last)
            m = self._misses.count_since(last)
            per_hour = p * _HOUR_S / span
            if p + m:
                read_rate = p / (p + m)
            p24 = self._passes_24h.count_since(last)
            m24 = self._misses_24h.count_since(last)
            if p24 + m24:
                read_rate_24h = p24 / (p24 + m24)

        return {
            "uptime_s": round(uptime_s, 3),
            "fps": round(self._fps.rate(now, uptime_s), 2),
            "frames": {
                "processed": self._gauge("frames_processed"),
                "dropped": self._gauge("frames_dropped"),
                "errors": self._gauge("frame_errors"),
            },
            "queues": {name: fn() for name, fn in self._queues.items()},
            "decode": {
                "p50_ms": percentile(lat, 0.50),
                "p95_ms": percentile(lat, 0.95),
                "samples": len(lat),
                "pyzbar_calls": self._gauge("pyzbar_calls"),
                "dmtx_calls": self._gauge("dmtx_calls"),
                "fallback_calls": self._gauge("fallback_calls"),
                "budget_overruns": self._gauge("budget_overruns"),
            },
            "passes": {
                "emitted": self._gauge("passes_emitted"),
                "merged": self._gauge("passes_merged"),
                "per_hour": round(per_hour, 2),
            },
            "misses": {"emitted": self._gauge("misses_emitted")},
            "source": {
                "stalls": self._gauge("source_stalls"),
                "reconnects": self._gauge("source_reconnects"),
                "reopen_failures": self._gauge("source_reopen_failures"),
                "zombie_readers": self._gauge("source_zombie_readers"),
            },
            "read_rate_1h": read_rate,
            "read_rate_24h": read_rate_24h,
            "events": {
                "handled": self._gauge("events_handled"),
                "sink_errors": self._gauge("sink_errors"),
            },
            "outbox": self._outbox_probe() if self._outbox_probe else None,
        }
