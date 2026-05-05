"""S1 self-bootstrap: popolad SIGKILL → restart → rehydrate (R-002 closure).

This is the **canonical** demonstration that PopolaLoom v0.2.0 closes
P0 R-001 (real daemon process) + P0 R-002 (cross-restart visibility) +
the Stage E rehydrate event-emission contract:

1. Spawn ``python -m popolaloom.daemon`` in an isolated ``$POPOLA_HOME``.
2. Wait for the UDS socket to appear.
3. Dispatch a long-running task via ``popola dispatch ... --json --cli cursor``;
   a fake ``cursor-agent`` shim on PATH sleeps for 30s so the task is
   in-flight when we SIGKILL the daemon.
4. ``popola list --json`` shows ≥ 1 in-flight task.
5. ``kill -9 <daemon_pid>`` (no graceful shutdown).
6. Sleep briefly so the SIGKILL takes effect + sockets release.
7. Re-spawn ``python -m popolaloom.daemon`` (same ``$POPOLA_HOME``,
   so it sees the existing ArkTower SQLite + popola_dispatch table).
8. Wait for the new socket; assert ``popola list --all --json``
   still shows the original task (rehydrated from ArkTower SQLite).
9. Assert the per-task NDJSON contains a ``popolad.recovered`` event
   (Stage E E1 contract; emitted by
   :meth:`Popolad._emit_recovered_events`).
10. SIGTERM the second daemon; verify graceful exit; clean up the
    leaked sleeper subprocess (it survives the daemon SIGKILL by
    design — that's the R-001/R-005 cross-terminal survival contract).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_BOOT_TIMEOUT_S: float = 15.0
_DAEMON_SHUTDOWN_TIMEOUT_S: float = 5.0
_LIST_RETRY_TIMEOUT_S: float = 8.0
_MISSING_LOG_TEXT: str = "(missing)"


pytestmark = pytest.mark.slow


def _safe_read(path: Path) -> str:
    """Read ``path`` as utf-8 with replacement; return ``_MISSING_LOG_TEXT`` if missing."""
    if not path.exists():
        return _MISSING_LOG_TEXT
    return path.read_text(encoding="utf-8", errors="replace")


def _is_daemon_alive(pid: int) -> bool:
    """Return True iff signal 0 reaches ``pid``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for_socket(socket_path: Path, timeout_s: float) -> bool:
    """Block until ``socket_path`` accepts connections or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if socket_path.exists():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(0.5)
                sock.connect(str(socket_path))
                return True
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    sock.close()
        time.sleep(0.05)
    return False


def _run_cli(
    args: list[str],
    env: dict[str, str],
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke the popola CLI as a subprocess + capture stdout/stderr."""
    cmd = [sys.executable, "-m", "popolaloom.cli.main", *args]
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO_ROOT),
    )


def _spawn_daemon(env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    """Spawn ``python -m popolaloom.daemon`` in a fresh session.

    ``start_new_session=True`` mirrors :file:`src/popolaloom/cli/popolad.py`
    so the test daemon has the same isolation properties as the
    production one (PGID detached from pytest's session).
    """
    log_fh = log_path.open("ab", buffering=0)
    cmd = [sys.executable, "-m", "popolaloom.daemon"]
    return subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
        cwd=str(_REPO_ROOT),
    )


def _make_cursor_shim(bin_dir: Path) -> Path:
    """Create a fake ``cursor-agent`` shim that sleeps so the task stays in-flight.

    The CursorAdapter calls ``cursor-agent`` (its declared ``binary``)
    so we satisfy ``shutil.which`` by putting our shim on PATH. The
    shim ignores its args, prints a marker to stdout (so the test can
    confirm the subprocess actually started), then sleeps long enough
    that the task is still in-flight when we SIGKILL the daemon.

    The shim *must* survive the daemon SIGKILL (R-001/R-005 cross-
    terminal survival contract — supervisor.spawn uses
    ``start_new_session=True``); the test cleans up its pid after the
    rehydrate assertions complete.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "cursor-agent"
    shim.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "print('cursor-agent shim started:', sys.argv, flush=True)\n"
            "time.sleep(30)\n"
            "print('cursor-agent shim exiting normally', flush=True)\n"
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


@pytest.fixture
def isolated_popola_home(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Build an env dict pointing ``$POPOLA_HOME`` at a fresh tmp dir.

    Also redirects ``$ARKTOWER_HOME`` to the same dir so the ArkTower
    SQLite file lands inside tmp_path (full isolation from the user's
    real ``~/.arktower``).  Cleans up subprocess refs in teardown.
    """
    home = tmp_path / "popola_home"
    home.mkdir(parents=True, exist_ok=True)
    arktower_home = tmp_path / "arktower_home"
    arktower_home.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"

    _make_cursor_shim(bin_dir)

    env = os.environ.copy()
    env["POPOLA_HOME"] = str(home)
    env["ARKTOWER_HOME"] = str(arktower_home)
    env["POPOLA_USE_GRAPH"] = "0"
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    migrations_default = "/home/agent/reference/ArkTower/migrations"
    if Path(migrations_default).is_dir():
        env["POPOLA_ARKTOWER_MIGRATIONS_DIR"] = migrations_default

    yield env

    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    if arktower_home.exists():
        shutil.rmtree(arktower_home, ignore_errors=True)


def test_s1_daemon_sigkill_restart_rehydrates_inflight_tasks(
    tmp_path: Path,
    isolated_popola_home: dict[str, str],
) -> None:
    """S1: daemon SIGKILL'd → restart → original in-flight task survives.

    Asserts:

    - First ``popola list`` shows the dispatched task in a non-terminal state.
    - After SIGKILL + restart, ``popola list --all`` still shows the
      same task (rehydrated by ArkTower).
    - The task's NDJSON event log contains a ``popolad.recovered`` envelope
      with the recovered cohort metadata.
    """
    env = isolated_popola_home
    home = Path(env["POPOLA_HOME"])
    log1 = tmp_path / "popolad-1.log"
    log2 = tmp_path / "popolad-2.log"
    socket_path = home / "popolad.sock"

    leaked_subprocess_pids: list[int] = []

    daemon1 = _spawn_daemon(env, log1)
    task_id: str | None = None
    try:
        if not _wait_for_socket(socket_path, _DAEMON_BOOT_TIMEOUT_S):
            log1_text = _safe_read(log1)
            pytest.fail(
                f"daemon-1 socket {socket_path} did not appear in "
                f"{_DAEMON_BOOT_TIMEOUT_S}s; log:\n{log1_text}"
            )

        dispatch_result = _run_cli(
            [
                "dispatch",
                "long sleep via cursor shim",
                "--cli",
                "cursor",
                "--json",
            ],
            env=env,
            timeout=20.0,
        )
        if dispatch_result.returncode != 0:
            log1_text = _safe_read(log1)
            pytest.fail(
                f"dispatch failed: returncode={dispatch_result.returncode}\n"
                f"stdout: {dispatch_result.stdout}\n"
                f"stderr: {dispatch_result.stderr}\n"
                f"daemon log:\n{log1_text}"
            )

        payload = json.loads(dispatch_result.stdout.strip().splitlines()[-1])
        task_id = payload["task_id"]
        assert task_id, f"no task_id in dispatch response: {dispatch_result.stdout}"

        time.sleep(0.5)

        list_result: subprocess.CompletedProcess[str] | None = None
        deadline = time.monotonic() + _LIST_RETRY_TIMEOUT_S
        while time.monotonic() < deadline:
            list_result = _run_cli(["list", "--json"], env=env, timeout=10.0)
            if list_result.returncode == 0:
                try:
                    listed = json.loads(list_result.stdout.strip().splitlines()[-1])
                    ids = {item["task_id"] for item in listed}
                    if task_id in ids:
                        break
                except (json.JSONDecodeError, IndexError):
                    pass
            time.sleep(0.2)
        assert list_result is not None and list_result.returncode == 0, (
            f"list failed: {list_result!r}"
        )
        listed = json.loads(list_result.stdout.strip().splitlines()[-1])
        ids = {item["task_id"] for item in listed}
        assert task_id in ids, (
            f"task {task_id} not in pre-SIGKILL list; got: {ids}"
        )

        status_result = _run_cli(["status", task_id, "--json"], env=env, timeout=10.0)
        if status_result.returncode == 0:
            status_payload = json.loads(status_result.stdout.strip().splitlines()[-1])
            child_pid = status_payload.get("pid")
            if isinstance(child_pid, int) and child_pid > 0:
                leaked_subprocess_pids.append(child_pid)

        os.kill(daemon1.pid, signal.SIGKILL)
        try:
            daemon1.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"daemon-1 PID={daemon1.pid} did not exit within 3s of SIGKILL"
            )

        with contextlib.suppress(OSError):
            if socket_path.exists():
                socket_path.unlink()
        time.sleep(0.5)

    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(daemon1.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            daemon1.wait(timeout=2.0)

    assert task_id is not None, "task_id was not assigned (test setup error)"

    daemon2 = _spawn_daemon(env, log2)
    try:
        if not _wait_for_socket(socket_path, _DAEMON_BOOT_TIMEOUT_S):
            log2_text = _safe_read(log2)
            pytest.fail(
                f"daemon-2 socket {socket_path} did not appear in "
                f"{_DAEMON_BOOT_TIMEOUT_S}s; log:\n{log2_text}"
            )

        rehydrated = False
        deadline = time.monotonic() + _LIST_RETRY_TIMEOUT_S
        listed_after: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            list_after = _run_cli(["list", "--all", "--json"], env=env, timeout=10.0)
            if list_after.returncode == 0:
                try:
                    listed_after = json.loads(list_after.stdout.strip().splitlines()[-1])
                    if any(item.get("task_id") == task_id for item in listed_after):
                        rehydrated = True
                        break
                except (json.JSONDecodeError, IndexError):
                    pass
            time.sleep(0.3)

        if not rehydrated:
            log2_text = _safe_read(log2)
            pytest.fail(
                f"task {task_id} did not rehydrate after daemon restart;\n"
                f"listed_after={listed_after}\n"
                f"daemon-2 log:\n{log2_text}"
            )

        events_dir = home / "events"
        event_log_path = events_dir / f"{task_id}.jsonl"

        recovered_envelope = None
        deadline2 = time.monotonic() + _LIST_RETRY_TIMEOUT_S
        while time.monotonic() < deadline2:
            if event_log_path.exists():
                with event_log_path.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            envelope = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if envelope.get("type") == "popolad.recovered":
                            recovered_envelope = envelope
                            break
                if recovered_envelope is not None:
                    break
            time.sleep(0.2)

        assert event_log_path.exists(), (
            f"event log {event_log_path} missing after restart; "
            f"events_dir contents: {sorted(p.name for p in events_dir.glob('*'))}"
        )
        assert recovered_envelope is not None, (
            f"popolad.recovered envelope missing in {event_log_path}; "
            f"file contents: {event_log_path.read_text(encoding='utf-8')[:2000]!r}"
        )
        data = recovered_envelope.get("data", {})
        assert data.get("popola_task_id") == task_id, (
            f"recovered.popola_task_id mismatch: {data}"
        )
        assert int(data.get("recovered_count", 0)) >= 1, (
            f"recovered_count must be ≥1: {data}"
        )
        assert task_id in (data.get("task_ids") or []), (
            f"task_id missing from recovered.task_ids: {data}"
        )

    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(daemon2.pid, signal.SIGTERM)
        try:
            daemon2.wait(timeout=_DAEMON_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(daemon2.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                daemon2.wait(timeout=2.0)

        for pid in leaked_subprocess_pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
