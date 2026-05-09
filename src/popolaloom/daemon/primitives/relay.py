"""relay primitive — cross-CLI handoff (v0.3.0 F2.2; v0.9.0 native HandoffEnvelope).

Spawns a child task (typically on a different CLI) carrying a payload
from the source task, with the source task linked as ``parent_task_id``
in the new task's ``extra`` bag.  The handoff_envelope schema travels
through ``extra["handoff_envelope"]`` so downstream consumers (F5
self-bootstrap S5, F4 HITL renderers) can inspect it.

Design (per spec §4.2 + §4.3 owned_files invariant; see
``.local/memory/specs/popolaloom/spec.md``):

- :class:`popolaloom.handoff.HandoffEnvelope` is the canonical wire
  schema for the handoff payload (Markdown front-matter; v0.7.1+).
  Validated strictly (``extra="forbid"``).
- :func:`relay` = the primitive function.  Reuses the existing
  :meth:`Popolad.dispatch_task` to spawn the child, threading the
  envelope through ``extra["handoff_envelope"]``.
- Returns the new ``child_task_id`` (popola task id, NOT ArkTower id —
  matches the dispatch contract).

v0.9.0 (BL-v0.9.0-1, Q-D-3 lock) — the legacy v0.3.0
``RelayHandoffEnvelope`` Pydantic model and the ``to_handoff_envelope``
migration helper are removed; the primitive now constructs a
:class:`popolaloom.handoff.HandoffEnvelope` directly. Free-form relay
payloads (the v0.3.0 ``payload`` arg) fold into
``adapter_extra["_relay_payload"]`` so downstream consumers can detect a
relay-origin envelope vs a fresh dispatch (matching the v0.7.3 bridge
mapping).

Workspace rule "No Silent Failures": when ``source_task_id`` doesn't
exist in the popolad state store we raise :class:`ValueError`; the RPC
layer maps that to ``HTTP 400``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from popolaloom.daemon.server import Popolad
    from popolaloom.handoff import HandoffEnvelope

logger = logging.getLogger(__name__)

CliName = Literal["cursor", "claude", "codex", "kimi", "copilot", "echo"]
"""Known CLI adapter names; ``echo`` is the test/mock adapter."""


def _build_handoff_envelope(
    *,
    source_cli: str,
    target_cli: str,
    source_task_id: str,
    payload: dict[str, Any],
    reason: str,
    constraints: dict[str, Any],
    prompt: str,
    cwd: str | None = None,
) -> HandoffEnvelope:
    """Construct a :class:`HandoffEnvelope` for a relay dispatch.

    Field mapping mirrors the v0.7.3 ``to_handoff_envelope`` bridge so
    consumers that inspected the previous shape see a stable migration:

    - ``source_cli`` → ``source_cli`` (verbatim).
    - ``target_cli`` → ``target_cli`` (verbatim).
    - ``source_task_id`` → ``parent_task_id`` (the new schema's relay
      linkage field).
    - ``payload`` (free-form dict) → folded into ``adapter_extra`` under
      a single ``"_relay_payload"`` key so downstream consumers can
      detect a relay-origin envelope vs a fresh dispatch.
    - ``constraints`` → ``constraints`` (verbatim).
    - ``reason`` → ``reason`` (verbatim).
    - Synthesised: ``handoff_id`` via :func:`generate_handoff_id` over
      the relay's natural key (target_cli + prompt + parent_task_id);
      ``created_at`` = now UTC; ``tags`` = ``["relay"]``.
    """
    from popolaloom.handoff import HandoffEnvelope, generate_handoff_id

    adapter_extra: dict[str, Any] = {}
    if payload:
        adapter_extra["_relay_payload"] = dict(payload)

    return HandoffEnvelope(
        handoff_id=generate_handoff_id(
            target_cli,
            prompt,
            parent_task_id=source_task_id,
            adapter_extra=adapter_extra,
            constraints=dict(constraints) if constraints else None,
        ),
        created_at=datetime.now(UTC),
        source_cli=source_cli,
        target_cli=target_cli,
        parent_task_id=source_task_id,
        prompt=prompt,
        cwd=cwd,
        adapter_extra=adapter_extra,
        constraints=dict(constraints) if constraints else {},
        reason=reason,
        tags=["relay"],
    )


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
            Travels through the new task's ``extra["handoff_envelope"]``
            under ``adapter_extra["_relay_payload"]``.
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
    if not target_cli:
        raise ValueError("relay: target_cli must be a non-empty string")
    if not reason:
        raise ValueError("relay: reason must be a non-empty string")

    parent = popolad.state_store.get(source_task_id)
    if parent is None:
        raise ValueError(
            f"relay: source_task_id={source_task_id!r} not found in popolad state"
        )

    resolved_source_cli = source_cli or parent.cli
    child_prompt = prompt or f"[relay from {source_task_id}] {reason}"

    envelope = _build_handoff_envelope(
        source_cli=resolved_source_cli,
        target_cli=target_cli,
        source_task_id=source_task_id,
        payload=payload,
        reason=reason,
        constraints=constraints or {},
        prompt=child_prompt,
    )
    extra: dict[str, Any] = {
        "parent_task_id": source_task_id,
        "handoff_envelope": envelope.model_dump(mode="json"),
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
