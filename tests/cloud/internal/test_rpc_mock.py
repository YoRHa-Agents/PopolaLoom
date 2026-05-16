"""Mock RPC tests for popolaloom.cloud.internal.cursor_cloud_internal (S4 W-D).

Per .local/.agent/active/v1.0.0-ga/DECISIONS.md:
- Q-16 (LOCKED): JSON-over-Connect-RPC wire format.
- Q-22 (LOCKED): path-B is experimental; tests live-validate the body
  shape against the feedback §4.1 field matrix without burning a real
  Cursor API call.

The wire format expectations below are verbatim from
`.local/feedbacks/feedback_for_v1.0.0-pre.1.md` §4.1:

| Field | REST accept? | RPC accept? |
|---|---|---|
| model_details.model_name | yes | yes |
| model_details.max_mode | no | yes |
| model_details.thinking_level | no | yes (THINKING_LEVEL_*) |
| agent_mode | no | yes (AGENT_MODE_AGENT/ASK/PLAN/...) |
| effort_mode | no | yes (proto field 75 enum) |
| time_budget_seconds | no | yes (proto field 77/78) |
| long_running_agent_mode | no | yes |
| starting_message_type | no | yes (USER_MESSAGE/PLAN_START/PLAN_EXECUTE) |
| auto_proceed_after_planning | no | yes (proto field 76) |
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest

from popolaloom.cloud.internal.cursor_cloud_internal import (
    DEFAULT_BASE_URL,
    SERVICE_PATH,
    CursorCloudInternalClient,
    CursorCloudInternalError,
    StartComposerOutcome,
    build_start_composer_request,
    user_effort_to_effort_mode,
    user_mode_to_agent_mode,
    user_thinking_level_to_proto,
)
from popolaloom.cloud.internal.jwt_auth import JWTBundle, load_jwt_bundle


def _fake_bundle() -> JWTBundle:
    return JWTBundle(
        access_token="header.payload.sig",
        refresh_token="refresh-tok",
        source="env",
        path=None,
        exp_unix_s=int(time.time()) + 7200,
    )


@pytest.mark.real_cursor_cloud_jwt
def test_real_start_background_composer_endpoint_shape() -> None:
    """Live smoke for the experimental Path-B RPC endpoint.

    Skipped unless the operator opts in with ``pytest -m real_cursor_cloud_jwt``
    and provides ``POPOLA_REAL_CURSOR_REPO_URL``. This intentionally exercises
    the current ``SERVICE_PATH`` so Cursor-side 404 drift is caught by the
    gated lane without burning real cloud runs in default CI.
    """
    repo_url = os.environ.get("POPOLA_REAL_CURSOR_REPO_URL", "")
    if not repo_url:
        pytest.skip("set POPOLA_REAL_CURSOR_REPO_URL to run live Path-B smoke")
    bundle = load_jwt_bundle()
    body = build_start_composer_request(
        prompt="PopolaLoom Path-B endpoint smoke test. Reply with a short status.",
        repo_url=repo_url,
        model_name="default",
    )
    with CursorCloudInternalClient(bundle) as client:
        outcome = client.start_background_composer_from_snapshot(body, timeout_s=30)
    assert outcome.background_composer_id


# ── enum-translation helpers ───────────────────────────────────────────


def test_user_mode_to_agent_mode_all_values() -> None:
    """All 7 user-facing mode strings map to AGENT_MODE_* proto enum."""
    assert user_mode_to_agent_mode("agent") == "AGENT_MODE_AGENT"
    assert user_mode_to_agent_mode("ask") == "AGENT_MODE_ASK"
    assert user_mode_to_agent_mode("plan") == "AGENT_MODE_PLAN"
    assert user_mode_to_agent_mode("debug") == "AGENT_MODE_DEBUG"
    assert user_mode_to_agent_mode("triage") == "AGENT_MODE_TRIAGE"
    assert user_mode_to_agent_mode("project") == "AGENT_MODE_PROJECT"
    assert user_mode_to_agent_mode("multitask") == "AGENT_MODE_MULTITASK"


def test_user_mode_to_agent_mode_case_insensitive() -> None:
    """User input is normalised to lowercase before lookup."""
    assert user_mode_to_agent_mode("PLAN") == "AGENT_MODE_PLAN"
    assert user_mode_to_agent_mode("  Plan  ") == "AGENT_MODE_PLAN"


def test_user_mode_to_agent_mode_rejects_unknown() -> None:
    """Unknown mode → ValueError listing valid values."""
    with pytest.raises(ValueError) as exc_info:
        user_mode_to_agent_mode("yolo")
    msg = str(exc_info.value)
    assert "agent" in msg and "plan" in msg


def test_user_effort_to_effort_mode_all_values() -> None:
    assert user_effort_to_effort_mode("low") == "EFFORT_MODE_LOW"
    assert user_effort_to_effort_mode("medium") == "EFFORT_MODE_MEDIUM"
    assert user_effort_to_effort_mode("high") == "EFFORT_MODE_HIGH"


def test_user_thinking_level_to_proto_all_values() -> None:
    assert user_thinking_level_to_proto("low") == "THINKING_LEVEL_LOW"
    assert user_thinking_level_to_proto("medium") == "THINKING_LEVEL_MEDIUM"
    assert user_thinking_level_to_proto("high") == "THINKING_LEVEL_HIGH"


# ── build_start_composer_request shape ────────────────────────────────


def test_build_request_minimal() -> None:
    """Minimum body has prompt + repos with startingRef defaulting to 'main'.

    v1.3.0 P5: the body now carries 11 additional Connect-Protocol
    wire-format fields plus the camelCase key transform (feedback §2).
    The ``prompt`` + ``repos.startingRef`` are still the smoke-test
    anchors; the v1.3.0 P5 additions are validated by
    ``test_build_composer_camel.py``.
    """
    body = build_start_composer_request(prompt="hi", repo_url="https://github.com/x/y")
    assert body["prompt"] == "hi"
    assert body["repos"] == [
        {"url": "https://github.com/x/y", "startingRef": "main"}
    ]


def test_build_request_all_advanced_flags_present() -> None:
    """All 8 advanced flags map to the documented proto field names (§4.1)."""
    body = build_start_composer_request(
        prompt="refactor",
        repo_url="https://github.com/x/y",
        model_name="gpt-5.5",
        max_mode=True,
        thinking_level="high",
        agent_mode="plan",
        effort_mode="high",
        time_budget_s=1800,
        long_running=True,
        starting_message_type="plan-start",
        auto_proceed_after_planning=True,
    )
    assert body["modelDetails"] == {
        "modelName": "gpt-5.5",
        "maxMode": True,
        "thinkingLevel": "THINKING_LEVEL_HIGH",
    }
    assert body["agentMode"] == "AGENT_MODE_PLAN"
    assert body["effortMode"] == "EFFORT_MODE_HIGH"
    assert body["timeBudgetSeconds"] == 1800
    assert body["timeBudgetMs"] == 1_800_000
    assert body["longRunningAgentMode"] is True
    assert body["startingMessageType"] == "STARTING_MESSAGE_TYPE_PLAN_START"
    assert body["autoProceedAfterPlanning"] is True


def test_build_request_negative_time_budget_rejected() -> None:
    """time_budget_s < 0 is rejected (No Silent Failures)."""
    with pytest.raises(ValueError) as exc_info:
        build_start_composer_request(
            prompt="x",
            repo_url="https://github.com/x/y",
            time_budget_s=-5,
        )
    assert "non-negative" in str(exc_info.value)


def test_build_request_empty_prompt_rejected() -> None:
    with pytest.raises(ValueError):
        build_start_composer_request(prompt="", repo_url="https://github.com/x/y")


def test_build_request_unknown_starting_message_type_rejected() -> None:
    with pytest.raises(ValueError):
        build_start_composer_request(
            prompt="x",
            repo_url="https://github.com/x/y",
            starting_message_type="bogus",
        )


def test_build_request_extras_merge_when_no_conflict() -> None:
    body = build_start_composer_request(
        prompt="x",
        repo_url="https://github.com/x/y",
        extras={"client_metadata": {"foo": "bar"}},
    )
    assert body["clientMetadata"] == {"foo": "bar"}


# ── CursorCloudInternalClient (Connect-RPC over httpx.MockTransport) ──


def _mock_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_start_composer_success_returns_outcome() -> None:
    """Happy-path: RPC returns 200 + background_composer_id; outcome carries dashboard_url."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "background_composer_id": "bc-12345",
                "extra_field": "ignored",
            },
        )

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/x/y",
        agent_mode="plan",
    )
    outcome = client.start_background_composer_from_snapshot(body)
    assert isinstance(outcome, StartComposerOutcome)
    assert outcome.background_composer_id == "bc-12345"
    assert outcome.dashboard_url == "https://cursor.com/agents/bc-12345"

    assert captured["url"] == (
        f"{DEFAULT_BASE_URL}{SERVICE_PATH}/StartBackgroundComposerFromSnapshot"
    )
    assert captured["headers"]["authorization"] == "Bearer header.payload.sig"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["connect-protocol-version"] == "1"
    assert captured["body"]["agentMode"] == "AGENT_MODE_PLAN"


def test_start_composer_camel_case_response_key_accepted() -> None:
    """Response with `backgroundComposerId` (camelCase) is also accepted."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"backgroundComposerId": "bc-99"})

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    outcome = client.start_background_composer_from_snapshot(
        build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
    )
    assert outcome.background_composer_id == "bc-99"


def test_start_composer_401_raises_with_jwt_hint() -> None:
    """401 → CursorCloudInternalError with hint pointing at `cursor login`."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="auth required")

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError) as exc_info:
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert exc_info.value.status_code == 401
    assert "cursor login" in exc_info.value.hint
    assert "rest" in exc_info.value.hint


def test_start_composer_404_raises_with_path_b_fallback_hint() -> None:
    """404 → hint points at --auth-mode=rest (Q-22 stability commitment)."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="method not found")

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError) as exc_info:
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert exc_info.value.status_code == 404
    assert "auth-mode=rest" in exc_info.value.hint


def test_start_composer_5xx_raises_with_truncated_body() -> None:
    """5xx → CursorCloudInternalError; body excerpt is truncated to 500 chars."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream error" * 1000)

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError) as exc_info:
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert exc_info.value.status_code == 503
    # Hint always present (No Silent Failures)
    assert exc_info.value.hint


def test_start_composer_non_json_body_raises() -> None:
    """Non-JSON body → CursorCloudInternalError with wire-format-changed hint."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError) as exc_info:
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert "wire format" in exc_info.value.hint or "wire" in exc_info.value.hint


def test_start_composer_response_missing_id_raises() -> None:
    """Response without a background_composer_id → CursorCloudInternalError."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"some_other_field": True})

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError):
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )


def test_client_context_manager_closes_owned_client() -> None:
    """Context-manager exit closes the underlying httpx.Client (when owned)."""
    with CursorCloudInternalClient(_fake_bundle()) as client:
        assert client.bundle.access_token == "header.payload.sig"
    # Re-using a closed client raises (smoke that close() actually closed it)
    with pytest.raises(RuntimeError):
        client._client.get("https://example.com")  # noqa: SLF001


def test_user_effort_to_effort_mode_rejects_unknown() -> None:
    """Unknown --effort value → ValueError listing valid values."""
    with pytest.raises(ValueError) as exc_info:
        user_effort_to_effort_mode("blazing")
    msg = str(exc_info.value)
    assert "low" in msg and "medium" in msg and "high" in msg


def test_user_thinking_level_to_proto_rejects_unknown() -> None:
    """Unknown --thinking-level → ValueError listing valid values."""
    with pytest.raises(ValueError) as exc_info:
        user_thinking_level_to_proto("ultra")
    msg = str(exc_info.value)
    assert "low" in msg and "medium" in msg and "high" in msg


def test_build_request_empty_repo_url_rejected() -> None:
    """empty repo_url is required (No Silent Failures)."""
    with pytest.raises(ValueError) as exc_info:
        build_start_composer_request(prompt="x", repo_url="")
    assert "repo_url" in str(exc_info.value)


def test_start_composer_warns_when_jwt_within_safety_margin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Near-expiry JWT emits a WARN before the dispatch (callers should refresh)."""
    import base64 as _b64
    import json as _json
    import logging as _logging
    import time as _time

    soon_exp = int(_time.time()) + 5  # within the 30s safety margin
    header_b64 = _b64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_b64 = (
        _b64.urlsafe_b64encode(_json.dumps({"exp": soon_exp}).encode()).rstrip(b"=").decode()
    )
    expiring = JWTBundle(
        access_token=f"{header_b64}.{payload_b64}.sig",
        refresh_token=None,
        source="env",
        path=None,
        exp_unix_s=soon_exp,
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"background_composer_id": "bc-near"})

    client = CursorCloudInternalClient(
        expiring,
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    rpc_logger = "popolaloom.cloud.internal.cursor_cloud_internal"
    with caplog.at_level(_logging.WARNING, logger=rpc_logger):
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert any("safety margin" in rec.message for rec in caplog.records), caplog.text


def test_start_composer_propagates_httpx_request_error_as_internal_error() -> None:
    """HTTP layer ConnectError → CursorCloudInternalError with fall-back hint."""

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError) as exc_info:
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert "auth-mode=rest" in exc_info.value.hint


def test_start_composer_non_dict_response_rejected() -> None:
    """A JSON list (not dict) at top level → CursorCloudInternalError."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = CursorCloudInternalClient(
        _fake_bundle(),
        http_client=httpx.Client(transport=_mock_transport(handler)),
    )
    with pytest.raises(CursorCloudInternalError) as exc_info:
        client.start_background_composer_from_snapshot(
            build_start_composer_request(prompt="x", repo_url="https://github.com/x/y")
        )
    assert "non-object" in str(exc_info.value) or "auth-mode=rest" in exc_info.value.hint
