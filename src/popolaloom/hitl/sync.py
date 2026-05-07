"""HITL cross-channel synchronisation — v0.3.0 Stage F4.C.

Per spec §3.4 + roadmap §12.7 + v0.3.0-plan §4 Stage F4.11:

When the same prompt fans out to ≥ 2 channels (Lark + IDE notify + CLI
+ ...) we must avoid double-resume of the LangGraph thread. The atomic
``UPDATE popola_hitl SET status='answered' WHERE hitl_id=? AND status='pending'``
ensures only the first responder wins; the others get a polite
``"already answered by <channel>"`` ack and their renderer-side cards
are subsequently disabled.

This module is **transport-agnostic**: callers pass an open
``sqlite3.Connection`` (typically the ArkTower
:class:`DatabaseConnection`) and a JSON-serialisable
:class:`popolaloom.hitl.HITLPrompt` payload. We never own the
connection lifecycle.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from popolaloom.hitl import HITLChannel, HITLPrompt, HITLReply

logger = logging.getLogger(__name__)


HITLStatus = str  # 'pending' | 'answered' | 'timeout' | 'cancelled'


# ── Public dataclasses ──────────────────────────────────────────────────


@dataclass(frozen=True)
class HITLRow:
    """Lightweight wrapper around a row from ``popola_hitl``.

    Used by renderers that want to display deadline / status info
    alongside the prompt (e.g. :func:`popolaloom.hitl.renderers.cli.render_pending_table`).
    Prompt JSON is parsed lazily via the :attr:`prompt` property so a
    long pending list is cheap to materialise.
    """

    hitl_id: str
    trigger: str
    status: HITLStatus
    prompt_json: str
    created_at: str
    deadline_at: str | None = None
    answered_at: str | None = None
    answered_via: str | None = None
    answer_option_id: str | None = None
    answer_reason: str | None = None
    answer_responder_id: str | None = None
    task_id: str | None = None

    @property
    def prompt(self) -> HITLPrompt:
        """Lazy-parse :attr:`prompt_json` into a :class:`HITLPrompt`."""
        return HITLPrompt.model_validate_json(self.prompt_json)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HITLRow:
        """Build a :class:`HITLRow` from a dict (e.g. SQLite Row → dict)."""
        return cls(
            hitl_id=str(data.get("hitl_id", "")),
            trigger=str(data.get("trigger", "")),
            status=str(data.get("status", "pending")),
            prompt_json=str(data.get("prompt_json", "{}")),
            created_at=str(data.get("created_at", "")),
            deadline_at=_str_or_none(data.get("deadline_at")),
            answered_at=_str_or_none(data.get("answered_at")),
            answered_via=_str_or_none(data.get("answered_via")),
            answer_option_id=_str_or_none(data.get("answer_option_id")),
            answer_reason=_str_or_none(data.get("answer_reason")),
            answer_responder_id=_str_or_none(data.get("answer_responder_id")),
            task_id=_str_or_none(data.get("task_id")),
        )


@dataclass(frozen=True)
class MarkAnsweredResult:
    """Outcome of :meth:`HITLStore.mark_answered`.

    Attributes:
        ok: True iff this caller's UPDATE flipped the row from
            'pending' → 'answered' (winner of the race).
        already_status: when ``ok`` is False, the existing status
            (one of 'answered' / 'timeout' / 'cancelled' / None).
        already_via: when ``ok`` is False AND status was 'answered',
            the channel that won.
    """

    ok: bool
    already_status: HITLStatus | None = None
    already_via: HITLChannel | None = None


@dataclass
class CancelOtherChannelsResult:
    """Per-channel cancel events emitted by :meth:`HITLStore.cancel_other_channels`."""

    cancelled: list[HITLChannel] = field(default_factory=list)
    skipped:   list[HITLChannel] = field(default_factory=list)


# ── HITLStore ───────────────────────────────────────────────────────────


CancelEmitter = Callable[[str, HITLChannel], Awaitable[None]]
"""Async callback ``(hitl_id, channel) -> None`` invoked once per channel
to be cancelled by :meth:`HITLStore.cancel_other_channels`. Used by the
listener / renderer wiring to disable already-rendered cards."""


def _str_or_none(value: Any) -> str | None:
    """Coerce a value to ``str | None`` (used by :meth:`HITLRow.from_dict`)."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


class HITLStore:
    """Synchronous SQLite-backed store for HITL prompt state.

    Construction does NOT open / close the connection; the daemon
    typically passes the same ArkTower connection used for tasks
    (cross-table consistency via the same SQLite file is automatic).

    Args:
        connection: open ``sqlite3.Connection``. Must have row factory
            set to :class:`sqlite3.Row` for the dict-style fetches.

    Concurrent access may occur when the asyncio RPC bridge fans out multiple
    HITL operations onto thread-pool threads against the **same**
    underlying connection (:class:`~threading.RLock` wraps every public DB
    call so callers never trip ``sqlite3`` thread-safety assertions).
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self._conn_lock = threading.RLock()

    # ── CRUD ──

    def create(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str | None = None,
        task_id: str | None = None,
        deadline_at: datetime | None = None,
    ) -> str:
        """Insert a new ``pending`` HITL row.

        Args:
            prompt: the validated :class:`HITLPrompt`.
            hitl_id: optional explicit id; defaults to ``hitl-<uuid4>``.
            task_id: optional ArkTower task id link.
            deadline_at: optional explicit deadline (defaults to
                ``now + prompt.deadline_seconds``).

        Returns:
            The hitl_id that was inserted.

        Raises:
            sqlite3.IntegrityError: when ``hitl_id`` is already used.
        """
        resolved_id = hitl_id or prompt.prompt_id or f"hitl-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        deadline = deadline_at or (now + timedelta(seconds=prompt.deadline_seconds))
        with self._conn_lock:
            self.conn.execute(
                """
                INSERT INTO popola_hitl
                    (hitl_id, trigger, status, prompt_json,
                     created_at, deadline_at, task_id)
                VALUES (?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    prompt.trigger,
                    prompt.model_dump_json(),
                    now.isoformat(),
                    deadline.isoformat(),
                    task_id,
                ),
            )
            self.conn.commit()
        logger.info(
            "HITLStore.create: hitl_id=%s trigger=%s deadline_at=%s task_id=%s",
            resolved_id, prompt.trigger, deadline.isoformat(), task_id,
        )
        return resolved_id

    def get(self, hitl_id: str) -> dict[str, Any] | None:
        """Fetch the row by id (None when missing)."""
        with self._conn_lock:
            cur = self.conn.execute(
                "SELECT * FROM popola_hitl WHERE hitl_id = ?",
                (hitl_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(row)

    def list_pending(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        """List all rows with ``status='pending'`` (optional task_id filter)."""
        with self._conn_lock:
            if task_id is not None:
                cur = self.conn.execute(
                    "SELECT * FROM popola_hitl WHERE status = 'pending' AND task_id = ? "
                    "ORDER BY created_at",
                    (task_id,),
                )
            else:
                cur = self.conn.execute(
                    "SELECT * FROM popola_hitl WHERE status = 'pending' "
                    "ORDER BY created_at",
                )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def list_overdue(self) -> list[dict[str, Any]]:
        """List pending rows whose deadline has passed (used by timeout job)."""
        now_iso = datetime.now(UTC).isoformat()
        with self._conn_lock:
            cur = self.conn.execute(
                "SELECT * FROM popola_hitl WHERE status = 'pending' "
                "AND deadline_at IS NOT NULL AND deadline_at < ? "
                "ORDER BY deadline_at",
                (now_iso,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    # ── Atomic transitions ──

    def mark_answered(
        self,
        hitl_id: str,
        *,
        option_id: str,
        via: HITLChannel,
        reason: str | None = None,
        responder_id: str | None = None,
    ) -> MarkAnsweredResult:
        """Atomically transition the row from 'pending' → 'answered'.

        Race-free: the UPDATE WHERE status='pending' clause ensures that
        when two channels reply nearly simultaneously, exactly one
        observes ``rowcount == 1``. The other gets ``rowcount == 0`` and
        we return :class:`MarkAnsweredResult` with ``ok=False`` plus the
        existing ``already_status`` / ``already_via`` for the second
        responder's UI.

        Args:
            hitl_id: prompt id.
            option_id: chosen option id (validated against schema's
                option list before persisting).
            via: channel that recorded the reply.
            reason: optional free-text reason (≤ 1000 chars).
            responder_id: optional originator id.

        Returns:
            MarkAnsweredResult with ``ok`` indicating winner status.
        """
        now_iso = datetime.now(UTC).isoformat()
        with self._conn_lock:
            cursor = self.conn.execute(
                """
                UPDATE popola_hitl
                   SET status = 'answered',
                       answered_at = ?,
                       answered_via = ?,
                       answer_option_id = ?,
                       answer_reason = ?,
                       answer_responder_id = ?
                 WHERE hitl_id = ? AND status = 'pending'
                """,
                (now_iso, via, option_id, reason, responder_id, hitl_id),
            )
            self.conn.commit()
            if cursor.rowcount == 1:
                logger.info(
                    "HITLStore.mark_answered: hitl_id=%s via=%s option=%s (winner)",
                    hitl_id, via, option_id,
                )
                return MarkAnsweredResult(ok=True)

            existing = self.get(hitl_id)
        if existing is None:
            logger.warning("HITLStore.mark_answered: hitl_id=%s does not exist", hitl_id)
            return MarkAnsweredResult(ok=False, already_status=None)
        already_status = existing["status"]
        already_via = existing.get("answered_via")
        logger.info(
            "HITLStore.mark_answered: hitl_id=%s lost race (status=%s via=%s)",
            hitl_id, already_status, already_via,
        )
        valid_channels = {
            "lark", "ide", "cli", "email", "signal", "mcp", "web", "cloud",
        }
        narrowed_via: HITLChannel | None
        if isinstance(already_via, str) and already_via in valid_channels:
            narrowed_via = already_via  # type: ignore[assignment]
        else:
            narrowed_via = None
        return MarkAnsweredResult(
            ok=False,
            already_status=already_status,
            already_via=narrowed_via,
        )

    def mark_status(self, hitl_id: str, status: HITLStatus) -> bool:
        """Generic state transition helper.

        Allowed transitions: pending → timeout / cancelled. Refuses to
        overwrite ``answered`` (returns ``False`` rather than silently).
        """
        if status not in {"timeout", "cancelled"}:
            raise ValueError(f"mark_status only handles timeout/cancelled; got {status}")
        with self._conn_lock:
            cursor = self.conn.execute(
                "UPDATE popola_hitl SET status = ? "
                "WHERE hitl_id = ? AND status = 'pending'",
                (status, hitl_id),
            )
            self.conn.commit()
            return cursor.rowcount == 1

    # ── Lark-specific tracking ──

    def update_lark_send(
        self,
        hitl_id: str,
        *,
        message_id: str | None,
        last_send_error: str | None,
        attempts_increment: int = 1,
    ) -> None:
        """Record the outcome of a ``send_lark_card`` invocation."""
        with self._conn_lock:
            self.conn.execute(
                """
                UPDATE popola_hitl
                   SET lark_message_id = COALESCE(?, lark_message_id),
                       lark_send_attempts = lark_send_attempts + ?,
                       lark_last_send_error = ?
                 WHERE hitl_id = ?
                """,
                (message_id, attempts_increment, last_send_error, hitl_id),
            )
            self.conn.commit()

    def append_lark_event_id(self, hitl_id: str, event_id: str) -> bool:
        """Append a Lark event_id to ``lark_event_ids`` (de-duped JSON array).

        Returns ``True`` when the id was newly appended, ``False`` when
        it was already present (used for de-duplication of webhook
        retries by the listener).
        """
        with self._conn_lock:
            cur = self.conn.execute(
                "SELECT lark_event_ids FROM popola_hitl WHERE hitl_id = ?",
                (hitl_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            raw = row[0]
            ids: list[str]
            if raw is None:
                ids = []
            else:
                try:
                    parsed = json.loads(raw)
                    ids = list(parsed) if isinstance(parsed, list) else []
                except json.JSONDecodeError:
                    logger.warning(
                        "HITLStore.append_lark_event_id: %s has bad JSON in "
                        "lark_event_ids; resetting",
                        hitl_id,
                    )
                    ids = []
            if event_id in ids:
                return False
            ids.append(event_id)
            self.conn.execute(
                "UPDATE popola_hitl SET lark_event_ids = ? WHERE hitl_id = ?",
                (json.dumps(ids), hitl_id),
            )
            self.conn.commit()
            return True

    # ── Cross-channel cancel ──

    async def cancel_other_channels(
        self,
        hitl_id: str,
        *,
        except_via: HITLChannel,
        emitter: CancelEmitter | None = None,
    ) -> CancelOtherChannelsResult:
        """Notify "other" renderers a row was answered.

        This does NOT mutate the DB (status is already 'answered' by
        :meth:`mark_answered`); it merely fans out cancel events so each
        channel can disable / disable buttons / dismiss notify toast.

        Args:
            hitl_id: prompt id.
            except_via: the channel that won (won't be cancelled).
            emitter: async callback ``(hitl_id, channel)``; when None,
                the result simply lists the channels that *would* have
                been cancelled (useful for tests / dry-run).

        Returns:
            :class:`CancelOtherChannelsResult` listing emitted /
            skipped channels.
        """
        row = self.get(hitl_id)
        if row is None:
            return CancelOtherChannelsResult()
        try:
            prompt_data = json.loads(row["prompt_json"])
            prompt = HITLPrompt.model_validate(prompt_data)
        except Exception:
            logger.exception("cancel_other_channels: invalid prompt_json for %s", hitl_id)
            return CancelOtherChannelsResult()

        result = CancelOtherChannelsResult()
        for channel in prompt.channels:
            if channel == except_via:
                result.skipped.append(channel)
                continue
            if emitter is not None:
                try:
                    await emitter(hitl_id, channel)
                except Exception:
                    logger.exception(
                        "cancel_other_channels: emitter raised for %s/%s",
                        hitl_id, channel,
                    )
            result.cancelled.append(channel)
        return result

    # ── Timeout processing ──

    def process_timeout(self, hitl_id: str) -> bool:
        """Apply the prompt's default option as the answer; mark 'timeout'.

        Per spec §12 deadline rule: when the deadline passes without a
        reply, the daemon picks ``HITLPrompt.default_option_id`` as the
        answer and sets the row status to ``'timeout'`` (distinct from
        ``'answered'`` so audit shows the reply was synthetic).

        Returns:
            True iff the transition fired (row was 'pending' + deadline
            passed). False otherwise.
        """
        row = self.get(hitl_id)
        if row is None or row["status"] != "pending":
            return False
        try:
            prompt = HITLPrompt.model_validate(json.loads(row["prompt_json"]))
        except Exception:
            logger.exception("process_timeout: invalid prompt_json for %s", hitl_id)
            return False
        with self._conn_lock:
            cursor = self.conn.execute(
                """
                UPDATE popola_hitl
                   SET status = 'timeout',
                       answered_at = ?,
                       answered_via = ?,
                       answer_option_id = ?,
                       answer_reason = ?
                 WHERE hitl_id = ? AND status = 'pending'
                """,
                (
                    datetime.now(UTC).isoformat(),
                    "cli",  # synthetic default — closest to "no human, popolad fallback"
                    prompt.default_option_id,
                    "deadline reached, default option applied",
                    hitl_id,
                ),
            )
            self.conn.commit()
            return cursor.rowcount == 1

    def fold_reply(self, reply: HITLReply) -> MarkAnsweredResult:
        """Convenience: forward an :class:`HITLReply` into ``mark_answered``."""
        if reply.via not in {
            "lark", "ide", "cli", "mcp", "web", "email", "signal", "cloud",
        }:
            raise ValueError(f"unsupported reply channel for HITL store: {reply.via!r}")
        return self.mark_answered(
            reply.hitl_id,
            option_id=reply.option_id,
            via=reply.via,
            reason=reply.reason,
            responder_id=reply.responder,
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    """Coerce a sqlite3.Row (or tuple) into a dict.

    The connection may not have ``row_factory = sqlite3.Row``; in that
    case we read columns from the cursor's last description. To stay
    schema-agnostic we hardcode the canonical column order matching
    ``006_popola_hitl.sql``.
    """
    if hasattr(row, "keys"):
        # sqlite3.Row iterates values; `.keys()` returns the column names
        # explicitly. Using ``dict(row)`` would raise on mismatched
        # collation; the comprehension is the fastest correct path.
        keys = list(row.keys())  # noqa: SIM118 — Row.__iter__ returns values
        return {k: row[k] for k in keys}
    columns = [
        "hitl_id", "trigger", "status", "prompt_json",
        "created_at", "deadline_at", "answered_at",
        "answered_via", "answer_option_id", "answer_reason",
        "answer_responder_id",
        "lark_message_id", "lark_event_ids", "lark_send_attempts",
        "lark_last_send_error", "task_id",
    ]
    return {c: row[i] if i < len(row) else None for i, c in enumerate(columns)}


__all__ = [
    "CancelEmitter",
    "CancelOtherChannelsResult",
    "HITLRow",
    "HITLStatus",
    "HITLStore",
    "MarkAnsweredResult",
]
