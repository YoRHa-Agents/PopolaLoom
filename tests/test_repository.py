"""Stage C C1 tests — :mod:`popolaloom.daemon.repository` real ArkTower接入.

Closes R-004 P0 (ArkTower schema parity → real persistence) per
v0.2.0-plan §4 Stage C C1.  Three required cases:

1. ``test_make_persistence_creates_db_and_schema`` — make_persistence in
   tmp_path → SQLite file exists, can list tasks (empty initial result).
2. ``test_dispatch_persists_to_arktower`` — Popolad with persistence →
   dispatch_task → query repository, find the new task with
   ``parameters["popola_task_id"]`` == popola id.
3. ``test_arktower_failure_returns_none_persisted_false`` — mock
   ``task_service.create_task`` to raise → handle.persisted == False,
   ``arktower_task_id is None``.

Plus bonus rehydrate test for AC #8 (``rehydrate_from_persistence``).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from popolaloom._vendored.arktower.core.models import (
    TaskCreate,
    TaskFilter,
    TaskStatus,
)
from popolaloom.daemon import (
    Popolad,
    TaskPersistence,
    make_persistence,
)

_ARKTOWER_MIGRATIONS_DIR = Path("/home/agent/reference/ArkTower/migrations")
_POPOLALOOM_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def persistence(tmp_path: Path) -> Iterator[TaskPersistence]:
    """Build a fully-migrated TaskPersistence on a tmp_path SQLite file."""
    db_path = tmp_path / "arktower.db"
    p = make_persistence(
        db_path=db_path,
        arktower_migrations_dir=_ARKTOWER_MIGRATIONS_DIR,
        popolaloom_migrations_dir=_POPOLALOOM_MIGRATIONS_DIR,
    )
    try:
        yield p
    finally:
        p.close()


def _noop_adapter(
    cli: str,
    prompt: str,
    cwd: Path | None,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Echo adapter that exits 0 quickly (4-arg per Stage E AdapterCallback)."""
    return [sys.executable, "-c", f"print('persisted:', {prompt!r})"]


# ── 1. make_persistence creates the schema ──────────────────────────────


def test_make_persistence_creates_db_and_schema(tmp_path: Path) -> None:
    """``make_persistence`` opens the DB, applies migrations, exposes a usable repo.

    Verifies AC #5 prerequisite: ``~/.arktower/arktower.db`` (here
    tmp_path) is created with the ArkTower 4 + PopolaLoom 1 = 5
    migrations applied; ``schema_version`` table reports version 5.
    Repository.list returns an empty list (DB is fresh).  Also asserts
    that the popolaloom-specific ``popola_dispatch`` table was created.
    """
    db_path = tmp_path / "arktower.db"
    p = make_persistence(
        db_path=db_path,
        arktower_migrations_dir=_ARKTOWER_MIGRATIONS_DIR,
        popolaloom_migrations_dir=_POPOLALOOM_MIGRATIONS_DIR,
    )
    try:
        assert db_path.exists(), f"SQLite file not created at {db_path}"

        tasks = p.repository.list(TaskFilter())
        assert tasks == [], f"Fresh DB should have no tasks, got: {tasks}"

        conn = p.connection.get_connection()
        ver_row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        assert ver_row["v"] >= 5, (
            f"Expected schema_version >= 5 (ArkTower 1-4 + popolaloom 005); got {ver_row['v']}"
        )

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tasks" in tables, f"ArkTower 'tasks' table missing; got {tables}"
        assert "task_history" in tables, (
            f"ArkTower 'task_history' table missing; got {tables}"
        )
        assert "popola_dispatch" in tables, (
            f"PopolaLoom 'popola_dispatch' table missing; got {tables}"
        )
    finally:
        p.close()


# ── 2. dispatch_task persists to ArkTower ───────────────────────────────


def test_dispatch_persists_to_arktower(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """End-to-end: Popolad with persistence → dispatch → row appears in ArkTower.

    Closes R-004: the popola dispatch path now writes a real
    ``arktower.tasks`` row via ``TaskService.create_task``.  Verified by:

    1. ``status["persisted"] is True``
    2. ``status["arktower_task_id"]`` matches a row found in
       ``repository.list(TaskFilter())``
    3. The persisted row's ``parameters["popola_task_id"]`` equals the
       popola task id (the cross-reference popolaloom relies on for
       rehydrate).
    """
    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )

    task_id = popolad.dispatch_task(cli="cursor", prompt="hello arktower")

    status = popolad.get_status(task_id)
    assert status["persisted"] is True, f"persisted should be True; got {status}"
    assert status["arktower_task_id"] is not None, (
        f"arktower_task_id should not be None; got {status}"
    )

    ark_tasks = persistence.repository.list(TaskFilter(limit=50))
    assert len(ark_tasks) == 1, f"Expected 1 ArkTower task, got {len(ark_tasks)}"
    ark_task = ark_tasks[0]

    assert ark_task.id == status["arktower_task_id"], (
        f"ArkTower id mismatch: {ark_task.id} vs {status['arktower_task_id']}"
    )
    # ``kind`` and ``preferred_agent_type`` are upstream ArkTower props
    # that ``TaskService.create_task`` does NOT yet forward from
    # TaskCreate to Task; we therefore only assert on the fields
    # ArkTower 0.1.0 reliably persists (parameters is one of them).
    assert ark_task.parameters.get("popola_task_id") == task_id, (
        f"popola_task_id roundtrip failed; ark_task.parameters={ark_task.parameters}"
    )
    assert ark_task.parameters.get("cli") == "cursor"
    assert ark_task.title.startswith("[cursor]")
    assert "hello arktower" in ark_task.description
    assert ark_task.status == TaskStatus.SUBMITTED

    history = persistence.repository.get_history(ark_task.id)
    assert len(history) >= 1, (
        f"Expected at least one TaskEvent for SUBMIT transition; got {history}"
    )


# ── 3. ArkTower failure returns (None, False) ───────────────────────────


def test_arktower_failure_returns_none_persisted_false(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """When ``TaskService.create_task`` raises, dispatch must not silently fake success.

    R-008 alignment: persistence failure → ``arktower_task_id is None``,
    ``persisted is False``, dispatch still completes (graceful
    degradation), task.dispatched event still emitted with
    ``persisted=False`` so consumers see the explicit signal.

    We force the failure by monkey-patching ``persistence.task_service``'s
    repository to raise inside ``create``; this exercises the full
    ``_maybe_create_arktower_task`` exception path without altering
    public APIs.
    """
    events_dir = tmp_path / "events"

    class _BoomRepo:
        """Repository stand-in that raises on ``create()``."""

        def create(self, task: Any) -> Any:
            raise RuntimeError("simulated ArkTower DB outage")

    persistence.task_service._repo = _BoomRepo()  # type: ignore[attr-defined]

    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )

    task_id = popolad.dispatch_task(cli="claude", prompt="arktower-down")

    status = popolad.get_status(task_id)
    assert status["persisted"] is False, (
        f"persisted should be False on ArkTower failure; got {status}"
    )
    assert status["arktower_task_id"] is None, (
        f"arktower_task_id should be None on ArkTower failure; got {status}"
    )

    events = popolad.tail_events(task_id)
    dispatched = next((ev for ev in events if ev["type"] == "task.dispatched"), None)
    assert dispatched is not None, f"task.dispatched missing; got {events}"
    assert dispatched["data"]["persisted"] is False, (
        f"task.dispatched.persisted should be False; got {dispatched}"
    )
    assert dispatched["data"]["arktower_task_id"] is None


# ── 4. Bonus: rehydrate_from_persistence (AC #8) ─────────────────────────


def test_rehydrate_from_persistence_loads_in_flight_tasks(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """``rehydrate_from_persistence`` loads non-terminal tasks back into StateStore.

    Stage E (R-002 closure) widens the rehydrate filter from
    ``IN_PROGRESS+INPUT_REQUIRED`` to **all non-terminal statuses**
    (``SUBMITTED`` / ``QUEUED`` / ``IN_PROGRESS`` / ``REVIEW`` /
    ``INPUT_REQUIRED`` / ``BLOCKED``) because v0.2.0 dispatch leaves
    new ArkTower rows in ``SUBMITTED`` (the upstream lifecycle is
    owned by ``TaskService`` which doesn't auto-advance).  S1 self-
    bootstrap requires SUBMITTED tasks to be rehydratable so the
    sleeping subprocess from before the SIGKILL is still visible
    after restart.

    Test plan:

    1. Pre-seed two tasks: one SUBMITTED (popola-dispatch'd, never
       advanced) + one IN_PROGRESS (advanced + claimed). Both must
       rehydrate (n == 2) since both are non-terminal.
    2. Pre-seed a third task that we mark COMPLETED — it must NOT
       rehydrate (terminal status filter).
    3. Verify state_store reflects exactly 2 active handles.
    """
    import asyncio

    from popolaloom._vendored.arktower.core.models import Trigger

    async def _seed() -> tuple[str, str, str]:
        submitted = await persistence.task_service.create_task(
            TaskCreate(
                title="[cursor] one",
                description="one",
                parameters={
                    "popola_task_id": "cursor-aaaaaaaaaaaa",
                    "cli": "cursor",
                    "cmd": ["echo", "one"],
                },
                kind="popola.dispatch",
                preferred_agent_type="cursor",
            )
        )
        running = await persistence.task_service.create_task(
            TaskCreate(
                title="[claude] two",
                description="two",
                parameters={
                    "popola_task_id": "claude-bbbbbbbbbbbb",
                    "cli": "claude",
                    "cmd": ["echo", "two"],
                },
                kind="popola.dispatch",
                preferred_agent_type="claude",
            )
        )
        await persistence.task_service.advance_task(running.id, Trigger.ENQUEUE)
        await persistence.task_service.claim_task(running.id, agent_id="claude-agent-1")

        completed = await persistence.task_service.create_task(
            TaskCreate(
                title="[codex] done",
                description="done",
                parameters={
                    "popola_task_id": "codex-ccccccccccc",
                    "cli": "codex",
                    "cmd": ["echo", "done"],
                },
                kind="popola.dispatch",
                preferred_agent_type="codex",
            )
        )
        await persistence.task_service.advance_task(completed.id, Trigger.ENQUEUE)
        await persistence.task_service.claim_task(completed.id, agent_id="codex-agent-1")
        await persistence.task_service.advance_task(completed.id, Trigger.COMPLETE)
        return submitted.id, running.id, completed.id

    submitted_id, running_id, completed_id = asyncio.run(_seed())

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )
    n = popolad.rehydrate_from_persistence()
    assert n == 2, (
        f"Expected 2 non-terminal tasks (submitted + in_progress) to rehydrate; got {n}"
    )

    active = popolad.list_active()
    active_ids = {item["task_id"] for item in active}
    assert active_ids == {"cursor-aaaaaaaaaaaa", "claude-bbbbbbbbbbbb"}, (
        f"Expected both non-terminal tasks active after rehydrate; got {active_ids}"
    )

    handle_running = popolad.state_store.get("claude-bbbbbbbbbbbb")
    assert handle_running is not None
    assert handle_running.arktower_task_id == running_id
    assert handle_running.persisted is True
    assert handle_running.cli == "claude"

    handle_submitted = popolad.state_store.get("cursor-aaaaaaaaaaaa")
    assert handle_submitted is not None
    assert handle_submitted.arktower_task_id == submitted_id
    assert handle_submitted.cli == "cursor"
    assert handle_submitted.persisted is True

    assert popolad.state_store.get("codex-ccccccccccc") is None, (
        "completed task must not rehydrate"
    )
    _ = completed_id


# ── 5. R-002 closure: popolad.recovered event emitted per rehydrated task ──


def test_rehydrate_emits_popolad_recovered_event(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """``rehydrate_from_persistence`` writes ``popolad.recovered`` to each task's NDJSON.

    R-002 closure (Stage E E1): the S1 self-bootstrap test relies on
    finding a ``popolad.recovered`` envelope in the task event log
    after a daemon SIGKILL → restart → rehydrate cycle. Per Stage E
    plan, the event payload includes ``recovered_count`` (cohort size)
    and ``task_ids`` (full list) so consumers can correlate cohort
    membership across multiple per-task files.
    """
    import asyncio

    from popolaloom._vendored.arktower.core.models import Trigger

    async def _seed() -> str:
        running = await persistence.task_service.create_task(
            TaskCreate(
                title="[cursor] recovered_event",
                description="check popolad.recovered event",
                parameters={
                    "popola_task_id": "cursor-recoveredtest",
                    "cli": "cursor",
                    "cmd": ["echo", "hi"],
                },
                kind="popola.dispatch",
                preferred_agent_type="cursor",
            )
        )
        await persistence.task_service.advance_task(running.id, Trigger.ENQUEUE)
        await persistence.task_service.claim_task(running.id, agent_id="cursor-agent-1")
        return running.id

    asyncio.run(_seed())

    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )
    n = popolad.rehydrate_from_persistence()
    assert n == 1, f"expected 1 rehydrated task, got {n}"

    events = popolad.tail_events("cursor-recoveredtest")
    recovered = [ev for ev in events if ev["type"] == "popolad.recovered"]
    assert len(recovered) == 1, (
        f"expected exactly 1 popolad.recovered event, got {len(recovered)}; "
        f"all events: {events}"
    )
    payload = recovered[0]["data"]
    assert payload["popola_task_id"] == "cursor-recoveredtest"
    assert payload["cli"] == "cursor"
    assert payload["recovered_count"] == 1
    assert payload["task_ids"] == ["cursor-recoveredtest"]
    assert payload["arktower_task_id"]
