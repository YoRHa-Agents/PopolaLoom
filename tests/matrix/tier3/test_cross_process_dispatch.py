"""T3.2 — cross-process dispatch / status / list consistency.

Per testing-matrix.md §1.3 — Tier 3 owes "跨进程 status (终端 A
dispatch + 终端 B attach)" verification.  We model "Process A / B / C"
by spawning **3 separate httpx.Client connections** to the same UDS
socket — each connection mirrors a separate developer terminal hitting
the daemon over the same Unix Domain Socket.  Combined with a real
daemon subprocess (the actual cross-process boundary), this exercises
the same RPC layer that ``popola dispatch`` / ``popola list`` /
``popola status`` would hit from separate Python interpreters.

Why not literally spawn 3 ``subprocess.Popen`` calls of the popola CLI?
We tried; the resulting test ran ~3 separate Python interpreters per
case (each loading ~250 MB of langgraph / fastapi imports) which
blew through the test container's memory cgroup well before the
assertions could complete.  The 3-distinct-Client variant exercises
the *same UDS RPC contract* (each client opens a fresh UDS socket per
request) without the per-interpreter cold-start tax.

A single dedicated case
:func:`test_cli_subprocess_can_query_real_daemon` still spawns one
subprocess CLI to prove the production code path works end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.fixtures.real_popolad import RealPopoladHandle

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _open_fresh_client(handle: RealPopoladHandle, timeout: float = 5.0):
    """Open a brand-new sync httpx.Client on the daemon's UDS.

    Each "process" gets its own client so we exercise the multi-client
    UDS path (one connection per request) instead of a shared keepalive.
    """
    return handle.make_sync_client(timeout=timeout)


def test_dispatch_in_client_a_visible_in_client_b_status(
    real_popolad: RealPopoladHandle,
) -> None:
    """Client A POST /dispatch → Client B GET /status/{id} sees the same task."""
    with _open_fresh_client(real_popolad) as client_a:
        resp_a = client_a.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "client A dispatched", "cwd": None, "extra": None},
        )
        assert resp_a.status_code == 200, (
            f"client-A dispatch: {resp_a.status_code} {resp_a.text}"
        )
        task_id = resp_a.json()["task_id"]

    with _open_fresh_client(real_popolad) as client_b:
        deadline = time.monotonic() + 5.0
        last_resp = None
        while time.monotonic() < deadline:
            last_resp = client_b.get(f"/status/{task_id}")
            if last_resp.status_code == 200 and last_resp.json().get("task_id") == task_id:
                break
            time.sleep(0.1)
        assert last_resp is not None
        assert last_resp.status_code == 200, last_resp.text
        body = last_resp.json()
        assert body["task_id"] == task_id


def test_dispatch_in_client_a_visible_in_client_c_list(
    real_popolad: RealPopoladHandle,
) -> None:
    """Client A POST /dispatch → Client C GET /list includes the task."""
    with _open_fresh_client(real_popolad) as client_a:
        resp_a = client_a.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "list-visibility test", "cwd": None, "extra": None},
        )
        assert resp_a.status_code == 200
        task_id = resp_a.json()["task_id"]

    with _open_fresh_client(real_popolad) as client_c:
        deadline = time.monotonic() + 5.0
        items: list[dict] = []
        while time.monotonic() < deadline:
            resp_c = client_c.get("/list")
            if resp_c.status_code == 200:
                items = resp_c.json()
                if any(it.get("task_id") == task_id for it in items):
                    break
            time.sleep(0.1)
        assert any(it.get("task_id") == task_id for it in items), (
            f"task {task_id} missing; saw: {[it.get('task_id') for it in items]}"
        )


def test_three_clients_consistent_state_view(
    real_popolad: RealPopoladHandle,
) -> None:
    """A dispatches → B lists → C statuses; all three see the same cli + id."""
    with _open_fresh_client(real_popolad) as client_a:
        resp_a = client_a.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "tri-client", "cwd": None, "extra": None},
        )
        assert resp_a.status_code == 200
        a_payload = resp_a.json()
        task_id = a_payload["task_id"]
        assert a_payload["cli"] == "cursor"

    deadline = time.monotonic() + 5.0
    converged = False
    last_b: list[dict] = []
    last_c: dict = {}
    while time.monotonic() < deadline:
        with _open_fresh_client(real_popolad) as client_b:
            resp_b = client_b.get("/list")
        with _open_fresh_client(real_popolad) as client_c:
            resp_c = client_c.get(f"/status/{task_id}")
        if resp_b.status_code == 200 and resp_c.status_code == 200:
            last_b = resp_b.json()
            last_c = resp_c.json()
            matched_b = next(
                (it for it in last_b if it.get("task_id") == task_id), None
            )
            if matched_b is not None and last_c.get("task_id") == task_id:
                assert matched_b["cli"] == last_c["cli"] == "cursor"
                converged = True
                break
        time.sleep(0.1)

    assert converged, (
        f"3-client consistency timed out:\n"
        f"  list={last_b}\n"
        f"  status={last_c}\n"
        f"  daemon log:\n{real_popolad.read_log()}"
    )


def test_cli_subprocess_can_query_real_daemon(
    real_popolad: RealPopoladHandle,
) -> None:
    """End-to-end smoke: a separate ``popola list --json`` subprocess works.

    Production-shaped check that the popola CLI (which loads its full
    httpx + Typer stack in a separate Python interpreter) talks to the
    real daemon.  Single subprocess per test to keep memory bounded.
    """
    env = real_popolad.env
    cmd = [sys.executable, "-m", "popolaloom.cli.main", "list", "--json"]
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15.0,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"popola list failed: stderr={result.stderr!r}\nstdout={result.stdout!r}"
    )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    assert lines, "expected at least one JSON line from popola list"
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, list)
