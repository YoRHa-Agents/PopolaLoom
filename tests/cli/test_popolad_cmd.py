"""Default-lane tests for ``popola popolad`` (v0.5.4 Loop 4 — L4.D).

Per release-notes-v0.5.4.md L4.D: ``cli/popolad.py`` was at 89 % coverage
going into Loop 4. The ``start`` / ``stop`` / ``status`` subcommands have
several uncovered conditional branches:

- start when already running (port-in-use) → error + exit 1.
- start when stale PID file exists but no live process → recoverable.
- stop when not running (no PID file) → cleanup-and-exit.
- stop when stale socket exists but no PID file → cleans up the socket.
- status when corrupt PID file (non-int contents) → reports pid_file_error.
- status JSON output mode → asserts JSON envelope structure.
- status when socket exists but /health returns non-200 → status_code in payload.
- status with no socket → exits 1.

Each test uses ``CliRunner`` against the ``popolad`` Typer subapp; the
``$POPOLA_HOME`` env is pointed at a tmp_path so the test never touches
the developer's real ``~/.popola/`` directory or spawns a real daemon.
``subprocess.Popen`` is monkey-patched in the start-path tests so the
suite remains hermetic + fast (< 100 ms per case).
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli.popolad import app as popolad_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_popola_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Isolate ``$POPOLA_HOME`` so tests never touch the real ``~/.popola/``."""
    home = tmp_path / "popola_home"
    home.mkdir()
    monkeypatch.setenv("POPOLA_HOME", str(home))
    yield home


def _combined(result: object) -> str:
    """Return ``result.stdout`` + best-effort ``result.stderr`` (click 8.x compat)."""
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except (ValueError, AttributeError):
        stderr = ""
    output = getattr(result, "output", "") or ""
    return stdout + stderr + output


# ── start: refuses when already running ─────────────────────────────────


def test_popolad_start_refuses_when_pid_file_alive(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popolad start`` refuses with exit 1 when an existing PID file
    points at a live process (lines 103-114).
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = runner.invoke(popolad_app, ["start"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "already running" in out
    assert str(os.getpid()) in out


def test_popolad_start_handles_corrupt_pid_file_as_dead(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt PID file (non-int) is treated as ``existing_pid = -1``
    (lines 105-107 ValueError fallback) which means the start loop
    continues past the early-return.

    We monkeypatch ``subprocess.Popen`` so the test doesn't spawn a real
    daemon; the goal is to assert that the corrupt PID file does NOT
    cause an "already running" exit.
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text("not-an-int\n", encoding="utf-8")

    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.poll.return_value = 1
    fake_proc.returncode = 1
    monkeypatch.setattr(
        "popolaloom.cli.popolad.subprocess.Popen",
        lambda *_a, **_kw: fake_proc,
    )

    result = runner.invoke(popolad_app, ["start", "--timeout", "0.5"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "already running" not in out
    assert ("exited prematurely" in out) or ("failed to bind" in out)


# ── start: stale socket cleanup ─────────────────────────────────────────


def test_popolad_start_removes_stale_socket(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popolad start`` unlinks an existing socket file before spawning
    (lines 116-118). We seed a stale socket then assert it's gone.

    The Popen mock returns a process that exits immediately so the start
    loop exits via the "exited prematurely" path, but the unlink is
    independent of the spawn outcome.
    """
    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.poll.return_value = 1
    fake_proc.returncode = 1
    monkeypatch.setattr(
        "popolaloom.cli.popolad.subprocess.Popen",
        lambda *_a, **_kw: fake_proc,
    )

    result = runner.invoke(popolad_app, ["start", "--timeout", "0.5"])
    assert result.exit_code == 1
    assert not sock.exists()


# ── start: subprocess exits prematurely ─────────────────────────────────


def test_popolad_start_subprocess_exits_prematurely_errors(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``Popen.poll()`` returns a non-None exit code before the
    socket appears, ``popolad start`` exits 1 with an explanatory message
    (lines 146-152).
    """
    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.poll.return_value = 42
    fake_proc.returncode = 42
    monkeypatch.setattr(
        "popolaloom.cli.popolad.subprocess.Popen",
        lambda *_a, **_kw: fake_proc,
    )

    result = runner.invoke(popolad_app, ["start", "--timeout", "1.0"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "exited prematurely" in out
    assert "code=42" in out


def test_popolad_start_socket_bind_timeout_terminates(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the timeout deadline passes WITHOUT either socket appearing
    or the subprocess exiting, ``popolad start`` terminates the
    subprocess + exits 1 (lines 153-162).
    """
    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.poll.return_value = None
    fake_proc.terminate = MagicMock()
    monkeypatch.setattr(
        "popolaloom.cli.popolad.subprocess.Popen",
        lambda *_a, **_kw: fake_proc,
    )

    result = runner.invoke(popolad_app, ["start", "--timeout", "0.2"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "failed to bind socket" in out
    fake_proc.terminate.assert_called_once()


# ── stop: no PID file ───────────────────────────────────────────────────


def test_popolad_stop_when_not_running(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad stop`` with no PID file prints the not-running message
    + returns 0 (line 177-182).
    """
    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 0
    assert "not running" in _combined(result)


def test_popolad_stop_when_stale_socket_no_pid_file(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad stop`` removes a stale socket even when there's no PID file
    (lines 178-181 socket-cleanup branch).
    """
    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "not running" in out
    assert not sock.exists()


def test_popolad_stop_with_dead_pid_cleans_files(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad stop`` with PID file pointing at a dead process cleans
    up files + returns 0 (line 190-193).

    PID 0 is never alive (signal 0 against pid 0 → ProcessLookupError).
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text("0\n", encoding="utf-8")
    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "process" in out
    assert "is gone" in out
    assert not pid_file.exists()
    assert not sock.exists()


def test_popolad_stop_when_pid_file_unreadable(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad stop`` with a non-int PID file exits 1 with the unreadable
    message (lines 184-188).
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text("not-a-pid\n", encoding="utf-8")

    result = runner.invoke(popolad_app, ["stop"])
    assert result.exit_code == 1
    assert "PID file unreadable" in _combined(result)


def test_popolad_stop_signals_live_process(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popolad stop`` against a live process sends SIGTERM and then
    waits for the process to exit (line 195-208).

    We monkey-patch ``os.kill`` and ``_pid_alive`` so the test doesn't
    actually signal the test runner. After SIGTERM is sent we flip the
    aliveness flag so the wait loop exits cleanly.
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text("12345\n", encoding="utf-8")
    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    sent_signals: list[tuple[int, int]] = []
    alive_state = {"alive": True}

    def _fake_kill(pid: int, sig: int) -> None:
        sent_signals.append((pid, sig))
        if sig == signal.SIGTERM:
            alive_state["alive"] = False

    monkeypatch.setattr("popolaloom.cli.popolad.os.kill", _fake_kill)
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_alive",
        lambda _: alive_state["alive"],
    )

    result = runner.invoke(popolad_app, ["stop", "--grace", "0.5"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "SIGTERM" in out
    assert "exited gracefully" in out
    assert (12345, signal.SIGTERM) in sent_signals
    assert not pid_file.exists()
    assert not sock.exists()


def test_popolad_stop_escalates_to_sigkill_after_grace(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popolad stop`` escalates to SIGKILL when the process refuses to
    exit within the grace window (lines 211-214).
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    sent_signals: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        sent_signals.append((pid, sig))

    monkeypatch.setattr("popolaloom.cli.popolad.os.kill", _fake_kill)
    monkeypatch.setattr(
        "popolaloom.cli.popolad._pid_alive",
        lambda _: True,
    )

    result = runner.invoke(popolad_app, ["stop", "--grace", "0.05"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "did not exit" in out
    assert "SIGKILL" in out
    sent_sigs = {sig for _pid, sig in sent_signals}
    assert signal.SIGTERM in sent_sigs
    assert signal.SIGKILL in sent_sigs


# ── status: corrupt PID file ────────────────────────────────────────────


def test_popolad_status_with_corrupt_pid_file_reports_error(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad status`` with a non-int PID file does NOT raise; it
    captures the parse error in ``state["pid_file_error"]`` and prints
    it (lines 239-240, 281).

    Note: ``--json`` returns early without raising Exit(1); the no-socket
    case is still observable via the captured payload.
    """
    pid_file = isolated_popola_home / "popolad.pid"
    pid_file.write_text("garbage\n", encoding="utf-8")

    result = runner.invoke(popolad_app, ["status", "--json"])
    assert result.exit_code == 0
    out = _combined(result)
    import json
    payload = json.loads(out.strip().splitlines()[-1])
    assert "pid_file_error" in payload


def test_popolad_status_no_socket_exits_1(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad status`` with no socket present exits 1 (line 286)."""
    result = runner.invoke(popolad_app, ["status"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "socket_exists" in out
    assert "False" in out


def test_popolad_status_json_envelope_keys(
    isolated_popola_home: Path,
    runner: CliRunner,
) -> None:
    """``popolad status --json`` envelope has the documented top-level keys.

    Locks the consumer-facing schema (CI gates / monitoring scripts
    parse this); pinpoints the dict construction at line 226-234.
    Note: ``--json`` mode bypasses the table-render branch and returns
    early (line 263), so exit code is 0 even when no daemon is up.
    """
    import json

    result = runner.invoke(popolad_app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    expected_keys = {
        "socket_path",
        "socket_exists",
        "pid_file",
        "pid",
        "pid_alive",
        "health",
        "probe",
    }
    assert expected_keys.issubset(set(payload.keys())), (
        f"missing keys: {expected_keys - set(payload.keys())}"
    )


def test_popolad_status_with_unreachable_socket(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popolad status`` reports ``http_error`` when the socket exists
    but no daemon listens (lines 259-260).
    """
    import json

    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("popolaloom.cli.popolad.httpx.Client", _StubClient)

    result = runner.invoke(popolad_app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert "http_error" in payload
    assert payload["socket_exists"] is True
    assert payload["health"] is None

    table_result = runner.invoke(popolad_app, ["status"])
    assert table_result.exit_code == 1
    assert "http_error" in _combined(table_result)


def test_popolad_status_health_non_200_reports_status_code(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``GET /health`` returns a non-200, status reports the code
    (line 254-255).
    """
    import json

    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    fake_health = MagicMock()
    fake_health.status_code = 503
    fake_health.json.return_value = {}

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, url: str) -> object:
            return fake_health

    monkeypatch.setattr("popolaloom.cli.popolad.httpx.Client", _StubClient)

    result = runner.invoke(popolad_app, ["status", "--json"])
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert payload["health"] == {"status_code": 503}


def test_popolad_status_up_returns_zero(
    isolated_popola_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the socket exists AND ``/health`` returns 200, status exits 0
    (line 285-286, takes the no-Exit branch).
    """
    sock = isolated_popola_home / "popolad.sock"
    sock.write_bytes(b"")

    fake_health = MagicMock()
    fake_health.status_code = 200
    fake_health.json.return_value = {"status": "ok"}
    fake_probe = MagicMock()
    fake_probe.status_code = 200
    fake_probe.json.return_value = {"daemon_pid": 4242}

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, url: str) -> object:
            if url == "/health":
                return fake_health
            return fake_probe

    monkeypatch.setattr("popolaloom.cli.popolad.httpx.Client", _StubClient)

    result = runner.invoke(popolad_app, ["status"])
    assert result.exit_code == 0


# ── _pid_alive helper ──────────────────────────────────────────────────


def test_pid_alive_returns_false_for_nonpositive_pid() -> None:
    """``_pid_alive(0)`` and ``_pid_alive(-1)`` return False (line 295-296).

    Defensive guard — passing 0 to ``os.kill`` would signal the entire
    process group; the early-return prevents that for the read-only
    "is the daemon alive" check.
    """
    from popolaloom.cli.popolad import _pid_alive

    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False


def test_pid_alive_returns_false_for_dead_pid() -> None:
    """``_pid_alive(99999999)`` returns False (line 299-300 ProcessLookupError)."""
    from popolaloom.cli.popolad import _pid_alive

    assert _pid_alive(99999999) is False


def test_pid_alive_returns_true_for_live_pid() -> None:
    """``_pid_alive`` returns True for the test runner's own PID."""
    from popolaloom.cli.popolad import _pid_alive

    assert _pid_alive(os.getpid()) is True


# ── _can_connect helper ────────────────────────────────────────────────


def test_can_connect_returns_false_on_http_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_can_connect`` swallows ``httpx.HTTPError`` (line 315) → False."""
    from popolaloom.cli.popolad import _can_connect

    class _StubClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _StubClient:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, _url: str) -> object:
            raise httpx.ConnectError("nope")

    monkeypatch.setattr("popolaloom.cli.popolad.httpx.Client", _StubClient)
    assert _can_connect(tmp_path / "fake.sock") is False


# ── _cleanup_files helper ──────────────────────────────────────────────


def test_cleanup_files_removes_existing_files(tmp_path: Path) -> None:
    """``_cleanup_files`` removes both PID + sock files when present."""
    from popolaloom.cli.popolad import _cleanup_files

    pid_file = tmp_path / "p.pid"
    sock = tmp_path / "p.sock"
    pid_file.write_text("1\n")
    sock.write_bytes(b"")
    _cleanup_files(pid_file, sock)
    assert not pid_file.exists()
    assert not sock.exists()


def test_cleanup_files_silent_when_files_absent(tmp_path: Path) -> None:
    """``_cleanup_files`` is a no-op when neither file exists."""
    from popolaloom.cli.popolad import _cleanup_files

    pid_file = tmp_path / "p.pid"
    sock = tmp_path / "p.sock"
    _cleanup_files(pid_file, sock)
    assert not pid_file.exists()
    assert not sock.exists()
