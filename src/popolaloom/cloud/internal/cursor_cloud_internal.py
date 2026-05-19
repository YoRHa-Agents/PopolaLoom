"""EXPERIMENTAL Connect-RPC adapter for Cursor's BackgroundComposerService.

Per :file:`.local/.agent/active/v1.0.0-ga/DECISIONS.md`:

- **Q-13 (LOCKED)**: ``--auth-mode=session-jwt`` is opt-in default-OFF;
  this module is only consumed when that flag is set.
- **Q-16 (LOCKED)**: JSON-over-Connect-RPC wire format. We hand-roll the
  request body as a dict (matching the 74-field
  ``StartBackgroundComposerFromSnapshotRequest``); no protobuf
  compilation required.
- **Q-22 (LOCKED)**: This module is **experimental**, NOT part of the
  v1.x SemVer stability surface. The wire format is reverse-engineered
  from the ``cursor-agent`` binary's protobuf descriptor (see
  ``.local/feedbacks/feedback_for_v1.0.0-pre.1.md`` §3 for the
  research lineage); Cursor may change it without notice. When path-B
  breaks, callers MUST surface a friendly hint pointing at
  ``--auth-mode=rest`` as the working fallback.

Endpoint surface (all calls):

- Base URL: ``https://api2.cursor.sh``
- Path:     ``/aiserver.v1.BackgroundComposerService/<MethodName>``
- Method:   ``POST``
- Headers:  ``Authorization: Bearer <accessToken>``,
            ``Content-Type: application/json``,
            ``Connect-Protocol-Version: 1``
- Body:     JSON dict matching the corresponding Connect Protocol
            request message.

Methods exposed (only what dispatch actually needs):

- :meth:`CursorCloudInternalClient.start_background_composer_from_snapshot`
  — corresponds to RPC ``StartBackgroundComposerFromSnapshot``.

Field names below mirror the protobuf descriptor verbatim (snake_case
with the underscore preserved) so that a protobuf-compiled migration
would be a pure encode-format swap, not a field-rename refactor.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from popolaloom.cloud.internal.jwt_auth import (
    JWTAuthError,
    JWTBundle,
    _is_jwt_expired,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api2.cursor.sh"
SERVICE_PATH = "/aiserver.v1.BackgroundComposerService"
DEFAULT_TIMEOUT_S = 60.0

# v1.5.0 — escape-hatch settings for the path-B body when the upstream
# Connect-RPC server rejects the v1.5.0 default shape. See PLAN §A
# (Phase A) + §"关键风险" §A. Three tiers:
#
# * ``"machine"`` (default) — body emits ``env={"type":"machine","name":<X>}``
#   (mirrors the REST :class:`popolaloom.adapters.cursor_cloud.AgentEnv`
#   shape). The server-side wire spec for this field on path-B has not
#   been empirically verified; if it rejects this shape, the operator
#   must re-dispatch with one of the two fallback modes below.
# * ``"label"`` — body omits ``env`` entirely; instead the
#   ``snapshot_name_or_id`` is normalized to ``<owner>/<repo>`` so the
#   worker's auto-label matcher can claim it. Use when ``"machine"``
#   triggers a 400 ``invalid_argument`` envelope on ``env``.
# * ``"none"`` — body omits ``env`` AND skips ``snapshot_name_or_id``
#   normalization; falls back to the v1.3.0 behaviour of relying purely
#   on ``use_private_worker=True``.
#
# Per the No-Silent-Fallback invariant, popola does NOT auto-shift
# between modes — the operator opts in via ``--cli-flag env_emit_mode=<X>``
# on the next dispatch attempt.
ENV_EMIT_MODE_MACHINE = "machine"
ENV_EMIT_MODE_LABEL = "label"
ENV_EMIT_MODE_NONE = "none"
_VALID_ENV_EMIT_MODES = frozenset(
    {ENV_EMIT_MODE_MACHINE, ENV_EMIT_MODE_LABEL, ENV_EMIT_MODE_NONE}
)

# Q-19: REST-rejection contract — these flag → field mappings are ONLY
# valid on the path-B (RPC) transport. The CLI layer rejects them up
# front when --auth-mode=rest, so this module never sees them on REST.
AgentMode = Literal[
    "AGENT_MODE_AGENT",
    "AGENT_MODE_ASK",
    "AGENT_MODE_PLAN",
    "AGENT_MODE_DEBUG",
    "AGENT_MODE_TRIAGE",
    "AGENT_MODE_PROJECT",
    "AGENT_MODE_MULTITASK",
]
"""Agent mode enum (Connect-RPC field ``agent_mode``).

Reverse-engineered from the cursor-agent protobuf descriptor; values are
verbatim per feedback_for_v1.0.0-pre.1.md §3.
"""

EffortMode = Literal[
    "EFFORT_MODE_LOW",
    "EFFORT_MODE_MEDIUM",
    "EFFORT_MODE_HIGH",
]
"""Effort mode enum (Connect-RPC field ``effort_mode``, proto field 75)."""

StartingMessageType = Literal[
    "STARTING_MESSAGE_TYPE_USER_MESSAGE",
    "STARTING_MESSAGE_TYPE_PLAN_START",
    "STARTING_MESSAGE_TYPE_PLAN_EXECUTE",
]
"""Starting message type enum (Connect-RPC field ``starting_message_type``)."""

ThinkingLevel = Literal[
    "THINKING_LEVEL_LOW",
    "THINKING_LEVEL_MEDIUM",
    "THINKING_LEVEL_HIGH",
]
"""Thinking level enum (model_details.thinking_level)."""


def _to_camel(key: str) -> str:
    """Translate snake_case identifier to camelCase (``a_b_c`` → ``aBC``).

    Single-word keys pass through unchanged. Empty string passes through.
    Preserves leading underscores (idiomatic ``__dunder``-style keys are
    NOT used in the body, but defending against them keeps the helper
    safe to reuse in tests).
    """
    if not key:
        return key
    parts = key.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:] if part)


def _camelize_keys(obj: Any) -> Any:
    """Recursively rename ``dict`` keys snake_case → camelCase.

    Lists are recursed. Non-dict non-list values pass through unchanged.
    Used by :func:`build_start_composer_request` to flip the body to
    the Connect-Protocol JSON wire format Cursor's server expects (per
    feedback_for_v1.2.0.md §2 "实测 wire 规格" — the snake_case body
    was rejected with 400 invalid_argument; camelCase + full field set
    returns 200).
    """
    if isinstance(obj, dict):
        return {_to_camel(str(k)): _camelize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camelize_keys(item) for item in obj]
    return obj


_USER_FACING_TO_AGENT_MODE: dict[str, AgentMode] = {
    "agent": "AGENT_MODE_AGENT",
    "ask": "AGENT_MODE_ASK",
    "plan": "AGENT_MODE_PLAN",
    "debug": "AGENT_MODE_DEBUG",
    "triage": "AGENT_MODE_TRIAGE",
    "project": "AGENT_MODE_PROJECT",
    "multitask": "AGENT_MODE_MULTITASK",
}
_USER_FACING_TO_EFFORT_MODE: dict[str, EffortMode] = {
    "low": "EFFORT_MODE_LOW",
    "medium": "EFFORT_MODE_MEDIUM",
    "high": "EFFORT_MODE_HIGH",
}
_USER_FACING_TO_THINKING_LEVEL: dict[str, ThinkingLevel] = {
    "low": "THINKING_LEVEL_LOW",
    "medium": "THINKING_LEVEL_MEDIUM",
    "high": "THINKING_LEVEL_HIGH",
}


def user_mode_to_agent_mode(user_value: str) -> AgentMode:
    """Translate user-facing ``--mode`` value to the proto enum.

    Raises:
        ValueError: when the value is not in the supported set; carries
            a list of valid values for the operator-facing error message.
    """
    key = user_value.strip().lower()
    if key not in _USER_FACING_TO_AGENT_MODE:
        valid = sorted(_USER_FACING_TO_AGENT_MODE.keys())
        raise ValueError(
            f"--mode={user_value!r} is not one of {valid}; "
            f"(--mode={user_value!r} 必须是 {valid} 之一)"
        )
    return _USER_FACING_TO_AGENT_MODE[key]


def user_effort_to_effort_mode(user_value: str) -> EffortMode:
    """Translate user-facing ``--effort`` value to the proto enum."""
    key = user_value.strip().lower()
    if key not in _USER_FACING_TO_EFFORT_MODE:
        valid = sorted(_USER_FACING_TO_EFFORT_MODE.keys())
        raise ValueError(
            f"--effort={user_value!r} is not one of {valid}; "
            f"(--effort={user_value!r} 必须是 {valid} 之一)"
        )
    return _USER_FACING_TO_EFFORT_MODE[key]


def user_thinking_level_to_proto(user_value: str) -> ThinkingLevel:
    """Translate user-facing ``--thinking-level`` value to the proto enum."""
    key = user_value.strip().lower()
    if key not in _USER_FACING_TO_THINKING_LEVEL:
        valid = sorted(_USER_FACING_TO_THINKING_LEVEL.keys())
        raise ValueError(
            f"--thinking-level={user_value!r} is not one of {valid}; "
            f"(--thinking-level={user_value!r} 必须是 {valid} 之一)"
        )
    return _USER_FACING_TO_THINKING_LEVEL[key]


class CursorCloudInternalError(RuntimeError):
    """Raised on path-B failure (HTTP / RPC / decode / wire-format).

    Carries a structured ``hint`` field with a bilingual operator-facing
    message that points at ``--auth-mode=rest`` as the supported fallback
    when path-B is unhealthy (per Q-22 stability commitment of NONE).

    v1.3.0 P4: Extended with structured Connect-Protocol envelope fields
    (``connect_code``, ``connect_message``, ``details_summary``) and an
    ``error_kind`` discriminator so operators can distinguish
    ``path_b_rpc_400_invalid_argument`` from ``path_b_rpc_404`` (which
    were both surfaced identically pre-1.3.0 — see
    feedback_for_v1.2.0.md §2). The ``__str__`` override appends these
    fields when present so any caller that simply logs ``str(exc)`` gets
    actionable diagnostics out of the box.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status_code: int | None = None,
        connect_code: str | None = None,
        connect_message: str | None = None,
        details_summary: str | None = None,
        error_kind: str = "path_b_rpc_other",
    ) -> None:
        super().__init__(message)
        self.hint = hint or ""
        self.status_code = status_code
        self.connect_code = connect_code
        self.connect_message = connect_message
        self.details_summary = details_summary
        self.error_kind = error_kind

    def __str__(self) -> str:
        base = super().__str__()
        suffix_parts: list[str] = []
        if self.error_kind and self.error_kind != "path_b_rpc_other":
            suffix_parts.append(f"kind={self.error_kind}")
        if self.connect_code:
            suffix_parts.append(f"connect_code={self.connect_code}")
        if self.connect_message:
            suffix_parts.append(f"connect_message={self.connect_message!r}")
        if self.details_summary:
            trimmed = self.details_summary[:200]
            suffix_parts.append(f"details={trimmed!r}")
        if not suffix_parts:
            return base
        return f"{base} [{', '.join(suffix_parts)}]"


@dataclass(frozen=True, slots=True)
class StartComposerOutcome:
    """Decoded response envelope from ``StartBackgroundComposerFromSnapshot``.

    The full Connect-RPC response carries dozens of fields; we surface
    only what dispatch actually needs:

    Attributes:
        background_composer_id: The new ``bc-...`` id (analogous to the
            REST adapter's ``cursor_agent_id``); used by the daemon
            poller and by attach.
        dashboard_url: A best-effort ``https://cursor.com/agents/<id>``
            URL the CLI surfaces to the operator. ``None`` when the
            response did not carry enough info to compose it.
        initial_run_id: v1.5.0 — the ``initialRunId`` / ``initial_run_id``
            field Cursor returns alongside ``composer.bcId`` (v1.3.0+
            response shape). Surfaced so the daemon supervisor can seed
            ``TaskHandle.cursor_run_id`` at queue time, enabling SSE
            correlation without an extra round-trip. ``None`` when the
            response did not include the field (older server build, or
            the dispatch did not start a run).
        raw_response: The full decoded JSON envelope, kept for tests +
            future field surfacing without a code change.
    """

    background_composer_id: str
    dashboard_url: str | None
    raw_response: dict[str, Any]
    initial_run_id: str | None = None


def build_start_composer_request(
    *,
    prompt: str,
    repo_url: str,
    starting_ref: str = "main",
    model_name: str | None = None,
    model_id_override: str | None = None,
    max_mode: bool | None = None,
    thinking_level: str | None = None,
    agent_mode: str | None = None,
    effort_mode: str | None = None,
    time_budget_s: int | None = None,
    long_running: bool | None = None,
    starting_message_type: str | None = None,
    auto_proceed_after_planning: bool | None = None,
    snapshot_name_or_id: str | None = None,
    devcontainer_url: str | None = None,
    devcontainer_ref: str | None = None,
    snapshot_workspace_root_path: str = "/workspace",
    auto_branch: bool = True,
    auto_create_pr: bool = False,
    work_on_current_branch: bool = False,
    skip_reviewer_request: bool = False,
    return_immediately: bool = True,
    use_private_worker: bool = True,
    target_machine_name: str | None = None,
    env_emit_mode: str = ENV_EMIT_MODE_MACHINE,
    source: str = "BACKGROUND_COMPOSER_SOURCE_WEBSITE",
    bc_id: str | None = None,
    add_initial_message_to_responses: bool = True,
    conversation_history: list[dict[str, Any]] | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``StartBackgroundComposerFromSnapshot``.

    Per Q-16 (LOCKED) we hand-roll the body as a dict matching the
    proto field names. The 74-field ``StartBackgroundComposerFromSnapshotRequest``
    is mostly defaults; this builder only sets the fields the CLI flags
    map to (per feedback_for_v1.0.0-pre.1.md §4.1 table).

    Args:
        prompt: The user prompt (required; matches the REST ``prompt.text``).
        repo_url: The dispatch-target repository URL (required).
        starting_ref: Git ref to start from (default ``main``).
        model_name: Model id (e.g. ``"gpt-5.5"``); maps to
            ``model_details.model_name``.
        max_mode: When ``True``, sets ``model_details.max_mode = true``
            (max-context mode).
        thinking_level: User-facing ``--thinking-level`` value
            (``low|medium|high``); translated to the proto enum and
            placed in ``model_details.thinking_level``.
        agent_mode: User-facing ``--mode`` value
            (``agent|ask|plan|debug|...``); translated to the proto
            ``agent_mode`` enum.
        effort_mode: User-facing ``--effort`` value (``low|medium|high``);
            translated to the proto ``effort_mode`` enum.
        time_budget_s: ``time_budget_seconds`` field. ``time_budget_ms``
            is derived as ``s * 1000`` for forward-compat.
        long_running: When ``True``, sets ``long_running_agent_mode = true``.
        starting_message_type: User-facing string
            (``"user-message"|"plan-start"|"plan-execute"``); translated
            to the proto enum.
        auto_proceed_after_planning: When ``True``, sets the same-named
            proto field. Typically paired with ``starting_message_type=
            "plan-start"`` so the agent moves from PLAN to EXECUTE
            without an interactive confirm.
        snapshot_name_or_id: v1.3.0 P5 (feedback §2) — Cursor BC
            ``snapshotNameOrId`` field. Wire format expects
            ``<host>/<owner>/<repo>`` (NO ``https://`` prefix, NO
            ``.git`` suffix). Defaults to deriving from ``repo_url``.
        devcontainer_url: v1.3.0 P5 — devcontainer starting point URL
            (full ``https://...``); defaults to ``repo_url``.
        devcontainer_ref: v1.3.0 P5 — devcontainer git ref; defaults to
            ``starting_ref``.
        snapshot_workspace_root_path: v1.3.0 P5 — defaults to
            ``"/workspace"`` (matches Cursor BC default).
        auto_branch: v1.3.0 P5 — defaults to ``True``.
        return_immediately: v1.3.0 P5 — defaults to ``True``.
        use_private_worker: v1.3.0 P5 — defaults to ``True`` (self-hosted
            worker preference). Note: per feedback §2, Cursor server
            currently routes non-GitHub repos to the public pool
            regardless of this flag (server-side hard constraint).
        target_machine_name: v1.5.0 (feedback_for_v1.4.0 §1 task #2) —
            when non-empty AND ``env_emit_mode == "machine"``, emit
            ``env = {"type": "machine", "name": <target_machine_name>}``
            on the request body so Cursor's BackgroundComposerService
            routes the run directly to the named self-hosted worker.
            Mirrors the REST :class:`AgentEnv` shape used by
            ``cursor_cloud.py`` for the v1 ``POST /v1/agents`` flow.
        env_emit_mode: v1.5.0 escape hatch (PLAN §A / §"关键风险"
            §A) — one of :data:`ENV_EMIT_MODE_MACHINE` (default),
            :data:`ENV_EMIT_MODE_LABEL`, or :data:`ENV_EMIT_MODE_NONE`.
            ``"machine"`` emits ``env``; ``"label"`` drops ``env`` and
            normalizes ``snapshot_name_or_id`` to ``<owner>/<repo>``;
            ``"none"`` drops both. Operator-facing toggle via
            ``--cli-flag env_emit_mode=label`` when the default shape
            is rejected by the upstream server. Invalid values raise
            :class:`ValueError` (No Silent Failures).
        auto_create_pr: v1.5.0 — Path-B body ``auto_create_pr`` field
            (mirror of the REST adapter's same-named knob). Defaults
            to ``False`` so a JWT-direct dispatch does NOT spawn a PR
            unless the operator opts in via ``--auto-create-pr``.
        work_on_current_branch: v1.5.0 — Path-B body
            ``work_on_current_branch`` field; when ``True`` instructs
            the worker to skip auto-branch creation and operate on the
            cwd's current ref. Used by ``--work-on-current-branch`` to
            satisfy feedback G4 ("跳过 git 分支 / PR 相关操作").
        skip_reviewer_request: v1.5.0 — Path-B body
            ``skip_reviewer_request`` field; suppresses the auto
            reviewer request on the resulting PR. Pairs with
            ``auto_create_pr`` for the "create PR but don't ping
            reviewers" workflow.
        model_id_override: v1.5.0 escape hatch (PLAN §"关键风险" §B) —
            when non-empty, overrides ``model_details.model_name`` with
            the supplied id. Required for the GPT-5.5 dual-naming case
            where the REST gateway accepts ``"gpt-5.5"`` but the
            cursor-agent CLI expects ``"gpt-5.5-high"`` (or vice-versa
            on path-B). Operator-facing toggle via
            ``--cli-flag model_id_override=<id>``. Per the
            No-Silent-Fallback invariant, popola does NOT auto-switch
            id forms — the operator opts in.
        source: v1.3.0 P5 — defaults to
            ``"BACKGROUND_COMPOSER_SOURCE_WEBSITE"`` (matches the
            successful reverse-engineered wire-spec).
        bc_id: v1.3.0 P5 — client-provided BC id; defaults to a fresh
            ``f"bc-{uuid.uuid4()}"`` so each dispatch carries a stable
            client correlator.
        add_initial_message_to_responses: v1.3.0 P5 — defaults to
            ``True``.
        conversation_history: v1.3.0 P5 — initial message list; defaults
            to ``[{"text": prompt, "type": "MESSAGE_TYPE_HUMAN", "richText": "{}"}]``.
        extras: Optional verbatim extras merged into the body shallow.
            Used by tests + future flag additions; should NOT be used
            for new operator-facing controls (those should add an
            explicit kwarg here).

    Returns:
        A JSON-serialisable dict ready to be sent as the RPC body. Per
        v1.3.0 P5 the dict keys are camelCase (Connect-Protocol JSON wire
        format Cursor's server requires); the Python-side construction
        remains snake_case for readability, and ``_camelize_keys`` is
        applied once at the end.

    Raises:
        ValueError: when an enum-valued kwarg has an unknown user value.
            The error message lists the valid values (No Silent Failures).
    """
    if not prompt:
        raise ValueError("prompt is required")
    if not repo_url:
        raise ValueError("repo_url is required")

    normalized_env_emit_mode = (env_emit_mode or "").strip().lower()
    if normalized_env_emit_mode not in _VALID_ENV_EMIT_MODES:
        valid = sorted(_VALID_ENV_EMIT_MODES)
        raise ValueError(
            f"env_emit_mode={env_emit_mode!r} not in {valid}; "
            f"(env_emit_mode 必须是 {valid} 之一)"
        )

    # Helper: snake_case normalized snapshot_name_or_id (no scheme, no .git).
    def _normalize_snapshot(value: str) -> str:
        derived = value
        for prefix in ("https://", "http://"):
            if derived.startswith(prefix):
                derived = derived[len(prefix):]
                break
        if derived.endswith(".git"):
            derived = derived[: -len(".git")]
        return derived

    if snapshot_name_or_id is None:
        snapshot_name_or_id = _normalize_snapshot(repo_url)
    elif normalized_env_emit_mode == ENV_EMIT_MODE_LABEL:
        # v1.5.0 — when env is dropped, the worker matches against the
        # snapshot label; renormalize to <owner>/<repo> for parity with
        # the v1.3.0 auto-label match logic.
        snapshot_name_or_id = _normalize_snapshot(snapshot_name_or_id)
    if devcontainer_url is None:
        devcontainer_url = repo_url
    if devcontainer_ref is None:
        devcontainer_ref = starting_ref
    if bc_id is None:
        bc_id = f"bc-{uuid.uuid4()}"
    if conversation_history is None:
        conversation_history = [
            {"text": prompt, "type": "MESSAGE_TYPE_HUMAN", "richText": "{}"},
        ]

    body: dict[str, Any] = {
        "prompt": prompt,
        "repos": [{"url": repo_url, "starting_ref": starting_ref}],
        "snapshot_name_or_id": snapshot_name_or_id,
        "devcontainer_starting_point": {
            "url": devcontainer_url,
            "ref": devcontainer_ref,
        },
        "repository_info": {},
        "snapshot_workspace_root_path": snapshot_workspace_root_path,
        "auto_branch": bool(auto_branch),
        "return_immediately": bool(return_immediately),
        "repo_url": repo_url,
        "conversation_history": conversation_history,
        "source": source,
        "bc_id": bc_id,
        "add_initial_message_to_responses": bool(add_initial_message_to_responses),
        "use_private_worker": bool(use_private_worker),
    }

    # v1.5.0 — Path-B "skip branch / PR" knobs. Only emit fields when the
    # caller opts in (True for the bool knobs, non-empty for the env).
    if auto_create_pr:
        body["auto_create_pr"] = True
    if work_on_current_branch:
        body["work_on_current_branch"] = True
    if skip_reviewer_request:
        body["skip_reviewer_request"] = True

    # v1.5.0 — emit ``env`` for worker routing ONLY in the "machine" mode.
    # Modes "label" and "none" deliberately omit ``env`` so the operator
    # has an escape hatch when the upstream server rejects the
    # ``AgentEnv``-shaped body on path-B. Per the No-Silent-Fallback
    # invariant, popola does NOT auto-shift between modes.
    if (
        normalized_env_emit_mode == ENV_EMIT_MODE_MACHINE
        and target_machine_name
        and target_machine_name.strip()
    ):
        body["env"] = {
            "type": "machine",
            "name": target_machine_name.strip(),
        }

    model_details: dict[str, Any] = {}
    # v1.5.0 — model_id_override wins over model_name when both are set.
    resolved_model_name: str | None = None
    if model_id_override and model_id_override.strip():
        resolved_model_name = model_id_override.strip()
    elif model_name:
        resolved_model_name = model_name
    if resolved_model_name:
        model_details["model_name"] = resolved_model_name
    if max_mode is not None:
        model_details["max_mode"] = bool(max_mode)
    if thinking_level is not None:
        model_details["thinking_level"] = user_thinking_level_to_proto(thinking_level)
    if model_details:
        body["model_details"] = model_details

    if agent_mode is not None:
        body["agent_mode"] = user_mode_to_agent_mode(agent_mode)
    if effort_mode is not None:
        body["effort_mode"] = user_effort_to_effort_mode(effort_mode)

    if time_budget_s is not None:
        if time_budget_s < 0:
            raise ValueError(
                f"time_budget_s must be non-negative, got {time_budget_s}"
            )
        body["time_budget_seconds"] = int(time_budget_s)
        body["time_budget_ms"] = int(time_budget_s) * 1000

    if long_running is not None:
        body["long_running_agent_mode"] = bool(long_running)

    if starting_message_type is not None:
        smt_map = {
            "user-message": "STARTING_MESSAGE_TYPE_USER_MESSAGE",
            "plan-start": "STARTING_MESSAGE_TYPE_PLAN_START",
            "plan-execute": "STARTING_MESSAGE_TYPE_PLAN_EXECUTE",
        }
        key = starting_message_type.strip().lower()
        if key not in smt_map:
            valid = sorted(smt_map.keys())
            raise ValueError(
                f"starting_message_type={starting_message_type!r} not in {valid}"
            )
        body["starting_message_type"] = smt_map[key]

    if auto_proceed_after_planning is not None:
        body["auto_proceed_after_planning"] = bool(auto_proceed_after_planning)

    if extras:
        for k, v in extras.items():
            if k not in body:
                body[k] = v
    camelized: dict[str, Any] = _camelize_keys(body)
    return camelized


def _extract_connect_error_envelope(
    resp: httpx.Response,
) -> tuple[str | None, str | None, str | None]:
    """Best-effort Connect-Protocol error envelope extraction.

    Connect-Protocol JSON error responses carry a top-level
    ``{"code": "<short_token>", "message": "<text>", "details": [...]}``
    envelope. We extract ``(code, message, details_summary)`` so the
    raised :class:`CursorCloudInternalError` can surface the real
    upstream failure reason (per feedback_for_v1.2.0.md §2; the
    pre-1.3.0 ``_post_rpc`` was mis-labeling a 400 ``invalid_argument``
    "At least one model details is required" envelope as a 404
    "method not found").

    Returns ``(connect_code, connect_message, details_summary)``; any
    failure to parse JSON / unexpected structure returns
    ``(None, None, None)`` so callers fall through to the legacy hint
    without crashing.
    """
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return (None, None, None)
    if not isinstance(body, dict):
        return (None, None, None)
    raw_code = body.get("code")
    code = raw_code if isinstance(raw_code, str) else None
    raw_message = body.get("message")
    message = raw_message if isinstance(raw_message, str) else None
    details = body.get("details")
    summary_chunks: list[str] = []
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            debug = item.get("debug")
            if isinstance(debug, dict):
                nested = debug.get("details")
                if isinstance(nested, dict):
                    detail_str = nested.get("detail")
                    if isinstance(detail_str, str) and detail_str:
                        summary_chunks.append(detail_str)
                        continue
            t = item.get("type")
            if isinstance(t, str):
                summary_chunks.append(t)
    summary = " | ".join(summary_chunks) if summary_chunks else None
    return (code, message, summary)


class CursorCloudInternalClient:
    """Thin Connect-RPC over JSON client for Cursor's BackgroundComposerService.

    EXPERIMENTAL — see module docstring for stability commitment (NONE
    per Q-22). Callers MUST handle :class:`CursorCloudInternalError`
    and provide a graceful fallback (typically: print a hint pointing
    at ``--auth-mode=rest`` and exit non-zero).

    Args:
        bundle: A loaded :class:`JWTBundle` (typically from
            :func:`popolaloom.cloud.internal.jwt_auth.load_jwt_bundle`).
        base_url: Override for the Cursor API base URL (defaults to
            :data:`DEFAULT_BASE_URL`); env-var override at the CLI
            layer is the supported way to flip this for tests.
        http_client: Override for the underlying ``httpx.Client`` (used
            by tests to inject a ``MockTransport``).
        timeout_s: Per-call timeout in seconds.
    """

    def __init__(
        self,
        bundle: JWTBundle,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._bundle = bundle
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_s)

    def __enter__(self) -> CursorCloudInternalClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying httpx client iff we created it."""
        if self._owns_client:
            self._client.close()

    @property
    def bundle(self) -> JWTBundle:
        return self._bundle

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bundle.access_token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": "popolaloom/1.0.0 (cursor-cloud-internal)",
        }

    def _post_rpc(
        self,
        method: str,
        body: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Single-shot Connect-RPC POST returning the decoded JSON envelope.

        Raises:
            CursorCloudInternalError: on HTTP / decode / wire failure.
                The error's ``status_code`` mirrors the upstream HTTP
                status when available; ``hint`` always points at the
                supported ``--auth-mode=rest`` fallback per Q-22.
        """
        if _is_jwt_expired(self._bundle.access_token):
            logger.warning(
                "path-B JWT exp claim is within the safety margin; "
                "callers should refresh before issuing further RPCs"
            )
        url = f"{self._base_url}{SERVICE_PATH}/{method}"
        try:
            resp = self._client.post(
                url,
                content=json.dumps(body),
                headers=self._headers(),
                timeout=timeout_s if timeout_s is not None else self._timeout_s,
            )
        except httpx.HTTPError as exc:
            raise CursorCloudInternalError(
                f"path-B HTTP error calling {method}: {exc}",
                hint=(
                    "To retry on REST transport instead, re-dispatch "
                    "with --auth-mode=rest. (popola does NOT auto-switch "
                    "transports; v1.5.0 no-silent-fallback invariant.) "
                    "(如需走 REST,请重新带 --auth-mode=rest 派发——"
                    "popola 不会自动切换传输方式)"
                ),
            ) from exc

        if resp.status_code >= 400:
            connect_code, connect_message, details_summary = (
                _extract_connect_error_envelope(resp)
            )
            sc = resp.status_code
            if sc == 401:
                error_kind = "path_b_rpc_401_auth"
                hint = (
                    "Re-run `agent login` to refresh the JWT. "
                    "Alternatively, re-dispatch with --auth-mode=rest "
                    "and --cli-flag api_key=<X> (popola does NOT "
                    "auto-switch transports; v1.5.0 "
                    "no-silent-fallback invariant). "
                    "(请重新运行 `agent login` 刷新 JWT;若需走 REST,"
                    "请重新带 --auth-mode=rest 配合 CURSOR_API_KEY 派发——"
                    "popola 不会自动切换传输方式)"
                )
                base_msg = f"path-B authentication failed for {method}: HTTP 401"
            elif sc == 404:
                error_kind = "path_b_rpc_404"
                hint = (
                    "Path-B 404 may indicate EITHER (a) the upstream "
                    "Connect-RPC method path was renamed, OR (b) the "
                    "request body failed Connect-Protocol validation "
                    "(a 404 fallback). Check the detail line above. "
                    "If you want to try REST instead, re-dispatch with "
                    "--auth-mode=rest (popola does NOT auto-switch "
                    "transports; v1.5.0 no-silent-fallback invariant). "
                    "(Path-B 404 可能是上游方法路径被改,也可能是请求体未通过 "
                    "Connect-Protocol 校验;详情见上面的 detail 行;"
                    "若需走 REST,请重新带 --auth-mode=rest 派发——"
                    "popola 不会自动切换传输方式)"
                )
                base_msg = f"path-B method {method!r} returned 404 at {url}"
            elif sc == 400 and connect_code == "invalid_argument":
                error_kind = "path_b_rpc_400_invalid_argument"
                hint = (
                    "Path-B request body failed Connect-Protocol validation. "
                    "Inspect the connect_message + details for the missing/invalid "
                    "field; v1.3.0 P5 added 11 required fields — older "
                    "build_start_composer_request callers may be missing them. "
                    "If the rejected field is 'env', re-dispatch with "
                    "--cli-flag env_emit_mode=label (or =none) to drop "
                    "the env field. (popola does NOT auto-shift "
                    "env_emit_mode; v1.5.0 no-silent-fallback invariant.) "
                    "(请求体未通过 Connect-Protocol 校验;参考 connect_message "
                    "与 details 排查缺失字段;若被拒字段是 env,请重新带 "
                    "--cli-flag env_emit_mode=label 或 =none 派发——"
                    "popola 不会自动切换 env_emit_mode)"
                )
                base_msg = f"path-B {method} 400 invalid_argument"
            elif sc >= 500:
                error_kind = "path_b_rpc_5xx"
                hint = (
                    "Upstream 5xx — retry, or re-dispatch with "
                    "--auth-mode=rest (popola does NOT auto-switch "
                    "transports; v1.5.0 no-silent-fallback invariant). "
                    "(上游 5xx,可重试;若需走 REST,请重新带 "
                    "--auth-mode=rest 派发——popola 不会自动切换传输方式)"
                )
                base_msg = f"path-B HTTP {sc} from {method}"
            else:
                error_kind = "path_b_rpc_other"
                hint = (
                    "If this persists, re-dispatch with --auth-mode=rest "
                    "(popola does NOT auto-switch transports; v1.5.0 "
                    "no-silent-fallback invariant). "
                    "(如持续失败,请重新带 --auth-mode=rest 派发——"
                    "popola 不会自动切换传输方式)"
                )
                base_msg = f"path-B HTTP {sc} from {method}"

            logger.warning(
                "path-B %s %s: kind=%s connect_code=%s message=%s details=%s",
                method,
                sc,
                error_kind,
                connect_code,
                connect_message,
                (details_summary or "")[:200],
            )
            raise CursorCloudInternalError(
                base_msg,
                hint=hint,
                status_code=sc,
                connect_code=connect_code,
                connect_message=connect_message,
                details_summary=details_summary,
                error_kind=error_kind,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise CursorCloudInternalError(
                f"path-B {method} returned non-JSON body: {resp.text[:500]!r}",
                hint=(
                    "The wire format may have changed. To retry on REST "
                    "transport instead, re-dispatch with "
                    "--auth-mode=rest. (popola does NOT auto-switch "
                    "transports; v1.5.0 no-silent-fallback invariant.) "
                    "(上游 wire 格式可能已变更;若需走 REST,请重新带 "
                    "--auth-mode=rest 派发——popola 不会自动切换传输方式)"
                ),
            ) from exc

        if not isinstance(payload, dict):
            raise CursorCloudInternalError(
                f"path-B {method} returned non-object JSON: "
                f"{type(payload).__name__}",
                hint=(
                    "To retry on REST transport instead, re-dispatch "
                    "with --auth-mode=rest. (popola does NOT auto-switch "
                    "transports; v1.5.0 no-silent-fallback invariant.) "
                    "(若需走 REST,请重新带 --auth-mode=rest 派发——"
                    "popola 不会自动切换传输方式)"
                ),
            )
        return payload

    def start_background_composer_from_snapshot(
        self,
        body: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> StartComposerOutcome:
        """Call the ``StartBackgroundComposerFromSnapshot`` Connect-RPC method.

        See module docstring for the full endpoint URL + auth + wire-format details.

        Args:
            body: A pre-built request body (typically from
                :func:`build_start_composer_request`).
            timeout_s: Per-call timeout override.

        Returns:
            A :class:`StartComposerOutcome` carrying the new
            ``background_composer_id`` + dashboard URL + raw response.

        Raises:
            CursorCloudInternalError: on any HTTP / decode error;
                callers MUST surface ``error.hint`` to the operator
                and exit non-zero (No Silent Failures rule).
        """
        payload = self._post_rpc(
            "StartBackgroundComposerFromSnapshot",
            body,
            timeout_s=timeout_s,
        )
        # v1.5.0 — Cursor's server changed the response shape between
        # v1.3.0 and v1.4.0: the ``background_composer_id`` / camelCase
        # ``backgroundComposerId`` field moved into the nested
        # ``composer.bcId`` envelope, with ``initialRunId`` /
        # ``initial_run_id`` surfaced alongside. Accept all three shapes
        # so this client stays compatible across builds; the response
        # shape is empirically reverse-engineered and Q-22 makes no
        # stability promises (see feedback_for_v1.4.0 §1 task #2).
        composer_envelope = payload.get("composer")
        if not isinstance(composer_envelope, dict):
            composer_envelope = {}
        bc_id = (
            payload.get("background_composer_id")
            or payload.get("backgroundComposerId")
            or composer_envelope.get("bc_id")
            or composer_envelope.get("bcId")
            or ""
        )
        if not isinstance(bc_id, str) or not bc_id:
            raise CursorCloudInternalError(
                f"path-B StartBackgroundComposerFromSnapshot response missing "
                f"background_composer_id; got keys: {sorted(payload.keys())}",
                hint=(
                    "The response shape may have changed. To retry on "
                    "REST transport instead, re-dispatch with "
                    "--auth-mode=rest. (popola does NOT auto-switch "
                    "transports; v1.5.0 no-silent-fallback invariant.) "
                    "(响应字段缺失;若需走 REST,请重新带 --auth-mode=rest "
                    "派发——popola 不会自动切换传输方式)"
                ),
            )
        initial_run_id_raw: Any = (
            payload.get("initial_run_id")
            or payload.get("initialRunId")
            or composer_envelope.get("initial_run_id")
            or composer_envelope.get("initialRunId")
        )
        initial_run_id: str | None
        if isinstance(initial_run_id_raw, str) and initial_run_id_raw:
            initial_run_id = initial_run_id_raw
        else:
            initial_run_id = None
        dashboard_url = f"https://cursor.com/agents/{bc_id}" if bc_id else None
        return StartComposerOutcome(
            background_composer_id=bc_id,
            dashboard_url=dashboard_url,
            raw_response=payload,
            initial_run_id=initial_run_id,
        )


__all__ = [
    "AgentMode",
    "CursorCloudInternalClient",
    "CursorCloudInternalError",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "ENV_EMIT_MODE_LABEL",  # v1.5.0 escape-hatch enum.
    "ENV_EMIT_MODE_MACHINE",  # v1.5.0 escape-hatch enum.
    "ENV_EMIT_MODE_NONE",  # v1.5.0 escape-hatch enum.
    "EffortMode",
    "JWTAuthError",  # re-exported for callers that catch both
    "SERVICE_PATH",
    "StartComposerOutcome",
    "StartingMessageType",
    "ThinkingLevel",
    "_VALID_ENV_EMIT_MODES",  # v1.5.0 — exposed for caller validation.
    "_camelize_keys",  # v1.3.0 P5 — exposed for camelize-roundtrip tests.
    "_extract_connect_error_envelope",  # v1.3.0 P4 — exposed for envelope tests.
    "_to_camel",  # v1.3.0 P5 — exposed for camelize-roundtrip tests.
    "build_start_composer_request",
    "user_effort_to_effort_mode",
    "user_mode_to_agent_mode",
    "user_thinking_level_to_proto",
]
