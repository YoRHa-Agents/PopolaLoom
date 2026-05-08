"""Cloud-runtime poller — translates Cursor Cloud Agent run lifecycle into
PopolaLoom EventLog events + TaskState transitions.

Used by Supervisor._spawn_cloud as the background liveness driver for tasks
created via /v1/agents. Replaces the local subprocess.wait() pattern with
a polling loop because cloud agents have no OS-level handle on our side.

v0.8.5 (Stage 2 of the Cloud Agent integration). See
.local/research/v0.8.5_cloud_agent/research.md §7 (phased rollout).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from popolaloom.adapters.cursor_cloud import CloudCursorClient, CursorCloudError
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskState

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

    def _emit_run_status(self, phase: str, prev_phase: str | None) -> None:
        # I-1 sole-writer: only this module writes cloud_phase via StateStore.update
        # (see state-source-of-truth.md §1.2 rule 1; CI guard in tests/conftest.py
        # via test_invariant_i1_sole_writer_of_cloud_phase enforces this at PR time).
        self.event_log.append(
            "cloud.run_status",
            {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "run_id": self.run_id,
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
                    self.event_log.append(
                        "task.failed",
                        {
                            "task_id": self.task_id,
                            "exit_code": 1,
                            "runtime": "cloud",
                            "agent_id": self.agent_id,
                            "run_id": self.run_id,
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
                    if phase == "FINISHED":
                        self.event_log.append(
                            "task.completed",
                            {
                                "task_id": self.task_id,
                                "exit_code": 0,
                                "runtime": "cloud",
                                "agent_id": self.agent_id,
                                "run_id": self.run_id,
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
                    self.event_log.append(
                        "task.failed",
                        {
                            "task_id": self.task_id,
                            "exit_code": 1,
                            "runtime": "cloud",
                            "agent_id": self.agent_id,
                            "run_id": self.run_id,
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
) -> threading.Thread:
    """Create and start a daemon thread running :meth:`CloudPollLoop.run`.

    v0.8.6 T2.2.2: the optional ``wake_event`` parameter is forwarded to
    :class:`CloudPollLoop`. When set, calls to ``wake_event.set()`` from any
    other thread (typically :class:`SSEReader.terminal_hint` from
    ``adapters/cursor_cloud.py``) interrupt the inter-poll sleep so the poller
    can confirm a terminal phase within ≤200 ms instead of waiting up to
    ``interval_s``. ``wake_event=None`` (default) preserves v0.8.5 behaviour.
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
    )
    thread = threading.Thread(
        target=loop.run,
        name=f"popolad-cloud-poll-{task_id}",
        daemon=True,
    )
    thread.start()
    return thread
