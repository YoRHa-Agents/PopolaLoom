"""Lark listener supervisor — v0.3.0 Stage F4.D.

Per spec §3.4 + roadmap §12.8.2 + RV3-5 mitigation: when the
``lark-cli event consume`` subprocess dies (network blip, lark-cli
crash, OOM), we want to **restart it ≤ 3 times** before escalating
to a HITL ``destructive_op`` prompt that surfaces the dead listener
to the operator.

The supervisor:

- Tracks restart attempts per "lifetime" (resets to 0 after the
  listener stays alive ``RESET_THRESHOLD_S`` seconds).
- Calls ``listener.start()`` and awaits ``listener.is_alive`` going
  False; on death, increments counter + restarts (with exponential
  back-off ``2s/4s/8s``) until the counter exceeds ``MAX_RESTARTS``.
- Emits structured events via the optional ``on_event`` callback so
  callers can persist them to NDJSON / nines.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from popolaloom.lark.listener import LarkListener

logger = logging.getLogger(__name__)


MAX_RESTARTS: int = 3
"""Per roadmap §12.8.2 + RV3-5: 3 consecutive deaths → escalate to human."""

RESTART_DELAYS_S: tuple[float, ...] = (2.0, 4.0, 8.0)
"""Exponential back-off between restart attempts."""

RESET_THRESHOLD_S: float = 60.0
"""If the listener stays alive ≥ 60 s, the failure counter resets."""

POLL_INTERVAL_S: float = 1.0
"""How often the supervisor polls ``listener.is_alive`` (s)."""


@dataclass
class SupervisorState:
    """Public-facing supervisor stats (used by tests + nines lark_health)."""

    started_at: datetime | None = None
    restart_count: int = 0
    total_restarts: int = 0
    last_restart_at: datetime | None = None
    escalated: bool = False
    healthy_uptime_s: float = 0.0
    events: list[dict[str, str]] = field(default_factory=list)


SupervisorEventCallback = Callable[[dict[str, str]], Awaitable[None]]


class LarkSupervisor:
    """Watchdog for a :class:`LarkListener`.

    Args:
        listener: the listener to supervise (already constructed but not
            yet ``start()``-ed; the supervisor will start it).
        on_event: optional async callback for lifecycle events
            (``listener.started``, ``listener.died``,
            ``listener.restarted``, ``listener.escalated``).
        max_restarts: override :data:`MAX_RESTARTS` (used by tests).
        restart_delays_s: override :data:`RESTART_DELAYS_S` (used by tests).
    """

    def __init__(
        self,
        listener: LarkListener,
        *,
        on_event: SupervisorEventCallback | None = None,
        max_restarts: int = MAX_RESTARTS,
        restart_delays_s: tuple[float, ...] = RESTART_DELAYS_S,
        reset_threshold_s: float = RESET_THRESHOLD_S,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self.listener = listener
        self.on_event = on_event
        self.max_restarts = max_restarts
        self.restart_delays_s = restart_delays_s
        self.reset_threshold_s = reset_threshold_s
        self.poll_interval_s = poll_interval_s
        self.state = SupervisorState()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        """Start the listener + spawn the watchdog task."""
        if self._task is not None:
            raise RuntimeError("LarkSupervisor already running")
        await self.listener.start()
        self.state.started_at = datetime.now(UTC)
        await self._emit({"event": "listener.started"})
        self._task = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        """Cancel watchdog and stop the listener (cooperative shutdown)."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.listener.stop()

    async def _watch(self) -> None:
        """Loop: poll listener.is_alive; restart on death up to max."""
        last_alive_at = time.monotonic()
        while not self._stopped:
            await asyncio.sleep(self.poll_interval_s)
            if self._stopped:
                return
            if self.listener.is_alive:
                # Track uptime; reset counter if alive ≥ RESET_THRESHOLD_S.
                now = time.monotonic()
                self.state.healthy_uptime_s = now - last_alive_at
                if (
                    self.state.restart_count > 0
                    and self.state.healthy_uptime_s >= self.reset_threshold_s
                ):
                    logger.info(
                        "LarkSupervisor: listener alive %.1fs ≥ %.1fs; resetting counter",
                        self.state.healthy_uptime_s, self.reset_threshold_s,
                    )
                    self.state.restart_count = 0
                continue

            # Listener died.
            await self._emit({"event": "listener.died"})
            if self.state.restart_count >= self.max_restarts:
                self.state.escalated = True
                await self._emit({"event": "listener.escalated"})
                logger.error(
                    "LarkSupervisor: listener died %d times in a row (max=%d); "
                    "escalating to HITL",
                    self.state.restart_count, self.max_restarts,
                )
                return
            delay_idx = min(self.state.restart_count, len(self.restart_delays_s) - 1)
            delay = self.restart_delays_s[delay_idx]
            self.state.restart_count += 1
            self.state.total_restarts += 1
            self.state.last_restart_at = datetime.now(UTC)
            logger.warning(
                "LarkSupervisor: restart #%d after %.1fs", self.state.restart_count, delay,
            )
            await asyncio.sleep(delay)
            try:
                await self.listener.start()
            except Exception:
                logger.exception("LarkSupervisor: listener.start() raised")
                continue
            await self._emit({"event": "listener.restarted",
                              "attempt": str(self.state.restart_count)})
            last_alive_at = time.monotonic()

    async def _emit(self, event: dict[str, str]) -> None:
        """Append to local history + dispatch the optional callback."""
        event = dict(event)
        event["at"] = datetime.now(UTC).isoformat()
        self.state.events.append(event)
        if len(self.state.events) > 200:
            self.state.events = self.state.events[-100:]
        if self.on_event is not None:
            try:
                await self.on_event(event)
            except Exception:
                logger.exception("LarkSupervisor: on_event raised")


__all__ = [
    "LarkSupervisor",
    "MAX_RESTARTS",
    "POLL_INTERVAL_S",
    "RESET_THRESHOLD_S",
    "RESTART_DELAYS_S",
    "SupervisorEventCallback",
    "SupervisorState",
]
