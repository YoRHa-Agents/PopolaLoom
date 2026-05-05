"""C11 — Disk full during NDJSON write (ENOSPC) → state=failed, log captured.

Per testing-matrix.md §10 #11.  When the EventLog's underlying fd
write returns ``OSError(ENOSPC)``, the ``append`` call MUST NOT
silently swallow the error — the writer's caller (Supervisor /
Popolad) needs to know the event log is broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popolaloom.daemon.event_log import EventLog


def test_chaos_event_log_write_enospc_raises(
    tmp_path: Path,
    mocker,
) -> None:
    """Mocked ``OSError(ENOSPC)`` from fd.write → propagates."""
    log = EventLog(tmp_path / "c11.jsonl", fsync_interval_s=0.0)
    try:
        mocker.patch.object(
            log._fd,
            "write",
            side_effect=OSError(28, "No space left on device"),
        )

        with pytest.raises(OSError) as exc:
            log.append("task.dispatched", {"task_id": "t-c11"})
        assert exc.value.errno == 28
    finally:
        log._closed = True
        log._stop_event.set()


def test_chaos_event_log_fsync_oserror_logged_not_raised(
    tmp_path: Path,
    mocker,
    caplog,
) -> None:
    """``os.fsync`` raising during fsync worker → logged as WARNING, not raised.

    The ``_do_fsync_locked`` helper deliberately catches OSError so the
    background fsync worker doesn't die — but it MUST log a WARNING so
    operators see the disk pressure.
    """
    import logging
    log = EventLog(tmp_path / "c11_fsync.jsonl", fsync_interval_s=0.0)
    try:
        log.append("task.dispatched", {"task_id": "t-c11-fsync"})
        mocker.patch("popolaloom.daemon.event_log.os.fsync", side_effect=OSError(28, "ENOSPC"))

        with caplog.at_level(logging.WARNING, logger="popolaloom.daemon.event_log"):
            log.fsync()

        assert any("fsync failed" in r.message for r in caplog.records), (
            "fsync OSError must be logged as WARNING (No Silent Failures)"
        )
    finally:
        log._closed = True
        log._stop_event.set()
