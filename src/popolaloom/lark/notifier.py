"""Lark proactive terminal-state notifier — v0.4.1 Stage L2.A.

Per the v0.4.1 minor plan §2.1 #3 + §4 Stage L2.A + research §F.1 +
§G.2 #3, this module is the daemon-side hook that turns every
terminal :class:`popolaloom.daemon.state.TaskState` transition
(``COMPLETED`` / ``FAILED`` / ``CANCELED``) into a Lark interactive
card delivered to the operator's chat.

Architecture:

- :func:`send_terminal_notification` is an **async coroutine** so the
  daemon's :meth:`Popolad._on_subprocess_exit` (which runs in a
  subprocess wait-thread, not on the event loop) can schedule it via
  :func:`asyncio.run_coroutine_threadsafe`. The actual blocking
  ``lark-cli`` subprocess call is offloaded to a worker thread via
  :func:`asyncio.to_thread`.
- Reuses :func:`popolaloom.hitl.renderers.lark.send_lark_card` with
  ``kind="terminal"`` so retry / timeout / log lines / NDJSON envelope
  semantics are shared with the HITL channel (per research §F.1 + the
  v0.4.1 D4.1.2 decision: reuse over duplicate).
- Pure observability: every skip and every send is logged at INFO
  with the explicit reason (per workspace rule "No Silent Failures").
- Hard freeze contract for v0.5.0 (per v0.4.1 plan §0.5 row #5):
  :data:`LARK_NOTIFICATION_LOG_KEYS` and :class:`NotificationOutcome`
  are import-stable so :mod:`popolaloom.cli.doctor` (v0.5.0) can
  walk recent ``lark.send.*`` events without re-defining the schema.

Env-var configuration (defaults from research §E.2.4):

- ``LARK_NOTIFY_TARGET_OPEN_ID`` — Lark target open_id; falls back to
  ``LARK_HITL_TARGET_OPEN_ID`` so single-user setups need only set one.
- ``LARK_NOTIFY_ON_COMPLETED`` — default ``"1"`` (ON).
- ``LARK_NOTIFY_ON_FAILED`` — default ``"1"`` (ON).
- ``LARK_NOTIFY_ON_CANCELED`` — default ``"1"`` (ON).
- ``LARK_NOTIFY_ON_CANCEL_ESCALATED`` — default ``"0"`` (OFF; opt-in
  because SIGKILL escalation also fires the regular CANCELED card and
  many operators consider the second card noise).
- ``LARK_NOTIFY_PROMPT_TRUNCATE`` — default ``"200"``; clamped to
  ``[50, 2000]`` defensively.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from popolaloom.daemon.state import TaskState
from popolaloom.lark import is_lark_runtime_available, lark_target_open_id

if TYPE_CHECKING:
    from popolaloom.daemon.server import Popolad

logger = logging.getLogger(__name__)


LARK_NOTIFICATION_LOG_KEYS: tuple[str, str] = ("lark.send.ok", "lark.send.failed")
"""Frozen NDJSON event-type keys emitted by the underlying send pipeline.

This constant is part of the v0.4.1 → v0.5.0 compatibility contract
(plan §0.5 row #5): :mod:`popolaloom.cli.doctor` (v0.5.0) walks the
last N matching events in each per-task NDJSON to compute Lark
delivery health without redefining the keys here. Do not rename or
re-order without bumping the v0.5.0 plan."""


_TERMINAL_STATES_FOR_NOTIFY: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}
)
"""States this notifier knows how to render. Other states are silently
skipped with an explicit log line so the wait-thread never blocks the
daemon on an undefined state."""


_DEFAULT_PROMPT_TRUNCATE: int = 200
_PROMPT_TRUNCATE_MIN: int = 50
_PROMPT_TRUNCATE_MAX: int = 2000


@dataclass(frozen=True)
class NotificationOutcome:
    """Frozen result from :func:`send_terminal_notification`.

    Attributes:
        ok: ``True`` iff the underlying ``lark-cli`` invocation
            succeeded (``LarkSendResult.ok == True``).
        skipped: ``True`` iff the notifier opted out before invoking
            ``lark-cli`` (env var off, runtime missing, target unset,
            non-terminal state, etc.). Mutually exclusive with ``ok``.
        reason: human-readable diagnostic. For ``skipped=True`` this is
            the gating condition; for ``ok=False`` and ``skipped=False``
            it is the underlying error string.

    Hard-frozen for the v0.4.1 → v0.5.0 compatibility contract (plan
    §0.5 row #5): :mod:`popolaloom.cli.doctor` (v0.5.0) consumes this
    shape directly. Adding fields is OK; removing or renaming requires
    a v0.5.0 plan revision.
    """

    ok: bool
    skipped: bool
    reason: str | None = None

    @classmethod
    def success(cls, *, reason: str | None = None) -> NotificationOutcome:
        """Build a successful-send outcome (``ok=True``, ``skipped=False``)."""
        return cls(ok=True, skipped=False, reason=reason)

    @classmethod
    def skip(cls, reason: str) -> NotificationOutcome:
        """Build a "skipped silently" outcome (``ok=False``, ``skipped=True``)."""
        return cls(ok=False, skipped=True, reason=reason)

    @classmethod
    def failure(cls, reason: str) -> NotificationOutcome:
        """Build a send-attempted-but-failed outcome (``ok=False``, ``skipped=False``)."""
        return cls(ok=False, skipped=False, reason=reason)


def _lark_notify_target_open_id() -> str | None:
    """Resolve the notification target open_id.

    Order: ``LARK_NOTIFY_TARGET_OPEN_ID`` env var → ``LARK_HITL_TARGET_OPEN_ID``
    fallback (via :func:`popolaloom.lark.lark_target_open_id`). Empty
    string is treated as unset.
    """
    explicit = os.environ.get("LARK_NOTIFY_TARGET_OPEN_ID", "").strip()
    if explicit:
        return explicit
    return lark_target_open_id()


def _is_truthy_env(value: str | None) -> bool:
    """Standard truthy parse: ``"1"``/``"true"``/``"yes"``/``"on"`` (case-insensitive).

    Per workspace rule "No Silent Failures": invalid values fall back
    to ``False`` (with no exception) AND are logged by the caller when
    they gate a skip — the caller has more context to log the env name.
    """
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_enabled_for_state(state: TaskState) -> tuple[bool, str]:
    """Return ``(enabled, env_var_name)`` for the given terminal state.

    Defaults: COMPLETED/FAILED/CANCELED ON; CANCEL_ESCALATED checked
    separately (see :func:`_env_enabled_for_cancel_escalated`).
    """
    mapping = {
        TaskState.COMPLETED: ("LARK_NOTIFY_ON_COMPLETED", "1"),
        TaskState.FAILED: ("LARK_NOTIFY_ON_FAILED", "1"),
        TaskState.CANCELED: ("LARK_NOTIFY_ON_CANCELED", "1"),
    }
    pair = mapping.get(state)
    if pair is None:
        return False, "<no-env-var>"
    var_name, default = pair
    raw = os.environ.get(var_name, default)
    return _is_truthy_env(raw), var_name


def _env_enabled_for_cancel_escalated() -> bool:
    """``LARK_NOTIFY_ON_CANCEL_ESCALATED`` (default OFF per research §E.2.1)."""
    return _is_truthy_env(os.environ.get("LARK_NOTIFY_ON_CANCEL_ESCALATED", "0"))


def _resolve_prompt_truncate() -> int:
    """Read ``LARK_NOTIFY_PROMPT_TRUNCATE`` clamped to ``[50, 2000]``.

    Invalid integer falls back to the default (200) with an INFO log so
    operators see the misconfiguration but the daemon does not raise.
    """
    raw = os.environ.get("LARK_NOTIFY_PROMPT_TRUNCATE")
    if raw is None or raw.strip() == "":
        return _DEFAULT_PROMPT_TRUNCATE
    try:
        value = int(raw)
    except ValueError:
        logger.info(
            "lark.notify.config: LARK_NOTIFY_PROMPT_TRUNCATE=%r not int; "
            "using default %d",
            raw,
            _DEFAULT_PROMPT_TRUNCATE,
        )
        return _DEFAULT_PROMPT_TRUNCATE
    return max(_PROMPT_TRUNCATE_MIN, min(_PROMPT_TRUNCATE_MAX, value))


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars + ``…`` if longer (no-op when shorter)."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _prompt_summary_from_handle(handle: Any, truncate: int) -> str:
    """Best-effort prompt-summary extraction from a :class:`TaskHandle`.

    Order: ``handle.prompt_summary`` (future hook; v0.4.1 doesn't set
    it on construction but v0.5.0 may add it) → ``" ".join(handle.cmd)``
    → empty string. The result is truncated per ``truncate`` (caller
    passes the env-var-resolved value).
    """
    summary = getattr(handle, "prompt_summary", None)
    if isinstance(summary, str) and summary.strip():
        return _truncate(summary, truncate)
    cmd = getattr(handle, "cmd", None)
    if isinstance(cmd, list) and cmd:
        joined = " ".join(str(part) for part in cmd)
        return _truncate(joined, truncate)
    return ""


def _resolve_event_log(popolad: Popolad, task_id: str) -> Any:
    """Return the per-task :class:`EventLog`, or ``None`` if not found.

    Used so :func:`send_lark_card` can write its
    ``lark.send.{ok,failed}`` NDJSON envelope into the same per-task
    log file the rest of the lifecycle events go to. ``None`` means
    "no log handle" — the send still happens, just without an
    auditable NDJSON trace.
    """
    event_log_fn = getattr(popolad, "event_log", None)
    if event_log_fn is None:
        return None
    try:
        return event_log_fn(task_id)
    except Exception:
        logger.exception(
            "lark.notify: popolad.event_log(%s) raised; sending without NDJSON trace",
            task_id,
        )
        return None


def _select_terminal_card(
    *,
    terminal_state: TaskState,
    handle: Any,
    exit_code: int | None,
    prompt_summary: str,
    completed_at_iso: str,
    use_escalated_card: bool,
) -> tuple[dict[str, Any], str]:
    """Pick the right L1 builder and produce ``(payload, trigger_kind)``.

    Returns a tuple where the second element is the human-readable
    trigger kind ("completed" / "failed" / "canceled" / "cancel_escalated")
    used in log lines for grep-ability.
    """
    from popolaloom.lark.card_templates import (
        build_cancel_escalated_card,
        build_canceled_card,
        build_completion_card,
        build_failure_card,
    )

    task_id = str(getattr(handle, "task_id", "") or "")
    cli = str(getattr(handle, "cli", "") or "")
    started_at_dt = getattr(handle, "started_at", None)
    started_at_iso = (
        started_at_dt.isoformat(timespec="milliseconds")
        if isinstance(started_at_dt, datetime)
        else ""
    )
    sigkill_escalated = bool(
        getattr(handle, "cancel_escalated_to_sigkill", False)
    )

    if terminal_state == TaskState.COMPLETED:
        payload = build_completion_card(
            task_id=task_id,
            cli=cli,
            prompt_summary=prompt_summary,
            exit_code=int(exit_code) if exit_code is not None else 0,
            started_at=started_at_iso,
            completed_at=completed_at_iso,
            latest_event_index=_latest_event_index(handle),
        )
        return payload, "completed"

    if terminal_state == TaskState.FAILED:
        payload = build_failure_card(
            task_id=task_id,
            cli=cli,
            prompt_summary=prompt_summary,
            exit_code=int(exit_code) if exit_code is not None else -1,
            last_stderr_lines=[],
            started_at=started_at_iso,
            failed_at=completed_at_iso,
        )
        return payload, "failed"

    if use_escalated_card:
        payload = build_cancel_escalated_card(
            task_id=task_id,
            cli=cli,
            prompt_summary=prompt_summary,
            exit_code=int(exit_code) if exit_code is not None else -9,
            sigterm_at=started_at_iso,
            sigkill_at=completed_at_iso,
        )
        return payload, "cancel_escalated"

    payload = build_canceled_card(
        task_id=task_id,
        cli=cli,
        prompt_summary=prompt_summary,
        escalated_to_sigkill=sigkill_escalated,
        started_at=started_at_iso,
        canceled_at=completed_at_iso,
    )
    return payload, "canceled"


def _latest_event_index(handle: Any) -> int:
    """Best-effort latest-event-index lookup (0 if not derivable).

    The notifier doesn't have direct access to the per-task event log
    count (that lives on :class:`Popolad`'s internal dict); we fall
    back to 0 for the rendered card. This keeps the notifier
    decoupled from the daemon's internal locking. v0.5.0 may add a
    ``handle.latest_event_index`` field for tighter numbers; until
    then a 0 simply means "see the NDJSON for the real count".
    """
    explicit = getattr(handle, "latest_event_index", None)
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    return 0


async def send_terminal_notification(
    popolad: Popolad,
    task_id: str,
    terminal_state: TaskState,
    exit_code: int | None,
) -> NotificationOutcome:
    """Send a Lark card on a task terminal-state transition.

    Picks the right card builder based on ``terminal_state`` plus, for
    ``CANCELED``, the StateStore's
    :attr:`TaskHandle.cancel_escalated_to_sigkill` flag combined with
    the ``LARK_NOTIFY_ON_CANCEL_ESCALATED`` env var. Reuses
    :func:`popolaloom.hitl.renderers.lark.send_lark_card` with
    ``kind="terminal"`` so retry / timeout / NDJSON envelope semantics
    match the HITL channel.

    Skips silently (returns :meth:`NotificationOutcome.skip`) — but
    always logs the reason at INFO — when:

    - ``terminal_state`` is not in
      ``{COMPLETED, FAILED, CANCELED}`` (defensive — only the
      caller's wait-thread should fan in here).
    - :func:`is_lark_runtime_available` returns ``False``
      (``lark-cli`` not on PATH).
    - :func:`_lark_notify_target_open_id` returns ``None`` (neither
      ``LARK_NOTIFY_TARGET_OPEN_ID`` nor ``LARK_HITL_TARGET_OPEN_ID``
      is set).
    - The relevant ``LARK_NOTIFY_ON_<STATE>`` env var is ``"0"`` /
      ``"false"``.
    - ``terminal_state == CANCELED`` AND
      ``cancel_escalated_to_sigkill == True`` AND
      ``LARK_NOTIFY_ON_CANCEL_ESCALATED == "0"`` AND
      ``LARK_NOTIFY_ON_CANCELED == "0"`` (both gates closed).
    - The per-task :class:`TaskHandle` is missing from the StateStore
      (race: cancel cleared it before the wait-thread fired).

    Returns an outcome dataclass; on exception (e.g. ``lark-cli``
    crashes mid-send) returns
    ``NotificationOutcome.failure(reason=...)`` per workspace rule
    "No Silent Failures" — never re-raises.

    Args:
        popolad: the daemon instance (only ``._state`` and
            ``.event_log()`` are read).
        task_id: the popola internal task id.
        terminal_state: the new state being transitioned to.
        exit_code: subprocess exit code (``None`` only on bizarre
            wait-thread paths; treated as ``-1`` for FAILED rendering).

    Returns:
        NotificationOutcome: structured result for the daemon to log
        and for v0.5.0 ``popola doctor`` to consume.
    """
    if terminal_state not in _TERMINAL_STATES_FOR_NOTIFY:
        reason = f"non_terminal_state={terminal_state}"
        logger.info(
            "lark.notify.skipped task_id=%s reason=%s", task_id, reason
        )
        return NotificationOutcome.skip(reason)

    if not is_lark_runtime_available():
        reason = "lark_cli_unavailable"
        logger.info(
            "lark.notify.skipped task_id=%s reason=%s", task_id, reason
        )
        return NotificationOutcome.skip(reason)

    target = _lark_notify_target_open_id()
    if target is None:
        reason = "target_open_id_unset"
        logger.info(
            "lark.notify.skipped task_id=%s reason=%s", task_id, reason
        )
        return NotificationOutcome.skip(reason)

    state_store = getattr(popolad, "_state", None)
    if state_store is None:
        reason = "popolad_state_store_missing"
        logger.warning(
            "lark.notify.skipped task_id=%s reason=%s", task_id, reason
        )
        return NotificationOutcome.skip(reason)

    handle = state_store.get(task_id)
    if handle is None:
        reason = "handle_not_in_state_store"
        logger.warning(
            "lark.notify.skipped task_id=%s reason=%s", task_id, reason
        )
        return NotificationOutcome.skip(reason)

    enabled, env_var = _env_enabled_for_state(terminal_state)
    sigkill_escalated = bool(
        getattr(handle, "cancel_escalated_to_sigkill", False)
    )
    use_escalated_card = (
        terminal_state == TaskState.CANCELED
        and sigkill_escalated
        and _env_enabled_for_cancel_escalated()
    )

    if not enabled and not use_escalated_card:
        reason = f"env_off var={env_var}"
        logger.info(
            "lark.notify.skipped task_id=%s reason=%s state=%s",
            task_id,
            reason,
            terminal_state,
        )
        return NotificationOutcome.skip(reason)

    truncate = _resolve_prompt_truncate()
    prompt_summary = _prompt_summary_from_handle(handle, truncate)
    completed_at_dt = getattr(handle, "completed_at", None) or datetime.now(UTC)
    completed_at_iso = completed_at_dt.isoformat(timespec="milliseconds")

    try:
        card_payload, trigger_kind = _select_terminal_card(
            terminal_state=terminal_state,
            handle=handle,
            exit_code=exit_code,
            prompt_summary=prompt_summary,
            completed_at_iso=completed_at_iso,
            use_escalated_card=use_escalated_card,
        )
    except Exception as exc:
        reason = f"card_build_failed: {exc!r}"
        logger.exception(
            "lark.notify.failed task_id=%s reason=card_build_failed", task_id
        )
        return NotificationOutcome.failure(reason)

    event_log = _resolve_event_log(popolad, task_id)

    try:
        result = await asyncio.to_thread(
            _send_card_payload,
            card_payload=card_payload,
            target=target,
            event_log=event_log,
        )
    except Exception as exc:
        reason = f"send_failed: {exc!r}"
        logger.exception(
            "lark.notify.failed task_id=%s reason=send_raised", task_id
        )
        return NotificationOutcome.failure(reason)

    if result.ok:
        logger.info(
            "lark.notify.sent task_id=%s kind=terminal trigger=%s target=%s "
            "message_id=%s",
            task_id,
            trigger_kind,
            target,
            result.message_id,
        )
        return NotificationOutcome.success(reason=f"trigger={trigger_kind}")

    reason = f"send_failed: {result.error}"
    logger.warning(
        "lark.notify.failed task_id=%s kind=terminal trigger=%s target=%s "
        "attempts=%d error=%s",
        task_id,
        trigger_kind,
        target,
        result.attempts,
        result.error,
    )
    return NotificationOutcome.failure(reason)


def _send_card_payload(
    *,
    card_payload: dict[str, Any],
    target: str,
    event_log: Any,
) -> Any:
    """Sync helper: call :func:`send_lark_card` with a pre-built terminal payload.

    The HITL renderer's :func:`send_lark_card` is HITLPrompt-shaped
    today (it builds the argv from a HITLPrompt). For terminal cards
    we already have the payload from a card_templates builder, so we
    bypass argv construction by passing ``card_payload`` directly via
    the ``card_payload`` keyword (added in v0.4.1 alongside the
    ``event_log`` parameter) — see
    :func:`popolaloom.hitl.renderers.lark.send_lark_card` for the
    parameter contract.
    """
    from popolaloom.hitl.renderers.lark import send_lark_card

    return send_lark_card(
        prompt=None,
        target_open_id=target,
        kind="terminal",
        event_log=event_log,
        card_payload=card_payload,
    )


__all__ = [
    "LARK_NOTIFICATION_LOG_KEYS",
    "NotificationOutcome",
    "send_terminal_notification",
]
