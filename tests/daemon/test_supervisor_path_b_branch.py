"""Tests for Supervisor's Path-B (``--auth-mode=session-jwt``) cloud branch (v1.1.0 Track 6).

When the marker payload carries ``extra["__auth_mode__"] = "session-jwt"``
the supervisor MUST route the dispatch through the experimental
Connect-RPC :class:`CursorCloudInternalClient` instead of the stable
REST :class:`CloudCursorClient`. This test file pins:

- The Path-B branch loads the JWT bundle and constructs a
  ``StartBackgroundComposerFromSnapshot`` body carrying the operator's
  ``effort`` / ``long_running`` / ``model`` knobs.
- The ``background_composer_id`` from the RPC response becomes the
  task's ``cursor_agent_id``.
- A ``cloud.queued`` event is appended with ``auth_mode="session-jwt"``
  (the discriminator that lets observers tell Path-A vs Path-B).
- Failure paths (JWT load failure, RPC failure) emit ``task.failed``
  with a Path-B-tagged ``error_kind`` per No-Silent-Failures.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import CLOUD_BUILD_COMMAND_MARKER
from popolaloom.cloud.internal.jwt_auth import JWTAuthError, JWTBundle
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.supervisor import Supervisor, _parse_path_b_time_budget


def _marker_cmd(prompt: str, extra: dict[str, Any]) -> list[str]:
    payload = {"cwd": None, "extra": extra, "prompt": prompt}
    return [
        *CLOUD_BUILD_COMMAND_MARKER,
        json.dumps(payload, sort_keys=True),
    ]


def _fake_bundle() -> JWTBundle:
    """Return a synthetic JWT bundle for tests (no signature validation)."""
    return JWTBundle(
        access_token="fake-jwt-supervisor-test",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 3600,
    )


@pytest.fixture
def cloud_task_env(
    tmp_path: Path,
) -> tuple[Supervisor, StateStore, EventLog, str]:
    """Pre-register a PENDING task handle and return ``(sup, store, log, task_id)``.

    Mirrors the convention in :mod:`tests.daemon.test_supervisor_cloud_branch`
    so the Path-B branch tests live alongside the REST branch tests
    with the same fixture shape.
    """
    task_id = "sup-path-b-1"
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


# ── _parse_path_b_time_budget helper ────────────────────────────────


def test_parse_path_b_time_budget_seconds_default() -> None:
    assert _parse_path_b_time_budget("60") == 60


def test_parse_path_b_time_budget_explicit_s_suffix() -> None:
    assert _parse_path_b_time_budget("90s") == 90


def test_parse_path_b_time_budget_minutes_suffix() -> None:
    assert _parse_path_b_time_budget("30m") == 30 * 60


def test_parse_path_b_time_budget_hours_suffix() -> None:
    assert _parse_path_b_time_budget("4h") == 4 * 3600


def test_parse_path_b_time_budget_grind_preset_value() -> None:
    """The grind preset's 14400s budget round-trips through the parser."""
    assert _parse_path_b_time_budget("14400s") == 14400


def test_parse_path_b_time_budget_empty_returns_zero() -> None:
    assert _parse_path_b_time_budget("") == 0


def test_parse_path_b_time_budget_invalid_raises() -> None:
    with pytest.raises(ValueError, match="not in accepted forms"):
        _parse_path_b_time_budget("forever")


# ── _spawn_cloud Path-B branch ──────────────────────────────────────


def test_path_b_branch_dispatches_via_connect_rpc(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
) -> None:
    """Marker with ``__auth_mode__=session-jwt`` routes through Connect-RPC.

    Mocks the internal Connect-RPC client's HTTP transport with
    :class:`httpx.MockTransport` returning a fixture
    ``background_composer_id``. Asserts:

    1. The RPC body carried the operator's ``effort=high`` /
       ``long_running=True`` / ``model=gpt-5.5`` knobs.
    2. ``state_store`` was rehydrated with ``cursor_agent_id="bc-test-123"``
       and ``runtime="cloud"``.
    3. ``cloud.queued`` event carries ``auth_mode="session-jwt"`` plus
       the dashboard URL.
    """
    sup, store, log, task_id = cloud_task_env

    captured_body: dict[str, Any] = {}
    captured_url: str = ""
    captured_headers: dict[str, str] = {}

    def _handle(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url, captured_headers
        captured_url = str(request.url)
        captured_headers = dict(request.headers)
        captured_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            status_code=200,
            json={"background_composer_id": "bc-test-123"},
        )

    transport = httpx.MockTransport(_handle)
    fake_client = httpx.Client(transport=transport)

    # Patch CursorCloudInternalClient so __enter__ returns a real client
    # backed by the mocked transport.
    from popolaloom.cloud.internal import (
        CursorCloudInternalClient as _RealClient,
    )

    def _make_client(bundle: JWTBundle, **_kwargs: Any) -> _RealClient:
        return _RealClient(bundle, http_client=fake_client)

    cmd = _marker_cmd(
        "smoke test",
        {
            "__auth_mode__": "session-jwt",
            "repo_url": "https://github.com/test/repo",
            "starting_ref": "main",
            "model": "gpt-5.5",
            "effort": "high",
            "long_running": True,
            "mode": "plan",
            "auto_proceed_after_plan": True,
            "time_budget": "14400s",
        },
    )

    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=_fake_bundle(),
    ), patch(
        "popolaloom.cloud.internal.CursorCloudInternalClient",
        side_effect=_make_client,
    ):
        pid = sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    fake_client.close()

    assert pid == 0

    assert captured_url.endswith(
        "/aiserver.v1.BackgroundComposerService/StartBackgroundComposerFromSnapshot"
    )
    assert captured_headers.get("authorization") == "Bearer fake-jwt-supervisor-test"
    assert captured_headers.get("connect-protocol-version") == "1"

    assert captured_body["prompt"] == "smoke test"
    assert captured_body["repos"] == [
        {"url": "https://github.com/test/repo", "starting_ref": "main"}
    ]
    assert captured_body["model_details"]["model_name"] == "gpt-5.5"
    assert captured_body["agent_mode"] == "AGENT_MODE_PLAN"
    assert captured_body["effort_mode"] == "EFFORT_MODE_HIGH"
    assert captured_body["long_running_agent_mode"] is True
    assert captured_body["auto_proceed_after_planning"] is True
    assert captured_body["time_budget_seconds"] == 14400
    assert captured_body["time_budget_ms"] == 14400 * 1000

    handle = store.get(task_id)
    assert handle is not None
    assert handle.runtime == "cloud"
    assert handle.cursor_agent_id == "bc-test-123"
    assert handle.cloud_phase == "CREATING"
    assert handle.state == TaskState.STARTING

    log.fsync()
    queued = next(e for e in log.tail() if e["type"] == "cloud.queued")
    assert queued["data"]["task_id"] == task_id
    assert queued["data"]["auth_mode"] == "session-jwt"
    assert queued["data"]["agent_id"] == "bc-test-123"
    assert queued["data"]["cursor_agent_id"] == "bc-test-123"
    assert queued["data"]["runtime"] == "cloud"
    assert queued["data"]["initial_phase"] == "CREATING"
    assert queued["data"]["dashboard_url"] == "https://cursor.com/agents/bc-test-123"


def test_path_b_branch_jwt_load_failure_emits_task_failed(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
) -> None:
    """JWTAuthError → ``task.failed`` with ``error_kind=path_b_jwt_load_failed``.

    No Silent Failures: the daemon refuses to dispatch and surfaces the
    bundled bilingual ``hint`` so the operator can fix the auth setup.
    """
    sup, _, log, task_id = cloud_task_env
    cmd = _marker_cmd(
        "smoke test",
        {
            "__auth_mode__": "session-jwt",
            "repo_url": "https://github.com/test/repo",
        },
    )
    err = JWTAuthError(
        "no JWT available",
        hint="Run `cursor login` to refresh ~/.config/cursor/auth.json.",
    )
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        side_effect=err,
    ):
        sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)

    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "path_b_jwt_load_failed"
    assert "no JWT available" in failed["data"]["error_detail"]
    assert "cursor login" in failed["data"]["error_detail"]


def test_path_b_branch_rpc_failure_emits_task_failed(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
) -> None:
    """Connect-RPC 401 → ``task.failed`` with ``error_kind=path_b_rpc_failed``.

    The error_detail MUST include both the underlying error message and
    the :class:`CursorCloudInternalError` ``hint`` (which points at
    ``--auth-mode=rest`` as the supported fallback per Q-22).
    """
    sup, _, log, task_id = cloud_task_env

    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=401,
            text="Unauthorized: token expired",
        )

    transport = httpx.MockTransport(_handle)
    fake_client = httpx.Client(transport=transport)

    from popolaloom.cloud.internal import (
        CursorCloudInternalClient as _RealClient,
    )

    def _make_client(bundle: JWTBundle, **_kwargs: Any) -> _RealClient:
        return _RealClient(bundle, http_client=fake_client)

    cmd = _marker_cmd(
        "smoke test",
        {
            "__auth_mode__": "session-jwt",
            "repo_url": "https://github.com/test/repo",
        },
    )
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=_fake_bundle(),
    ), patch(
        "popolaloom.cloud.internal.CursorCloudInternalClient",
        side_effect=_make_client,
    ):
        sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    fake_client.close()

    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "path_b_rpc_failed"
    # The error_detail carries both the original error and the bilingual hint.
    assert "401" in failed["data"]["error_detail"]
    assert "hint:" in failed["data"]["error_detail"]


def test_path_b_branch_missing_repo_url_fails_early(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
) -> None:
    """``--auth-mode=session-jwt`` without ``repo_url`` fails before RPC.

    The Connect-RPC body REQUIRES ``repos[0].url``; pr_url-only
    dispatch (which the REST path supports for "comment on this PR")
    is not supported on the StartBackgroundComposerFromSnapshot RPC.
    No-Silent-Failures: refuse early instead of dispatching a payload
    guaranteed to fail.
    """
    sup, _, log, task_id = cloud_task_env
    cmd = _marker_cmd(
        "smoke test",
        {
            "__auth_mode__": "session-jwt",
            # Note: no repo_url; pr_url instead (REST-only supported shape).
            "pr_url": "https://github.com/test/repo/pull/1",
        },
    )
    with patch(
        "popolaloom.cloud.internal.jwt_auth.load_jwt_bundle",
        return_value=_fake_bundle(),
    ):
        sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)
    log.fsync()
    failed = next(e for e in log.tail() if e["type"] == "task.failed")
    assert failed["data"]["error_kind"] == "marker_decode_error"
    assert "repo_url" in failed["data"]["error_detail"]


def test_path_b_branch_skips_when_auth_mode_marker_absent(
    cloud_task_env: tuple[Supervisor, StateStore, EventLog, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``__auth_mode__`` marker the supervisor stays on the REST path.

    Backward-compat guarantee: existing dispatches that don't set
    ``__auth_mode__`` MUST keep using :class:`CloudCursorClient` (REST
    POST /v1/agents). The Path-B branch only fires on the explicit
    ``"session-jwt"`` marker.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "k-test")
    sup, _, log, task_id = cloud_task_env

    rest_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeRestClient:
        def __init__(self, api_key: str) -> None:
            rest_calls.append(("init", {"api_key": api_key}))

        def create_agent(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            rest_calls.append(("create_agent", {"args": args, "kwargs": kwargs}))
            return {
                "agent": {"id": "rest-agent-id"},
                "run": {"id": "rest-run-id"},
            }

        def close(self) -> None:
            rest_calls.append(("close", {}))

    monkeypatch.setattr(
        "popolaloom.adapters.cursor_cloud.CloudCursorClient",
        _FakeRestClient,
    )

    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "popolaloom.daemon.cloud_poller.run_poll_loop",
        lambda *_a, **_kw: MagicMock(),
    )

    cmd = _marker_cmd(
        "rest-path smoke",
        {
            # No __auth_mode__ marker — should stay on REST.
            "repo_url": "https://github.com/test/repo",
        },
    )
    sup.spawn(task_id, cmd, cwd=None, env=None, event_log=log, on_exit=None)

    create_calls = [c for c in rest_calls if c[0] == "create_agent"]
    assert len(create_calls) == 1, (
        "REST CloudCursorClient.create_agent MUST be called when "
        f"__auth_mode__ marker is absent; got rest_calls={rest_calls}"
    )
