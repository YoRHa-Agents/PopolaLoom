"""ArkTower → popolad event bus bridge — v0.2.0 Stage C C2.

Subscribes to ArkTower's in-process :class:`EventBus` for the
``TASK_TRANSITION_EVENT`` topic and translates each
:class:`arktower.core.models.TaskEvent` into a single-line ``task.transition``
NDJSON record on the per-popola-task event log.

Why a bridge instead of letting popolad call ArkTower directly?

- :class:`Popolad` already owns the per-task :class:`EventLog` instances;
  funnelling ArkTower transitions through the same NDJSON file keeps
  consumers (CLI ``attach``, MCP ``popola_attach_stream``) on one tail
  source.  Closes spec §3.5.5 双轨 NDJSON contract.
- The bridge is the natural integration seam for v0.3.0 Lark
  notifications (Stage F4): subscribe a second handler against the same
  ``TASK_TRANSITION_EVENT`` topic.
- Keeps :mod:`popolaloom.daemon.server` from importing ArkTower's
  ``EventBus`` directly (DIP — only this thin module depends on
  ``arktower.core.event_bus`` + ``arktower.core.task_service``).

Translation note (TaskEvent → NDJSON)
-------------------------------------

ArkTower's :class:`TaskEvent` ships ``from_status`` / ``to_status``
(``TaskStatus`` enum) + ``timestamp`` (datetime) + ``trigger`` + ``actor``.
We serialise enum values as their ``.value`` (lower-case strings matching
spec §3.5.3 status enum) and timestamps as ISO-8601 with ``Z`` suffix
to stay consistent with :func:`popolaloom.daemon.event_log._utc_now_iso`.

ArkTower task_id → popola task_id resolution
--------------------------------------------

The :class:`TaskEvent.task_id` is ArkTower's id, **not** popolad's
internal ``cli-<12hex>`` id, so the bridge cannot directly index
``Popolad._event_logs``.  The constructor takes a
``get_event_log: Callable[[str], EventLog | None]`` callback; popolad
implements it by walking :meth:`StateStore.list_all` looking for
``handle.arktower_task_id == ark_task_id``.  Walks are O(N) over the
in-flight task count; for v0.2.0 (≤ ~10 tasks) this is negligible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from popolaloom._vendored.arktower.core.event_bus import EventBus
from popolaloom._vendored.arktower.core.task_service import (
    TASK_TRANSITION_EVENT,
)

if TYPE_CHECKING:
    from popolaloom._vendored.arktower.core.models import TaskEvent

    from popolaloom.daemon.event_log import EventLog


logger = logging.getLogger(__name__)


GetEventLogFn = Callable[[str], "EventLog | None"]
"""Signature: ``ark_task_id -> EventLog | None``.

Returning ``None`` means "no popolad task tracks this ArkTower id"
(harmless — the bridge silently drops the event with a debug log; happens
e.g. when ArkTower's CLI creates a task directly without popolad).
"""


def _ts_to_iso(ts: datetime) -> str:
    """Render a (possibly naive) datetime as ISO-8601 ms with ``Z`` suffix.

    ArkTower's :class:`TaskEvent.timestamp` defaults to ``datetime.utcnow()``
    (naive); we coerce to aware UTC before formatting so downstream
    consumers can parse without timezone ambiguity.  Format mirrors
    :func:`popolaloom.daemon.event_log._utc_now_iso` for cross-event
    comparability.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PopolaEventBusBridge:
    """Subscribe ArkTower TASK_TRANSITION_EVENT and emit ``task.transition`` NDJSON.

    Usage::

        bridge = PopolaEventBusBridge(persistence.event_bus, popolad.event_log_for_arktower_id)
        bridge.subscribe()
        ...
        bridge.unsubscribe()  # daemon shutdown

    Thread-safety: ArkTower's :class:`EventBus.publish` calls handlers
    sequentially in the publishing thread / coroutine (sync fan-out;
    async handlers are awaited).  Our :meth:`_on_transition` only writes
    to the :class:`EventLog`, which is itself locked, so no extra
    synchronisation is needed here.
    """

    def __init__(
        self,
        event_bus: EventBus,
        get_event_log: GetEventLogFn,
    ) -> None:
        """Bind to *event_bus*; resolve event logs via *get_event_log*.

        Args:
            event_bus: ArkTower :class:`EventBus`, typically owned by a
                :class:`TaskPersistence` instance.
            get_event_log: Callback that maps ArkTower task id → popola
                :class:`EventLog`; ``None`` return → bridge drops the
                transition silently (with debug log).
        """
        self._event_bus = event_bus
        self._get_event_log = get_event_log
        self._subscribed: bool = False

    @property
    def subscribed(self) -> bool:
        """``True`` when :meth:`subscribe` has been called and the bridge
        is currently registered with the ArkTower event bus."""
        return self._subscribed

    def subscribe(self) -> None:
        """Register :meth:`_on_transition` against ``TASK_TRANSITION_EVENT``.

        Idempotent: calling twice is a no-op (a second :meth:`unsubscribe`
        would otherwise leak the second subscription).  ArkTower's
        :class:`EventBus.subscribe` already de-dupes the same handler
        instance, but we double-check defensively so our :attr:`subscribed`
        flag stays in sync.
        """
        if self._subscribed:
            return
        self._event_bus.subscribe(TASK_TRANSITION_EVENT, self._on_transition)
        self._subscribed = True
        logger.debug("PopolaEventBusBridge subscribed to %s", TASK_TRANSITION_EVENT)

    def unsubscribe(self) -> None:
        """Detach the handler from the ArkTower event bus.

        Idempotent: calling on an already-unsubscribed bridge is a no-op.
        Called from :meth:`Popolad.shutdown_persistence_bridge` (which
        rpc.py's lifespan finally-block invokes) so the daemon doesn't
        leak handler refs across reload-style restarts in long-lived
        Python processes.
        """
        if not self._subscribed:
            return
        self._event_bus.unsubscribe(TASK_TRANSITION_EVENT, self._on_transition)
        self._subscribed = False
        logger.debug("PopolaEventBusBridge unsubscribed from %s", TASK_TRANSITION_EVENT)

    def _on_transition(self, event: TaskEvent) -> None:
        """ArkTower → NDJSON translation for a single :class:`TaskEvent`.

        Steps:

        1. Resolve the popola event log via :attr:`_get_event_log`;
           ``None`` → drop with debug log (No Silent Failures: we *do*
           log, just at debug level — this is a normal case for tasks
           created outside popolad).
        2. Build a ``task.transition`` envelope with ``trigger``,
           ``from_status`` / ``to_status``, ``actor``, ``notes``,
           ``timestamp`` fields.  Stable schema for downstream consumers
           (CLI / MCP / Lark bridge planned in v0.3.0).
        3. Append to the NDJSON file via the standard
           :meth:`EventLog.append`.

        Exceptions inside this handler are caught + logged; ArkTower's
        :class:`EventBus` already swallows handler exceptions (see
        ``arktower.core.event_bus.EventBus.publish``), but we re-log with
        more context so debugging is easier.  We do NOT re-raise — the
        publish loop must continue to other subscribers (e.g. a future
        Lark notifier).
        """
        try:
            log = self._get_event_log(event.task_id)
            if log is None:
                logger.debug(
                    "task.transition for unknown ArkTower task_id=%s; dropping",
                    event.task_id,
                )
                return
            log.append(
                "task.transition",
                {
                    "task_id": event.task_id,
                    "event_id": event.event_id,
                    "trigger": event.trigger.value,
                    "from_status": event.from_status.value,
                    "to_status": event.to_status.value,
                    "actor": event.actor,
                    "notes": event.notes,
                    "timestamp": _ts_to_iso(event.timestamp),
                },
            )
        except Exception:
            logger.exception(
                "PopolaEventBusBridge handler failed for ark_task_id=%s; "
                "event log not updated",
                getattr(event, "task_id", "<unknown>"),
            )
