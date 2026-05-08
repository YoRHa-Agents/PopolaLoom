"""popolaloom_cloud_hitl_request — Cloud-agent HITL MCP verb (v0.8.7 W2.1 T2.1.1).

This module ships the new MCP verb that wraps the v0.8.5 daemon RPC
triad — ``POST /hitl/cloud/request``, ``GET /hitl/cloud/wait/{hitl_id}``,
``POST /hitl/cloud/answer/{hitl_id}`` — behind a single tool call so
**Cursor cloud agents** running on a Self-Hosted Worker can defer to a
human operator over Lark and **block** until the human answers, the
deadline elapses, or the daemon returns an explicit error envelope.

Locked decisions (per Q-B-3 + ``DECISIONS.md`` OQ-1):

- **Blocking** (single ``tool_call.result`` per invocation; the inner
  long-poll loop wraps the daemon's 60-s ``/wait`` cap minus a 5-s slack).
- **30-min default** ``timeout_s``; range ``[60, 86400]`` (24 h ceiling).
- **Idempotency** via ``request_digest = sha256(task_id|agent_id|run_id|
  question_text).hexdigest()[:32]`` when the caller omits
  ``idempotency_key`` (Q-B-4); replays inside the daemon's 1-h dedup
  window return ``deduped: true``.
- **6 explicit error codes** (per ``mcp-tool-contract.md`` §3.3 enum):
  ``timeout`` / ``cancelled`` / ``invalid_context`` /
  ``lark_unreachable`` / ``daemon_unreachable`` / ``internal``.
  Rejection (``option_id == "reject"``) is **not** an error — it returns
  success with the human's reason in ``answer`` (per §7 row 5).

Security (per ``SECURITY_CHECKLIST.md`` §4 S1 / I-1 invariant):
The literal value of ``CURSOR_API_KEY`` MUST never appear in the tool's
input, output, or error envelope. :func:`_redact_api_key` is the
defense-in-depth runtime guard; every emitted ``CallToolResult`` text
runs through it before reaching the cloud agent.

Cross-references:

- Contract: ``.local/research/v0.8.7_hitl/mcp-tool-contract.md`` §3 / §6 / §7.
- Probe protocol (T1.1.1): ``.local/research/v0.8.7_hitl/long-tool-call-probe.md`` §5.
- Locked decisions: ``.local/.agent/active/v0.8.7-cloud-hitl-prod/DECISIONS.md`` OQ-1.
- Sibling tasks: T2.1.2 (Lark card render) · T2.1.3 (daemon idempotency persistence).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

import httpx
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from popolaloom.mcp.tools import (  # noqa: F401 — VerbHandler re-exported for consumers
    _DAEMON_DOWN_MSG,
    TOOL_DEFINITIONS,
    ToolDefinition,
    VerbHandler,
)

__all__ = [
    "API_KEY_REDACTION_PLACEHOLDER",
    "CLOUD_HITL_ERROR_ENVELOPE_SCHEMA",
    "CLOUD_HITL_INPUT_SCHEMA",
    "CLOUD_HITL_OUTPUT_SCHEMA",
    "CLOUD_HITL_TOOL_DEFINITION",
    "CLOUD_HITL_TOOL_DEFINITIONS",
    "CLOUD_HITL_VERB_NAME",
    "DAEMON_LONG_POLL_CAP_S",
    "DEFAULT_OPTIONS",
    "DEFAULT_TIMEOUT_S",
    "ERROR_CODES",
    "IDEMPOTENCY_KEY_HEX_LEN",
    "IDEMPOTENCY_WINDOW_S",
    "MAX_CONTEXT_SUMMARY_LEN",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MAX_QUESTION_TEXT_LEN",
    "MAX_TIMEOUT_S",
    "MIN_TIMEOUT_S",
    "build_extended_handler_map",
    "build_extended_tool_list",
    "popolaloom_cloud_hitl_request",
]

logger = logging.getLogger(__name__)


# ── public constants ─────────────────────────────────────────────────────


CLOUD_HITL_VERB_NAME: str = "popolaloom_cloud_hitl_request"
"""MCP tool name (per ``mcp-tool-contract.md`` §2)."""

DEFAULT_TIMEOUT_S: int = 1800
"""Default per-call human-reply budget in seconds (Q-B-3: 30 min)."""

MIN_TIMEOUT_S: int = 60
"""Lower bound for ``timeout_s`` (matches contract §3.1)."""

MAX_TIMEOUT_S: int = 86400
"""Upper bound for ``timeout_s`` (24 h ceiling per contract §3.1)."""

DAEMON_LONG_POLL_CAP_S: float = 55.0
"""Inner long-poll slice; matches the daemon's 60-s cap minus 5-s slack."""

DAEMON_LONG_POLL_HTTP_TIMEOUT_S: float = 65.0
"""httpx read timeout for one ``/wait`` call (slice + 10 s buffer)."""

REQUEST_HTTP_TIMEOUT_S: float = 30.0
"""httpx timeout for the initial ``POST /hitl/cloud/request``."""

IDEMPOTENCY_WINDOW_S: int = 3600
"""Daemon's dedup window in seconds (matches contract §5)."""

IDEMPOTENCY_KEY_HEX_LEN: int = 32
"""Hex char count for the auto-derived sha256 prefix (Q-B-4)."""

MAX_QUESTION_TEXT_LEN: int = 4000
"""Upper bound for ``question_text`` per contract §3.1."""

MAX_CONTEXT_SUMMARY_LEN: int = 8000
"""Upper bound for ``context_summary`` per contract §3.1."""

MAX_IDEMPOTENCY_KEY_LEN: int = 128
"""Caller-supplied ``idempotency_key`` cap per contract §3.1."""

DEFAULT_OPTIONS: tuple[dict[str, str], ...] = (
    {"id": "approve", "label": "Approve"},
    {"id": "reject", "label": "Reject"},
    {"id": "custom", "label": "Custom answer"},
)
"""Default Lark-card buttons per contract §3.1 / §8 visual contract."""

ERROR_CODES: tuple[str, ...] = (
    "timeout",
    "cancelled",
    "invalid_context",
    "lark_unreachable",
    "daemon_unreachable",
    "internal",
)
"""6 error codes per contract §3.3 enum."""

API_KEY_REDACTION_PLACEHOLDER: str = "<REDACTED:CURSOR_API_KEY>"
"""String inserted in place of ``CURSOR_API_KEY`` when it appears in tool I/O.

Defense-in-depth for SECURITY_CHECKLIST §4 S1 (``CURSOR_API_KEY`` MUST
never appear in MCP tool I/O). The literal env-var value is read once
per output emission and replaced; the value itself is never logged.
"""


# ── input / output / error envelope schemas (per contract §3) ────────────


CLOUD_HITL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "PopolaLoom task id the cloud agent belongs to."
            ),
        },
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Cursor cloud agent id (e.g. bc-...). "
                "Maps to cursor_agent_id on the wire."
            ),
        },
        "run_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Cursor run id under that agent. "
                "Maps to cursor_run_id on the wire."
            ),
        },
        "question_text": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_QUESTION_TEXT_LEN,
            "description": (
                "The actual question shown to the human (becomes the "
                "Lark card body / prompt_body)."
            ),
        },
        "context_summary": {
            "type": "string",
            "maxLength": MAX_CONTEXT_SUMMARY_LEN,
            "description": (
                "Optional excerpt of the prompt / tool-call context that "
                "motivated the question. Truncated to 200 chars in the "
                "Lark card body; full text reachable via expand link in "
                "the card metadata."
            ),
        },
        "timeout_s": {
            "type": "integer",
            "minimum": MIN_TIMEOUT_S,
            "maximum": MAX_TIMEOUT_S,
            "default": DEFAULT_TIMEOUT_S,
            "description": (
                "Total wall-clock budget for the human reply, in seconds. "
                "Default 1800 (30 min); upper bound 86400 (24 h)."
            ),
        },
        "idempotency_key": {
            "type": "string",
            "maxLength": MAX_IDEMPOTENCY_KEY_LEN,
            "description": (
                "Optional caller-supplied dedup key. Defaults to "
                "request_digest = sha256(task_id|agent_id|run_id|"
                "question_text)[:32] when omitted."
            ),
        },
        "options": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["id", "label"],
            },
            "description": (
                "Optional override for the Lark card buttons. Default: "
                "[{id:'approve',label:'Approve'},{id:'reject',label:"
                "'Reject'},{id:'custom',label:'Custom answer'}]."
            ),
        },
    },
    "required": ["task_id", "agent_id", "run_id", "question_text"],
    "additionalProperties": False,
}
"""Input schema (per contract §3.1, verbatim)."""

CLOUD_HITL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hitl_id": {"type": "string"},
        "answer": {"type": "string"},
        "option_id": {"type": "string"},
        "answered_at": {"type": "string", "format": "date-time"},
        "answered_by": {"type": "string"},
        "channel": {
            "type": "string",
            "enum": ["lark", "ide", "cli", "mcp", "web", "cloud"],
        },
        "deduped": {"type": "boolean"},
    },
    "required": [
        "hitl_id",
        "answer",
        "option_id",
        "answered_at",
        "answered_by",
        "channel",
    ],
}
"""Output schema (per contract §3.2, verbatim)."""

CLOUD_HITL_ERROR_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": list(ERROR_CODES)},
                "message": {"type": "string"},
                "hitl_id": {"type": "string"},
                "retry_after_s": {"type": "integer"},
            },
            "required": ["code", "message"],
        }
    },
    "required": ["error"],
}
"""Error envelope schema (per contract §3.3, verbatim)."""


# ── internal helpers ─────────────────────────────────────────────────────


class _ValidatedInputs(NamedTuple):
    """Result of :func:`_validate_inputs` on a successful caller args parse."""

    task_id: str
    agent_id: str
    run_id: str
    question_text: str
    context_summary: str | None
    timeout_s: int
    idempotency_key: str
    options: list[dict[str, str]]


def _redact_api_key(text: str) -> str:
    """Replace any literal ``CURSOR_API_KEY`` value in ``text`` with a placeholder.

    Defense-in-depth for SECURITY_CHECKLIST §4 S1. The implementation is
    intentionally cheap (one ``str.replace`` per emission); the cost is
    negligible compared to the daemon round-trip and the workspace rule
    "No Silent Failures" requires us to emit *something* visible (the
    placeholder) rather than dropping the leaked secret without notice.
    """
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key or api_key not in text:
        return text
    return text.replace(api_key, API_KEY_REDACTION_PLACEHOLDER)


def _derive_idempotency_key(
    task_id: str, agent_id: str, run_id: str, question_text: str
) -> str:
    """Compute ``sha256(task_id|agent_id|run_id|question_text)[:32]`` (Q-B-4).

    The 32-hex-char prefix is opaque and tenant-scoped per
    SECURITY_CHECKLIST §5 R1. Callers MAY supply their own
    ``idempotency_key`` (≤ 128 chars); when omitted this function
    produces the canonical default — replays inside the daemon's 1-h
    dedup window will return the existing ``hitl_id`` with
    ``deduped: true``.
    """
    raw = f"{task_id}|{agent_id}|{run_id}|{question_text}".encode()
    return hashlib.sha256(raw).hexdigest()[:IDEMPOTENCY_KEY_HEX_LEN]


def _utcnow_iso() -> str:
    """Return wall-clock UTC ISO 8601 (millisecond precision) for fallback."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _render_answer(option_id: str, reason: str | None) -> str:
    """Render the human-readable answer text per contract §6.2.

    - ``approve`` → ``"approve"`` (or ``"approve: <reason>"`` when reason)
    - ``reject``  → ``"reject"``  (or ``"reject: <reason>"`` when reason)
    - ``custom``  → ``reason`` verbatim (the typed-in text)
    - other       → ``"<option_id>: <reason>"`` if reason else ``option_id``
    """
    reason_str = reason.strip() if isinstance(reason, str) else ""
    if option_id == "approve":
        return f"approve: {reason_str}" if reason_str else "approve"
    if option_id == "reject":
        return f"reject: {reason_str}" if reason_str else "reject"
    if option_id == "custom":
        return reason_str or "custom"
    return f"{option_id}: {reason_str}" if reason_str else option_id


def _error_envelope(
    code: str,
    message: str,
    *,
    hitl_id: str | None = None,
    retry_after_s: int | None = None,
) -> dict[str, Any]:
    """Build an error envelope per contract §3.3.

    Every ``message`` is run through :func:`_redact_api_key` so a leaked
    ``CURSOR_API_KEY`` (defense-in-depth) is never echoed to the cloud
    agent. ``hitl_id`` is included when the daemon row was created;
    ``retry_after_s`` is set for transient errors (per contract §7).
    Unknown codes are rejected loudly (No Silent Failures).
    """
    if code not in ERROR_CODES:
        raise ValueError(
            f"unknown error code: {code!r}; expected one of {list(ERROR_CODES)}"
        )
    err: dict[str, Any] = {"code": code, "message": _redact_api_key(message)}
    if hitl_id:
        err["hitl_id"] = hitl_id
    if retry_after_s is not None:
        err["retry_after_s"] = int(retry_after_s)
    return {"error": err}


def _make_error_result(envelope: dict[str, Any]) -> CallToolResult:
    """Wrap an error envelope into ``CallToolResult(isError=True)``."""
    text = json.dumps(envelope, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=_redact_api_key(text))],
        isError=True,
    )


def _make_success_result(payload: dict[str, Any]) -> CallToolResult:
    """Wrap a success payload into a non-error :class:`CallToolResult`."""
    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=_redact_api_key(text))],
        isError=False,
    )


def _validate_inputs(args: dict[str, Any]) -> _ValidatedInputs | dict[str, Any]:
    """Validate caller args; return ``_ValidatedInputs`` or an error envelope dict.

    Strict checks (contract §3.1):

    - ``task_id`` / ``agent_id`` / ``run_id`` / ``question_text`` are required
      non-empty strings; ``question_text`` ≤ 4 000 chars.
    - Optional ``context_summary`` ≤ 8 000 chars.
    - ``timeout_s`` integer in ``[60, 86 400]``; default 1 800. ``bool`` is
      explicitly rejected (Python's ``True/False`` would otherwise be
      coerced via ``int(...)``).
    - Optional ``idempotency_key`` non-empty string ≤ 128 chars; auto-derived
      via :func:`_derive_idempotency_key` when omitted.
    - Optional ``options`` list with ≥ 2 entries, each ``{id,label}``.

    On any failure: return an ``error_envelope`` dict (caller wraps).
    """
    task_id = args.get("task_id")
    agent_id = args.get("agent_id")
    run_id = args.get("run_id")
    question_text = args.get("question_text")
    if not isinstance(task_id, str) or not task_id:
        return _error_envelope(
            "invalid_context", "'task_id' is required (non-empty string)"
        )
    if not isinstance(agent_id, str) or not agent_id:
        return _error_envelope(
            "invalid_context", "'agent_id' is required (non-empty string)"
        )
    if not isinstance(run_id, str) or not run_id:
        return _error_envelope(
            "invalid_context", "'run_id' is required (non-empty string)"
        )
    if not isinstance(question_text, str) or not question_text:
        return _error_envelope(
            "invalid_context", "'question_text' is required (non-empty string)"
        )
    if len(question_text) > MAX_QUESTION_TEXT_LEN:
        return _error_envelope(
            "invalid_context",
            f"'question_text' must be ≤ {MAX_QUESTION_TEXT_LEN} chars; "
            f"got {len(question_text)}",
        )

    context_summary_raw = args.get("context_summary")
    context_summary: str | None = None
    if context_summary_raw is not None:
        if not isinstance(context_summary_raw, str):
            return _error_envelope(
                "invalid_context",
                "'context_summary' must be a string when provided",
            )
        if len(context_summary_raw) > MAX_CONTEXT_SUMMARY_LEN:
            return _error_envelope(
                "invalid_context",
                f"'context_summary' must be ≤ {MAX_CONTEXT_SUMMARY_LEN} chars",
            )
        context_summary = context_summary_raw

    timeout_raw = args.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout_raw, bool):
        return _error_envelope(
            "invalid_context",
            f"'timeout_s' must be an integer; got {timeout_raw!r}",
        )
    try:
        timeout_s = int(timeout_raw)
    except (TypeError, ValueError):
        return _error_envelope(
            "invalid_context",
            f"'timeout_s' must be an integer; got {timeout_raw!r}",
        )
    if timeout_s < MIN_TIMEOUT_S or timeout_s > MAX_TIMEOUT_S:
        return _error_envelope(
            "invalid_context",
            f"'timeout_s' must be in [{MIN_TIMEOUT_S}, {MAX_TIMEOUT_S}]; "
            f"got {timeout_s}",
        )

    idempotency_raw = args.get("idempotency_key")
    if idempotency_raw is None:
        idempotency_key = _derive_idempotency_key(
            task_id, agent_id, run_id, question_text
        )
    else:
        if not isinstance(idempotency_raw, str) or not idempotency_raw:
            return _error_envelope(
                "invalid_context",
                "'idempotency_key' must be a non-empty string when provided",
            )
        if len(idempotency_raw) > MAX_IDEMPOTENCY_KEY_LEN:
            return _error_envelope(
                "invalid_context",
                f"'idempotency_key' must be ≤ {MAX_IDEMPOTENCY_KEY_LEN} chars",
            )
        idempotency_key = idempotency_raw

    options_raw = args.get("options")
    if options_raw is None:
        options = [dict(o) for o in DEFAULT_OPTIONS]
    elif not isinstance(options_raw, list) or len(options_raw) < 2:
        return _error_envelope(
            "invalid_context",
            "'options' must be a list with ≥ 2 entries when provided",
        )
    else:
        options = []
        for entry in options_raw:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not isinstance(entry.get("label"), str)
                or not entry["id"]
                or not entry["label"]
            ):
                return _error_envelope(
                    "invalid_context",
                    "each 'options' entry must have non-empty 'id' + 'label' strings",
                )
            options.append({"id": str(entry["id"]), "label": str(entry["label"])})

    return _ValidatedInputs(
        task_id=task_id,
        agent_id=agent_id,
        run_id=run_id,
        question_text=question_text,
        context_summary=context_summary,
        timeout_s=timeout_s,
        idempotency_key=idempotency_key,
        options=options,
    )


def _build_request_body(inputs: _ValidatedInputs) -> dict[str, Any]:
    """Build the wire body for ``POST /hitl/cloud/request`` per contract §6.1.

    Renames per contract: ``agent_id → cursor_agent_id``,
    ``run_id → cursor_run_id``, ``question_text → prompt_body``;
    ``idempotency_key`` and ``context_summary`` ride on ``metadata``.
    """
    metadata: dict[str, Any] = {"idempotency_key": inputs.idempotency_key}
    if inputs.context_summary is not None:
        metadata["context_summary"] = inputs.context_summary
    return {
        "task_id": inputs.task_id,
        "cursor_agent_id": inputs.agent_id,
        "cursor_run_id": inputs.run_id,
        "prompt_title": f"PopolaLoom HITL — task: {inputs.task_id}",
        "prompt_body": inputs.question_text,
        "options": inputs.options,
        "metadata": metadata,
        "timeout_s": float(inputs.timeout_s),
    }


def _build_success_payload(
    *,
    hitl_id: str,
    answer_payload: dict[str, Any],
    answered_at_top: str | None,
    deduped: bool,
) -> dict[str, Any]:
    """Map a daemon ``status: "answered"`` payload into the contract §3.2 shape."""
    option_id = str(answer_payload.get("option_id") or "")
    reason_raw = answer_payload.get("reason")
    reason = reason_raw if isinstance(reason_raw, str) else None
    channel = str(answer_payload.get("channel") or "cloud")
    responder_id = str(answer_payload.get("responder_id") or "")
    answered_at_inner = answer_payload.get("answered_at")
    if isinstance(answered_at_top, str) and answered_at_top:
        answered_at = answered_at_top
    elif isinstance(answered_at_inner, str) and answered_at_inner:
        answered_at = answered_at_inner
    else:
        answered_at = _utcnow_iso()
    return {
        "hitl_id": hitl_id,
        "answer": _render_answer(option_id, reason),
        "option_id": option_id,
        "answered_at": answered_at,
        "answered_by": responder_id,
        "channel": channel,
        "deduped": deduped,
    }


# ── verb implementation ─────────────────────────────────────────────────


async def popolaloom_cloud_hitl_request(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> CallToolResult:
    """Defer to a human via Lark; block until answer or timeout (Q-B-3).

    The implementation maps:

    1. Caller ``args`` → ``POST /hitl/cloud/request`` (contract §6.1).
    2. Daemon ``hitl_id`` → ``GET /hitl/cloud/wait/{hitl_id}?timeout_s=55``
       in a loop until the daemon returns a terminal status or
       ``total_elapsed >= timeout_s``.
    3. Terminal status → MCP success / error envelope per contract §6.2.
    """
    validation = _validate_inputs(args)
    if isinstance(validation, dict):
        return _make_error_result(validation)
    inputs: _ValidatedInputs = validation

    body = _build_request_body(inputs)

    try:
        request_resp = await client.post(
            "/hitl/cloud/request",
            json=body,
            timeout=REQUEST_HTTP_TIMEOUT_S,
        )
    except httpx.ConnectError as exc:
        logger.debug("popolad UDS connect failure (request): %r", exc)
        return _make_error_result(
            _error_envelope(
                "daemon_unreachable",
                _DAEMON_DOWN_MSG,
                retry_after_s=60,
            )
        )
    except httpx.HTTPError as exc:
        return _make_error_result(
            _error_envelope(
                "daemon_unreachable",
                f"POST /hitl/cloud/request transport error: {exc!r}",
                retry_after_s=30,
            )
        )

    if request_resp.status_code != 200:
        return _make_error_result(
            _error_envelope(
                "daemon_unreachable",
                f"POST /hitl/cloud/request HTTP {request_resp.status_code}: "
                f"{request_resp.text[:500]}",
                retry_after_s=30,
            )
        )

    try:
        request_payload = request_resp.json()
    except ValueError:
        return _make_error_result(
            _error_envelope(
                "internal",
                f"daemon /hitl/cloud/request returned non-JSON: "
                f"{request_resp.text[:500]}",
            )
        )
    if not isinstance(request_payload, dict):
        return _make_error_result(
            _error_envelope(
                "internal",
                f"daemon /hitl/cloud/request returned non-object: {request_payload!r}",
            )
        )

    hitl_id_raw = request_payload.get("hitl_id")
    if not isinstance(hitl_id_raw, str) or not hitl_id_raw:
        return _make_error_result(
            _error_envelope(
                "internal",
                "daemon /hitl/cloud/request response missing 'hitl_id'",
            )
        )
    hitl_id: str = hitl_id_raw
    deduped = bool(request_payload.get("deduped", False))
    # T2.1.3 will wire the daemon-side flag; default True for v0.8.5 back-compat.
    lark_dispatched = bool(request_payload.get("lark_dispatched", True))

    started = time.monotonic()
    total_budget = float(inputs.timeout_s)
    while True:
        # v0.8.7 default; see DECISIONS.md OQ-1 + long-tool-call-probe.md §5
        elapsed = time.monotonic() - started
        if elapsed >= total_budget:
            return _make_error_result(
                _make_timeout_envelope(
                    hitl_id=hitl_id,
                    lark_dispatched=lark_dispatched,
                    detail=(
                        f"HITL request exceeded {inputs.timeout_s}s budget without "
                        f"a human answer (hitl_id={hitl_id})"
                    ),
                )
            )

        slice_s = min(DAEMON_LONG_POLL_CAP_S, total_budget - elapsed)
        try:
            wait_resp = await client.get(
                f"/hitl/cloud/wait/{hitl_id}",
                params={"timeout_s": slice_s},
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=DAEMON_LONG_POLL_HTTP_TIMEOUT_S,
                    write=5.0,
                    pool=5.0,
                ),
            )
        except httpx.ConnectError as exc:
            logger.debug("popolad UDS connect failure (wait): %r", exc)
            return _make_error_result(
                _error_envelope(
                    "daemon_unreachable",
                    _DAEMON_DOWN_MSG,
                    hitl_id=hitl_id,
                    retry_after_s=60,
                )
            )
        except httpx.HTTPError as exc:
            return _make_error_result(
                _error_envelope(
                    "daemon_unreachable",
                    f"GET /hitl/cloud/wait transport error: {exc!r}",
                    hitl_id=hitl_id,
                    retry_after_s=30,
                )
            )

        if wait_resp.status_code == 404:
            return _make_error_result(
                _error_envelope(
                    "invalid_context",
                    f"hitl_id {hitl_id!r} not found at daemon",
                    hitl_id=hitl_id,
                )
            )
        if wait_resp.status_code not in (200, 202):
            return _make_error_result(
                _error_envelope(
                    "daemon_unreachable",
                    f"GET /hitl/cloud/wait HTTP {wait_resp.status_code}: "
                    f"{wait_resp.text[:500]}",
                    hitl_id=hitl_id,
                    retry_after_s=30,
                )
            )

        try:
            wait_payload = wait_resp.json()
        except ValueError:
            return _make_error_result(
                _error_envelope(
                    "internal",
                    f"daemon /hitl/cloud/wait returned non-JSON: "
                    f"{wait_resp.text[:500]}",
                    hitl_id=hitl_id,
                )
            )
        if not isinstance(wait_payload, dict):
            return _make_error_result(
                _error_envelope(
                    "internal",
                    f"daemon /hitl/cloud/wait returned non-object: {wait_payload!r}",
                    hitl_id=hitl_id,
                )
            )

        status = wait_payload.get("status")
        if status == "answered":
            answer_payload = wait_payload.get("answer")
            if not isinstance(answer_payload, dict):
                return _make_error_result(
                    _error_envelope(
                        "internal",
                        "daemon /wait answered without 'answer' payload",
                        hitl_id=hitl_id,
                    )
                )
            answered_at_top = wait_payload.get("answered_at")
            return _make_success_result(
                _build_success_payload(
                    hitl_id=hitl_id,
                    answer_payload=answer_payload,
                    answered_at_top=(
                        answered_at_top if isinstance(answered_at_top, str) else None
                    ),
                    deduped=deduped,
                )
            )
        if status == "timeout":
            return _make_error_result(
                _make_timeout_envelope(
                    hitl_id=hitl_id,
                    lark_dispatched=lark_dispatched,
                    detail=(
                        f"HITL request timed out (daemon reported terminal "
                        f"timeout for hitl_id={hitl_id})"
                    ),
                )
            )
        if status == "cancelled":
            return _make_error_result(
                _error_envelope(
                    "cancelled",
                    f"HITL request was cancelled (hitl_id={hitl_id})",
                    hitl_id=hitl_id,
                )
            )
        if status == "pending":
            continue
        return _make_error_result(
            _error_envelope(
                "internal",
                f"unknown daemon /wait status: {status!r}",
                hitl_id=hitl_id,
            )
        )


def _make_timeout_envelope(
    *, hitl_id: str, lark_dispatched: bool, detail: str
) -> dict[str, Any]:
    """Surface a timeout as ``lark_unreachable`` when Lark fan-out failed.

    Per contract §7 row 4: a Lark-unreachable scenario where the row
    was created but the card never reached the human surfaces as a
    poll-then-error — the wait will eventually time out, and the tool
    flips the error code to ``lark_unreachable`` so the cloud agent can
    show "approval pending; check Lark" instead of a bare timeout.
    """
    if lark_dispatched:
        return _error_envelope("timeout", detail, hitl_id=hitl_id)
    return _error_envelope(
        "lark_unreachable",
        detail + " (Lark fan-out reported unreachable at request time)",
        hitl_id=hitl_id,
        retry_after_s=60,
    )


# ── tool definition + extension registry ─────────────────────────────────


CLOUD_HITL_TOOL_DEFINITION: ToolDefinition = ToolDefinition(
    name=CLOUD_HITL_VERB_NAME,
    description=(
        "Send a question to a human operator via Lark and block until they "
        "answer (default 30 min, max 24 h). Built-in idempotency via "
        "request_digest. Cloud agents should treat the returned answer as "
        "user input, not as command output."
    ),
    input_schema=CLOUD_HITL_INPUT_SCHEMA,
    annotations=ToolAnnotations(
        title="Defer to human via PopolaLoom HITL (Lark approval)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    handler=popolaloom_cloud_hitl_request,
)
"""ToolDefinition for the v0.8.7 cloud HITL verb (per contract §2)."""

CLOUD_HITL_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (CLOUD_HITL_TOOL_DEFINITION,)
"""Equivalent registry — extends ``mcp.tools.TOOL_DEFINITIONS`` per AC (a)."""


def build_extended_tool_list() -> list[Tool]:
    """Return the union of v0.2.0+ verbs + the v0.8.7 cloud HITL verb.

    Used by ``mcp/server.py`` (W2.2 wiring) to register the new verb
    alongside the existing 10 in ``tools.py`` without modifying that
    module (per task constraint "DO NOT touch tools.py").
    """
    base = [td.to_tool() for td in TOOL_DEFINITIONS]
    extra = [td.to_tool() for td in CLOUD_HITL_TOOL_DEFINITIONS]
    return [*base, *extra]


def build_extended_handler_map() -> dict[str, VerbHandler]:
    """Return ``{verb_name: handler}`` for all registered verbs.

    Mirrors the pattern in ``tools.py``'s private ``_HANDLER_BY_NAME``
    so a single dispatch table can route both ``popola_*`` and
    ``popolaloom_cloud_hitl_request`` calls.
    """
    handlers: dict[str, VerbHandler] = {td.name: td.handler for td in TOOL_DEFINITIONS}
    handlers.update({td.name: td.handler for td in CLOUD_HITL_TOOL_DEFINITIONS})
    return handlers
