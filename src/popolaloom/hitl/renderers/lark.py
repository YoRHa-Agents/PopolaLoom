"""Lark renderer — outbound card v2 + inbound reply parser (v0.3.0 F4.B).

Per spec §12.8 + roadmap §12.8.1 + v0.3.0-plan §4 Stage F4.3.

This module is the renderer-facade for the Lark channel.  It delegates
heavy lifting to :mod:`popolaloom.lark.card_templates` (card v2 JSON)
and :mod:`popolaloom.lark.listener` (event parser).

Functions:

- :func:`render_lark_card` — :class:`HITLPrompt` → card v2 dict.
- :func:`send_lark_card` — invoke ``lark-cli im +send --card``;
  retry up to 3× with exponential backoff (1s/3s/9s).
- :func:`parse_reply` — accepts a Lark event (card_action_event or
  message_receive_event) → :class:`HITLReply` or ``None``.

Workspace rule "lark-cli 写入操作须追加来源标注": footer is built into
the card body by :func:`build_card_payload` (see :data:`LARK_FOOTER`).
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any, Literal

from popolaloom.hitl import HITLPrompt, HITLReply
from popolaloom.lark import lark_target_open_id
from popolaloom.lark.card_templates import (
    LARK_FOOTER,
    build_card_payload,
    build_card_send_argv,
)
from popolaloom.lark.listener import (
    LarkEventResult,
    parse_card_action,
    parse_message_command,
)

logger = logging.getLogger(__name__)

LarkCardKind = Literal["hitl", "terminal", "notification"]
"""v0.4.1 Stage L1.C — channel-of-origin tag for outbound Lark cards.

- ``"hitl"``: human-in-the-loop prompt (v0.3.0 default; ``send_lark_card``
  callers from :mod:`popolaloom.hitl` use this).
- ``"terminal"``: terminal-event notification card (Stage L2's
  :mod:`popolaloom.lark.notifier` will use this for
  ``task.completed``/``task.failed``/``task.canceled`` cards).
- ``"notification"``: generic out-of-band notice (reserved; no caller in
  v0.4.1 — kept in the literal so adding callers in v0.5.0 doesn't break
  the type alias).
"""

__all__ = [
    "LARK_FOOTER",
    "LarkCardKind",
    "LarkSendResult",
    "parse_reply",
    "render_lark_card",
    "send_lark_card",
]


_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0, 9.0)
"""Per spec §12.8.4 — 3-attempt retry with 1s/3s/9s backoff.

After 3 failures the renderer falls back to a plain-text message
(handled by the daemon HITL dispatcher; see
:func:`popolaloom.hitl.sync.HITLStore.record_lark_send`)."""


class LarkSendResult:
    """Return value of :func:`send_lark_card`.

    Attributes:
        ok: whether the send succeeded.
        message_id: Lark message_id (if extractable from CLI stdout);
            empty string on failure.
        attempts: number of attempts (1-based; max 3).
        error: optional error message on failure.
        argv: the actual argv used (for audit logs).
        stdout / stderr: full subprocess output (truncated to 2 KB).
    """

    __slots__ = (
        "argv",
        "attempts",
        "error",
        "message_id",
        "ok",
        "stderr",
        "stdout",
    )

    def __init__(
        self,
        *,
        ok: bool,
        message_id: str = "",
        attempts: int = 0,
        error: str | None = None,
        argv: list[str] | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.ok = ok
        self.message_id = message_id
        self.attempts = attempts
        self.error = error
        self.argv = list(argv or [])
        self.stdout = stdout[:2048]
        self.stderr = stderr[:2048]


def render_lark_card(prompt: HITLPrompt) -> dict[str, Any]:
    """Render a :class:`HITLPrompt` as a Lark interactive card v2 dict.

    Convenience wrapper around
    :func:`popolaloom.lark.card_templates.build_card_payload`.
    The footer ``LARK_FOOTER`` is injected by the underlying builder.
    """
    return build_card_payload(prompt)


def send_lark_card(
    prompt: HITLPrompt,
    target_open_id: str | None = None,
    *,
    runner: Any = None,
    backoff_s: tuple[float, ...] = _RETRY_BACKOFF_S,
    timeout_s: float = 10.0,
    kind: LarkCardKind = "hitl",
) -> LarkSendResult:
    """Send the rendered card via ``lark-cli im +send --card`` (with retry).

    Args:
        prompt: HITL prompt to send.
        target_open_id: Lark user open_id receiver; defaults to
            :func:`lark_target_open_id`.  When None and env var
            ``LARK_HITL_TARGET_OPEN_ID`` is unset, returns a non-OK
            result without spawning the subprocess (renderer disabled).
        runner: optional injectable subprocess runner (test seam);
            defaults to :func:`subprocess.run`. Must accept
            ``argv: list[str]`` + return an object with attributes
            ``returncode``, ``stdout``, ``stderr`` (subprocess.CompletedProcess
            satisfies this).
        backoff_s: retry backoff schedule.
        timeout_s: per-attempt subprocess timeout.
        kind: v0.4.1 Stage L1.C — origin tag emitted in success / failure
            log lines (``lark.send.ok kind=<kind> target=<target>`` /
            ``lark.send.failed kind=<kind> target=<target>``) so
            terminal-notification (Stage L2) and HITL traffic can be
            disambiguated when grepping daemon logs. Valid values:

            - ``"hitl"`` (default; v0.3.0-compatible)
            - ``"terminal"`` (Stage L2 :mod:`lark.notifier` callers)
            - ``"notification"`` (reserved for v0.5.0)

            Default ``"hitl"`` preserves backward-compat for every
            v0.3.0 caller; never changes argv or NDJSON envelopes (v0.4.1
            Stage L1 is logging-only — Stage L2 will extend the NDJSON
            envelope with a ``kind`` field per research §F.1).

    Returns:
        LarkSendResult: details of the attempt (success or failure).
    """
    target = target_open_id or lark_target_open_id()
    if not target:
        logger.warning(
            "lark.send.failed kind=%s target=%s reason=target_unset",
            kind,
            "<unset>",
        )
        return LarkSendResult(
            ok=False,
            error="LARK_HITL_TARGET_OPEN_ID unset; lark renderer disabled",
            attempts=0,
        )
    argv = build_card_send_argv(prompt, target)

    if runner is None:
        runner = subprocess.run

    last_error: str | None = None
    last_stdout = ""
    last_stderr = ""
    for attempt in range(1, len(backoff_s) + 1):
        try:
            result = runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = f"timeout after {timeout_s}s"
            logger.warning(
                "send_lark_card: attempt %d timed out", attempt
            )
        except FileNotFoundError as exc:
            last_error = f"lark-cli not found: {exc}"
            logger.error("send_lark_card: lark-cli binary missing")
            logger.warning(
                "lark.send.failed kind=%s target=%s reason=cli_missing attempts=%d",
                kind,
                target,
                attempt,
            )
            return LarkSendResult(
                ok=False,
                error=last_error,
                attempts=attempt,
                argv=argv,
            )
        except Exception as exc:
            last_error = repr(exc)
            logger.exception("send_lark_card: attempt %d raised", attempt)
        else:
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            last_stdout = stdout
            last_stderr = stderr
            if getattr(result, "returncode", 1) == 0:
                msg_id = _extract_message_id(stdout)
                logger.info(
                    "send_lark_card: success attempt=%d message_id=%s",
                    attempt,
                    msg_id,
                )
                logger.info(
                    "lark.send.ok kind=%s target=%s message_id=%s attempt=%d",
                    kind,
                    target,
                    msg_id,
                    attempt,
                )
                return LarkSendResult(
                    ok=True,
                    message_id=msg_id,
                    attempts=attempt,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            last_error = (
                f"lark-cli rc={result.returncode}; stderr[:200]={stderr[:200]}"
            )
            logger.warning(
                "send_lark_card: attempt %d failed rc=%s", attempt, result.returncode
            )

        if attempt < len(backoff_s):
            time.sleep(backoff_s[attempt - 1])

    logger.warning(
        "lark.send.failed kind=%s target=%s attempts=%d error=%s",
        kind,
        target,
        len(backoff_s),
        last_error,
    )
    return LarkSendResult(
        ok=False,
        error=last_error,
        attempts=len(backoff_s),
        argv=argv,
        stdout=last_stdout,
        stderr=last_stderr,
    )


def parse_reply(card_action_event: dict[str, Any]) -> HITLReply | None:
    """Parse a Lark inbound event into a :class:`HITLReply`.

    Accepts both ``card.action.trigger_v1`` and ``im.message.receive_v1``
    events; returns ``None`` (no reply) on any parse failure or
    unauthorised responder.

    Args:
        card_action_event: dict from the lark-cli NDJSON stream
            (caller has already loaded the JSON line).

    Returns:
        HITLReply: when the event is a valid, authorised reply.
        None: on any benign mismatch (logged at debug).
    """
    header = card_action_event.get("header", {}) or {}
    event_type = header.get("event_type") if isinstance(header, dict) else None
    result: LarkEventResult
    if event_type == "card.action.trigger_v1":
        result = parse_card_action(card_action_event)
    elif event_type == "im.message.receive_v1":
        result = parse_message_command(card_action_event)
    else:
        logger.debug("lark.parse_reply: unknown event_type %r", event_type)
        return None
    if not result.ok or result.reply is None:
        if result.unauthorized:
            logger.warning(
                "lark.parse_reply: unauthorised sender=%s reason=%s",
                result.sender_open_id,
                result.reason,
            )
        else:
            logger.debug("lark.parse_reply: drop reason=%s", result.reason)
        return None
    reply: HITLReply = result.reply
    return reply


def _extract_message_id(stdout: str) -> str:
    """Best-effort message_id extraction from ``lark-cli im +send`` output.

    lark-cli typically prints a JSON line containing ``message_id``;
    when not present, returns the entire trimmed stdout (truncated)
    so the caller can audit.
    """
    import json as _json

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = _json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            mid = payload.get("message_id") or payload.get("id")
            if isinstance(mid, str) and mid:
                return mid
            data = payload.get("data")
            if isinstance(data, dict):
                mid = data.get("message_id") or data.get("id")
                if isinstance(mid, str) and mid:
                    return mid
    return stdout.strip()[:128]
