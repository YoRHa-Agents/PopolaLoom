"""Concrete SQLite implementation of the TaskRepository protocol.

Vendored from ArkTower @ commit 467a087 (arktower/store/sqlite_repository.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from popolaloom._vendored.arktower.core.models import (
    Dependency,
    DependencyType,
    PoolStats,
    Task,
    TaskEvent,
    TaskFilter,
    TaskPriority,
    TaskStatus,
    TaskTemplate,
    TaskUpdate,
    Trigger,
)
from popolaloom._vendored.arktower.store.connection import DatabaseConnection

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unable to parse datetime: {value!r}")


class TaskNotFoundError(Exception):
    """Raised when a task lookup fails."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task not found: {task_id}")


class ClaimFailedError(Exception):
    """Raised when atomic claim cannot proceed (race or wrong status)."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(
            f"Failed to claim task {task_id}: not in 'queued' status or already claimed"
        )


class SqliteTaskRepository:
    """Full-featured SQLite implementation using JSON1 and FTS5.

    All public methods use parameterized queries to prevent SQL injection.
    JSON fields (``tags``, ``labels``, ``parameters``) are stored as TEXT
    and serialized/deserialized transparently.
    """

    def __init__(self, connection: DatabaseConnection) -> None:
        self._db = connection

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._db.get_connection()

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert a tasks-table row into a Task model, resolving tags."""
        tags = [
            r["tag"]
            for r in self._conn.execute(
                "SELECT tag FROM tags WHERE task_id = ?", (row["id"],)
            )
        ]
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            priority=TaskPriority(row["priority"]),
            parent_id=row["parent_id"],
            context_id=row["context_id"],
            owner_id=row["owner_id"],
            assigned_to=row["assigned_to"],
            assigned_type=row["assigned_type"],
            parameters=json.loads(row["parameters"]),
            output=row["output"],
            error=row["error"],
            tags=tags,
            labels=json.loads(row["labels"]),
            template_id=row["template_id"],
            max_steps=row["max_steps"],
            capabilities=json.loads(row["capabilities"]),
            required_tools=json.loads(row["required_tools"]),
            estimated_complexity=row["estimated_complexity"],
            version=row["version"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            started_at=_parse_dt(row["started_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            task_type=row["task_type"],
            kind=row["kind"],
            timeout_seconds=row["timeout_seconds"],
            max_retries=row["max_retries"],
            deadline=_parse_dt(row["deadline"]),
            budget_tokens=row["budget_tokens"],
            input_schema=json.loads(row["input_schema"]),
            output_schema=json.loads(row["output_schema"]),
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
            constraints=json.loads(row["constraints"]),
            context_refs=json.loads(row["context_refs"]),
            subtask_ids=json.loads(row["subtask_ids"]),
            quality_thresholds=json.loads(row["quality_thresholds"]),
            estimated_effort_minutes=row["estimated_effort_minutes"],
            agent_instructions=row["agent_instructions"],
            preferred_agent_type=row["preferred_agent_type"],
            retry_count=row["retry_count"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> TaskEvent:
        return TaskEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            trigger=Trigger(row["trigger"]),
            from_status=TaskStatus(row["from_status"]),
            to_status=TaskStatus(row["to_status"]),
            actor=row["actor"],
            notes=row["notes"],
            timestamp=_parse_dt(row["timestamp"]),
        )

    def _row_to_dependency(self, row: sqlite3.Row) -> Dependency:
        return Dependency(
            from_task_id=row["from_task_id"],
            to_task_id=row["to_task_id"],
            dep_type=DependencyType(row["dep_type"]),
        )

    def _row_to_template(self, row: sqlite3.Row) -> TaskTemplate:
        return TaskTemplate(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            default_priority=TaskPriority(row["default_priority"]),
            default_tags=json.loads(row["default_tags"]),
            default_labels=json.loads(row["default_labels"]),
            parameter_schema=json.loads(row["parameter_schema"]),
            checklist=json.loads(row["checklist"]),
            created_at=_parse_dt(row["created_at"]),
        )

    def _insert_tags(self, task_id: str, tags: list[str]) -> None:
        if not tags:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO tags (task_id, tag) VALUES (?, ?)",
            [(task_id, t) for t in tags],
        )

    def _replace_tags(self, task_id: str, tags: list[str]) -> None:
        self._conn.execute("DELETE FROM tags WHERE task_id = ?", (task_id,))
        self._insert_tags(task_id, tags)

    @staticmethod
    def _dt_str(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def create(self, task: Task) -> Task:
        now = _utcnow_iso()
        conn = self._conn
        conn.execute(
            """INSERT INTO tasks
               (id, title, description, status, priority, parent_id,
                context_id, owner_id, assigned_to, assigned_type,
                parameters, output, error, labels, template_id,
                max_steps, capabilities, required_tools,
                estimated_complexity, version, created_at, updated_at,
                started_at, completed_at,
                task_type, kind, timeout_seconds, max_retries, deadline,
                budget_tokens, input_schema, output_schema,
                acceptance_criteria, constraints, context_refs,
                subtask_ids, quality_thresholds, estimated_effort_minutes,
                agent_instructions, preferred_agent_type, retry_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id,
                task.title,
                task.description,
                task.status.value,
                task.priority.value,
                task.parent_id,
                task.context_id,
                task.owner_id,
                task.assigned_to,
                task.assigned_type,
                json.dumps(task.parameters),
                task.output,
                task.error,
                json.dumps(task.labels),
                task.template_id,
                task.max_steps,
                json.dumps(task.capabilities),
                json.dumps(task.required_tools),
                task.estimated_complexity,
                task.version,
                self._dt_str(task.created_at) or now,
                self._dt_str(task.updated_at) or now,
                self._dt_str(task.started_at),
                self._dt_str(task.completed_at),
                task.task_type,
                task.kind,
                task.timeout_seconds,
                task.max_retries,
                self._dt_str(task.deadline),
                task.budget_tokens,
                json.dumps(task.input_schema),
                json.dumps(task.output_schema),
                json.dumps(task.acceptance_criteria),
                json.dumps(task.constraints),
                json.dumps(task.context_refs),
                json.dumps(task.subtask_ids),
                json.dumps(task.quality_thresholds),
                task.estimated_effort_minutes,
                task.agent_instructions,
                task.preferred_agent_type,
                task.retry_count,
            ),
        )
        self._insert_tags(task.id, task.tags)
        conn.commit()
        logger.debug("Created task %s", task.id)
        return task

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def update(self, task_id: str, updates: TaskUpdate) -> Task:
        existing = self.get(task_id)
        if existing is None:
            raise TaskNotFoundError(task_id)

        fields: dict[str, Any] = {}
        update_data = updates.model_dump(exclude_unset=True)
        tags_changed = False

        _json_fields = {
            "labels", "parameters", "capabilities", "required_tools",
            "input_schema", "output_schema", "acceptance_criteria",
            "constraints", "context_refs", "subtask_ids", "quality_thresholds",
        }
        _dt_fields = {"started_at", "completed_at", "deadline"}

        for key, value in update_data.items():
            if key == "tags":
                tags_changed = True
                continue
            if key in _json_fields:
                fields[key] = json.dumps(value)
            elif key == "priority":
                fields[key] = value.value if isinstance(value, TaskPriority) else value
            elif key == "status":
                fields[key] = value.value if isinstance(value, TaskStatus) else value
            elif key in _dt_fields:
                fields[key] = self._dt_str(value) if value is not None else None
            else:
                fields[key] = value

        if not fields and not tags_changed:
            return existing

        fields["updated_at"] = _utcnow_iso()
        fields["version"] = existing.version + 1

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]

        conn = self._conn
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)  # noqa: S608

        if tags_changed and updates.tags is not None:
            self._replace_tags(task_id, updates.tags)

        conn.commit()
        logger.debug("Updated task %s", task_id)

        refreshed = self.get(task_id)
        assert refreshed is not None  # noqa: S101
        return refreshed

    def delete(self, task_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("Deleted task %s", task_id)
        return deleted

    def _build_filter_query(
        self,
        filters: TaskFilter,
        *,
        select: str = "t.*",
    ) -> tuple[str, list[Any]]:
        """Build a parameterized WHERE clause from TaskFilter."""
        clauses: list[str] = []
        params: list[Any] = []

        use_fts = filters.search is not None and filters.search.strip()
        if use_fts:
            base = (
                f"SELECT {select} FROM tasks t "
                "INNER JOIN tasks_fts ON tasks_fts.rowid = t.rowid"
            )
            clauses.append("tasks_fts MATCH ?")
            params.append(filters.search)
        else:
            base = f"SELECT {select} FROM tasks t"

        if filters.status:
            placeholders = ", ".join("?" for _ in filters.status)
            clauses.append(f"t.status IN ({placeholders})")
            params.extend(s.value for s in filters.status)

        if filters.priority:
            placeholders = ", ".join("?" for _ in filters.priority)
            clauses.append(f"t.priority IN ({placeholders})")
            params.extend(p.value for p in filters.priority)

        if filters.assigned_to is not None:
            clauses.append("t.assigned_to = ?")
            params.append(filters.assigned_to)

        if filters.parent_id is not None:
            clauses.append("t.parent_id = ?")
            params.append(filters.parent_id)

        if filters.context_id is not None:
            clauses.append("t.context_id = ?")
            params.append(filters.context_id)

        if filters.tags:
            placeholders = ", ".join("?" for _ in filters.tags)
            clauses.append(
                f"EXISTS (SELECT 1 FROM tags WHERE task_id = t.id AND tag IN ({placeholders}))"
            )
            params.extend(filters.tags)

        if filters.task_type is not None:
            clauses.append("t.task_type = ?")
            params.append(filters.task_type)

        if filters.kind is not None:
            clauses.append("t.kind = ?")
            params.append(filters.kind)

        if filters.preferred_agent_type is not None:
            clauses.append("t.preferred_agent_type = ?")
            params.append(filters.preferred_agent_type)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return base + where, params

    def list(self, filters: TaskFilter) -> list[Task]:
        query, params = self._build_filter_query(filters)
        query += " ORDER BY t.created_at DESC LIMIT ? OFFSET ?"
        params.extend([filters.limit, filters.offset])

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    def count(self, filters: TaskFilter) -> int:
        query, params = self._build_filter_query(filters, select="COUNT(*) AS cnt")
        row = self._conn.execute(query, params).fetchone()
        return row["cnt"] if row else 0

    def atomic_claim(
        self,
        task_id: str,
        agent_id: str,
        agent_type: str | None = None,
    ) -> Task:
        """Atomically claim a queued task using a conditional UPDATE.

        Returns the claimed task or raises ``ClaimFailedError`` if the
        task is not in ``queued`` status (handles race conditions).
        """
        now = _utcnow_iso()
        cursor = self._conn.execute(
            """UPDATE tasks
               SET status = 'in_progress',
                   assigned_to = ?,
                   assigned_type = ?,
                   started_at = ?,
                   updated_at = ?,
                   version = version + 1
               WHERE id = ? AND status = 'queued'""",
            (agent_id, agent_type, now, now, task_id),
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise ClaimFailedError(task_id)
        self._conn.commit()
        logger.info("Task %s claimed by %s", task_id, agent_id)

        task = self.get(task_id)
        assert task is not None  # noqa: S101
        return task

    def record_event(self, event: TaskEvent) -> None:
        self._conn.execute(
            """INSERT INTO task_history
               (event_id, task_id, trigger, from_status, to_status,
                actor, notes, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                event.event_id,
                event.task_id,
                event.trigger.value,
                event.from_status.value,
                event.to_status.value,
                event.actor,
                event.notes,
                self._dt_str(event.timestamp) or _utcnow_iso(),
            ),
        )
        self._conn.commit()

    def get_history(self, task_id: str) -> list[TaskEvent]:
        rows = self._conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def create_dependency(self, dep: Dependency) -> None:
        self._conn.execute(
            """INSERT INTO dependencies (from_task_id, to_task_id, dep_type)
               VALUES (?, ?, ?)""",
            (dep.from_task_id, dep.to_task_id, dep.dep_type.value),
        )
        self._conn.commit()

    def get_dependencies(self, task_id: str) -> list[Dependency]:
        """Get dependencies where *task_id* depends on other tasks."""
        rows = self._conn.execute(
            "SELECT * FROM dependencies WHERE from_task_id = ?", (task_id,)
        ).fetchall()
        return [self._row_to_dependency(r) for r in rows]

    def get_dependents(self, task_id: str) -> list[Dependency]:
        """Get tasks that depend on *task_id*."""
        rows = self._conn.execute(
            "SELECT * FROM dependencies WHERE to_task_id = ?", (task_id,)
        ).fetchall()
        return [self._row_to_dependency(r) for r in rows]

    def create_template(self, template: TaskTemplate) -> TaskTemplate:
        now = _utcnow_iso()
        self._conn.execute(
            """INSERT INTO task_templates
               (id, name, description, default_priority, default_tags,
                default_labels, parameter_schema, checklist, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                template.id,
                template.name,
                template.description,
                template.default_priority.value,
                json.dumps(template.default_tags),
                json.dumps(template.default_labels),
                json.dumps(template.parameter_schema),
                json.dumps(template.checklist),
                self._dt_str(template.created_at) or now,
                now,
            ),
        )
        self._conn.commit()
        return template

    def get_template(self, template_id: str) -> TaskTemplate | None:
        row = self._conn.execute(
            "SELECT * FROM task_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_template(row)

    def list_templates(self) -> list[TaskTemplate]:
        rows = self._conn.execute(
            "SELECT * FROM task_templates ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_template(r) for r in rows]

    def get_stats(self) -> PoolStats:
        conn = self._conn

        total_row = conn.execute("SELECT COUNT(*) AS cnt FROM tasks").fetchone()
        total = total_row["cnt"] if total_row else 0

        by_status: dict[str, int] = {}
        for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"):
            by_status[row["status"]] = row["cnt"]

        by_priority: dict[str, int] = {}
        for row in conn.execute("SELECT priority, COUNT(*) AS cnt FROM tasks GROUP BY priority"):
            by_priority[row["priority"]] = row["cnt"]

        oldest_row = conn.execute(
            """SELECT MIN(created_at) AS oldest
               FROM tasks WHERE status = 'queued'"""
        ).fetchone()
        oldest_queued_age: float | None = None
        if oldest_row and oldest_row["oldest"]:
            oldest_dt = _parse_dt(oldest_row["oldest"])
            if oldest_dt is not None:
                delta = datetime.now(timezone.utc) - oldest_dt
                oldest_queued_age = delta.total_seconds()

        avg_row = conn.execute(
            """SELECT AVG(
                   (julianday(completed_at) - julianday(started_at)) * 86400
               ) AS avg_secs
               FROM tasks
               WHERE completed_at IS NOT NULL AND started_at IS NOT NULL"""
        ).fetchone()
        avg_completion: float | None = None
        if avg_row and avg_row["avg_secs"] is not None:
            avg_completion = float(avg_row["avg_secs"])

        return PoolStats(
            total=total,
            by_status=by_status,
            by_priority=by_priority,
            oldest_queued_age_seconds=oldest_queued_age,
            avg_completion_seconds=avg_completion,
        )
