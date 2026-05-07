"""relay primitive — cross-CLI handoff (v0.3.0 F2.2; v0.7.3 bridge to HandoffEnvelope).

Spawns a child task (typically on a different CLI) carrying a payload
from the source task, with the source task linked as ``parent_task_id``
in the new task's ``extra`` bag.  The handoff_envelope schema travels
through ``extra["handoff_envelope"]`` so downstream consumers (F5
self-bootstrap S5, F4 HITL renderers) can inspect it.

Design (per spec §4.2 + §4.3 owned_files invariant; see
``.local/memory/specs/popolaloom/spec.md``):

- :class:`RelayHandoffEnvelope` Pydantic v2 model = the v0.3.0 wire
  schema for the handoff payload.  Validated strictly (``extra="forbid"``).
- :func:`relay` = the primitive function.  Reuses the existing
  :meth:`Popolad.dispatch_task` to spawn the child, threading the
  envelope through ``extra["handoff_envelope"]``.
- Returns the new ``child_task_id`` (popola task id, NOT ArkTower id —
  matches the dispatch contract).

v0.7.3 bridge — :func:`to_handoff_envelope` converts a v0.3.0
``RelayHandoffEnvelope`` into the new v0.8.0
:class:`popolaloom.handoff.HandoffEnvelope`. The two coexist during
the transition: relay() still emits the legacy schema into
``extra["handoff_envelope"]`` (so any v0.3.0–v0.7.2 consumer keeps
working unchanged), but new code paths can call
``to_handoff_envelope(relay_env)`` to obtain a HandoffEnvelope and
write it via :func:`popolaloom.handoff.write_envelope` for file-based
audit. Future v0.8.x or v0.9.0 may flip relay() to emit the new
schema natively + deprecate ``RelayHandoffEnvelope``.

Workspace rule "No Silent Failures": when ``source_task_id`` doesn't
exist in the popolad state store we raise :class:`ValueError`; the RPC
layer maps that to ``HTTP 400``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from popolaloom.daemon.server import Popolad
    from popolaloom.handoff import HandoffEnvelope

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


def to_handoff_envelope(
    relay_env: RelayHandoffEnvelope,
    *,
    prompt: str | None = None,
    cwd: str | None = None,
) -> HandoffEnvelope:
    """Convert a v0.3.0 :class:`RelayHandoffEnvelope` to the v0.8.0 :class:`HandoffEnvelope`.

    v0.7.3 bridge — provides a forward-migration path for code that
    currently dispatches via :func:`relay` and wants to also write a
    file-based handoff envelope (for audit / replay / cross-CLI hand-off).

    Field mapping:

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
      the relay's natural key (target_cli + reason + source_task_id);
      ``created_at`` = now UTC; ``tags`` = ``["relay-bridged"]``.

    The ``prompt`` and ``cwd`` are NOT carried by RelayHandoffEnvelope (it
    pre-dates the v0.8.0 envelope), so they must be supplied by the caller
    or default to a synthesised relay prompt + ``None`` respectively.

    Args:
        relay_env: source envelope (Pydantic v2 instance).
        prompt: optional override for the new envelope's ``prompt``.
            ``None`` (default) synthesises ``"[relay from <source_id>] <reason>"``
            mirroring :func:`relay`'s child-prompt convention.
        cwd: optional override for the new envelope's ``cwd``. ``None``
            (default) leaves it unset (popolad will use its CWD).

    Returns:
        HandoffEnvelope: validated new-schema envelope.

    Raises:
        TypeError: if ``relay_env`` is not a RelayHandoffEnvelope.
    """
    from popolaloom.handoff import HandoffEnvelope, generate_handoff_id

    if not isinstance(relay_env, RelayHandoffEnvelope):
        raise TypeError(
            f"to_handoff_envelope expects RelayHandoffEnvelope, "
            f"got {type(relay_env).__name__}"
        )

    resolved_prompt = (
        prompt
        if prompt is not None
        else f"[relay from {relay_env.source_task_id}] {relay_env.reason}"
    )

    adapter_extra: dict[str, Any] = {}
    if relay_env.payload:
        adapter_extra["_relay_payload"] = dict(relay_env.payload)

    return HandoffEnvelope(
        handoff_id=generate_handoff_id(
            relay_env.target_cli,
            resolved_prompt,
            parent_task_id=relay_env.source_task_id,
            adapter_extra=adapter_extra,
            constraints=dict(relay_env.constraints) if relay_env.constraints else None,
        ),
        created_at=datetime.now(UTC),
        source_cli=relay_env.source_cli,
        target_cli=relay_env.target_cli,
        parent_task_id=relay_env.source_task_id,
        prompt=resolved_prompt,
        cwd=cwd,
        adapter_extra=adapter_extra,
        constraints=dict(relay_env.constraints) if relay_env.constraints else {},
        reason=relay_env.reason,
        tags=["relay-bridged"],
    )
