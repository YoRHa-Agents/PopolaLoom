"""T3.1 — real popolad daemon lifecycle (boot / SIGTERM / SIGKILL / double-start).

Per testing-matrix.md §1.3 example
``test_real_popolad_dispatch.py::test_dispatch_via_uds_real_daemon`` and
spec §10 canonical paths.

These cases verify the end-to-end **process-level** invariants of the
daemon — the unit / integration layers are covered in tier1 + tier2;
here we boot a *real* ``python -m popolaloom.daemon`` and prove:

1. The UDS socket appears under ``$POPOLA_HOME/popolad.sock`` and
   ``GET /probe`` returns a 200 + version string;
2. ``SIGTERM`` triggers graceful shutdown — the socket file is removed
   inside the 5 s grace window;
3. ``SIGKILL`` leaves the socket as an orphan but a *new* daemon can
   bind cleanly because the production startup path
   (:func:`popolaloom.daemon.main.main`) unlinks stale sockets before
   binding;
4. Starting a second daemon while the first is still alive fails
   *cleanly* — the second process exits non-zero and writes a useful
   error to stderr (No Silent Failures rule per workspace policy).

All cases use the function-scoped :func:`real_popolad` fixture (or
spawn directly via :func:`spawn_real_popolad`) to keep state fresh.
"""

from __future__ import annotations

import os
import signal
import socket as stdlib_socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.fixtures.real_popolad import (
    RealPopoladHandle,
    _spawn_daemon_process,
    _wait_for_socket,
    spawn_real_popolad,
)

pytestmark = pytest.mark.slow


def test_daemon_starts_socket_appears_probe_returns_200(
    real_popolad: RealPopoladHandle,
) -> None:
    """Daemon boot → UDS exists → ``GET /probe`` → 200 + version field."""
    assert real_popolad.socket_path.exists(), (
        f"socket missing post-boot: log:\n{real_popolad.read_log()}"
    )
    with real_popolad.make_sync_client() as client:
        resp = client.get("/probe")
        assert resp.status_code == 200
        body = resp.json()
        assert body["daemon_pid"] == real_popolad.pid
        assert body["active_tasks"] == 0
        assert isinstance(body["uptime_seconds"], (int, float))
        assert body["uptime_seconds"] >= 0.0
        assert "version" in body and isinstance(body["version"], str)


def test_daemon_sigterm_triggers_graceful_shutdown_socket_removed(
    tmp_path: Path,
) -> None:
    """SIGTERM → daemon exits cleanly + UDS file is removed."""
    with spawn_real_popolad(tmp_path) as handle:
        assert handle.is_alive()
        assert handle.socket_path.exists()

        os.kill(handle.pid, signal.SIGTERM)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if handle.proc.poll() is not None:
                break
            time.sleep(0.05)

        assert handle.proc.poll() is not None, (
            f"daemon still alive 5s after SIGTERM:\n{handle.read_log()}"
        )

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and handle.socket_path.exists():
            time.sleep(0.05)
        assert not handle.socket_path.exists(), (
            "socket file should be removed by graceful shutdown cleanup; "
            f"log:\n{handle.read_log()}"
        )


def test_daemon_sigkill_then_second_daemon_starts_cleanly(
    tmp_path: Path,
) -> None:
    """SIGKILL leaves orphan socket; next daemon unlinks it on boot."""
    with spawn_real_popolad(tmp_path, home_subdir="popola_home") as handle1:
        socket_path = handle1.socket_path
        env_first = handle1.env

        os.kill(handle1.pid, signal.SIGKILL)
        try:
            handle1.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pytest.fail("daemon did not exit within 3s of SIGKILL")
    time.sleep(0.5)

    log_path2 = tmp_path / "popolad-2.log"
    proc2 = _spawn_daemon_process(env_first, log_path2)
    try:
        assert _wait_for_socket(socket_path, 5.0), (
            f"second daemon failed to bind socket; log:\n"
            f"{log_path2.read_text(encoding='utf-8', errors='replace')}"
        )
        sock = stdlib_socket.socket(stdlib_socket.AF_UNIX, stdlib_socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(socket_path))
        sock.close()
    finally:
        if proc2.poll() is None:
            os.kill(proc2.pid, signal.SIGTERM)
            try:
                proc2.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                os.kill(proc2.pid, signal.SIGKILL)


def test_second_daemon_cannot_double_bind_existing_socket(
    tmp_path: Path,
) -> None:
    """Starting a 2nd daemon while the 1st is alive → second exits with error.

    No Silent Failures: the second daemon must surface either via a
    non-zero exit code or a failure-to-bind log line so operators see
    what went wrong.
    """
    with spawn_real_popolad(tmp_path, home_subdir="popola_home") as handle1:
        env = handle1.env
        log_path2 = tmp_path / "popolad-double.log"
        proc2 = _spawn_daemon_process(env, log_path2)
        try:
            try:
                proc2.wait(timeout=8.0)
                exit_code = proc2.returncode
            except subprocess.TimeoutExpired:
                proc2.kill()
                proc2.wait(timeout=3.0)
                exit_code = -9

            assert exit_code != 0, (
                f"second daemon should fail to bind but exited 0; log:\n"
                f"{log_path2.read_text(encoding='utf-8', errors='replace')}"
            )
            log_text = log_path2.read_text(encoding="utf-8", errors="replace").lower()
            keywords = ("address", "bind", "in use", "running")
            assert any(k in log_text for k in keywords), (
                f"second daemon stderr should mention bind / in-use / running; "
                f"got:\n{log_text}"
            )
            assert handle1.is_alive(), "first daemon must still be alive after second's failure"
        finally:
            if proc2.poll() is None:
                proc2.kill()
                proc2.wait(timeout=2.0)


def test_daemon_dispatch_endpoint_works_end_to_end(
    real_popolad: RealPopoladHandle,
    tmp_path: Path,
) -> None:
    """Sanity: ``POST /dispatch`` accepts an echo task end-to-end.

    Uses ``cli=cursor`` which the conftest fixture provides via
    cursor-agent shim on the daemon's PATH.  We don't wait for
    completion (the shim sleeps 30 s); instead we assert the daemon
    accepted the dispatch and ``GET /list`` shows the task.
    """
    with real_popolad.make_sync_client(timeout=10.0) as client:
        body = {
            "cli": "cursor",
            "prompt": "hello from tier3 dispatch test",
            "cwd": None,
            "extra": None,
        }
        resp = client.post("/dispatch", json=body)
        assert resp.status_code == 200, (
            f"dispatch failed: {resp.status_code} {resp.text}; log:\n"
            f"{real_popolad.read_log()}"
        )
        payload = resp.json()
        task_id = payload["task_id"]
        real_popolad.cleanup_pids.append(0)

        list_resp = client.get("/list")
        assert list_resp.status_code == 200
        items = list_resp.json()
        assert any(item.get("task_id") == task_id for item in items), (
            f"task {task_id} missing from list: {items}"
        )

        status_resp = client.get(f"/status/{task_id}")
        assert status_resp.status_code == 200
        st = status_resp.json()
        assert st["task_id"] == task_id

        cancel_resp = client.post(f"/cancel/{task_id}")
        assert cancel_resp.status_code == 200, cancel_resp.text
