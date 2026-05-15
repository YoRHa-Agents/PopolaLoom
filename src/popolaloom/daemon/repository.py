"""ArkTower task pool injection — v0.2.0 Stage C C1.

This module is the **only** place where popolad reaches into the ArkTower
SQLite task pool.  It assembles the 3-component stack
(:class:`DatabaseConnection` + :class:`SqliteTaskRepository` +
:class:`TaskService`) plus the in-process :class:`EventBus` that
:mod:`popolaloom.daemon.event_bus` will subscribe to.

Why a dataclass instead of returning the ``TaskService`` directly?

- The :class:`Popolad` instance needs to hand the ``EventBus`` to the
  :class:`PopolaEventBusBridge` and call ``connection.close()`` on
  shutdown; keeping all 4 references in one struct keeps the wiring
  explicit (DIP — :doc:`09-iter1-self-eval` §6.1).
- ``repository`` is exposed for tests that want to assert on
  ``repository.list(...)`` directly without going through the service
  layer (e.g. ``tests/test_repository.py``).

Spec / ADR references:

- ADR-0001 §2.1 default db path ``~/.arktower/arktower.db``;
- ADR-0001 §2.2 PopolaLoom owns ``005_popolaloom_extensions.sql`` (this
  module wires the runner so 001-004 from ArkTower + 005 from PopolaLoom
  apply in order);
- spec.md §5.1 ArkTower 依赖契约 — closed by Stage C (R-004 P0).

Migration discovery
-------------------

Since v0.5.0 Stage S1 ArkTower's migrations are vendored under
``popolaloom._vendored.arktower.migrations/`` and shipped in the wheel.
We try, in order:

1. The ``arktower_migrations_dir=`` argument (explicit caller override);
2. ``$POPOLA_ARKTOWER_MIGRATIONS_DIR`` env var (operator override);
3. :func:`popolaloom._vendored.arktower.cli.deps.migrations_dir` (the
   vendored copy — works for both editable installs and wheel installs
   because the path resolves relative to ``cli/deps.py`` at runtime);
4. The legacy reference path ``/home/agent/reference/ArkTower/migrations``
   used by v0.2.0 development.

v0.6.1 (CI hotfix) refinement: an explicit
``arktower_migrations_dir=`` whose Path does **not** exist falls
through to step 3 instead of feeding a phantom dir into
``MigrationRunner``. This unblocks ``tests/test_repository.py`` on
GitHub-hosted runners that lack the legacy ``/home/agent/reference``
clone — the test fixture passes the legacy path explicitly, but with
the fallback the vendored copy is picked up automatically.

If none of the candidates resolve, ``MigrationRunner.run_migrations``
no-ops on the missing directory (see
``arktower.store.migration:get_pending_migrations``) — the
popola_dispatch table will still be created from PopolaLoom's own
005 migration, but the core ArkTower tables won't exist and the
next ``task_service.create_task`` will raise.  We log a clear
warning so the operator can fix it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from popolaloom._vendored.arktower.core.event_bus import EventBus
from popolaloom._vendored.arktower.core.task_service import TaskService
from popolaloom._vendored.arktower.store.connection import DatabaseConnection
from popolaloom._vendored.arktower.store.migration import MigrationRunner
from popolaloom._vendored.arktower.store.sqlite_repository import (
    SqliteTaskRepository,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_REQUIRED_POPOLALOOM_MIGRATIONS: tuple[str, ...] = (
    "005_popolaloom_extensions.sql",
    "006_popola_hitl.sql",
    "007_popola_hitl_metadata.sql",
)


class MigrationsMissingError(RuntimeError):
    """Raised when packaged PopolaLoom SQL migrations are missing."""

    hint_zh: str = (
        "Cloud HITL 表 popola_hitl 未初始化，wheel 安装下默认会出此错；"
        "请重装 popolaloom>=1.1.1 或参考 popola doctor 输出"
    )
    hint_en: str = (
        "Cloud HITL table popola_hitl is not initialised. Wheel installs missing "
        "migrations 005/006/007 cannot start; reinstall popolaloom>=1.1.1 or run "
        "popola doctor"
    )

    def __init__(self, missing: tuple[str, ...], migrations_dir: Path) -> None:
        self.missing = missing
        self.migrations_dir = migrations_dir
        super().__init__(
            "missing packaged PopolaLoom migrations "
            f"{', '.join(missing)} under {migrations_dir}. {self.hint_en}"
        )


_ARKTOWER_MIGRATIONS_ENV: str = "POPOLA_ARKTOWER_MIGRATIONS_DIR"
"""Env var override for ArkTower's migrations directory.

Set to the absolute path of ``<arktower-repo>/migrations`` when arktower
is installed as a non-editable wheel.  Documented for operators in
:doc:`v0.2.0-plan` §4 Stage C C3."""

_ARKTOWER_MIGRATIONS_FALLBACK: Path = Path("/home/agent/reference/ArkTower/migrations")
"""Fallback path used during v0.2.0 development.

This pin is acceptable for v0.2.0 because the entire reference setup is
documented in :doc:`spec` §10 canonical paths.  v0.3.0 will switch to a
proper data-files install (ArkTower needs to ship its migrations in the
wheel — out of scope for PopolaLoom)."""


def _default_db_path() -> Path:
    """Default ArkTower DB path per ADR-0001 §2.1.

    Honors ``$ARKTOWER_HOME`` (set by ArkTower's own config) for
    consistency, otherwise falls back to ``~/.arktower/arktower.db``.
    """
    home = os.environ.get("ARKTOWER_HOME")
    if home:
        return Path(home).expanduser() / "arktower.db"
    return Path.home() / ".arktower" / "arktower.db"


def _arktower_migrations_dir() -> Path | None:
    """Locate ArkTower's migrations directory.

    Returns ``None`` if no candidate path exists; callers should log a
    warning rather than raise (the popolaloom 005 migration can still be
    applied alone, even if it leaves the schema half-baked — operator
    needs to fix the install).
    """
    override = os.environ.get(_ARKTOWER_MIGRATIONS_ENV)
    if override:
        path = Path(override).expanduser()
        if path.is_dir():
            return path
        logger.warning(
            "%s=%s does not point to an existing directory; ignoring",
            _ARKTOWER_MIGRATIONS_ENV,
            override,
        )

    try:
        from popolaloom._vendored.arktower.cli.deps import (
            migrations_dir as _ark_migrations_dir,
        )

        candidate: Path = _ark_migrations_dir()
        if candidate.is_dir():
            return candidate
    except Exception:
        logger.debug(
            "popolaloom._vendored.arktower.cli.deps.migrations_dir() unavailable",
            exc_info=True,
        )

    if _ARKTOWER_MIGRATIONS_FALLBACK.is_dir():
        return _ARKTOWER_MIGRATIONS_FALLBACK

    return None


def _popolaloom_migrations_dir() -> Path:
    """Return the on-disk path of PopolaLoom's own migrations directory.

    The SQL files live under ``popolaloom.migrations`` so editable and wheel
    installs resolve through the same package-resource path.
    """
    return Path(str(resources.files("popolaloom.migrations")))


def _publish_migrations_missing_event(event_bus: EventBus, error: MigrationsMissingError) -> None:
    """Emit the FAIL-loud migration event before daemon startup aborts."""
    payload = {
        "missing": list(error.missing),
        "migrations_dir": str(error.migrations_dir),
        "hint_zh": error.hint_zh,
        "hint_en": error.hint_en,
    }
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(event_bus.publish("popolad.migrations_missing", payload))
        return

    # ``make_persistence`` is synchronous. If an embedding caller invokes it
    # from an active event loop, dispatch synchronous subscribers immediately
    # so the diagnostic event is still emitted before raising.
    subscribers = getattr(event_bus, "_subscribers", {}).get(
        "popolad.migrations_missing", []
    )
    for handler in list(subscribers):
        try:
            result = handler(payload)
            if asyncio.iscoroutine(result):
                result.close()
                logger.error(
                    "Cannot await async popolad.migrations_missing handler %r during "
                    "synchronous migration startup failure",
                    handler,
                )
        except Exception:
            logger.exception("Handler %r raised on event 'popolad.migrations_missing'", handler)


def _ensure_required_popolaloom_migrations(popola_dir: Path, event_bus: EventBus) -> None:
    """Refuse daemon startup when Cloud HITL migrations are absent."""
    missing = tuple(
        filename
        for filename in _REQUIRED_POPOLALOOM_MIGRATIONS
        if not (popola_dir / filename).is_file()
    )
    if not missing:
        return

    error = MigrationsMissingError(missing, popola_dir)
    logger.error(
        "PopolaLoom packaged migrations missing: %s; refusing to start. %s",
        ", ".join(missing),
        error.hint_en,
    )
    _publish_migrations_missing_event(event_bus, error)
    raise error


@dataclass
class TaskPersistence:
    """Bundle of ArkTower components owned by a single popolad instance.

    Attributes:
        task_service: The async-flavored :class:`TaskService` facade
            popolad calls (``create_task``, ``advance_task``, etc.).
        repository: Direct :class:`SqliteTaskRepository` handle for
            tests / diagnostics.  Production code should prefer
            :attr:`task_service` so transitions emit ``EventBus``
            notifications + audit log entries.
        connection: :class:`DatabaseConnection` for graceful shutdown
            (``connection.close()``).
        event_bus: :class:`EventBus` instance subscribed by both
            :class:`PopolaEventBusBridge` (NDJSON sink, v0.2.0) and the
            v0.3.0 Lark notifier (planned hook).
    """

    task_service: TaskService
    repository: SqliteTaskRepository
    connection: DatabaseConnection
    event_bus: EventBus

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        try:
            self.connection.close()
        except Exception:
            logger.exception("TaskPersistence.close() failed for %s", self.connection.db_path)


def make_persistence(
    db_path: Path | None = None,
    *,
    event_bus: EventBus | None = None,
    arktower_migrations_dir: Path | None = None,
    popolaloom_migrations_dir: Path | None = None,
    run_migrations: bool = True,
) -> TaskPersistence:
    """Assemble the ArkTower task-pool stack and return a :class:`TaskPersistence`.

    Args:
        db_path: SQLite file path; defaults to ``~/.arktower/arktower.db``
            per ADR-0001 §2.1.  Use ``Path(":memory:")`` only at your own
            risk — ArkTower disables WAL on ``:memory:`` (see
            :class:`DatabaseConnection`) but multi-thread access is fragile.
            Tests prefer ``tmp_path / "arktower.db"`` for full isolation.
        event_bus: Optional pre-constructed :class:`EventBus` (lets tests
            inject a fake bus).  Defaults to a fresh :class:`EventBus`.
        arktower_migrations_dir: Override the auto-detected ArkTower
            migrations directory.  When ``None`` we try
            :func:`_arktower_migrations_dir`; missing → warning, popolaloom
            005 still applied (best-effort).
        popolaloom_migrations_dir: Override PopolaLoom's own migrations
            directory.  Defaults to ``<repo-root>/migrations``.
        run_migrations: When ``True`` (default), apply pending migrations
            from both directories at startup.  Tests that pre-seed the
            schema themselves can pass ``False``.

    Returns:
        TaskPersistence: ready-to-use 4-tuple.

    Raises:
        OSError: when ``db_path``'s parent directory cannot be created.
        Exception: ``MigrationRunner.run_migrations`` propagates as
            :class:`MigrationError` if any SQL is malformed (No Silent
            Failures rule — broken schema must surface, not silently
            half-apply).
    """
    db_path = db_path or _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = DatabaseConnection(db_path=str(db_path))
    connection.connect()

    if run_migrations:
        # v0.6.1 (CI hotfix): when the explicit ``arktower_migrations_dir``
        # does not point to an existing directory, fall through to the
        # auto-detected location instead of feeding a phantom path into
        # ``MigrationRunner`` (which silently no-ops on a missing dir, see
        # ``store/migration.py:get_pending_migrations``). The legacy v0.2.0
        # development pin ``/home/agent/reference/ArkTower/migrations``
        # used by ``tests/test_repository.py`` does not exist on GitHub-
        # hosted runners; without this fallback the four
        # ``test_repository.py`` cases failed with
        # ``sqlite3.OperationalError: no such table: tasks`` because no
        # ArkTower schema migrations were applied. ``_arktower_migrations_dir``
        # then prefers ``popolaloom._vendored.arktower.cli.deps.migrations_dir``
        # which resolves to the in-package vendored copy bundled in the
        # wheel via ``[tool.hatch.build.targets.wheel] packages``.
        ark_dir: Path | None = arktower_migrations_dir
        if ark_dir is None or not ark_dir.is_dir():
            if ark_dir is not None:
                logger.debug(
                    "arktower_migrations_dir=%s does not exist; "
                    "falling back to vendored auto-detection",
                    ark_dir,
                )
            ark_dir = _arktower_migrations_dir()
        popola_dir = popolaloom_migrations_dir or _popolaloom_migrations_dir()
        bus = event_bus if event_bus is not None else EventBus()

        if ark_dir is None:
            logger.warning(
                "Could not locate ArkTower migrations dir "
                "(set %s to override); core schema may be missing.",
                _ARKTOWER_MIGRATIONS_ENV,
            )
        else:
            ark_runner = MigrationRunner(connection, ark_dir)
            applied = ark_runner.run_migrations()
            logger.info(
                "Applied %d ArkTower migration(s) from %s (current version=%d)",
                applied,
                ark_dir,
                ark_runner.get_current_version(),
            )

        _ensure_required_popolaloom_migrations(popola_dir, bus)
        popola_runner = MigrationRunner(connection, popola_dir)
        applied = popola_runner.run_migrations()
        logger.info(
            "Applied %d PopolaLoom migration(s) from %s (current version=%d)",
            applied,
            popola_dir,
            popola_runner.get_current_version(),
        )

    repository = SqliteTaskRepository(connection)
    if not run_migrations:
        bus = event_bus if event_bus is not None else EventBus()
    task_service = TaskService(repository, bus)

    return TaskPersistence(
        task_service=task_service,
        repository=repository,
        connection=connection,
        event_bus=bus,
    )
