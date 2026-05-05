"""Tier 2 / B5 — buffered EventLog thread-safety + close durability invariants.

Per the L3 brief:

1. Two threads append concurrently → both writes appear; no data
   corruption (uses :class:`threading.Barrier` to maximise overlap).
2. ``close()`` is idempotent + final fsync makes the file's tail
   match the in-memory write count.

Plus a couple of extra cases that pin down the buffered/fsync worker
contract (manual fsync; close-then-append raises).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from popolaloom.daemon.event_log import EventLog

# ── 1: concurrent appends — no data corruption ───────────────────────────


def test_two_threads_append_concurrently_no_data_loss(tmp_path: Path) -> None:
    """N appends across 2 threads → file has exactly N envelopes (no loss / dup).

    Uses a Barrier so both threads start at the same instant, maximising
    contention on the buffered fd's lock.
    """
    log = EventLog(tmp_path / "concur.jsonl", fsync_interval_s=0)
    n_per_thread = 200
    barrier = threading.Barrier(2)

    def worker(thread_id: int) -> None:
        barrier.wait()
        for i in range(n_per_thread):
            log.append(
                "concurrent.test",
                {"thread": thread_id, "i": i},
            )

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive()
    assert not t2.is_alive()

    log.fsync()
    events = log.tail()
    assert len(events) == 2 * n_per_thread, (
        f"expected {2 * n_per_thread} events, got {len(events)}"
    )

    seen_thread_idx = {(e["data"]["thread"], e["data"]["i"]) for e in events}
    expected = {(t, i) for t in (1, 2) for i in range(n_per_thread)}
    assert seen_thread_idx == expected
    log.close()


# ── 2: close() flushes + fsyncs; in-memory tail matches file ─────────────


def test_close_flushes_and_fsync_durability(tmp_path: Path) -> None:
    """After close(), the file content matches every append() ever made."""
    log = EventLog(tmp_path / "close.jsonl", fsync_interval_s=0)
    for i in range(50):
        log.append("close.test", {"i": i})
    log.close()

    on_disk_lines = (tmp_path / "close.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(on_disk_lines) == 50, (
        f"expected 50 lines on disk after close, got {len(on_disk_lines)}"
    )


# ── 3: close() is idempotent ────────────────────────────────────────────


def test_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close() twice doesn't raise (matches __exit__ context manager)."""
    log = EventLog(tmp_path / "idem.jsonl", fsync_interval_s=0)
    log.append("a", {"k": 1})
    log.close()
    log.close()
    assert log.closed is True


# ── 4: append after close raises (No Silent Failures) ───────────────────


def test_append_after_close_raises(tmp_path: Path) -> None:
    """Writing to a closed log MUST raise (per docstring contract)."""
    log = EventLog(tmp_path / "after.jsonl", fsync_interval_s=0)
    log.append("a", {})
    log.close()
    with pytest.raises(RuntimeError, match="closed"):
        log.append("b", {})


# ── 5: __len__ reflects flushed count ────────────────────────────────────


def test_len_reflects_count_after_flush(tmp_path: Path) -> None:
    """len(log) returns the number of appends that have been flushed."""
    log = EventLog(tmp_path / "len.jsonl", fsync_interval_s=0)
    log.append("a", {})
    log.append("b", {})
    log.append("c", {})
    assert len(log) == 3
    log.close()


# ── 6: context manager closes on exit ────────────────────────────────────


def test_context_manager_closes_on_exit(tmp_path: Path) -> None:
    """``with EventLog(...) as log:`` closes on block exit."""
    path = tmp_path / "ctx.jsonl"
    with EventLog(path, fsync_interval_s=0) as log:
        log.append("a", {"k": 1})
        assert log.closed is False
    assert log.closed is True
    on_disk = path.read_text(encoding="utf-8").splitlines()
    assert len(on_disk) == 1
