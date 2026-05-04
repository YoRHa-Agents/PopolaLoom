"""IDE renderer — desktop notify (Linux notify-send / macOS osascript).

Per spec §3.4 + roadmap §12.5 + v0.3.0-plan §4 Stage F4.7.

The IDE channel is a one-way notification: it pops a desktop dialog
to alert the human, then redirects them to ``popola feedback <id>``
on the CLI for the actual reply.  This keeps the rendering simple
(no cross-process input handling) while still surfacing the prompt
through a different sensory channel from the Lark inbox.

Functions:

- :func:`render_ide_notify` — produce the notification message
  (subject/body) for the host OS.
- :func:`dispatch_ide_notify` — actually invoke notify-send / osascript.
- :func:`parse_reply` — IDE has no inbound reply; this is a stub
  that asserts the caller already routed via popola CLI feedback.

Workspace rule "No Silent Failures": when the host OS lacks the
notification binary, :func:`dispatch_ide_notify` returns ``False``
and logs at WARNING (does NOT raise — the other 4 channels still work).
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from typing import Any

from popolaloom.hitl import HITLPrompt, HITLReply

logger = logging.getLogger(__name__)

__all__ = [
    "IdeNotifyMessage",
    "dispatch_ide_notify",
    "parse_reply",
    "render_ide_notify",
]


_DEFAULT_BODY_MAX_CHARS: int = 200
"""Truncate body text to this many chars (notify dialogs hate long text)."""


class IdeNotifyMessage:
    """Plain-data carrier for the rendered IDE notification.

    Attributes:
        title: 1-line notification title.
        body: ≤ 200-char body.
        urgency: ``"low"`` / ``"normal"`` / ``"critical"`` (notify-send
            -u flag); maps from the trigger.
        cli_command: ``popola feedback <id> --option=...`` hint
            included verbatim in the body so the human can copy-paste.
        prompt_id: original prompt id.
    """

    __slots__ = ("body", "cli_command", "prompt_id", "title", "urgency")

    def __init__(
        self,
        *,
        title: str,
        body: str,
        urgency: str,
        cli_command: str,
        prompt_id: str,
    ) -> None:
        self.title = title
        self.body = body
        self.urgency = urgency
        self.cli_command = cli_command
        self.prompt_id = prompt_id


_URGENCY_BY_TRIGGER: dict[str, str] = {
    "info_request": "low",
    "round_floor": "normal",
    "approval": "normal",
    "destructive_op": "critical",
    "ambiguous_input": "normal",
}


def render_ide_notify(prompt: HITLPrompt) -> IdeNotifyMessage:
    """Build an :class:`IdeNotifyMessage` from a :class:`HITLPrompt`.

    The body includes a ready-to-paste CLI command so the human can
    immediately reply without hunting for the ``hitl_id`` on the CLI.
    """
    pid = prompt.ensure_prompt_id()
    options_csv = "/".join(opt.id for opt in prompt.options)
    cli_command = (
        f"popola feedback {pid} --option={prompt.default_option_id}"
        f"   # other options: {options_csv}"
    )
    short_what = (prompt.what or "").strip().splitlines()[0][:_DEFAULT_BODY_MAX_CHARS]
    short_why = (prompt.why or "").strip().splitlines()[0][:_DEFAULT_BODY_MAX_CHARS]
    body = (
        f"{short_why}\n\n{short_what}\n\n"
        f"Reply: {cli_command}"
    )
    title = f"PopolaLoom HITL · {prompt.trigger}"
    return IdeNotifyMessage(
        title=title,
        body=body,
        urgency=_URGENCY_BY_TRIGGER.get(prompt.trigger, "normal"),
        cli_command=cli_command,
        prompt_id=pid,
    )


def dispatch_ide_notify(
    prompt: HITLPrompt,
    *,
    runner: Any = None,
    timeout_s: float = 5.0,
) -> bool:
    """Invoke the host OS notification binary; return ``True`` on success.

    On Linux: ``notify-send -u <urgency> <title> <body>``.
    On macOS: ``osascript -e 'display notification ...'``.
    On other platforms: log at WARNING and return False.

    Args:
        prompt: prompt to render.
        runner: optional subprocess.run-compatible callable (test seam).
        timeout_s: subprocess timeout.

    Returns:
        bool: ``True`` iff the notify command exited 0.
    """
    msg = render_ide_notify(prompt)
    argv = _build_notify_argv(msg)
    if argv is None:
        logger.warning(
            "dispatch_ide_notify: no notify binary on PATH; IDE renderer disabled"
        )
        return False

    if runner is None:
        runner = subprocess.run

    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        logger.warning("dispatch_ide_notify: %s", exc)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("dispatch_ide_notify: timeout after %ss", timeout_s)
        return False
    except Exception:
        logger.exception("dispatch_ide_notify: subprocess raised")
        return False
    rc = getattr(result, "returncode", 1)
    if rc != 0:
        stderr = getattr(result, "stderr", "")
        logger.warning(
            "dispatch_ide_notify: notify exited rc=%s stderr[:200]=%s",
            rc,
            stderr[:200] if stderr else "",
        )
    return rc == 0


def _build_notify_argv(msg: IdeNotifyMessage) -> list[str] | None:
    """Pick the right argv for the host OS, or None if no notifier present."""
    system = platform.system().lower()
    if system == "linux":
        if shutil.which("notify-send") is None:
            return None
        return [
            "notify-send",
            "-u", msg.urgency,
            msg.title,
            msg.body,
        ]
    if system == "darwin":
        if shutil.which("osascript") is None:
            return None
        body_escaped = msg.body.replace('"', '\\"').replace("\n", " ")
        title_escaped = msg.title.replace('"', '\\"')
        return [
            "osascript",
            "-e",
            f'display notification "{body_escaped}" with title "{title_escaped}"',
        ]
    return None


def parse_reply(reply_payload: dict[str, Any]) -> HITLReply | None:
    """IDE notify is one-way; this stub validates the popola CLI carrier.

    The actual reply path is :mod:`popolaloom.hitl.renderers.cli`'s
    ``popola feedback`` command — which writes a :class:`HITLReply`
    payload via the daemon RPC.  This function simply lifts the dict
    into a :class:`HITLReply` if it has the required shape, or
    returns ``None`` for benign cases (workspace rule "No Silent
    Failures": shape mismatch is benign because the cli renderer is
    the source of truth).
    """
    hitl_id = reply_payload.get("hitl_id")
    option_id = reply_payload.get("option_id")
    if not isinstance(hitl_id, str) or not isinstance(option_id, str):
        logger.debug("ide.parse_reply: missing hitl_id/option_id; benign skip")
        return None
    return HITLReply(
        hitl_id=hitl_id,
        option_id=option_id,
        via="ide",
        reason=reply_payload.get("reason"),
        responder=reply_payload.get("responder"),
    )
