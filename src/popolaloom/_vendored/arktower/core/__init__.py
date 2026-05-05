"""Core domain models and business logic (vendored subset).

This vendored package re-exports only the names PopolaLoom imports at
runtime. Upstream ``arktower.core.__init__`` also re-exports
``normalizer.TaskNormalizer``, but that pulls in
``arktower.analysis.tag_extractor`` which is NOT used by PopolaLoom and
is therefore not vendored. Refer to ``VENDORING.md`` at the repo root.
"""

from popolaloom._vendored.arktower.core.event_bus import EventBus
from popolaloom._vendored.arktower.core.models import (
    Dependency,
    DependencyType,
    PoolStats,
    Task,
    TaskCreate,
    TaskEvent,
    TaskFilter,
    TaskPriority,
    TaskStatus,
    TaskTemplate,
    TaskUpdate,
    Trigger,
)
from popolaloom._vendored.arktower.core.state_machine import (
    TERMINAL_STATES,
    TRANSITION_TABLE,
    GateCheckError,
    InvalidTransition,
    StateMachine,
    TransitionError,
)

__all__ = [
    "Dependency",
    "DependencyType",
    "EventBus",
    "GateCheckError",
    "InvalidTransition",
    "PoolStats",
    "StateMachine",
    "TERMINAL_STATES",
    "TRANSITION_TABLE",
    "Task",
    "TaskCreate",
    "TaskEvent",
    "TaskFilter",
    "TaskPriority",
    "TaskStatus",
    "TaskTemplate",
    "TaskUpdate",
    "TransitionError",
    "Trigger",
]
