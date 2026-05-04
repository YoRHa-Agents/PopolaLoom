"""MCP renderer — elicitation/create form-mode (v0.3.0 F4.B).

Per spec §3.5.4 + spec §9 R-1 + roadmap §12.5 + v0.3.0-plan §4 Stage F4.9.

Re-uses the v0.2.0 Stage D form-mode builder
(:func:`popolaloom.mcp.elicitation.build_elicitation_request`) by adapting
a :class:`HITLPrompt` into the daemon's ``pending_interrupts`` payload
shape.

Functions:

- :func:`render_mcp_elicitation` — :class:`HITLPrompt` → MCP
  ``elicitation/create`` JSON-RPC params dict (form mode + enum).
- :func:`parse_reply` — extract :class:`HITLReply` from the form
  response (the IDE Agent calls back via :func:`popola_supply_feedback`).

The MCP elicitation must be sent in response to an in-flight client
request (spec §9 R-1: server-to-client push limitation), so the
caller is responsible for piggybacking it on a ``popola_status`` /
``popola_attach_stream`` reply (handled in v0.3.0 F4 daemon wiring).

Workspace rule "No Silent Failures": invalid / missing form responses
return ``None`` (benign) but malformed payload shape (non-dict) raises.
"""

from __future__ import annotations

import logging
from typing import Any

from popolaloom.hitl import HITLPrompt, HITLReply
from popolaloom.mcp.elicitation import build_elicitation_request

logger = logging.getLogger(__name__)

__all__ = [
    "parse_reply",
    "render_mcp_elicitation",
]


def render_mcp_elicitation(prompt: HITLPrompt) -> dict[str, Any]:
    """Render a :class:`HITLPrompt` as an MCP elicitation/create payload.

    Adapts the prompt to the v0.2.0 Stage D
    ``ELICITATION_PAYLOAD_SCHEMA`` shape and delegates to
    :func:`build_elicitation_request`.
    """
    pid = prompt.ensure_prompt_id()
    payload = {
        "interrupt_id": pid,
        "task_id": pid,
        "message": (
            f"[{prompt.trigger}] {prompt.why}\n\n{prompt.what}"
        ),
        "options": [opt.id for opt in prompt.options],
        "allow_reason": True,
    }
    return build_elicitation_request(payload)


def parse_reply(form_response: dict[str, Any]) -> HITLReply | None:
    """Convert an MCP elicitation form response into a :class:`HITLReply`.

    Expected form_response shape (per
    :func:`build_elicitation_request`)::

        {
          "hitl_id": "<uuid>",  # echoed from elicitation _meta
          "choice": "<option_id>",
          "reason": "<optional>"
        }

    Args:
        form_response: dict from the MCP client (after the IDE Agent
            renders the form and the human submits).

    Returns:
        HITLReply: when shape is valid.
        None: when missing fields (logged at debug; benign).

    Raises:
        TypeError: when ``form_response`` is not a dict.
    """
    if not isinstance(form_response, dict):
        raise TypeError(
            f"parse_reply: form_response must be dict; got {type(form_response).__name__}"
        )
    hitl_id = form_response.get("hitl_id") or form_response.get("interrupt_id")
    choice = form_response.get("choice") or form_response.get("option_id")
    if not isinstance(hitl_id, str) or not hitl_id:
        logger.debug("mcp.parse_reply: missing hitl_id; benign skip")
        return None
    if not isinstance(choice, str) or not choice:
        logger.debug("mcp.parse_reply: missing choice/option_id; benign skip")
        return None
    reason = form_response.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    return HITLReply(
        hitl_id=hitl_id,
        option_id=choice,
        via="mcp",
        reason=reason,
        responder=form_response.get("responder"),
    )
