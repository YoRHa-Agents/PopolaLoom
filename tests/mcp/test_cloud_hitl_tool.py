"""Tests for popolaloom_cloud_hitl_request MCP verb (v0.8.7 W2.1 T2.1.1).

Per the AC table (≥ 7 cases):

1. happy path (approve)                                        — covered
2. timeout returns explicit ``error.code: "timeout"``          — covered
3. daemon-unreachable                                          — covered (×2)
4. lark-unreachable surfaces as poll-then-error                — covered
5. replay returns ``deduped: true``                            — covered
6. invalid_context (empty ``question_text``)                   — covered
7. reject-is-not-an-error (``option_id: "reject"`` → success)  — covered

Plus AC (g): ``CURSOR_API_KEY`` MUST never appear in tool I/O — covered
via :func:`test_cursor_api_key_redacted_in_output`. Plus AC (a)
verb-registration check via :func:`test_verb_registered_in_registry`.

The tests use :class:`httpx.MockTransport` to fake the popolad daemon —
no real UDS / popolad process required. Per the workspace rule
"Mandatory Verification" the suite is fast (<1 s) and covers every
error envelope branch in the contract §3.3 enum.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, ToolAnnotations

from popolaloom.mcp.cloud_hitl_tool import (
    API_KEY_REDACTION_PLACEHOLDER,
    CLOUD_HITL_INPUT_SCHEMA,
    CLOUD_HITL_OUTPUT_SCHEMA,
    CLOUD_HITL_TOOL_DEFINITION,
    CLOUD_HITL_TOOL_DEFINITIONS,
    CLOUD_HITL_VERB_NAME,
    DEFAULT_TIMEOUT_S,
    ERROR_CODES,
    IDEMPOTENCY_KEY_HEX_LEN,
    MAX_TIMEOUT_S,
    MIN_TIMEOUT_S,
    _derive_idempotency_key,
    build_extended_handler_map,
    build_extended_tool_list,
    popolaloom_cloud_hitl_request,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Build an :class:`httpx.AsyncClient` backed by a :class:`MockTransport`."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://popolad")


def _parse_text(result: CallToolResult) -> dict[str, Any]:
    """Return the JSON-parsed first ``TextContent`` from ``result.content``."""
    text_block = result.content[0]
    text = getattr(text_block, "text", None)
    assert isinstance(text, str), f"expected TextContent.text str, got {text_block!r}"
    return dict(json.loads(text))


_REQUIRED_INPUT: dict[str, Any] = {
    "task_id": "T-abc",
    "agent_id": "bc-agent",
    "run_id": "r1",
    "question_text": "Approve deploy?",
}


# ── 1: registry / contract surface (AC a) ────────────────────────────────


def test_verb_registered_in_registry() -> None:
    """AC (a): verb is registered in the equivalent registry with correct schema."""
    assert len(CLOUD_HITL_TOOL_DEFINITIONS) == 1
    td = CLOUD_HITL_TOOL_DEFINITIONS[0]
    assert td is CLOUD_HITL_TOOL_DEFINITION
    assert td.name == CLOUD_HITL_VERB_NAME == "popolaloom_cloud_hitl_request"
    assert td.handler is popolaloom_cloud_hitl_request
    assert isinstance(td.annotations, ToolAnnotations)
    assert td.annotations.idempotentHint is True
    assert td.annotations.openWorldHint is True
    assert td.annotations.readOnlyHint is False
    assert td.annotations.destructiveHint is False

    schema = td.input_schema
    assert schema is CLOUD_HITL_INPUT_SCHEMA
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["task_id", "agent_id", "run_id", "question_text"]
    props = schema["properties"]
    for required in ("task_id", "agent_id", "run_id", "question_text"):
        assert props[required]["type"] == "string"
        assert props[required]["minLength"] == 1
    assert props["question_text"]["maxLength"] == 4000
    assert props["context_summary"]["maxLength"] == 8000
    assert props["timeout_s"]["minimum"] == MIN_TIMEOUT_S == 60
    assert props["timeout_s"]["maximum"] == MAX_TIMEOUT_S == 86400
    assert props["timeout_s"]["default"] == DEFAULT_TIMEOUT_S == 1800
    assert props["idempotency_key"]["maxLength"] == 128
    assert props["options"]["minItems"] == 2

    assert set(ERROR_CODES) == {
        "timeout",
        "cancelled",
        "invalid_context",
        "lark_unreachable",
        "daemon_unreachable",
        "internal",
    }
    assert "deduped" in CLOUD_HITL_OUTPUT_SCHEMA["properties"]


def test_build_extended_tool_list_appends_cloud_hitl_verb() -> None:
    """The extended tool list adds the new verb to the existing 10."""
    tools = build_extended_tool_list()
    names = [t.name for t in tools]
    assert "popolaloom_cloud_hitl_request" in names
    assert names.count("popolaloom_cloud_hitl_request") == 1
    handlers = build_extended_handler_map()
    assert handlers["popolaloom_cloud_hitl_request"] is popolaloom_cloud_hitl_request


def test_idempotency_key_is_sha256_truncated() -> None:
    """AC (d): auto-derived key is the 32-hex-char prefix of the sha256 digest."""
    key = _derive_idempotency_key("T-1", "ag-1", "r-1", "Q?")
    expected = hashlib.sha256(b"T-1|ag-1|r-1|Q?").hexdigest()[:IDEMPOTENCY_KEY_HEX_LEN]
    assert key == expected
    assert len(key) == IDEMPOTENCY_KEY_HEX_LEN
    other = _derive_idempotency_key("T-1", "ag-1", "r-1", "Q!")
    assert key != other, "different question_text MUST produce a different digest"


# ── 2: happy path approve (AC f.1) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_approve_returns_success() -> None:
    """AC (f.1) + (b): full happy path including wire-mapping verification."""
    posts: list[dict[str, Any]] = []
    gets: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/hitl/cloud/request":
            posts.append(json.loads(req.content))
            return httpx.Response(
                200,
                json={
                    "hitl_id": "h-001",
                    "status": "pending",
                    "deadline_at": "2026-05-08T12:00:00+00:00",
                    "lark_dispatched": True,
                    "deduped": False,
                },
            )
        if req.method == "GET" and req.url.path == "/hitl/cloud/wait/h-001":
            gets.append(str(req.url))
            return httpx.Response(
                200,
                json={
                    "hitl_id": "h-001",
                    "status": "answered",
                    "answer": {
                        "option_id": "approve",
                        "reason": None,
                        "responder_id": "ou_user_42",
                        "channel": "lark",
                    },
                },
            )
        return httpx.Response(500, json={"detail": f"unexpected {req.method} {req.url.path}"})

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    payload = _parse_text(result)
    assert payload["hitl_id"] == "h-001"
    assert payload["option_id"] == "approve"
    assert payload["answer"] == "approve"
    assert payload["answered_by"] == "ou_user_42"
    assert payload["channel"] == "lark"
    assert payload["deduped"] is False
    assert "answered_at" in payload and isinstance(payload["answered_at"], str)

    assert len(posts) == 1
    sent = posts[0]
    assert sent["task_id"] == "T-abc"
    assert sent["cursor_agent_id"] == "bc-agent"
    assert sent["cursor_run_id"] == "r1"
    assert sent["prompt_body"] == "Approve deploy?"
    assert sent["prompt_title"] == "PopolaLoom HITL — task: T-abc"
    expected_key = _derive_idempotency_key(
        "T-abc", "bc-agent", "r1", "Approve deploy?"
    )
    assert sent["metadata"]["idempotency_key"] == expected_key
    assert sent["timeout_s"] == float(DEFAULT_TIMEOUT_S)
    option_ids = {o["id"] for o in sent["options"]}
    assert option_ids == {"approve", "reject", "custom"}
    assert any("/hitl/cloud/wait/h-001" in url for url in gets)


# ── 3: reject is not an error (AC f.7) ───────────────────────────────────


@pytest.mark.asyncio
async def test_reject_is_not_an_error() -> None:
    """AC (f.7): ``option_id="reject"`` returns success (NOT an error envelope)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "hitl_id": "h-r",
                    "status": "pending",
                    "deadline_at": "x",
                    "lark_dispatched": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "hitl_id": "h-r",
                "status": "answered",
                "answer": {
                    "option_id": "reject",
                    "reason": "not safe yet",
                    "responder_id": "ou_user_5",
                    "channel": "lark",
                },
            },
        )

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is False, "rejection MUST NOT be reported as a tool error"
    payload = _parse_text(result)
    assert payload["option_id"] == "reject"
    assert payload["answer"] == "reject: not safe yet"


# ── 4: timeout returns explicit error.code: "timeout" (AC f.2) ───────────


@pytest.mark.asyncio
async def test_timeout_returns_explicit_error_envelope() -> None:
    """AC (f.2): daemon ``status: "timeout"`` → error envelope with code=timeout."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "hitl_id": "h-to",
                    "status": "pending",
                    "deadline_at": "x",
                    "lark_dispatched": True,
                },
            )
        return httpx.Response(
            200, json={"hitl_id": "h-to", "status": "timeout", "answer": None}
        )

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is True
    payload = _parse_text(result)
    assert payload["error"]["code"] == "timeout"
    assert payload["error"]["hitl_id"] == "h-to"
    assert "timed out" in payload["error"]["message"].lower()


# ── 5: lark-unreachable surfaces as poll-then-error (AC f.4) ─────────────


@pytest.mark.asyncio
async def test_lark_unreachable_after_poll_timeout() -> None:
    """AC (f.4): when ``lark_dispatched=false`` at request time, a wait timeout
    surfaces as ``lark_unreachable`` (not bare ``timeout``) per contract §7 row 4.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "hitl_id": "h-lk",
                    "status": "pending",
                    "deadline_at": "x",
                    "lark_dispatched": False,
                },
            )
        return httpx.Response(
            200, json={"hitl_id": "h-lk", "status": "timeout", "answer": None}
        )

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is True
    payload = _parse_text(result)
    assert payload["error"]["code"] == "lark_unreachable"
    assert payload["error"]["hitl_id"] == "h-lk"
    assert payload["error"]["retry_after_s"] == 60


# ── 6: replay returns deduped: true (AC f.5) ─────────────────────────────


@pytest.mark.asyncio
async def test_replay_returns_deduped_true() -> None:
    """AC (f.5): when daemon returns ``deduped: true``, success carries it through."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "hitl_id": "h-dup",
                    "status": "pending",
                    "deadline_at": "x",
                    "deduped": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "hitl_id": "h-dup",
                "status": "answered",
                "answer": {
                    "option_id": "approve",
                    "reason": None,
                    "responder_id": "ou_user_first",
                    "channel": "lark",
                },
            },
        )

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is False
    payload = _parse_text(result)
    assert payload["deduped"] is True
    assert payload["hitl_id"] == "h-dup"


# ── 7: daemon-unreachable (AC f.3) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_unreachable_connect_error() -> None:
    """AC (f.3): :class:`httpx.ConnectError` surfaces as ``daemon_unreachable``."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused (test stub)")

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is True
    payload = _parse_text(result)
    assert payload["error"]["code"] == "daemon_unreachable"
    assert payload["error"]["retry_after_s"] == 60
    assert "popolad not running" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_daemon_unreachable_5xx() -> None:
    """5xx from popolad → ``daemon_unreachable`` envelope."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "HITL store not wired up"})

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is True
    payload = _parse_text(result)
    assert payload["error"]["code"] == "daemon_unreachable"


# ── 8: invalid_context (AC f.6) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_context_empty_question_text() -> None:
    """AC (f.6): empty ``question_text`` → invalid_context, no daemon call made."""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"hitl_id": "x"})

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(
            client, {**_REQUIRED_INPUT, "question_text": ""}
        )

    assert result.isError is True
    payload = _parse_text(result)
    assert payload["error"]["code"] == "invalid_context"
    assert calls == [], "no daemon call should have been made"
    assert "hitl_id" not in payload["error"]


@pytest.mark.asyncio
async def test_invalid_context_timeout_below_min() -> None:
    """timeout_s < 60 → invalid_context (caller bug; no daemon call)."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("daemon must not be called for invalid_context")

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(
            client, {**_REQUIRED_INPUT, "timeout_s": 30}
        )

    assert result.isError is True
    assert _parse_text(result)["error"]["code"] == "invalid_context"


# ── 9: cancelled status → cancelled envelope ─────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_returns_cancelled_envelope() -> None:
    """daemon ``status: "cancelled"`` → error envelope with code=cancelled."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                200, json={"hitl_id": "h-c", "status": "pending", "deadline_at": "x"}
            )
        return httpx.Response(
            200, json={"hitl_id": "h-c", "status": "cancelled", "answer": None}
        )

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is True
    payload = _parse_text(result)
    assert payload["error"]["code"] == "cancelled"
    assert payload["error"]["hitl_id"] == "h-c"


# ── 10: CURSOR_API_KEY runtime guard (AC g) ──────────────────────────────


@pytest.mark.asyncio
async def test_cursor_api_key_redacted_in_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC (g) + SECURITY S1: ``CURSOR_API_KEY`` MUST never appear in tool I/O.

    Defense-in-depth runtime guard test: simulate an upstream daemon error
    message that *leaks* the literal env-var value, and assert that the
    MCP tool's error envelope text does NOT contain it (the redaction
    placeholder appears instead).
    """
    secret_key = "key_abc_super_secret_12345"
    monkeypatch.setenv("CURSOR_API_KEY", secret_key)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=f"failure: stale token = {secret_key} found in upstream",
        )

    async with _make_client(handler) as client:
        result = await popolaloom_cloud_hitl_request(client, dict(_REQUIRED_INPUT))

    assert result.isError is True
    raw_text = _parse_text(result)
    raw_str = json.dumps(raw_text, ensure_ascii=False)
    assert secret_key not in raw_str, (
        "CURSOR_API_KEY MUST NOT appear in tool output (S1 defense-in-depth)"
    )
    assert API_KEY_REDACTION_PLACEHOLDER in raw_str
