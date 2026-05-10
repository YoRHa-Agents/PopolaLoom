"""Unit tests for :mod:`popolaloom.adapters.cursor_cloud` (v0.10.0 cloud REST).

v0.10.0 Wave D2 (DECISIONS Q-2 / Q-8 / Q-11): the request-body schema pivoted
from the legacy ``usePrivateWorker:true + labels.worker:X +
autoGenerateBranch:false`` shape to a typed ``env: {type, name?}``
discriminated union. The default model fallback also shifted from
``"composer-2"`` to ``"default"`` (research/02-path-1-visibility-probe.md
§1 L70-77). Tests in this file pin both the new behavior AND the
``DeprecationWarning`` translation path that lets v0.9.x extras keep
working through the v0.10.x deprecation window.

Uses ``respx`` to mock ``httpx`` calls against ``https://api.cursor.com``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from popolaloom.adapters import list_registered
from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    AgentEnv,
    CloudCursorClient,
    CursorCloudAdapter,
    CursorCloudAuthError,
    CursorCloudConflictError,
    CursorCloudError,
    WorkerInfo,
    basic_auth_header_value,
)


@pytest.fixture
def api_key() -> str:
    return "test-api-key"


@pytest.fixture
def router() -> respx.Router:
    with respx.mock(base_url=CURSOR_API_BASE, assert_all_called=False) as router:
        yield router


def test_build_command_returns_cloud_marker() -> None:
    """v0.10.0 (AC12): default model is now ``"default"`` not ``"composer-2"``."""
    adapter = CursorCloudAdapter()
    marker = adapter.build_command(
        "do the thing",
        cwd=Path("/tmp/ws"),
        extra={"repo_url": "https://github.com/o/r"},
    )
    assert marker[0] == "__cloud__"
    assert marker[1] == "cursor-cloud"
    payload = json.loads(marker[2])
    assert payload["prompt"] == "do the thing"
    assert payload["cwd"] == "/tmp/ws"
    assert payload["extra"]["repo_url"] == "https://github.com/o/r"
    assert payload["extra"]["model"] == "default"
    assert payload["extra"]["auto_create_pr"] is False


def test_build_command_requires_repo_url_or_pr_url() -> None:
    adapter = CursorCloudAdapter()
    with pytest.raises(ValueError, match="repo_url or pr_url"):
        adapter.build_command("p", extra={})


def test_build_command_validates_extra_types() -> None:
    adapter = CursorCloudAdapter()
    with pytest.raises(ValueError, match="auto_create_pr must be bool"):
        adapter.build_command(
            "p",
            extra={"repo_url": "https://github.com/o/r", "auto_create_pr": "yes"},
        )


def test_adapter_is_registered_under_cursor_cloud() -> None:
    assert "cursor-cloud" in list_registered()


def test_is_available_requires_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = CursorCloudAdapter()
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert adapter.is_available() is False
    monkeypatch.setenv("CURSOR_API_KEY", "secret")
    assert adapter.is_available() is True
    monkeypatch.setenv("CURSOR_API_KEY", "   ")
    assert adapter.is_available() is False


def test_create_agent_uses_basic_auth(router: respx.Router, api_key: str) -> None:
    """v0.10.0: Basic-auth header still the same; model defaults to ``default``."""
    expected = basic_auth_header_value(api_key)

    def check_headers(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == expected
        return httpx.Response(
            200,
            json={
                "agent": {"id": "bc-x"},
                "run": {"id": "run-x"},
            },
        )

    router.post("/v1/agents").mock(side_effect=check_headers)
    client = CloudCursorClient(api_key)
    client.create_agent(
        "hi",
        "default",
        "https://github.com/o/r",
        skip_github_app_preflight=True,
    )
    client.close()


def test_create_agent_payload_shape(router: respx.Router, api_key: str) -> None:
    """v0.10.0 AC1 (a)+(b): body emits ``workOnCurrentBranch:true`` (not the legacy
    ``autoGenerateBranch:false``) and NEVER emits ``usePrivateWorker`` /
    ``labels`` / ``autoGenerateBranch``.
    """
    captured: dict[str, httpx.Request] = {}

    def record(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200,
            json={"agent": {"id": "bc-x"}, "run": {"id": "run-x"}},
        )

    router.post("/v1/agents").mock(side_effect=record)
    client = CloudCursorClient(api_key)
    client.create_agent(
        "fix bug",
        "default",
        "https://github.com/acme/app",
        starting_ref="develop",
        auto_create_pr=True,
        work_on_current_branch=True,
        skip_reviewer_request=True,
        env_vars={"FOO": "bar"},
        skip_github_app_preflight=True,
    )
    body = json.loads(captured["req"].content.decode())
    assert body["prompt"] == {"text": "fix bug"}
    assert body["model"] == {"id": "default"}
    assert body["repos"] == [
        {
            "url": "https://github.com/acme/app",
            "startingRef": "develop",
        }
    ]
    assert body["autoCreatePR"] is True
    # AC1 (b): workOnCurrentBranch:true replaces autoGenerateBranch:false.
    assert body["workOnCurrentBranch"] is True
    # AC1 (a): legacy fields are NEVER on the request body.
    assert "autoGenerateBranch" not in body
    assert "usePrivateWorker" not in body
    assert "labels" not in body
    assert body["skipReviewerRequest"] is True
    assert body["envVars"] == {"FOO": "bar"}
    client.close()


def test_create_agent_with_env_machine_emits_env_field(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (a): ``env={"type":"machine","name":"X"}`` lands on body as ``env``."""
    captured: dict[str, httpx.Request] = {}

    def record(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200,
            json={"agent": {"id": "bc-x"}, "run": {"id": "run-x"}},
        )

    router.post("/v1/agents").mock(side_effect=record)
    client = CloudCursorClient(api_key)
    client.create_agent(
        "deploy",
        "default",
        "https://github.com/acme/app",
        env={"type": "machine", "name": "probe-w1"},
        skip_github_app_preflight=True,
    )
    body = json.loads(captured["req"].content.decode())
    assert body["env"] == {"type": "machine", "name": "probe-w1"}
    # No legacy v0.9.x fields are present.
    assert "usePrivateWorker" not in body
    assert "labels" not in body
    assert "autoGenerateBranch" not in body
    client.close()


def test_create_agent_with_env_pool_emits_env_field(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (e): ``env={"type":"pool","name":"X"}`` lands on body as ``env``."""
    captured: dict[str, httpx.Request] = {}

    def record(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200,
            json={"agent": {"id": "bc-x"}, "run": {"id": "run-x"}},
        )

    router.post("/v1/agents").mock(side_effect=record)
    client = CloudCursorClient(api_key)
    env_param: AgentEnv = {"type": "pool", "name": "team-pool"}
    client.create_agent(
        "deploy",
        "default",
        "https://github.com/acme/app",
        env=env_param,
        skip_github_app_preflight=True,
    )
    body = json.loads(captured["req"].content.decode())
    assert body["env"] == {"type": "pool", "name": "team-pool"}
    client.close()


def test_create_agent_env_kwarg_is_optional(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (e): ``create_agent`` accepts ``env: AgentEnv | None``; default omits ``env``."""
    captured: dict[str, httpx.Request] = {}

    def record(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200,
            json={"agent": {"id": "bc-x"}, "run": {"id": "run-x"}},
        )

    router.post("/v1/agents").mock(side_effect=record)
    client = CloudCursorClient(api_key)
    client.create_agent(
        "deploy",
        "default",
        "https://github.com/acme/app",
        skip_github_app_preflight=True,
    )
    body = json.loads(captured["req"].content.decode())
    assert "env" not in body
    client.close()


def test_create_agent_work_on_current_branch_emits_new_field(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (b): ``work_on_current_branch=True`` emits ``workOnCurrentBranch:true``,
    NEVER ``autoGenerateBranch:false``.
    """
    captured: dict[str, httpx.Request] = {}

    def record(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200,
            json={"agent": {"id": "bc-x"}, "run": {"id": "run-x"}},
        )

    router.post("/v1/agents").mock(side_effect=record)
    client = CloudCursorClient(api_key)
    client.create_agent(
        "p",
        "default",
        "https://github.com/o/r",
        work_on_current_branch=True,
        skip_github_app_preflight=True,
    )
    body = json.loads(captured["req"].content.decode())
    assert body["workOnCurrentBranch"] is True
    assert "autoGenerateBranch" not in body
    client.close()


def test_create_agent_work_on_current_branch_false_omits_field(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (b): when ``work_on_current_branch=False`` the body does not emit the field."""
    captured: dict[str, httpx.Request] = {}

    def record(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200,
            json={"agent": {"id": "bc-x"}, "run": {"id": "run-x"}},
        )

    router.post("/v1/agents").mock(side_effect=record)
    client = CloudCursorClient(api_key)
    client.create_agent(
        "p",
        "default",
        "https://github.com/o/r",
        work_on_current_branch=False,
        skip_github_app_preflight=True,
    )
    body = json.loads(captured["req"].content.decode())
    assert "workOnCurrentBranch" not in body
    assert "autoGenerateBranch" not in body
    client.close()


def test_create_agent_use_private_worker_kwarg_raises_type_error(api_key: str) -> None:
    """AC1 (c): ``create_agent(use_private_worker=True)`` raises ``TypeError``.

    Q-11 dropped the legacy kwarg from the signature; passing it must raise
    a Python-level ``TypeError`` (unexpected keyword argument) so the
    failure surfaces at the call site rather than silently being ignored.
    """
    client = CloudCursorClient(api_key)
    try:
        with pytest.raises(
            TypeError,
            match=r"unexpected keyword argument 'use_private_worker'",
        ):
            client.create_agent(  # type: ignore[call-arg]
                "p",
                "default",
                "https://github.com/o/r",
                use_private_worker=True,
            )
    finally:
        client.close()


def test_create_agent_labels_kwarg_raises_type_error(api_key: str) -> None:
    """AC1 (c) sibling: ``create_agent(labels=...)`` also raises ``TypeError``."""
    client = CloudCursorClient(api_key)
    try:
        with pytest.raises(
            TypeError,
            match=r"unexpected keyword argument 'labels'",
        ):
            client.create_agent(  # type: ignore[call-arg]
                "p",
                "default",
                "https://github.com/o/r",
                labels={"worker": "ci-1"},
            )
    finally:
        client.close()


def test_normalize_cloud_extra_use_private_worker_with_worker_translates_with_warning() -> None:
    """AC1 (d): legacy ``use_private_worker=True + worker_name=X`` extras path
    emits ``DeprecationWarning`` AND translates to ``env={type:"machine", name:"X"}``.

    The kwarg path is removed (TypeError per AC1 (c)), but the extras
    payload-passthrough path stays alive for one minor release per Q-11.
    """
    from popolaloom.adapters.cursor_cloud import _normalize_cloud_extra

    with pytest.warns(DeprecationWarning, match=r"deprecated"):
        out = _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "use_private_worker": True,
                "worker_name": "ci-worker-1",
            }
        )
    assert out["env"] == {"type": "machine", "name": "ci-worker-1"}
    # Legacy keys are no longer emitted into the marker payload.
    assert "use_private_worker" not in out
    assert "labels" not in out


def test_normalize_cloud_extra_use_private_worker_with_labels_translates_with_warning() -> None:
    """AC1 (d) variant: ``use_private_worker=True + labels={"worker":"X"}`` also
    translates to ``env={type:"machine", name:"X"}`` with ``DeprecationWarning``.
    """
    from popolaloom.adapters.cursor_cloud import _normalize_cloud_extra

    with pytest.warns(DeprecationWarning, match=r"deprecated"):
        out = _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "use_private_worker": True,
                "labels": {"worker": "ci-worker-1"},
            }
        )
    assert out["env"] == {"type": "machine", "name": "ci-worker-1"}
    assert "use_private_worker" not in out
    assert "labels" not in out


def test_create_agent_returns_agent_id(router: respx.Router, api_key: str) -> None:
    """v0.10.0: ``create_agent`` returns the parsed body — model is now ``default``."""
    router.post("/v1/agents").mock(
        return_value=httpx.Response(
            200,
            json={
                "agent": {"id": "bc-test123"},
                "run": {"id": "run-abc"},
            },
        )
    )
    client = CloudCursorClient(api_key)
    data = client.create_agent(
        "x",
        "default",
        "https://github.com/o/r",
        skip_github_app_preflight=True,
    )
    assert data["agent"]["id"] == "bc-test123"
    assert data["run"]["id"] == "run-abc"
    client.close()


# ---------------------------------------------------------------------------
# AC1 (f): ``me()`` parses ``userId|userFirstName|userLastName`` presence as
# ``api_key_class="personal"``; absence as ``"service_account"``.
# ---------------------------------------------------------------------------


def test_me_returns_personal_when_user_id_present(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (f): personal-key trio (``userId``) presence → ``api_key_class="personal"``."""
    router.get("/v1/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "apiKeyName": "test-key",
                "userId": 42,
                "userEmail": "alice@example.com",
            },
        )
    )
    client = CloudCursorClient(api_key)
    try:
        info = client.me()
    finally:
        client.close()
    assert info["api_key_class"] == "personal"
    assert info["user_id"] == 42
    assert info["user_email"] == "alice@example.com"


def test_me_returns_personal_when_user_first_name_present(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (f): ``userFirstName`` alone is enough for the personal-key heuristic."""
    router.get("/v1/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "apiKeyName": "test-key",
                "userFirstName": "Alice",
                "userEmail": "alice@example.com",
            },
        )
    )
    client = CloudCursorClient(api_key)
    try:
        info = client.me()
    finally:
        client.close()
    assert info["api_key_class"] == "personal"


def test_me_returns_personal_when_user_last_name_present(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (f): ``userLastName`` alone also flips the heuristic to personal."""
    router.get("/v1/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "apiKeyName": "test-key",
                "userLastName": "Doe",
                "userEmail": "alice@example.com",
            },
        )
    )
    client = CloudCursorClient(api_key)
    try:
        info = client.me()
    finally:
        client.close()
    assert info["api_key_class"] == "personal"


def test_me_returns_service_account_when_no_personal_marker(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (f): without any of the personal-marker trio → ``api_key_class="service_account"``."""
    router.get("/v1/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "apiKeyName": "ci-bot",
                "userEmail": "ci-bot@example.com",
                "createdAt": "2026-01-01T00:00:00Z",
            },
        )
    )
    client = CloudCursorClient(api_key)
    try:
        info = client.me()
    finally:
        client.close()
    assert info["api_key_class"] == "service_account"
    assert info["user_id"] is None
    assert info["user_email"] == "ci-bot@example.com"


# ---------------------------------------------------------------------------
# AC1 (g): ``list_workers()`` parses ``GET /v0/private-workers`` into
# typed ``WorkerInfo`` rows (snake_case).
# ---------------------------------------------------------------------------


def test_list_workers_parses_typed_worker_info(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (g): ``list_workers()`` returns ``list[WorkerInfo]`` with snake_case keys."""
    router.get("/v0/private-workers").mock(
        return_value=httpx.Response(
            200,
            json={
                "workers": [
                    {
                        "workerId": "uuid-1",
                        "name": "probe-w1",
                        "isInUse": False,
                        "activeBcId": None,
                        "repoUrl": "https://github.com/acme/app",
                        "userId": 42,
                    },
                    {
                        "workerId": "uuid-2",
                        "name": "probe-w2",
                        "isInUse": True,
                        "activeBcId": "bc-running",
                        "repoUrl": "https://github.com/acme/app",
                        "userId": 42,
                    },
                ]
            },
        )
    )
    client = CloudCursorClient(api_key)
    try:
        rows = client.list_workers()
    finally:
        client.close()
    assert len(rows) == 2
    first: WorkerInfo = rows[0]
    assert first["worker_id"] == "uuid-1"
    assert first["name"] == "probe-w1"
    assert first["is_in_use"] is False
    assert first["active_bc_id"] is None
    assert first["repo_url"] == "https://github.com/acme/app"
    assert first["user_id"] == 42
    second = rows[1]
    assert second["is_in_use"] is True
    assert second["active_bc_id"] == "bc-running"
    # Wire-side camelCase keys must NOT leak through.
    for row in rows:
        for wire_key in ("workerId", "isInUse", "activeBcId", "repoUrl", "userId"):
            assert wire_key not in row, (
                f"{wire_key!r} (camelCase) leaked into WorkerInfo row {row!r}"
            )


def test_list_workers_empty_list_when_no_workers(
    router: respx.Router,
    api_key: str,
) -> None:
    """AC1 (g): missing / empty ``workers`` field returns ``[]``."""
    router.get("/v0/private-workers").mock(
        return_value=httpx.Response(200, json={"workers": []})
    )
    client = CloudCursorClient(api_key)
    try:
        rows = client.list_workers()
    finally:
        client.close()
    assert rows == []


def test_get_agent(router: respx.Router, api_key: str) -> None:
    router.get("/v1/agents/bc-1").mock(
        return_value=httpx.Response(200, json={"id": "bc-1", "status": "ACTIVE"})
    )
    client = CloudCursorClient(api_key)
    data = client.get_agent("bc-1")
    assert data["id"] == "bc-1"
    client.close()


def test_get_run(router: respx.Router, api_key: str) -> None:
    router.get("/v1/agents/bc-1/runs/run-9").mock(
        return_value=httpx.Response(
            200,
            json={"id": "run-9", "agentId": "bc-1", "status": "RUNNING"},
        )
    )
    client = CloudCursorClient(api_key)
    data = client.get_run("bc-1", "run-9")
    assert data["status"] == "RUNNING"
    client.close()


def test_cancel_run(router: respx.Router, api_key: str) -> None:
    router.post("/v1/agents/bc-1/runs/run-9/cancel").mock(
        return_value=httpx.Response(200, json={"id": "run-9"})
    )
    client = CloudCursorClient(api_key)
    data = client.cancel_run("bc-1", "run-9")
    assert data["id"] == "run-9"
    client.close()


def test_4xx_raises_auth_error(router: respx.Router, api_key: str) -> None:
    router.get("/v1/agents/bc-1").mock(return_value=httpx.Response(401, text="nope"))
    client = CloudCursorClient(api_key)
    with pytest.raises(CursorCloudAuthError) as ei:
        client.get_agent("bc-1")
    assert ei.value.status_code == 401
    assert ei.value.is_retryable is False
    client.close()


def test_409_raises_conflict_error(router: respx.Router, api_key: str) -> None:
    router.post("/v1/agents/bc-1/runs/run-9/cancel").mock(
        return_value=httpx.Response(409, text="agent_busy")
    )
    client = CloudCursorClient(api_key)
    with pytest.raises(CursorCloudConflictError) as ei:
        client.cancel_run("bc-1", "run-9")
    assert ei.value.status_code == 409
    assert ei.value.is_retryable is False
    client.close()


def test_5xx_is_retryable(router: respx.Router, api_key: str) -> None:
    router.get("/v1/agents/bc-1").mock(return_value=httpx.Response(503, text="upstream"))
    client = CloudCursorClient(api_key)
    with pytest.raises(CursorCloudError) as ei:
        client.get_agent("bc-1")
    exc = ei.value
    assert exc.status_code == 503
    assert type(exc) is CursorCloudError
    assert exc.is_retryable is True
    client.close()


def test_timeout_propagates(api_key: str) -> None:
    client = CloudCursorClient(api_key)
    with (
        patch.object(
            client._client,
            "request",
            side_effect=httpx.TimeoutException("timeout"),
        ),
        pytest.raises(CursorCloudError) as ei,
    ):
        client.get_agent("bc-1")
    assert ei.value.is_retryable is True
    assert ei.value.status_code is None
    client.close()
