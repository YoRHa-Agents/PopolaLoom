"""v0.2.2 coverage gap fillers — push line coverage from 82% to ≥85%.

Targeted tests for under-covered modules:

* :mod:`popolaloom.mcp.server` — ``build_server`` + ``_server_lifecycle``
  + ``_sync_main`` happy paths.
* :mod:`popolaloom.cli.popolad` — ``stop``/``status`` PID file edge
  cases (unreadable PID, dead process, missing pid file).
* :mod:`popolaloom.daemon.main` — ``__getattr__`` Popolad / create_app
  shim and signal-handler installation NotImplementedError fallback.

Each test mocks the deepest stable boundary so the suite stays Tier 2
fast (< 1 s per case).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import popolaloom.cli.popolad as popolad_cli
import popolaloom.daemon.main as daemon_main
import popolaloom.mcp.server as mcp_server


def test_mcp_build_server_returns_server_with_correct_metadata() -> None:
    """``build_server`` produces a Server with name + version set."""
    client = mcp_server.make_async_client(uds=Path("/tmp/non-existent.sock"))
    try:
        srv = mcp_server.build_server(client)
        assert srv.name == "popolaloom-mcp"
    finally:
        asyncio.run(client.aclose())


def test_mcp_make_async_client_default_uds_path_uses_socket_path_helper() -> None:
    """``make_async_client(None)`` uses :func:`socket_path` default."""
    client = mcp_server.make_async_client(uds=None)
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        asyncio.run(client.aclose())


def test_mcp_socket_path_honours_popola_home_env(monkeypatch, tmp_path: Path) -> None:
    """``$POPOLA_HOME`` override flows through to ``socket_path()``."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    sp = mcp_server.socket_path()
    assert sp == tmp_path / "popolad.sock"


def test_mcp_socket_path_default_when_env_unset(monkeypatch) -> None:
    """No ``$POPOLA_HOME`` → falls back to ``~/.popola/popolad.sock``."""
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    sp = mcp_server.socket_path()
    assert sp == Path.home() / ".popola" / "popolad.sock"


def test_mcp_server_lifecycle_yields_server_and_client(tmp_path: Path) -> None:
    """``_server_lifecycle`` enters / exits cleanly, closes the client."""

    async def _drive() -> tuple[bool, bool]:
        async with mcp_server._server_lifecycle(uds=tmp_path / "no.sock") as (srv, cli):
            return (srv is not None, isinstance(cli, httpx.AsyncClient))

    yielded_srv, is_client = asyncio.run(_drive())
    assert yielded_srv
    assert is_client


def test_popolad_stop_pid_file_unreadable_exits_1(
    monkeypatch, tmp_path: Path
) -> None:
    """``popolad stop`` with a malformed PID file → exit 1 + clear error."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    pid_file = tmp_path / "popolad.pid"
    pid_file.write_text("not-an-int", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(popolad_cli.app, ["stop"])
    assert result.exit_code == 1, result.output
    assert "PID file unreadable" in result.output


def test_popolad_stop_pid_dead_cleans_up_files(
    monkeypatch, tmp_path: Path
) -> None:
    """``popolad stop`` when the PID is dead → cleanup + exit 0."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    pid_file = tmp_path / "popolad.pid"
    sock = tmp_path / "popolad.sock"
    pid_file.write_text("999999\n", encoding="utf-8")
    sock.touch()

    runner = CliRunner()
    result = runner.invoke(popolad_cli.app, ["stop"])
    assert result.exit_code == 0
    assert "process 999999 is gone" in result.output


def test_popolad_status_no_socket_exits_1(monkeypatch, tmp_path: Path) -> None:
    """``popolad status`` with no socket → exit 1."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(popolad_cli.app, ["status"])
    assert result.exit_code == 1


def test_popolad_status_json_output_no_socket(
    monkeypatch, tmp_path: Path
) -> None:
    """``popolad status --json`` emits parseable JSON when socket missing."""
    import json
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(popolad_cli.app, ["status", "--json"])
    parsed = json.loads(result.output.strip())
    assert parsed["socket_exists"] is False


def test_daemon_main_getattr_exposes_popolad_class() -> None:
    """``daemon.main.Popolad`` lazy attribute access works."""
    klass = daemon_main.__getattr__("Popolad")
    from popolaloom.daemon.server import Popolad as RealPopolad
    assert klass is RealPopolad


def test_daemon_main_getattr_exposes_create_app() -> None:
    """``daemon.main.create_app`` lazy attribute access works."""
    factory = daemon_main.__getattr__("create_app")
    from popolaloom.daemon.rpc import create_app as real_factory
    assert factory is real_factory


def test_daemon_main_getattr_unknown_raises_attribute_error() -> None:
    """Unknown attribute on ``daemon.main`` → AttributeError."""
    with pytest.raises(AttributeError):
        daemon_main.__getattr__("definitely_not_a_symbol")


def test_daemon_main_remove_pid_file_missing_path_no_error(
    tmp_path: Path,
) -> None:
    """``remove_pid_file`` with non-existent path → silent no-op."""
    pid_path = tmp_path / "nonexistent.pid"
    daemon_main.remove_pid_file(pid_path)
    assert not pid_path.exists()


def test_daemon_main_remove_pid_file_oserror_logged(
    tmp_path: Path, mocker, caplog
) -> None:
    """``remove_pid_file`` swallows OSError but logs WARNING."""
    pid_path = tmp_path / "popolad.pid"
    pid_path.write_text("1234", encoding="utf-8")
    mocker.patch.object(Path, "unlink", side_effect=OSError(13, "Permission denied"))

    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon"):
        daemon_main.remove_pid_file(pid_path)

    assert any("Failed to remove PID" in r.message for r in caplog.records)


def test_daemon_main_remove_socket_missing_path_no_error(tmp_path: Path) -> None:
    """``remove_socket`` non-existent path → no-op (best-effort)."""
    sock_path = tmp_path / "popolad.sock"
    daemon_main.remove_socket(sock_path)
    assert not sock_path.exists()


def test_daemon_main_remove_socket_oserror_logged(
    tmp_path: Path, mocker, caplog
) -> None:
    """``remove_socket`` swallows OSError but logs WARNING."""
    sock_path = tmp_path / "popolad.sock"
    sock_path.touch()
    mocker.patch.object(Path, "unlink", side_effect=OSError(13, "Permission denied"))

    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon"):
        daemon_main.remove_socket(sock_path)

    assert any("Failed to remove socket" in r.message for r in caplog.records)


def test_daemon_main_get_popola_home_default(monkeypatch) -> None:
    """No env → ``~/.popola``."""
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    home = daemon_main.get_popola_home()
    assert home == Path.home() / ".popola"
    assert home.exists()


def test_daemon_main_write_pid_file_creates_parent_dir(tmp_path: Path) -> None:
    """``write_pid_file`` creates missing parent dirs."""
    pid_path = tmp_path / "deep" / "nested" / "pid.pid"
    written = daemon_main.write_pid_file(pid_path)
    assert written == pid_path
    assert pid_path.exists()
    content = int(pid_path.read_text(encoding="utf-8").strip())
    assert content == os.getpid()


def test_daemon_main_signal_handler_install_no_op_on_notimplementederror(
    tmp_path: Path, mocker
) -> None:
    """When ``add_signal_handler`` raises NotImplementedError, main logs + continues."""
    fake_loop = mocker.MagicMock()
    fake_loop.add_signal_handler.side_effect = NotImplementedError("Windows asyncio")
    mocker.patch.object(asyncio, "get_running_loop", return_value=fake_loop)

    fake_server = mocker.MagicMock()

    async def _ok():
        return None

    fake_server.serve = _ok
    mocker.patch.object(daemon_main.uvicorn, "Server", return_value=fake_server)
    mocker.patch.object(daemon_main.uvicorn, "Config", return_value=mocker.MagicMock())
    mocker.patch.object(daemon_main, "_build_default_popolad", return_value=mocker.MagicMock())

    asyncio.run(
        daemon_main.main(
            socket_path=tmp_path / "no.sock",
            events_dir=tmp_path / "events",
            pid_path=tmp_path / "popolad.pid",
        )
    )
    assert fake_loop.add_signal_handler.called
