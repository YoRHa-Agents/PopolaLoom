"""Cloud-runtime poller — translates Cursor Cloud Agent run lifecycle into
PopolaLoom EventLog events + TaskState transitions.

Used by Supervisor._spawn_cloud as the background liveness driver for tasks
created via /v1/agents. Replaces the local subprocess.wait() pattern with
a polling loop because cloud agents have no OS-level handle on our side.

v0.8.5 (Stage 2 of the Cloud Agent integration). See
.local/research/v0.8.5_cloud_agent/research.md §7 (phased rollout).

v0.8.8 (T2.1.1) extends the loop with multi-run plumbing per
``event-merge-spec.md`` §2:

- ``run_index`` (0-based ordinal within the agent) is propagated through
  every ``cloud.run_status`` and terminal ``task.*`` envelope so consumers
  can group / sort by ``(run_index, seq)`` without out-of-band lookup.
- ``cloud.run_started`` is emitted once at the start of :meth:`run`,
  bracketing the inner stream of ``cloud.run_status`` / ``cloud.sse.*``.
- ``cloud.run_finished`` is emitted at the terminal phase (alongside the
  existing ``task.{completed,failed,canceled}`` event).
- :meth:`_reconcile_run_index` is the lazy reconciliation helper for
  out-of-band follow-ups created via cursor.com (DECISIONS.md OQ-3) — it
  is invoked **only** when the supervisor's authoritative
  ``TaskHandle.cloud_runs`` map has no entry for the current ``run_id``.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from popolaloom.adapters.cursor_cloud import CloudCursorClient, CursorCloudError
from popolaloom.daemon.cloud_events import record_run_finished, record_run_started
from popolaloom.daemon.event_log import (
    EventLog,
    record_busy_dispatched,
    record_busy_queued,
    record_busy_timeout,
)
from popolaloom.daemon.state import StateStore, TaskState

if TYPE_CHECKING:
    from popolaloom.daemon.main import BusyStrategyConfig

logger = logging.getLogger(__name__)

_PHASE_MAP: dict[str, TaskState] = {
    "CREATING": TaskState.STARTING,
    "RUNNING": TaskState.RUNNING,
    "FINISHED": TaskState.COMPLETED,
    "ERROR": TaskState.FAILED,
    "CANCELLED": TaskState.CANCELED,
    "EXPIRED": TaskState.FAILED,
}

_TERMINAL_PHASES: frozenset[str] = frozenset(
    {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}
)


def _utc_ts() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe_on_exit(
    callback: Callable[[str, int], None] | None,
    task_id: str,
    exit_code: int,
) -> None:
    if callback is None:
        return
    try:
        callback(task_id, exit_code)
    except Exception:  # noqa: BLE001
        logger.exception("on_exit callback failed for task %s", task_id)


@dataclass
class CloudPollLoop:
    """One-task cloud polling loop.

    v0.8.6 (T2.2.2): optional ``wake_event`` parameter lets external signalers
    (e.g., :class:`SSEReader.terminal_hint`) interrupt the inter-poll sleep so
    the poller can confirm a terminal phase faster than the default
    ``interval_s`` cadence. The signal is **advisory only** — the next iteration
    still calls :meth:`CloudCursorClient.get_run` to obtain the authoritative
    phase. Backwards compatible: ``wake_event=None`` (default) keeps the v0.8.5
    behaviour of a plain :func:`time.sleep`.

    v0.8.8 (T2.1.1) adds ``run_index`` — the 0-based ordinal of this run
    within its agent (per ``event-merge-spec.md`` §2.2). The default ``0``
    keeps every existing v0.8.5/0.8.6 caller compatible (single-run task =
    implicit run-0); follow-ups dispatched via
    :meth:`CloudCursorClient.create_followup_run` carry ``1, 2, …``. The
    field is stamped into every ``cloud.run_status`` and terminal
    ``task.*`` envelope; it also becomes the ``data.run_index`` on the
    ``cloud.run_started`` and ``cloud.run_finished`` brackets emitted by
    :meth:`_emit_run_started_marker` / :meth:`_emit_run_finished_marker`.
    """

    task_id: str
    agent_id: str
    run_id: str
    client: CloudCursorClient
    state_store: StateStore
    event_log: EventLog
    on_exit: Callable[[str, int], None] | None
    interval_s: float = 2.0
    max_polls: int = 1800
    retry_max: int = 5
    wake_event: threading.Event | None = None
    run_index: int = 0

    def _poll_run_body(self) -> dict[str, Any]:
        last_exc: CursorCloudError | None = None
        for attempt in range(self.retry_max):
            try:
                return self.client.get_run(self.agent_id, self.run_id)
            except CursorCloudError as exc:
                last_exc = exc
                if not exc.is_retryable or attempt >= self.retry_max - 1:
                    raise
                backoff = 0.5 * (2**attempt)
                logger.warning(
                    "cloud poll retry task=%s agent=%s run=%s attempt=%d/%d "
                    "backoff=%.2fs: %s",
                    self.task_id,
                    self.agent_id,
                    self.run_id,
                    attempt + 1,
                    self.retry_max,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def _resolved_run_index(self) -> int:
        """Return the run_index, lazily reconciling on the missing-index path.

        v0.8.8 lazy reconciliation; see DECISIONS.md OQ-3 + event-merge-spec.md
        §7 row 1. The supervisor populates :attr:`TaskHandle.cloud_runs` at
        run-creation time (before ``POST /v1/agents`` / ``POST /v1/agents/{id}/runs``
        returns), so the common in-process path requires zero extra REST
        calls — we return :attr:`run_index` immediately. Reconciliation
        only fires when the supervisor never observed this run (e.g., a
        manual follow-up via cursor.com), in which case we walk
        :meth:`_reconcile_run_index` to derive a best-effort ordinal.
        """
        handle = self.state_store.get(self.task_id)
        if handle is None:
            return self.run_index
        meta = handle.cloud_runs.get(self.run_id)
        if isinstance(meta, dict):
            cached = meta.get("run_index")
            if isinstance(cached, int):
                return cached
        # Missing-index path — only here do we consider hitting the API.
        return self._reconcile_run_index()

    def _reconcile_run_index(self) -> int:
        """Lazy reconciliation hook for out-of-band follow-ups (OQ-3).

        v0.8.8 lazy reconciliation; see DECISIONS.md OQ-3 + event-merge-spec.md
        §7 row 1. The full implementation walks ``GET /v1/agents/{id}/runs``
        oldest-first to derive an ordinal; that REST traversal lands in
        T2.1.3's ``_retrying_request`` infra so a 429 on the reconcile path
        does not double-count against the regular poll budget. For T2.1.1
        the helper falls back to the in-process :attr:`run_index` and
        emits a ``cloud.run_index_reconciled`` event so SREs can detect
        runaway out-of-band follow-ups even before the full walk lands.

        Returns:
            The reconciled ``run_index``; falls back to :attr:`run_index`
            (typically ``0``) when the upstream ``GET /runs`` walk yields
            no match (or has not yet been wired in T2.1.3).
        """
        fallback = self.run_index
        self.event_log.append(
            "cloud.run_index_reconciled",
            {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "run_id": self.run_id,
                "run_index": fallback,
                "method": "fallback_inprocess",
            },
        )
        return fallback

    def _emit_run_started_marker(self) -> None:
        """Emit the ``cloud.run_started`` bracket once at loop entry (I-10).

        Called from :meth:`run` before the polling loop begins; the
        ``time`` of this envelope is therefore the earliest among all
        events for this ``run_id``, satisfying I-10. The supervisor MAY
        emit the same event earlier (at ``POST /v1/agents/{id}/runs``
        completion) for the bracket to land before the first poll —
        this method is the sole source-of-truth in v0.8.8 to keep the
        contract enforceable from one module.
        """
        record_run_started(
            self.event_log,
            task_id=self.task_id,
            agent_id=self.agent_id,
            run_id=self.run_id,
            run_index=self._resolved_run_index(),
            started_at=_utc_ts(),
        )

    def _emit_run_finished_marker(self, terminal_phase: str, exit_code: int) -> None:
        """Emit the ``cloud.run_finished`` bracket at terminal phase (I-10)."""
        record_run_finished(
            self.event_log,
            task_id=self.task_id,
            agent_id=self.agent_id,
            run_id=self.run_id,
            run_index=self._resolved_run_index(),
            terminal_phase=terminal_phase,
            ended_at=_utc_ts(),
            exit_code=exit_code,
        )

    def _emit_run_status(self, phase: str, prev_phase: str | None) -> None:
        # I-1 sole-writer: only this module writes cloud_phase via StateStore.update
        # (see state-source-of-truth.md §1.2 rule 1; CI guard in tests/conftest.py
        # via test_invariant_i1_sole_writer_of_cloud_phase enforces this at PR time).
        # v0.8.8 (T2.1.1): stamp run_index into data so the renderer's
        # `(run_index, seq)` sort key (event-merge-spec.md §3.4) works
        # for cloud.run_status envelopes too — not just cloud.sse.*.
        self.event_log.append(
            "cloud.run_status",
            {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "run_id": self.run_id,
                "run_index": self._resolved_run_index(),
                "phase": phase,
                "prev_phase": prev_phase,
                "ts": _utc_ts(),
            },
        )

    def _terminal_exit_code(self, phase: str) -> int:
        if phase == "FINISHED":
            return 0
        if phase == "CANCELLED":
            return -2
        return 1

    def run(self) -> None:
        """Block until terminal status or max_polls; called from a daemon thread."""
        prev_phase: str | None = None
        successful_polls = 0
        # v0.8.8 (T2.1.1) — emit cloud.run_started at loop entry so I-10's
        # bracket invariant holds (this envelope's time is <= every other
        # envelope's time for this run_id). The supervisor MAY emit an
        # earlier instance from the run-creation site; the renderer dedups
        # them on (type, run_id) but for v0.8.8 we keep this as the sole
        # in-tree source so the contract is enforceable from one module.
        self._emit_run_started_marker()
        try:
            while True:
                try:
                    body = self._poll_run_body()
                except CursorCloudError as exc:
                    logger.error(
                        "cloud poll failed (non-retryable or exhausted) task=%s: %s",
                        self.task_id,
                        exc,
                    )
                    self.state_store.update(
                        self.task_id,
                        state=TaskState.FAILED,
                        cloud_phase=prev_phase,
                    )
                    self._emit_run_finished_marker("ERROR", 1)
                    self.event_log.append(
                        "task.failed",
                        {
                            "task_id": self.task_id,
                            "exit_code": 1,
                            "runtime": "cloud",
                            "agent_id": self.agent_id,
                            "run_id": self.run_id,
                            "run_index": self._resolved_run_index(),
                            "terminal_phase": "ERROR",
                            "error_kind": "cloud_run_error",
                            "error_detail": str(exc),
                            "error": {
                                "error_type": type(exc).__name__,
                                "is_retryable": exc.is_retryable,
                                "message": str(exc),
                            },
                        },
                    )
                    _safe_on_exit(self.on_exit, self.task_id, 1)
                    return

                successful_polls += 1
                raw_status = body.get("status")
                phase = (
                    str(raw_status).strip().upper()
                    if raw_status is not None
                    else ""
                )
                if not phase:
                    phase = "UNKNOWN"

                if phase not in _PHASE_MAP:
                    logger.warning(
                        "unknown cloud run status %r for task=%s — treating as RUNNING",
                        raw_status,
                        self.task_id,
                    )
                    mapped = TaskState.RUNNING
                else:
                    mapped = _PHASE_MAP[phase]

                if phase in _TERMINAL_PHASES:
                    self._emit_run_status(phase, prev_phase)
                    self.state_store.update(
                        self.task_id,
                        state=mapped,
                        cloud_phase=phase,
                    )
                    exit_code = self._terminal_exit_code(phase)
                    self._emit_run_finished_marker(phase, exit_code)
                    if phase == "FINISHED":
                        self.event_log.append(
                            "task.completed",
                            {
                                "task_id": self.task_id,
                                "exit_code": 0,
                                "runtime": "cloud",
                                "agent_id": self.agent_id,
                                "run_id": self.run_id,
                                "run_index": self._resolved_run_index(),
                                "terminal_phase": "FINISHED",
                            },
                        )
                    elif phase == "CANCELLED":
                        self.event_log.append(
                            "task.canceled",
                            {
                                "task_id": self.task_id,
                                "exit_code": -2,
                                "runtime": "cloud",
                                "agent_id": self.agent_id,
                                "run_id": self.run_id,
                                "run_index": self._resolved_run_index(),
                                "terminal_phase": "CANCELLED",
                            },
                        )
                    elif phase == "EXPIRED":
                        self.event_log.append(
                            "task.failed",
                            {
                                "task_id": self.task_id,
                                "exit_code": 1,
                                "runtime": "cloud",
                                "agent_id": self.agent_id,
                                "run_id": self.run_id,
                                "run_index": self._resolved_run_index(),
                                "terminal_phase": "EXPIRED",
                                "error_kind": "cloud_run_expired",
                            },
                        )
                    else:
                        self.event_log.append(
                            "task.failed",
                            {
                                "task_id": self.task_id,
                                "exit_code": 1,
                                "runtime": "cloud",
                                "agent_id": self.agent_id,
                                "run_id": self.run_id,
                                "run_index": self._resolved_run_index(),
                                "terminal_phase": "ERROR",
                                "error_kind": "cloud_run_error",
                            },
                        )
                    logger.info(
                        "cloud run terminal task=%s phase=%s exit_code=%s",
                        self.task_id,
                        phase,
                        exit_code,
                    )
                    _safe_on_exit(self.on_exit, self.task_id, exit_code)
                    return

                if phase != prev_phase:
                    self._emit_run_status(phase, prev_phase)
                self.state_store.update(
                    self.task_id,
                    state=mapped,
                    cloud_phase=phase,
                )
                prev_phase = phase

                if successful_polls >= self.max_polls:
                    logger.error(
                        "cloud poll timeout task=%s after %d polls",
                        self.task_id,
                        successful_polls,
                    )
                    self.state_store.update(
                        self.task_id,
                        state=TaskState.FAILED,
                        cloud_phase=prev_phase,
                    )
                    self._emit_run_finished_marker("ERROR", 1)
                    self.event_log.append(
                        "task.failed",
                        {
                            "task_id": self.task_id,
                            "exit_code": 1,
                            "runtime": "cloud",
                            "agent_id": self.agent_id,
                            "run_id": self.run_id,
                            "run_index": self._resolved_run_index(),
                            "terminal_phase": prev_phase,
                            "error_kind": "cloud_poll_timeout",
                        },
                    )
                    _safe_on_exit(self.on_exit, self.task_id, 1)
                    return

                # v0.8.6 T2.2.2: wake_event lets the SSEReader (or any other
                # signaler) interrupt the inter-poll sleep so we can confirm a
                # terminal phase faster than ``interval_s`` (validates I-6 drift
                # bound). When None, fall back to the v0.8.5 plain sleep so all
                # existing tests remain green. The signal is advisory — we still
                # call ``get_run`` on the next loop to obtain the authoritative
                # phase. A spurious wake during a non-sleep window is a no-op
                # because we ``clear()`` immediately after each wait.
                if self.wake_event is not None:
                    self.wake_event.wait(self.interval_s)
                    self.wake_event.clear()
                else:
                    time.sleep(self.interval_s)
        finally:
            try:
                self.client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "cloud client close failed for task=%s: %s",
                    self.task_id,
                    exc,
                )


def run_poll_loop(
    task_id: str,
    agent_id: str,
    run_id: str,
    *,
    client: CloudCursorClient,
    state_store: StateStore,
    event_log: EventLog,
    on_exit: Callable[[str, int], None] | None,
    interval_s: float = 2.0,
    max_polls: int = 1800,
    retry_max: int = 5,
    wake_event: threading.Event | None = None,
    run_index: int = 0,
) -> threading.Thread:
    """Create and start a daemon thread running :meth:`CloudPollLoop.run`.

    v0.8.6 T2.2.2: the optional ``wake_event`` parameter is forwarded to
    :class:`CloudPollLoop`. When set, calls to ``wake_event.set()`` from any
    other thread (typically :class:`SSEReader.terminal_hint` from
    ``adapters/cursor_cloud.py``) interrupt the inter-poll sleep so the poller
    can confirm a terminal phase within ≤200 ms instead of waiting up to
    ``interval_s``. ``wake_event=None`` (default) preserves v0.8.5 behaviour.

    v0.8.8 T2.1.1: ``run_index`` (default ``0``) is the 0-based ordinal of
    this run within its agent. Initial runs use ``0``; follow-ups created
    via :meth:`CloudCursorClient.create_followup_run` use ``1, 2, …``. The
    value is propagated into every ``cloud.run_status`` /
    ``cloud.run_started`` / ``cloud.run_finished`` / terminal ``task.*``
    envelope so consumers can group / sort by ``(run_index, seq)``.
    """
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id=agent_id,
        run_id=run_id,
        client=client,
        state_store=state_store,
        event_log=event_log,
        on_exit=on_exit,
        interval_s=interval_s,
        max_polls=max_polls,
        retry_max=retry_max,
        wake_event=wake_event,
        run_index=run_index,
    )
    thread = threading.Thread(
        target=loop.run,
        name=f"popolad-cloud-poll-{task_id}",
        daemon=True,
    )
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# v0.8.8 T2.2.2 — `409 agent_busy` async-queue (`quota-config.md` §4)
#
# Three building blocks land in this disjoint code block (separate from
# T2.1.1's CloudPollLoop above):
#
# 1. :class:`PendingDispatch` — a frozen-once data record describing one
#    queued follow-up dispatch.
# 2. :class:`PendingDispatchQueue` — a thread-safe in-memory FIFO keyed by
#    ``agent_id``. Persistence is deferred to the v0.9.0 ArkTower hook
#    (per ``quota-config.md`` §8 OQ-3); a daemon restart drops the queue,
#    matching the v0.8.5 behaviour for live SSE state.
# 3. :class:`PendingDispatchDrainer` — a daemon-thread polling loop that
#    polls each agent's latest run, dispatches the head when terminal,
#    and emits ``cloud.busy_queued`` / ``cloud.busy_dispatched`` /
#    ``cloud.busy_timeout`` events. A single-step ``tick()`` is exposed
#    so unit tests can drive the drainer deterministically without a
#    real thread.
#
# The drainer is intentionally callback-driven (``poll_phase`` /
# ``dispatch``) so tests can plug in pure-Python doubles instead of a
# real :class:`CloudCursorClient`. Production wiring composes the
# drainer with the Popolad's CloudCursorClient + EventLog resolver in
# :mod:`popolaloom.daemon.server` (T2.2.1's relay path is the first
# real consumer).
# ---------------------------------------------------------------------------


_TERMINAL_QUEUE_PHASES: frozenset[str] = frozenset(
    {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}
)
"""Phases at which the drainer pops the queue head and re-issues the dispatch.

Mirrors :data:`_TERMINAL_PHASES` — the queue path is just the inverse view
of "agent ready to accept a follow-up".
"""


def _utc_iso(dt: datetime) -> str:
    """ISO-8601 UTC with ms precision and ``Z`` suffix.

    Centralised here so :class:`PendingDispatch.deadline_iso` and the
    busy-event payloads share one rendering — ``popola status`` parses
    these strings to surface the WAITING line.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass
class PendingDispatch:
    """One queued follow-up dispatch awaiting an agent's terminal phase.

    Fields are populated at enqueue time by :meth:`PendingDispatchQueue.enqueue`
    and treated as read-only afterwards (the dataclass is intentionally
    NOT frozen so the queue can mutate ``queue_position`` post-hoc when an
    earlier entry is removed; callers MUST treat fields other than
    ``queue_position`` as immutable).

    Attributes:
        task_id: PopolaLoom-internal task id of the queued dispatch.
        agent_id: Cursor durable ``bc-...`` agent id whose run is busy.
        payload: The would-be POST body (e.g. ``{"prompt": {"text": ...}}``)
            that the drainer will re-issue via ``client.create_followup_run``
            once the existing run terminates. Stored verbatim so the queue
            survives an operator config change without losing fidelity.
        current_run_id: The non-terminal run id we are waiting on.
        notify_token: Random 16-hex token the CLI presents on attach to
            subscribe to dispatch-ready notifications. Generated at
            enqueue; opaque to the drainer (it just round-trips it via
            ``cloud.busy_queued``).
        enqueued_at_mono: Monotonic clock at enqueue (used for waited_ms).
        deadline_mono: Monotonic clock deadline; ``None`` ⇒ wait forever
            (operator opted into ``queue_max_wait_s = 0``).
        deadline_iso: ISO-8601 wall-clock deadline; ``None`` ⇒ no deadline.
            Surfaced verbatim in ``cloud.busy_queued.deadline_ts``.
        on_dispatch: Optional caller-supplied dispatch callback. When
            ``None`` (the default — used by tests / introspection),
            :meth:`PendingDispatchDrainer.dispatch` is the canonical
            re-issue path. When set, the drainer prefers the per-entry
            callback so a relay flow can override the dispatch verb
            (e.g. with a different idempotency key).
    """

    task_id: str
    agent_id: str
    payload: dict[str, Any]
    current_run_id: str
    notify_token: str = field(default_factory=lambda: secrets.token_hex(16))
    enqueued_at_mono: float = field(default_factory=time.monotonic)
    deadline_mono: float | None = None
    deadline_iso: str | None = None
    on_dispatch: Callable[[PendingDispatch], dict[str, Any]] | None = None
    queue_position: int = 0


class PendingDispatchQueue:
    """In-memory FIFO queue keyed by ``agent_id`` (thread-safe).

    The queue is intentionally a thin data structure: it owns no client,
    no thread, and no event-log wiring. The drainer
    (:class:`PendingDispatchDrainer`) supplies those side effects so a
    single queue can serve multiple agents through one shared drainer
    while tests drive it step-by-step via :meth:`PendingDispatchDrainer.tick`.

    Persistence is deferred to v0.9.0 (``quota-config.md`` §8 OQ-3); a
    daemon restart drops queued entries, matching v0.8.5's drop-on-restart
    behaviour for in-flight SSE state.

    Thread-safety: every public method acquires :attr:`_lock`. Callers
    that need to inspect multiple queues atomically must use
    :meth:`snapshot` (not chained per-agent calls).
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[PendingDispatch]] = {}
        self._lock = threading.RLock()

    # -- mutation -----------------------------------------------------

    def enqueue(self, dispatch: PendingDispatch) -> int:
        """Append ``dispatch`` to its agent's queue; return 1-based position.

        The :attr:`PendingDispatch.queue_position` field is stamped at
        enqueue and retained on the entry so :class:`PendingDispatchDrainer`
        can echo it into the ``cloud.busy_queued`` envelope without a
        second lock acquisition.
        """
        with self._lock:
            q = self._queues.setdefault(dispatch.agent_id, deque())
            q.append(dispatch)
            dispatch.queue_position = len(q)
            return dispatch.queue_position

    def pop_head(self, agent_id: str) -> PendingDispatch | None:
        """Remove and return the head dispatch for ``agent_id``; ``None`` if empty.

        Cleans up the per-agent deque when it empties so :meth:`agents`
        does not list ghosts.
        """
        with self._lock:
            q = self._queues.get(agent_id)
            if not q:
                return None
            head = q.popleft()
            if not q:
                del self._queues[agent_id]
            return head

    def remove_dispatch(self, dispatch: PendingDispatch) -> bool:
        """Remove ``dispatch`` from its queue (used for timeout / cancel).

        Returns ``True`` when the entry was found and removed; ``False``
        when it was already gone (idempotent for retry safety).
        """
        with self._lock:
            q = self._queues.get(dispatch.agent_id)
            if q is None:
                return False
            try:
                q.remove(dispatch)
            except ValueError:
                return False
            if not q:
                del self._queues[dispatch.agent_id]
            return True

    # -- inspection --------------------------------------------------

    def peek_head(self, agent_id: str) -> PendingDispatch | None:
        """Return the head dispatch without removing it (or ``None``)."""
        with self._lock:
            q = self._queues.get(agent_id)
            if not q:
                return None
            return q[0]

    def list_dispatches(self, agent_id: str) -> list[PendingDispatch]:
        """Snapshot copy of pending dispatches for one agent (FIFO order)."""
        with self._lock:
            q = self._queues.get(agent_id)
            if q is None:
                return []
            return list(q)

    def agents(self) -> list[str]:
        """Return all agent_ids with at least one pending dispatch."""
        with self._lock:
            return list(self._queues.keys())

    def snapshot(self) -> dict[str, list[PendingDispatch]]:
        """Return a dict copy ``{agent_id: [dispatches]}`` for atomic introspection."""
        with self._lock:
            return {k: list(v) for k, v in self._queues.items()}

    def __len__(self) -> int:
        """Total number of pending dispatches across all agents."""
        with self._lock:
            return sum(len(q) for q in self._queues.values())


# Type aliases for the drainer's pluggable side effects so tests can
# replace them with pure-Python doubles. Production wiring binds:
#
# - ``poll_phase`` to ``client.get_run(agent_id, run_id) -> body``
#   followed by extracting ``body["status"]``
# - ``event_log_resolver`` to ``Popolad.event_log(task_id)`` (one
#   :class:`EventLog` per task, owner of the NDJSON file)
# - ``dispatch`` to ``client.create_followup_run(agent_id,
#   payload["prompt"]["text"], model=payload.get("model"))`` plus
#   downstream poll-loop spawn (handled by the supervisor; see
#   ``cli/relay_cmd.py`` T2.2.1 callsite).
PhasePoller = Callable[[str, str], "str | None"]
"""Callable[(agent_id, run_id), phase_uppercase_or_None].

``None`` ⇒ a transient error (e.g. 5xx); the drainer logs a WARN and
retries on the next tick. ``""`` ⇒ unknown phase, treated as RUNNING.
"""

DispatchInvoker = Callable[[PendingDispatch], dict[str, Any]]
"""Callable[(dispatch,) -> response_dict].

Returns the parsed body of the re-issued POST. The drainer extracts
``body["id"]`` (or ``body["run"]["id"]`` as fallback) for the
``cloud.busy_dispatched.new_run_id`` field. Exceptions raised here
propagate to :meth:`PendingDispatchDrainer.tick`'s caller via
``logger.exception`` + the entry is dropped (No-Silent-Failures).
"""

EventLogResolver = Callable[[str], "EventLog | None"]
"""Callable[(task_id) -> EventLog or None].

The resolver returns ``None`` when the task is unknown (e.g. it has been
purged); the drainer then skips event emission for that entry but still
performs the dispatch / timeout side effect.
"""


@dataclass
class PendingDispatchDrainer:
    """Background drainer that polls latest runs and dispatches the queue.

    The drainer's :meth:`tick` is the unit of work: one tick walks every
    pending agent and either (a) re-issues the head if the existing run
    is terminal, (b) drops the head if its deadline has passed, or
    (c) leaves it alone otherwise. Tests call :meth:`tick` directly;
    production wires :meth:`start` which spawns a daemon thread looping
    on :meth:`tick` every :attr:`config.queue_poll_interval_s`.

    Attributes:
        queue: The :class:`PendingDispatchQueue` to drain.
        config: A :class:`BusyStrategyConfig`-shaped dataclass (any
            object with the four fields will do; the drainer accepts a
            ``Protocol``-style duck-type so tests can pass a
            :class:`SimpleNamespace`).
        poll_phase: Pluggable phase poller (see :data:`PhasePoller`).
        dispatch: Pluggable dispatch invoker (see :data:`DispatchInvoker`).
        event_log_resolver: Pluggable event-log resolver
            (see :data:`EventLogResolver`).
        sleep: Injectable sleep function; defaults to :func:`time.sleep`.
            Tests pass a no-op so :meth:`run` returns instantly.
        clock: Injectable monotonic clock; defaults to :func:`time.monotonic`.
            Tests pass a counter so deadlines are deterministic.
        wallclock: Injectable wall clock for ISO-8601 deadline rendering.
            Defaults to ``datetime.now(UTC)`` — only the ``cloud.busy_queued``
            envelope's ``deadline_ts`` and ``PendingDispatch.deadline_iso``
            consume it.
    """

    queue: PendingDispatchQueue
    config: BusyStrategyConfig
    poll_phase: PhasePoller
    dispatch: DispatchInvoker
    event_log_resolver: EventLogResolver
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    wallclock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    # -- public surface ----------------------------------------------

    def enqueue_dispatch(
        self,
        *,
        task_id: str,
        agent_id: str,
        payload: dict[str, Any],
        current_run_id: str,
        on_dispatch: DispatchInvoker | None = None,
    ) -> PendingDispatch:
        """Build a :class:`PendingDispatch`, enqueue it, and emit ``cloud.busy_queued``.

        The convenience wrapper composes the four side effects callers
        always pair: dataclass build → :meth:`PendingDispatchQueue.enqueue`
        → ISO-8601 deadline rendering → ``cloud.busy_queued`` event. It
        is the single point where the deadline is computed (so tests
        only need to control the clock, not the formula).
        """
        now_mono = self.clock()
        max_wait = int(self.config.queue_max_wait_s)
        if max_wait > 0:
            deadline_mono: float | None = now_mono + float(max_wait)
            deadline_dt = self.wallclock() + timedelta(seconds=max_wait)
            deadline_iso: str | None = _utc_iso(deadline_dt)
        else:
            deadline_mono = None
            deadline_iso = None

        dispatch = PendingDispatch(
            task_id=task_id,
            agent_id=agent_id,
            payload=payload,
            current_run_id=current_run_id,
            enqueued_at_mono=now_mono,
            deadline_mono=deadline_mono,
            deadline_iso=deadline_iso,
            on_dispatch=on_dispatch,
        )
        position = self.queue.enqueue(dispatch)
        event_log = self.event_log_resolver(task_id)
        if event_log is not None:
            try:
                record_busy_queued(
                    event_log,
                    task_id=task_id,
                    agent_id=agent_id,
                    current_run_id=current_run_id,
                    queue_position=position,
                    deadline_ts=deadline_iso,
                )
            except Exception:  # noqa: BLE001 — event emission must not block enqueue
                logger.exception(
                    "cloud.busy_queued emit failed for task=%s agent=%s",
                    task_id,
                    agent_id,
                )
        return dispatch

    def tick(self) -> None:
        """One drainer iteration: walk every pending agent.

        Public for unit tests; production code calls :meth:`run` (loops
        on tick + sleep). Per-agent failures are logged and isolated so
        one bad agent does not block the rest of the queue.
        """
        for agent_id in self.queue.agents():
            try:
                self._tick_agent(agent_id)
            except Exception:  # noqa: BLE001 — drainer must never die
                logger.exception(
                    "PendingDispatchDrainer tick failed for agent=%s",
                    agent_id,
                )

    def start(self) -> threading.Thread:
        """Spawn the background drainer daemon thread (idempotent).

        Returns the thread handle so callers may join it on shutdown.
        Subsequent calls are no-ops once the thread is alive.
        """
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run_forever,
            name="popolad-busy-drainer",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return thread

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the drainer to stop and join it (best-effort)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "PendingDispatchDrainer did not stop within %.1fs",
                    timeout,
                )

    # -- internals ---------------------------------------------------

    def _run_forever(self) -> None:
        """Loop: tick → sleep(queue_poll_interval_s) → repeat until stopped."""
        interval = float(self.config.queue_poll_interval_s)
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("PendingDispatchDrainer outer tick failed")
            # Use the stop-event as the sleep so shutdown is bounded by
            # lock contention rather than the poll cadence.
            if self._stop_event.wait(interval):
                return

    def _tick_agent(self, agent_id: str) -> None:
        head = self.queue.peek_head(agent_id)
        if head is None:
            return

        # Timeout check — fires before the phase poll so a hung server
        # cannot keep the queue alive past its deadline.
        if head.deadline_mono is not None and self.clock() >= head.deadline_mono:
            self._handle_timeout(head)
            return

        phase = self.poll_phase(agent_id, head.current_run_id)
        if phase is None:
            # Transient error path (e.g. 5xx from get_run); the WARN was
            # already logged by the phase poller. Leave the head in
            # place; the next tick retries.
            return
        phase_upper = phase.strip().upper()
        if phase_upper not in _TERMINAL_QUEUE_PHASES:
            return

        # Existing run terminated — pop and re-issue.
        self._handle_dispatch(head)

    def _handle_dispatch(self, head: PendingDispatch) -> None:
        # Pop atomically before re-issue so a same-tick re-enqueue (rare
        # but possible — a relay that loops) cannot be served twice.
        popped = self.queue.pop_head(head.agent_id)
        if popped is None or popped is not head:
            # Another tick raced us; not an error — just bail.
            return

        invoker = head.on_dispatch if head.on_dispatch is not None else self.dispatch
        try:
            response = invoker(head)
        except Exception:  # noqa: BLE001
            logger.exception(
                "PendingDispatchDrainer dispatch failed task=%s agent=%s",
                head.task_id,
                head.agent_id,
            )
            return

        new_run_id = self._extract_new_run_id(response)
        waited_ms = max(0, int((self.clock() - head.enqueued_at_mono) * 1000))
        event_log = self.event_log_resolver(head.task_id)
        if event_log is not None and self.config.notify_on_dispatch:
            try:
                record_busy_dispatched(
                    event_log,
                    task_id=head.task_id,
                    agent_id=head.agent_id,
                    prev_run_id=head.current_run_id,
                    new_run_id=new_run_id,
                    waited_ms=waited_ms,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "cloud.busy_dispatched emit failed task=%s",
                    head.task_id,
                )

    def _handle_timeout(self, head: PendingDispatch) -> None:
        popped = self.queue.pop_head(head.agent_id)
        if popped is None or popped is not head:
            return
        waited_ms = max(0, int((self.clock() - head.enqueued_at_mono) * 1000))
        event_log = self.event_log_resolver(head.task_id)
        if event_log is not None:
            try:
                record_busy_timeout(
                    event_log,
                    task_id=head.task_id,
                    agent_id=head.agent_id,
                    waited_ms=waited_ms,
                    current_run_id_at_timeout=head.current_run_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "cloud.busy_timeout emit failed task=%s",
                    head.task_id,
                )

    @staticmethod
    def _extract_new_run_id(response: dict[str, Any] | None) -> str:
        """Best-effort extraction of the newly-issued run id from a dispatch response.

        ``POST /v1/agents/{id}/runs`` returns ``{"id": "run-...", ...}``
        (per ``endpoints.md`` retrieved 2026-05-08); the legacy
        ``POST /v1/agents`` shape used ``{"run": {"id": ...}}``. We try
        both so both paths surface a meaningful ``new_run_id`` in
        ``cloud.busy_dispatched`` — falling back to ``""`` rather than
        ``None`` because the wire schema declares the field as a string.
        """
        if not isinstance(response, dict):
            return ""
        candidate = response.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
        run_obj = response.get("run")
        if isinstance(run_obj, dict):
            run_id = run_obj.get("id")
            if isinstance(run_id, str) and run_id:
                return run_id
        return ""


def _phase_from_run_body(body: dict[str, Any] | None) -> str | None:
    """Helper to extract an upper-cased phase from a ``GET /runs`` body.

    Production wiring uses this to build a :data:`PhasePoller` from a
    :class:`CloudCursorClient`:

    .. code-block:: python

        def poll_phase(agent_id: str, run_id: str) -> str | None:
            try:
                body = client.get_run(agent_id, run_id)
            except CursorCloudError as exc:
                logger.warning("get_run failed agent=%s run=%s: %s",
                               agent_id, run_id, exc)
                return None
            return _phase_from_run_body(body)

    Tests prefer to skip this helper and pass a hand-rolled
    :data:`PhasePoller` so they can hit the ``None`` (transient error)
    and ``""`` (unknown phase) paths without an httpx mock.
    """
    if not isinstance(body, dict):
        return ""
    raw = body.get("status")
    if raw is None:
        return ""
    return str(raw).strip().upper()


# Re-export the new public surface (Mandatory Verification: tests import
# these by name, so the module's public contract stays grep-able).
__all__ = [
    "CloudPollLoop",
    "PendingDispatch",
    "PendingDispatchDrainer",
    "PendingDispatchQueue",
    "_TERMINAL_QUEUE_PHASES",
    "_phase_from_run_body",
    "_utc_iso",
    "run_poll_loop",
]
