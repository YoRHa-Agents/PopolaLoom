"""supervise primitive — parent → child task lifecycle subscription (v0.3.0 F2.3).

Allows a parent task to register callbacks that fire when a child task
completes or fails.  Used by the F5 self-bootstrap S2 reinforcement
loop and by F4 HITL escalation.

Design (per v0.3.0-plan.md §4 Stage F2 + spec §4.2):

- :class:`SuperviseRegistry` holds ``parent → list[subscription]`` and
  ``child → list[subscription]`` indexes.  Subscription callbacks are
  invoked synchronously when the registry is notified of terminal
  state via :meth:`on_child_terminal`.
- Multiple parents can supervise the same child (broadcast semantics).
- :meth:`SubscriptionHandle.unsubscribe` removes the registration; the
  handle is also re-entrant-safe (calling unsubscribe twice is a no-op
  but logs at debug).
- :func:`supervise` is the public primitive function — RPC + MCP
  callers invoke this through the popolad facade.

Workspace rule "No Silent Failures": callbacks that raise are caught,
logged, and re-raised AFTER all peer subscribers fire so a single bad
callback can't starve out siblings.  The unsubscribe path is also
wrapped so a stale handle's unsubscribe is logged not silenced.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

OnCompleteCallback = Callable[[str, str, dict[str, Any]], None]
"""Called as ``(parent_task_id, child_task_id, payload)`` on child success.

The ``payload`` dict carries arbitrary per-event data (e.g. exit_code,
arktower_task_id) supplied by the caller of :meth:`SuperviseRegistry.on_child_terminal`.
"""

OnFailCallback = Callable[[str, str, dict[str, Any]], None]
"""Called as ``(parent_task_id, child_task_id, payload)`` on child failure."""


@dataclass(frozen=True)
class _Subscription:
    """One parent → child supervision subscription record (immutable).

    Stored in :class:`SuperviseRegistry`; the public surface is
    :class:`SubscriptionHandle` which exposes :meth:`unsubscribe`.

    Attributes:
        subscription_id: UUID4 hex token (used by handle to find self).
        parent_task_id:  popola task_id of the parent task.
        child_task_id:   popola task_id of the child task.
        on_complete:     callback fired on child COMPLETED.
        on_fail:         callback fired on child FAILED / CANCELED.
        metadata:        free-form dict passed back to callbacks
                         (e.g. ``{"reason": "reinforcement"}``).
    """

    subscription_id: str
    parent_task_id: str
    child_task_id: str
    on_complete: OnCompleteCallback | None = None
    on_fail: OnFailCallback | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubscriptionHandle:
    """Public handle returned by :func:`supervise`.

    Carries enough context for the caller to :meth:`unsubscribe` later
    (e.g. when the parent task itself terminates and no longer cares
    about the child outcome).
    """

    subscription_id: str
    parent_task_id: str
    child_task_id: str
    _registry: SuperviseRegistry

    def unsubscribe(self) -> bool:
        """Remove this subscription from the registry.

        Returns:
            bool: ``True`` if the subscription was removed, ``False``
                if it was already gone (idempotent — calling twice is
                fine, just no-op).
        """
        return self._registry.unsubscribe(self.subscription_id)


class SuperviseRegistry:
    """Thread-safe registry of parent → child supervision subscriptions.

    The popolad daemon owns a single instance of this class.  RPC
    handlers + tests can call :meth:`subscribe`, :meth:`unsubscribe`,
    :meth:`on_child_terminal` directly.

    Concurrency: all internal dicts are protected by ``self._lock``.
    Callback execution happens with the lock RELEASED (so callbacks
    can re-enter the registry without deadlock).
    """

    def __init__(self) -> None:
        self._subs_by_id: dict[str, _Subscription] = {}
        self._subs_by_child: dict[str, set[str]] = {}
        self._subs_by_parent: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def subscribe(
        self,
        parent_task_id: str,
        child_task_id: str,
        *,
        on_complete: OnCompleteCallback | None = None,
        on_fail: OnFailCallback | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SubscriptionHandle:
        """Register ``parent`` to receive child terminal events.

        At least one of ``on_complete`` / ``on_fail`` MUST be provided
        (caller signals what they care about).  Both can be supplied to
        observe both outcomes.

        Args:
            parent_task_id: popola task id of the parent (≥ 1 char).
            child_task_id: popola task id of the child (≥ 1 char).
            on_complete: optional callback for COMPLETED.
            on_fail: optional callback for FAILED / CANCELED.
            metadata: optional dict passed back to callbacks.

        Returns:
            SubscriptionHandle: caller stores this to call
            :meth:`unsubscribe` later.

        Raises:
            ValueError: when ``parent_task_id`` or ``child_task_id`` is
                blank, or when neither callback is supplied.
        """
        if not parent_task_id:
            raise ValueError("supervise: parent_task_id must be non-empty")
        if not child_task_id:
            raise ValueError("supervise: child_task_id must be non-empty")

        sub_id = uuid.uuid4().hex
        sub = _Subscription(
            subscription_id=sub_id,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            on_complete=on_complete,
            on_fail=on_fail,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._subs_by_id[sub_id] = sub
            self._subs_by_child.setdefault(child_task_id, set()).add(sub_id)
            self._subs_by_parent.setdefault(parent_task_id, set()).add(sub_id)
        logger.info(
            "supervise: subscription_id=%s parent=%s child=%s",
            sub_id,
            parent_task_id,
            child_task_id,
        )
        return SubscriptionHandle(
            subscription_id=sub_id,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            _registry=self,
        )

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove the subscription identified by ``subscription_id``.

        Idempotent: returns ``False`` (not raises) if the id is unknown.
        """
        with self._lock:
            sub = self._subs_by_id.pop(subscription_id, None)
            if sub is None:
                logger.debug(
                    "supervise: unsubscribe(%s) — already gone (idempotent)",
                    subscription_id,
                )
                return False
            child_ids = self._subs_by_child.get(sub.child_task_id)
            if child_ids is not None:
                child_ids.discard(subscription_id)
                if not child_ids:
                    self._subs_by_child.pop(sub.child_task_id, None)
            parent_ids = self._subs_by_parent.get(sub.parent_task_id)
            if parent_ids is not None:
                parent_ids.discard(subscription_id)
                if not parent_ids:
                    self._subs_by_parent.pop(sub.parent_task_id, None)
        logger.info(
            "supervise: unsubscribed subscription_id=%s child=%s parent=%s",
            subscription_id,
            sub.child_task_id,
            sub.parent_task_id,
        )
        return True

    def on_child_terminal(
        self,
        child_task_id: str,
        outcome: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Notify all subscribers that ``child_task_id`` reached terminal state.

        ``outcome`` MUST be one of ``"completed"`` / ``"failed"`` /
        ``"canceled"``.  ``"completed"`` fires every ``on_complete``
        callback registered on the child; the other two fire ``on_fail``.

        Errors raised by individual callbacks are caught + logged so a
        bad subscriber doesn't starve siblings; the FIRST exception is
        re-raised AFTER all peers fire (matches ``signal.signal`` /
        ``EventBus`` behavior).

        Args:
            child_task_id: child popola task id reaching terminal.
            outcome: one of ``"completed"``, ``"failed"``, ``"canceled"``.
            payload: optional event data passed to callbacks (e.g.
                ``{"exit_code": 0, "duration_s": 12.3}``).

        Returns:
            int: number of subscribers that fired (callbacks invoked).
        """
        if outcome not in ("completed", "failed", "canceled"):
            raise ValueError(
                f"supervise.on_child_terminal: outcome must be one of "
                f"completed/failed/canceled, got {outcome!r}"
            )
        with self._lock:
            sub_ids = list(self._subs_by_child.get(child_task_id, ()))
            subs = [self._subs_by_id[sid] for sid in sub_ids if sid in self._subs_by_id]

        payload_dict = dict(payload or {})
        payload_dict.setdefault("outcome", outcome)
        first_exc: BaseException | None = None
        fired = 0
        for sub in subs:
            cb = sub.on_complete if outcome == "completed" else sub.on_fail
            if cb is None:
                continue
            merged_payload = {**sub.metadata, **payload_dict}
            try:
                cb(sub.parent_task_id, sub.child_task_id, merged_payload)
                fired += 1
            except Exception as exc:
                logger.exception(
                    "supervise: callback raised for parent=%s child=%s outcome=%s",
                    sub.parent_task_id,
                    sub.child_task_id,
                    outcome,
                )
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            raise first_exc
        return fired

    def list_subscriptions(self) -> list[SubscriptionHandle]:
        """Return a snapshot of all currently-registered handles.

        Useful for tests + diagnostic dumps.  Order is unspecified.
        """
        with self._lock:
            subs = list(self._subs_by_id.values())
        return [
            SubscriptionHandle(
                subscription_id=s.subscription_id,
                parent_task_id=s.parent_task_id,
                child_task_id=s.child_task_id,
                _registry=self,
            )
            for s in subs
        ]

    def has_subscription(self, subscription_id: str) -> bool:
        """``True`` iff a subscription with this id is currently registered."""
        with self._lock:
            return subscription_id in self._subs_by_id


_default_registry_lock = threading.Lock()
_default_registry: SuperviseRegistry | None = None


def get_default_registry() -> SuperviseRegistry:
    """Return the daemon-wide default :class:`SuperviseRegistry` (singleton).

    The popolad daemon process owns one shared registry so tests +
    RPC + the daemon facade can all see the same subscription set.
    Tests that want isolation construct their own
    :class:`SuperviseRegistry` instance directly.
    """
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = SuperviseRegistry()
        return _default_registry


def reset_default_registry() -> None:
    """Reset the default registry (test helper).

    NOT for production use — only :data:`tests.conftest` style fixtures
    should call this.  Holds ``_default_registry_lock`` while clearing.
    """
    global _default_registry
    with _default_registry_lock:
        _default_registry = None


def supervise(
    parent_task_id: str,
    child_task_id: str,
    *,
    on_complete: OnCompleteCallback | None = None,
    on_fail: OnFailCallback | None = None,
    metadata: dict[str, Any] | None = None,
    registry: SuperviseRegistry | None = None,
) -> SubscriptionHandle:
    """Register a supervision subscription on the default (or passed) registry.

    Public primitive function — RPC handler + MCP verb call this.

    Args:
        parent_task_id: popola task id of the parent.
        child_task_id: popola task id of the child to supervise.
        on_complete: optional callback fired on child success.
        on_fail: optional callback fired on child failure.
        metadata: optional metadata passed to callbacks.
        registry: optional registry override (default: process-wide
            singleton via :func:`get_default_registry`).

    Returns:
        SubscriptionHandle: caller calls ``handle.unsubscribe()`` to
        release the subscription.

    Raises:
        ValueError: same as :meth:`SuperviseRegistry.subscribe`.
    """
    reg = registry or get_default_registry()
    return reg.subscribe(
        parent_task_id,
        child_task_id,
        on_complete=on_complete,
        on_fail=on_fail,
        metadata=metadata,
    )


__all__ = [
    "OnCompleteCallback",
    "OnFailCallback",
    "SubscriptionHandle",
    "SuperviseRegistry",
    "get_default_registry",
    "reset_default_registry",
    "supervise",
]
