"""Tests for Supervisor cloud-marker spawn path (v0.8.5 Stage 2 + v0.10.0 Wave A1.5).

v0.10.0 (DECISIONS Q-2 / Q-11 / Wave A1.5): the supervisor's marker
translator no longer forwards the legacy ``use_private_worker`` /
``labels`` kwargs to ``CloudCursorClient.create_agent``. Instead, the
supervisor:

1. Reads ``extra["env"]`` (the v0.10.0 marker payload shape from
   ``_normalize_cloud_extra``) and forwards it as ``env=AgentEnv``.
2. As a defensive deprecation translator, when ``env`` is absent AND
   legacy ``use_private_worker=True + labels.worker=X`` are both
   present, translates the pair into ``env={type:"machine", name:X}``
   and emits a single ``WARN`` log (so a v0.9.x CLI pinned by an
   operator can still dispatch to a v0.10.0 daemon).

Tests in this file pin both the new pass-through behaviour and the
defensive translator (with ``caplog`` capturing the WARN line).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from popolaloom.adapters.cursor_cloud import (
    CLOUD_BUILD_COMMAND_MARKER,
    CursorCloudError,
)
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.supervisor import Supervisor


def _marker_cmd(prompt: str, extra: dict[str, Any]) -> list[str]:
    payload = {"cwd": None, "extra": extra, "prompt": prompt}
    return [
        *CLOUD_BUILD_COMMAND_MARKER,
        json.dumps(payload, sort_keys=True),
    ]


@pytest.fixture
def cloud_task_env(
    tmp_path: Path,
) -> tuple[Supervisor, StateStore, EventLog, str]:
    task_id = "sup-cloud-1"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    store.register(
        TaskHandle(
            task_id=task_id,
            cli="cursor-cloud",
            pid=None,
            state=TaskState.PENDING,
            started_at=datetime.now(UTC),
            event_log_path=log_path,
        )
    )
    sup = Supervisor(state_store=store)
    return sup, store, log, task_id


def test_cloud_marker_triggers_create_agent_path(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.10.0 (AC6 a): supervisor forwards ``env`` (NOT ``use_private_worker`` / ``labels``).

    The marker payload now carries ``extra["env"]`` (an :class:`AgentEnv`
    discriminated union) instead of the legacy ``use_private_worker`` +
    ``labels`` pair. The supervisor passes ``env`` through to
    ``CloudCursorClient.create_agent`` via the typed ``env=`` kwarg and
    NEVER reconstructs the v0.9.x kwargs.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, store, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-test"},
        "run": {"id": "run-test"},
    }
    instance.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    cmd = _marker_cmd(
        "do work",
        {
            "repo_url": "https://github.com/o/r",
            "env": {"type": "machine", "name": "ci-1"},
        },
    )
    pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    assert pid == 0
    assert sup.join(task_id, timeout=5.0)
    mock_cls.assert_called_once()
    instance.create_agent.assert_called_once()
    kwargs = instance.create_agent.call_args.kwargs
    # v0.10.0 (AC6 a): the new ``env=AgentEnv`` kwarg replaces the v0.9.x
    # ``use_private_worker`` + ``labels`` pair.
    assert kwargs["env"] == {"type": "machine", "name": "ci-1"}
    # Legacy v0.9.x kwargs are NEVER forwarded.
    assert "use_private_worker" not in kwargs
    assert "labels" not in kwargs
    handle = store.get(task_id)
    assert handle is not None
    assert handle.runtime == "cloud"
    assert handle.cursor_agent_id == "bc-test"
    assert handle.cursor_run_id == "run-test"
    assert handle.state == TaskState.COMPLETED
    mock_cls.return_value.close.assert_called()


def test_legacy_v0_9_x_extras_translated_to_env_with_warn_log(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """v0.10.0 (AC6 b): legacy ``use_private_worker=True + labels.worker=X`` translates.

    Per Wave A1.5 the supervisor's defensive deprecation translator
    accepts a v0.9.x marker payload and translates it to
    ``env={type:"machine", name:X}`` while emitting a single WARN log.
    This keeps a pinned v0.9.x CLI working against a v0.10.0 daemon for
    one minor release (the one-release deprecation window per Q-11).
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, store, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-test"},
        "run": {"id": "run-test"},
    }
    instance.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    cmd = _marker_cmd(
        "do work",
        {
            "repo_url": "https://github.com/o/r",
            "use_private_worker": True,
            "labels": {"pool": "popolaloom", "worker": "ci-1"},
        },
    )

    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon.supervisor"):
        pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
        assert pid == 0
        assert sup.join(task_id, timeout=5.0)

    instance.create_agent.assert_called_once()
    kwargs = instance.create_agent.call_args.kwargs
    # AC6 (b): legacy pair translated to env={type:"machine", name:X}.
    assert kwargs["env"] == {"type": "machine", "name": "ci-1"}
    assert "use_private_worker" not in kwargs
    assert "labels" not in kwargs

    # AC6 (b): WARN log captured with the documented deprecation text.
    warn_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "legacy marker payload detected" in r.getMessage()
        and "translating use_private_worker+labels to env=AgentEnv" in r.getMessage()
    ]
    assert warn_records, (
        "expected a WARN log matching 'legacy marker payload detected; translating "
        "use_private_worker+labels to env=AgentEnv'; got "
        f"{[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
    )

    handle = store.get(task_id)
    assert handle is not None
    assert handle.runtime == "cloud"
    assert handle.state == TaskState.COMPLETED


def test_legacy_use_private_worker_without_labels_fails_early(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.10.0 (AC6 b sibling): ``use_private_worker=True`` without a worker name fails.

    Per the supervisor's deprecation translator (and the No-Silent-Failures
    workspace rule), ``use_private_worker=True`` without a paired
    ``labels.worker=X`` would round-trip to a 400 from the gateway
    (``env={type:"machine"}`` requires ``name``). The supervisor refuses
    early with ``error_kind="marker_decode_error"`` rather than dispatching
    a payload guaranteed to fail.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env
    mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    cb = MagicMock()
    cmd = _marker_cmd(
        "do work",
        {
            "repo_url": "https://github.com/o/r",
            "use_private_worker": True,
            # NB: no labels.worker provided.
        },
    )
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "marker_decode_error"
    assert "use_private_worker=True requires" in failed["data"]["error_detail"]
    cb.assert_called_once_with(task_id, 1)


def test_local_cmd_does_not_use_cloud_client(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
) -> None:
    sup, _, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    pid = sup.spawn(
        task_id,
        cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        cwd=None,
        env=None,
        event_log=log,
        on_exit=None,
    )
    assert pid > 0
    mock_cls.assert_not_called()
    assert sup.join(task_id, timeout=5.0)


def test_cloud_spawn_updates_state_starting(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, store, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-test"},
        "run": {"id": "run-test"},
    }

    states: list[TaskState] = []

    def capture_run(*args: Any, **kwargs: Any) -> MagicMock:
        h = store.get(task_id)
        if h is not None:
            states.append(h.state)
        return MagicMock()

    mocker.patch(
        "popolaloom.daemon.cloud_poller.run_poll_loop",
        side_effect=capture_run,
    )
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    assert TaskState.STARTING in states
    handle = store.get(task_id)
    assert handle is not None
    assert handle.state == TaskState.STARTING


def test_cloud_queued_event_emitted(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-test"},
        "run": {"id": "run-test"},
    }
    mocker.patch(
        "popolaloom.daemon.cloud_poller.run_poll_loop",
        return_value=MagicMock(),
    )
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    log.fsync()
    types = [e["type"] for e in log.tail()]
    assert "cloud.queued" in types
    data = next(e["data"] for e in log.tail() if e["type"] == "cloud.queued")
    assert data["task_id"] == task_id
    assert data["agent_id"] == "bc-test"
    assert data["run_id"] == "run-test"
    assert data["runtime"] == "cloud"
    assert data["initial_phase"] == "CREATING"


def test_create_agent_cursor_error_emits_failed_and_on_exit(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.side_effect = CursorCloudError(
        "boom",
        status_code=500,
        is_retryable=True,
    )
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    assert pid == 0
    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "cloud_create_failed"
    assert failed["data"]["error"]["error_type"] == "CursorCloudError"
    assert failed["data"]["error"]["is_retryable"] is True
    cb.assert_called_once_with(task_id, 1)


def test_spawn_returns_zero_cloud_path(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-test"},
        "run": {"id": "run-test"},
    }
    mocker.patch(
        "popolaloom.daemon.cloud_poller.run_poll_loop",
        return_value=MagicMock(),
    )
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    assert sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log) == 0


def test_malformed_marker_json_emits_task_failed(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env
    mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    cmd = [*CLOUD_BUILD_COMMAND_MARKER, "not-json"]
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "marker_decode_error"
    cb.assert_called_once_with(task_id, 1)


def test_missing_api_key_emits_task_failed(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    sup, _, log, task_id = cloud_task_env
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "missing_api_key"
    cb.assert_called_once_with(task_id, 1)


def test_spawn_cloud_failed_path_still_tags_runtime_cloud(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing API key after valid marker: runtime=cloud before task.failed."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    sup, store, log, task_id = cloud_task_env
    update_calls: list[tuple[str, dict[str, Any]]] = []
    orig_update = store.update

    def capture_update(tid: str, **kwargs: Any) -> TaskHandle:
        update_calls.append((tid, dict(kwargs)))
        return orig_update(tid, **kwargs)

    store.update = capture_update  # type: ignore[method-assign]

    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    log.fsync()

    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "missing_api_key"

    assert update_calls, "expected state_store.update to be called"
    first_tid, first_kw = update_calls[0]
    assert first_tid == task_id
    assert first_kw == {"runtime": "cloud"}

    handle = store.get(task_id)
    assert handle is not None
    assert handle.runtime == "cloud"


def test_poller_thread_registered_in_workers(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-test"},
        "run": {"id": "run-test"},
    }
    instance.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    deadline = time.monotonic() + 5.0
    threads: list[Any] = []
    while time.monotonic() < deadline:
        with sup._lock:
            threads = list(sup._workers.get(task_id, []))
        if threads and all(not t.is_alive() for t in threads):
            break
        time.sleep(0.05)
    assert len(threads) == 1
    assert hasattr(threads[0], "daemon")
    assert threads[0].daemon is True
