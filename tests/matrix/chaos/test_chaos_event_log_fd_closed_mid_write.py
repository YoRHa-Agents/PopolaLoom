"""C4 — EventLog.fd closed mid-write → clear error, no silent vanish.

Per testing-matrix.md §10 #5.  When the EventLog's underlying fd is
closed (e.g. via :meth:`EventLog.close`), subsequent ``append()`` calls
MUST raise ``RuntimeError`` per the existing contract — not silently
swallow the write.

We additionally simulate the fd being closed *out from under* the
EventLog (via os-level fd close) and assert the next ``append()``
either reopens or raises a clear ValueError/OSError.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from popolaloom.daemon.event_log import EventLog


def test_chaos_append_after_close_raises_runtimeerror(tmp_path: Path) -> None:
    """Existing contract: ``append`` after ``close`` must raise."""
    log = EventLog(tmp_path / "c4_a.jsonl", fsync_interval_s=0.0)
    log.append("task.dispatched", {"task_id": "t-1"})
    log.close()

    with pytest.raises(RuntimeError, match="closed"):
        log.append("task.completed", {"task_id": "t-1"})


def test_chaos_external_fd_close_surfaces_clear_error(
    tmp_path: Path,
    mocker,
) -> None:
    """When something external closes the fd, ``append`` must raise (not log-and-eat).

    We monkey-patch ``EventLog._fd.write`` to raise ``ValueError("I/O
    operation on closed file")`` — exactly what Python emits when you
    try to write to a closed text file object.  The append wrapper
    must NOT swallow this.
    """
    log = EventLog(tmp_path / "c4_b.jsonl", fsync_interval_s=0.0)
    try:
        mocker.patch.object(
            log._fd,
            "write",
            side_effect=ValueError("I/O operation on closed file"),
        )

        with pytest.raises(ValueError, match="closed file"):
            log.append("task.dispatched", {"task_id": "t-2"})
    finally:
        log._closed = True
        log._stop_event.set()


def test_chaos_event_log_handles_truly_closed_underlying_fd(tmp_path: Path) -> None:
    """Force-close fd → next ``append`` (with flush) raises ``ValueError``.

    Demonstrates that the fd-held buffered-writer pattern in
    :class:`EventLog` does not silently swallow underlying fd
    failures: when we force a flush by switching to unbuffered mode,
    the next write hits the closed fd and Python raises ValueError.
    """
    log = EventLog(tmp_path / "c4_c.jsonl", fsync_interval_s=0.0, buffer_bytes=1)
    try:
        os.close(log._fd.fileno())
        with pytest.raises((ValueError, OSError)):
            log.append("task.dispatched", {"task_id": "t-3"})
    finally:
        log._closed = True
        log._stop_event.set()
