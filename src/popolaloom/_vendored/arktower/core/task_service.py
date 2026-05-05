"""Application service coordinating tasks, repository persistence, and events.

Vendored from ArkTower @ commit 467a087 (arktower/core/task_service.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from popolaloom._vendored.arktower.core.event_bus import EventBus
from popolaloom._vendored.arktower.core.models import (
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
    StateMachine,
)
from popolaloom._vendored.arktower.store.repository import TaskRepository

TASK_TRANSITION_EVENT = "task.transition"


class TaskNotFoundError(Exception):
    """Raised when a task id does not exist in the repository."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task not found: {task_id}")


class TemplateNotFoundError(Exception):
    """Raised when a template id does not exist."""

    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        super().__init__(f"Template not found: {template_id}")


_PRIORITY_RANK: dict[TaskPriority, int] = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.MEDIUM: 2,
    TaskPriority.LOW: 3,
}


class TaskService:
    """Coordinates task lifecycle operations with state validation and audit events."""

    def __init__(
        self,
        repository: TaskRepository,
        event_bus: EventBus,
        *,
        state_machine: StateMachine | None = None,
    ) -> None:
        self._repo = repository
        self._bus = event_bus
        self._sm = state_machine or StateMachine()

    def get_task(self, task_id: str) -> Task:
        task = self._repo.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def update_task(self, task_id: str, updates: TaskUpdate) -> Task:
        return self._repo.update(task_id, updates)

    def list_tasks(self, filters: TaskFilter | None = None) -> list[Task]:
        return self._repo.list(filters or TaskFilter())

    def get_stats(self) -> PoolStats:
        return self._repo.get_stats()

    def create_template(self, template: TaskTemplate) -> TaskTemplate:
        return self._repo.create_template(template)

    async def create_task(self, data: TaskCreate, *, actor: str | None = None) -> Task:
        actor = actor or data.owner_id
        self._sm.validate_transition(None, Trigger.SUBMIT)
        task = Task(
            title=data.title,
            description=data.description,
            priority=data.priority,
            parent_id=data.parent_id,
            context_id=data.context_id,
            owner_id=data.owner_id,
            tags=list(data.tags),
            labels=dict(data.labels),
            parameters=dict(data.parameters),
            template_id=data.template_id,
            max_steps=data.max_steps,
            capabilities=list(data.capabilities),
            required_tools=list(data.required_tools),
            estimated_complexity=data.estimated_complexity,
            status=TaskStatus.SUBMITTED,
        )
        created = self._repo.create(task)
        event = TaskEvent(
            task_id=created.id,
            trigger=Trigger.SUBMIT,
            from_status=TaskStatus.SUBMITTED,
            to_status=TaskStatus.SUBMITTED,
            actor=actor,
        )
        self._repo.record_event(event)
        await self._bus.publish(TASK_TRANSITION_EVENT, event)
        return created

    async def create_from_template(
        self,
        template_id: str,
        title: str,
        *,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Task:
        template = self._repo.get_template(template_id)
        if template is None:
            raise TemplateNotFoundError(template_id)
        merged_params = dict(parameters or {})
        create = TaskCreate(
            title=title,
            description=description if description is not None else template.description,
            priority=template.default_priority,
            tags=list(template.default_tags),
            labels=dict(template.default_labels),
            parameters=merged_params,
            template_id=template_id,
        )
        return await self.create_task(create, actor=actor)

    def get_next_task(self) -> Task | None:
        queued = self._repo.list(
            TaskFilter(status=[TaskStatus.QUEUED], limit=500, offset=0)
        )
        if not queued:
            return None
        queued.sort(
            key=lambda t: (
                _PRIORITY_RANK.get(t.priority, 99),
                t.created_at,
            )
        )
        return queued[0]

    def get_next_task_for_agent(self, capabilities: list[str]) -> Task | None:
        """Find the highest-priority queued task whose required capabilities are
        a subset of the given agent capabilities. Falls back to
        :meth:`get_next_task` when *capabilities* is empty."""
        if not capabilities:
            return self.get_next_task()
        queued = self._repo.list(
            TaskFilter(status=[TaskStatus.QUEUED], limit=500, offset=0)
        )
        if not queued:
            return None
        agent_caps = set(capabilities)
        eligible = [t for t in queued if set(t.capabilities).issubset(agent_caps)]
        if not eligible:
            return None
        eligible.sort(
            key=lambda t: (
                _PRIORITY_RANK.get(t.priority, 99),
                t.created_at,
            )
        )
        return eligible[0]

    async def advance_task(
        self,
        task_id: str,
        trigger: Trigger,
        *,
        actor: str = "system",
        notes: str | None = None,
        extra: TaskUpdate | None = None,
    ) -> Task:
        if trigger == Trigger.CLAIM:
            raise ValueError(
                "CLAIM must be performed via claim_task(agent_id=...) for atomic semantics"
            )
        task = self.get_task(task_id)
        from_status = task.status
        to_status = self._sm.validate_transition(from_status, trigger)
        update = self._transition_updates(task, trigger, to_status, extra)
        updated = self._repo.update(task_id, update)
        event = TaskEvent(
            task_id=task_id,
            trigger=trigger,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            notes=notes,
        )
        self._repo.record_event(event)
        await self._bus.publish(TASK_TRANSITION_EVENT, event)
        return updated

    async def claim_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        agent_type: str | None = None,
        actor: str | None = None,
        notes: str | None = None,
    ) -> Task:
        actor = actor or agent_id
        task_before = self.get_task(task_id)
        from_status = task_before.status
        self._sm.validate_transition(from_status, Trigger.CLAIM)
        claimed = self._repo.atomic_claim(task_id, agent_id, agent_type)
        event = TaskEvent(
            task_id=task_id,
            trigger=Trigger.CLAIM,
            from_status=from_status,
            to_status=claimed.status,
            actor=actor,
            notes=notes,
        )
        self._repo.record_event(event)
        await self._bus.publish(TASK_TRANSITION_EVENT, event)
        return claimed

    async def complete_task(
        self,
        task_id: str,
        *,
        actor: str,
        output: str | None = None,
        notes: str | None = None,
    ) -> Task:
        extra = TaskUpdate(output=output) if output is not None else None
        return await self.advance_task(
            task_id,
            Trigger.COMPLETE,
            actor=actor,
            notes=notes,
            extra=extra,
        )

    async def fail_task(
        self,
        task_id: str,
        *,
        actor: str,
        error: str,
        notes: str | None = None,
    ) -> Task:
        return await self.advance_task(
            task_id,
            Trigger.FAIL,
            actor=actor,
            notes=notes,
            extra=TaskUpdate(error=error),
        )

    def _transition_updates(
        self,
        task: Task,
        trigger: Trigger,
        to_status: TaskStatus,
        extra: TaskUpdate | None,
    ) -> TaskUpdate:
        now = datetime.now(timezone.utc)
        parts: dict[str, Any] = {"status": to_status}

        if to_status in TERMINAL_STATES:
            parts["completed_at"] = now

        if trigger == Trigger.REOPEN:
            parts["completed_at"] = None
            parts["output"] = None
            parts["error"] = None

        base = TaskUpdate(**parts)
        if extra is None:
            return base
        merged = base.model_dump(exclude_unset=True) | extra.model_dump(exclude_unset=True)
        return TaskUpdate(**merged)
