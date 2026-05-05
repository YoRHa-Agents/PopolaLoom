"""popolaloom-mcp form-mode elicitation builder (v0.2.0 Stage D D3).

When :func:`popola_status` returns a task with a ``pending_interrupts``
payload (planned for v0.3.0 F4 wiring; v0.2.0 daemon does not yet emit
this field), the MCP server SHOULD respond by sending the IDE Agent an
``elicitation/create`` request in **form mode** with an enum of the
available choices. The host IDE Agent renders this as a chooser UI for
the human user, then routes the answer back to popolad via
:func:`popola_supply_feedback` (also v0.3.0 F4).

This module exposes :func:`build_elicitation_request` which produces the
JSON-RPC payload **but does not actually send it** — sending requires
:class:`mcp.server.session.ServerSession.elicit` inside the active tool
call request context, which only works once the daemon emits a real
``pending_interrupts`` field. We ship the schema builder + tests now so
v0.3.0 F4 only needs the wiring step (see v0.2.0-plan §F4).

Spec §9 R-1 ("MCP server-to-client push must be tied to an in-flight
client request") is honoured because the elicitation is fired as part of
the ``popola_status`` call response, not as an unsolicited push.
"""

from __future__ import annotations

from typing import Any

import jsonschema  # type: ignore[import-untyped]
from mcp.types import ElicitRequest, ElicitRequestFormParams

__all__ = [
    "ELICITATION_PAYLOAD_SCHEMA",
    "build_elicitation_request",
    "validate_elicitation_request",
]


# ── input contract: shape of the daemon's ``pending_interrupts`` payload ─


ELICITATION_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interrupt_id": {
            "type": "string",
            "description": (
                "Stable identifier for the LangGraph interrupt() call; "
                "echoed back when the IDE Agent calls popola_supply_feedback."
            ),
        },
        "task_id": {
            "type": "string",
            "description": "popolaloom task identifier holding the interrupt.",
        },
        "message": {
            "type": "string",
            "description": "Question presented to the user.",
        },
        "options": {
            "type": "array",
            "description": (
                "List of allowed answers; rendered as the enum constraint "
                "of the form's primary field. Must be non-empty."
            ),
            "items": {"type": "string"},
            "minItems": 1,
        },
        "allow_reason": {
            "type": "boolean",
            "description": (
                "When true, the form also collects a free-text 'reason' "
                "field alongside the choice. Default true."
            ),
            "default": True,
        },
    },
    "required": ["task_id", "message", "options"],
    "additionalProperties": True,
}
"""JSON Schema for the ``pending_interrupts[*]`` payload entries v0.3.0
will eventually emit on ``GET /status`` responses.

Locked here so v0.2.0 elicitation tests can validate v0.3.0's contract,
catching breakages early."""


# ── elicitation request builder ──────────────────────────────────────────


def build_elicitation_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP ``elicitation/create`` JSON-RPC params dict from a
    daemon ``pending_interrupts[*]`` payload.

    The returned dict matches :class:`mcp.types.ElicitRequestFormParams`
    (validated via :func:`validate_elicitation_request`) and is ready to
    pass to :class:`ServerSession.elicit` (v0.3.0 F4 wiring).

    Args:
        payload: a dict matching :data:`ELICITATION_PAYLOAD_SCHEMA`. Must
            include ``task_id``, ``message`` and a non-empty ``options``
            list.

    Returns:
        JSON-RPC params dict shaped as::

            {
              "method": "elicitation/create",
              "params": {
                "mode": "form",
                "message": str,
                "requestedSchema": {
                  "type": "object",
                  "properties": {
                    "choice": {"type": "string", "enum": [...]},
                    "reason": {"type": "string"}   # if allow_reason
                  },
                  "required": ["choice"]
                }
              }
            }

    Raises:
        ValueError: when ``payload`` is missing required fields or
            contains an empty ``options`` list (No Silent Failures rule).
    """
    try:
        jsonschema.validate(instance=payload, schema=ELICITATION_PAYLOAD_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError(
            f"build_elicitation_request: invalid pending_interrupt payload: {exc.message}"
        ) from exc

    options = list(payload["options"])
    if not options:
        raise ValueError(
            "build_elicitation_request: 'options' must be a non-empty list"
        )

    message = str(payload["message"])
    allow_reason = bool(payload.get("allow_reason", True))

    properties: dict[str, Any] = {
        "choice": {
            "type": "string",
            "title": "Choice",
            "description": "Pick one of the provided options.",
            "enum": options,
        },
    }
    required: list[str] = ["choice"]
    if allow_reason:
        properties["reason"] = {
            "type": "string",
            "title": "Reason",
            "description": "Optional rationale; logged to NDJSON event log.",
        }

    requested_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }

    return {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "requestedSchema": requested_schema,
            "_meta": {
                "task_id": payload["task_id"],
                "interrupt_id": payload.get("interrupt_id"),
            },
        },
    }


def validate_elicitation_request(envelope: dict[str, Any]) -> ElicitRequest:
    """Validate an envelope from :func:`build_elicitation_request` against
    the :class:`mcp.types.ElicitRequest` Pydantic model.

    This is the structural contract test — if MCP SDK changes its
    elicitation request shape, this assertion fails loudly during pytest
    (catching breakages well before any v0.3.0 IDE-Agent hookup).

    Args:
        envelope: dict produced by :func:`build_elicitation_request`.

    Returns:
        The validated :class:`ElicitRequest` (with form-mode params).

    Raises:
        ValueError: when validation fails. Wraps the original
            :class:`pydantic.ValidationError` so callers don't have to
            import pydantic.
    """
    if envelope.get("method") != "elicitation/create":
        raise ValueError(
            "validate_elicitation_request: method must be 'elicitation/create', "
            f"got: {envelope.get('method')!r}"
        )
    params = envelope.get("params") or {}
    if params.get("mode") != "form":
        raise ValueError(
            "validate_elicitation_request: only form mode is supported in v0.2.0; "
            f"got mode={params.get('mode')!r}"
        )
    try:
        form_params = ElicitRequestFormParams.model_validate(params)
    except Exception as exc:
        raise ValueError(
            f"validate_elicitation_request: invalid form params: {exc}"
        ) from exc
    return ElicitRequest(method="elicitation/create", params=form_params)
