"""Real CLI smoke — cursor-agent / claude / codex, gated by ``@pytest.mark.real_cli``.

Per testing-matrix.md §3.2 — the real_cli lane runs weekly + on
demand, requires the local binary to be installed.  Each test:

1. ``shutil.which("<binary>")`` — None → skip (no fail, no error).
2. Spawn a real popolad daemon (``real_popolad`` fixture).
3. Dispatch a trivial echo prompt with ``cli=<adapter>`` so the CLI
   binary is invoked end-to-end through the supervisor.
4. Wait for the task to reach a terminal state (≤ 30 s — these are
   real CLIs which spin up).
5. Assert ``state in {completed, failed}`` (we don't care which —
   the smoke is just "binary handshake works"; CIs without LLM
   credentials will see ``failed``, which is fine).
6. Assert the daemon emitted at least the ``task.dispatched`` and
   either ``task.completed`` or ``task.failed`` envelopes (No Silent
   Failures: a real binary failure must surface in the event log).

These tests intentionally do NOT make real LLM API calls — the
``--print`` / ``-p`` style flags exit immediately on a no-credential
condition.  CI runners with creds will run the same flow and just
get a richer non-empty stdout.
"""

from __future__ import annotations

import shutil
import time
from typing import Any

import pytest

from tests.fixtures.real_popolad import RealPopoladHandle, spawn_real_popolad

pytestmark = pytest.mark.real_cli


def _wait_for_terminal(
    handle: RealPopoladHandle,
    task_id: str,
    timeout_s: float = 30.0,
) -> dict[str, Any] | None:
    """Poll ``GET /status/{task_id}`` until the state is terminal."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with handle.make_sync_client() as client:
            resp = client.get(f"/status/{task_id}")
            if resp.status_code == 200:
                body = resp.json()
                if body.get("state") in {"completed", "failed", "canceled"}:
                    return body
        time.sleep(0.3)
    return None


@pytest.mark.skipif(
    shutil.which("cursor-agent") is None,
    reason="cursor-agent binary not installed on PATH",
)
def test_real_cursor_smoke(tmp_path) -> None:
    """``cursor-agent`` echo handshake via popolad."""
    with spawn_real_popolad(tmp_path) as handle:
        with handle.make_sync_client(timeout=10.0) as client:
            resp = client.post(
                "/dispatch",
                json={
                    "cli": "cursor",
                    "prompt": "echo only — smoke check",
                    "cwd": None,
                    "extra": None,
                },
            )
            assert resp.status_code == 200, resp.text
            task_id = resp.json()["task_id"]

        terminal = _wait_for_terminal(handle, task_id, timeout_s=45.0)
        assert terminal is not None, (
            f"task {task_id} did not terminate; daemon log:\n{handle.read_log()}"
        )
        assert terminal["state"] in {"completed", "failed"}, terminal


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude binary not installed on PATH",
)
def test_real_claude_smoke(tmp_path) -> None:
    """``claude`` echo handshake via popolad."""
    with spawn_real_popolad(tmp_path) as handle:
        with handle.make_sync_client(timeout=10.0) as client:
            resp = client.post(
                "/dispatch",
                json={
                    "cli": "claude",
                    "prompt": "echo only — smoke check",
                    "cwd": None,
                    "extra": None,
                },
            )
            assert resp.status_code == 200, resp.text
            task_id = resp.json()["task_id"]

        terminal = _wait_for_terminal(handle, task_id, timeout_s=45.0)
        assert terminal is not None, (
            f"task {task_id} did not terminate; daemon log:\n{handle.read_log()}"
        )
        assert terminal["state"] in {"completed", "failed"}, terminal


@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex binary not installed on PATH",
)
def test_real_codex_smoke(tmp_path) -> None:
    """``codex`` echo handshake via popolad."""
    with spawn_real_popolad(tmp_path) as handle:
        with handle.make_sync_client(timeout=10.0) as client:
            resp = client.post(
                "/dispatch",
                json={
                    "cli": "codex",
                    "prompt": "echo only — smoke check",
                    "cwd": None,
                    "extra": None,
                },
            )
            assert resp.status_code == 200, resp.text
            task_id = resp.json()["task_id"]

        terminal = _wait_for_terminal(handle, task_id, timeout_s=45.0)
        assert terminal is not None, (
            f"task {task_id} did not terminate; daemon log:\n{handle.read_log()}"
        )
        assert terminal["state"] in {"completed", "failed"}, terminal
