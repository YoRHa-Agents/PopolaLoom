"""Tier 2 / Coverage — popola popolad start/stop/status subcommand tests.

Drives ``cli/popolad.py`` (currently ~15% covered) by mocking
:func:`subprocess.Popen`, :func:`os.kill`, and :class:`httpx.Client` so
none of the cases need a real daemon process.

Cases:

1. ``popolad start`` happy path — Popen spawns, socket appears, exit 0.
2. ``popolad start`` when an existing PID is alive — exits 1 with
   "already running" message.
3. ``popolad start`` when stale socket but no live PID — proceeds.
4. ``popolad stop`` no PID file — prints "popolad not running".
5. ``popolad stop`` with live PID + graceful exit — sends SIGTERM,
   PID dies, cleans up files.
6. ``popolad stop`` with PID file pointing to dead pid — cleans up.
7. ``popolad stop`` SIGTERM-then-SIGKILL escalation when grace expires.
8. ``popolad status`` socket missing — exits 1 with table output.
9. ``popolad status --json`` socket present + /health 200 — exits 0
   with JSON payload.
10. ``popolad status`` socket present but health probe raises — http_error in payload.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import popolad as popolad_cli


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point POPOLA_HOME at tmp_path so all helpers look there."""
    home = tmp_path / "popola_home"
    home.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(home))
    return home


# ── 1: start happy path ──────────────────────────────────────────────────


def test_start_happy_path_socket_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock Popen to return a fake proc + monkey-patch _can_connect to True; expect exit 0."""
    home = _isolate_home(tmp_path, monkeypatch)
    sock = home / "popolad.sock"

    class _FakeProc:
        pid = 99999
        returncode = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeProc:
        sock.write_text("")  # mimic uvicorn binding the UDS
        return _FakeProc()

    monkeypatch.setattr(popolad_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(popolad_cli, "_can_connect", lambda _p: True)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["start", "--timeout", "1"])
    assert r.exit_code == 0, f"stdout={r.stdout!r} exc={r.exception!r}"
    assert "popolad started, PID=99999" in r.stdout


# ── 2: start when already running → exit 1 ───────────────────────────────


def test_start_when_already_running_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If PID file points to a living process, ``start`` exits 1 with helpful message."""
    home = _isolate_home(tmp_path, monkeypatch)
    pid_file = home / "popolad.pid"
    pid_file.write_text(str(os.getpid()))  # we are alive

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["start"])
    assert r.exit_code == 1
    output = r.stdout + (r.stderr if hasattr(r, "stderr") else "")
    assert "already running" in output


# ── 3: start with stale socket but no live pid → proceeds ────────────────


def test_start_with_stale_socket_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a stale socket exists but PID is dead/missing, ``start`` removes it and continues."""
    home = _isolate_home(tmp_path, monkeypatch)
    sock = home / "popolad.sock"
    sock.write_text("stale")

    class _FakeProc:
        pid = 88888
        returncode = None

        def poll(self) -> int | None:
            return None

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeProc:
        sock.write_text("new")
        return _FakeProc()

    monkeypatch.setattr(popolad_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(popolad_cli, "_can_connect", lambda _p: True)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["start", "--timeout", "1"])
    assert r.exit_code == 0


# ── 4: stop with no pid file ─────────────────────────────────────────────


def test_stop_with_no_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` with no PID file prints "not running" + exits 0."""
    _isolate_home(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["stop"])
    assert r.exit_code == 0
    assert "not running" in r.stdout


# ── 5: stop with live pid + graceful exit ────────────────────────────────


def test_stop_graceful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` sends SIGTERM, sees pid die, cleans up files."""
    home = _isolate_home(tmp_path, monkeypatch)
    pid_file = home / "popolad.pid"
    pid_file.write_text("12345")
    sock = home / "popolad.sock"
    sock.write_text("")

    alive_calls: list[int] = []
    sigterm_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        sigterm_calls.append((pid, sig))

    def fake_alive(pid: int) -> bool:
        alive_calls.append(pid)
        # First call: process is alive (initial check before SIGTERM).
        # Subsequent calls: claim it died gracefully.
        return len(alive_calls) == 1

    monkeypatch.setattr(popolad_cli.os, "kill", fake_kill)
    monkeypatch.setattr(popolad_cli, "_pid_alive", fake_alive)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["stop", "--grace", "0.2"])
    assert r.exit_code == 0
    assert "exited gracefully" in r.stdout
    assert sigterm_calls == [(12345, signal.SIGTERM)]
    assert not pid_file.exists()
    assert not sock.exists()


# ── 6: stop with stale pid (process gone) ────────────────────────────────


def test_stop_with_stale_pid_cleans_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PID file exists but process is gone, ``stop`` cleans up + exits 0."""
    home = _isolate_home(tmp_path, monkeypatch)
    pid_file = home / "popolad.pid"
    pid_file.write_text("12345")
    sock = home / "popolad.sock"
    sock.write_text("")

    monkeypatch.setattr(popolad_cli, "_pid_alive", lambda _pid: False)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["stop"])
    assert r.exit_code == 0
    assert "process 12345 is gone" in r.stdout
    assert not pid_file.exists()
    assert not sock.exists()


# ── 7: stop SIGTERM → SIGKILL escalation ─────────────────────────────────


def test_stop_escalates_to_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When SIGTERM grace expires, stop escalates to SIGKILL."""
    home = _isolate_home(tmp_path, monkeypatch)
    pid_file = home / "popolad.pid"
    pid_file.write_text("12345")

    sigs: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        sigs.append((pid, sig))

    monkeypatch.setattr(popolad_cli.os, "kill", fake_kill)
    monkeypatch.setattr(popolad_cli, "_pid_alive", lambda _pid: True)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["stop", "--grace", "0.05"])
    assert r.exit_code == 0
    # SIGKILL message goes via stderr; in older Click versions stderr is mixed
    # into stdout; in 8.2+ it lives on r.stderr. Tolerate both.
    output = r.stdout + (getattr(r, "stderr", "") or "")
    assert "SIGKILL" in output
    assert (12345, signal.SIGTERM) in sigs
    assert (12345, signal.SIGKILL) in sigs


# ── 8: status with socket missing → exit 1 ───────────────────────────────


def test_status_socket_missing_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When socket isn't present, status renders a table + exits 1."""
    _isolate_home(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["status"])
    assert r.exit_code == 1
    assert "socket_exists" in r.stdout


# ── 9: status JSON, healthy ──────────────────────────────────────────────


def test_status_json_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When socket exists + httpx.Client returns 200 health, ``status --json`` exits 0."""
    home = _isolate_home(tmp_path, monkeypatch)
    sock = home / "popolad.sock"
    sock.write_text("")

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def get(self, path: str) -> httpx.Response:
            if path == "/health":
                return httpx.Response(
                    200, json={"status": "ok"}, request=httpx.Request("GET", path)
                )
            if path == "/probe":
                return httpx.Response(
                    200,
                    json={
                        "daemon_pid": 4242,
                        "started_at": "2026-05-04T11:00Z",
                        "uptime_seconds": 1.0,
                        "active_tasks": 0,
                        "version": "0.2.1",
                    },
                    request=httpx.Request("GET", path),
                )
            return httpx.Response(404, request=httpx.Request("GET", path))

    monkeypatch.setattr(popolad_cli.httpx, "Client", _FakeClient)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["status", "--json"])
    assert r.exit_code == 0, f"stdout={r.stdout!r} exc={r.exception!r}"
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["socket_exists"] is True
    assert payload["health"] == {"status": "ok"}


# ── 10: status with HTTP error during probe ──────────────────────────────


def test_status_http_error_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When httpx raises, ``http_error`` field is recorded in the status output."""
    home = _isolate_home(tmp_path, monkeypatch)
    sock = home / "popolad.sock"
    sock.write_text("")

    class _BoomClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _BoomClient:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def get(self, _path: str) -> httpx.Response:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(popolad_cli.httpx, "Client", _BoomClient)

    runner = CliRunner()
    r = runner.invoke(popolad_cli.app, ["status", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert "http_error" in payload


# ── 11: _pid_alive helper ────────────────────────────────────────────────


def test_pid_alive_returns_false_for_zero() -> None:
    """``_pid_alive(0)`` returns False (PID 0 is invalid)."""
    assert popolad_cli._pid_alive(0) is False


def test_pid_alive_returns_false_for_negative() -> None:
    """``_pid_alive(-1)`` returns False (negative PIDs are invalid)."""
    assert popolad_cli._pid_alive(-1) is False


def test_pid_alive_returns_true_for_self() -> None:
    """``_pid_alive(os.getpid())`` returns True for the running process."""
    assert popolad_cli._pid_alive(os.getpid()) is True
