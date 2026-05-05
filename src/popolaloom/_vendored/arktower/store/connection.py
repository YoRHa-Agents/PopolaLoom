"""SQLite connection management with WAL mode, foreign keys, and JSON1 support.

Vendored from ArkTower @ commit 467a087 (arktower/store/connection.py).
Do not edit manually — refresh per VENDORING.md at the repo root.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)


class DatabaseConnection:
    """Manages a single SQLite connection with recommended pragmas enabled.

    Supports both file-based and in-memory databases.  For ``:memory:``
    databases, WAL mode is skipped (it requires a file-backed journal).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._connection: sqlite3.Connection | None = None

    @property
    def db_path(self) -> str:
        return self._db_path

    def connect(self) -> sqlite3.Connection:
        """Open a connection and apply performance / safety pragmas."""
        if self._connection is not None:
            return self._connection

        self._connection = sqlite3.connect(self._db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row

        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA cache_size = -64000")

        if self._db_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")

        logger.debug("Connected to SQLite database at %s", self._db_path)
        return self._connection

    def close(self) -> None:
        """Close the connection if open."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            logger.debug("Closed SQLite connection for %s", self._db_path)

    def get_connection(self) -> sqlite3.Connection:
        """Return the existing connection or lazily create one."""
        if self._connection is None:
            return self.connect()
        return self._connection

    def __enter__(self) -> DatabaseConnection:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
