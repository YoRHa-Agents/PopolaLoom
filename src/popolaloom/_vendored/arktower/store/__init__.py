"""Persistence layer: SQLite storage with WAL mode, FTS5, and JSON1.

Vendored subset for PopolaLoom — see ``VENDORING.md`` at the repo root.
"""

from popolaloom._vendored.arktower.store.connection import DatabaseConnection
from popolaloom._vendored.arktower.store.migration import MigrationRunner
from popolaloom._vendored.arktower.store.repository import TaskRepository
from popolaloom._vendored.arktower.store.sqlite_repository import SqliteTaskRepository

__all__ = [
    "DatabaseConnection",
    "MigrationRunner",
    "SqliteTaskRepository",
    "TaskRepository",
]
