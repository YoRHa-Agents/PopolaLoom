"""Gap-filler coverage for :mod:`popolaloom.adapters.cursor_cloud` (v0.8.5 CI gate).

Branches are mostly validation in :func:`_normalize_cloud_extra`, HTTP mapping,
``CloudCursorClient`` edges, and empty JSON bodies — cheap pure / mocked tests.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudAdapter,
    CursorCloudError,
    _normalize_cloud_extra,
)


@pytest.fixture
def api_key() -> str:
    return "cov-api-key"


@pytest.fixture
def router() -> respx.Router:
    with respx.mock(base_url=CURSOR_API_BASE, assert_all_called=False) as router:
        yield router


def test_basic_auth_header_value_round_trip() -> None:
    from popolaloom.adapters.cursor_cloud import basic_auth_header_value

    key = "test_key_xyz"
    header = basic_auth_header_value(key)
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("ascii")
    assert decoded == f"{key}:"


def test_normalize_cloud_extra_requires_repo_or_pr_url() -> None:
    with pytest.raises(ValueError, match="repo_url or pr_url is required"):
        _normalize_cloud_extra({})


def test_normalize_cloud_extra_repo_url_must_be_str() -> None:
    with pytest.raises(ValueError, match="repo_url must be str"):
        _normalize_cloud_extra({"repo_url": 123, "pr_url": None})


def test_normalize_cloud_extra_pr_url_must_be_str() -> None:
    with pytest.raises(ValueError, match="pr_url must be str"):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "pr_url": 99})


def test_normalize_cloud_extra_starting_ref_must_be_str() -> None:
    with pytest.raises(ValueError, match="starting_ref must be str"):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "starting_ref": ["main"]})


def test_normalize_cloud_extra_model_must_be_str() -> None:
    with pytest.raises(ValueError, match="model must be str"):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "model": None})


@pytest.mark.parametrize(
    "flag",
    ["auto_create_pr", "work_on_current_branch", "skip_reviewer_request"],
)
def test_normalize_cloud_extra_bool_flags_must_be_bool(flag: str) -> None:
    extra: dict[str, object] = {"repo_url": "https://github.com/o/r", flag: "yes"}
    with pytest.raises(ValueError, match=f"{flag} must be bool"):
        _normalize_cloud_extra(extra)


def test_normalize_cloud_extra_env_vars_none_omitted() -> None:
    out = _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "env_vars": None})
    assert "env_vars" not in out


def test_normalize_cloud_extra_env_vars_must_be_dict() -> None:
    with pytest.raises(ValueError, match="env_vars must be dict"):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "env_vars": "x"})


def test_normalize_cloud_extra_env_vars_values_must_be_str() -> None:
    with pytest.raises(ValueError, match="dict\\[str, str\\] only"):
        _normalize_cloud_extra(
            {"repo_url": "https://github.com/o/r", "env_vars": {"A": "ok", "B": 1}}
        )


def test_normalize_cloud_extra_accepts_private_worker_convenience_labels() -> None:
    out = _normalize_cloud_extra(
        {
            "repo_url": "https://github.com/o/r",
            "worker_name": "ci-worker-1",
            "pool_name": "popolaloom",
        }
    )
    assert out["use_private_worker"] is True
    assert out["labels"] == {"worker": "ci-worker-1", "pool": "popolaloom"}


def test_build_command_merges_private_worker_labels_via_adapter() -> None:
    adapter = CursorCloudAdapter()
    marker = adapter.build_command(
        "txt",
        extra={
            "repo_url": "https://github.com/o/r",
            "labels": {"team": "infra"},
            "machine_name": "devbox-7",
        },
    )
    payload = json.loads(marker[2])
    assert payload["extra"]["use_private_worker"] is True
    assert payload["extra"]["labels"] == {
        "team": "infra",
        "machine": "devbox-7",
    }


def test_normalize_cloud_extra_rejects_private_worker_label_conflict() -> None:
    with pytest.raises(ValueError, match="worker_name.*conflicts"):
        _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "labels": {"worker": "explicit-worker"},
                "worker_name": "other-worker",
            }
        )


def test_normalize_cloud_extra_rejects_private_worker_disabled_with_labels() -> None:
    with pytest.raises(ValueError, match="use_private_worker=false"):
        _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "use_private_worker": False,
                "labels": {"pool": "popolaloom"},
            }
        )


@pytest.mark.parametrize(
    "extra, match",
    [
        ({"use_private_worker": "yes"}, "use_private_worker must be bool"),
        ({"labels": "pool=popolaloom"}, "labels must be dict"),
        ({"labels": {"pool": 123}}, "labels must be dict\\[str, str\\] only"),
        ({"worker_name": ""}, "worker_name must be a non-empty str"),
    ],
)
def test_normalize_cloud_extra_rejects_private_worker_wrong_types(
    extra: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", **extra})


def test_normalize_cloud_extra_timeout_s_must_be_numeric() -> None:
    with pytest.raises(ValueError, match="timeout_s must be int or float"):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "timeout_s": "60"})


def test_normalize_cloud_extra_timeout_accepts_int_and_float() -> None:
    a = _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "timeout_s": 30})
    b = _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "timeout_s": 30.5})
    assert a["timeout_s"] == 30.0
    assert b["timeout_s"] == 30.5


def test_normalize_cloud_extra_api_key_must_be_str() -> None:
    with pytest.raises(ValueError, match="api_key must be str"):
        _normalize_cloud_extra({"repo_url": "https://github.com/o/r", "api_key": 1})


def test_normalize_cloud_extra_optional_out_fields() -> None:
    repo_only = _normalize_cloud_extra({"repo_url": "https://github.com/a/b"})
    assert "repo_url" in repo_only
    assert "pr_url" not in repo_only
    assert "env_vars" not in repo_only
    assert "api_key" not in repo_only
    assert repo_only["use_private_worker"] is False

    pr_only = _normalize_cloud_extra({"pr_url": "https://github.com/o/r/pull/1"})
    assert "pr_url" in pr_only
    assert "repo_url" not in pr_only

    with_key = _normalize_cloud_extra(
        {
            "repo_url": "https://github.com/a/b",
            "env_vars": {"X": "y"},
            "api_key": "override",
        }
    )
    assert with_key["env_vars"] == {"X": "y"}
    assert with_key["api_key"] == "override"


def test_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key must be non-empty"):
        CloudCursorClient("")


def test_client_context_manager_calls_close(api_key: str) -> None:
    client = CloudCursorClient(api_key)
    real_close = client._client.close
    mock_close = MagicMock(side_effect=real_close)
    client._client.close = mock_close  # type: ignore[method-assign]
    with client:
        pass
    mock_close.assert_called_once()


def test_client_explicit_exit_closes_transport(api_key: str) -> None:
    """Covers ``__exit__`` -> :meth:`~CloudCursorClient.close`."""

    client = CloudCursorClient(api_key)
    real_close = client._client.close
    mock_close = MagicMock(side_effect=real_close)
    client._client.close = mock_close  # type: ignore[method-assign]
    client.__exit__(None, None, None)
    mock_close.assert_called_once()


def test_create_agent_requires_repo_or_pr_url(api_key: str) -> None:
    client = CloudCursorClient(api_key)
    try:
        with pytest.raises(ValueError, match="repo_url or pr_url is required"):
            client.create_agent("p", "composer-2")
    finally:
        client.close()


def test_create_agent_pr_url_branch(router: respx.Router, api_key: str) -> None:
    def check_body(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["repos"] == [{"prUrl": "https://github.com/o/r/pull/2"}]
        return httpx.Response(200, json={"agent": {"id": "a"}, "run": {"id": "r"}})

    router.post("/v1/agents").mock(side_effect=check_body)
    client = CloudCursorClient(api_key)
    try:
        client.create_agent(
            "p",
            "composer-2",
            repo_url=None,
            pr_url="https://github.com/o/r/pull/2",
        )
    finally:
        client.close()


def test_generic_4xx_maps_to_non_retryable_error(router: respx.Router, api_key: str) -> None:
    router.get("/v1/agents/x").mock(return_value=httpx.Response(404, text="missing"))
    client = CloudCursorClient(api_key)
    try:
        with pytest.raises(CursorCloudError) as ei:
            client.get_agent("x")
        assert ei.value.status_code == 404
        assert ei.value.is_retryable is False
        assert isinstance(ei.value, CursorCloudError)
    finally:
        client.close()


def test_request_error_wrapped_retryable(api_key: str) -> None:
    client = CloudCursorClient(api_key)
    try:
        with (
            patch.object(client._client, "request", side_effect=httpx.ConnectError("nope")),
            pytest.raises(CursorCloudError) as ei,
        ):
            client.get_agent("bc-1")
        assert ei.value.is_retryable is True
        assert ei.value.status_code is None
    finally:
        client.close()


def test_empty_json_body_returns_empty_dict(router: respx.Router, api_key: str) -> None:
    router.get("/v1/agents/bc-empty").mock(return_value=httpx.Response(200, content=b""))
    client = CloudCursorClient(api_key)
    try:
        assert client.get_agent("bc-empty") == {}
    finally:
        client.close()


def test_build_command_cwd_none_and_extra_validation_via_adapter() -> None:
    adapter = CursorCloudAdapter()
    marker = adapter.build_command("txt", cwd=None, extra={"repo_url": "https://github.com/o/r"})
    payload = json.loads(marker[2])
    assert payload["cwd"] is None
    assert payload["prompt"] == "txt"


def test_normalize_default_starting_ref_and_model_when_absent() -> None:
    out = _normalize_cloud_extra({"repo_url": "https://github.com/o/r"})
    assert out["starting_ref"] == "main"
    assert out["model"] == "composer-2"
