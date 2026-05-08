"""Coverage gap-filler for ``Supervisor._spawn_cloud`` fall-through branches.

Per ``.local/.agent/active/v0.8.6-cloud-sse/COVERAGE.md`` §4.1, the cloud
spawn dispatcher in :mod:`popolaloom.daemon.supervisor` sits at 78.57 %
default-lane coverage with the missing lines concentrated in the
``_fail(error_kind=...)`` early-return branches that fire when:

* the marker JSON is malformed, the payload is the wrong shape, or any
  type-checked extra field has the wrong type
  (``marker_decode_error`` × multiple cases — payload not dict, prompt
  not str, repo_url / pr_url / env_vars / timeout_s wrong types);
* ``CURSOR_API_KEY`` is unset (``missing_api_key``);
* :meth:`CloudCursorClient.create_agent` raises ``ValueError`` /
  :class:`CursorCloudError` / :class:`CursorCloudAuthError`
  (``cloud_create_failed``);
* the Cursor response is missing ``agent.id`` or ``run.id``;
* the :class:`StateStore` does not contain a pre-registered handle when
  ``_spawn_cloud`` reaches the seeding step (Popolad pre-register
  contract violated);
* the supervisor is constructed without a :class:`StateStore`
  (``state_store is None``).

These tests parallel the existing ``tests/daemon/test_supervisor_cloud_branch.py``
patterns (``_marker_cmd`` helper + ``CloudCursorClient`` mocker.patch) but
focus exclusively on the fall-through paths the v0.8.5 baseline tests
left uncovered.

T4.1.1.a — owned file (NEW), parallel-safe with W2.2.2.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from popolaloom.adapters.cursor_cloud import (
    CLOUD_BUILD_COMMAND_MARKER,
    CursorCloudAuthError,
    CursorCloudError,
)
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.supervisor import Supervisor

# ── shared fixtures + helpers ────────────────────────────────────────────


def _marker_cmd_raw(payload_json: str) -> list[str]:
    """Build a cloud-marker argv with a pre-encoded JSON payload string."""
    return [*CLOUD_BUILD_COMMAND_MARKER, payload_json]


def _marker_cmd(prompt: Any, extra: Any) -> list[str]:
    """Build a cloud-marker argv wrapping ``prompt`` + ``extra`` into JSON.

    Mirrors the helper in ``test_supervisor_cloud_branch.py`` so the two
    files share the same marker-shape contract; ``Any``-typed parameters
    let us stuff non-string ``prompt`` / non-dict ``extra`` values to
    drive the type-check fall-throughs.
    """
    payload: dict[str, Any] = {"cwd": None, "extra": extra, "prompt": prompt}
    return [*CLOUD_BUILD_COMMAND_MARKER, json.dumps(payload)]


@pytest.fixture
def cloud_env(
    tmp_path: Path,
) -> tuple[Supervisor, StateStore, EventLog, str]:
    """Construct ``(supervisor, state_store, event_log, task_id)`` per test."""
    task_id = "sup-cloud-failpath-1"
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


def _last_failed_event(log: EventLog) -> dict[str, Any]:
    """Return the data dict of the most-recent ``task.failed`` envelope."""
    log.fsync()
    failed = [e for e in log.tail() if e["type"] == "task.failed"]
    assert failed, "expected at least one task.failed event in log"
    return dict(failed[-1]["data"])


# ── 1. state_store-is-None pre-flight ────────────────────────────────────


def test_state_store_is_none_emits_cloud_create_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Supervisor()`` (no store) + cloud marker → ``cloud_create_failed`` early return.

    Covers the pre-flight branch at supervisor.py:282-286: when no
    :class:`StateStore` was injected, ``_spawn_cloud`` cannot rehydrate
    the seeded ``STARTING/CREATING`` snapshot, so it emits ``task.failed``
    with ``error_kind="cloud_create_failed"`` (and an explanatory
    ``error_detail``) **before** any marker decode is attempted.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    task_id = "sup-no-store"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    sup = Supervisor()  # NO state_store
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    assert pid == 0
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert "state_store" in data["error_detail"]
    assert data["runtime"] == "cloud"
    cb.assert_called_once_with(task_id, 1)


# ── 2. marker_decode_error: payload not a dict ───────────────────────────


def test_marker_decode_error_payload_not_dict(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker JSON parses to a list, not a dict → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd_raw(json.dumps([1, 2, 3]))
    cb = MagicMock()
    pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    assert pid == 0
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "JSON object" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 3. marker_decode_error: extra is not a dict ──────────────────────────


def test_marker_decode_error_extra_not_dict(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra`` is a string instead of dict → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd("hi", "not-a-dict")
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "extra" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 4. marker_decode_error: prompt not str ───────────────────────────────


def test_marker_decode_error_prompt_not_str(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prompt`` is an int instead of str → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd(42, {"repo_url": "https://github.com/o/r"})
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "prompt" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 5. marker_decode_error: repo_url not str ─────────────────────────────


def test_marker_decode_error_repo_url_not_str(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.repo_url`` is an int → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd("hi", {"repo_url": 123})
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "repo_url" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 6. marker_decode_error: pr_url not str ───────────────────────────────


def test_marker_decode_error_pr_url_not_str(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.pr_url`` is an int → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r", "pr_url": 99})
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "pr_url" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 7. marker_decode_error: env_vars wrong shape ─────────────────────────


def test_marker_decode_error_env_vars_not_object(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.env_vars`` is a list (not object/null) → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd(
        "hi",
        {"repo_url": "https://github.com/o/r", "env_vars": ["A=b"]},
    )
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "env_vars" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


def test_marker_decode_error_env_vars_values_not_str(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.env_vars`` has non-string values → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd(
        "hi",
        {"repo_url": "https://github.com/o/r", "env_vars": {"A": 1}},
    )
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "env_vars" in data["error_detail"]
    assert "dict[str, str]" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 8. marker_decode_error: timeout_s not numeric ────────────────────────


def test_marker_decode_error_timeout_s_not_numeric(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.timeout_s`` is a non-numeric string → ``marker_decode_error``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd(
        "hi",
        {"repo_url": "https://github.com/o/r", "timeout_s": "five-minutes"},
    )
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "marker_decode_error"
    assert "timeout_s" in data["error_detail"]
    cb.assert_called_once_with(task_id, 1)


# ── 9. missing_api_key (env unset, extra.api_key absent) ─────────────────


def test_missing_api_key_when_env_unset_and_no_extra_override(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CURSOR_API_KEY`` unset and no ``extra.api_key`` → ``missing_api_key``.

    Note: this complements the existing ``test_missing_api_key_emits_task_failed``
    by also asserting the ``error_detail`` is ``None`` (the
    ``missing_api_key`` branch is the only ``_fail()`` call site that
    omits ``error_detail``).
    """
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    sup, _, log, task_id = cloud_env
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    cb = MagicMock()
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "missing_api_key"
    assert data.get("error_detail") is None
    cb.assert_called_once_with(task_id, 1)


# ── 10. cloud_create_failed: ValueError from create_agent ────────────────


def test_cloud_create_failed_value_error_closes_client(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_agent`` raising ``ValueError`` → ``cloud_create_failed`` + ``client.close()``.

    Covers supervisor.py:385-391 (the ``except ValueError`` clause).
    The close-on-fail is asserted to confirm the ``client is not None``
    branch on line 386 executes.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.side_effect = ValueError("simulated bad input")
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    assert pid == 0
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert "simulated bad input" in data["error_detail"]
    instance.close.assert_called_once()
    cb.assert_called_once_with(task_id, 1)


# ── 11. cloud_create_failed: CursorCloudAuthError (CursorCloudError subclass) ─


def test_cloud_create_failed_cursor_auth_error_emits_full_error_payload(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CursorCloudAuthError`` (401/403) is a ``CursorCloudError`` subclass.

    Confirms the ``except CursorCloudError as exc`` clause catches the
    auth subclass, the client is closed, and the failure envelope carries
    the subclass name + the ``is_retryable=False`` default of
    :class:`CursorCloudAuthError`.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.side_effect = CursorCloudAuthError(
        "401 unauthorized",
        status_code=401,
        is_retryable=False,
    )
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert data["error"]["error_type"] == "CursorCloudAuthError"
    assert data["error"]["is_retryable"] is False
    assert "401 unauthorized" in data["error"]["message"]
    instance.close.assert_called_once()
    cb.assert_called_once_with(task_id, 1)


# ── 12. cloud_create_failed: generic CursorCloudError 5xx ────────────────


def test_cloud_create_failed_cursor_error_retryable(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic ``CursorCloudError`` (5xx) → ``cloud_create_failed`` with retryable=True.

    Complements the existing ``test_create_agent_cursor_error_emits_failed_and_on_exit``
    by inspecting the ``client.close()`` call (line 393-394 fall-through)
    and the full error-payload shape.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.side_effect = CursorCloudError(
        "503 service unavailable",
        status_code=503,
        is_retryable=True,
    )
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert data["error"]["error_type"] == "CursorCloudError"
    assert data["error"]["is_retryable"] is True
    instance.close.assert_called_once()
    cb.assert_called_once_with(task_id, 1)


# ── 13. cloud_create_failed: response missing agent.id ───────────────────


def test_cloud_create_failed_response_missing_agent_id(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_agent`` returns ``run.id`` but no ``agent.id`` → ``cloud_create_failed``.

    Covers supervisor.py:417-423: response missing required keys ⇒ close
    client + ``_fail()``. We assert the client.close() is invoked AND no
    poller thread is registered (workers dict stays empty for this task).
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {},
        "run": {"id": "run-y"},
    }
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert "agent.id" in data["error_detail"]
    instance.close.assert_called_once()
    with sup._lock:
        assert sup._workers.get(task_id, []) == []
    cb.assert_called_once_with(task_id, 1)


# ── 14. cloud_create_failed: response missing run.id ─────────────────────


def test_cloud_create_failed_response_missing_run_id(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_agent`` returns ``agent.id`` but no ``run.id`` → ``cloud_create_failed``."""
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-x"},
        "run": {},
    }
    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert "run.id" in data["error_detail"]
    instance.close.assert_called_once()
    cb.assert_called_once_with(task_id, 1)


# ── 15. cloud_create_failed: existing handle removed before seed ─────────


def test_cloud_create_failed_existing_handle_missing_from_state_store(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-register contract violated → ``cloud_create_failed`` + client.close().

    We monkeypatch :meth:`StateStore.get` so it returns ``None`` *only for*
    the lookup at supervisor.py:435 (``existing_handle = self._state_store.get(task_id)``)
    while leaving the earlier ``runtime="cloud"`` ``update`` call intact.
    Implementation: we override ``store.get`` after the supervisor has
    already done its ``store.update(task_id, runtime="cloud")``.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, store, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-x"},
        "run": {"id": "run-y"},
    }

    real_get = store.get
    call_count = {"n": 0}

    def faked_get(tid: str) -> TaskHandle | None:
        call_count["n"] += 1
        # 1st call (if any) inside _spawn_cloud is at the `existing_handle`
        # site at supervisor.py:435 — we want it to return None to drive the
        # "Popolad pre-register contract violated" branch.
        if call_count["n"] == 1:
            return None
        return real_get(tid)

    monkeypatch.setattr(store, "get", faked_get)

    cb = MagicMock()
    cmd = _marker_cmd("hi", {"repo_url": "https://github.com/o/r"})
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=cb)
    data = _last_failed_event(log)
    assert data["error_kind"] == "cloud_create_failed"
    assert "missing from state_store" in data["error_detail"]
    instance.close.assert_called_once()
    cb.assert_called_once_with(task_id, 1)


# ── 16. happy path with env_vars + timeout_s + extra.api_key override ────


def test_extra_api_key_override_and_env_vars_normalized(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.api_key`` (non-empty) overrides ``CURSOR_API_KEY``; env_vars + timeout_s pass.

    Covers supervisor.py:319-320 (the ``raw_override`` branch — non-empty
    extra.api_key is preferred), :350-358 (env_vars dict[str, str]
    valid path → ``env_vars_param = dict(ev)``), and :361-364 (timeout_s
    valid float coercion). No ``_fail()`` should fire.
    """
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)  # force extra.api_key
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-z"},
        "run": {"id": "run-z"},
    }
    mocker.patch(
        "popolaloom.daemon.cloud_poller.run_poll_loop",
        return_value=MagicMock(),
    )
    cmd = _marker_cmd(
        "hi",
        {
            "repo_url": "https://github.com/o/r",
            "api_key": "override-key",
            "env_vars": {"FOO": "bar"},
            "timeout_s": 30,
        },
    )
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    log.fsync()
    types = [e["type"] for e in log.tail()]
    assert "cloud.queued" in types
    assert "task.failed" not in types
    mock_cls.assert_called_once_with("override-key")
    kwargs = instance.create_agent.call_args.kwargs
    assert kwargs["env_vars"] == {"FOO": "bar"}
    assert kwargs["timeout_s"] == 30.0


# ── 17. env_vars=None branch (explicit null) ────────────────────────────


def test_env_vars_explicit_null_treated_as_unset(
    cloud_env: tuple[Supervisor, StateStore, EventLog, str],
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra.env_vars=None`` (JSON ``null``) is allowed and normalized to ``None``.

    Covers supervisor.py:343-344 — the explicit ``ev is None`` branch
    inside the ``"env_vars" in extra`` block (different from the
    ``"env_vars" not in extra`` outer condition).
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_env
    mock_cls = mocker.patch("popolaloom.adapters.cursor_cloud.CloudCursorClient")
    instance = mock_cls.return_value
    instance.create_agent.return_value = {
        "agent": {"id": "bc-z"},
        "run": {"id": "run-z"},
    }
    mocker.patch(
        "popolaloom.daemon.cloud_poller.run_poll_loop",
        return_value=MagicMock(),
    )
    cmd = _marker_cmd(
        "hi",
        {"repo_url": "https://github.com/o/r", "env_vars": None},
    )
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    log.fsync()
    types = [e["type"] for e in log.tail()]
    assert "cloud.queued" in types
    assert instance.create_agent.call_args.kwargs["env_vars"] is None
