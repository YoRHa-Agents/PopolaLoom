"""NFR-8 — failure recovery rate ≥ 95% (spec §6 NFR-8).

Per testing-matrix.md §9 — 20 trials of "dispatch → SIGKILL → restart
→ rehydrate" must succeed ≥ 95% of the time.  The CI variant runs N=5
trials (still statistically valid for catching breakage; 5/5 gives
100% > 95%, 4/5 = 80% which would correctly FAIL the assertion).

Each trial:

1. Spawn fresh daemon under ``$POPOLA_HOME = tmp / trial_<i>``.
2. Dispatch one task with a long-running cursor-agent shim sleeper
   (so the task is in-flight when the daemon dies).
3. ``os.kill(daemon_pid, SIGKILL)`` (NOT SIGTERM — we want a dirty
   exit per the NFR-8 contract).
4. Sleep ~0.5 s for socket release.
5. Re-spawn daemon with the same ``$POPOLA_HOME``.
6. Poll ``GET /list?include_terminal=true`` for up to 8 s; success =
   the original task_id appears in the list (rehydrated from
   ArkTower's SQLite).
7. SIGTERM the second daemon; clean up.

Scoring: ``recovery_count / N >= 0.95``.
"""

from __future__ import annotations

import contextlib
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

_NFR_8_TRIALS_CI: int = 5
"""Reduced N for CI (spec target N=20; 5 still catches regressions because
``recovery_count / N >= 0.95`` requires all 5 to pass)."""

_NFR_8_TARGET_RATE: float = 0.95
"""Spec §6 NFR-8 target recovery rate."""


def _run_one_trial(trial_dir: Path) -> tuple[bool, str]:
    """Execute one S1-style SIGKILL/restart trial; return (success, reason)."""
    home = trial_dir / "home"
    bin_dir = trial_dir / "bin"
    make_cursor_shim(bin_dir, sleep_seconds=20.0)
    env = make_isolated_env(home, extra_path=bin_dir)
    socket_path = home / "popolad.sock"

    log1 = trial_dir / "popolad-1.log"
    proc1 = _spawn_daemon_process(env, log1)
    task_id: str | None = None
    leaked: list[int] = []
    try:
        if not _wait_for_socket(socket_path, 10.0):
            return False, f"daemon-1 socket failed; log:\n{log1.read_text(errors='replace')}"

        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)),
            base_url="http://popolad",
            timeout=10.0,
        ) as client:
            resp = client.post(
                "/dispatch",
                json={"cli": "cursor", "prompt": "nfr8 trial", "cwd": None, "extra": None},
            )
            if resp.status_code != 200:
                return False, f"dispatch failed: {resp.status_code} {resp.text}"
            task_id = resp.json()["task_id"]

            time.sleep(0.5)

            status_resp = client.get(f"/status/{task_id}")
            if status_resp.status_code == 200:
                child_pid = status_resp.json().get("pid")
                if isinstance(child_pid, int) and child_pid > 0:
                    leaked.append(child_pid)

        try:
            os.kill(proc1.pid, signal.SIGKILL)
        except ProcessLookupError:
            return False, "daemon-1 vanished before SIGKILL"
        try:
            proc1.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            return False, "daemon-1 did not exit within 3s of SIGKILL"
    finally:
        _terminate_daemon(proc1)
        with contextlib.suppress(OSError):
            if socket_path.exists():
                socket_path.unlink()

    time.sleep(0.5)
    log2 = trial_dir / "popolad-2.log"
    proc2 = _spawn_daemon_process(env, log2)
    try:
        if not _wait_for_socket(socket_path, 10.0):
            return False, f"daemon-2 socket failed; log:\n{log2.read_text(errors='replace')}"
        assert task_id is not None

        deadline = time.monotonic() + 8.0
        rehydrated = False
        while time.monotonic() < deadline:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket_path)),
                base_url="http://popolad",
                timeout=5.0,
            ) as client:
                resp = client.get("/list", params={"include_terminal": True})
                if resp.status_code == 200 and any(
                    item.get("task_id") == task_id for item in resp.json()
                ):
                    rehydrated = True
                    break
            time.sleep(0.2)
        if not rehydrated:
            return False, f"task {task_id} not visible after restart"
        return True, "ok"
    finally:
        _terminate_daemon(proc2)
        for pid in leaked:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        kill_orphan_cursor_shims()


def test_nfr_8_recovery_rate_at_least_95pct_over_n_trials(tmp_path: Path) -> None:
    """``recovery_count / N >= 0.95`` over a small CI-friendly N."""
    successes: int = 0
    failures: list[str] = []
    for i in range(_NFR_8_TRIALS_CI):
        trial_dir = tmp_path / f"trial_{i}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        ok, reason = _run_one_trial(trial_dir)
        if ok:
            successes += 1
        else:
            failures.append(f"trial {i}: {reason}")

    rate = successes / _NFR_8_TRIALS_CI
    print(
        f"\nNFR-8 recovery: {successes}/{_NFR_8_TRIALS_CI} = "
        f"{rate * 100:.1f}% (target ≥ {_NFR_8_TARGET_RATE * 100:.0f}%)"
    )
    assert rate >= _NFR_8_TARGET_RATE, (
        f"NFR-8 violated: recovery rate {rate * 100:.1f}% < "
        f"{_NFR_8_TARGET_RATE * 100:.0f}%; failures:\n" + "\n".join(failures)
    )
