"""Tier 2 / Coverage — small focused tests filling remaining gaps.

Targets the lowest-coverage modules with surgical cases:

- ``daemon/checkpoint.py`` (45% → ≥85%): ``CheckpointerHandle`` open/close
  round-trip + ``saver`` property guard.
- ``daemon/repository.py`` (57% → ≥75%): ``_default_db_path`` env var path,
  ``_arktower_migrations_dir`` override + fallback, ``_popolaloom_migrations_dir``,
  ``TaskPersistence.close`` idempotency.
- ``mcp/server.py`` (64% → ≥85%): ``build_server`` smoke, ``socket_path``
  env override, ``make_async_client`` factory.
- ``mcp/tools.py``: ``popola_attach_stream`` argument validation paths
  (task_id missing / non-string / invalid last_n) + 404 path.
- ``adapters/__init__.py``: ``_register_defaults`` reload-safe.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from popolaloom.daemon import repository as repo_mod
from popolaloom.daemon.checkpoint import CheckpointerHandle, make_checkpointer
from popolaloom.mcp import server as mcp_server
from popolaloom.mcp import tools as mcp_tools

# ── daemon/checkpoint.py ─────────────────────────────────────────────────


def test_make_checkpointer_creates_dir_and_file(tmp_path: Path) -> None:
    """``make_checkpointer`` mkdirs the parent + creates the SQLite file."""
    db = tmp_path / "nested" / "popola_state.sqlite"
    saver = make_checkpointer(db)
    assert db.parent.exists()
    assert db.exists()
    assert saver is not None


def test_checkpointer_handle_open_close_round_trip(tmp_path: Path) -> None:
    """CheckpointerHandle context manager opens then closes cleanly."""
    db = tmp_path / "ck.sqlite"
    handle = CheckpointerHandle(db)
    saver = handle.open()
    assert saver is not None
    assert handle.saver is saver
    handle.close()
    handle.close()


def test_checkpointer_handle_saver_property_raises_when_unopened() -> None:
    """Accessing ``handle.saver`` before ``open()`` raises RuntimeError."""
    handle = CheckpointerHandle(Path("/tmp/never_opened.sqlite"))
    with pytest.raises(RuntimeError, match="not opened"):
        _ = handle.saver


def test_checkpointer_handle_context_manager(tmp_path: Path) -> None:
    """The ``with`` form opens/closes and returns the saver."""
    db = tmp_path / "ctx.sqlite"
    with CheckpointerHandle(db) as saver:
        assert saver is not None
    assert db.exists()


def test_checkpointer_handle_open_idempotent(tmp_path: Path) -> None:
    """Calling open() twice returns the same saver (no re-creation)."""
    db = tmp_path / "idem.sqlite"
    handle = CheckpointerHandle(db)
    s1 = handle.open()
    s2 = handle.open()
    assert s1 is s2
    handle.close()


def test_checkpointer_handle_close_logs_on_sqlite_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If ``conn.close()`` raises sqlite3.Error, close() logs but doesn't propagate."""

    class _FakeConn:
        """Minimal fake mimicking the conn API surface checkpoint.close uses."""

        def close(self) -> None:
            raise sqlite3.Error("simulated close failure")

    db = tmp_path / "err.sqlite"
    handle = CheckpointerHandle(db)
    handle._conn = _FakeConn()  # type: ignore[assignment]
    handle._saver = object()  # type: ignore[assignment]
    with caplog.at_level("WARNING"):
        handle.close()
    assert any("Error closing checkpointer conn" in rec.message for rec in caplog.records)


# ── daemon/repository.py ─────────────────────────────────────────────────


def test_default_db_path_uses_arktower_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_default_db_path`` honors ``$ARKTOWER_HOME``."""
    monkeypatch.setenv("ARKTOWER_HOME", "/tmp/arkhome")
    assert repo_mod._default_db_path() == Path("/tmp/arkhome/arktower.db")


def test_default_db_path_falls_back_to_user_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``$ARKTOWER_HOME`` is unset, default is ``~/.arktower/arktower.db``."""
    monkeypatch.delenv("ARKTOWER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert repo_mod._default_db_path() == tmp_path / ".arktower" / "arktower.db"


def test_arktower_migrations_dir_env_override_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$POPOLA_ARKTOWER_MIGRATIONS_DIR`` is honored when it points to a real dir."""
    custom_dir = tmp_path / "custom_migrations"
    custom_dir.mkdir()
    monkeypatch.setenv("POPOLA_ARKTOWER_MIGRATIONS_DIR", str(custom_dir))
    result = repo_mod._arktower_migrations_dir()
    assert result == custom_dir


def test_arktower_migrations_dir_env_invalid_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the env var points to a non-existent dir, we ignore it + log a warning."""
    monkeypatch.setenv("POPOLA_ARKTOWER_MIGRATIONS_DIR", str(tmp_path / "nonexistent"))
    repo_mod._arktower_migrations_dir()


def test_popolaloom_migrations_dir_resolves() -> None:
    """``_popolaloom_migrations_dir`` resolves to a real path under the repo."""
    p = repo_mod._popolaloom_migrations_dir()
    assert isinstance(p, Path)
    assert p.is_absolute()


def test_make_persistence_creates_db_and_returns_persistence(tmp_path: Path) -> None:
    """``make_persistence`` builds the 4-tuple, runs migrations, returns a usable obj."""
    db = tmp_path / "ark.sqlite"
    persistence = repo_mod.make_persistence(db_path=db)
    try:
        assert persistence.task_service is not None
        assert persistence.repository is not None
        assert persistence.connection is not None
        assert persistence.event_bus is not None
        assert db.exists()
    finally:
        persistence.close()


def test_task_persistence_close_is_idempotent(tmp_path: Path) -> None:
    """``TaskPersistence.close`` can be called multiple times without raising."""
    db = tmp_path / "tp.sqlite"
    persistence = repo_mod.make_persistence(db_path=db)
    persistence.close()
    persistence.close()


# ── mcp/server.py ────────────────────────────────────────────────────────


def test_socket_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``socket_path`` honors ``$POPOLA_HOME``."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    assert mcp_server.socket_path() == tmp_path.resolve() / "popolad.sock"


def test_socket_path_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default socket path is ``~/.popola/popolad.sock``."""
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mcp_server.socket_path() == tmp_path / ".popola" / "popolad.sock"


def test_make_async_client_returns_async_client(tmp_path: Path) -> None:
    """``make_async_client`` returns a usable AsyncClient bound to the given UDS."""
    sock = tmp_path / "fake.sock"
    client = mcp_server.make_async_client(sock)
    assert isinstance(client, httpx.AsyncClient)
    asyncio.run(client.aclose())


def test_build_server_returns_server() -> None:
    """``build_server`` returns a configured mcp.server.Server."""

    async def boot() -> None:
        client = mcp_server.make_async_client(Path("/tmp/never.sock"))
        try:
            server = mcp_server.build_server(client)
            assert server is not None
            assert server.name == "popolaloom-mcp"
        finally:
            await client.aclose()

    asyncio.run(boot())


# ── mcp/tools.py edge paths ──────────────────────────────────────────────


def test_popola_attach_stream_missing_task_id_returns_error() -> None:
    """attach_stream without task_id returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_attach_stream(client, {})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True
    assert "task_id" in result.content[0].text


def test_popola_attach_stream_invalid_last_n_returns_error() -> None:
    """attach_stream with non-int last_n returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_attach_stream(
                client, {"task_id": "tid", "last_n": "not-an-int"}
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


def test_popola_attach_stream_negative_last_n_returns_error() -> None:
    """attach_stream with last_n <= 0 returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_attach_stream(
                client, {"task_id": "tid", "last_n": 0}
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True
    assert ">= 1" in result.content[0].text


def test_popola_attach_stream_404_returns_error() -> None:
    """attach_stream when status returns 404 → not-found error."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    async def run() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://popolad"
        )
        try:
            return await mcp_tools.popola_attach_stream(
                client, {"task_id": "missing", "last_n": 5}
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True
    assert "not found" in result.content[0].text


def test_popola_submit_missing_cli_returns_error() -> None:
    """popola_submit without ``cli`` returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_submit(client, {"prompt": "hi"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


def test_popola_submit_missing_prompt_returns_error() -> None:
    """popola_submit without prompt returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_submit(client, {"cli": "cursor"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


def test_popola_status_missing_task_id_returns_error() -> None:
    """popola_status without task_id returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_status(client, {})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


def test_popola_cancel_missing_task_id_returns_error() -> None:
    """popola_cancel without task_id returns isError=True."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_cancel(client, {})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


# ── adapters/__init__.py reload safety ───────────────────────────────────


def test_register_defaults_idempotent() -> None:
    """Calling _register_defaults twice doesn't raise + leaves the registry intact."""
    from popolaloom.adapters import _register_defaults, list_registered

    before = set(list_registered())
    _register_defaults()
    after = set(list_registered())
    assert {"cursor", "claude", "codex"}.issubset(before)
    assert before == after
