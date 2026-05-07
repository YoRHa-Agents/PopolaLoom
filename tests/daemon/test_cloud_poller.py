"""Unit tests for :mod:`popolaloom.daemon.cloud_poller`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from pytest_mock import MockerFixture

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudError,
)
from popolaloom.daemon.cloud_poller import CloudPollLoop, run_poll_loop
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState


def _register_handle(
    store: StateStore,
    task_id: str,
    log_path: Path,
) -> None:
    handle = TaskHandle(
        task_id=task_id,
        cli="cursor-cloud",
        pid=None,
        state=TaskState.STARTING,
        started_at=datetime.now(UTC),
        event_log_path=log_path,
        runtime="cloud",
    )
    store.register(handle)


def _event_types_and_data(log: EventLog) -> tuple[list[str], list[dict[str, Any]]]:
    log.fsync()
    entries = log.tail()
    return [e["type"] for e in entries], [e["data"] for e in entries]


@pytest.fixture
def cloud_setup(tmp_path: Path) -> tuple[str, StateStore, EventLog, MagicMock]:
    task_id = "cloud-task-1"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    client = MagicMock()
    return task_id, store, log, client


def test_phase_creating_maps_starting_emits_run_status(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [
        {"status": "CREATING"},
        {"status": "FINISHED"},
    ]
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=2.0,
        max_polls=10,
    )
    loop.run()
    types, _ = _event_types_and_data(log)
    assert types.count("cloud.run_status") >= 1
    assert "task.completed" in types
    h = store.get(task_id)
    assert h is not None
    assert h.state == TaskState.COMPLETED


@pytest.mark.parametrize(
    ("status", "expect_state", "terminal_type", "exit_code", "error_kind"),
    [
        ("FINISHED", TaskState.COMPLETED, "task.completed", 0, None),
        ("ERROR", TaskState.FAILED, "task.failed", 1, "cloud_run_error"),
        ("CANCELLED", TaskState.CANCELED, "task.canceled", -2, None),
        ("EXPIRED", TaskState.FAILED, "task.failed", 1, "cloud_run_expired"),
    ],
)
def test_terminal_status_mapping(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
    status: str,
    expect_state: TaskState,
    terminal_type: str,
    exit_code: int,
    error_kind: str | None,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": status}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    mock_exit = MagicMock()
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=mock_exit,
        max_polls=10,
    )
    loop.run()
    handle = store.get(task_id)
    assert handle is not None
    assert handle.state == expect_state
    types, data_list = _event_types_and_data(log)
    assert terminal_type in types
    terminal_payload = next(d for i, d in enumerate(data_list) if types[i] == terminal_type)
    assert terminal_payload["exit_code"] == exit_code
    if error_kind is not None:
        assert terminal_payload["error_kind"] == error_kind
    mock_exit.assert_called_once_with(task_id, exit_code)


def test_running_phase_updates_state_before_finish(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [
        {"status": "RUNNING"},
        {"status": "FINISHED"},
    ]
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=10,
    ).run()
    assert store.get(task_id) and store.get(task_id).state == TaskState.COMPLETED


def test_unknown_status_warn_treat_as_running(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [
        {"status": "MYSTERY"},
        {"status": "FINISHED"},
    ]
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=10,
    )
    loop.run()
    assert any("unknown cloud run status" in r.message for r in caplog.records)
    assert store.get(task_id) and store.get(task_id).state == TaskState.COMPLETED


def test_non_terminal_repeated_running_single_run_status_until_terminal(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [
        {"status": "RUNNING"},
        {"status": "RUNNING"},
        {"status": "FINISHED"},
    ]
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=10,
    ).run()
    types, data_list = _event_types_and_data(log)
    run_statuses = [d for i, d in enumerate(data_list) if types[i] == "cloud.run_status"]
    assert len([d for d in run_statuses if d["phase"] == "RUNNING"]) == 1
    assert types[-1] == "task.completed"


def test_terminal_finished_calls_on_exit_zero(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    cb = MagicMock()
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=cb,
        max_polls=5,
    ).run()
    cb.assert_called_once_with(task_id, 0)


def test_poll_retryable_backoff_then_success(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    err = CursorCloudError("transient", is_retryable=True)
    client.get_run.side_effect = [err, err, {"status": "FINISHED"}]
    sleep = mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=10,
        retry_max=5,
    ).run()
    assert client.get_run.call_count == 3
    assert sleep.call_count >= 2
    types, _ = _event_types_and_data(log)
    assert "task.completed" in types


def test_poll_exhausts_retryable_emits_failed(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    err = CursorCloudError("give up", is_retryable=True)
    client.get_run.side_effect = err
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    cb = MagicMock()
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=cb,
        max_polls=10,
        retry_max=3,
    ).run()
    assert client.get_run.call_count == 3
    types, data_list = _event_types_and_data(log)
    assert "task.failed" in types
    fail = next(d for i, d in enumerate(data_list) if types[i] == "task.failed")
    assert fail["error_kind"] == "cloud_run_error"
    cb.assert_called_once_with(task_id, 1)


def test_poll_non_retryable_fails_fast(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = CursorCloudError("nope", is_retryable=False)
    sleep = mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=10,
        retry_max=5,
    ).run()
    assert client.get_run.call_count == 1
    sleep.assert_not_called()


def test_max_polls_timeout(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "RUNNING"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    cb = MagicMock()
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=cb,
        interval_s=0.01,
        max_polls=3,
    ).run()
    assert client.get_run.call_count == 3
    types, data_list = _event_types_and_data(log)
    fail = next(d for i, d in enumerate(data_list) if types[i] == "task.failed")
    assert fail["error_kind"] == "cloud_poll_timeout"
    cb.assert_called_once_with(task_id, 1)


def test_terminal_no_extra_polls(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "FINISHED"}
    sleep = mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=99,
    ).run()
    assert client.get_run.call_count == 1
    sleep.assert_not_called()


def test_run_poll_loop_returns_daemon_thread(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    thread = run_poll_loop(
        task_id,
        "bc-a",
        "run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=5,
    )
    assert thread.daemon is True
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_task_failed_includes_runtime_and_terminal_kind(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "ERROR"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=5,
    ).run()
    _, data_list = _event_types_and_data(log)
    failed = next(d for d in data_list if d.get("error_kind") == "cloud_run_error")
    assert failed["runtime"] == "cloud"
    assert failed["terminal_phase"] == "ERROR"


def test_task_canceled_payload(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "CANCELLED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=5,
    ).run()
    types, data_list = _event_types_and_data(log)
    payload = next(d for i, d in enumerate(data_list) if types[i] == "task.canceled")
    assert payload["exit_code"] == -2
    assert payload["terminal_phase"] == "CANCELLED"


def test_cloud_run_status_shape(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [{"status": "CREATING"}, {"status": "FINISHED"}]
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-x",
        run_id="run-x",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=10,
    ).run()
    types, data_list = _event_types_and_data(log)
    statuses = [d for i, d in enumerate(data_list) if types[i] == "cloud.run_status"]
    first = statuses[0]
    assert first["task_id"] == task_id
    assert first["agent_id"] == "bc-x"
    assert first["run_id"] == "run-x"
    assert "phase" in first and "prev_phase" in first and "ts" in first


def test_poll_loop_hits_cursor_api_via_respx(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    task_id = "respx-task"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    with respx.mock(base_url=CURSOR_API_BASE, assert_all_called=False) as router:
        router.get("/v1/agents/bc-rpx/runs/run-rpx").mock(
            return_value=httpx.Response(200, json={"status": "FINISHED"})
        )
        client = CloudCursorClient("k-respx")
        CloudPollLoop(
            task_id=task_id,
            agent_id="bc-rpx",
            run_id="run-rpx",
            client=client,
            state_store=store,
            event_log=log,
            on_exit=None,
            max_polls=5,
        ).run()
    types, _ = _event_types_and_data(log)
    assert "task.completed" in types
