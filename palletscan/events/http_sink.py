"""HTTP POST sink with an on-disk store-and-forward outbox.

Outbox pattern, two stages. The bus thread's :meth:`HttpSink.handle` does
one fast local thing — INSERT the event JSON into a SQLite outbox (WAL
mode, one connection per thread) — so a dead endpoint can never stall
event flow: offline-first by construction. A dedicated uploader thread
drains the outbox in insertion order: POST, 2xx deletes the row, any
failure backs off exponentially (jittered, success resets) and retries.

Delivery contract: **one event per POST**, body = the event JSON, any 2xx
is the ack. Semantics are **at-least-once** — a crash between POST and
DELETE re-sends, so receivers dedupe on ``event_id``. Non-2xx responses
are retried indefinitely (an endpoint misconfiguration must not silently
discard events), and redirects are treated as failures rather than
followed — urllib would convert a redirected POST into a body-less GET,
acking an event the receiver never saw; point ``url`` at the final
endpoint. The size/age caps bound the backlog, pruning oldest-first with
a counted and logged drop — account-for-everything applies to the outbox
too.

``close()`` stops the uploader after the in-flight attempt; pending rows
survive on disk and drain on the next start. That persistence *is* the
store-and-forward guarantee.

SQLite over JSONL segments: transactional ack (no partial-line corruption
on crash), trivial size/age caps, stdlib-only. The sender is stdlib
``urllib.request`` — no new runtime dependency.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from palletscan.config import HttpSinkConfig
from palletscan.events.sinks import Sink, event_to_dict
from palletscan.types import Event

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    body TEXT NOT NULL,
    enqueued_utc REAL NOT NULL
);
PRAGMA user_version = 1;
"""

#: Uploader idle poll when the outbox is empty (a wake event makes new
#: inserts effectively immediate; this is only the fallback cadence).
_IDLE_POLL_S = 0.5

def _init_db(path: Path) -> None:
    """Create the schema and switch to WAL, once, single-threaded.

    The rollback->WAL transition needs an exclusive lock and SQLite returns
    SQLITE_BUSY *immediately* (no busy-handler retry) if another connection
    is mid-initialization — so this must happen before the bus and uploader
    threads open their own connections.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    """Per-thread connection; the database is already WAL-initialized."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn 3xx into HTTPError: a redirected POST silently becomes a GET
    without the body, which would count an undelivered event as acked."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None

class HttpSink(Sink):
    """Store-and-forward HTTP sink (see module docstring for the contract)."""

    def __init__(
        self, cfg: HttpSinkConfig, clock: Callable[[], float] = time.time
    ) -> None:
        self._cfg = cfg
        self._clock = clock
        self._path = Path(cfg.outbox_path)
        self._bus_conn: sqlite3.Connection | None = None  # lazy, bus thread
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.delivered = 0
        self.upload_failures = 0
        self.dropped = 0
        # Seq the uploader is currently POSTing; the pruner skips it so a
        # cap-prune cannot drop (and double-count) an in-flight event.
        self._in_flight_seq: int | None = None
        self._opener = urllib.request.build_opener(_NoRedirect())
        _init_db(self._path)
        # Drain any backlog from a previous run even if no new events arrive.
        self._uploader = threading.Thread(
            target=self._upload_loop, name="http-uploader", daemon=True
        )
        self._uploader.start()

    # -- bus thread ------------------------------------------------------------

    def handle(self, event: Event) -> None:
        """Fast local enqueue only — never touches the network."""
        if self._bus_conn is None:
            self._bus_conn = _connect(self._path)
        body = json.dumps(event_to_dict(event))
        now = self._clock()
        with self._bus_conn:
            self._bus_conn.execute(
                "INSERT INTO outbox (event_id, body, enqueued_utc) VALUES (?,?,?)",
                (event.event_id, body, now),
            )
        self._prune(self._bus_conn, now)
        self._wake.set()

    def _prune(self, conn: sqlite3.Connection, now: float) -> None:
        """Enforce age/size caps, oldest first, counting every drop."""
        in_flight = self._in_flight_seq  # snapshot once (uploader mutates)
        cutoff = now - self._cfg.max_age_days * 86400.0
        with conn:
            aged = conn.execute(
                "DELETE FROM outbox WHERE enqueued_utc < ? "
                "AND seq IS NOT ?",
                (cutoff, in_flight),
            ).rowcount
        if aged > 0:
            self.dropped += aged
            log.warning(
                "outbox dropped %d events older than %.1f days (total dropped %d)",
                aged,
                self._cfg.max_age_days,
                self.dropped,
            )
        cap_bytes = self._cfg.max_mb * 1024 * 1024
        (size,) = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(body)), 0) FROM outbox"
        ).fetchone()
        pruned = 0
        while size > cap_bytes:
            row = conn.execute(
                "SELECT seq, LENGTH(body) FROM outbox WHERE seq IS NOT ? "
                "ORDER BY seq LIMIT 1",
                (in_flight,),
            ).fetchone()
            if row is None:
                break  # only the in-flight row remains; uploader owns it
            seq, length = row
            with conn:
                deleted = conn.execute(
                    "DELETE FROM outbox WHERE seq = ?", (seq,)
                ).rowcount
            # If the uploader delivered this row meanwhile, the size still
            # shrank by its length — only the drop accounting differs.
            size -= length
            pruned += deleted
        if pruned > 0:
            self.dropped += pruned
            log.warning(
                "outbox over %.1f MB; dropped %d oldest events (total dropped %d)",
                self._cfg.max_mb,
                pruned,
                self.dropped,
            )

    # -- uploader thread ---------------------------------------------------------

    def _post(self, body: str) -> int:
        req = urllib.request.Request(
            self._cfg.url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json", **self._cfg.headers},
            method="POST",
        )
        with self._opener.open(req, timeout=self._cfg.timeout_s) as resp:
            return int(resp.status)

    def _backoff(self, failures: int, status: object) -> None:
        """Jittered exponential wait, capped at retry.cap_s (the documented
        ceiling). stop() interrupts it immediately."""
        delay = min(
            self._cfg.retry.cap_s,
            self._cfg.retry.base_s
            * 2 ** min(failures - 1, 16)
            * random.uniform(0.5, 1.5),
        )
        log.warning(
            "outbox attempt failed (status=%s, attempt %d); retrying in %.1fs",
            status,
            failures,
            delay,
        )
        self._stop.wait(delay)

    def _upload_loop(self) -> None:
        """Drain loop. Transient SQLite errors (locked database, full disk)
        are retried with the same backoff as network failures — this thread
        must outlive anything short of process death, or delivery silently
        halts while the outbox fills."""
        conn = _connect(self._path)
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    row = conn.execute(
                        "SELECT seq, body FROM outbox ORDER BY seq LIMIT 1"
                    ).fetchone()
                except sqlite3.Error as exc:
                    failures += 1
                    self._backoff(failures, f"sqlite:{exc}")
                    continue
                if row is None:
                    self._wake.clear()
                    self._wake.wait(_IDLE_POLL_S)
                    continue
                seq, body = row
                # Guarded from just before the POST until after the ack
                # DELETE; cleared before any backoff so a down endpoint
                # cannot shield the oldest row from the age/size caps.
                self._in_flight_seq = seq
                status: object
                try:
                    code = self._post(body)
                    ok = 200 <= code < 300
                    status = code
                except urllib.error.HTTPError as exc:
                    status, ok = exc.code, False
                except Exception as exc:
                    status, ok = repr(exc), False
                if not ok:
                    self._in_flight_seq = None
                    self.upload_failures += 1
                    failures += 1
                    self._backoff(failures, status)
                    continue
                try:
                    with conn:
                        acked = conn.execute(
                            "DELETE FROM outbox WHERE seq = ?", (seq,)
                        ).rowcount
                except sqlite3.Error as exc:
                    # Delivered but not acked: the row re-sends later
                    # (at-least-once, receiver dedupes on event_id).
                    self._in_flight_seq = None
                    failures += 1
                    self._backoff(failures, f"sqlite-ack:{exc}")
                    continue
                self._in_flight_seq = None
                self.delivered += 1
                failures = 0
                if acked == 0:  # pragma: no cover - microsecond race
                    # The pruner won the race and counted this row dropped —
                    # but it WAS delivered; reconcile the ledger.
                    self.dropped = max(0, self.dropped - 1)
                    log.info(
                        "event seq=%d pruned mid-flight but delivered; "
                        "drop count corrected",
                        seq,
                    )
        except Exception:  # pragma: no cover - defensive
            log.exception("http uploader thread failed; outbox keeps queuing")
        finally:
            conn.close()

    # -- any thread -------------------------------------------------------------

    def outbox_stats(self) -> dict[str, Any]:
        """Metrics probe (snapshot-rate calls; opens a short-lived reader)."""
        try:
            conn = sqlite3.connect(self._path)
            try:
                depth, oldest = conn.execute(
                    "SELECT COUNT(*), MIN(enqueued_utc) FROM outbox"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:  # outbox not created yet
            depth, oldest = 0, None
        return {
            "depth": int(depth),
            "oldest_age_s": (
                round(self._clock() - oldest, 1) if oldest is not None else None
            ),
            "delivered": self.delivered,
            "upload_failures": self.upload_failures,
            "dropped": self.dropped,
        }

    def close(self) -> None:
        """Stop the uploader after its in-flight attempt; rows persist."""
        self._stop.set()
        self._wake.set()
        self._uploader.join(timeout=self._cfg.timeout_s + 5.0)
        if self._uploader.is_alive():  # pragma: no cover - defensive
            log.error("http uploader did not stop in time")
        if self._bus_conn is not None:
            self._bus_conn.close()
            self._bus_conn = None
