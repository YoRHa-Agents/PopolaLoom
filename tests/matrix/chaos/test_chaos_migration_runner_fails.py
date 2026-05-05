"""C9 — migration runner fails → daemon cannot start (clear error).

Per testing-matrix.md §10 #8 / #12.  When the ArkTower migration
runner raises (e.g. SQL syntax error), :func:`make_persistence` MUST
propagate the error rather than silently swallow it — otherwise the
daemon would boot with a half-applied schema and corrupt subsequent
writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import popolaloom.daemon.repository as repo


def test_chaos_migration_runner_raises_propagates(
    tmp_path: Path,
    mocker,
) -> None:
    """``MigrationRunner.run_migrations`` raising → propagates from make_persistence."""
    db_path = tmp_path / "arktower.db"
    fake_runner = mocker.MagicMock()
    fake_runner.run_migrations.side_effect = RuntimeError(
        "simulated SQL syntax error in 005_popolaloom_extensions.sql"
    )

    def _fake_ctor(*args, **kwargs):
        return fake_runner

    mocker.patch.object(repo, "MigrationRunner", side_effect=_fake_ctor)

    with pytest.raises(RuntimeError, match="simulated SQL syntax error"):
        repo.make_persistence(
            db_path=db_path,
            arktower_migrations_dir=tmp_path,
            popolaloom_migrations_dir=tmp_path,
        )


def test_chaos_migration_no_silent_swallow_in_main_helper(
    tmp_path: Path,
    mocker,
) -> None:
    """``daemon.main._build_persistence_safely`` returns None on failure + logs.

    Verifies the daemon's defensive wrapper specifically: when
    persistence init raises, _build_persistence_safely catches and
    returns None (so the daemon can boot in degraded mode), but it
    MUST log the exception so operators see what's wrong.
    """
    import popolaloom.daemon.main as daemon_main

    mocker.patch.object(
        daemon_main,
        "_build_persistence_safely",
        wraps=daemon_main._build_persistence_safely,
    )
    mocker.patch(
        "popolaloom.daemon.repository.make_persistence",
        side_effect=RuntimeError("migration disaster"),
    )

    import logging
    logger = logging.getLogger("popolaloom.daemon")
    handler_records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            handler_records.append(record)

    h = _Handler(level=logging.WARNING)
    logger.addHandler(h)
    try:
        result = daemon_main._build_persistence_safely()
    finally:
        logger.removeHandler(h)

    assert result is None
    assert any(
        "Failed to build TaskPersistence" in r.message
        or "migration disaster" in str(r.exc_info or "")
        for r in handler_records
    ), (
        "_build_persistence_safely must log when make_persistence raises "
        f"(records: {[r.message for r in handler_records]})"
    )
