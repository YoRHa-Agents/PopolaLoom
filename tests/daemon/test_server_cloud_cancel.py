"""Cloud-runtime cancel path on :class:`popolad.Popolad` (v0.8.5 Stage 2 T2.B).

v1.5.1 O.G3.3 (race-window contract): the cancel path now waits up to
``cloud_cancel_grace_s`` seconds for the supervisor to populate the
cloud runtime/agent_id pair before deciding how to terminate a
``cursor-``-prefixed task. The four trailing tests in this module
exercise that contract:

- ``test_cancel_cloud_task_waits_for_runtime_flip`` — supervisor flips
  ``runtime`` and ``cursor_agent_id`` mid-wait; cancel proceeds.
- ``test_cancel_cloud_task_waits_for_agent_id_population`` — supervisor
  only needs to populate ``cursor_agent_id`` mid-wait.
- ``test_cancel_cloud_task_grace_window_timeout_emits_error_event`` —
  no flip ever arrives; cancel emits ``task.failed`` + raises.
- ``test_cancel_local_task_does_not_enter_grace_window`` — backward-compat:
  non-``cursor-`` task_ids skip the wait loop entirely.
- ``test_cancel_cloud_task_grace_window_zero_emits_event_immediately`` —
  backward-compat ratchet: cursor-prefixed task with
  ``cloud_cancel_grace_s=0.0`` and unhydrated cloud handle fails fast
  (< 0.5s) with the structured ``cloud_cancel_race_window_exceeded``
  event, mirroring v1.5.0's loud-immediate-fail semantics under the
  v1.5.1 error-kind name.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from popolaloom.adapters.cursor_cloud import (
    CursorCloudConflictError,
    CursorCloudError,
)
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskHandle, TaskState


def _register_cloud_handle(
    popolad: Popolad,
    tmp_path: Path,
    *,
    task_id: str = "cloud-cancel-1",
    state: TaskState = TaskState.RUNNING,
    cursor_agent_id: str | None = "bc-agent-1",
    cursor_run_id: str | None = "run-1",
    runtime: str = "cloud",
) -> str:
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{task_id}.jsonl"
    handle = TaskHandle(
        task_id=task_id,
        cli="cursor-cloud",
        pid=None,
        state=state,
        started_at=datetime.now(UTC),
        event_log_path=path,
        runtime=runtime,
        cursor_agent_id=cursor_agent_id,
        cursor_run_id=cursor_run_id,
        cloud_phase="RUNNING" if runtime == "cloud" else None,
    )
    popolad.state_store.register(handle)
    log = EventLog(path, source=f"popola/{task_id}", fsync_interval_s=0)
    popolad._event_logs[task_id] = log  # reuse explicit log so tail_events works
    return task_id


def test_cancel_cloud_task_calls_cursor_api(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    popolad.cancel_task(tid)
    mock_client.cancel_run.assert_called_once_with("bc-agent-1", "run-1")


def test_cancel_cloud_task_does_not_call_os_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []

    def _kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr("os.kill", _kill)
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    popolad.cancel_task(tid)
    assert calls == []


def test_cancel_local_task_still_uses_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []

    def _kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr("os.kill", _kill)

    popolad = Popolad(events_dir=tmp_path / "events")
    tid = "local-sig"
    path = tmp_path / "events" / f"{tid}.jsonl"
    log = EventLog(path, source=f"popola/{tid}", fsync_interval_s=0)
    popolad._event_logs[tid] = log
    h = TaskHandle(
        task_id=tid,
        cli="cursor",
        pid=4242,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=path,
        runtime="local",
    )
    popolad.state_store.register(h)

    popolad.cancel_task(tid, sigterm_grace_s=0.0)
    assert calls, "expected os.kill for local cancel"
    assert any(sig == 15 for _, sig in calls)  # SIGTERM


def test_cancel_cloud_task_marks_state_canceled(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    popolad.cancel_task(tid)
    assert popolad.state_store.get(tid) is not None
    assert popolad.state_store.get(tid).state == TaskState.CANCELED


def test_cancel_cloud_task_emits_task_canceled_event(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    popolad.cancel_task(tid)
    events = popolad.tail_events(tid)
    last = next(e for e in reversed(events) if e["type"] == "task.canceled")
    assert last["data"]["runtime"] == "cloud"
    assert last["data"]["agent_id"] == "bc-agent-1"
    assert last["data"]["run_id"] == "run-1"


def test_cancel_cloud_task_handles_409_busy(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.side_effect = CursorCloudConflictError(
        "agent busy", status_code=409, is_retryable=False
    )
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    popolad.cancel_task(tid)
    assert popolad.state_store.get(tid).state == TaskState.CANCELED
    events = popolad.tail_events(tid)
    canc = next(e for e in reversed(events) if e["type"] == "task.canceled")
    assert canc["data"].get("cancel_kind") == "best_effort_after_409"


def test_cancel_cloud_task_handles_4xx_other(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.side_effect = CursorCloudError(
        "not found", status_code=404, is_retryable=False
    )
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    with pytest.raises(CursorCloudError):
        popolad.cancel_task(tid)
    assert popolad.state_store.get(tid).state == TaskState.RUNNING
    events = popolad.tail_events(tid)
    failed = next(e for e in reversed(events) if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_cancel_failed"


def test_cancel_cloud_task_handles_network_error(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.side_effect = CursorCloudError(
        "cursor-cloud request failed: boom",
        status_code=None,
        is_retryable=True,
    )
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    with pytest.raises(CursorCloudError):
        popolad.cancel_task(tid)
    assert popolad.state_store.get(tid).state == TaskState.RUNNING
    events = popolad.tail_events(tid)
    failed = next(e for e in reversed(events) if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_cancel_network_error"
    assert failed["data"]["error"]["is_retryable"] is True


def test_cancel_already_terminal_task_no_op(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad, tmp_path, task_id="cloud-term", state=TaskState.COMPLETED
    )
    with pytest.raises(RuntimeError, match="terminal"):
        popolad.cancel_task(tid)
    mock_client.cancel_run.assert_not_called()


def test_cancel_cloud_task_without_agent_ids_fails_explicitly(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad,
        tmp_path,
        task_id="no-ids",
        cursor_agent_id=None,
        cursor_run_id="run-only",
    )
    with pytest.raises(RuntimeError, match="cloud cancel failed"):
        popolad.cancel_task(tid)
    mock_client.cancel_run.assert_not_called()
    events = popolad.tail_events(tid)
    failed = next(e for e in reversed(events) if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_cancel_no_handle"


def test_cancel_401_emits_cloud_cancel_failed(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.side_effect = CursorCloudError(
        "unauthorized", status_code=401, is_retryable=False
    )
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(popolad, tmp_path)
    with pytest.raises(CursorCloudError):
        popolad.cancel_task(tid)
    events = popolad.tail_events(tid)
    failed = next(e for e in reversed(events) if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_cancel_failed"


def test_cancel_cloud_task_waits_for_runtime_flip(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad,
        tmp_path,
        task_id="cursor-race-1",
        runtime="local",
        cursor_agent_id=None,
        cursor_run_id=None,
    )

    def _hydrate() -> None:
        time.sleep(0.15)
        popolad.state_store.update(
            tid,
            runtime="cloud",
            cursor_agent_id="bc-late-1",
            cursor_run_id="run-late-1",
        )

    t = threading.Thread(target=_hydrate, daemon=True)
    t.start()
    try:
        popolad.cancel_task(tid, cloud_cancel_grace_s=2.0)
    finally:
        t.join(timeout=3.0)

    assert not t.is_alive()
    mock_client.cancel_run.assert_called_once_with("bc-late-1", "run-late-1")


def test_cancel_cloud_task_waits_for_agent_id_population(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad,
        tmp_path,
        task_id="cursor-race-2",
        runtime="cloud",
        cursor_agent_id=None,
        cursor_run_id=None,
    )

    def _hydrate() -> None:
        time.sleep(0.15)
        popolad.state_store.update(
            tid,
            cursor_agent_id="bc-late-2",
            cursor_run_id="run-late-2",
        )

    t = threading.Thread(target=_hydrate, daemon=True)
    t.start()
    try:
        popolad.cancel_task(tid, cloud_cancel_grace_s=2.0)
    finally:
        t.join(timeout=3.0)

    assert not t.is_alive()
    mock_client.cancel_run.assert_called_once_with("bc-late-2", "run-late-2")


def test_cancel_cloud_task_grace_window_timeout_emits_error_event(
    tmp_path: Path,
) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad,
        tmp_path,
        task_id="cursor-stuck-1",
        runtime="local",
        cursor_agent_id=None,
        cursor_run_id=None,
    )

    with pytest.raises(RuntimeError, match=r"grace window"):
        popolad.cancel_task(tid, cloud_cancel_grace_s=0.2)

    events = popolad.tail_events(tid)
    failed = next(e for e in reversed(events) if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_cancel_race_window_exceeded"
    assert failed["data"]["cloud_cancel_grace_s"] == 0.2
    assert failed["data"]["runtime"] == "local"
    assert failed["data"]["cursor_agent_id"] is None
    assert failed["data"]["cursor_run_id"] is None
    mock_client.cancel_run.assert_not_called()


def test_cancel_local_task_does_not_enter_grace_window(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad,
        tmp_path,
        task_id="local-no-wait",
        runtime="local",
        cursor_agent_id=None,
        cursor_run_id=None,
    )

    start = time.monotonic()
    with pytest.raises(
        RuntimeError,
        match=r"has no pid yet|race window between dispatch and spawn",
    ):
        popolad.cancel_task(tid, cloud_cancel_grace_s=10.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"expected legacy LOCAL guard to fire immediately for non-cursor "
        f"task_id, but cancel_task took {elapsed:.3f}s (the grace window "
        f"loop should be skipped when task_id does not start with 'cursor-')"
    )
    mock_client.cancel_run.assert_not_called()


def test_cancel_cloud_task_grace_window_zero_emits_event_immediately(
    tmp_path: Path,
) -> None:
    """v1.5.1 backward-compat ratchet — closes the gap between the public
    plan §S.1.b (``test_cancel_cloud_task_grace_window_zero_preserves_old_behavior``)
    and the four shipped tests.

    Asserts that for a ``cursor-``-prefixed task with an unhydrated cloud
    handle (``runtime != "cloud"`` AND ``cursor_agent_id is None``),
    passing ``cloud_cancel_grace_s=0.0`` (the v1.5.0-equivalent opt-out)
    causes the new cancel path to fail loudly + immediately rather than
    blocking on a positive-duration grace window.

    Note on the error-string change vs v1.5.0: pre-v1.5.1 this exact
    code path raised the LOCAL ``"has no pid yet (race window between
    dispatch and spawn)"`` ``RuntimeError``. v1.5.1 raises the new
    ``cloud_cancel_race_window_exceeded`` variant (same loud-fail tier,
    structured ``task.failed`` event with explicit ``error_kind``).
    Both are loud failures with no silent fallback; the message string
    change is documented under CHANGELOG ``[1.5.1] Changed``.
    """
    mock_client = MagicMock(spec=["cancel_run"])
    mock_client.cancel_run.return_value = {}
    popolad = Popolad(events_dir=tmp_path / "events", cloud_client=mock_client)
    tid = _register_cloud_handle(
        popolad,
        tmp_path,
        task_id="cursor-zero-1",
        runtime="local",
        cursor_agent_id=None,
        cursor_run_id=None,
    )

    start = time.monotonic()
    with pytest.raises(RuntimeError, match=r"grace window"):
        popolad.cancel_task(tid, cloud_cancel_grace_s=0.0)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, (
        f"expected cloud_cancel_grace_s=0.0 to fail immediately for a "
        f"cursor-prefixed unhydrated handle, but cancel_task took "
        f"{elapsed:.3f}s (the deadline check should fire on the first "
        f"loop iteration when grace=0)"
    )

    events = popolad.tail_events(tid)
    failed = next(e for e in reversed(events) if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_cancel_race_window_exceeded"
    assert failed["data"]["cloud_cancel_grace_s"] == 0.0
    assert failed["data"]["runtime"] == "local"
    assert failed["data"]["cursor_agent_id"] is None
    assert failed["data"]["cursor_run_id"] is None
    mock_client.cancel_run.assert_not_called()
