"""Carry-over migration tests for the vendored ArkTower subset (v0.5.5 L5.D).

PopolaLoom v0.5.0 vendored ArkTower under
``src/popolaloom/_vendored/arktower/`` (per
[`VENDORING.md`](../VENDORING.md), Stage S1 / D5.7 LOCKED Path B). The
prior loop plan listed a "vendored ArkTower migration test suite" as
a deferred item — this file closes it.

Suite shape (4 cases):

1. The vendored package imports cleanly (root + 4 subpackages).
2. The two PopolaLoom-owned migrations
   (``migrations/005_popolaloom_extensions.sql`` +
   ``migrations/006_popola_hitl.sql``) exist and contain at least one
   ``CREATE TABLE`` statement (a basic "is valid SQL" smoke check).
3. The vendored :class:`MigrationRunner` can apply the four
   ArkTower migrations against an in-memory SQLite DB; the
   ``schema_version`` table gets populated with versions 1..4.
4. The ``POPOLA_ARKTOWER_MIGRATIONS_DIR`` env var override resolves
   to the requested directory when the path is valid; falls back to
   the vendored sibling when unset.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ── 1. import the vendored package ───────────────────────────────────────


def test_vendored_arktower_imports_cleanly() -> None:
    """Vendored package + 4 subpackages all import without error.

    Mutating the vendored ``__init__.py`` exports (or breaking the
    package data layout) would surface here as an ImportError.
    """
    import popolaloom._vendored.arktower as arktower_pkg

    assert arktower_pkg.__vendored_from__.startswith(
        "https://github.com/YoRHa-Agents/ArkTower"
    )
    assert arktower_pkg.__vendored_version__

    from popolaloom._vendored.arktower.cli.deps import migrations_dir
    from popolaloom._vendored.arktower.core.event_bus import EventBus
    from popolaloom._vendored.arktower.core.task_service import TaskService
    from popolaloom._vendored.arktower.store import (
        DatabaseConnection,
        MigrationRunner,
        SqliteTaskRepository,
        TaskRepository,
    )

    assert callable(migrations_dir)
    assert EventBus is not None
    assert TaskService is not None
    assert DatabaseConnection is not None
    assert MigrationRunner is not None
    assert SqliteTaskRepository is not None
    assert TaskRepository is not None


# ── 2. PopolaLoom-owned migrations exist + are syntactically valid SQL ────


@pytest.fixture
def repo_root() -> Path:
    """Resolve the repo root so the test works in editable + wheel installs."""
    candidate = Path(__file__).resolve().parents[1]
    if (candidate / "migrations").is_dir():
        return candidate
    raise pytest.skip(
        "test only runs in an editable install where migrations/ is on disk"
    )


def test_popolaloom_owned_migrations_exist_and_create_tables(repo_root: Path) -> None:
    """``005_popolaloom_extensions.sql`` + ``006_popola_hitl.sql`` are present.

    Each file must contain at least one ``CREATE TABLE`` statement
    (basic syntax smoke check); the file must be apply-able against an
    in-memory SQLite DB without raising.
    """
    five = repo_root / "migrations" / "005_popolaloom_extensions.sql"
    six = repo_root / "migrations" / "006_popola_hitl.sql"
    assert five.is_file(), f"missing {five}"
    assert six.is_file(), f"missing {six}"

    for path in (five, six):
        body = path.read_text(encoding="utf-8")
        assert "CREATE TABLE" in body, (
            f"{path.name} missing a CREATE TABLE statement (basic SQL smoke check)"
        )

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(five.read_text(encoding="utf-8"))
        conn.executescript(six.read_text(encoding="utf-8"))
        conn.commit()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    assert "popola_dispatch" in tables, (
        "005_popolaloom_extensions.sql should create popola_dispatch"
    )
    assert "popola_hitl" in tables, "006_popola_hitl.sql should create popola_hitl"


# ── 3. MigrationRunner applies the 4 ArkTower migrations end-to-end ──────


def test_vendored_migration_runner_applies_arktower_migrations(
    tmp_path: Path,
) -> None:
    """``MigrationRunner.run_migrations`` applies the 4 vendored ArkTower SQLs.

    After the run, ``schema_version`` should have rows for versions
    1..4 (matching the four files under
    ``src/popolaloom/_vendored/arktower/migrations/``).
    """
    from popolaloom._vendored.arktower.cli.deps import migrations_dir
    from popolaloom._vendored.arktower.store import (
        DatabaseConnection,
        MigrationRunner,
    )

    db_path = tmp_path / "scratch.db"
    conn = DatabaseConnection(db_path=str(db_path))
    conn.connect()
    try:
        runner = MigrationRunner(conn, migrations_dir())
        applied = runner.run_migrations()
        assert applied >= 4, f"expected ≥ 4 ArkTower migrations applied, got {applied}"
        assert runner.get_current_version() >= 4

        cur = conn.get_connection().execute(
            "SELECT version FROM schema_version ORDER BY version"
        )
        versions = [row["version"] for row in cur.fetchall()]
        for required in (1, 2, 3, 4):
            assert required in versions, (
                f"schema_version missing version {required}; saw {versions}"
            )

        second_apply = runner.run_migrations()
        assert second_apply == 0, "second run_migrations() must be a no-op"
    finally:
        conn.close()


# ── 4. POPOLA_ARKTOWER_MIGRATIONS_DIR env-var override ───────────────────


def test_arktower_migrations_dir_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POPOLA_ARKTOWER_MIGRATIONS_DIR=<path>`` makes the loader prefer that path.

    The repository helper ``_arktower_migrations_dir`` (in
    ``daemon/repository.py``) honours the env var when the path is a
    real directory; bogus paths log a warning and fall back to the
    vendored sibling.
    """
    from popolaloom.daemon.repository import (
        _ARKTOWER_MIGRATIONS_ENV,
        _arktower_migrations_dir,
    )

    custom = tmp_path / "custom-arktower-migrations"
    custom.mkdir()
    placeholder = custom / "001_custom.sql"
    placeholder.write_text("-- intentionally empty\n", encoding="utf-8")

    monkeypatch.setenv(_ARKTOWER_MIGRATIONS_ENV, str(custom))
    assert _arktower_migrations_dir() == custom

    bogus = tmp_path / "definitely-not-here"
    monkeypatch.setenv(_ARKTOWER_MIGRATIONS_ENV, str(bogus))
    fallback = _arktower_migrations_dir()
    assert fallback is not None
    assert fallback != bogus
    assert (fallback / "001_initial_schema.sql").is_file()

    monkeypatch.delenv(_ARKTOWER_MIGRATIONS_ENV, raising=False)
    no_env = _arktower_migrations_dir()
    assert no_env is not None
    assert (no_env / "001_initial_schema.sql").is_file()
