"""Tier 2 — v0.3.5 round 5 supplementary coverage gap-fillers.

Pre-v0.4.0 GA push to lift default-lane coverage from 90.93 % toward
the 92 % target.  Targets the highest-leverage missed lines:

- ``cli/popolad.py``    82 % — start/stop/status edge cases
- ``mcp/server.py``     85 % — ``_sync_main`` KeyboardInterrupt branch
- ``daemon/main.py``    81 % — ``run()`` exception paths

All tests are pure / mocked-subprocess (no daemon spawned).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli.popolad import (
    _can_connect,
    _cleanup_files,
    _pid_alive,
)
from popolaloom.cli.popolad import app as popolad_app

runner = CliRunner()


# ── _pid_alive helper ────────────────────────────────────────────────


def test_pid_alive_returns_false_for_zero() -> None:
    assert _pid_alive(0) is False


def test_pid_alive_returns_false_for_negative() -> None:
    assert _pid_alive(-1) is False


def test_pid_alive_returns_false_for_nonexistent_pid() -> None:
    """signal 0 to a likely-missing PID returns False (ProcessLookupError)."""
    assert _pid_alive(99_999_999) is False


def test_pid_alive_returns_true_for_self() -> None:
    """signal 0 to our own pid succeeds."""
    import os

    assert _pid_alive(os.getpid()) is True


def test_pid_alive_handles_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """PermissionError → returns True (the process exists, we just can't signal)."""

    def _raise_perm(_pid: int, _sig: int) -> None:
        raise PermissionError("EPERM")

    monkeypatch.setattr("os.kill", _raise_perm)
    assert _pid_alive(1234) is True


# ── _can_connect helper ─────────────────────────────────────────────


def test_can_connect_returns_false_on_missing_socket(tmp_path: Path) -> None:
    assert _can_connect(tmp_path / "missing.sock") is False


def test_can_connect_returns_false_on_http_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If httpx.Client.get raises, _can_connect returns False (best-effort)."""

    class _BoomClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _BoomClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, _path: str) -> Any:
            raise httpx.ConnectError("uds gone")

    monkeypatch.setattr("popolaloom.cli.popolad.httpx.Client", _BoomClient)
    assert _can_connect(tmp_path / "any.sock") is False


# ── _cleanup_files ──────────────────────────────────────────────────


def test_cleanup_files_removes_existing_files(tmp_path: Path) -> None:
    pid_file = tmp_path / "popolad.pid"
    sock = tmp_path / "popolad.sock"
    pid_file.write_text("12345")
    sock.write_text("socket-marker")
    _cleanup_files(pid_file, sock)
    assert not pid_file.exists()
    assert not sock.exists()


def test_cleanup_files_warns_on_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OSError on unlink → warning printed (No Silent Failures)."""
    pid_file = tmp_path / "popolad.pid"
    pid_file.write_text("12345")

    def _raise_unlink(self: Path) -> None:
        raise OSError("readonly fs")

    monkeypatch.setattr(Path, "unlink", _raise_unlink)
    _cleanup_files(pid_file, tmp_path / "popolad.sock")
    captured = capsys.readouterr()
    assert "warning: could not remove" in captured.err


def test_cleanup_files_handles_missing_files(tmp_path: Path) -> None:
    """Missing files are a no-op (don't raise)."""
    _cleanup_files(tmp_path / "nope.pid", tmp_path / "nope.sock")


# ── popolad start with stale PID file (existing process check) ────


def test_popolad_start_with_stale_pid_file_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing PID file + alive process → exit 1 with helpful message."""
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_path", lambda: tmp_path / "popolad.pid"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._socket_path", lambda: tmp_path / "popolad.sock"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._log_path", lambda: tmp_path / "popolad.log"
    )
    monkeypatch.setattr("popolaloom.cli.popolad._pid_alive", lambda pid: True)
    (tmp_path / "popolad.pid").write_text("99999")

    result = runner.invoke(popolad_app, ["start"])
    assert result.exit_code == 1
    assert "popolad already running" in result.output


def test_popolad_start_with_corrupt_pid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Corrupt PID file (non-int) → treated as stale; existing-pid path skipped."""
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_path", lambda: tmp_path / "popolad.pid"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._socket_path", lambda: tmp_path / "popolad.sock"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._log_path", lambda: tmp_path / "popolad.log"
    )
    monkeypatch.setattr("popolaloom.cli.popolad._pid_alive", lambda _: False)

    # Mock subprocess to spawn a fake daemon that exits immediately
    class _FakeProc:
        pid = 1
        returncode = 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            pass

    monkeypatch.setattr(
        "popolaloom.cli.popolad.subprocess.Popen",
        lambda *_a, **_k: _FakeProc(),
    )

    (tmp_path / "popolad.pid").write_text("not-a-pid")
    result = runner.invoke(popolad_app, ["start", "--timeout", "0.5"])
    assert result.exit_code == 1
    assert "popolad subprocess exited prematurely" in result.output


# ── popolad stop edge cases ─────────────────────────────────────────


def test_popolad_stop_no_pid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No PID file → idempotent message, exit 0."""
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_path", lambda: tmp_path / "popolad.pid"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._socket_path", lambda: tmp_path / "popolad.sock"
    )
    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 0
    assert "popolad not running" in result.output


def test_popolad_stop_pid_file_corrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Corrupt PID file → exit 1 with helpful error."""
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_path", lambda: tmp_path / "popolad.pid"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._socket_path", lambda: tmp_path / "popolad.sock"
    )
    (tmp_path / "popolad.pid").write_text("definitely-not-a-pid")
    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 1
    assert "PID file unreadable" in result.output


def test_popolad_stop_pid_already_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PID file points to dead process → cleanup files + exit 0."""
    pid_file = tmp_path / "popolad.pid"
    sock = tmp_path / "popolad.sock"
    pid_file.write_text("12345")
    sock.write_text("stale-socket")

    monkeypatch.setattr("popolaloom.cli.popolad._pid_path", lambda: pid_file)
    monkeypatch.setattr("popolaloom.cli.popolad._socket_path", lambda: sock)
    monkeypatch.setattr("popolaloom.cli.popolad._pid_alive", lambda _: False)

    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 0
    assert "process 12345 is gone" in result.output
    assert not pid_file.exists()
    assert not sock.exists()


def test_popolad_stop_pid_lookup_error_during_sigterm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ProcessLookupError during SIGTERM → cleanup + exit 0 (race-safe)."""
    pid_file = tmp_path / "popolad.pid"
    sock = tmp_path / "popolad.sock"
    pid_file.write_text("12345")
    sock.write_text("stale-socket")

    monkeypatch.setattr("popolaloom.cli.popolad._pid_path", lambda: pid_file)
    monkeypatch.setattr("popolaloom.cli.popolad._socket_path", lambda: sock)
    monkeypatch.setattr("popolaloom.cli.popolad._pid_alive", lambda _: True)

    def _raise_lookup(pid: int, sig: int) -> None:
        raise ProcessLookupError("dead")

    monkeypatch.setattr("popolaloom.cli.popolad.os.kill", _raise_lookup)
    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 0
    assert "already gone" in result.output


# ── popolad status edge cases ───────────────────────────────────────


def test_popolad_status_no_socket_or_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No PID file, no socket → table reports nothing alive; exit 1."""
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_path", lambda: tmp_path / "popolad.pid"
    )
    monkeypatch.setattr(
        "popolaloom.cli.popolad._socket_path", lambda: tmp_path / "popolad.sock"
    )
    result = runner.invoke(popolad_app, ["status"])
    assert result.exit_code == 1


def test_popolad_status_corrupt_pid_file_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Corrupt PID file → status records pid_file_error, doesn't crash."""
    pid_file = tmp_path / "popolad.pid"
    pid_file.write_text("oops-not-a-pid")
    monkeypatch.setattr("popolaloom.cli.popolad._pid_path", lambda: pid_file)
    monkeypatch.setattr(
        "popolaloom.cli.popolad._socket_path", lambda: tmp_path / "popolad.sock"
    )
    result = runner.invoke(popolad_app, ["status"])
    assert result.exit_code == 1


# ── mcp/server.py: _sync_main KeyboardInterrupt path ────────────────


def test_mcp_server_sync_main_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_sync_main`` swallows KeyboardInterrupt and logs (lines 250-253)."""
    from popolaloom.mcp.server import _sync_main

    def _raise_kbi(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("popolaloom.mcp.server.asyncio.run", _raise_kbi)
    with caplog.at_level(logging.INFO, logger="popolaloom.mcp"):
        _sync_main()  # MUST NOT raise


def test_mcp_server_sync_main_other_exception_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-KeyboardInterrupt exceptions in main() propagate (no swallowing)."""
    from popolaloom.mcp.server import _sync_main

    def _raise_runtime(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("server boom")

    monkeypatch.setattr("popolaloom.mcp.server.asyncio.run", _raise_runtime)
    with pytest.raises(RuntimeError, match="server boom"):
        _sync_main()


# ── daemon/main.py run() exception paths ────────────────────────────


def test_daemon_main_run_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run()`` swallows KeyboardInterrupt + logs cleanup attempt."""
    from popolaloom.daemon.main import run

    def _raise_kbi(*_: Any, **__: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("popolaloom.daemon.main.asyncio.run", _raise_kbi)
    run()  # MUST NOT raise


def test_daemon_main_run_re_raises_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-KeyboardInterrupt exceptions in run() are logged + re-raised."""
    from popolaloom.daemon.main import run

    def _boom(*_: Any, **__: Any) -> Any:
        raise SystemError("daemon crashed in run()")

    monkeypatch.setattr("popolaloom.daemon.main.asyncio.run", _boom)
    with pytest.raises(SystemError, match="daemon crashed"):
        run()
