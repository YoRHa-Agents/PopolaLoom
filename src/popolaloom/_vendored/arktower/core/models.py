"""Pydantic v2 domain models for ArkTower task pool.

Vendored from ArkTower @ commit 467a087 (arktower/core/models.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    INPUT_REQUIRED = "input_required"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Trigger(str, enum.Enum):
    SUBMIT = "submit"
    ENQUEUE = "enqueue"
    CLAIM = "claim"
    REQUEST_INPUT = "request_input"
    RESUME = "resume"
    BLOCK = "block"
    UNBLOCK = "unblock"
    SEND_REVIEW = "send_review"
    APPROVE = "approve"
    REJECT = "reject"
    COMPLETE = "complete"
    FAIL = "fail"
    CANCEL = "cancel"
    TIMEOUT = "timeout"
    REOPEN = "reopen"


class DependencyType(str, enum.Enum):
    BLOCKS = "blocks"
    RELATES_TO = "relates_to"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.SUBMITTED
    priority: TaskPriority = TaskPriority.MEDIUM
    parent_id: str | None = None
    context_id: str | None = None
    owner_id: str = "system"
    assigned_to: str | None = None
    assigned_type: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    output: str | None = None
    error: str | None = None
    tags: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    template_id: str | None = None
    max_steps: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    estimated_complexity: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    task_type: str | None = None
    kind: str = "task"

    timeout_seconds: int | None = None
    max_retries: int = 0
    deadline: datetime | None = None
    budget_tokens: int | None = None

    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    context_refs: list[dict[str, str]] = Field(default_factory=list)
    subtask_ids: list[str] = Field(default_factory=list)

    quality_thresholds: dict[str, Any] = Field(default_factory=dict)
    estimated_effort_minutes: int | None = None

    agent_instructions: str | None = None
    preferred_agent_type: str | None = None
    retry_count: int = 0


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: TaskPriority = TaskPriority.MEDIUM
    parent_id: str | None = None
    context_id: str | None = None
    owner_id: str = "system"
    tags: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    template_id: str | None = None
    max_steps: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    estimated_complexity: str | None = None

    task_type: str | None = None
    kind: str = "task"

    timeout_seconds: int | None = None
    max_retries: int = 0
    deadline: datetime | None = None
    budget_tokens: int | None = None

    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    context_refs: list[dict[str, str]] = Field(default_factory=list)
    subtask_ids: list[str] = Field(default_factory=list)

    quality_thresholds: dict[str, Any] = Field(default_factory=dict)
    estimated_effort_minutes: int | None = None

    agent_instructions: str | None = None
    preferred_agent_type: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    tags: list[str] | None = None
    labels: dict[str, str] | None = None
    parameters: dict[str, Any] | None = None
    max_steps: int | None = None
    output: str | None = None
    error: str | None = None
    assigned_to: str | None = None
    assigned_type: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    task_type: str | None = None
    kind: str | None = None

    timeout_seconds: int | None = None
    max_retries: int | None = None
    deadline: datetime | None = None
    budget_tokens: int | None = None

    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    acceptance_criteria: list[str] | None = None
    constraints: list[str] | None = None

    context_refs: list[dict[str, str]] | None = None
    subtask_ids: list[str] | None = None

    quality_thresholds: dict[str, Any] | None = None
    estimated_effort_minutes: int | None = None

    agent_instructions: str | None = None
    preferred_agent_type: str | None = None
    retry_count: int | None = None


class TaskFilter(BaseModel):
    status: list[TaskStatus] | None = None
    priority: list[TaskPriority] | None = None
    tags: list[str] | None = None
    assigned_to: str | None = None
    parent_id: str | None = None
    context_id: str | None = None
    search: str | None = None
    task_type: str | None = None
    kind: str | None = None
    preferred_agent_type: str | None = None
    limit: int = 50
    offset: int = 0


class TaskEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    trigger: Trigger
    from_status: TaskStatus
    to_status: TaskStatus
    actor: str = "system"
    notes: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Dependency(BaseModel):
    from_task_id: str
    to_task_id: str
    dep_type: DependencyType = DependencyType.BLOCKS


class TaskTemplate(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    default_priority: TaskPriority = TaskPriority.MEDIUM
    default_tags: list[str] = Field(default_factory=list)
    default_labels: dict[str, str] = Field(default_factory=dict)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    checklist: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PoolStats(BaseModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    oldest_queued_age_seconds: float | None = None
    avg_completion_seconds: float | None = None
