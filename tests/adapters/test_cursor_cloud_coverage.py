"""Gap-filler coverage for :mod:`popolaloom.adapters.cursor_cloud` (v0.8.5 CI gate).

Branches are mostly validation in :func:`_normalize_cloud_extra`, HTTP mapping,
``CloudCursorClient`` edges, and empty JSON bodies — cheap pure / mocked tests.

v0.10.0 Wave D2 (DECISIONS Q-2 / Q-11): the ``_normalize_cloud_extra``
output dict no longer carries ``use_private_worker`` / ``labels`` keys —
the adapter routes via ``env: {type, name?}`` instead. Tests that pinned
the v0.9.x output keys have been flipped to assert the new ``env`` shape.
The default model fallback is now ``"default"`` (NOT ``"composer-2"``)
per research/02-path-1-visibility-probe.md §1 L70-77.
"""

from __future__ import annotations

import base64
import json
import warnings
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


def test_normalize_cloud_extra_worker_name_translates_to_env_machine() -> None:
    """v0.10.0 (AC4 a): ``worker_name`` extra translates to ``env={type:"machine", name:X}``.

    Previously this test asserted the v0.9.x output keys
    (``use_private_worker=True`` and ``labels={worker, pool}``); both
    keys are now removed from the marker payload. The new contract is
    that ``worker_name`` lands in ``out["env"]`` as a typed
    :class:`AgentEnv` discriminated union — and conflicts with
    ``pool_name`` per the new mutual-exclusion rule (one routing shape
    per dispatch).
    """
    with pytest.warns(DeprecationWarning, match=r"deprecated"):
        out = _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "worker_name": "ci-worker-1",
            }
        )
    assert out["env"] == {"type": "machine", "name": "ci-worker-1"}
    assert "use_private_worker" not in out
    assert "labels" not in out


def test_normalize_cloud_extra_worker_name_with_pool_name_conflicts() -> None:
    """v0.10.0 (AC4 a sibling): ``worker_name`` + ``pool_name`` is mutually exclusive.

    The v0.9.x output co-emitted both ``labels.worker`` and ``labels.pool``;
    v0.10.0 picks ONE env-type per dispatch and raises ``ValueError`` on
    the conflict per Q-2's "single env discriminator" invariant.
    """
    with pytest.raises(ValueError, match=r"pool_name is mutually exclusive"):
        _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "worker_name": "ci-worker-1",
                "pool_name": "popolaloom",
            }
        )


def test_build_command_merges_machine_via_adapter_emits_env_field() -> None:
    """v0.10.0 (AC4 a via adapter): ``labels.machine`` + ``machine_name`` extras
    translate to ``env={type:"machine", name:X}`` on the marker payload.

    The previous v0.9.x assertion was that the marker had ``use_private_worker:true``
    and ``labels: {team, machine}``. v0.10.0 collapses both into the
    single ``env`` field; ``labels`` is no longer part of the marker grammar.
    """
    adapter = CursorCloudAdapter()
    with pytest.warns(DeprecationWarning, match=r"deprecated"):
        marker = adapter.build_command(
            "txt",
            extra={
                "repo_url": "https://github.com/o/r",
                "machine_name": "devbox-7",
            },
        )
    payload = json.loads(marker[2])
    assert payload["extra"]["env"] == {"type": "machine", "name": "devbox-7"}
    assert "use_private_worker" not in payload["extra"]
    assert "labels" not in payload["extra"]


def test_normalize_cloud_extra_rejects_private_worker_label_conflict() -> None:
    """v0.10.0 (AC4 c): conflict detection still raises ValueError.

    The output dict no longer has ``use_private_worker``/``labels`` keys
    (those were the v0.9.x marker shape) but the conflict-detection
    semantics carry forward verbatim — when ``labels.worker`` and
    ``worker_name`` carry different values, ``_normalize_cloud_extra``
    must raise ``ValueError``. This protects the operator from typo'd
    routing combinations regardless of which legacy alias they used.
    """
    with pytest.raises(ValueError, match=r"conflicts with worker_name"):
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
    """v0.10.0 (AC4 a): the marker payload no longer carries
    ``use_private_worker`` / ``labels`` — both are translated into the
    new ``env`` slot. When NO routing knob is set, ``env`` is also
    omitted and the gateway defaults to ``{type:"cloud"}``.
    """
    repo_only = _normalize_cloud_extra({"repo_url": "https://github.com/a/b"})
    assert "repo_url" in repo_only
    assert "pr_url" not in repo_only
    assert "env_vars" not in repo_only
    assert "api_key" not in repo_only
    # v0.10.0: legacy keys are NEVER emitted on the marker payload.
    assert "use_private_worker" not in repo_only
    assert "labels" not in repo_only
    # v0.10.0: ``env`` is omitted when no routing knob is set (the
    # gateway interprets a missing ``env`` as ``{type:"cloud"}``).
    assert "env" not in repo_only

    pr_only = _normalize_cloud_extra({"pr_url": "https://github.com/o/r/pull/1"})
    assert "pr_url" in pr_only
    assert "repo_url" not in pr_only
    assert "env" not in pr_only

    with_key = _normalize_cloud_extra(
        {
            "repo_url": "https://github.com/a/b",
            "env_vars": {"X": "y"},
            "api_key": "override",
        }
    )
    assert with_key["env_vars"] == {"X": "y"}
    assert with_key["api_key"] == "override"
    assert "env" not in with_key


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
    """v0.10.0 (AC4 b): default model fallback is ``"default"`` (NOT ``"composer-2"``).

    Per ``research/02-path-1-visibility-probe.md`` §1 L70-77, the v0.10.0
    default lets Cursor pick the recommended model for the user's plan
    rather than pinning popola to a specific composer version that may
    rotate. ``starting_ref`` default ``"main"`` is unchanged.
    """
    out = _normalize_cloud_extra({"repo_url": "https://github.com/o/r"})
    assert out["starting_ref"] == "main"
    assert out["model"] == "default"
    # Belt-and-suspenders: explicitly assert the v0.9.x default is gone.
    assert out["model"] != "composer-2"


# ---------------------------------------------------------------------------
# v0.10.0 — additional ``_normalize_cloud_extra`` coverage for the new
# ``cloud_target`` knob and ``env`` output shape (DECISIONS Q-2 / Q-6).
# ---------------------------------------------------------------------------


def test_normalize_cloud_extra_cloud_target_self_hosted_requires_worker() -> None:
    """v0.10.0: ``cloud_target="self-hosted"`` without ``worker_name`` raises.

    Per Q-7 (no-fallback contract): self-hosted dispatch must explicitly
    name the target worker; missing-name is a hard failure rather than
    a silent fallback to a registered default.
    """
    with pytest.raises(ValueError, match=r"requires a worker name"):
        _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "cloud_target": "self-hosted",
            }
        )


def test_normalize_cloud_extra_cloud_target_cursor_managed_rejects_worker_name() -> None:
    """v0.10.0: ``cloud_target="cursor-managed"`` is mutually exclusive with worker_name.

    Per Q-2: ``cursor-managed`` routes to the Cursor cloud VM; setting a
    self-hosted-routing knob alongside it is a configuration bug.
    """
    with pytest.raises(ValueError, match=r"mutually exclusive"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _normalize_cloud_extra(
                {
                    "repo_url": "https://github.com/o/r",
                    "cloud_target": "cursor-managed",
                    "worker_name": "ci-worker-1",
                }
            )


def test_normalize_cloud_extra_cloud_target_ask_each_time_rejected() -> None:
    """v0.10.0: ``cloud_target="ask-each-time"`` is rejected at adapter time.

    Per Q-6: ``ask-each-time`` is only valid as a stored default on
    ``[user_preferences]``; the CLI must resolve it to a concrete value
    before invoking the adapter, so the adapter sees only resolved targets.
    """
    with pytest.raises(ValueError, match=r"ask-each-time.*default"):
        _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "cloud_target": "ask-each-time",
            }
        )


def test_normalize_cloud_extra_cloud_target_invalid_value_rejected() -> None:
    """v0.10.0: ``cloud_target`` outside the valid set raises a precise error."""
    with pytest.raises(ValueError, match=r"cloud_target must be one of"):
        _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "cloud_target": "self-hostd",  # typo
            }
        )


def test_normalize_cloud_extra_cloud_target_self_hosted_with_worker_emits_env() -> None:
    """v0.10.0: full happy path — ``cloud_target=self-hosted`` + ``worker_name=X``."""
    with pytest.warns(DeprecationWarning, match=r"deprecated"):
        out = _normalize_cloud_extra(
            {
                "repo_url": "https://github.com/o/r",
                "cloud_target": "self-hosted",
                "worker_name": "probe-w1",
            }
        )
    assert out["env"] == {"type": "machine", "name": "probe-w1"}
    assert out["cloud_target"] == "self-hosted"
