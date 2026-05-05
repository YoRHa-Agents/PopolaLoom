"""Task lifecycle state machine with named-trigger transitions.

Vendored from ArkTower @ commit 467a087 (arktower/core/state_machine.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

from popolaloom._vendored.arktower.core.models import TaskStatus, Trigger

TRANSITION_TABLE: dict[Trigger, dict[TaskStatus | None, TaskStatus]] = {
    Trigger.SUBMIT: {None: TaskStatus.SUBMITTED},
    Trigger.ENQUEUE: {TaskStatus.SUBMITTED: TaskStatus.QUEUED},
    Trigger.CLAIM: {TaskStatus.QUEUED: TaskStatus.IN_PROGRESS},
    Trigger.REQUEST_INPUT: {TaskStatus.IN_PROGRESS: TaskStatus.INPUT_REQUIRED},
    Trigger.RESUME: {TaskStatus.INPUT_REQUIRED: TaskStatus.IN_PROGRESS},
    Trigger.BLOCK: {TaskStatus.IN_PROGRESS: TaskStatus.BLOCKED},
    Trigger.UNBLOCK: {TaskStatus.BLOCKED: TaskStatus.IN_PROGRESS},
    Trigger.SEND_REVIEW: {TaskStatus.IN_PROGRESS: TaskStatus.REVIEW},
    Trigger.APPROVE: {TaskStatus.REVIEW: TaskStatus.COMPLETED},
    Trigger.REJECT: {TaskStatus.REVIEW: TaskStatus.IN_PROGRESS},
    Trigger.COMPLETE: {TaskStatus.IN_PROGRESS: TaskStatus.COMPLETED},
    Trigger.FAIL: {TaskStatus.IN_PROGRESS: TaskStatus.FAILED},
    Trigger.CANCEL: {
        TaskStatus.SUBMITTED: TaskStatus.CANCELED,
        TaskStatus.QUEUED: TaskStatus.CANCELED,
        TaskStatus.IN_PROGRESS: TaskStatus.CANCELED,
        TaskStatus.REVIEW: TaskStatus.CANCELED,
        TaskStatus.INPUT_REQUIRED: TaskStatus.CANCELED,
        TaskStatus.BLOCKED: TaskStatus.CANCELED,
    },
    Trigger.TIMEOUT: {
        TaskStatus.IN_PROGRESS: TaskStatus.TIMED_OUT,
        TaskStatus.BLOCKED: TaskStatus.TIMED_OUT,
        TaskStatus.INPUT_REQUIRED: TaskStatus.TIMED_OUT,
    },
    Trigger.REOPEN: {
        TaskStatus.COMPLETED: TaskStatus.QUEUED,
        TaskStatus.FAILED: TaskStatus.QUEUED,
        TaskStatus.CANCELED: TaskStatus.QUEUED,
        TaskStatus.TIMED_OUT: TaskStatus.QUEUED,
    },
}

TERMINAL_STATES: set[TaskStatus] = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
    TaskStatus.TIMED_OUT,
}


class InvalidTransition(Exception):
    """Raised when a trigger cannot be applied to the current task status."""

    def __init__(self, current: TaskStatus | None, trigger: Trigger) -> None:
        self.current = current
        self.trigger = trigger
        status_label = current.value if current is not None else "(none)"
        super().__init__(
            f"Cannot apply trigger '{trigger.value}' to task in status '{status_label}'"
        )


TransitionError = InvalidTransition


class GateCheckError(Exception):
    """Raised when a pre-transition gate check fails (e.g. unresolved deps)."""


class StateMachine:
    """Validates and executes task state transitions via named triggers."""

    def validate_transition(
        self,
        current: TaskStatus | None,
        trigger: Trigger,
    ) -> TaskStatus:
        """Return the target status if the transition is valid.

        Raises ``InvalidTransition`` if *trigger* is not allowed from *current*.
        """
        allowed = TRANSITION_TABLE.get(trigger)
        if allowed is None:
            raise InvalidTransition(current, trigger)
        target = allowed.get(current)
        if target is None:
            raise InvalidTransition(current, trigger)
        return target

    def get_available_triggers(self, status: TaskStatus) -> list[Trigger]:
        """Return all triggers that can fire from *status*."""
        return [
            trigger
            for trigger, mapping in TRANSITION_TABLE.items()
            if status in mapping
        ]

    def is_terminal(self, status: TaskStatus) -> bool:
        """Return ``True`` if *status* is a terminal (end-of-life) state."""
        return status in TERMINAL_STATES
