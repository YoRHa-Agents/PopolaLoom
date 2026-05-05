"""popolaloom-mcp 7 dispatch verbs (v0.2.0 Stage D D2).

Each verb maps an MCP ``tools/call`` invocation to a popolad RPC call over
the Unix Domain Socket — the MCP server reuses the same httpx UDS pattern
as the popola CLI (Stage A's ``cli/main.py``). Verbs are registered as
:class:`mcp.types.Tool` descriptors with full inputSchemas + annotations
(per MCP spec §"Tool Annotations") so IDE Agents (Cursor / Claude IDE)
can reason about read-only / idempotent / destructive characteristics.

The 7 verbs (per spec §3.2 row "popolaloom-mcp" + v0.2.0-plan §4 D2):

1. ``popola_submit``           — POST /dispatch
2. ``popola_list``             — GET  /list?include_terminal=...
3. ``popola_status``           — GET  /status/{task_id}
4. ``popola_attach_stream``    — GET  /attach_stream/{task_id} (snapshot)
5. ``popola_supply_feedback``  — *deferred* to v0.3.0 F4 (HITL resume)
6. ``popola_cancel``           — POST /cancel/{task_id}
7. ``popola_inject_subtask``   — *deferred* to v0.3.0 F2 (supervise primitive)

Spec §9 R-1 (MCP server-to-client push limitation) dictates that
``popola_attach_stream`` returns a one-shot **snapshot** of recent events
rather than a true stream — MCP is not designed for server-initiated
push to the client outside of in-flight client requests.

Workspace rule "No Silent Failures": every daemon-down (httpx.ConnectError)
or non-200 response surfaces as an MCP ``CallToolResult`` with
``isError=True`` and a clear text message ("popolad not running, run
``popola popolad start``" / etc.).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

__all__ = [
    "TOOL_DEFINITIONS",
    "VerbHandler",
    "build_tool_list",
    "call_verb",
    "popola_attach_stream",
    "popola_cancel",
    "popola_federate",
    "popola_inject_subtask",
    "popola_list",
    "popola_relay",
    "popola_status",
    "popola_submit",
    "popola_supervise",
    "popola_supply_feedback",
]

logger = logging.getLogger(__name__)


VerbHandler = Callable[[httpx.AsyncClient, dict[str, Any]], Awaitable[CallToolResult]]
"""Type alias: ``async (client, args) -> CallToolResult``.

All verb functions follow this contract so :func:`call_verb` can dispatch
uniformly. Tools always return :class:`CallToolResult` directly so error
paths can set ``isError=True`` (No Silent Failures rule)."""


_DAEMON_DOWN_MSG: str = (
    "popolad not running. Start it with: `popola popolad start`. "
    "Verify with: `popola probe`. "
    "(MCP verb couldn't open the Unix Domain Socket.)"
)
"""Friendly daemon-down error text, mirroring CLI's ``_render_connect_error``."""


_ATTACH_STREAM_DEFAULT_LAST_N: int = 50
"""Default number of trailing events ``popola_attach_stream`` returns when
the caller doesn't specify ``last_n``. Snapshot semantics — see R-1 above."""


_ATTACH_STREAM_READ_TIMEOUT_S: float = 3.0
"""Bounded read window for the SSE snapshot.

For an already-terminal task the daemon's producer drains the file then
closes immediately. For an in-flight task the producer keeps polling, so
we cap our read at this many seconds to keep the MCP response responsive.
"""


# ── tool descriptor dataclass ────────────────────────────────────────────


@dataclass(frozen=True)
class ToolDefinition:
    """Static descriptor for one of the 7 dispatch verbs.

    Attributes:
        name: MCP tool name (matches the function name, e.g. ``popola_submit``).
        description: One-line human-readable description shown in tools/list.
        input_schema: JSON Schema validated by the MCP framework before
            ``call_tool`` is invoked.
        annotations: :class:`ToolAnnotations` hints (readOnly / idempotent /
            destructive) — guide IDE Agents per MCP spec §"Tool Annotations".
        handler: The async function that executes the verb.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: ToolAnnotations
    handler: VerbHandler

    def to_tool(self) -> Tool:
        """Convert to the official :class:`mcp.types.Tool` descriptor."""
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
            annotations=self.annotations,
        )


# ── helpers ──────────────────────────────────────────────────────────────


def _success(payload: Any) -> CallToolResult:
    """Build a non-error :class:`CallToolResult` with JSON-encoded text.

    Returning the JSON form (rather than raw repr) makes the output
    parseable by IDE Agents — they can trivially feed the text back into
    a JSON parser to recover the structured shape.
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


def _error(message: str) -> CallToolResult:
    """Build an MCP error result with ``isError=True`` (No Silent Failures)."""
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


def _connect_error(exc: httpx.HTTPError) -> CallToolResult:
    """Daemon-down friendly error, matching CLI's ``_render_connect_error`` text."""
    logger.debug("popolad UDS connect failure: %r", exc)
    return _error(_DAEMON_DOWN_MSG)


def _http_error(verb_label: str, response: httpx.Response) -> CallToolResult:
    """Render a non-200 daemon response as an MCP error result.

    Includes the HTTP status code + body so IDE agents (and humans
    debugging via mcp-inspector) get actionable diagnostics.
    """
    body = response.text
    return _error(
        f"{verb_label} failed: HTTP {response.status_code}: {body[:500]}"
    )


# ── verb implementations ─────────────────────────────────────────────────


async def popola_submit(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /dispatch — create a new dispatch task on the popolad daemon.

    Args (validated upstream by inputSchema):
        cli: required adapter name (e.g. ``cursor`` / ``claude`` / ``codex``).
        prompt: required prompt forwarded verbatim to the chosen CLI.
        cwd: optional working directory; defaults to popolad's CWD.
        extra: optional adapter-specific extras (R-012, e.g. ``{"yolo": True}``).

    Returns:
        CallToolResult with ``{task_id, events_log, cli}`` on success.
    """
    cli = args.get("cli")
    prompt = args.get("prompt")
    if not isinstance(cli, str) or not cli:
        return _error("popola_submit: 'cli' is required (adapter name).")
    if not isinstance(prompt, str):
        return _error("popola_submit: 'prompt' is required (string).")

    body: dict[str, Any] = {"cli": cli, "prompt": prompt}
    cwd = args.get("cwd")
    if isinstance(cwd, str) and cwd:
        body["cwd"] = cwd
    extra = args.get("extra")
    if isinstance(extra, dict):
        body["extra"] = extra

    try:
        response = await client.post("/dispatch", json=body)
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_submit: transport error: {exc!r}")

    if response.status_code != 200:
        return _http_error("popola_submit", response)
    return _success(response.json())


async def popola_list(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """GET /list — return active task summaries.

    Args:
        include_terminal: when true, also include completed/failed/canceled
            tasks; defaults to false (matches CLI ``popola list``'s default).
    """
    include_terminal = bool(args.get("include_terminal", False))
    try:
        response = await client.get(
            "/list", params={"include_terminal": include_terminal}
        )
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_list: transport error: {exc!r}")

    if response.status_code != 200:
        return _http_error("popola_list", response)
    return _success(response.json())


async def popola_status(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """GET /status/{task_id} — return full status of a single task."""
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _error("popola_status: 'task_id' is required (string).")

    try:
        response = await client.get(f"/status/{task_id}")
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_status: transport error: {exc!r}")

    if response.status_code == 404:
        return _error(f"popola_status: task not found: {task_id}")
    if response.status_code != 200:
        return _http_error("popola_status", response)
    return _success(response.json())


async def popola_attach_stream(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """GET /attach_stream/{task_id} — return a snapshot of recent events.

    MCP isn't designed for server-initiated streaming inside a tool
    response (spec §9 R-1: server-to-client push must be tied to an
    in-flight client request); this verb therefore returns a **snapshot**
    of the trailing ``last_n`` events rather than a live stream.

    Strategy:

    1. ``GET /status/{task_id}`` to confirm the task exists + read the
       current ``latest_event_index``.
    2. Compute ``since = max(0, latest_event_index - last_n)``.
    3. Open the SSE stream with ``since=since`` and consume frames with a
       short read timeout (terminal tasks drain immediately; in-flight
       tasks return whatever is available within
       :data:`_ATTACH_STREAM_READ_TIMEOUT_S`).
    """
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _error("popola_attach_stream: 'task_id' is required (string).")
    raw_last_n = args.get("last_n", _ATTACH_STREAM_DEFAULT_LAST_N)
    try:
        last_n = int(raw_last_n)
    except (TypeError, ValueError):
        return _error(
            f"popola_attach_stream: 'last_n' must be an integer, got: {raw_last_n!r}"
        )
    if last_n <= 0:
        return _error("popola_attach_stream: 'last_n' must be >= 1.")

    try:
        status_resp = await client.get(f"/status/{task_id}")
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_attach_stream: transport error: {exc!r}")

    if status_resp.status_code == 404:
        return _error(f"popola_attach_stream: task not found: {task_id}")
    if status_resp.status_code != 200:
        return _http_error("popola_attach_stream", status_resp)

    status = status_resp.json()
    latest_idx = int(status.get("latest_event_index") or 0)
    since = max(0, latest_idx - last_n)

    events: list[dict[str, Any]] = []
    try:
        async with client.stream(
            "GET",
            f"/attach_stream/{task_id}",
            params={"since": since},
            timeout=httpx.Timeout(
                connect=5.0,
                read=_ATTACH_STREAM_READ_TIMEOUT_S,
                write=5.0,
                pool=5.0,
            ),
        ) as stream:
            if stream.status_code != 200:
                await stream.aread()
                return _error(
                    f"popola_attach_stream: SSE HTTP {stream.status_code}: "
                    f"{stream.text[:500]}"
                )
            try:
                async for raw_line in stream.aiter_lines():
                    if not raw_line:
                        continue
                    if not raw_line.startswith("data: "):
                        continue
                    payload = raw_line[len("data: ") :]
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping un-parsable SSE frame (task=%s): %r",
                            task_id,
                            payload[:200],
                        )
            except httpx.ReadTimeout:
                logger.debug(
                    "popola_attach_stream: read timeout after %ss "
                    "(task in-flight, returning %d snapshot events)",
                    _ATTACH_STREAM_READ_TIMEOUT_S,
                    len(events),
                )
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_attach_stream: stream error: {exc!r}")

    snapshot = events[-last_n:] if len(events) > last_n else events
    return _success(
        {
            "task_id": task_id,
            "since": since,
            "count": len(snapshot),
            "events": snapshot,
        }
    )


async def popola_supply_feedback(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /supply_feedback/{task_id} — *deferred to v0.3.0 F4*.

    Resumes a paused LangGraph ``interrupt()`` with a user-supplied
    response value. The corresponding daemon RPC route does not yet exist
    in v0.2.0 (Stage B's graph wiring + Stage C's ArkTower interrupt
    persistence are pre-requisites), so this verb returns a clear
    "not implemented" CallToolResult with isError=True.

    See ``v0.2.0-plan.md`` §F4 (next-iteration HITL primitive) for the
    full plan; the inputSchema here is occupied as the v0.3.0 contract.
    """
    return _error(
        "popola_supply_feedback: not implemented in v0.2.0 (deferred to v0.3.0 F4 — "
        "HITL interrupt-resume primitive). Daemon RPC POST /supply_feedback/{task_id} "
        "is not yet wired; see .local/memory/specs/popolaloom/v0.2.0-plan.md §F4. "
        "args=" + json.dumps(args, ensure_ascii=False)
    )


async def popola_cancel(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /cancel/{task_id} — SIGTERM with 5s SIGKILL escalation."""
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _error("popola_cancel: 'task_id' is required (string).")

    try:
        response = await client.post(f"/cancel/{task_id}")
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_cancel: transport error: {exc!r}")

    if response.status_code == 404:
        return _error(f"popola_cancel: task not found: {task_id}")
    if response.status_code == 409:
        try:
            detail = response.json().get("detail", "")
        except json.JSONDecodeError:
            detail = response.text
        return _success(
            {
                "task_id": task_id,
                "cancelled": True,
                "already_terminal": True,
                "note": (
                    "task already in terminal state — cancel is idempotent "
                    f"(server detail: {detail!r})"
                ),
            }
        )
    if response.status_code != 200:
        return _http_error("popola_cancel", response)
    return _success(response.json())


async def popola_inject_subtask(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /inject_subtask — superseded by ``popola_relay`` in v0.3.0 F2.

    Kept for back-compat with v0.2.x clients that still address the
    legacy verb name; new code should use ``popola_relay``.
    """
    return await popola_relay(client, args)


async def popola_relay(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /relay — cross-CLI handoff (v0.3.0 F2).

    Spawns a child task on ``target_cli`` carrying a handoff envelope
    derived from a source task.  Returns the new ``child_task_id``.

    Args (validated upstream by inputSchema):
        source_task_id: required parent popola task id
        target_cli:     required new CLI name (may differ from source)
        payload:        artifact dict to thread into the child
        reason:         short human-readable handoff reason
        constraints:    optional execution hints (timeout, max_tokens)
        source_cli:     optional override (defaults to parent's CLI)
        prompt:         optional override (defaults to a synthesised prompt)
    """
    source_task_id = args.get("source_task_id") or args.get("parent_task_id")
    target_cli = args.get("target_cli") or args.get("cli")
    reason = args.get("reason", "relay handoff")
    if not isinstance(source_task_id, str) or not source_task_id:
        return _error("popola_relay: 'source_task_id' is required (string).")
    if not isinstance(target_cli, str) or not target_cli:
        return _error("popola_relay: 'target_cli' is required (string).")

    body: dict[str, Any] = {
        "source_task_id": source_task_id,
        "target_cli": target_cli,
        "reason": str(reason),
    }
    payload = args.get("payload")
    if isinstance(payload, dict):
        body["payload"] = payload
    constraints = args.get("constraints")
    if isinstance(constraints, dict):
        body["constraints"] = constraints
    source_cli = args.get("source_cli")
    if isinstance(source_cli, str) and source_cli:
        body["source_cli"] = source_cli
    prompt = args.get("prompt")
    if isinstance(prompt, str) and prompt:
        body["prompt"] = prompt

    try:
        response = await client.post("/relay", json=body)
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_relay: transport error: {exc!r}")

    if response.status_code != 200:
        return _http_error("popola_relay", response)
    return _success(response.json())


async def popola_supervise(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /supervise — register parent → child completion subscription (F2)."""
    parent_task_id = args.get("parent_task_id")
    child_task_id = args.get("child_task_id")
    if not isinstance(parent_task_id, str) or not parent_task_id:
        return _error("popola_supervise: 'parent_task_id' is required (string).")
    if not isinstance(child_task_id, str) or not child_task_id:
        return _error("popola_supervise: 'child_task_id' is required (string).")

    body: dict[str, Any] = {
        "parent_task_id": parent_task_id,
        "child_task_id": child_task_id,
    }
    callback_url = args.get("callback_url")
    if isinstance(callback_url, str) and callback_url:
        body["callback_url"] = callback_url

    try:
        response = await client.post("/supervise", json=body)
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_supervise: transport error: {exc!r}")

    if response.status_code != 200:
        return _http_error("popola_supervise", response)
    return _success(response.json())


async def popola_federate(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """POST /federate — multi-CLI vote on a shared prompt (F2)."""
    cli_list = args.get("cli_list")
    prompt = args.get("prompt")
    if not isinstance(cli_list, list) or len(cli_list) < 3:
        return _error(
            "popola_federate: 'cli_list' must be a list with ≥ 3 CLI names."
        )
    if not isinstance(prompt, str) or not prompt:
        return _error("popola_federate: 'prompt' is required (non-empty string).")

    body: dict[str, Any] = {"cli_list": cli_list, "prompt": prompt}
    voting_strategy = args.get("voting_strategy")
    if isinstance(voting_strategy, str) and voting_strategy:
        body["voting_strategy"] = voting_strategy
    timeout_s = args.get("timeout_s")
    if isinstance(timeout_s, (int, float)):
        body["timeout_s"] = float(timeout_s)

    try:
        response = await client.post("/federate", json=body)
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        return _error(f"popola_federate: transport error: {exc!r}")

    if response.status_code != 200:
        return _http_error("popola_federate", response)
    return _success(response.json())


# ── tool definitions registry ────────────────────────────────────────────


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="popola_submit",
        description=(
            "Dispatch a new task to the popolad daemon. Spawns the chosen "
            "local agent CLI (cursor / claude / codex / ...) under a fresh "
            "session id, persists the task in ArkTower, and starts an "
            "NDJSON event log. Returns {task_id, events_log, cli}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "cli": {
                    "type": "string",
                    "description": (
                        "Adapter name registered in popolaloom.adapters "
                        "(e.g. 'cursor', 'claude', 'codex')."
                    ),
                    "minLength": 1,
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Prompt string forwarded verbatim to the chosen CLI."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory for the spawned subprocess; "
                        "defaults to popolad's CWD when omitted."
                    ),
                },
                "extra": {
                    "type": "object",
                    "description": (
                        "Adapter-specific extras (R-012, e.g. "
                        "{'yolo': true} for cursor or "
                        "{'output_format': 'stream-json'} for claude)."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["cli", "prompt"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Submit a popola dispatch task",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=popola_submit,
    ),
    ToolDefinition(
        name="popola_list",
        description=(
            "List active (non-terminal) popola tasks. Set "
            "include_terminal=true to also see completed / failed / "
            "canceled tasks."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_terminal": {
                    "type": "boolean",
                    "description": (
                        "When true, also return completed/failed/canceled "
                        "tasks. Default: false (active only)."
                    ),
                    "default": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="List active dispatch tasks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=popola_list,
    ),
    ToolDefinition(
        name="popola_status",
        description=(
            "Fetch full runtime status of a single task: state, pid, "
            "exit_code, latest_event_index, arktower_task_id, persisted, "
            "started_at, completed_at."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "Task identifier returned by popola_submit."
                    ),
                    "minLength": 1,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Get task status",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=popola_status,
    ),
    ToolDefinition(
        name="popola_attach_stream",
        description=(
            "Return a snapshot of the trailing N CloudEvents envelopes "
            "for the task. MCP can't push live streams "
            "(spec §9 R-1: server-to-client push limitation); call this "
            "verb repeatedly for a polling loop."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "Task identifier whose events to snapshot."
                    ),
                    "minLength": 1,
                },
                "last_n": {
                    "type": "integer",
                    "description": (
                        "How many trailing events to return. "
                        f"Default {_ATTACH_STREAM_DEFAULT_LAST_N}, max 1000."
                    ),
                    "default": _ATTACH_STREAM_DEFAULT_LAST_N,
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Attach to task event stream (snapshot)",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=popola_attach_stream,
    ),
    ToolDefinition(
        name="popola_supply_feedback",
        description=(
            "Resume a paused LangGraph interrupt() with a user-supplied "
            "response. *Deferred to v0.3.0 F4* — daemon RPC not wired in "
            "v0.2.0; calling this verb returns a clear "
            "'not implemented' error."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "Task identifier (must be in 'awaiting_feedback')."
                    ),
                    "minLength": 1,
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Resume value injected back into the LangGraph "
                        "Command(resume=value) call."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Optional rationale logged to NDJSON event log."
                    ),
                },
            },
            "required": ["task_id", "value"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Supply HITL feedback (deferred to v0.3.0 F4)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=popola_supply_feedback,
    ),
    ToolDefinition(
        name="popola_cancel",
        description=(
            "Cancel a running task: SIGTERM the subprocess, SIGKILL after "
            "a 5s grace window. Calling on an already-terminal task is "
            "idempotent (returns already_terminal=true)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task identifier to cancel.",
                    "minLength": 1,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Cancel a running task",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=popola_cancel,
    ),
    ToolDefinition(
        name="popola_inject_subtask",
        description=(
            "Legacy alias for ``popola_relay`` (v0.2.x clients). New code "
            "should call ``popola_relay`` directly. The same payload "
            "schema is accepted; ``parent_task_id`` is treated as "
            "``source_task_id`` and ``cli`` as ``target_cli``."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "parent_task_id": {"type": "string", "minLength": 1},
                "cli": {"type": "string", "minLength": 1},
                "prompt": {"type": "string"},
                "reason": {"type": "string", "default": "relay handoff"},
                "payload": {"type": "object", "additionalProperties": True},
                "constraints": {"type": "object", "additionalProperties": True},
            },
            "required": ["parent_task_id", "cli"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Inject subtask (legacy alias for popola_relay)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=popola_inject_subtask,
    ),
    ToolDefinition(
        name="popola_relay",
        description=(
            "Cross-CLI handoff (v0.3.0 F2). Spawns a child task on "
            "``target_cli`` carrying a ``RelayHandoffEnvelope`` payload "
            "from the source task. Returns the new child_task_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source_task_id": {
                    "type": "string",
                    "description": "Parent popola task id producing the artifact.",
                    "minLength": 1,
                },
                "target_cli": {
                    "type": "string",
                    "description": "CLI name that should continue the work.",
                    "minLength": 1,
                },
                "payload": {
                    "type": "object",
                    "description": "Free-form artifact bundle handed to the child.",
                    "additionalProperties": True,
                },
                "reason": {
                    "type": "string",
                    "description": "Human-readable handoff reason (≥ 1 char).",
                    "minLength": 1,
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional execution constraints (timeout/max_tokens).",
                    "additionalProperties": True,
                },
                "source_cli": {
                    "type": "string",
                    "description": "Override source CLI name (defaults to parent's CLI).",
                },
                "prompt": {
                    "type": "string",
                    "description": "Override child task prompt.",
                },
            },
            "required": ["source_task_id", "target_cli", "reason"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Cross-CLI relay handoff",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=popola_relay,
    ),
    ToolDefinition(
        name="popola_supervise",
        description=(
            "Register a parent task as a supervisor of a child task "
            "(v0.3.0 F2). Returns a subscription_id; in-process "
            "callbacks fire when the child reaches a terminal state."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "parent_task_id": {
                    "type": "string",
                    "description": "Parent popola task id.",
                    "minLength": 1,
                },
                "child_task_id": {
                    "type": "string",
                    "description": "Child popola task id being supervised.",
                    "minLength": 1,
                },
                "callback_url": {
                    "type": "string",
                    "description": "Optional HTTP webhook URL (forensic only in v0.3.0).",
                },
            },
            "required": ["parent_task_id", "child_task_id"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Supervise child task completion",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=popola_supervise,
    ),
    ToolDefinition(
        name="popola_federate",
        description=(
            "Multi-CLI federate vote (v0.3.0 F2). Dispatches the same "
            "prompt to ≥ 3 CLIs concurrently and returns the spawned "
            "child_task_ids; voting result is determined when children "
            "complete (mvp: hash similarity → majority)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "cli_list": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "description": "≥ 3 distinct CLI names.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Shared prompt sent to every CLI.",
                    "minLength": 1,
                },
                "voting_strategy": {
                    "type": "string",
                    "enum": ["majority", "unanimous", "first_to_finish"],
                    "default": "majority",
                },
                "timeout_s": {
                    "type": "number",
                    "default": 60.0,
                    "exclusiveMinimum": 0.0,
                    "maximum": 3600.0,
                },
            },
            "required": ["cli_list", "prompt"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            title="Federate prompt across CLIs",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=popola_federate,
    ),
)
"""Frozen tuple of all 7 dispatch verb descriptors.

Order matches v0.2.0-plan §4 D2 (submit / list / status / attach /
feedback / cancel / inject). The :func:`build_tool_list` helper unpacks
to :class:`Tool` for the MCP ``tools/list`` handler; :func:`call_verb`
dispatches by name."""


_HANDLER_BY_NAME: dict[str, VerbHandler] = {td.name: td.handler for td in TOOL_DEFINITIONS}


def build_tool_list() -> list[Tool]:
    """Return all 7 verbs as :class:`mcp.types.Tool` for ``tools/list``."""
    return [td.to_tool() for td in TOOL_DEFINITIONS]


async def call_verb(
    name: str, arguments: dict[str, Any], client: httpx.AsyncClient
) -> CallToolResult:
    """Dispatch ``tools/call`` to the matching verb handler.

    Args:
        name: Tool name (one of the 7 defined in :data:`TOOL_DEFINITIONS`).
        arguments: Tool arguments dict (already validated by the MCP
            framework against the verb's inputSchema before this is called).
        client: An :class:`httpx.AsyncClient` bound to the popolad UDS.

    Returns:
        :class:`CallToolResult` — error case has ``isError=True`` per the
        No Silent Failures rule.
    """
    handler = _HANDLER_BY_NAME.get(name)
    if handler is None:
        return _error(
            f"unknown popolaloom tool: {name!r}; expected one of "
            f"{sorted(_HANDLER_BY_NAME)}"
        )
    return await handler(client, arguments or {})
