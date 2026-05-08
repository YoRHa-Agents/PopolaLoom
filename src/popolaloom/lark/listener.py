"""Lark event-consume listener — v0.3.0 Stage F4.D inbound side.

Per spec §3.4 + roadmap §12.8.2: PopolaLoom must consume two Lark events
to receive HITL replies:

- ``card.action.trigger_v1`` — user clicked an option button
- ``im.message.receive_v1``  — user typed ``/popola feedback <id>
  --option=<id>`` in the chat

We invoke ``lark-cli event consume <events> --as bot --output ndjson``
as an asyncio subprocess and consume stdout NDJSON line by line. The
helper waits for ``EVENT_CONSUME_READY`` on stderr (lark-cli emits this
once auth + websocket subscription is live) before considering the
listener "started"; the supervisor (:mod:`popolaloom.lark.supervisor`)
restarts the subprocess if it dies (≤ 3 retries).

The text-feedback regex is documented in :data:`POPOLA_FEEDBACK_PATTERN`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────


READY_MARKER: str = "EVENT_CONSUME_READY"
"""lark-cli emits this on stderr once event-consume is bound + subscribed."""

READY_TIMEOUT_S: float = 30.0
"""Max wait for the ready marker before declaring the subprocess dead."""

DEFAULT_EVENTS: tuple[str, ...] = (
    "card.action.trigger_v1",
    "im.message.receive_v1",
)
"""Lark events PopolaLoom subscribes to for HITL replies."""

POPOLA_FEEDBACK_PATTERN = re.compile(
    r"/popola\s+feedback\s+"
    r"(?P<hitl_id>[A-Za-z0-9_\-]+)"
    r"\s+--option\s*=\s*(?P<option_id>[A-Za-z0-9_\-]+)"
    r"(?:\s+--reason\s*=\s*\"?(?P<reason>[^\"]*?)\"?)?\s*$"
)
"""Regex for the text-channel reply fallback.

Matches things like::

    /popola feedback hitl-abc-123 --option=approve
    /popola feedback hitl-abc-123 --option=reject --reason="bad diff"
"""


# ── Public dataclasses ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LarkEventCallbacks:
    """Bundle of async callbacks the listener invokes on parsed events.

    Each callback receives the raw event dict (NDJSON-decoded) plus any
    parsed payload. Failures inside a callback are logged but do not
    crash the listener (per workspace rule "No Silent Failures" — we
    log + skip the bad event, never silently drop).

    Attributes:
        on_card_action: invoked for ``card.action.trigger_v1`` events;
            second arg is the parsed ``(hitl_id, option_id)`` tuple.
        on_text_feedback: invoked for ``im.message.receive_v1`` events
            that match :data:`POPOLA_FEEDBACK_PATTERN`; second arg is the
            parsed match dict ``{hitl_id, option_id, reason?}``.
        on_unauthorized: invoked when an event arrives from a sender
            whose ``open_id`` is not in :attr:`LarkListener.allowed_responders`.
    """

    on_card_action: Callable[[dict[str, Any], tuple[str, str]], Awaitable[None]] | None = None
    on_text_feedback: Callable[[dict[str, Any], dict[str, str]], Awaitable[None]] | None = None
    on_unauthorized: Callable[[dict[str, Any], str], Awaitable[None]] | None = None


@dataclass
class _ListenerState:
    """Mutable runtime state of a :class:`LarkListener`."""

    proc: asyncio.subprocess.Process | None = None
    started_at: datetime | None = None
    stopped: bool = False
    last_event_at: datetime | None = None
    events_seen: int = 0
    parse_errors: int = 0
    unauthorized: int = 0
    stderr_buffer: list[str] = field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────


def _lark_cli_bin() -> str:
    """Resolve lark-cli (mirror of :func:`hitl.renderers.lark._lark_cli_bin`)."""
    explicit = os.getenv("LARK_CLI_BIN")
    if explicit:
        return explicit
    found = shutil.which("lark-cli")
    if not found:
        raise FileNotFoundError(
            "lark-cli not on PATH; set LARK_CLI_BIN or install lark-cli"
        )
    return found


def _extract_sender_open_id(event: dict[str, Any]) -> str | None:
    """Pluck ``sender.open_id`` / ``operator.open_id`` from a Lark event."""
    inner = event.get("event") if isinstance(event.get("event"), dict) else event
    sender = inner.get("sender") if isinstance(inner, dict) else None
    if isinstance(sender, dict):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, dict):
            open_id = sender_id.get("open_id")
            if isinstance(open_id, str) and open_id:
                return open_id
        oid = sender.get("open_id")
        if isinstance(oid, str) and oid:
            return oid
    operator = inner.get("operator") if isinstance(inner, dict) else None
    if isinstance(operator, dict):
        oid = operator.get("open_id")
        if isinstance(oid, str) and oid:
            return oid
    return None


def _extract_event_type(event: dict[str, Any]) -> str | None:
    """Lark events carry ``header.event_type`` (v2)."""
    header = event.get("header")
    if isinstance(header, dict):
        et = header.get("event_type")
        if isinstance(et, str) and et:
            return et
    schema = event.get("schema")
    if schema == "1.0":
        # v1 events use top-level ``event_type`` in event
        inner = event.get("event")
        if isinstance(inner, dict):
            et = inner.get("type")
            if isinstance(et, str) and et:
                return et
    return None


def _extract_text_message(event: dict[str, Any]) -> str | None:
    """Pluck text content from im.message.receive_v1 event."""
    inner = event.get("event") if isinstance(event.get("event"), dict) else None
    if not isinstance(inner, dict):
        return None
    message = inner.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    text = decoded.get("text")
    return text if isinstance(text, str) else None


# ── LarkListener ────────────────────────────────────────────────────────


class LarkListener:
    """Async wrapper around ``lark-cli event consume`` subprocess.

    Per workspace rule "No Silent Failures": the listener logs every
    parse / authorisation failure and dispatches them to the
    :attr:`callbacks.on_unauthorized` callback when configured; the
    supervisor escalates after 3 consecutive subprocess deaths.

    Args:
        callbacks: :class:`LarkEventCallbacks`; nullable callbacks become no-ops.
        allowed_responders: list of ``open_id`` allowed to reply (default:
            empty → all rejected with ``unauthorized``; per D3.7 the
            HITL daemon adds ``target_open_id`` here at startup).
        events: list of Lark event types to subscribe to (default: card
            action + text message).
        bin_override: optional explicit ``lark-cli`` path (test).
    """

    def __init__(
        self,
        callbacks: LarkEventCallbacks,
        *,
        allowed_responders: list[str] | None = None,
        events: tuple[str, ...] = DEFAULT_EVENTS,
        bin_override: str | None = None,
    ) -> None:
        self.callbacks = callbacks
        self.allowed_responders = list(allowed_responders or [])
        self.events = events
        self.bin_override = bin_override
        self._state = _ListenerState()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready_event = asyncio.Event()

    # ── lifecycle ──

    async def start(self) -> None:
        """Spawn the lark-cli subprocess and wait for ``EVENT_CONSUME_READY``.

        Raises:
            FileNotFoundError: lark-cli not resolvable.
            RuntimeError: ready marker not received within :data:`READY_TIMEOUT_S`.
        """
        if self._state.proc is not None:
            raise RuntimeError("LarkListener already started")
        bin_path = self.bin_override or _lark_cli_bin()
        events_csv = ",".join(self.events)
        argv = [
            bin_path, "event", "consume", events_csv,
            "--as", "bot",
            "--output", "ndjson",
        ]
        logger.info("starting LarkListener: %s", " ".join(argv))
        self._state.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._state.started_at = datetime.now(UTC)
        assert self._state.proc.stdout is not None
        assert self._state.proc.stderr is not None

        self._stdout_task = asyncio.create_task(self._consume_stdout())
        self._stderr_task = asyncio.create_task(self._consume_stderr())

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=READY_TIMEOUT_S)
        except TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                f"lark-cli event consume did not emit {READY_MARKER!r} within "
                f"{READY_TIMEOUT_S}s; stderr tail: "
                f"{''.join(self._state.stderr_buffer[-10:])!r}"
            ) from exc

    async def stop(self, timeout_s: float = 5.0) -> None:
        """Send SIGTERM, optionally SIGKILL after grace, then await exit.

        Idempotent — second call is a no-op once the proc is gone.
        """
        self._state.stopped = True
        proc = self._state.proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                logger.warning("lark-cli did not exit on SIGTERM; sending SIGKILL")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # log + continue (No Silent Failures)
                    logger.exception("listener task raised on shutdown")
        self._state.proc = None

    @property
    def is_alive(self) -> bool:
        """True iff subprocess is still running."""
        proc = self._state.proc
        return proc is not None and proc.returncode is None

    @property
    def stats(self) -> dict[str, Any]:
        """Snapshot of listener stats (used by supervisor + nines)."""
        return {
            "is_alive": self.is_alive,
            "started_at": self._state.started_at.isoformat() if self._state.started_at else None,
            "events_seen": self._state.events_seen,
            "parse_errors": self._state.parse_errors,
            "unauthorized": self._state.unauthorized,
            "last_event_at": (
                self._state.last_event_at.isoformat()
                if self._state.last_event_at
                else None
            ),
        }

    # ── stdout / stderr consumers ──

    async def _consume_stdout(self) -> None:
        """Read NDJSON line by line; dispatch to handlers."""
        proc = self._state.proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                line = await proc.stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LarkListener stdout read failed")
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError as exc:
                self._state.parse_errors += 1
                logger.warning("LarkListener: bad NDJSON line: %s", exc)
                continue
            if not isinstance(event, dict):
                self._state.parse_errors += 1
                continue
            self._state.events_seen += 1
            self._state.last_event_at = datetime.now(UTC)
            try:
                await self._dispatch_event(event)
            except Exception:
                logger.exception("LarkListener: dispatch failed for event")

    async def _consume_stderr(self) -> None:
        """Wait for the ready marker; buffer rest for diagnostics."""
        proc = self._state.proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                line = await proc.stderr.readline()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LarkListener stderr read failed")
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            self._state.stderr_buffer.append(text)
            if len(self._state.stderr_buffer) > 200:
                self._state.stderr_buffer = self._state.stderr_buffer[-100:]
            if READY_MARKER in text and not self._ready_event.is_set():
                logger.info("LarkListener ready (saw %s)", READY_MARKER)
                self._ready_event.set()

    # ── event dispatch ──

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        event_type = _extract_event_type(event) or ""
        sender = _extract_sender_open_id(event)
        if self.allowed_responders and sender not in self.allowed_responders:
            self._state.unauthorized += 1
            logger.warning(
                "LarkListener: unauthorized responder %s (event_type=%s); ignoring",
                sender, event_type,
            )
            if self.callbacks.on_unauthorized is not None:
                try:
                    await self.callbacks.on_unauthorized(event, sender or "")
                except Exception:
                    logger.exception("on_unauthorized callback raised")
            return

        if event_type.startswith("card.action.trigger") or event_type == "card.action.trigger_v1":
            await self._handle_card_action(event)
        elif event_type.startswith("im.message.receive") or event_type == "im.message.receive_v1":
            await self._handle_text_feedback(event)
        else:
            logger.debug("LarkListener: ignoring event_type=%r", event_type)

    async def _handle_card_action(self, event: dict[str, Any]) -> None:
        from popolaloom.lark.card_templates import extract_action_value

        inner = event.get("event")
        action = inner.get("action") if isinstance(inner, dict) else None
        if not isinstance(action, dict):
            self._state.parse_errors += 1
            logger.warning("card.action.trigger event missing event.action")
            return
        raw_value = action.get("value")
        try:
            if isinstance(raw_value, dict):
                hitl_id = raw_value.get("hitl_id")
                option_id = raw_value.get("option_id")
                if not isinstance(hitl_id, str) or not isinstance(option_id, str):
                    raise ValueError(f"missing keys in dict value: {raw_value}")
                # M7 / SECURITY R4: dispatch on template_version so a future
                # v2 card cannot satisfy v1 dedup keys (and vice versa).
                # Unknown versions are rejected as ``unauthorized`` so the
                # callback path treats the event the same as a non-allowlist
                # responder (No Silent Failures). Missing template_version
                # defaults to ``"v1"`` for backward compat with v0.8.5
                # cards that pre-date the version stamp.
                template_version_raw = raw_value.get("template_version", "v1")
                template_version = (
                    template_version_raw
                    if isinstance(template_version_raw, str)
                    else str(template_version_raw)
                )
                if template_version not in SUPPORTED_TEMPLATE_VERSIONS:
                    self._state.unauthorized += 1
                    logger.warning(
                        "card.action.trigger rejected unknown template_version=%r "
                        "(supported: %s) hitl_id=%s",
                        template_version,
                        sorted(SUPPORTED_TEMPLATE_VERSIONS),
                        hitl_id,
                    )
                    if self.callbacks.on_unauthorized is not None:
                        sender = _extract_sender_open_id(event) or ""
                        try:
                            await self.callbacks.on_unauthorized(event, sender)
                        except Exception:
                            logger.exception(
                                "on_unauthorized callback raised on "
                                "unsupported template_version"
                            )
                    return
                parsed = (hitl_id, option_id)
            else:
                parsed = extract_action_value(str(raw_value))
        except ValueError as exc:
            self._state.parse_errors += 1
            logger.warning("card.action.trigger value parse failed: %s", exc)
            return
        if self.callbacks.on_card_action is not None:
            await self.callbacks.on_card_action(event, parsed)

    async def _handle_text_feedback(self, event: dict[str, Any]) -> None:
        text = _extract_text_message(event)
        if text is None:
            self._state.parse_errors += 1
            return
        match = POPOLA_FEEDBACK_PATTERN.search(text)
        if match is None:
            logger.debug("LarkListener: text not a /popola feedback command: %r", text)
            return
        parsed = {
            "hitl_id": match.group("hitl_id"),
            "option_id": match.group("option_id"),
        }
        reason = match.group("reason")
        if reason is not None:
            parsed["reason"] = reason.strip()
        if self.callbacks.on_text_feedback is not None:
            await self.callbacks.on_text_feedback(event, parsed)


## ── public parse helpers (used by renderer; v0.3.0 F4.B) ────────────────


@dataclass
class LarkEventResult:
    """Uniform parse result for a single Lark event (renderer adapter).

    Wraps :func:`parse_card_action` / :func:`parse_message_command` output
    so the renderer's ``parse_reply`` can return a single shape regardless
    of which event flavour it processed.

    Attributes:
        ok: whether the event yielded a usable HITL reply.
        reply: optional :class:`popolaloom.hitl.HITLReply` (None on failure).
        sender_open_id: best-effort responder open_id.
        event_id: optional Lark CloudEvents id (de-dup hint).
        unauthorized: True when sender was rejected by allowed_responders.
        reason: short diagnostic when ok=False.
        raw: original event dict (for audit logs).
    """

    ok: bool
    reply: Any = None
    sender_open_id: str | None = None
    event_id: str | None = None
    unauthorized: bool = False
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _lazy_lark_allowed_responders() -> list[str]:
    """Defer the env lookup to avoid circular import with :mod:`popolaloom.lark`."""
    from popolaloom.lark import lark_allowed_responders

    return lark_allowed_responders()


SUPPORTED_TEMPLATE_VERSIONS: frozenset[str] = frozenset({"v1"})
"""Set of accepted ``card_metadata.template_version`` values (M7 dispatch).

Per ``SECURITY_CHECKLIST.md`` §5 R4 + ``lark-card-spec.md`` §4.3: card
clicks carrying an unknown ``template_version`` MUST be rejected at
the listener boundary (an unauthorised callback), so a v0.8.8+ ``v2``
card cannot replay against a v1 listener and vice-versa. The MVP only
honours ``"v1"``; expanding this set is the contract for future
versions (each new card template adds itself here in lockstep with
its receiver code path).
"""


def parse_card_action(
    event: dict[str, Any],
    *,
    allowed_responders: list[str] | None = None,
) -> LarkEventResult:
    """Parse a ``card.action.trigger_v1`` event into a :class:`LarkEventResult`.

    Drives the renderer-side ``parse_reply`` pipeline; the listener's
    own callbacks-based dispatch (:meth:`LarkListener._handle_card_action`)
    is the runtime path used inside the consume loop.

    v0.8.7 M7 (REVIEW.md): the action value's ``template_version`` is
    consulted before any other parse step; unknown versions are
    rejected with ``unauthorized=True`` so a forged / replayed card
    from a different template generation cannot resolve the row.
    """
    from popolaloom.hitl import HITLReply
    from popolaloom.lark.card_templates import extract_button_value

    header = event.get("header", {}) or {}
    event_id = header.get("event_id") if isinstance(header, dict) else None
    sender = _extract_sender_open_id(event)
    inner = event.get("event", {}) or {}
    action = inner.get("action") if isinstance(inner, dict) else None
    value = action.get("value") if isinstance(action, dict) else None

    # M7 dispatch: reject unknown template_version BEFORE the value is
    # destructured into hitl_id / option_id so a v2 card cannot ride
    # the v1 dedup keys (per SECURITY R4 + spec §4.3). Missing values
    # default to ``"v1"`` for backward compat with v0.8.5 cards that
    # pre-date the version stamp.
    template_version = "v1"
    if isinstance(value, dict):
        raw_version = value.get("template_version")
        if isinstance(raw_version, str) and raw_version:
            template_version = raw_version
    if template_version not in SUPPORTED_TEMPLATE_VERSIONS:
        return LarkEventResult(
            ok=False,
            sender_open_id=sender,
            event_id=event_id,
            unauthorized=True,
            reason=(
                f"unsupported template_version {template_version!r}; "
                f"expected one of {sorted(SUPPORTED_TEMPLATE_VERSIONS)}"
            ),
            raw=event,
        )

    hitl_id, option_id = extract_button_value(value if value is not None else {})
    if not hitl_id or not option_id:
        return LarkEventResult(
            ok=False,
            sender_open_id=sender,
            event_id=event_id,
            reason="missing hitl_id/option_id in action.value",
            raw=event,
        )
    whitelist = (
        allowed_responders
        if allowed_responders is not None
        else _lazy_lark_allowed_responders()
    )
    if whitelist and (sender is None or sender not in whitelist):
        return LarkEventResult(
            ok=False,
            sender_open_id=sender,
            event_id=event_id,
            unauthorized=True,
            reason=f"sender {sender!r} not in allowed_responders",
            raw=event,
        )
    reply = HITLReply(
        hitl_id=hitl_id, option_id=option_id, via="lark", responder=sender
    )
    return LarkEventResult(
        ok=True,
        reply=reply,
        sender_open_id=sender,
        event_id=event_id,
        raw=event,
    )


def parse_message_command(
    event: dict[str, Any],
    *,
    allowed_responders: list[str] | None = None,
) -> LarkEventResult:
    """Parse an ``im.message.receive_v1`` event into a :class:`LarkEventResult`.

    Looks for the ``/popola feedback <hitl_id> --option=<id> [--reason=...]``
    pattern (see :data:`POPOLA_FEEDBACK_PATTERN`).
    """
    from popolaloom.hitl import HITLReply

    header = event.get("header", {}) or {}
    event_id = header.get("event_id") if isinstance(header, dict) else None
    sender = _extract_sender_open_id(event)
    text = _extract_text_message(event) or ""
    match = POPOLA_FEEDBACK_PATTERN.search(text.strip())
    if match is None:
        return LarkEventResult(
            ok=False,
            sender_open_id=sender,
            event_id=event_id,
            reason="not a feedback command",
            raw=event,
        )
    whitelist = (
        allowed_responders
        if allowed_responders is not None
        else _lazy_lark_allowed_responders()
    )
    if whitelist and (sender is None or sender not in whitelist):
        return LarkEventResult(
            ok=False,
            sender_open_id=sender,
            event_id=event_id,
            unauthorized=True,
            reason=f"sender {sender!r} not in allowed_responders",
            raw=event,
        )
    hitl_id = match.group("hitl_id")
    option_id = match.group("option_id")
    reason = match.group("reason")
    reply = HITLReply(
        hitl_id=hitl_id,
        option_id=option_id,
        via="lark",
        reason=(reason.strip() if reason else None),
        responder=sender,
    )
    return LarkEventResult(
        ok=True,
        reply=reply,
        sender_open_id=sender,
        event_id=event_id,
        raw=event,
    )


__all__ = [
    "DEFAULT_EVENTS",
    "LarkEventCallbacks",
    "LarkEventResult",
    "LarkListener",
    "POPOLA_FEEDBACK_PATTERN",
    "READY_MARKER",
    "READY_TIMEOUT_S",
    "SUPPORTED_TEMPLATE_VERSIONS",
    "parse_card_action",
    "parse_message_command",
]
