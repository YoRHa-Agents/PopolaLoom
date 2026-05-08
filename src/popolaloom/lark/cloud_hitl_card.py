"""Cloud HITL Lark card v1 builder (v0.8.7 Stage 2 T2.1.2).

Per spec ``.local/research/v0.8.7_hitl/lark-card-spec.md`` §2 (template
structure), §3 (P0 state-machine scenarios), §4 (versioning policy), and
§6 (security checks).

This module is the **third** Lark card family in the codebase, dedicated
to the v0.8.7 Cursor Cloud Agent HITL approval flow:

- :class:`CloudHITLCardInput` — typed allowlist dataclass (per §6.1
  "allowlist, don't denylist"); ad-hoc ``dict[str, Any]`` is forbidden
  at the public boundary so secrets cannot accidentally leak into a card.
- :func:`build_cloud_hitl_card` — pure function that returns the card v2
  envelope (header + B1 + B2 + B3 + A1 + ``card_metadata``) per §2.3 and
  §2.4. Reuses :data:`popolaloom.lark.card_templates.LARK_NOTIFY_PROMPT_TRUNCATE`
  (=200) for B2 truncation and :func:`footer_with_origin_note` so the
  workspace-rule footer ``---\\n本消息由飞书工具 Lark-Cli 发送`` is appended
  automatically.
- :func:`mutate_card_for_pending_second_approver` — S2 mid-state mutator;
  flips header to ``wathet`` and stamps ``first_approver_*`` into
  ``card_metadata`` (per §3.2).
- :func:`mutate_card_for_answered` — S1 / S2 final-state mutator; flips
  header to ``green`` / ``red`` / ``yellow`` per option, replaces B3 with
  the answered-by note, and removes the action block (per §3.1).
- :func:`mutate_card_for_timeout` — S3 mutator; flips header to ``grey``,
  replaces B3 with the timed-out note, removes the action block (per §3.3).

No Silent Failures: questions ≥ 2 000 chars are rejected at the builder
boundary with :class:`ValueError` (truncating B1 would change the question
the human sees vs the agent submitted, per spec §2.3 B1 rule).
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from popolaloom.lark.card_templates import (
    LARK_NOTIFY_PROMPT_TRUNCATE,
    _truncate_prompt,
    footer_with_origin_note,
)

__all__ = [
    "CARD_TEMPLATE_ID",
    "CARD_TEMPLATE_VERSION",
    "CARD_METADATA_KEYS",
    "DEFAULT_POPOLAD_HOST",
    "DEFAULT_POPOLAD_PORT",
    "DEFAULT_TIMEOUT_S",
    "MAX_QUESTION_TEXT_LEN",
    "RESPONDER_POLICIES",
    "CloudHITLCardInput",
    "build_cloud_hitl_card",
    "compute_idempotency_key",
    "mutate_card_for_answered",
    "mutate_card_for_pending_second_approver",
    "mutate_card_for_timeout",
]


# ── Constants ───────────────────────────────────────────────────────────


CARD_TEMPLATE_ID: Final[str] = "cloud_hitl_request_card_v1"
"""Stable identifier for this card template — never renamed (spec §2.4)."""

CARD_TEMPLATE_VERSION: Final[str] = "v1"
"""Driver of versioned dispatch in the webhook handler (spec §4.1)."""

DEFAULT_TIMEOUT_S: Final[int] = 1800
"""Default 30-min approval timeout per user-locked decision Q-B-3."""

MAX_QUESTION_TEXT_LEN: Final[int] = 2000
"""Hard cap on the B1 question text (Lark per-element soft cap; spec §2.3 B1).

Per workspace rule "No Silent Failures": questions at or above this cap
are rejected with :class:`ValueError`; truncating B1 would change the
question semantics."""

DEFAULT_POPOLAD_HOST: Final[str] = "127.0.0.1"
"""Default loopback host for the ``[Expand →]`` link (γ mode; spec §2.3 B2)."""

DEFAULT_POPOLAD_PORT: Final[int] = 8765
"""Default popolad HTTP port for the ``[Expand →]`` link."""

RESPONDER_POLICIES: Final[tuple[str, ...]] = ("single", "serial_two")
"""Allowed values for ``responder_policy`` (spec §2.4 + §3)."""

CARD_METADATA_KEYS: Final[tuple[str, ...]] = (
    "template_version",
    "template_id",
    "hitl_id",
    "task_id",
    "cursor_agent_id",
    "cursor_run_id",
    "idempotency_key",
    "expiration_at",
    "timeout_seconds",
    "responder_policy",
    "first_approver_open_id",
    "first_approver_at",
)
"""Stable list of ``card_metadata`` keys per spec §2.4 (12 entries).

The webhook handler dispatches on ``template_version`` and may rely on
this exact key set; renderers MUST emit all 12 (even when nullable) so
older + newer dispatchers stay compatible."""


# Header colors — drawn from the Lark v2 12-color palette per spec §2.1.
_HEADER_COLOR_PENDING: Final[str] = "blue"
_HEADER_COLOR_PENDING_SECOND: Final[str] = "wathet"
_HEADER_COLOR_TIMEOUT: Final[str] = "grey"
_HEADER_COLOR_BY_OPTION: Final[dict[str, str]] = {
    "approve": "green",
    "reject": "red",
    "custom": "yellow",
}


# ── Public dataclass (allowlist input — spec §6.1) ──────────────────────


@dataclass(frozen=True)
class CloudHITLCardInput:
    """Typed input for :func:`build_cloud_hitl_card` (allowlist pattern).

    Per spec §6.1 ("Allowlist, don't denylist"): the card builder
    accepts a typed dataclass with explicit fields; ad-hoc
    ``dict[str, Any]`` is forbidden so secrets cannot accidentally
    reach the rendered card.

    Attributes:
        hitl_id: 32-char hex UUID — primary key into ``popola_hitl``.
        task_id: popola task id (operator-recognisable from ``popola list``).
        question_text: verbatim agent-supplied question for B1; never
            truncated. Lengths ≥ :data:`MAX_QUESTION_TEXT_LEN` raise
            :class:`ValueError` at build time.
        prompt_body: free-form context summary for B2; truncated to
            :data:`LARK_NOTIFY_PROMPT_TRUNCATE` chars (=200) + ``…``
            suffix when longer.
        cursor_agent_id: Cursor Cloud Agent opaque id (``bc-...``);
            ``None`` when the caller is not a cloud agent (test harness).
        cursor_run_id: Cursor run id (``run-...``); ``None`` when absent.
        idempotency_key: opaque dedup key — accepted as raw hex digest
            or already prefixed ``sha256:<hex>``; the builder normalises
            to the spec §2.4 display form ``sha256:<first 16 hex chars>``.
        expiration_at: server-truth deadline (timezone-aware ``datetime``);
            rendered into B3 + ``card_metadata.expiration_at`` as ISO 8601 UTC.
        timeout_seconds: configurable approval window; defaults to
            :data:`DEFAULT_TIMEOUT_S` (1800 = 30 min, per Q-B-3).
        responder_policy: ``"single"`` (S1) or ``"serial_two"`` (S2);
            other values raise :class:`ValueError`.
        popolad_host / popolad_port: host + port for the ``[Expand →]``
            link in B2; default to loopback for γ mode.
        first_approver_open_id / first_approver_at: populated only after
            an S2 first approval (None on initial build).
        title_task_id_override: if set, used in the header title instead
            of ``task_id``; otherwise the title reads
            ``"PopolaLoom HITL — <task_id>"``.
    """

    hitl_id: str
    task_id: str
    question_text: str
    prompt_body: str
    cursor_agent_id: str | None
    cursor_run_id: str | None
    idempotency_key: str
    expiration_at: datetime
    timeout_seconds: int = DEFAULT_TIMEOUT_S
    responder_policy: str = "single"
    popolad_host: str = DEFAULT_POPOLAD_HOST
    popolad_port: int = DEFAULT_POPOLAD_PORT
    first_approver_open_id: str | None = None
    first_approver_at: datetime | None = None
    title_task_id_override: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.hitl_id, str) or not self.hitl_id:
            raise ValueError("hitl_id must be a non-empty string")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(self.question_text, str):
            raise TypeError(
                f"question_text must be str, got {type(self.question_text).__name__}"
            )
        if not isinstance(self.prompt_body, str):
            raise TypeError(
                f"prompt_body must be str, got {type(self.prompt_body).__name__}"
            )
        if len(self.question_text) >= MAX_QUESTION_TEXT_LEN:
            raise ValueError(
                f"question_text length {len(self.question_text)} >= "
                f"{MAX_QUESTION_TEXT_LEN} (B1 is never truncated; reject at "
                "builder boundary per spec §2.3 B1 / workspace rule "
                "'No Silent Failures')"
            )
        if self.responder_policy not in RESPONDER_POLICIES:
            raise ValueError(
                f"responder_policy {self.responder_policy!r} not in "
                f"{RESPONDER_POLICIES!r}"
            )
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be a positive int, got {self.timeout_seconds!r}"
            )
        if not isinstance(self.expiration_at, datetime):
            raise TypeError(
                f"expiration_at must be datetime, got {type(self.expiration_at).__name__}"
            )
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")


# ── Helpers ─────────────────────────────────────────────────────────────


def compute_idempotency_key(
    *,
    task_id: str,
    cursor_run_id: str | None,
    question_text: str,
) -> str:
    """Compute the opaque idempotency key (spec §6.2).

    Uses sha256 over ``f"{task_id}|{cursor_run_id or ''}|{question_text}"``
    truncated to the first 16 hex chars (= 64 bits) and prefixed with
    ``sha256:`` so consumers can grep for the format. The truncation is a
    deliberate dedup-vs-collision tradeoff per spec §6.2: ~1 collision per
    4 billion requests is acceptable for the 1-hour replay window, but the
    key is **never** suitable for cryptographic identity claims.

    Properties:
    - **One-way**: SHA-256 truncated to 64 bits is non-reversible.
    - **Stable**: same inputs always produce the same key.
    - **Bounded length**: ``len() == len("sha256:") + 16 == 23``.

    Args:
        task_id: popola task id.
        cursor_run_id: Cursor run id, or ``None`` (treated as empty).
        question_text: the agent-supplied prompt text.

    Returns:
        The ``"sha256:<16hex>"`` display-form key.
    """
    cursor_run_part = cursor_run_id if cursor_run_id is not None else ""
    raw = f"{task_id}|{cursor_run_part}|{question_text}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def _normalise_idempotency_key(raw: str) -> str:
    """Normalise ``raw`` to the spec §2.4 display form ``sha256:<16hex>``.

    Accepts either:
    - already-prefixed ``sha256:<hex>`` — re-truncated to 16 hex chars,
    - or a raw hex digest — prefixed and truncated.

    The normalisation is intentionally tolerant so the MCP tool can
    persist a 32-hex digest in SQLite ``metadata.idempotency_key`` while
    the card surface displays the shorter 16-hex form.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("idempotency_key must be a non-empty string")
    rest = raw[len("sha256:"):] if raw.startswith("sha256:") else raw
    if not rest:
        raise ValueError(f"idempotency_key {raw!r} has no hex body")
    return f"sha256:{rest[:16]}"


def _iso_utc(dt: datetime) -> str:
    """Render ``dt`` as ISO 8601 with millisecond precision in UTC."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="milliseconds")


def _human_remaining(deadline: datetime, *, now: datetime | None = None) -> str:
    """Render a coarse ``"<X>m <Y>s remaining"`` string for B3.

    Lark cards are not live, so this is rendered at build time only;
    the S3 timeout mutator re-renders B3 with the elapsed seconds when
    the watchdog fires.
    """
    current = now if now is not None else datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    remaining = (deadline - current).total_seconds()
    if remaining < 0:
        return "expired"
    minutes, seconds = divmod(int(remaining), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m remaining"
    if minutes > 0:
        return f"{minutes}m {seconds}s remaining"
    return f"{seconds}s remaining"


def _expand_url(host: str, port: int, hitl_id: str) -> str:
    """Build the popolad-served context URL for the ``[Expand →]`` link.

    Per spec §2.3 B2 + §6.4: defaults to the loopback host for γ mode;
    the page itself authenticates the operator (Lark cookie SSO or
    one-time token). The card never exposes the URL to anonymous users.
    """
    return f"http://{host}:{port}/hitl/cloud/context/{hitl_id}"


def _build_metadata(input_: CloudHITLCardInput) -> dict[str, Any]:
    """Build the ``card_metadata`` block per spec §2.4 (12 keys).

    Per spec §6.1 + workspace rule "No Silent Failures": the block lists
    the same 12 keys whether or not the optional ones (``cursor_agent_id``,
    ``cursor_run_id``, ``first_approver_*``) are populated — empty values
    are emitted as ``null`` so the webhook handler can dispatch on the
    full key set.
    """
    metadata: dict[str, Any] = {
        "template_version": CARD_TEMPLATE_VERSION,
        "template_id": CARD_TEMPLATE_ID,
        "hitl_id": input_.hitl_id,
        "task_id": input_.task_id,
        "cursor_agent_id": input_.cursor_agent_id,
        "cursor_run_id": input_.cursor_run_id,
        "idempotency_key": _normalise_idempotency_key(input_.idempotency_key),
        "expiration_at": _iso_utc(input_.expiration_at),
        "timeout_seconds": input_.timeout_seconds,
        "responder_policy": input_.responder_policy,
        "first_approver_open_id": input_.first_approver_open_id,
        "first_approver_at": (
            _iso_utc(input_.first_approver_at)
            if input_.first_approver_at is not None
            else None
        ),
    }
    return metadata


def _action_block(hitl_id: str) -> dict[str, Any]:
    """Build the A1 action block per spec §2.3 A1 (3 buttons)."""
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Approve"},
                "type": "primary",
                "value": {
                    "hitl_id": hitl_id,
                    "action": "approve",
                    "template_version": CARD_TEMPLATE_VERSION,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Reject"},
                "type": "danger",
                "value": {
                    "hitl_id": hitl_id,
                    "action": "reject",
                    "template_version": CARD_TEMPLATE_VERSION,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Custom…"},
                "type": "default",
                "value": {
                    "hitl_id": hitl_id,
                    "action": "custom",
                    "template_version": CARD_TEMPLATE_VERSION,
                },
                "behaviors": [
                    {"type": "open_input", "input_id": "custom_text"}
                ],
            },
        ],
    }


def _build_b1(question_text: str) -> dict[str, Any]:
    """Build B1 — verbatim question (no truncation, spec §2.3 B1)."""
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**Question**\n\n{question_text}",
        },
    }


def _build_b2(prompt_body: str, expand_url: str) -> dict[str, Any]:
    """Build B2 — truncated context + ``[Expand →]`` link (spec §2.3 B2)."""
    truncated = _truncate_prompt(prompt_body, LARK_NOTIFY_PROMPT_TRUNCATE)
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**Context**\n\n{truncated} [Expand →]({expand_url})",
        },
    }


def _build_b3(input_: CloudHITLCardInput, *, now: datetime | None = None) -> dict[str, Any]:
    """Build B3 — visible plain-identifier metadata footer (spec §2.3 B3).

    The body is plain-identifier-only and ends with the workspace-rule
    footer via :func:`footer_with_origin_note` so the rule
    "lark-cli 写入操作须追加来源标注" is honoured automatically.
    """
    expiration_iso = _iso_utc(input_.expiration_at)
    remaining = _human_remaining(input_.expiration_at, now=now)
    body = (
        f"`hitl_id`: `{input_.hitl_id}` · "
        f"`task_id`: `{input_.task_id}` · "
        f"`agent`: `{input_.cursor_agent_id or '-'}`\n"
        f"`run`: `{input_.cursor_run_id or '-'}` · "
        f"expires: `{expiration_iso}` (in {remaining})"
    )
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": footer_with_origin_note(body),
        },
    }


# ── Public builder ──────────────────────────────────────────────────────


def build_cloud_hitl_card(
    input_: CloudHITLCardInput,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the v1 cloud HITL card envelope (spec §2.3, §2.4).

    Returns the 4-block body envelope (B1 + B2 + B3 + A1) wrapped in the
    ``schema: 2.0`` Lark card v2 frame, with header template ``"blue"``
    (Pending) and the 12-key ``card_metadata`` block per spec §2.4.

    Args:
        input_: typed allowlist input (per spec §6.1).
        now: optional override for "current time" (test hook); defaults
            to :func:`datetime.now` in UTC.

    Returns:
        ``dict[str, Any]`` ready to ``json.dumps`` and pass to
        ``lark-cli im +send --card``.

    Raises:
        ValueError: if ``input_.question_text`` is at or above
            :data:`MAX_QUESTION_TEXT_LEN` (validated in
            :class:`CloudHITLCardInput.__post_init__`).
    """
    title_task = input_.title_task_id_override or input_.task_id
    expand = _expand_url(input_.popolad_host, input_.popolad_port, input_.hitl_id)
    elements: list[dict[str, Any]] = [
        _build_b1(input_.question_text),
        _build_b2(input_.prompt_body, expand),
        _build_b3(input_, now=now),
        _action_block(input_.hitl_id),
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"PopolaLoom HITL — {title_task}",
            },
            "subtitle": {
                "tag": "plain_text",
                "content": "Cloud agent approval",
            },
            "template": _HEADER_COLOR_PENDING,
            "ud_icon": {"token": "approval_outlined"},
        },
        "body": {"elements": elements},
        "card_metadata": _build_metadata(input_),
    }


# ── State-machine mutators (spec §3) ────────────────────────────────────


def _replace_or_append_div(
    elements: list[dict[str, Any]],
    *,
    index: int,
    new_div: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace ``elements[index]`` with ``new_div`` if a ``div`` exists there;
    otherwise append. Pure helper for mutator composability."""
    out = list(elements)
    if 0 <= index < len(out) and out[index].get("tag") == "div":
        out[index] = new_div
        return out
    out.append(new_div)
    return out


def _strip_action_block(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``elements`` with the ``action`` block removed."""
    return [e for e in elements if e.get("tag") != "action"]


def _update_metadata(
    card: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Return a deep copy of ``card`` with ``card_metadata`` merged in.

    Mutators must NOT mutate the caller's card (callers may keep a
    reference for audit logs); a deep copy is the simplest way to
    guarantee that property without per-field cloning logic.
    """
    out = copy.deepcopy(card)
    metadata = out.setdefault("card_metadata", {})
    if not isinstance(metadata, dict):  # pragma: no cover - defensive
        raise TypeError(
            f"card_metadata must be dict, got {type(metadata).__name__}"
        )
    metadata.update(updates)
    return out


def mutate_card_for_pending_second_approver(
    card: dict[str, Any],
    first_approver: str,
    *,
    first_approver_at: datetime | None = None,
) -> dict[str, Any]:
    """S2 mid-state mutator (spec §3.2).

    Called after the first approver clicks: flips the header to ``wathet``,
    stamps ``card_metadata.first_approver_open_id`` + ``first_approver_at``,
    and updates B3 with the badge ``"1/2 approved by <U1> — waiting for 2nd"``.
    The action block stays so a second approver can finalise.

    Args:
        card: the previous card payload (must carry ``card_metadata``;
            typically the output of :func:`build_cloud_hitl_card`).
        first_approver: ``open_id`` of the first approver (validated by
            the webhook handler against the allowlist before this is called).
        first_approver_at: optional timestamp; defaults to ``now(UTC)``.

    Returns:
        A new card payload (deep-copied; the input is not mutated).
    """
    when = first_approver_at if first_approver_at is not None else datetime.now(UTC)
    out = _update_metadata(
        card,
        {
            "first_approver_open_id": first_approver,
            "first_approver_at": _iso_utc(when),
        },
    )
    header = out.setdefault("header", {})
    header["template"] = _HEADER_COLOR_PENDING_SECOND
    elements: list[dict[str, Any]] = list(out.get("body", {}).get("elements", []))
    badge_text = (
        f"⏳ **1/2 approved by `{first_approver}` at "
        f"`{_iso_utc(when)}`** — waiting for 2nd approver."
    )
    badge_div: dict[str, Any] = {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": footer_with_origin_note(badge_text),
        },
    }
    div_indices = [i for i, e in enumerate(elements) if e.get("tag") == "div"]
    if len(div_indices) >= 3:
        elements = _replace_or_append_div(
            elements, index=div_indices[2], new_div=badge_div
        )
    else:
        elements.append(badge_div)
    out.setdefault("body", {})["elements"] = elements
    return out


def mutate_card_for_answered(
    card: dict[str, Any],
    answered_by: str,
    answered_at: datetime,
    *,
    option_id: str = "approve",
    channel: str = "lark",
    reason: str | None = None,
) -> dict[str, Any]:
    """S1 / S2 final-state mutator (spec §3.1).

    Flips the header colour according to the chosen option, replaces B3
    with the answered-by note, and removes the action block so the card
    can no longer be re-clicked.

    Args:
        card: the previous card payload.
        answered_by: ``open_id`` of the responder (S1: any allowed; S2: the
            second approver, who must differ from
            ``card_metadata.first_approver_open_id``).
        answered_at: timezone-aware timestamp the answer was recorded.
        option_id: one of ``approve`` / ``reject`` / ``custom`` (drives
            header colour); other values fall back to the pending colour
            with an explicit ``"option:<id>"`` badge.
        channel: which channel the click came from (``"lark"`` /
            ``"api"`` / etc.); rendered in the badge for audit clarity.
        reason: optional free-text reason (e.g. ``custom`` answer payload);
            truncated to 200 chars in the badge to bound card size.

    Returns:
        A new card payload (deep-copied; the input is not mutated).
    """
    color = _HEADER_COLOR_BY_OPTION.get(option_id, _HEADER_COLOR_PENDING)
    out = copy.deepcopy(card)
    header = out.setdefault("header", {})
    header["template"] = color

    answered_iso = _iso_utc(answered_at)
    badge_emoji = {
        "approve": "✅",
        "reject": "❌",
        "custom": "📝",
    }.get(option_id, "•")
    base_line = (
        f"{badge_emoji} **Answered** by `{answered_by}` at "
        f"`{answered_iso}` via `{channel}` "
        f"(option: `{option_id}`)"
    )
    if reason:
        truncated_reason = _truncate_prompt(reason, 200)
        base_line = f"{base_line}\n\n> {truncated_reason}"
    badge_div = {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": footer_with_origin_note(base_line),
        },
    }

    elements: list[dict[str, Any]] = list(out.get("body", {}).get("elements", []))
    elements = _strip_action_block(elements)
    div_indices = [i for i, e in enumerate(elements) if e.get("tag") == "div"]
    if len(div_indices) >= 3:
        elements = _replace_or_append_div(
            elements, index=div_indices[2], new_div=badge_div
        )
    else:
        elements.append(badge_div)
    out.setdefault("body", {})["elements"] = elements
    return out


def mutate_card_for_timeout(
    card: dict[str, Any],
    *,
    timed_out_at: datetime | None = None,
) -> dict[str, Any]:
    """S3 timeout-rejection mutator (spec §3.3).

    Flips the header to ``grey``, replaces B3 with the timed-out badge,
    and removes the action block. The MCP tool returns ``error.code:
    "timeout"`` separately (handled in T2.2.1 / T2.1.1 — this mutator only
    flips the visible card state).

    Args:
        card: the previous card payload.
        timed_out_at: optional timestamp the timeout fired; defaults to
            ``now(UTC)``.

    Returns:
        A new card payload (deep-copied; the input is not mutated).
    """
    when = timed_out_at if timed_out_at is not None else datetime.now(UTC)
    when_iso = _iso_utc(when)
    timeout_seconds = (
        card.get("card_metadata", {}).get("timeout_seconds", DEFAULT_TIMEOUT_S)
    )
    out = copy.deepcopy(card)
    header = out.setdefault("header", {})
    header["template"] = _HEADER_COLOR_TIMEOUT

    badge_text = (
        f"⏰ **Timed out** — auto-rejected at `{when_iso}` after "
        f"`{timeout_seconds}`s of waiting."
    )
    badge_div = {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": footer_with_origin_note(badge_text),
        },
    }
    elements: list[dict[str, Any]] = list(out.get("body", {}).get("elements", []))
    elements = _strip_action_block(elements)
    div_indices = [i for i, e in enumerate(elements) if e.get("tag") == "div"]
    if len(div_indices) >= 3:
        elements = _replace_or_append_div(
            elements, index=div_indices[2], new_div=badge_div
        )
    else:
        elements.append(badge_div)
    out.setdefault("body", {})["elements"] = elements
    return out
