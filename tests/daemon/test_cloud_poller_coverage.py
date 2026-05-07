"""Gap-filler coverage for :mod:`popolaloom.daemon.cloud_poller` edge branches."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from popolaloom.daemon.cloud_poller import CloudPollLoop, _safe_on_exit
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


@pytest.fixture
def cloud_setup(tmp_path: Path) -> tuple[str, StateStore, EventLog, MagicMock]:
    task_id = "cloud-cov-1"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    client = MagicMock()
    return task_id, store, log, client


def test_safe_on_exit_logs_when_callback_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR")

    def bad_cb(_tid: str, _code: int) -> None:
        raise RuntimeError("no exit for you")

    _safe_on_exit(bad_cb, "t-bad", 0)
    assert any("on_exit callback failed" in r.message for r in caplog.records)


def test_null_status_normalizes_to_unknown_then_finishes(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    caplog.set_level("WARNING")
    client.get_run.side_effect = [
        {"status": None},
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
    assert any("unknown cloud run status" in r.message for r in caplog.records)
    assert store.get(task_id) and store.get(task_id).state == TaskState.COMPLETED


def test_client_close_failure_in_finally_emits_debug(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task_id, store, log, client = cloud_setup
    caplog.set_level("DEBUG")
    client.get_run.return_value = {"status": "FINISHED"}
    client.close.side_effect = OSError("close busted")
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
    assert any("cloud client close failed" in r.message for r in caplog.records)


def test_terminal_on_exit_raises_is_swallowed_via_safe_on_exit(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``run`` invokes :func:`_safe_on_exit`; a throwing callback logs and does not crash."""

    task_id, store, log, client = cloud_setup
    caplog.set_level("ERROR")
    client.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)

    def boom(_t: str, _c: int) -> None:
        raise RuntimeError("on_exit boom")

    CloudPollLoop(
        task_id=task_id,
        agent_id="bc-a",
        run_id="run-a",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=boom,
        max_polls=5,
    ).run()

    assert any("on_exit callback failed" in r.message for r in caplog.records)
    handle = store.get(task_id)
    assert handle is not None
    assert handle.state == TaskState.COMPLETED
