"""v0.4.1 Stage L1.A — supervisor wait-thread terminal-event tests.

Per the v0.4.1 task spec L1.D #2 (~ 3 cases): cover the new three-way
terminal event emission of the supervisor wait-thread:

(a) clean exit_code=0 → ``task.completed`` (no state_store CANCELED).
(b) SIGTERM-cancel via :class:`StateStore` flag → ``task.canceled``
    with ``data.sigkill_escalated == False``.
(c) SIGKILL-escalation via flag → ``task.canceled`` with
    ``data.sigkill_escalated == True``.

These tests drive the :class:`Supervisor` directly with an injected
:class:`StateStore` (no full Popolad pipeline) so the contract under
test is exactly the wait-thread emit decision — not the cancel_task
side-effects (those are covered by Tier 2 dispatch-chain integration
tests).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.supervisor import Supervisor


def _wait_for_event_type(
    event_log: EventLog,
    event_type: str,
    timeout_s: float = 4.0,
) -> dict[str, Any] | None:
    """Poll ``event_log.tail()`` until an envelope of ``event_type`` appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for ev in event_log.tail():
            if ev["type"] == event_type:
                return ev
        time.sleep(0.05)
    return None


def _register_running_handle(
    state_store: StateStore,
    *,
    task_id: str,
    event_log_path: Path,
) -> TaskHandle:
    handle = TaskHandle(
        task_id=task_id,
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=event_log_path,
    )
    state_store.register(handle)
    return handle


# ── case (a): clean exit 0 → task.completed (no CANCELED state) ─────────


def test_clean_exit_zero_emits_task_completed_when_state_running(
    tmp_path: Path,
) -> None:
    """exit_code=0 + state==RUNNING → ``task.completed`` (v0.4.0 fallback path)."""
    log = EventLog(tmp_path / "ok.jsonl", fsync_interval_s=0)
    state_store = StateStore()
    _register_running_handle(
        state_store, task_id="ok-zero", event_log_path=tmp_path / "ok.jsonl"
    )

    sup = Supervisor(state_store=state_store)
    sup.spawn(
        task_id="ok-zero",
        cmd=[sys.executable, "-c", "print('done'); import sys; sys.exit(0)"],
        cwd=None,
        env=None,
        event_log=log,
        on_exit=None,
    )
    assert sup.join("ok-zero", timeout=4.0)

    completed = _wait_for_event_type(log, "task.completed")
    assert completed is not None, (
        f"expected task.completed envelope, got events: {[e['type'] for e in log.tail()]}"
    )
    assert completed["data"]["exit_code"] == 0
    assert completed["data"]["task_id"] == "ok-zero"

    types = {e["type"] for e in log.tail()}
    assert "task.canceled" not in types
    assert "task.failed" not in types
    log.close()


# ── case (b): SIGTERM cancel → task.canceled, sigkill_escalated=False ──


def test_sigterm_cancel_emits_task_canceled_no_escalation(tmp_path: Path) -> None:
    """state==CANCELED + sigkill flag False + SIGTERM exit → ``task.canceled``."""
    log = EventLog(tmp_path / "cancel.jsonl", fsync_interval_s=0)
    state_store = StateStore()
    _register_running_handle(
        state_store,
        task_id="cancel-soft",
        event_log_path=tmp_path / "cancel.jsonl",
    )

    sup = Supervisor(state_store=state_store)
    pid = sup.spawn(
        task_id="cancel-soft",
        cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=None,
        env=None,
        event_log=log,
        on_exit=None,
    )
    time.sleep(0.15)

    state_store.update(
        "cancel-soft",
        state=TaskState.CANCELED,
        cancel_escalated_to_sigkill=False,
    )
    os.kill(pid, signal.SIGTERM)

    assert sup.join("cancel-soft", timeout=4.0)
    canceled = _wait_for_event_type(log, "task.canceled")
    assert canceled is not None, (
        f"expected task.canceled envelope, got: {[e['type'] for e in log.tail()]}"
    )
    assert canceled["data"]["task_id"] == "cancel-soft"
    assert canceled["data"]["sigkill_escalated"] is False
    assert canceled["data"]["exit_code"] == -signal.SIGTERM
    assert canceled["data"]["pid"] == pid

    types = {e["type"] for e in log.tail()}
    assert "task.failed" not in types
    log.close()


# ── case (c): SIGKILL escalation → task.canceled, sigkill_escalated=True ─


def test_sigkill_cancel_emits_task_canceled_with_escalation(tmp_path: Path) -> None:
    """state==CANCELED + sigkill flag True + SIGKILL exit → escalation card data."""
    log = EventLog(tmp_path / "kill.jsonl", fsync_interval_s=0)
    state_store = StateStore()
    _register_running_handle(
        state_store,
        task_id="cancel-hard",
        event_log_path=tmp_path / "kill.jsonl",
    )

    sup = Supervisor(state_store=state_store)
    pid = sup.spawn(
        task_id="cancel-hard",
        cmd=[
            sys.executable,
            "-c",
            (
                "import signal as s, time;"
                "s.signal(s.SIGTERM, s.SIG_IGN);"
                "time.sleep(60)"
            ),
        ],
        cwd=None,
        env=None,
        event_log=log,
        on_exit=None,
    )
    time.sleep(0.15)

    state_store.update(
        "cancel-hard",
        state=TaskState.CANCELED,
        cancel_escalated_to_sigkill=True,
    )
    os.kill(pid, signal.SIGKILL)

    assert sup.join("cancel-hard", timeout=4.0)
    canceled = _wait_for_event_type(log, "task.canceled")
    assert canceled is not None, (
        f"expected task.canceled envelope, got: {[e['type'] for e in log.tail()]}"
    )
    assert canceled["data"]["task_id"] == "cancel-hard"
    assert canceled["data"]["sigkill_escalated"] is True
    assert canceled["data"]["exit_code"] == -signal.SIGKILL
    assert canceled["data"]["pid"] == pid

    types = {e["type"] for e in log.tail()}
    assert "task.failed" not in types
    assert "task.completed" not in types
    log.close()
