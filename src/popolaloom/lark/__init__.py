"""popolaloom-lark — Lark out + in HITL channel (v0.3.0 F4.D §12.8).

Per spec §3.4 + §5.4 + roadmap §12.8 + v0.3.0-plan §4 Stage F4.D.

This package wires PopolaLoom into the Lark (Feishu) bot ecosystem
via the **lark-cli** subprocess (NOT the lark-oapi Python SDK; lark-cli
is a JS-based CLI installed at ``/root/.npm-global/bin/lark-cli`` and
already works with multiple skill sets).

Components:

- :mod:`popolaloom.lark.card_templates` — pure-function builders that
  turn a :class:`HITLPrompt` into a Lark interactive card v2 JSON
  payload.  The footer ``---\\n本消息由飞书工具 Lark-Cli 发送`` is
  appended automatically per workspace rule.
- :mod:`popolaloom.lark.listener` — :class:`LarkListener` async class
  that spawns ``lark-cli event consume`` as a subprocess and parses
  NDJSON events from stdout (card.action.trigger_v1 button clicks +
  im.message.receive_v1 chat commands).
- :mod:`popolaloom.lark.supervisor` — :class:`LarkSupervisor` that
  babysits the listener subprocess (≤ 3 restarts; emits escalation
  event after 4th death; 60-second heartbeat).

The whole package is OPTIONAL at runtime — if ``lark-cli`` is not in
PATH or ``LARK_HITL_TARGET_OPEN_ID`` is unset, the daemon logs a
warning and disables the lark renderer (the other 4 channels stay
working).  See :func:`is_lark_runtime_available`.

Workspace rule "lark-cli 写入操作须追加来源标注": every outbound
``lark-cli im +send`` invocation built by :mod:`card_templates`
includes the standard footer at the end of the card body.
"""

from __future__ import annotations

import os
import shutil

from popolaloom.lark.card_templates import (
    LARK_FOOTER,
    build_card_payload,
    build_card_send_argv,
)
from popolaloom.lark.listener import LarkListener
from popolaloom.lark.supervisor import LarkSupervisor

__all__ = [
    "LARK_FOOTER",
    "LARK_NOTIFICATION_LOG_KEYS",
    "LarkListener",
    "LarkSupervisor",
    "NotificationOutcome",
    "build_card_payload",
    "build_card_send_argv",
    "is_lark_runtime_available",
    "lark_allowed_responders",
    "lark_target_open_id",
    "send_terminal_notification",
]


def is_lark_runtime_available() -> bool:
    """Return ``True`` iff the lark-cli binary is in PATH.

    Used by :class:`popolaloom.daemon.server.Popolad` startup to decide
    whether to spawn the Lark listener.  When ``False``, the daemon
    logs a warning and the lark renderer is disabled (other 4 channels
    keep working — workspace rule "No Silent Failures" honoured by the
    explicit warning).
    """
    return shutil.which("lark-cli") is not None


def lark_target_open_id() -> str | None:
    """Resolve the Lark target user open_id from env (``LARK_HITL_TARGET_OPEN_ID``).

    Returns ``None`` when unset; the renderer treats this as "lark
    channel disabled" (the daemon warns at startup).
    """
    value = os.environ.get("LARK_HITL_TARGET_OPEN_ID", "").strip()
    return value or None


def lark_allowed_responders() -> list[str]:
    """Resolve the allowed_responders whitelist from env.

    ``LARK_HITL_ALLOWED_RESPONDERS`` is a comma-separated list of
    open_ids; defaults to ``[lark_target_open_id()]`` when unset
    (the target is always allowed to reply per D3.7).

    Per workspace rule "No Silent Failures": empty list is returned
    explicitly when no env vars are set; the listener then refuses
    all replies (defense in depth — operator must opt-in).
    """
    raw = os.environ.get("LARK_HITL_ALLOWED_RESPONDERS", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    target = lark_target_open_id()
    if target:
        return [target]
    return []


# v0.4.1 Stage L2.A — re-export the proactive notifier surface AFTER the
# helper functions above are defined, so ``notifier`` can import them at
# module load without hitting a partially-initialised package
# (circular-import-safe).
from popolaloom.lark.notifier import (  # noqa: E402
    LARK_NOTIFICATION_LOG_KEYS,
    NotificationOutcome,
    send_terminal_notification,
)
