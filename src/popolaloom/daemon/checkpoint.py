"""SqliteSaver lifecycle wrapper for popolad (v0.2.0 Stage B B2).

LangGraph 1.x exposes :class:`SqliteSaver` either via ``from_conn_string``
(a context manager that yields a fresh saver each call) or via the direct
``SqliteSaver(sqlite3.Connection)`` constructor.

For Popolad we need a *long-lived* saver whose underlying ``sqlite3.Connection``
outlives many graph invokes (one per dispatch). The context-manager form is
the wrong shape for that pattern, so we construct ``SqliteSaver`` directly,
own the connection, run ``saver.setup()`` once, and provide a small
:class:`CheckpointerHandle` for explicit lifecycle control.

ADR-0002 §2.1 / §2.5 contract:

- DB path defaults to ``~/.popola/state.sqlite`` (per spec §10 canonical paths).
- ``thread_id`` corresponds to the popola ``task_id``; LangGraph manages the
  ``checkpoint_ns`` namespace itself for nested subgraphs.
- ``check_same_thread=False``: the daemon spawns a background thread per
  dispatch which runs ``graph.invoke``; without this the second dispatch
  would crash on the shared connection.
- Phase 2 may swap to ``PostgresSaver`` for cross-machine deploy
  (ADR-0002 §3.2 — abstraction is a one-line change).

Workspace rules honored:

- *No Silent Failures*: directory mkdir errors surface; ``setup()`` errors
  propagate; ``close()`` is best-effort but logs.
- *Mandatory Verification*: see ``tests/test_graph.py`` test_interrupt_resume +
  test_thread_id_isolation.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import TracebackType

from langgraph.checkpoint.sqlite import SqliteSaver

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    """``~/.popola/state.sqlite`` per spec §10 canonical paths."""
    return Path.home() / ".popola" / "state.sqlite"


def make_checkpointer(db_path: Path | None = None) -> SqliteSaver:
    """Construct + initialise a :class:`SqliteSaver` against ``db_path``.

    Args:
        db_path: SQLite file path; default ``~/.popola/state.sqlite``.
            Parent directory is created with ``parents=True``.

    Returns:
        A live :class:`SqliteSaver` bound to a ``sqlite3.Connection`` with
        ``check_same_thread=False``.

    Caveat:
        The returned saver owns its connection internally; the caller does
        **not** get a handle to the conn. For lifecycle management (close
        the conn at daemon shutdown), prefer :class:`CheckpointerHandle`.
        For one-off use (tests, scripts) the conn will be closed when the
        process exits — sqlite3 handles that gracefully.
    """
    db_path = db_path or _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


class CheckpointerHandle:
    """Context-manager owning the ``SqliteSaver`` + ``sqlite3.Connection``.

    Daemons should construct one at startup and ``close()`` it on shutdown
    so the SQLite write-ahead log is flushed cleanly.

    Usage (as context manager)::

        with CheckpointerHandle() as saver:
            graph = build_main_graph(checkpointer=saver, callbacks=...)
            graph.invoke(initial_state, config={"configurable": {"thread_id": tid}})

    Usage (manual)::

        handle = CheckpointerHandle(my_db_path)
        handle.open()
        saver = handle.saver
        # ... use saver ...
        handle.close()

    Attributes:
        db_path: The resolved SQLite path (default ``~/.popola/state.sqlite``).
        saver:   The live :class:`SqliteSaver` (``None`` before ``open()`` /
                 after ``close()``).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path: Path = db_path or _default_db_path()
        self._conn: sqlite3.Connection | None = None
        self._saver: SqliteSaver | None = None

    @property
    def saver(self) -> SqliteSaver:
        """Return the open saver; raise if the handle is not opened."""
        if self._saver is None:
            raise RuntimeError(
                "CheckpointerHandle not opened; call open() or use as context manager"
            )
        return self._saver

    def open(self) -> SqliteSaver:
        """Create the parent dir, open conn, build saver, run setup()."""
        if self._saver is not None:
            return self._saver
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._saver = SqliteSaver(self._conn)
        self._saver.setup()
        logger.info("CheckpointerHandle opened: %s", self.db_path)
        return self._saver

    def close(self) -> None:
        """Close the underlying connection (best-effort, logs on error).

        Per workspace rule "No Silent Failures": close errors are logged
        but do not raise — caller is shutting down anyway and re-raising
        would mask the original shutdown reason.
        """
        if self._conn is None:
            return
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            logger.warning(
                "Error closing checkpointer conn at %s: %s", self.db_path, exc
            )
        finally:
            self._conn = None
            self._saver = None

    def __enter__(self) -> SqliteSaver:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "CheckpointerHandle",
    "make_checkpointer",
]
