"""Tests for popolaloom-mcp stdio server (v0.2.0 Stage D).

Coverage targets (≥ 8 cases per the v0.2.0-plan §4 D-tests):

1. ``test_list_tools_exposes_7_verbs`` — :func:`build_tool_list` returns
   exactly 7 :class:`Tool` descriptors with the expected names.
2. ``test_tool_annotations_correct`` — each tool's
   ``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint`` match the
   v0.2.0-plan §4 D2 contract.
3. ``test_popola_submit_routes_to_dispatch`` — calling :func:`call_verb`
   with ``popola_submit`` mocks pushes ``POST /dispatch`` with the right
   body and returns a non-error :class:`CallToolResult`.
4. ``test_popola_list_routes_to_list`` — same pattern for ``GET /list``.
5. ``test_popola_status_routes_to_status`` — bonus, ``GET /status/{id}``.
6. ``test_popola_cancel_routes_to_cancel`` — bonus, ``POST /cancel/{id}``.
7. ``test_popola_cancel_already_terminal_idempotent`` — 409 → success
   ``already_terminal=True`` (idempotent semantics).
8. ``test_daemon_down_returns_clear_error`` — :class:`httpx.ConnectError`
   surfaces as ``isError=True`` with a friendly text matching the
   "popolad not running" message.
9. ``test_popola_supply_feedback_returns_not_implemented`` — verb returns
   ``isError=True`` with a clear "v0.3.0 F4 deferred" message.
10. ``test_popola_inject_subtask_returns_not_implemented`` — same for
    "v0.3.0 F2".
11. ``test_call_verb_unknown_name_returns_error`` — guard against typos.
12. ``test_elicitation_request_schema_form_mode`` —
    :func:`build_elicitation_request` produces an envelope that
    round-trips through :func:`validate_elicitation_request`.
13. ``test_elicitation_invalid_payload_raises`` — empty options + missing
    fields surface as :class:`ValueError` (No Silent Failures).
14. ``test_input_schemas_are_valid_json_schema`` — every tool's
    ``inputSchema`` validates as a JSON Schema (catches typos in
    ``required`` / ``type`` early).
15. ``test_server_module_imports_and_builds`` — :func:`build_server`
    works (no import-side-effect crash; no real socket required).

The httpx client is stubbed with a tiny in-process fake; no real popolad
daemon is required for any of these cases (per workspace rule "Mandatory
Verification" — daemon-mocked unit coverage runs in <1s).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jsonschema
import pytest
from mcp.types import CallToolResult, Tool, ToolAnnotations

from popolaloom.mcp.elicitation import (
    ELICITATION_PAYLOAD_SCHEMA,
    build_elicitation_request,
    validate_elicitation_request,
)
from popolaloom.mcp.server import build_server, make_async_client, socket_path
from popolaloom.mcp.tools import (
    TOOL_DEFINITIONS,
    build_tool_list,
    call_verb,
    popola_cancel,
    popola_inject_subtask,
    popola_list,
    popola_status,
    popola_submit,
    popola_supply_feedback,
)

# ── tiny httpx stub (no responses/respx dep needed) ──────────────────────


class _FakeResponse:
    """Mimic enough of :class:`httpx.Response` for the verb error paths.

    Only ``status_code`` / ``json()`` / ``text`` are hit by the verbs;
    SSE streaming uses the full :class:`httpx.AsyncClient` interface so
    we don't fake those (the attach_stream verb is exercised in the AC
    integration tests rather than these unit tests)."""

    def __init__(self, status_code: int, body: Any | None = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        if self._body is None:
            return ""
        return json.dumps(self._body, ensure_ascii=False)


class _FakeClient:
    """Captures POST/GET calls; returns canned :class:`_FakeResponse`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.next_response: _FakeResponse = _FakeResponse(200, {})

    async def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> _FakeResponse:
        self.calls.append(("POST", url, json))
        return self.next_response

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> _FakeResponse:
        self.calls.append(("GET", url, dict(params) if params is not None else None))
        return self.next_response


class _ConnectErrorClient:
    """Always raises :class:`httpx.ConnectError` (daemon-down simulation)."""

    async def post(self, url: str, **_: Any) -> _FakeResponse:
        raise httpx.ConnectError("connection refused (daemon-down stub)")

    async def get(self, url: str, **_: Any) -> _FakeResponse:
        raise httpx.ConnectError("connection refused (daemon-down stub)")


# ── test 1 + 2: tools/list shape + annotations ───────────────────────────


def test_list_tools_exposes_7_verbs() -> None:
    """build_tool_list() returns the v0.2.0 7 verbs + v0.3.0 F2 3 new verbs.

    v0.3.0 F2 added ``popola_relay`` / ``popola_supervise`` /
    ``popola_federate`` (legacy ``popola_inject_subtask`` kept as alias).
    """
    tools = build_tool_list()
    assert len(tools) == 10, f"expected 10 verbs (7 v0.2.x + 3 v0.3.0 F2), got {len(tools)}"
    names = [t.name for t in tools]
    assert names == [
        "popola_submit",
        "popola_list",
        "popola_status",
        "popola_attach_stream",
        "popola_supply_feedback",
        "popola_cancel",
        "popola_inject_subtask",
        "popola_relay",
        "popola_supervise",
        "popola_federate",
    ]
    for tool in tools:
        assert isinstance(tool, Tool)
        assert tool.description, f"{tool.name} missing description"
        assert tool.inputSchema, f"{tool.name} missing inputSchema"
        assert isinstance(tool.annotations, ToolAnnotations), (
            f"{tool.name} must carry ToolAnnotations"
        )


def test_tool_annotations_correct() -> None:
    """Annotation hints match the v0.2.0-plan §4 D2 contract."""
    by_name = {t.name: t for t in build_tool_list()}

    assert by_name["popola_submit"].annotations.readOnlyHint is False
    assert by_name["popola_submit"].annotations.destructiveHint is False
    assert by_name["popola_submit"].annotations.idempotentHint is False

    assert by_name["popola_list"].annotations.readOnlyHint is True
    assert by_name["popola_list"].annotations.idempotentHint is True

    assert by_name["popola_status"].annotations.readOnlyHint is True
    assert by_name["popola_status"].annotations.idempotentHint is True

    assert by_name["popola_attach_stream"].annotations.readOnlyHint is True

    assert by_name["popola_cancel"].annotations.destructiveHint is True
    assert by_name["popola_cancel"].annotations.idempotentHint is True


# ── test 3: popola_submit routes to POST /dispatch ───────────────────────


@pytest.mark.asyncio
async def test_popola_submit_routes_to_dispatch() -> None:
    """popola_submit issues POST /dispatch with the expected body."""
    client = _FakeClient()
    client.next_response = _FakeResponse(
        200,
        {
            "task_id": "T-abc",
            "events_log": "/tmp/popolaloom/events/T-abc.jsonl",
            "cli": "cursor",
        },
    )

    result = await popola_submit(
        client,  # type: ignore[arg-type]
        {
            "cli": "cursor",
            "prompt": "hello world",
            "cwd": "/tmp/work",
            "extra": {"yolo": True},
        },
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert client.calls == [
        (
            "POST",
            "/dispatch",
            {
                "cli": "cursor",
                "prompt": "hello world",
                "cwd": "/tmp/work",
                "extra": {"yolo": True},
            },
        )
    ]
    text = result.content[0].text  # type: ignore[union-attr]
    parsed = json.loads(text)
    assert parsed["task_id"] == "T-abc"


@pytest.mark.asyncio
async def test_popola_submit_via_call_verb() -> None:
    """call_verb routes 'popola_submit' to popola_submit()."""
    client = _FakeClient()
    client.next_response = _FakeResponse(
        200, {"task_id": "X-1", "events_log": "/x", "cli": "claude"}
    )
    result = await call_verb(
        "popola_submit", {"cli": "claude", "prompt": "p"}, client  # type: ignore[arg-type]
    )
    assert result.isError is False
    assert client.calls[0][1] == "/dispatch"


# ── test 4: popola_list routes to GET /list ──────────────────────────────


@pytest.mark.asyncio
async def test_popola_list_routes_to_list() -> None:
    """popola_list issues GET /list?include_terminal={bool}."""
    client = _FakeClient()
    client.next_response = _FakeResponse(
        200,
        [
            {"task_id": "T-1", "cli": "cursor", "state": "running"},
            {"task_id": "T-2", "cli": "claude", "state": "pending"},
        ],
    )

    result = await popola_list(
        client,  # type: ignore[arg-type]
        {"include_terminal": False},
    )

    assert result.isError is False
    assert client.calls == [("GET", "/list", {"include_terminal": False})]
    parsed = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert len(parsed) == 2
    assert parsed[0]["task_id"] == "T-1"


@pytest.mark.asyncio
async def test_popola_list_default_excludes_terminal() -> None:
    """Default include_terminal is False (matches CLI behavior)."""
    client = _FakeClient()
    client.next_response = _FakeResponse(200, [])
    await popola_list(client, {})  # type: ignore[arg-type]
    assert client.calls == [("GET", "/list", {"include_terminal": False})]


# ── test 5 + 6: status + cancel routing ──────────────────────────────────


@pytest.mark.asyncio
async def test_popola_status_routes_to_status() -> None:
    """popola_status issues GET /status/{task_id}."""
    client = _FakeClient()
    client.next_response = _FakeResponse(
        200,
        {"task_id": "T-9", "state": "running", "pid": 1234, "latest_event_index": 7},
    )

    result = await popola_status(
        client,  # type: ignore[arg-type]
        {"task_id": "T-9"},
    )

    assert result.isError is False
    assert client.calls == [("GET", "/status/T-9", None)]


@pytest.mark.asyncio
async def test_popola_status_404_returns_error() -> None:
    """404 → isError=True with task-not-found text."""
    client = _FakeClient()
    client.next_response = _FakeResponse(404, {"detail": "task not found"})
    result = await popola_status(client, {"task_id": "MISSING"})  # type: ignore[arg-type]
    assert result.isError is True
    assert "MISSING" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_cancel_routes_to_cancel() -> None:
    """popola_cancel issues POST /cancel/{task_id}."""
    client = _FakeClient()
    client.next_response = _FakeResponse(
        200,
        {
            "task_id": "T-X",
            "requested_signal": "SIGTERM",
            "escalated_to_sigkill": False,
            "pid": 4321,
        },
    )

    result = await popola_cancel(
        client,  # type: ignore[arg-type]
        {"task_id": "T-X"},
    )

    assert result.isError is False
    assert client.calls == [("POST", "/cancel/T-X", None)]


@pytest.mark.asyncio
async def test_popola_cancel_already_terminal_idempotent() -> None:
    """Calling cancel twice (409 from daemon) → success+already_terminal=True."""
    client = _FakeClient()
    client.next_response = _FakeResponse(409, {"detail": "task already in terminal state"})
    result = await popola_cancel(client, {"task_id": "T-DONE"})  # type: ignore[arg-type]
    assert result.isError is False, "cancel on terminal task should be idempotent"
    parsed = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert parsed["cancelled"] is True
    assert parsed["already_terminal"] is True


# ── test 7: daemon-down (httpx.ConnectError) ─────────────────────────────


@pytest.mark.asyncio
async def test_daemon_down_returns_clear_error() -> None:
    """httpx.ConnectError → CallToolResult(isError=True, friendly text)."""
    client = _ConnectErrorClient()
    result = await popola_submit(
        client,  # type: ignore[arg-type]
        {"cli": "cursor", "prompt": "hi"},
    )
    assert result.isError is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "popolad not running" in text.lower(), f"got error text: {text!r}"
    assert "popola popolad start" in text


@pytest.mark.asyncio
async def test_daemon_down_get_paths_also_friendly() -> None:
    """Daemon-down on GET-style verbs (popola_list) also returns friendly error."""
    client = _ConnectErrorClient()
    result = await popola_list(client, {})  # type: ignore[arg-type]
    assert result.isError is True
    assert "popolad not running" in result.content[0].text.lower()  # type: ignore[union-attr]


# ── test 8 + 9: deferred verbs return clear NotImplementedError ──────────


@pytest.mark.asyncio
async def test_popola_supply_feedback_returns_not_implemented() -> None:
    """popola_supply_feedback returns isError=True with v0.3.0 F4 message."""
    client = _FakeClient()
    result = await popola_supply_feedback(
        client,  # type: ignore[arg-type]
        {"task_id": "T-1", "value": "yes"},
    )
    assert result.isError is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "v0.3.0 F4" in text
    assert "not implemented" in text.lower()
    # No daemon call should have happened — verb is a stub
    assert client.calls == []


@pytest.mark.asyncio
async def test_popola_inject_subtask_returns_not_implemented() -> None:
    """v0.3.0 F2: popola_inject_subtask is now an alias for popola_relay.

    The legacy verb routes ``parent_task_id`` → ``source_task_id`` and
    ``cli`` → ``target_cli`` and POSTs to /relay (no longer
    NotImplementedError).
    """
    client = _FakeClient()
    client.next_response = _FakeResponse(
        200,
        {"child_task_id": "claude-zzz", "handoff_envelope": {}},
    )
    result = await popola_inject_subtask(
        client,  # type: ignore[arg-type]
        {"parent_task_id": "T-1", "cli": "claude", "prompt": "child task"},
    )
    assert result.isError is False, (
        "inject_subtask now relays to popola_relay; should succeed"
    )
    assert any(
        call[0] == "POST" and call[1] == "/relay" for call in client.calls
    ), f"expected POST /relay; got calls={client.calls}"


# ── test 10: call_verb unknown name guard ────────────────────────────────


@pytest.mark.asyncio
async def test_call_verb_unknown_name_returns_error() -> None:
    """call_verb with bogus name returns isError=True (No Silent Failures)."""
    client = _FakeClient()
    result = await call_verb("popola_does_not_exist", {}, client)  # type: ignore[arg-type]
    assert result.isError is True
    assert "popola_does_not_exist" in result.content[0].text  # type: ignore[union-attr]


# ── test 11 + 12: elicitation builder schema ─────────────────────────────


def test_elicitation_request_schema_form_mode() -> None:
    """build_elicitation_request → valid form-mode envelope."""
    payload = {
        "task_id": "T-1",
        "interrupt_id": "INT-1",
        "message": "Approve plan?",
        "options": ["approve", "reject", "ask_more"],
    }
    envelope = build_elicitation_request(payload)

    assert envelope["method"] == "elicitation/create"
    assert envelope["params"]["mode"] == "form"
    assert envelope["params"]["message"] == "Approve plan?"

    schema = envelope["params"]["requestedSchema"]
    assert schema["type"] == "object"
    assert "choice" in schema["properties"]
    assert schema["properties"]["choice"]["enum"] == [
        "approve",
        "reject",
        "ask_more",
    ]
    assert "reason" in schema["properties"], "default allow_reason=true adds reason"
    assert schema["required"] == ["choice"]
    assert envelope["params"]["_meta"]["task_id"] == "T-1"
    assert envelope["params"]["_meta"]["interrupt_id"] == "INT-1"

    # Round-trip through the strict ElicitRequest pydantic model — proves
    # the envelope is shape-compatible with the SDK's stdio writer.
    elicit = validate_elicitation_request(envelope)
    assert elicit.params.mode == "form"
    assert elicit.params.message == "Approve plan?"


def test_elicitation_disable_reason_field() -> None:
    """allow_reason=False omits the 'reason' field."""
    envelope = build_elicitation_request(
        {
            "task_id": "T-1",
            "message": "yes/no?",
            "options": ["yes", "no"],
            "allow_reason": False,
        }
    )
    schema = envelope["params"]["requestedSchema"]
    assert "reason" not in schema["properties"]
    assert schema["properties"]["choice"]["enum"] == ["yes", "no"]


def test_elicitation_invalid_payload_raises() -> None:
    """Empty options / missing fields raise ValueError (No Silent Failures)."""
    with pytest.raises(ValueError):
        build_elicitation_request({"task_id": "T-1", "options": [], "message": "?"})

    with pytest.raises(ValueError):
        build_elicitation_request({"task_id": "T-1", "options": ["yes"]})

    with pytest.raises(ValueError):
        build_elicitation_request({"message": "?", "options": ["yes"]})


def test_elicitation_payload_schema_self_check() -> None:
    """The exported ELICITATION_PAYLOAD_SCHEMA itself is valid JSON Schema."""
    jsonschema.Draft202012Validator.check_schema(ELICITATION_PAYLOAD_SCHEMA)


# ── test 13: input schemas are valid JSON Schema ─────────────────────────


def test_input_schemas_are_valid_json_schema() -> None:
    """Every Tool's inputSchema is a syntactically valid JSON Schema."""
    for tool_def in TOOL_DEFINITIONS:
        try:
            jsonschema.Draft202012Validator.check_schema(tool_def.input_schema)
        except jsonschema.SchemaError as exc:  # pragma: no cover
            pytest.fail(
                f"{tool_def.name} has malformed inputSchema: {exc.message}"
            )


def test_required_args_are_defined_in_properties() -> None:
    """Sanity: every name in 'required' must appear in 'properties'."""
    for tool_def in TOOL_DEFINITIONS:
        schema = tool_def.input_schema
        properties = set(schema.get("properties", {}).keys())
        required = set(schema.get("required", []))
        missing = required - properties
        assert not missing, (
            f"{tool_def.name}: required field(s) {missing} not in properties"
        )


# ── test 14: server module imports + builds ──────────────────────────────


@pytest.mark.asyncio
async def test_server_module_imports_and_builds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """build_server() works without import-side-effect crashes."""
    fake_uds = tmp_path / "popolad.sock"
    client = make_async_client(uds=fake_uds)
    try:
        server = build_server(client)
        assert server.name == "popolaloom-mcp"
        opts = server.create_initialization_options()
        assert opts.server_name == "popolaloom-mcp"
        assert opts.capabilities.tools is not None, (
            "list_tools handler must register a tools capability"
        )
    finally:
        await client.aclose()


def test_socket_path_respects_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """socket_path() honours $POPOLA_HOME (matches CLI behaviour)."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    sock = socket_path()
    assert sock == tmp_path / "popolad.sock"


# ── test 15: extra coverage — schema fields for deferred verbs ───────────


def test_deferred_verbs_have_documented_schema() -> None:
    """popola_supply_feedback + popola_inject_subtask have v0.3.0 schemas
    occupied (so IDE Agents see the planned contract today)."""
    by_name = {td.name: td for td in TOOL_DEFINITIONS}
    feedback = by_name["popola_supply_feedback"]
    assert "task_id" in feedback.input_schema["properties"]
    assert "value" in feedback.input_schema["properties"]
    assert set(feedback.input_schema["required"]) == {"task_id", "value"}

    inject = by_name["popola_inject_subtask"]
    assert "parent_task_id" in inject.input_schema["properties"]
    assert "cli" in inject.input_schema["properties"]
    assert "prompt" in inject.input_schema["properties"]
    assert set(inject.input_schema["required"]) == {"parent_task_id", "cli"}
