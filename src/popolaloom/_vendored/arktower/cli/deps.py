"""CLI dependency wiring (vendored subset — only :func:`migrations_dir`).

Upstream ``arktower.cli.deps`` also provides a ``ensure_cli_initialized`` /
``get_task_service`` / ``get_repository`` triad, but PopolaLoom does NOT
use any of them — its :mod:`popolaloom.daemon.repository` constructs the
``DatabaseConnection`` / ``MigrationRunner`` / ``SqliteTaskRepository`` /
``TaskService`` chain directly so it can supply popolaloom-specific
defaults. We therefore vendor only the path helper and keep this module
free of the upstream ``arktower.config`` dependency.
"""

from __future__ import annotations

from pathlib import Path


def migrations_dir() -> Path:
    """Directory containing ``*.sql`` migrations (vendored sibling)."""
    return Path(__file__).resolve().parent.parent / "migrations"
