"""Cloud-agent HITL bridge (v0.8.5 spike-poc Wave 3 / Stage 3).

Cursor Cloud Agents invoke PopolaLoom daemon RPC endpoints to persist a HITL
row in ``popola_hitl`` (:class:`~popolaloom.hitl.sync.HITLStore`), optionally
fan out via Lark (:func:`~popolaloom.hitl.renderers.lark.send_lark_card`), and
later collect the reply through the existing cross-channel first-responder wins
semantics in :meth:`HITLStore.mark_answered`.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from popolaloom.hitl import (
    HITLChannel,
    HITLOption,
    HITLPrompt,
    HITLReply,
    HITLStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloudHITLRequest:
    """Outbound view of a cloud-agent HITL submission.

    Rows are keyed by ``hitl_id``. The authoritative prompt lives in SQLite
    (``prompt_json``) as a validated :class:`HITLPrompt`.
    """

    hitl_id: str
    task_id: str
    cursor_agent_id: str | None
    cursor_run_id: str | None
    prompt: HITLPrompt
    options: tuple[HITLOption, ...]
    created_at: datetime
    deadline_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict[str, Any])


class CloudHITLLarkNotifier(Protocol):
    """Injectable Lark fan-out facade (production calls ``send_lark_card``)."""

    def send_hitl_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        event_log: Any | None = None,
        task_id: str | None = None,
    ) -> Any:
        """Best-effort HITL card send; may raise."""


class _DefaultCloudLarkNotifier:
    """Delegates to :func:`~popolaloom.hitl.renderers.lark.send_lark_card`."""

    def send_hitl_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        event_log: Any | None = None,
        task_id: str | None = None,
    ) -> Any:
        from popolaloom.hitl.renderers.lark import send_lark_card

        # hitl_id / task_id are accepted for test doubles and future store updates.
        _ = hitl_id, task_id
        return send_lark_card(prompt, event_log=event_log)


class _NoopCloudLarkNotifier:
    """Null sender for ``build_default_bridge`` when no notifier is provided."""

    def send_hitl_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        event_log: Any | None = None,
        task_id: str | None = None,
    ) -> Any:
        _ = prompt, hitl_id, event_log, task_id
        return None


def _ceil_deadline_seconds(timeout_s: float | None, default: float) -> int:
    """Clamp prompt ``deadline_seconds`` to (1, 86400] per :class:`HITLPrompt`."""
    raw = float(default if timeout_s is None else timeout_s)
    return max(1, min(86400, int(math.ceil(raw))))


def _build_cloud_prompt(
    *,
    prompt_title: str,
    prompt_body: str,
    options: list[dict[str, str]],
    task_id: str,
    cursor_agent_id: str | None,
    cursor_run_id: str | None,
    metadata: dict[str, Any] | None,
    deadline_seconds: int,
) -> HITLPrompt:
    """Build a store-safe :class:`HITLPrompt` for cloud-agent approvals.

    The DB ``trigger`` column holds ``approval`` (schema-valid). Cloud origin
    and caller metadata are embedded in ``why`` so prompts round-trip in SQLite
    without ``extra=`` fields.
    """
    hitl_options = [HITLOption(id=o["id"], label=o["label"]) for o in options]
    default_id = hitl_options[0].id
    ctx_bits = [
        f"task_id={task_id}",
        f"cursor_agent_id={cursor_agent_id!s}",
        f"cursor_run_id={cursor_run_id!s}",
    ]
    ctx = "\n".join(ctx_bits)
    meta_line = ""
    if metadata:
        meta_line = "\nMetadata: " + json.dumps(metadata, sort_keys=True)
    why = f"{prompt_title}\n\nCloud HITL context:\n{ctx}{meta_line}"
    return HITLPrompt(
        trigger="approval",
        why=why,
        what=prompt_body,
        options=hitl_options,
        default_option_id=default_id,
        channels=["lark", "mcp", "cloud"],
        deadline_seconds=deadline_seconds,
    )


class CloudHITLBridge:
    """Orchestrates cloud-sourced HITL rows + optional Lark delivery."""

    def __init__(
        self,
        store: HITLStore,
        lark_notifier: CloudHITLLarkNotifier | None,
        *,
        default_timeout_s: float = 600.0,
    ) -> None:
        self._store = store
        self._lark_notifier = lark_notifier
        self._default_timeout_s = default_timeout_s

    @property
    def store(self) -> HITLStore:
        return self._store

    def submit_request(
        self,
        *,
        task_id: str,
        cursor_agent_id: str | None,
        cursor_run_id: str | None,
        prompt_title: str,
        prompt_body: str,
        options: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        event_log: Any | None = None,
    ) -> CloudHITLRequest:
        """Create a HITL request originating from a cloud agent.

        Side effects:

        - Inserts a row into ``popola_hitl`` via :meth:`HITLStore.create`.
        - Best-effort sends a Lark card when :attr:`_lark_notifier` is set.
        """
        deadline_seconds = _ceil_deadline_seconds(timeout_s, self._default_timeout_s)
        meta = dict(metadata or {})
        prompt = _build_cloud_prompt(
            prompt_title=prompt_title,
            prompt_body=prompt_body,
            options=options,
            task_id=task_id,
            cursor_agent_id=cursor_agent_id,
            cursor_run_id=cursor_run_id,
            metadata=meta,
            deadline_seconds=deadline_seconds,
        )
        hitl_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        deadline_at = now + timedelta(seconds=deadline_seconds)
        self._store.create(
            prompt,
            hitl_id=hitl_id,
            task_id=task_id,
            deadline_at=deadline_at,
        )
        request = CloudHITLRequest(
            hitl_id=hitl_id,
            task_id=task_id,
            cursor_agent_id=cursor_agent_id,
            cursor_run_id=cursor_run_id,
            prompt=prompt,
            options=tuple(prompt.options),
            created_at=now,
            deadline_at=deadline_at,
            metadata=meta,
        )
        if self._lark_notifier is not None:
            try:
                self._lark_notifier.send_hitl_card(
                    prompt,
                    hitl_id=request.hitl_id,
                    event_log=event_log,
                    task_id=task_id,
                )
            except Exception as exc:
                logger.warning(
                    "Cloud HITL Lark delivery failed for hitl_id=%s — request still "
                    "recorded; user can answer via MCP/web/CLI fallback. Error: %r",
                    request.hitl_id,
                    exc,
                )
        return request

    def await_answer(
        self,
        hitl_id: str,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 1.0,
    ) -> HITLReply | None:
        """Block until the row is answered, or time out / reach terminal state."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        interval = max(0.05, poll_interval_s)
        while True:
            row = self._store.get(hitl_id)
            if row is None:
                return None
            status = str(row.get("status", ""))
            if status == "answered":
                return self._reply_from_row(hitl_id, row)
            if status in {"timeout", "cancelled"}:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def _reply_from_row(self, hitl_id: str, row: dict[str, Any]) -> HITLReply:
        via_raw = row.get("answered_via")
        via = cast(
            HITLChannel,
            via_raw
            if via_raw
            in (
                "lark",
                "ide",
                "cli",
                "email",
                "signal",
                "mcp",
                "web",
                "cloud",
            )
            else "cloud",
        )
        return HITLReply(
            hitl_id=hitl_id,
            option_id=str(row.get("answer_option_id") or ""),
            via=via,
            reason=_str_or_none(row.get("answer_reason")),
            responder_id=_str_or_none(row.get("answer_responder_id")),
        )

    def submit_answer(
        self,
        hitl_id: str,
        answer_option_id: str,
        *,
        responder_id: str,
        reason: str | None = None,
        channel: HITLChannel = "cloud",
    ) -> tuple[bool, str | None]:
        """Record an answer via :meth:`HITLStore.mark_answered`.

        Returns:
            ``(True, channel)`` when this call won the race, else
            ``(False, already_descriptor)`` where ``already_descriptor`` is
            best-effort ``"<via>:<responder>"`` from the existing row.
        """
        result = self._store.mark_answered(
            hitl_id,
            option_id=answer_option_id,
            via=channel,
            reason=reason,
            responder_id=responder_id,
        )
        if result.ok:
            return True, channel
        existing = self._store.get(hitl_id)
        if existing is None:
            return False, None
        via = _str_or_none(existing.get("answered_via")) or "unknown"
        rid = _str_or_none(existing.get("answer_responder_id")) or ""
        already = f"{via}:{rid}" if rid else via
        return False, already


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def build_default_bridge(
    connection: sqlite3.Connection,
    *,
    lark_notifier: CloudHITLLarkNotifier | None = None,
    default_timeout_s: float = 600.0,
) -> CloudHITLBridge:
    """Construct a bridge with :class:`HITLStore` on ``connection``."""
    resolved: CloudHITLLarkNotifier | None
    resolved = _NoopCloudLarkNotifier() if lark_notifier is None else lark_notifier
    store = HITLStore(connection)
    return CloudHITLBridge(store, resolved, default_timeout_s=default_timeout_s)


def bridge_for_daemon(
    store: HITLStore | None,
    *,
    send_lark: bool = True,
    default_timeout_s: float = 600.0,
) -> CloudHITLBridge | None:
    """Minimal factory used by :mod:`popolaloom.daemon.rpc` handlers."""
    if store is None:
        return None
    notifier = _DefaultCloudLarkNotifier() if send_lark else _NoopCloudLarkNotifier()
    return CloudHITLBridge(store, notifier, default_timeout_s=default_timeout_s)


__all__ = [
    "CloudHITLBridge",
    "CloudHITLLarkNotifier",
    "CloudHITLRequest",
    "bridge_for_daemon",
    "build_default_bridge",
]
