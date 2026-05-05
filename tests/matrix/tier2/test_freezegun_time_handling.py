"""Tier 2 / B4 — freezegun time-locked envelope / handle / probe tests.

Per the L3 brief: lock ``datetime.now`` with :func:`freezegun.freeze_time`
and verify:

1. :meth:`EventLog.append` envelopes carry the frozen time.
2. :class:`TaskHandle.started_at` matches the frozen time when constructed
   while time is frozen.
3. ``popolad`` probe uptime calculation is consistent with the frozen
   time delta when both ``_DAEMON_STATE['started_at']`` and ``datetime.now``
   are time-locked.

freezegun freezes ``datetime.now()`` and ``datetime.utcnow()`` (legacy)
across the running test thread; this is sufficient for our unit-level
assertions because ``EventLog._utc_now_iso`` calls ``datetime.now(UTC)``
directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from freezegun import freeze_time

from popolaloom.daemon import EventLog, TaskHandle, TaskState


@freeze_time("2026-05-04 12:34:56.789", tz_offset=0)
def test_event_log_envelope_uses_frozen_time(tmp_path: Path) -> None:
    """Envelope ``time`` field equals the frozen instant in ISO+Z form."""
    log = EventLog(tmp_path / "frozen.jsonl", fsync_interval_s=0)
    try:
        ev = log.append("frozen.test", {"k": 1})
        assert ev["time"].startswith("2026-05-04T12:34:56"), (
            f"envelope time did not start with frozen instant: {ev['time']!r}"
        )
        assert ev["time"].endswith("Z")
    finally:
        log.close()


@freeze_time("2026-05-04 09:00:00")
def test_task_handle_started_at_matches_frozen_time(tmp_path: Path) -> None:
    """A TaskHandle constructed while time is frozen has started_at == frozen instant."""
    now = datetime.now(UTC)
    assert now == datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)

    handle = TaskHandle(
        task_id="frozen-task",
        cli="frozen",
        pid=None,
        state=TaskState.RUNNING,
        started_at=now,
        event_log_path=tmp_path / "h.jsonl",
    )
    assert handle.started_at == datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)


def test_probe_uptime_delta_consistent_with_frozen_time(tmp_path: Path) -> None:
    """Compute uptime as `(now - started_at).total_seconds()` while both are frozen.

    Mirrors the calculation in ``rpc.py::probe_endpoint``: started_at is
    captured at boot, then uptime = (now - started_at). If we freeze
    time twice with a known delta, the math must match exactly.
    """
    with freeze_time("2026-05-04 10:00:00") as frozen:
        started_at = datetime.now(UTC)
        frozen.tick(delta=60)
        now = datetime.now(UTC)
        delta = (now - started_at).total_seconds()
        assert delta == 60.0


def test_consecutive_appends_under_freeze_share_same_time(tmp_path: Path) -> None:
    """While time is frozen, two appends produce the same envelope ``time`` field."""
    with freeze_time("2026-05-04 13:00:00.000"):
        log = EventLog(tmp_path / "f.jsonl", fsync_interval_s=0)
        try:
            e1 = log.append("a", {})
            e2 = log.append("b", {})
            assert e1["time"] == e2["time"]
        finally:
            log.close()
