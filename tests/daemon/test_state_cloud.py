"""Tests for v0.8.5 cloud-agent extensions to TaskState FSM + TaskHandle."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from popolaloom.daemon.state import (
    _TERMINAL_STATES,
    StateStore,
    TaskHandle,
    TaskState,
)


def _make_handle(
    task_id: str,
    *,
    state: TaskState = TaskState.PENDING,
    runtime: str = "local",
    cursor_agent_id: str | None = None,
    cursor_run_id: str | None = None,
    cloud_phase: str | None = None,
) -> TaskHandle:
    return TaskHandle(
        task_id=task_id,
        cli="cursor-cloud",
        pid=None,
        state=state,
        started_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        event_log_path=Path(f"/tmp/popola-cloud-tests/{task_id}.jsonl"),
        runtime=runtime,
        cursor_agent_id=cursor_agent_id,
        cursor_run_id=cursor_run_id,
        cloud_phase=cloud_phase,
    )


def test_taskstate_has_queued() -> None:
    assert TaskState.QUEUED is not None
    assert TaskState.QUEUED.value == "queued"


def test_taskstate_has_starting() -> None:
    assert TaskState.STARTING is not None
    assert TaskState.STARTING.value == "starting"


def test_queued_is_not_terminal() -> None:
    h = _make_handle("q1", state=TaskState.QUEUED)
    assert h.is_terminal() is False


def test_starting_is_not_terminal() -> None:
    h = _make_handle("s1", state=TaskState.STARTING)
    assert h.is_terminal() is False


def test_terminal_states_unchanged() -> None:
    assert frozenset(
        {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}
    ) == _TERMINAL_STATES
    assert len(_TERMINAL_STATES) == 3


def test_taskhandle_default_runtime_is_local() -> None:
    h = TaskHandle(
        task_id="local-default",
        cli="cursor",
        pid=123,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=Path("/tmp/x.jsonl"),
    )
    assert h.runtime == "local"


def test_taskhandle_cloud_fields_default_none() -> None:
    h = TaskHandle(
        task_id="cloud-defaults",
        cli="cursor",
        pid=None,
        state=TaskState.PENDING,
        started_at=datetime.now(UTC),
        event_log_path=Path("/tmp/y.jsonl"),
    )
    assert h.cursor_agent_id is None
    assert h.cursor_run_id is None
    assert h.cloud_phase is None


def test_taskhandle_cloud_runtime_with_ids() -> None:
    h = _make_handle(
        "cloud-ids",
        runtime="cloud",
        cursor_agent_id="bc-abc123",
        cursor_run_id="run-xyz",
        cloud_phase="CREATING",
    )
    assert h.runtime == "cloud"
    assert h.cursor_agent_id == "bc-abc123"
    assert h.cursor_run_id == "run-xyz"
    assert h.cloud_phase == "CREATING"


def test_statestore_update_runtime() -> None:
    store = StateStore()
    store.register(_make_handle("rt1"))
    updated = store.update("rt1", runtime="cloud")
    assert updated.runtime == "cloud"


def test_statestore_update_cloud_ids() -> None:
    store = StateStore()
    store.register(_make_handle("id1", runtime="cloud"))
    u = store.update(
        "id1",
        cursor_agent_id="bc-agent",
        cursor_run_id="run-r1",
    )
    assert u.cursor_agent_id == "bc-agent"
    assert u.cursor_run_id == "run-r1"


def test_statestore_update_cloud_phase_progression() -> None:
    store = StateStore()
    store.register(_make_handle("ph1", runtime="cloud"))
    assert store.update("ph1", cloud_phase="CREATING").cloud_phase == "CREATING"
    assert store.update("ph1", cloud_phase="RUNNING").cloud_phase == "RUNNING"
    assert store.update("ph1", cloud_phase="FINISHED").cloud_phase == "FINISHED"


def test_statestore_cloud_handles_filters() -> None:
    store = StateStore()
    store.register(_make_handle("loc-only", runtime="local"))
    store.register(_make_handle("cld-only", runtime="cloud"))
    clouds = store.cloud_handles()
    assert len(clouds) == 1
    assert clouds[0].task_id == "cld-only"


def test_statestore_cloud_handles_empty_when_only_local() -> None:
    store = StateStore()
    store.register(_make_handle("l1", runtime="local"))
    assert store.cloud_handles() == []


def test_state_transition_pending_to_queued_to_starting_to_running() -> None:
    store = StateStore()
    store.register(_make_handle("seq1", runtime="cloud"))
    store.update("seq1", state=TaskState.QUEUED)
    store.update("seq1", state=TaskState.STARTING)
    final = store.update("seq1", state=TaskState.RUNNING)
    assert final.state == TaskState.RUNNING
    assert final.is_terminal() is False


def test_state_transition_to_completed_clears_no_phase() -> None:
    store = StateStore()
    store.register(_make_handle("term-ph", runtime="cloud", cloud_phase="FINISHED"))
    done = store.update("term-ph", state=TaskState.COMPLETED, exit_code=0)
    assert done.cloud_phase == "FINISHED"
    assert done.is_terminal() is True

