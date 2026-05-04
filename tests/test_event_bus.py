"""Stage C C2 tests — :mod:`popolaloom.daemon.event_bus` PopolaEventBusBridge.

Three required cases (per v0.2.0-plan §4 Stage C C2 + L3 spec):

1. ``test_event_bus_bridge_subscribes_and_translates`` — a fake
   ArkTower-style :class:`EventBus.publish` triggers the bridge's handler
   which writes a ``task.transition`` envelope to the resolved
   :class:`EventLog`.
2. ``test_event_bus_bridge_unsubscribe`` — after :meth:`unsubscribe`,
   subsequent publishes do **not** reach the handler / event log.
3. ``test_event_bus_bridge_no_event_log_silent`` — when
   ``get_event_log`` returns ``None`` the bridge is a no-op (no crash,
   no event written).

Plus bonus: ``test_event_bus_bridge_handler_exception_does_not_crash_publish``
documenting the No Silent Failures behaviour (handler errors are logged,
not swallowed without trace).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from arktower.core.event_bus import EventBus
from arktower.core.models import TaskEvent, TaskStatus, Trigger
from arktower.core.task_service import TASK_TRANSITION_EVENT

from popolaloom.daemon import EventLog, PopolaEventBusBridge


def _make_event(
    *,
    task_id: str = "ark-task-001",
    trigger: Trigger = Trigger.CLAIM,
    from_status: TaskStatus = TaskStatus.QUEUED,
    to_status: TaskStatus = TaskStatus.IN_PROGRESS,
    actor: str = "tester",
    notes: str | None = "unit-test transition",
    timestamp: datetime | None = None,
) -> TaskEvent:
    """Build a TaskEvent matching ArkTower's signature exactly."""
    return TaskEvent(
        task_id=task_id,
        trigger=trigger,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        notes=notes,
        timestamp=timestamp or datetime.now(UTC),
    )


# ── 1. Subscribe + translate ────────────────────────────────────────────


def test_event_bus_bridge_subscribes_and_translates(tmp_path: Path) -> None:
    """Publish on TASK_TRANSITION_EVENT → bridge writes one ``task.transition`` line.

    Verifies the full flow: ``subscribe()`` registers the handler;
    ``EventBus.publish`` (async) dispatches; the handler resolves the
    target :class:`EventLog` via the ``get_event_log`` callback; the
    NDJSON envelope contains every field the L3 spec requires.
    """
    log_path = tmp_path / "events" / "popola-task-001.jsonl"
    log = EventLog(log_path)

    bus = EventBus()
    bridge = PopolaEventBusBridge(bus, lambda _ark_id: log)
    bridge.subscribe()
    assert bridge.subscribed is True

    event = _make_event(
        task_id="ark-task-001",
        trigger=Trigger.CLAIM,
        from_status=TaskStatus.QUEUED,
        to_status=TaskStatus.IN_PROGRESS,
        actor="popola-test",
        notes="transition under test",
    )
    asyncio.run(bus.publish(TASK_TRANSITION_EVENT, event))

    log.fsync()
    events = log.tail()
    assert len(events) == 1, f"Expected exactly 1 envelope; got {events}"
    env = events[0]

    assert env["specversion"] == "1.0"
    assert env["type"] == "task.transition"
    assert env["source"] == "popola/popola-task-001"
    data = env["data"]
    assert data["task_id"] == "ark-task-001"
    assert data["event_id"] == event.event_id
    assert data["trigger"] == "claim"
    assert data["from_status"] == "queued"
    assert data["to_status"] == "in_progress"
    assert data["actor"] == "popola-test"
    assert data["notes"] == "transition under test"
    assert data["timestamp"].endswith("Z"), (
        f"timestamp should be ISO-Z; got {data['timestamp']!r}"
    )

    bridge.unsubscribe()
    log.close()


# ── 2. Unsubscribe stops further forwarding ─────────────────────────────


def test_event_bus_bridge_unsubscribe(tmp_path: Path) -> None:
    """After :meth:`unsubscribe`, publishes are NOT translated to NDJSON."""
    log_path = tmp_path / "events" / "popola-task-002.jsonl"
    log = EventLog(log_path)

    bus = EventBus()
    bridge = PopolaEventBusBridge(bus, lambda _ark_id: log)
    bridge.subscribe()

    asyncio.run(
        bus.publish(
            TASK_TRANSITION_EVENT,
            _make_event(task_id="ark-002", trigger=Trigger.SUBMIT),
        )
    )
    log.fsync()
    assert len(log.tail()) == 1, "first publish should write 1 envelope"

    bridge.unsubscribe()
    assert bridge.subscribed is False

    asyncio.run(
        bus.publish(
            TASK_TRANSITION_EVENT,
            _make_event(task_id="ark-002", trigger=Trigger.COMPLETE),
        )
    )
    log.fsync()
    assert len(log.tail()) == 1, (
        "after unsubscribe, second publish must NOT add an envelope"
    )

    bridge.unsubscribe()
    log.close()


# ── 3. Missing event log → silent no-op ─────────────────────────────────


def test_event_bus_bridge_no_event_log_silent(tmp_path: Path) -> None:
    """When ``get_event_log`` returns ``None``, the bridge drops without crash.

    Use case: a TaskService transition fires for an ArkTower task that
    was created outside popolad (e.g. directly via ``arktower task add``)
    — popolad has no associated event log, so the bridge logs at debug
    level and returns silently.  No exception, no NDJSON file created.
    """
    log_dir = tmp_path / "events"
    log_dir.mkdir()

    bus = EventBus()
    bridge = PopolaEventBusBridge(bus, lambda _ark_id: None)
    bridge.subscribe()

    asyncio.run(
        bus.publish(
            TASK_TRANSITION_EVENT,
            _make_event(task_id="orphan-ark-id", trigger=Trigger.CLAIM),
        )
    )

    assert list(log_dir.iterdir()) == [], (
        f"No event log should have been created; got {list(log_dir.iterdir())}"
    )

    bridge.unsubscribe()


# ── 4. Bonus: handler exception is logged, not silently swallowed ──────


def test_event_bus_bridge_handler_exception_does_not_crash_publish(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Handler crash inside the bridge is logged + does not propagate to publish().

    Documents No Silent Failures: when the resolver callback raises,
    ``_on_transition`` catches + logs via ``logger.exception`` so the
    failure leaves an audit trail but other subscribers still run.
    """

    def _bad_resolver(_ark_id: str) -> EventLog | None:
        raise RuntimeError("simulated lookup failure")

    bus = EventBus()
    bridge = PopolaEventBusBridge(bus, _bad_resolver)
    bridge.subscribe()

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.event_bus"):
        asyncio.run(
            bus.publish(
                TASK_TRANSITION_EVENT,
                _make_event(task_id="will-fail", trigger=Trigger.CLAIM),
            )
        )

    bad_records = [
        r for r in caplog.records if "PopolaEventBusBridge handler failed" in r.message
    ]
    assert bad_records, (
        f"handler failure should be logged via logger.exception; got {caplog.records}"
    )

    bridge.unsubscribe()
