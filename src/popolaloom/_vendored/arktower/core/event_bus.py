"""In-process event bus supporting both sync and async handlers.

Vendored from ArkTower @ commit 467a087 (arktower/core/event_bus.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[..., Any]
AsyncHandler = Callable[..., Coroutine[Any, Any, Any]]


class EventBus:
    """Pub/sub event bus keyed by event-type strings.

    Handlers may be plain functions or ``async`` coroutines; the bus
    dispatches each appropriately.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register *handler* to be called whenever *event_type* is published."""
        if handler not in self._subscribers[event_type]:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """Remove *handler* from *event_type* subscribers.

        Silently ignores handlers that are not subscribed.
        """
        try:
            self._subscribers[event_type].remove(handler)
        except ValueError:
            pass

    async def publish(self, event_type: str, data: Any) -> None:
        """Dispatch *data* to every handler registered for *event_type*.

        Async handlers are awaited; sync handlers are called directly.
        Exceptions in individual handlers are logged but do not prevent
        remaining handlers from executing.
        """
        for handler in list(self._subscribers.get(event_type, [])):
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "Handler %r raised on event %r", handler, event_type
                )

    def clear(self) -> None:
        """Remove all subscriptions."""
        self._subscribers.clear()
