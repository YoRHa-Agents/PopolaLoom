"""Schema migration runner for SQLite.

Reads ``.sql`` files from a migrations directory, tracks applied versions
in a ``schema_version`` table, and runs each pending migration inside a
transaction.

Vendored from ArkTower @ commit 467a087 (arktower/store/migration.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from popolaloom._vendored.arktower.store.connection import DatabaseConnection

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^(\d+)")


def _extract_version(path: Path) -> int:
    """Pull the leading integer from a migration filename (e.g. ``001_init.sql`` → 1)."""
    match = _VERSION_RE.match(path.stem)
    if match is None:
        raise ValueError(f"Migration filename must start with a number: {path.name}")
    return int(match.group(1))


class MigrationError(Exception):
    """Raised when a migration fails to apply."""

    def __init__(self, version: int, name: str, cause: Exception) -> None:
        self.version = version
        self.name = name
        self.cause = cause
        super().__init__(f"Migration {version} ({name}) failed: {cause}")


class MigrationRunner:
    """Applies SQL migration files in order and records each in ``schema_version``.

    Parameters
    ----------
    connection:
        An active :class:`DatabaseConnection` instance.
    migrations_dir:
        Directory containing ``*.sql`` migration files whose names start
        with a numeric version prefix (e.g. ``001_initial_schema.sql``).
    """

    _BOOTSTRAP_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);"""

    def __init__(self, connection: DatabaseConnection, migrations_dir: Path) -> None:
        self._db = connection
        self._migrations_dir = migrations_dir
        self._ensure_table()

    def _ensure_table(self) -> None:
        conn = self._db.get_connection()
        conn.execute(self._BOOTSTRAP_SQL)
        conn.commit()

    def get_current_version(self) -> int:
        row = self._db.get_connection().execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        return row["v"] if row and row["v"] is not None else 0

    def get_pending_migrations(self) -> list[Path]:
        current = self.get_current_version()
        if not self._migrations_dir.is_dir():
            return []
        candidates = sorted(self._migrations_dir.glob("*.sql"), key=_extract_version)
        return [p for p in candidates if _extract_version(p) > current]

    def run_migrations(self) -> int:
        """Apply all pending migrations and return the count of applied ones."""
        pending = self.get_pending_migrations()
        if not pending:
            logger.info("No pending migrations.")
            return 0

        conn = self._db.get_connection()
        applied = 0

        for migration_path in pending:
            version = _extract_version(migration_path)
            name = migration_path.stem
            sql = migration_path.read_text(encoding="utf-8")

            logger.info("Applying migration %03d: %s …", version, name)
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                    (version, name),
                )
                conn.commit()
                applied += 1
            except Exception as exc:
                logger.error("Migration %03d failed: %s", version, exc)
                raise MigrationError(version, name, exc) from exc

        logger.info(
            "Applied %d migration(s). Current version: %d",
            applied,
            self.get_current_version(),
        )
        return applied
