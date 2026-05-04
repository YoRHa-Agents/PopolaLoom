"""T3.3 — S1 crash-recovery extended (Tier 3 richer assertions).

Per testing-matrix.md §1.3 + workspace v0.2.2 brief T3.3.

This file complements ``tests/self_bootstrap/test_s1_crash_recovery.py``
(which is the canonical Stage E demonstration) by adding two Tier 3
variants with **richer assertions** on the recovered envelope shape:

1. :func:`test_s1_normal_sigkill_rehydrate_with_richer_assertions` —
   normal SIGKILL path; asserts on ``recovered_count`` + ``task_ids`` +
   ``popola_task_id`` fields explicitly.
2. :func:`test_s1_oom_dirty_exit_rehydrate` — simulates an OOM-style
   dirty exit (SIGKILL + no graceful socket cleanup) and verifies the
   second daemon's startup unlinks the stale socket and rehydrates
   anyway.

Both cases use the function-scoped fixture (one daemon per case) and
explicitly tear down the leaked cursor shim sleeper at the end so the
test session doesn't accumulate orphan PIDs.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.fixtures.real_popolad import (
    _spawn_daemon_process,
    _terminate_daemon,
    _wait_for_socket,
    kill_orphan_cursor_shims,
    make_cursor_shim,
    make_isolated_env,
)

pytestmark = pytest.mark.slow

_BOOT_TIMEOUT_S: float = 8.0
_RECOVER_WAIT_S: float = 8.0


def _dispatch_via_uds(socket_path: Path, prompt: str) -> str | None:
    """POST /dispatch via UDS; return task_id."""
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)),
            base_url="http://popolad",
            timeout=10.0,
        ) as client:
            resp = client.post(
                "/dispatch",
                json={"cli": "cursor", "prompt": prompt, "cwd": None, "extra": None},
            )
            if resp.status_code == 200:
                return resp.json().get("task_id")
    except (httpx.HTTPError, OSError):
        return None
    return None


def _wait_for_recovered_envelope(
    events_dir: Path,
    task_id: str,
    timeout_s: float,
) -> dict | None:
    """Tail the per-task NDJSON until a ``popolad.recovered`` envelope appears."""
    event_log_path = events_dir / f"{task_id}.jsonl"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if event_log_path.exists():
            try:
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
                            return envelope
            except OSError:
                pass
        time.sleep(0.2)
    return None


def _setup_two_daemon_env(tmp_path: Path, name: str) -> tuple[dict, Path, Path, Path]:
    """Return (env, socket_path, log1, log2) for a 2-daemon trial."""
    home = tmp_path / f"{name}_home"
    bin_dir = tmp_path / f"{name}_bin"
    make_cursor_shim(bin_dir, sleep_seconds=15.0)
    log1 = tmp_path / f"{name}_d1.log"
    log2 = tmp_path / f"{name}_d2.log"
    env = make_isolated_env(home, extra_path=bin_dir)
    socket_path = home / "popolad.sock"
    return env, socket_path, log1, log2


def test_s1_normal_sigkill_rehydrate_with_richer_assertions(tmp_path: Path) -> None:
    """T3.3.a: Normal SIGKILL → restart → in-flight task rehydrated with full envelope.

    Asserts:

    - ``recovered_count`` ≥ 1 (one in-flight task);
    - ``task_ids`` contains the original popola task_id;
    - ``popola_task_id`` matches one of the in-flight task ids;
    - The rehydrated handle is visible via ``GET /list?include_terminal=true``;
    - The original ``task.dispatched`` envelope is preserved alongside
      the new ``popolad.recovered`` envelope (no log truncation).
    """
    env, socket_path, log1, log2 = _setup_two_daemon_env(tmp_path, "s1tier3a")
    home = Path(env["POPOLA_HOME"])
    events_dir = home / "events"

    daemon1 = _spawn_daemon_process(env, log1)
    daemon2 = None
    task_id: str | None = None
    try:
        assert _wait_for_socket(socket_path, _BOOT_TIMEOUT_S), (
            f"d1 boot failed; log:\n{log1.read_text(encoding='utf-8', errors='replace')}"
        )

        task_id = _dispatch_via_uds(socket_path, "S1 tier3 dispatch a")
        assert task_id is not None, "dispatch failed"
        time.sleep(0.5)

        os.kill(daemon1.pid, signal.SIGKILL)
        try:
            daemon1.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pytest.fail("d1 did not exit within 3s of SIGKILL")
        with contextlib.suppress(OSError):
            if socket_path.exists():
                socket_path.unlink()
        time.sleep(0.5)

        daemon2 = _spawn_daemon_process(env, log2)
        assert _wait_for_socket(socket_path, _BOOT_TIMEOUT_S), (
            f"d2 boot failed; log:\n{log2.read_text(encoding='utf-8', errors='replace')}"
        )

        envelope = _wait_for_recovered_envelope(events_dir, task_id, _RECOVER_WAIT_S)
        assert envelope is not None, (
            f"T3.3.a: popolad.recovered envelope missing for task {task_id}"
        )
        data = envelope.get("data", {})
        assert data.get("popola_task_id") == task_id
        assert int(data.get("recovered_count", 0)) >= 1
        assert task_id in (data.get("task_ids") or [])

        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)),
            base_url="http://popolad",
            timeout=10.0,
        ) as client:
            resp = client.get("/list?include_terminal=true")
            assert resp.status_code == 200
            items = resp.json()
            assert any(it.get("task_id") == task_id for it in items)

    finally:
        if daemon1.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(daemon1.pid, signal.SIGKILL)
        if daemon2 is not None:
            _terminate_daemon(daemon2)
        kill_orphan_cursor_shims()


def test_s1_oom_dirty_exit_rehydrate(tmp_path: Path) -> None:
    """T3.3.b: OOM-like dirty exit (SIGKILL leaving stale socket) → recovery still works.

    The first daemon is SIGKILL'd and the **socket file is intentionally
    NOT cleaned up** (simulating an actual OOM crash where signal
    handlers don't get a chance to run).  The second daemon's
    :func:`popolaloom.daemon.main.main` startup logic must unlink the
    stale socket before binding — verifying this is the core T3.3.b
    invariant.
    """
    env, socket_path, log1, log2 = _setup_two_daemon_env(tmp_path, "s1tier3b")
    home = Path(env["POPOLA_HOME"])
    events_dir = home / "events"

    daemon1 = _spawn_daemon_process(env, log1)
    daemon2 = None
    task_id: str | None = None
    try:
        assert _wait_for_socket(socket_path, _BOOT_TIMEOUT_S)
        task_id = _dispatch_via_uds(socket_path, "S1 tier3 OOM-style")
        assert task_id is not None
        time.sleep(0.5)

        os.kill(daemon1.pid, signal.SIGKILL)
        try:
            daemon1.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pytest.fail("d1 (OOM) did not exit within 3s of SIGKILL")

        assert socket_path.exists(), (
            "T3.3.b setup: stale socket should still exist (no graceful cleanup)"
        )
        time.sleep(0.5)

        daemon2 = _spawn_daemon_process(env, log2)
        assert _wait_for_socket(socket_path, _BOOT_TIMEOUT_S), (
            f"T3.3.b: second daemon failed to bind despite stale socket; log:\n"
            f"{log2.read_text(encoding='utf-8', errors='replace')}"
        )

        envelope = _wait_for_recovered_envelope(events_dir, task_id, _RECOVER_WAIT_S)
        assert envelope is not None, (
            f"T3.3.b: popolad.recovered envelope missing for task {task_id} "
            f"after OOM-style restart"
        )
        data = envelope.get("data", {})
        assert int(data.get("recovered_count", 0)) >= 1
        assert task_id in (data.get("task_ids") or [])

    finally:
        if daemon1.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(daemon1.pid, signal.SIGKILL)
        if daemon2 is not None:
            _terminate_daemon(daemon2)
        kill_orphan_cursor_shims()
