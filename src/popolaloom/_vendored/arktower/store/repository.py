"""Abstract repository protocol defining the storage contract.

Vendored from ArkTower @ commit 467a087 (arktower/store/repository.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

from typing import Protocol

from popolaloom._vendored.arktower.core.models import (
    Dependency,
    PoolStats,
    Task,
    TaskEvent,
    TaskFilter,
    TaskTemplate,
    TaskUpdate,
)


class TaskRepository(Protocol):
    """Storage contract for the ArkTower task pool.

    Concrete implementations (e.g. ``SqliteTaskRepository``) must satisfy
    every method signature listed here.  Using ``typing.Protocol`` allows
    structural subtyping — no inheritance required.
    """

    def create(self, task: Task) -> Task: ...

    def get(self, task_id: str) -> Task | None: ...

    def update(self, task_id: str, updates: TaskUpdate) -> Task: ...

    def atomic_claim(
        self,
        task_id: str,
        agent_id: str,
        agent_type: str | None = None,
    ) -> Task: ...

    def delete(self, task_id: str) -> bool: ...

    def list(self, filters: TaskFilter) -> list[Task]: ...

    def count(self, filters: TaskFilter) -> int: ...

    def record_event(self, event: TaskEvent) -> None: ...

    def get_history(self, task_id: str) -> list[TaskEvent]: ...

    def create_dependency(self, dep: Dependency) -> None: ...

    def get_dependencies(self, task_id: str) -> list[Dependency]: ...

    def get_dependents(self, task_id: str) -> list[Dependency]: ...

    def create_template(self, template: TaskTemplate) -> TaskTemplate: ...

    def get_template(self, template_id: str) -> TaskTemplate | None: ...

    def list_templates(self) -> list[TaskTemplate]: ...

    def get_stats(self) -> PoolStats: ...
