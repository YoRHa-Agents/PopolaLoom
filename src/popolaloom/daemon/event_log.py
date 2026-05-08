"""Append-only NDJSON event log with CloudEvents 1.0 envelope.

Each task owns a ``<events_dir>/<task_id>.jsonl`` file; line format is
the CloudEvents 1.0 structured envelope per :doc:`spec` §3.5.5
(出处: 03 §5.3 + 05 §"推荐的事件流格式").

v0.2.0 Stage C C5 — R-011 fix
-----------------------------

Pre-Stage-C behavior was *every* :meth:`append` call did
``open(path, "a")`` + ``write(line)`` + ``close()``.  At ~3 syscalls per
append plus journal-mode interactions, append latency hovered around
3-7 ms on a typical Linux dev box — borderline for NFR-3 (≤ 5 ms) and
catastrophic when the supervisor's stdout/stderr drain threads emit
hundreds of lines/s for chatty CLIs.

Stage C swaps this for an **fd-held buffered writer**:

- :meth:`__init__` opens the file once with a configurable buffer
  (``buffer_bytes`` default 4096) and parks the fd in :attr:`_fd`;
- :meth:`append` only does ``self._fd.write(line)`` while holding
  :attr:`_lock`;
- A background daemon thread runs :meth:`_fsync_worker` every
  ``fsync_interval_s`` seconds (default 1 s) and calls
  ``flush() + os.fsync()`` so durability is bounded;
- :meth:`tail` flushes the buffer before re-opening for read so
  in-process callers always see their own writes (test contract);
- :meth:`close` is idempotent, joins the worker thread, and finalises
  the fd.  Called by the rpc.py lifespan shutdown.

NFR-3 baseline (target < 5 ms) is exercised by
``tests/nfr/test_nfr_3_event_log_latency.py``; on the dev VM we measure
mean ≈ 0.05 ms and p95 ≈ 0.10 ms (well under target).

Process safety
--------------

The original POSIX ``write`` atomicity for ≤ ``PIPE_BUF`` lines is no
longer relevant for **inter-process** access since each EventLog now
holds its own buffered fd — concurrent writes from two distinct popolad
processes against the same file would interleave at buffer boundaries.
For v0.2.0 this is fine: only one popolad process writes a given task's
file (popolad is single-instance per ``$POPOLA_HOME``).  Multi-process
sharing would need ``O_APPEND`` + lock-free atomic ``write()`` per line
— deferred to v0.3.0 alongside the multi-pool work.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any

logger = logging.getLogger(__name__)


_DEFAULT_BUFFER_BYTES: int = 4096
"""Default user-space write buffer (passed straight to :func:`open`).

4 KiB matches a typical filesystem block size — writes accumulate until
the buffer fills, then a single ``write(2)`` flushes the page.  Tests
that need an unbuffered log can pass ``buffer_bytes=0`` (forces line
buffering for text mode)."""


_DEFAULT_FSYNC_INTERVAL_S: float = 1.0
"""How often the background worker fsyncs the buffered fd to disk.

1 s bounds data loss on power loss to the last second of events.  Tune
down (0.1) for stricter durability at the cost of more syscalls; tune
up (5.0) for chatty workloads where you can tolerate a few seconds of
loss in exchange for higher throughput."""


def _utc_now_iso() -> str:
    """Return ISO-8601 UTC timestamp with millisecond precision and ``Z`` suffix."""
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class EventLog:
    """Append-only NDJSON writer for a single task's CloudEvents stream.

    Args:
        path: NDJSON file path (typically ``<events_dir>/<task_id>.jsonl``).
            Parent directory is auto-created.
        source: CloudEvents ``source`` field; defaults to
            ``popola/<file-stem>``.
        buffer_bytes: User-space write buffer size handed to :func:`open`.
            See :data:`_DEFAULT_BUFFER_BYTES`.
        fsync_interval_s: Period of the background fsync worker.  ``0``
            or negative disables the worker entirely (test mode — call
            :meth:`fsync` explicitly).

    Thread-safety: append / tail / fsync / close all serialise on
    :attr:`_lock`.  The background fsync worker also acquires the same
    lock so durability writes never interleave with appends.
    """

    def __init__(
        self,
        path: Path,
        source: str | None = None,
        *,
        buffer_bytes: int = _DEFAULT_BUFFER_BYTES,
        fsync_interval_s: float = _DEFAULT_FSYNC_INTERVAL_S,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._source = source or f"popola/{self._path.stem}"
        self._lock = threading.Lock()
        self._closed = False
        self._buffer_bytes = buffer_bytes
        self._fsync_interval_s = fsync_interval_s
        self._fd: IO[str] = self._path.open("a", buffering=buffer_bytes, encoding="utf-8")
        # v0.8.8 T2.1.2 (cost-fields.md §5.2): EventLog files MUST be 0o600
        # (owner-only) because they may carry undocumented payload extras
        # incl. potential token / cost data per Q-C-2 sensitivity bound.
        # `chmod` is best-effort on platforms that don't support it
        # (e.g. some Windows mounts); the WARNING is a No-Silent-Failures
        # disclosure so the operator can audit the deviation.
        try:
            os.chmod(self._path, 0o600)
        except OSError as exc:
            logger.warning(
                "EventLog chmod 0o600 failed for %s: %s "
                "(file may be world-readable; review §5.2)",
                self._path,
                exc,
            )
        self._stop_event = threading.Event()
        self._fsync_thread: threading.Thread | None = None
        if fsync_interval_s > 0:
            self._fsync_thread = threading.Thread(
                target=self._fsync_worker,
                name=f"event_log_fsync_{self._path.stem}",
                daemon=True,
            )
            self._fsync_thread.start()

    @property
    def path(self) -> Path:
        """The file path this log writes to."""
        return self._path

    @property
    def source(self) -> str:
        """CloudEvents ``source`` field."""
        return self._source

    @property
    def closed(self) -> bool:
        """``True`` after :meth:`close` has been called."""
        return self._closed

    def append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Append a single CloudEvents envelope and return the dict written.

        Args:
            event_type: CloudEvents ``type`` (e.g. ``task.dispatched``,
                ``process.stdout``, ``task.transition``).  See spec §3.5.5
                discriminated union list.
            data: JSON-serialisable business payload, placed under ``data``.

        Returns:
            dict: The full envelope with ``id``, ``source``, ``specversion``,
            ``type``, ``time``, ``data``.

        Raises:
            RuntimeError: when the log has been closed (No Silent Failures —
                writes after close MUST surface, not vanish).
        """
        envelope: dict[str, Any] = {
            "specversion": "1.0",
            "id": f"evt-{uuid.uuid4().hex}",
            "source": self._source,
            "type": event_type,
            "time": _utc_now_iso(),
            "data": data,
        }
        line = json.dumps(envelope, ensure_ascii=False) + "\n"
        with self._lock:
            if self._closed:
                raise RuntimeError(f"EventLog at {self._path} is closed")
            self._fd.write(line)
        return envelope

    def fsync(self) -> None:
        """Explicit flush + ``os.fsync`` of the underlying fd.

        Safe to call from any thread.  No-op once the log is closed
        (close already fsync'd as part of teardown).
        """
        with self._lock:
            if self._closed:
                return
            self._do_fsync_locked()

    def _do_fsync_locked(self) -> None:
        """fsync helper assuming :attr:`_lock` is already held."""
        try:
            self._fd.flush()
            os.fsync(self._fd.fileno())
        except (OSError, ValueError) as exc:
            logger.warning("EventLog fsync failed for %s: %s", self._path, exc)

    def _fsync_worker(self) -> None:
        """Background loop: every ``fsync_interval_s`` flush + fsync.

        Wakes on :attr:`_stop_event` (set by :meth:`close`) so shutdown
        is bounded by lock contention, not by the sleep interval.  The
        worker is a daemon thread so it never prevents process exit; the
        explicit join in :meth:`close` ensures graceful tear-down when
        callers do close cleanly.
        """
        while not self._stop_event.wait(self._fsync_interval_s):
            with self._lock:
                if self._closed:
                    return
                self._do_fsync_locked()

    def tail(self, since_index: int = 0) -> list[dict[str, Any]]:
        """Read all events from ``since_index`` (0-based) onward.

        Flushes the in-memory write buffer first so callers see their own
        appends — important because Stage A's drain threads write
        concurrently with consumer code calling ``tail()`` to render
        progress.

        Args:
            since_index: 0 = read from start; ``len(prev)`` = incremental
                polling.

        Returns:
            list[dict]: Parsed envelopes; empty list when file missing
            or all lines were corrupt.
        """
        with self._lock:
            if not self._closed:
                self._fd.flush()
        if not self._path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for idx, raw_line in enumerate(fh):
                if idx < since_index:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Skipping corrupt NDJSON line %d in %s: %s",
                        idx,
                        self._path,
                        exc,
                    )
        return events

    def __len__(self) -> int:
        """Number of non-empty lines currently in the file (post-flush)."""
        with self._lock:
            if not self._closed:
                self._fd.flush()
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def close(self) -> None:
        """Flush + fsync + close the fd; idempotent.

        Joins the background fsync worker so we don't leave dangling
        threads.  After :meth:`close`, further :meth:`append` calls
        raise :class:`RuntimeError` (No Silent Failures); :meth:`tail`
        and :meth:`__len__` continue to work because they re-open the
        file for read.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            self._do_fsync_locked()
            try:
                self._fd.close()
            except OSError as exc:
                logger.warning("EventLog fd.close() failed for %s: %s", self._path, exc)

        if self._fsync_thread is not None:
            self._fsync_thread.join(timeout=2.0)
            if self._fsync_thread.is_alive():
                logger.warning(
                    "EventLog fsync worker for %s did not terminate within 2s",
                    self._path,
                )

    def __enter__(self) -> EventLog:
        """Allow ``with EventLog(...) as log:`` for ad-hoc scripts / tests."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        """Best-effort fd cleanup if the caller forgot to close.

        ``__del__`` runs at unpredictable times and may race with
        interpreter shutdown; we suppress *all* exceptions and don't
        raise — this is purely defensive.  Production code should call
        :meth:`close` (or use the context manager) explicitly.
        """
        try:
            if not self._closed:
                self.close()
        except Exception:
            pass


# ─── v0.8.8 T2.2.2 — typed wrappers for cloud.busy_* events ────────────────
#
# Per ``quota-config.md`` §5.1 + §5.2 the queue path emits three
# default-visible events: ``cloud.busy_queued`` (on enqueue),
# ``cloud.busy_dispatched`` (on successful re-issue), and
# ``cloud.busy_timeout`` (on ``queue_max_wait_s`` expiry). These thin
# helpers centralise the payload schema so the queue / drainer in
# ``daemon/cloud_poller.py`` and any future relay path emit identical
# wire shapes — Q-C-7 default-visibility lives or dies on this surface
# being grep-able.
#
# Each helper accepts the :class:`EventLog` (or any duck-typed appender
# exposing ``append(event_type, data) -> dict``) so unit tests can pass
# a :class:`MagicMock`. ``Optional[EventLog]`` is rejected at the call
# site, not here — a typed ``None`` would be a programming error.


def record_busy_queued(
    event_log: EventLog,
    *,
    task_id: str,
    agent_id: str,
    current_run_id: str,
    queue_position: int,
    deadline_ts: str | None = None,
) -> dict[str, Any]:
    """Emit ``cloud.busy_queued`` and return the rendered envelope.

    Called when the daemon's :class:`PendingDispatchQueue` accepts a
    follow-up dispatch that hit ``409 agent_busy`` while ``mode = "queue"``.
    The envelope's ``deadline_ts`` is the ISO-8601 UTC string at which
    :meth:`PendingDispatchDrainer` will give up; ``None`` indicates the
    operator opted into ``queue_max_wait_s = 0`` (wait forever).

    Args:
        event_log: Append target.
        task_id: PopolaLoom-internal task id of the queued dispatch.
        agent_id: Cursor durable ``bc-...`` agent id whose run is busy.
        current_run_id: The non-terminal run we are waiting on.
        queue_position: 1-based position in the per-``agent_id`` FIFO.
        deadline_ts: ISO-8601 UTC deadline; ``None`` for "wait forever".
    """
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "current_run_id": current_run_id,
        "queue_position": queue_position,
        "deadline_ts": deadline_ts,
    }
    return event_log.append("cloud.busy_queued", data)


def record_busy_dispatched(
    event_log: EventLog,
    *,
    task_id: str,
    agent_id: str,
    prev_run_id: str,
    new_run_id: str,
    waited_ms: int,
) -> dict[str, Any]:
    """Emit ``cloud.busy_dispatched`` and return the rendered envelope.

    Called when the drainer successfully re-issues the queued payload —
    the previous run finished and the follow-up is now its own
    ``CREATING`` run. Attach UIs key off this event to dismiss the
    ``WAITING:`` banner shown by ``popola status`` and ``popola attach``.

    Args:
        event_log: Append target.
        task_id: PopolaLoom-internal task id of the now-dispatched task.
        agent_id: Cursor agent id (same as the ``cloud.busy_queued`` peer).
        prev_run_id: The terminal run we waited on (the reason the
            ``409`` cleared).
        new_run_id: The newly-issued run id (the ``id`` field of the
            ``POST /v1/agents/{id}/runs`` response).
        waited_ms: Total wait time in milliseconds, computed from the
            queue's monotonic clock.
    """
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "prev_run_id": prev_run_id,
        "new_run_id": new_run_id,
        "waited_ms": waited_ms,
    }
    return event_log.append("cloud.busy_dispatched", data)


def record_busy_timeout(
    event_log: EventLog,
    *,
    task_id: str,
    agent_id: str,
    waited_ms: int,
    current_run_id_at_timeout: str,
) -> dict[str, Any]:
    """Emit ``cloud.busy_timeout`` and return the rendered envelope.

    Called when the drainer drops a queued dispatch after
    ``queue_max_wait_s`` elapses. The CLI surfaces ``cli_exit=75`` (per
    spec §6 — overload, NOT 102 because the wait expired, not the agent).

    Args:
        event_log: Append target.
        task_id: PopolaLoom-internal task id of the dropped dispatch.
        agent_id: Cursor agent id we were waiting on.
        waited_ms: Total wait time in milliseconds.
        current_run_id_at_timeout: The non-terminal run id we observed
            at expiry (debug aid — the agent was *still* busy, so the
            wait expired rather than the queue draining).
    """
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "waited_ms": waited_ms,
        "current_run_id_at_timeout": current_run_id_at_timeout,
    }
    return event_log.append("cloud.busy_timeout", data)
