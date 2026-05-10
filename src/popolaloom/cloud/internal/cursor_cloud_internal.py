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
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.hint = hint or ""
        self.status_code = status_code


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
        raw_response: The full decoded JSON envelope, kept for tests +
            future field surfacing without a code change.
    """

    background_composer_id: str
    dashboard_url: str | None
    raw_response: dict[str, Any]


def build_start_composer_request(
    *,
    prompt: str,
    repo_url: str,
    starting_ref: str = "main",
    model_name: str | None = None,
    max_mode: bool | None = None,
    thinking_level: str | None = None,
    agent_mode: str | None = None,
    effort_mode: str | None = None,
    time_budget_s: int | None = None,
    long_running: bool | None = None,
    starting_message_type: str | None = None,
    auto_proceed_after_planning: bool | None = None,
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
        extras: Optional verbatim extras merged into the body shallow.
            Used by tests + future flag additions; should NOT be used
            for new operator-facing controls (those should add an
            explicit kwarg here).

    Returns:
        A JSON-serialisable dict ready to be sent as the RPC body.

    Raises:
        ValueError: when an enum-valued kwarg has an unknown user value.
            The error message lists the valid values (No Silent Failures).
    """
    if not prompt:
        raise ValueError("prompt is required")
    if not repo_url:
        raise ValueError("repo_url is required")

    body: dict[str, Any] = {
        "prompt": {"text": prompt},
        "repos": [{"url": repo_url, "starting_ref": starting_ref}],
    }

    model_details: dict[str, Any] = {}
    if model_name:
        model_details["model_name"] = model_name
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
    return body


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
                    "Retry, or fall back to --auth-mode=rest. "
                    "(可重试,或改用 --auth-mode=rest 走 REST 路径)"
                ),
            ) from exc

        if resp.status_code == 401:
            raise CursorCloudInternalError(
                f"path-B authentication failed for {method}: "
                f"HTTP 401 — JWT may be expired",
                hint=(
                    "Re-run `cursor login` to refresh the JWT, OR fall "
                    "back to --auth-mode=rest with --cli-flag api_key=<X>. "
                    "(请重新运行 `cursor login` 刷新 JWT,或改用 "
                    "--auth-mode=rest 配合 CURSOR_API_KEY)"
                ),
                status_code=401,
            )
        if resp.status_code == 404:
            raise CursorCloudInternalError(
                f"path-B method {method!r} not found at {url} — Cursor "
                f"may have changed the service path or method name",
                hint=(
                    "Path-B is experimental (Q-22) — fall back to "
                    "--auth-mode=rest. (path-B 是实验性接口,请改用 "
                    "--auth-mode=rest)"
                ),
                status_code=404,
            )
        if resp.status_code >= 400:
            raise CursorCloudInternalError(
                f"path-B HTTP {resp.status_code} from {method}: "
                f"{resp.text[:500]!r}",
                hint=(
                    "If this persists, fall back to --auth-mode=rest. "
                    "(如持续失败,请改用 --auth-mode=rest)"
                ),
                status_code=resp.status_code,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise CursorCloudInternalError(
                f"path-B {method} returned non-JSON body: {resp.text[:500]!r}",
                hint=(
                    "The wire format may have changed; fall back to "
                    "--auth-mode=rest. (上游 wire 格式可能已变更,"
                    "请改用 --auth-mode=rest)"
                ),
            ) from exc

        if not isinstance(payload, dict):
            raise CursorCloudInternalError(
                f"path-B {method} returned non-object JSON: "
                f"{type(payload).__name__}",
                hint="(请改用 --auth-mode=rest)",
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
        bc_id = (
            payload.get("background_composer_id")
            or payload.get("backgroundComposerId")
            or ""
        )
        if not isinstance(bc_id, str) or not bc_id:
            raise CursorCloudInternalError(
                f"path-B StartBackgroundComposerFromSnapshot response missing "
                f"background_composer_id; got keys: {sorted(payload.keys())}",
                hint=(
                    "The response shape may have changed; fall back to "
                    "--auth-mode=rest. (响应字段缺失,请改用 --auth-mode=rest)"
                ),
            )
        dashboard_url = f"https://cursor.com/agents/{bc_id}" if bc_id else None
        return StartComposerOutcome(
            background_composer_id=bc_id,
            dashboard_url=dashboard_url,
            raw_response=payload,
        )


__all__ = [
    "AgentMode",
    "CursorCloudInternalClient",
    "CursorCloudInternalError",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "EffortMode",
    "JWTAuthError",  # re-exported for callers that catch both
    "SERVICE_PATH",
    "StartComposerOutcome",
    "StartingMessageType",
    "ThinkingLevel",
    "build_start_composer_request",
    "user_effort_to_effort_mode",
    "user_mode_to_agent_mode",
    "user_thinking_level_to_proto",
]
