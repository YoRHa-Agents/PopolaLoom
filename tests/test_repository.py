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
from importlib import resources
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
_POPOLALOOM_MIGRATIONS_DIR = Path(resources.files("popolaloom.migrations"))


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

    # v0.7.1 BUG-B contract: popolad-owned tasks (those with
    # popola_task_id) must have a popola_dispatch row to be rehydrated.
    # Seed the rows for the two non-terminal tasks here (the completed
    # task is filtered out by status and doesn't need one).
    conn = persistence.connection.get_connection()
    conn.executemany(
        """INSERT INTO popola_dispatch (dispatch_id, task_id, runtime, supervisor)
           VALUES (?, ?, 'popen', 'in-process')""",
        [
            (f"dispatch-{submitted_id[:8]}", submitted_id),
            (f"dispatch-{running_id[:8]}", running_id),
        ],
    )
    conn.commit()

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

    running_id = asyncio.run(_seed())

    # v0.7.1 BUG-B contract: popolad-owned tasks must have a
    # popola_dispatch row to rehydrate; seed it before constructing Popolad.
    conn = persistence.connection.get_connection()
    conn.execute(
        """INSERT INTO popola_dispatch (dispatch_id, task_id, runtime, supervisor)
           VALUES (?, ?, 'popen', 'in-process')""",
        (f"dispatch-{running_id[:8]}", running_id),
    )
    conn.commit()

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


# ── 6. v0.7.1 BUG-A: cancel orphan with no popola_dispatch row ──────────


def test_cancel_orphan_no_dispatch_row(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """v0.7.1 BUG-A: orphan reap when popola_dispatch has no row and started_at predates daemon.

    Reproduces the symptom from RELEASE_NOTES: an ArkTower task with
    status='submitted' lives on across a daemon restart but the
    popola_dispatch table never had a row (dispatch crashed before
    spawn). cancel_task must short-circuit the SIGTERM path and write
    a clean cancellation audit instead of forever raising
    'race window between dispatch and spawn'.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from popolaloom._vendored.arktower.core.models import (
        TaskCreate,
        TaskStatus,
    )
    from popolaloom.daemon.state import TaskHandle, TaskState

    async def _seed() -> str:
        ark = await persistence.task_service.create_task(
            TaskCreate(
                title="[cursor] orphan",
                description="orphan task",
                parameters={
                    "popola_task_id": "cursor-orphanpid0",
                    "cli": "cursor",
                    "cmd": ["sleep", "30"],
                },
                kind="popola.dispatch",
                preferred_agent_type="cursor",
            )
        )
        return ark.id

    arktower_task_id = asyncio.run(_seed())

    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )

    daemon_started_at = datetime.now(UTC)
    task_started_at = daemon_started_at - timedelta(minutes=10)
    handle = TaskHandle(
        task_id="cursor-orphanpid0",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=task_started_at,
        event_log_path=events_dir / "cursor-orphanpid0.jsonl",
        arktower_task_id=arktower_task_id,
        cmd=["sleep", "30"],
        persisted=True,
    )
    popolad.state_store.register(handle)

    result = popolad.cancel_task(
        "cursor-orphanpid0",
        daemon_started_at=daemon_started_at,
    )
    assert result["task_id"] == "cursor-orphanpid0"
    assert result["pid"] is None
    assert result["escalated_to_sigkill"] is False
    assert result["requested_signal"] == "none"
    assert result["result"] == "orphaned_by_daemon_restart"

    refreshed = persistence.repository.get(arktower_task_id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.CANCELED, (
        f"ArkTower task should be CANCELED; got {refreshed.status}"
    )

    # repository.get_history loads through Pydantic which rejects
    # 'cancel_orphan' (Trigger enum has no such value — we bypass the
    # model with raw SQL on insert per design). Tolerate the resulting
    # ValueError so we can still surface pydantic-decoded triggers in
    # the assertion message when present, but rely on raw SQL for the
    # actual contract check.
    try:
        history = persistence.repository.get_history(arktower_task_id)
        triggers = [
            ev.trigger.value if hasattr(ev.trigger, "value") else str(ev.trigger)
            for ev in history
        ]
    except ValueError:
        triggers = []
    raw_history = persistence.connection.get_connection().execute(
        "SELECT trigger, notes FROM task_history WHERE task_id = ? ORDER BY timestamp ASC",
        (arktower_task_id,),
    ).fetchall()
    raw_triggers = [row["trigger"] for row in raw_history]
    raw_notes = [row["notes"] for row in raw_history]
    assert "cancel_orphan" in raw_triggers, (
        f"Expected task_history trigger 'cancel_orphan'; "
        f"raw_triggers={raw_triggers} raw_notes={raw_notes} pydantic_triggers={triggers}"
    )
    assert "orphaned_by_daemon_restart" in raw_notes

    refreshed_handle = popolad.state_store.get("cursor-orphanpid0")
    assert refreshed_handle is not None
    assert refreshed_handle.state == TaskState.CANCELED

    events = popolad.tail_events("cursor-orphanpid0")
    canceled_events = [ev for ev in events if ev["type"] == "task.canceled"]
    assert canceled_events, f"task.canceled event missing; events={events}"
    payload = canceled_events[-1]["data"]
    assert payload.get("reason") == "orphaned_by_daemon_restart"
    assert payload.get("trigger") == "cancel_orphan"


def test_cancel_pre_orphan_race_still_errors(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """Race-window error preserved when started_at is NEWER than daemon.

    Guards against regression: legitimate dispatch-vs-spawn race must
    still raise (current daemon owns the task; orphan-reap path applies
    only to handles older than the running daemon process).
    """
    from datetime import UTC, datetime, timedelta

    from popolaloom.daemon.state import TaskHandle, TaskState

    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )
    daemon_started_at = datetime.now(UTC) - timedelta(minutes=5)
    task_started_at = datetime.now(UTC)
    popolad.state_store.register(
        TaskHandle(
            task_id="cursor-racepid0000",
            cli="cursor",
            pid=None,
            state=TaskState.RUNNING,
            started_at=task_started_at,
            event_log_path=events_dir / "cursor-racepid0000.jsonl",
            arktower_task_id=None,
            cmd=[],
            persisted=False,
        )
    )

    with pytest.raises(RuntimeError, match="race window"):
        popolad.cancel_task(
            "cursor-racepid0000",
            daemon_started_at=daemon_started_at,
        )


# ── 7. v0.7.1 BUG-B: rehydrate skips pre-dispatch SUBMITTED tasks ───────


def test_rehydrate_skips_pre_dispatch_submitted(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """v0.7.1 BUG-B: tasks without popola_dispatch row become FAILED, not RUNNING.

    Reproduces the 24-ghost regression: every non-terminal ArkTower task
    used to become TaskState.RUNNING in StateStore on rehydrate, even
    when dispatch crashed before populating popola_dispatch. Now those
    rows must be marked failed with error='spawn_aborted_pre_dispatch',
    must NOT appear in the in-memory state, and must emit a
    popolad.spawn_aborted forensic event.
    """
    import asyncio

    from popolaloom._vendored.arktower.core.models import (
        TaskCreate,
        TaskStatus,
    )

    async def _seed() -> tuple[str, str]:
        spawned = await persistence.task_service.create_task(
            TaskCreate(
                title="[cursor] spawned",
                description="had popola_dispatch row",
                parameters={
                    "popola_task_id": "cursor-spawnedpid0",
                    "cli": "cursor",
                    "cmd": ["echo", "spawned"],
                },
                kind="popola.dispatch",
                preferred_agent_type="cursor",
            )
        )
        aborted = await persistence.task_service.create_task(
            TaskCreate(
                title="[cursor] pre-dispatch",
                description="dispatch crashed before spawn",
                parameters={
                    "popola_task_id": "cursor-abortedpid0",
                    "cli": "cursor",
                    "cmd": ["echo", "aborted"],
                },
                kind="popola.dispatch",
                preferred_agent_type="cursor",
            )
        )
        return spawned.id, aborted.id

    spawned_ark_id, aborted_ark_id = asyncio.run(_seed())

    conn = persistence.connection.get_connection()
    conn.execute(
        """INSERT INTO popola_dispatch (dispatch_id, task_id, runtime, supervisor)
           VALUES (?, ?, 'popen', 'in-process')""",
        ("dispatch-spawned-0001", spawned_ark_id),
    )
    conn.commit()

    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )

    rehydrated = popolad.rehydrate_from_persistence()
    assert rehydrated == 1, (
        f"Expected only the spawned task to rehydrate; got {rehydrated}"
    )

    active = {item["task_id"] for item in popolad.list_active()}
    assert active == {"cursor-spawnedpid0"}, (
        f"Pre-dispatch orphan must not appear in StateStore; got {active}"
    )

    aborted_refreshed = persistence.repository.get(aborted_ark_id)
    assert aborted_refreshed is not None
    assert aborted_refreshed.status == TaskStatus.FAILED, (
        f"Pre-dispatch orphan should be FAILED in ArkTower; "
        f"got {aborted_refreshed.status}"
    )
    assert aborted_refreshed.error == "spawn_aborted_pre_dispatch", (
        f"error column should pinpoint the cause; "
        f"got error={aborted_refreshed.error!r}"
    )

    spawned_refreshed = persistence.repository.get(spawned_ark_id)
    assert spawned_refreshed is not None
    assert spawned_refreshed.status == TaskStatus.SUBMITTED, (
        "Tasks WITH a popola_dispatch row must be left untouched by the "
        f"rehydrate path; got {spawned_refreshed.status}"
    )

    aborted_log = events_dir / "cursor-abortedpid0.jsonl"
    assert aborted_log.exists(), "popolad.spawn_aborted event log should be written"
    raw_lines = aborted_log.read_text(encoding="utf-8").splitlines()
    assert any('"popolad.spawn_aborted"' in line for line in raw_lines), (
        f"popolad.spawn_aborted not emitted; lines={raw_lines}"
    )


# ── 8. v0.7.1 BUG-B prerequisite: dispatch populates popola_dispatch ────


def test_dispatch_populates_popola_dispatch_row(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """End-to-end: ``Popolad.dispatch_task`` writes a ``popola_dispatch`` row.

    Without this the BUG-B rehydrate heuristic (item #5 in
    feedback_for_v0.7.0.md) is unsound: every legitimately spawned
    task would be flagged as ``spawn_aborted`` on the next daemon
    restart because production never inserted into the table that
    has lived in the schema since v0.2.0 migration 005. NFR-8
    (tests/matrix/nfr/test_nfr_8_recovery_rate.py — full restart
    recovery) regresses to 0% recovery without the insert; this
    unit-level test gives a fast default-lane signal too.
    """
    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )

    task_id = popolad.dispatch_task(cli="cursor", prompt="dispatch row probe")
    status = popolad.get_status(task_id)
    arktower_task_id = status["arktower_task_id"]
    assert arktower_task_id is not None, (
        f"Test prerequisite: ArkTower id must be populated; got {status}"
    )

    conn = persistence.connection.get_connection()
    rows = conn.execute(
        "SELECT dispatch_id, task_id, runtime, supervisor "
        "FROM popola_dispatch WHERE task_id = ?",
        (arktower_task_id,),
    ).fetchall()
    assert len(rows) == 1, (
        f"Expected exactly 1 popola_dispatch row for ArkTower id "
        f"{arktower_task_id}; got {len(rows)}: {rows}"
    )
    row = rows[0]
    assert row["task_id"] == arktower_task_id
    assert row["runtime"] == "popen"
    assert row["supervisor"] == "in-process"
    assert row["dispatch_id"], "dispatch_id should be a non-empty primary key"


def test_record_popola_dispatch_is_idempotent(
    persistence: TaskPersistence,
    tmp_path: Path,
) -> None:
    """``_record_popola_dispatch`` uses INSERT OR IGNORE — repeat calls do not error.

    Defensive contract: if a future code path (e.g. a graph node retry
    or a recovery flow) re-records dispatch metadata for the same
    ArkTower task, we must not raise on UNIQUE conflict. The helper
    is also a no-op when persistence or the ArkTower id is unset
    (e.g. dispatch ran with persistence disabled).
    """
    events_dir = tmp_path / "events"
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_noop_adapter,
        persistence=persistence,
        use_graph=False,
    )

    popolad._record_popola_dispatch(None)

    fake_id = "ark-task-fake-0001"
    popolad._record_popola_dispatch(fake_id)
    popolad._record_popola_dispatch(fake_id)
    popolad._record_popola_dispatch(fake_id, runtime="popen", supervisor="in-process")

    conn = persistence.connection.get_connection()
    rows = conn.execute(
        "SELECT 1 FROM popola_dispatch WHERE task_id = ?",
        (fake_id,),
    ).fetchall()
    assert len(rows) == 1, (
        f"INSERT OR IGNORE should keep exactly 1 row across repeat calls; "
        f"got {len(rows)} rows"
    )
