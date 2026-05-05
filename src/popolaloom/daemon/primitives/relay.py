"""relay primitive — cross-CLI handoff (v0.3.0 F2.2).

Spawns a child task (typically on a different CLI) carrying a payload
from the source task, with the source task linked as ``parent_task_id``
in the new task's ``extra`` bag.  The handoff_envelope schema travels
through ``extra["handoff_envelope"]`` so downstream consumers (F5
self-bootstrap S5, F4 HITL renderers) can inspect it.

Design (per spec §4.2 + §4.3 owned_files invariant; see
``.local/memory/specs/popolaloom/spec.md``):

- :class:`RelayHandoffEnvelope` Pydantic v2 model = the wire schema for
  the handoff payload.  Validated strictly (``extra="forbid"``).
- :func:`relay` = the primitive function.  Reuses the existing
  :meth:`Popolad.dispatch_task` to spawn the child, threading the
  envelope through ``extra["handoff_envelope"]``.
- Returns the new ``child_task_id`` (popola task id, NOT ArkTower id —
  matches the dispatch contract).

Workspace rule "No Silent Failures": when ``source_task_id`` doesn't
exist in the popolad state store we raise :class:`ValueError`; the RPC
layer maps that to ``HTTP 400``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from popolaloom.daemon.server import Popolad

logger = logging.getLogger(__name__)

CliName = Literal["cursor", "claude", "codex", "kimi", "copilot", "echo"]
"""Known CLI adapter names; ``echo`` is the test/mock adapter."""


class RelayHandoffEnvelope(BaseModel):
    """Cross-CLI handoff envelope (per v0.3.0-plan.md D3.9).

    Attributes:
        source_cli:     CLI that produced the artifact being handed off.
        target_cli:     CLI that will continue the work (e.g. cursor →
                        claude for code-review, claude → codex for test).
        source_task_id: popola task id of the producer.
        payload:        Free-form dict (artifact ref / file diff / next
                        step instructions).  v0.3.0 doesn't constrain
                        the schema; v0.4.0+ may add per-payload-type
                        validators (e.g. ``payload.kind == "diff"``).
        reason:         Human-readable handoff reason (≥ 1 char) for
                        forensic audit + ArkTower task title prefix.
        constraints:    Optional execution constraints inherited by the
                        child task (timeout, max_tokens, etc.); shape is
                        adapter-specific.

    Workspace rule "No Silent Failures": ``extra="forbid"`` so unknown
    fields raise :class:`pydantic.ValidationError` instead of silently
    dropping handoff data.
    """

    model_config = ConfigDict(extra="forbid")

    source_cli: str = Field(..., min_length=1)
    target_cli: str = Field(..., min_length=1)
    source_task_id: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(..., min_length=1)
    constraints: dict[str, Any] = Field(default_factory=dict)


def relay(
    popolad: Popolad,
    *,
    source_task_id: str,
    target_cli: str,
    payload: dict[str, Any],
    reason: str,
    constraints: dict[str, Any] | None = None,
    source_cli: str | None = None,
    prompt: str | None = None,
) -> str:
    """Spawn a child task on ``target_cli`` carrying a handoff envelope.

    Args:
        popolad: The :class:`Popolad` facade (RPC injects the singleton).
        source_task_id: popola task id of the parent task.  MUST exist
            in the state store; otherwise :class:`ValueError`.
        target_cli: Adapter name for the new task (may differ from
            source — that's the whole point of relay).
        payload: Artifact bundle to hand off (diff, file list, summary).
            Travels through the new task's ``extra["handoff_envelope"]``.
        reason: Human-readable handoff reason (≥ 1 char).
        constraints: Optional execution constraints (timeout, max_tokens).
        source_cli: Optional source CLI name; resolved from the parent
            handle's ``cli`` field when omitted.
        prompt: Optional override prompt for the child task; defaults to
            ``"[relay from {source_task_id}] {reason}"``.

    Returns:
        str: ``child_task_id`` of the spawned child task.

    Raises:
        ValueError: when ``source_task_id`` is unknown to popolad or
            when ``target_cli`` / ``reason`` is blank.
    """
    parent = popolad.state_store.get(source_task_id)
    if parent is None:
        raise ValueError(
            f"relay: source_task_id={source_task_id!r} not found in popolad state"
        )

    resolved_source_cli = source_cli or parent.cli
    envelope = RelayHandoffEnvelope(
        source_cli=resolved_source_cli,
        target_cli=target_cli,
        source_task_id=source_task_id,
        payload=payload,
        reason=reason,
        constraints=constraints or {},
    )

    child_prompt = prompt or f"[relay from {source_task_id}] {reason}"
    extra: dict[str, Any] = {
        "parent_task_id": source_task_id,
        "handoff_envelope": envelope.model_dump(),
        "relay_reason": reason,
    }

    child_task_id = popolad.dispatch_task(
        cli=target_cli,
        prompt=child_prompt,
        cwd=None,
        env=None,
        adapter=None,
        extra=extra,
    )

    event_log = popolad.event_log(source_task_id)
    if event_log is not None:
        event_log.append(
            "relay.dispatched",
            {
                "source_task_id": source_task_id,
                "child_task_id": child_task_id,
                "target_cli": target_cli,
                "reason": reason,
            },
        )

    logger.info(
        "relay: source=%s child=%s target_cli=%s reason=%s",
        source_task_id,
        child_task_id,
        target_cli,
        reason,
    )
    return child_task_id
