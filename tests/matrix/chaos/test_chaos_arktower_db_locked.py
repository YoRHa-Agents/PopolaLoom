"""C8 — SQLite OperationalError "database is locked" → clear error / retry.

Per testing-matrix.md §10 #2 + #7.  When ArkTower's SQLite WAL hits a
busy lock, ``TaskService.create_task`` should propagate
``OperationalError`` rather than silently swallow it.  PopolaLoom's
``_maybe_create_arktower_task`` wraps the call in a try/except that
returns ``(None, False)`` and logs — so the dispatch is marked
``persisted=False`` and the operator sees an error log.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from popolaloom.daemon.server import Popolad


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["python", "-c", "print('chaos C8')"]


def test_chaos_arktower_database_locked_returns_none_persisted_false(
    tmp_path: Path,
    mocker,
    caplog,
) -> None:
    """``OperationalError("database is locked")`` → (None, False) + log."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter)

    fake_persistence = mocker.MagicMock()
    fake_persistence.task_service = mocker.MagicMock()

    async def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    fake_persistence.task_service.create_task = boom
    popolad._persistence = fake_persistence

    with caplog.at_level("ERROR"):
        ark_id, persisted = popolad._maybe_create_arktower_task(
            task_id="cursor-c8",
            cli="cursor",
            prompt="locked-db scenario",
            cmd=["echo", "x"],
        )

    assert ark_id is None
    assert persisted is False
    error_records = [r for r in caplog.records if "create_task failed" in r.message]
    assert error_records, "ArkTower OperationalError must be logged (No Silent Failures)"


def test_chaos_arktower_locked_dispatch_creates_unpersisted_handle(
    tmp_path: Path,
    mocker,
) -> None:
    """End-to-end: dispatch with locked DB → handle.persisted=False, no exception."""
    def _adapter(cli, prompt, cwd, extra=None):
        return ["python", "-c", "import sys; sys.exit(0)"]

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_adapter,
        use_graph=False,
    )

    fake_persistence = mocker.MagicMock()
    fake_persistence.task_service = mocker.MagicMock()

    async def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    fake_persistence.task_service.create_task = boom
    popolad._persistence = fake_persistence

    task_id = popolad.dispatch_task("cursor", "locked db dispatch", cwd=None)
    handle = popolad.state_store.get(task_id)
    assert handle is not None
    assert handle.persisted is False
    assert handle.arktower_task_id is None
