"""C10 — Bridge handler raises → other subscribers unaffected, error logged.

Per testing-matrix.md §10 #10.  When the popola↔ArkTower bridge
handler raises (e.g. NDJSON write fails), the exception MUST be
caught + logged in the bridge so other subscribers on the same topic
still receive the event.  This is the workspace "No Silent Failures"
rule applied to fan-out: log the exception so operators see it, but
don't propagate (which would break unrelated handlers).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

from popolaloom.daemon.event_bus import PopolaEventBusBridge


class _StubEventBus:
    """Minimal EventBus stub used to exercise subscribe / unsubscribe."""

    def __init__(self) -> None:
        self.subscribers = []

    def subscribe(self, topic, handler):
        self.subscribers.append((topic, handler))

    def unsubscribe(self, topic, handler):
        self.subscribers = [(t, h) for (t, h) in self.subscribers if h is not handler]


def _make_event(task_id: str = "ark-1") -> MagicMock:
    """Construct a fake TaskEvent with all the fields the bridge reads."""
    e = MagicMock()
    e.task_id = task_id
    e.event_id = "evt-fake-1"
    e.trigger = MagicMock(value="auto")
    e.from_status = MagicMock(value="pending")
    e.to_status = MagicMock(value="in_progress")
    e.actor = "popolad"
    e.notes = "test"
    e.timestamp = datetime.now(UTC)
    return e


def test_chaos_bridge_handler_raise_logged_and_swallowed(
    caplog,
    mocker,
) -> None:
    """``log.append`` raising → bridge logs, does NOT re-raise.

    Other subscribers on the same topic must still execute, so the
    bridge MUST swallow the exception (it's a fan-out boundary).
    """
    failing_log = MagicMock()
    failing_log.append.side_effect = RuntimeError(
        "simulated NDJSON write failure"
    )

    def _get_log(_arktower_task_id: str):
        return failing_log

    bridge = PopolaEventBusBridge(_StubEventBus(), _get_log)

    event = _make_event("ark-c10")
    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.event_bus"):
        bridge._on_transition(event)

    assert any(
        "handler failed" in record.message for record in caplog.records
    ), (
        "bridge must log the exception when its handler fails (No Silent Failures); "
        f"records: {[r.message for r in caplog.records]}"
    )


def test_chaos_unknown_arktower_task_id_drops_silently_with_debug_log(
    caplog,
) -> None:
    """``get_event_log`` returns None → bridge drops the event, debug-log only."""
    bridge = PopolaEventBusBridge(_StubEventBus(), lambda _: None)
    event = _make_event("ark-unknown")

    with caplog.at_level(logging.DEBUG, logger="popolaloom.daemon.event_bus"):
        bridge._on_transition(event)

    assert any(
        "unknown ArkTower task_id" in record.message
        for record in caplog.records
    ), "missing debug log for unknown task_id drop"


def test_chaos_subscribe_then_unsubscribe_idempotent() -> None:
    """Bridge subscribe/unsubscribe sets ``subscribed`` flag idempotently.

    We test the bridge's *own* state machine (the ``_subscribed`` bool),
    not the underlying EventBus's subscriber list, because the latter
    depends on bound-method identity which is implementation-detail of
    the ArkTower bus.  No Silent Failures: each subscribe/unsubscribe
    call MUST leave the flag in a consistent state.
    """
    bus = _StubEventBus()
    bridge = PopolaEventBusBridge(bus, lambda _: None)
    assert not bridge.subscribed

    bridge.subscribe()
    assert bridge.subscribed
    assert len(bus.subscribers) == 1

    bridge.subscribe()
    assert bridge.subscribed, "second subscribe must keep flag true"
    assert len(bus.subscribers) == 1, "second subscribe must not re-register"

    bridge.unsubscribe()
    assert not bridge.subscribed

    bridge.unsubscribe()
    assert not bridge.subscribed, "second unsubscribe must keep flag false"
