"""Tier 2 / Coverage — :mod:`popolaloom.daemon.main` pure helper tests.

The async ``main()`` entry point spawns uvicorn (T3 coverage); these
unit-level tests exercise the synchronous helpers that are individually
easy to test:

- ``get_popola_home`` honors ``$POPOLA_HOME`` env var + creates the dir.
- ``get_socket_path`` / ``get_pid_path`` / ``get_events_dir`` derive from
  ``get_popola_home``.
- ``write_pid_file`` writes the current pid; ``remove_pid_file`` is
  idempotent (no-op when missing).
- ``remove_socket`` is idempotent.
- ``_configure_logging`` installs exactly one handler on the root logger.
- ``_build_persistence_safely`` returns None when ArkTower import fails
  (mocked).
- The module-level ``__getattr__`` exposes ``Popolad`` + ``create_app``
  to debug imports.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from popolaloom.daemon import main as daemon_main


def test_get_popola_home_uses_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$POPOLA_HOME`` is honored verbatim and the dir is created."""
    target = tmp_path / "custom_home"
    monkeypatch.setenv("POPOLA_HOME", str(target))
    home = daemon_main.get_popola_home()
    assert home == target.resolve()
    assert home.exists()


def test_get_popola_home_default_is_user_dot_popola(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``$POPOLA_HOME`` is unset, default is ``~/.popola``."""
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = daemon_main.get_popola_home()
    assert home == tmp_path / ".popola"


def test_get_socket_pid_events_paths_match_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 3 path helpers derive their value from ``get_popola_home()``."""
    home = tmp_path / "h2"
    monkeypatch.setenv("POPOLA_HOME", str(home))
    assert daemon_main.get_socket_path() == home.resolve() / "popolad.sock"
    assert daemon_main.get_pid_path() == home.resolve() / "popolad.pid"
    assert daemon_main.get_events_dir() == home.resolve() / "events"
    assert (home.resolve() / "events").exists()


def test_write_and_remove_pid_file_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``write_pid_file`` writes pid; ``remove_pid_file`` removes it; both are idempotent."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path / "ph"))
    pid_path = daemon_main.write_pid_file()
    assert pid_path.exists()
    assert pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())

    daemon_main.remove_pid_file(pid_path)
    assert not pid_path.exists()
    daemon_main.remove_pid_file(pid_path)


def test_remove_socket_is_idempotent(tmp_path: Path) -> None:
    """``remove_socket`` is a no-op when the file is absent."""
    sock = tmp_path / "no_such_socket"
    daemon_main.remove_socket(sock)
    sock.write_text("x")
    daemon_main.remove_socket(sock)
    assert not sock.exists()


def test_configure_logging_installs_single_handler() -> None:
    """``_configure_logging`` clears + installs one handler on root logger."""
    root = logging.getLogger()
    daemon_main._configure_logging(level=logging.INFO)
    assert len(root.handlers) == 1
    assert root.level == logging.INFO


def test_build_persistence_safely_returns_none_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``make_persistence`` raises, ``_build_persistence_safely`` returns None."""
    import popolaloom.daemon.repository as repo

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated failure for test")

    monkeypatch.setattr(repo, "make_persistence", boom)
    result = daemon_main._build_persistence_safely()
    assert result is None


def test_module_getattr_exposes_popolad_and_create_app() -> None:
    """``daemon.main.Popolad`` and ``daemon.main.create_app`` are exposed via __getattr__."""
    popolad_cls = daemon_main.Popolad  # noqa: N806 — class symbol via __getattr__
    create_app_fn = daemon_main.create_app
    from popolaloom.daemon.server import Popolad as RealPopolad

    assert popolad_cls is RealPopolad
    assert callable(create_app_fn)


def test_module_getattr_unknown_raises_attribute_error() -> None:
    """An unknown attribute raises AttributeError (not KeyError or silent None)."""
    with pytest.raises(AttributeError):
        daemon_main.does_not_exist  # noqa: B018
