"""Unit tests for :mod:`popolaloom.adapters.cursor_cloud` (v0.8.5 cloud REST).

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
    CloudCursorClient,
    CursorCloudAdapter,
    CursorCloudAuthError,
    CursorCloudConflictError,
    CursorCloudError,
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
    assert payload["extra"]["model"] == "composer-2"
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
    client.create_agent("hi", "composer-2", "https://github.com/o/r")
    client.close()


def test_create_agent_payload_shape(router: respx.Router, api_key: str) -> None:
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
        "composer-2",
        "https://github.com/acme/app",
        starting_ref="develop",
        auto_create_pr=True,
        work_on_current_branch=True,
        skip_reviewer_request=True,
        env_vars={"FOO": "bar"},
    )
    body = json.loads(captured["req"].content.decode())
    assert body["prompt"] == {"text": "fix bug"}
    assert body["model"] == {"id": "composer-2"}
    assert body["repos"] == [
        {
            "url": "https://github.com/acme/app",
            "startingRef": "develop",
        }
    ]
    assert body["autoCreatePR"] is True
    assert body["autoGenerateBranch"] is False
    assert body["skipReviewerRequest"] is True
    assert body["envVars"] == {"FOO": "bar"}
    client.close()


def test_create_agent_payload_private_worker_routing(
    router: respx.Router,
    api_key: str,
) -> None:
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
        "route locally",
        "composer-2",
        "https://github.com/acme/app",
        use_private_worker=True,
        labels={"pool": "popolaloom", "worker": "ci-1"},
    )
    body = json.loads(captured["req"].content.decode())
    assert body["usePrivateWorker"] is True
    assert body["labels"] == {"pool": "popolaloom", "worker": "ci-1"}
    client.close()


def test_create_agent_returns_agent_id(router: respx.Router, api_key: str) -> None:
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
    data = client.create_agent("x", "composer-2", "https://github.com/o/r")
    assert data["agent"]["id"] == "bc-test123"
    assert data["run"]["id"] == "run-abc"
    client.close()


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
