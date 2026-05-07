"""HITL (Human-In-The-Loop) schemas + factories + renderers (v0.3.0 F4).

Per `.local/memory/specs/popolaloom/testing-matrix.md` §11.1 + roadmap
§12 + spec §3.4 + v0.3.0-plan §4 Stage F4 — this package owns the
full HITL contract:

- Pydantic v2 schemas (this module): :class:`HITLOption`,
  :class:`HITLPrompt`, :class:`ArtifactRef`, :class:`HITLReply`.
- Trigger factories (:mod:`popolaloom.hitl.triggers`): 5 helpers that
  produce a pre-filled :class:`HITLPrompt` for each trigger type
  (interrupt / round_floor / critical_error / ambiguous_fix /
  persistent_regression).
- Channel renderers (:mod:`popolaloom.hitl.renderers`): 5 modules
  (lark / ide / cli / mcp / web) that turn a :class:`HITLPrompt` into
  a channel-specific payload + parse the reply back.
- Cross-channel sync (:mod:`popolaloom.hitl.sync`): atomic
  ``popola_hitl`` SQLite UPDATE that prevents double-replies.

Validation rules (per spec §12 + workspace rule "No Silent Failures"):

- ``trigger`` ∈ {``round_floor``, ``ambiguous_input``,
  ``destructive_op``, ``approval``, ``info_request``}.
- ``why`` and ``what`` are non-empty strings.
- ``options`` ≥ 2 distinct entries (binary choice at minimum).
- ``default_option_id`` MUST match one of ``options[i].id``.
- ``channels`` ≥ 2 distinct values from {``lark``, ``ide``, ``cli``,
  ``email``, ``signal``, ``mcp``, ``web``, ``cloud``}.
- ``deadline_seconds`` is positive and ≤ 86400 (1-day cap so a stalled
  prompt doesn't pin a task forever).

Use::

    from popolaloom.hitl import HITLPrompt, HITLOption, ArtifactRef
    from popolaloom.hitl.triggers import create_approval_prompt

    prompt = create_approval_prompt(
        why="Auto-merge will rewrite history",
        what="Confirm rebase + force-push to main",
        options=[
            HITLOption(id="yes", label="Approve"),
            HITLOption(id="no", label="Block"),
        ],
    )
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Trigger / channel / artifact-type enums ─────────────────────────────

HITLTrigger = Literal[
    "round_floor",
    "ambiguous_input",
    "destructive_op",
    "approval",
    "info_request",
]
"""5-value enum per spec §12.6 — narrows the kind of human input needed
so the channel renderer can pick an appropriate template (e.g.
round_floor → 3-option escalation card)."""

HITLChannel = Literal[
    "lark", "ide", "cli", "email", "signal", "mcp", "web", "cloud"
]
"""8-value literal per spec §12.8 + v0.3.0 F4 + v0.8.5 cloud-agent HITL bridge.

Adds ``cloud`` for answers submitted through the Cursor cloud-agent HTTP
surface (alongside Lark / IDE / CLI / MCP / Web + legacy email / signal).

Every prompt MUST be wired to ≥ 2 distinct channels so a stale Lark bot
can't silently drop the prompt."""

ArtifactType = Literal[
    "event_log",
    "ark_task",
    "diff",
    "patch",
    "lark_card",
    "screenshot",
]
"""6-value enum per testing-matrix.md §12.5 — reflects what the human can
inspect to make an informed decision.  Add to this list cautiously; the
inner-gate also walks artifacts to compute the ``information_density``
nines dimension (v0.3.0 F1)."""


class ArtifactRef(BaseModel):
    """Typed reference to an inspectable artifact (v0.3.0 F4 prep).

    Attributes:
        type: One of :data:`ArtifactType`.
        uri:  Opaque URI string (no scheme constraints in v0.2.3 — the
            v0.3.0 renderer will validate per ``type``).
        label: Optional UI label; defaults to a stringified ``type``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ArtifactType
    uri: str = Field(..., min_length=1)
    label: str | None = None

    @field_validator("uri")
    @classmethod
    def _uri_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ArtifactRef.uri must be non-blank")
        return v


class HITLOption(BaseModel):
    """One choice in a HITL prompt (v0.3.0 F4 prep).

    Attributes:
        id: Stable identifier (used in the wire payload; must match one
            of :attr:`HITLPrompt.default_option_id`).
        label: Human-readable label rendered in the channel template.
        default: Hint that this option is the auto-selected one if the
            deadline expires.  Multiple ``default=True`` entries are
            allowed at the option level — the prompt-level
            :attr:`HITLPrompt.default_option_id` is the source of truth.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    default: bool = False

    @field_validator("id")
    @classmethod
    def _id_no_whitespace(cls, v: str) -> str:
        if " " in v or "\t" in v:
            raise ValueError("HITLOption.id must not contain whitespace")
        return v


class HITLPrompt(BaseModel):
    """The full HITL prompt envelope (v0.3.0 F4 prep, schema-only in v0.2.3).

    Per spec §12 + testing-matrix.md §1.4 / §1.5.  This schema is
    occupied in v0.2.3 so the Tier 1 schema test
    (``tests/matrix/tier1/test_hitl_prompt_schema.py``) can lock down
    the validation contract before the renderer ships in v0.3.0 F4.

    Attributes:
        trigger:           Why we're asking — one of :data:`HITLTrigger`.
        why:               Human-readable reason (≥ 1 char).
        what:              Action description (≥ 1 char).
        options:           ≥ 2 :class:`HITLOption` entries.
        default_option_id: Must equal one of ``options[i].id``.
        channels:          ≥ 2 distinct :data:`HITLChannel` values.
        deadline_seconds:  Positive, ≤ 86400 (1 day).
        artifacts:         Optional list of :class:`ArtifactRef`.
        prompt_id:         Optional stable id (defaults to ``hitl-<auto>``
                           in the v0.3.0 dispatcher; v0.2.3 leaves it
                           unset so tests can assign one).

    Workspace rule "No Silent Failures": all invariant violations raise
    :class:`pydantic.ValidationError` rather than coerce-and-warn.
    """

    model_config = ConfigDict(extra="forbid")

    trigger: HITLTrigger
    why: str = Field(..., min_length=1)
    what: str = Field(..., min_length=1)
    options: list[HITLOption]
    default_option_id: str
    channels: list[HITLChannel]
    deadline_seconds: int = Field(..., gt=0, le=86400)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    prompt_id: str | None = None

    @field_validator("options")
    @classmethod
    def _options_min_two(cls, v: list[HITLOption]) -> list[HITLOption]:
        if len(v) < 2:
            raise ValueError(
                "HITLPrompt.options requires ≥ 2 entries (binary choice minimum)"
            )
        ids = [o.id for o in v]
        if len(set(ids)) != len(ids):
            raise ValueError(
                f"HITLPrompt.options ids must be distinct; got {ids}"
            )
        return v

    @field_validator("channels")
    @classmethod
    def _channels_min_two(cls, v: list[HITLChannel]) -> list[HITLChannel]:
        if len(v) < 2:
            raise ValueError(
                "HITLPrompt.channels requires ≥ 2 entries (multi-channel rule)"
            )
        if len(set(v)) != len(v):
            raise ValueError(
                f"HITLPrompt.channels must be distinct; got {v}"
            )
        return v

    @model_validator(mode="after")
    def _default_option_must_exist(self) -> HITLPrompt:
        ids = {o.id for o in self.options}
        if self.default_option_id not in ids:
            raise ValueError(
                f"HITLPrompt.default_option_id={self.default_option_id!r} "
                f"must match one of options.id (got {sorted(ids)})"
            )
        return self

    def ensure_prompt_id(self) -> str:
        """Return ``prompt_id`` (auto-generating a UUID if unset).

        v0.3.0 F4 dispatcher uses this to stamp a stable id before
        persisting the prompt into ``popola_hitl``. ``HITLPrompt`` is
        not frozen so the auto-assignment is safe; tests that need a
        predictable id can pre-set ``prompt_id`` themselves.
        """
        if not self.prompt_id:
            self.prompt_id = f"hitl-{uuid.uuid4()}"
        return self.prompt_id


class HITLReply(BaseModel):
    """The reply envelope produced by every renderer's ``parse_reply``.

    A renderer turns a channel-specific event (Lark card action, MCP
    elicitation form response, popola CLI ``feedback`` invocation, ...)
    into one of these so :func:`popolaloom.hitl.sync.HITLStore.mark_answered`
    has a uniform schema to UPDATE on.

    Attributes:
        hitl_id: prompt identifier (matches :attr:`HITLPrompt.prompt_id`).
        option_id: the chosen option id (matches one of
            :attr:`HITLPrompt.options[i].id`).
        via: which channel produced the reply (one of
            :data:`HITLChannel`).
        reason: optional free-form rationale captured by the channel
            (Lark card has a ``reason`` field; IDE/CLI optional flag).
        responder: optional opaque identifier for the human (Lark
            ``open_id``, IDE login name, CLI ``$USER``, ...). Used for
            audit logs in v0.3.x.
        answered_at: UTC timestamp; defaults to now.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hitl_id: str = Field(..., min_length=1)
    option_id: str = Field(..., min_length=1)
    via: HITLChannel
    reason: str | None = None
    responder: str | None = None
    responder_id: str | None = None
    answered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _normalise_responder_aliases(self) -> HITLReply:
        """Keep ``responder`` and ``responder_id`` mutually consistent.

        We support both names so both the v0.3.0 F4 dispatcher
        (:attr:`responder_id`) and earlier in-progress code (:attr:`responder`)
        can interoperate.  The reply is frozen so we only set values
        during construction.
        """
        if self.responder and not self.responder_id:
            object.__setattr__(self, "responder_id", self.responder)
        elif self.responder_id and not self.responder:
            object.__setattr__(self, "responder", self.responder_id)
        return self


__all__ = [
    "ArtifactRef",
    "ArtifactType",
    "HITLChannel",
    "HITLOption",
    "HITLPrompt",
    "HITLReply",
    "HITLStore",
    "HITLTrigger",
    "create_ambiguous_fix_prompt",
    "create_critical_error_prompt",
    "create_interrupt_prompt",
    "create_persistent_regression_prompt",
    "create_round_floor_prompt",
]


# Lazy imports placed after the schema definitions to avoid circular
# dependencies (renderers / sync import HITLPrompt back).

from popolaloom.hitl.sync import HITLStore  # noqa: E402
from popolaloom.hitl.triggers import (  # noqa: E402
    create_ambiguous_fix_prompt,
    create_critical_error_prompt,
    create_interrupt_prompt,
    create_persistent_regression_prompt,
    create_round_floor_prompt,
)
