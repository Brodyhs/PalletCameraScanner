"""Full threaded PipelineRunner on easy synthetic passes."""

from __future__ import annotations

import json
import sqlite3
import threading

from palletscan.app import PipelineRunner
from palletscan.config import AppConfig


def test_easy_passes_all_decode_no_misses(fast_synth_config: AppConfig) -> None:
    cfg = fast_synth_config
    before = set(threading.enumerate())
    runner = PipelineRunner.from_config(cfg)
    summary = runner.run()

    # all 3 easy passes decode, nothing missed, nothing unaccounted
    assert summary.reconciliation is not None
    assert summary.reconciliation.truth_passes == 3
    assert summary.reconciliation.decoded == 3
    assert summary.misses == 0
    assert summary.unaccounted == 0
    assert summary.frames > 0

    # outputs landed
    jsonl = cfg.sinks.jsonl.path
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len([l for l in lines if l["kind"] == "pass"]) == 3
    conn = sqlite3.connect(cfg.sinks.sqlite.path)
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='pass'"
    ).fetchone()
    conn.close()
    assert count == 3

    # no leaked threads, queues drained
    leaked = [
        t
        for t in set(threading.enumerate()) - before
        if t.is_alive() and t.name in ("source", "pipeline", "eventbus")
    ]
    assert leaked == []
    assert runner._frame_q.qsize() == 0
    assert runner._bus.queue.qsize() == 0


def test_stop_requests_graceful_early_exit(fast_synth_config: AppConfig) -> None:
    cfg = fast_synth_config.model_copy(
        update={
            "synthetic": fast_synth_config.synthetic.model_copy(
                update={"num_passes": 50, "realtime": True}
            )
        }
    )
    runner = PipelineRunner.from_config(cfg)
    threading.Timer(0.5, runner.stop).start()
    summary = runner.run()  # must return promptly without raising
    assert summary.frames < 1000
