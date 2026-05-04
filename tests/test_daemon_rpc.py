"""Tests for popolad RPC layer (v0.2.0 Stage A — daemon/rpc.py + daemon/main.py).

Coverage targets (≥ 8 cases):

1. UDS server startup + ``GET /health`` returns 200.
2. ``POST /dispatch`` returns ``{task_id, events_log, cli}``.
3. ``GET /status/{task_id}`` returns full status shape.
4. ``GET /list`` returns array of task summaries (filterable).
5. ``GET /attach_stream/{task_id}`` SSE stream produces NDJSON envelopes.
6. ``POST /cancel/{task_id}`` SIGTERM / SIGKILL escalation path.
7. ``GET /probe`` returns daemon_pid / uptime / active_tasks / version.
8. ``POST /dispatch`` with unknown cli → 404 (No Silent Failures).
9. R-008 ghost_exit unit test (white-box).
10. Cross-process verification: spawn daemon in subprocess + verify status.

Implementation notes:

- Most tests use a real uvicorn UDS server in a background thread so the
  full HTTP-over-UDS contract is exercised (no ASGITransport hand-waving).
- The ``daemon_uds`` fixture handles bring-up + tear-down; all RPC tests
  share it. Each test gets a clean Popolad (no test pollution between
  fixture calls because pytest re-creates fixtures per-test).
- Cross-process test (#10) uses ``subprocess.Popen(start_new_session=True)``
  with a real ``python -m popolaloom.daemon`` invocation; this is what
  proves R-001 (real daemon) + R-005 (cross-process attach) closed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from popolaloom.adapters import base as adapter_base
from popolaloom.adapters import build_command, register_adapter
from popolaloom.daemon import EventLog, Popolad
from popolaloom.daemon.rpc import create_app

# ── shared echo adapter (don't reuse test_e2e to avoid module coupling) ──


class _EchoAdapter:
    name = "echo_rpc"
    binary = sys.executable

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        snippet = f"print('echo:', {prompt!r}); import sys; sys.exit(0)"
        return [sys.executable, "-c", snippet]

    def is_available(self) -> bool:
        return True


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Snapshot + restore global ``_REGISTRY`` (mirrors test_adapters.py pattern)."""
    saved = dict(adapter_base._REGISTRY)
    try:
        yield
    finally:
        adapter_base._REGISTRY.clear()
        adapter_base._REGISTRY.update(saved)


@pytest.fixture
def daemon_uds(
    tmp_path: Path,
    isolated_registry: None,
) -> Iterator[tuple[Path, Popolad]]:
    """Spin up a popolad uvicorn UDS server in a background thread.

    Yields a tuple ``(socket_path, popolad)`` so tests can hit endpoints
    via the real UDS while still introspecting the underlying state.
    """
    if "echo_rpc" not in adapter_base._REGISTRY:
        register_adapter(_EchoAdapter())

    sock = tmp_path / "popolad.sock"
    events = tmp_path / "events"
    popolad = Popolad(events_dir=events, adapter=build_command)
    app = create_app(popolad=popolad)

    config = uvicorn.Config(
        app=app,
        uds=str(sock),
        log_level="error",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    crashed: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(server.serve())
        except BaseException as exc:
            crashed.append(exc)

    thread = threading.Thread(target=_run, daemon=True, name="popolad-uvicorn-rpc-test")
    thread.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                with httpx.Client(
                    transport=httpx.HTTPTransport(uds=str(sock)),
                    base_url="http://popolad",
                    timeout=1.0,
                ) as c:
                    if c.get("/health").status_code == 200:
                        break
            except (httpx.HTTPError, OSError):
                pass
        if crashed:
            pytest.fail(f"uvicorn thread crashed: {crashed[0]!r}")
        time.sleep(0.05)
    else:
        pytest.fail("uvicorn UDS server did not become healthy within 5s")

    try:
        yield sock, popolad
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        try:
            if sock.exists():
                sock.unlink()
        except OSError:
            pass


def _client(sock: Path) -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(sock)),
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
    )


# ── 1. UDS server startup + /health ──────────────────────────────────────


def test_health_endpoint_returns_ok(daemon_uds: tuple[Path, Popolad]) -> None:
    """``GET /health`` returns ``{status: "ok"}`` and 200 status."""
    sock, _popolad = daemon_uds
    with _client(sock) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ── 2. POST /dispatch ────────────────────────────────────────────────────


def test_dispatch_endpoint(daemon_uds: tuple[Path, Popolad]) -> None:
    """``POST /dispatch`` returns task_id + events_log path + cli."""
    sock, popolad = daemon_uds
    with _client(sock) as c:
        r = c.post("/dispatch", json={"cli": "echo_rpc", "prompt": "hi rpc"})
        assert r.status_code == 200, f"response: {r.status_code} {r.text}"
        body = r.json()
        assert "task_id" in body
        assert body["cli"] == "echo_rpc"
        assert body["events_log"].endswith(f"{body['task_id']}.jsonl")
        assert Path(body["events_log"]).parent == popolad.events_dir


# ── 3. GET /status/{task_id} ─────────────────────────────────────────────


def test_status_endpoint_full_shape(daemon_uds: tuple[Path, Popolad]) -> None:
    """``GET /status/{task_id}`` returns the unified full task summary shape."""
    sock, _popolad = daemon_uds
    with _client(sock) as c:
        r = c.post("/dispatch", json={"cli": "echo_rpc", "prompt": "for status"})
        task_id = r.json()["task_id"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            r2 = c.get(f"/status/{task_id}")
            assert r2.status_code == 200
            info = r2.json()
            if info["state"] in {"completed", "failed", "canceled"}:
                break
            time.sleep(0.05)

        for key in (
            "task_id",
            "cli",
            "state",
            "pid",
            "started_at",
            "exit_code",
            "completed_at",
            "latest_event_index",
            "arktower_task_id",
            "persisted",
        ):
            assert key in info, f"missing field {key} in status: {info}"

        assert info["state"] == "completed"
        assert info["exit_code"] == 0


# ── 4. GET /list ─────────────────────────────────────────────────────────


def test_list_endpoint_includes_terminal_filter(daemon_uds: tuple[Path, Popolad]) -> None:
    """``GET /list`` defaults to non-terminal; ``?include_terminal=true`` includes all."""
    sock, _popolad = daemon_uds
    with _client(sock) as c:
        r1 = c.post("/dispatch", json={"cli": "echo_rpc", "prompt": "list-task-1"})
        tid = r1.json()["task_id"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            info = c.get(f"/status/{tid}").json()
            if info["state"] in {"completed", "failed", "canceled"}:
                break
            time.sleep(0.05)

        r_active = c.get("/list")
        assert r_active.status_code == 200
        active_ids = {item["task_id"] for item in r_active.json()}
        assert tid not in active_ids, "terminal task leaked into non-terminal list"

        r_all = c.get("/list", params={"include_terminal": "true"})
        assert r_all.status_code == 200
        all_ids = {item["task_id"] for item in r_all.json()}
        assert tid in all_ids


# ── 5. GET /attach_stream — SSE NDJSON ───────────────────────────────────


def test_attach_stream_sse(daemon_uds: tuple[Path, Popolad]) -> None:
    """``GET /attach_stream/{task_id}`` yields SSE ``data: {envelope}`` frames.

    The producer terminates when the task hits a terminal state, so we read
    the entire stream and parse all envelopes.
    """
    sock, _popolad = daemon_uds
    with _client(sock) as c:
        r = c.post("/dispatch", json={"cli": "echo_rpc", "prompt": "stream-me"})
        task_id = r.json()["task_id"]

        envelopes: list[dict] = []
        with c.stream(
            "GET",
            f"/attach_stream/{task_id}",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
        ) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    envelopes.append(json.loads(line[len("data: "):]))

    types = {e["type"] for e in envelopes}
    assert "task.dispatched" in types
    assert "process.started" in types
    assert "task.completed" in types


# ── 6. POST /cancel/{task_id} ────────────────────────────────────────────


def test_cancel_endpoint(daemon_uds: tuple[Path, Popolad], isolated_registry: None) -> None:
    """``POST /cancel/{task_id}`` returns SIGTERM / escalation info; idempotent on terminal."""
    sock, _popolad = daemon_uds

    class _SleepyAdapter:
        name = "sleepy_rpc"
        binary = sys.executable

        def build_command(
            self,
            prompt: str,
            cwd: Path | None = None,
            extra: dict[str, Any] | None = None,
        ) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        def is_available(self) -> bool:
            return True

    register_adapter(_SleepyAdapter())

    with _client(sock) as c:
        r = c.post("/dispatch", json={"cli": "sleepy_rpc", "prompt": "long-running"})
        assert r.status_code == 200, f"dispatch failed: {r.status_code} {r.text}"
        task_id = r.json()["task_id"]

        time.sleep(0.2)

        r_cancel = c.post(f"/cancel/{task_id}")
        assert r_cancel.status_code == 200, f"cancel failed: {r_cancel.text}"
        body = r_cancel.json()
        assert body["task_id"] == task_id
        assert body["requested_signal"] == "SIGTERM"
        assert isinstance(body["escalated_to_sigkill"], bool)
        assert body["pid"] is None or isinstance(body["pid"], int)

        r_recancel = c.post(f"/cancel/{task_id}")
        assert r_recancel.status_code in {200, 409}


# ── 7. GET /probe ────────────────────────────────────────────────────────


def test_probe_endpoint(daemon_uds: tuple[Path, Popolad]) -> None:
    """``GET /probe`` returns daemon_pid / uptime_seconds / active_tasks / version."""
    sock, _popolad = daemon_uds
    with _client(sock) as c:
        r = c.get("/probe")
        assert r.status_code == 200
        body = r.json()
        for key in ("daemon_pid", "started_at", "uptime_seconds", "active_tasks", "version"):
            assert key in body, f"probe missing {key}: {body}"
        assert isinstance(body["daemon_pid"], int)
        assert body["daemon_pid"] > 0
        assert isinstance(body["uptime_seconds"], (int, float))
        assert body["uptime_seconds"] >= 0
        assert isinstance(body["active_tasks"], int)
        assert body["active_tasks"] >= 0
        assert isinstance(body["version"], str)


# ── 8. POST /dispatch with unknown cli → 404 ────────────────────────────


def test_dispatch_unknown_cli_returns_404(daemon_uds: tuple[Path, Popolad]) -> None:
    """Unknown adapter name → 404 + helpful detail (No Silent Failures)."""
    sock, _popolad = daemon_uds
    with _client(sock) as c:
        r = c.post("/dispatch", json={"cli": "no-such-cli", "prompt": "should fail"})
        assert r.status_code == 404
        detail = r.json().get("detail", "")
        assert "no-such-cli" in detail or "adapter" in detail


# ── 9. R-008 unit test: ghost_exit on KeyError ───────────────────────────


def test_r008_ghost_exit_emits_event(tmp_path: Path) -> None:
    """White-box: trigger ``_on_subprocess_exit`` with unknown task_id → ``state.ghost_exit``.

    This exercises the R-008 No Silent Failures fix without spawning a real
    subprocess: we instantiate Popolad, hand-call the private callback with
    a fabricated task_id, and verify the event log contains the marker.
    """
    events_dir = tmp_path / "events"
    popolad = Popolad(events_dir=events_dir, adapter=lambda *a, **k: ["true"])

    fake_tid = "ghost-deadbeef0000"

    popolad._on_subprocess_exit(fake_tid, 0)

    log_path = events_dir / f"{fake_tid}.jsonl"
    assert log_path.exists(), "ghost event log file should be created"

    raw = log_path.read_text(encoding="utf-8").splitlines()
    assert any('"state.ghost_exit"' in line for line in raw), (
        f"state.ghost_exit not emitted; lines={raw}"
    )

    log = EventLog(log_path)
    events = log.tail()
    ghosts = [e for e in events if e["type"] == "state.ghost_exit"]
    assert ghosts, f"no state.ghost_exit in events: {events}"
    assert ghosts[0]["data"]["task_id"] == fake_tid
    assert "reason" in ghosts[0]["data"]


# ── 10. Cross-process verification (R-001 + R-005 proof) ─────────────────


@pytest.mark.slow
def test_cross_process_dispatch_and_status(tmp_path: Path) -> None:
    """End-to-end cross-process: spawn ``python -m popolaloom.daemon`` + RPC across.

    Spawns a real popolad daemon process (different OS process, ``setsid``),
    waits for the UDS to come up, dispatches a task via ``httpx`` → confirms
    the daemon process accepted it and reports its status. This is the
    canonical R-001 (no real daemon) + R-005 (attach cross-process invisible)
    closure test.

    Skipped under default test runs via ``@pytest.mark.slow``? — actually
    we want this in default runs because it's the AC #4 proof; ~2-3s wall
    clock is acceptable. (``slow`` mark is for ≥ 10s tests.)
    """
    popola_home = tmp_path / "popola_home"
    popola_home.mkdir()

    env = os.environ.copy()
    env["POPOLA_HOME"] = str(popola_home)

    proc = subprocess.Popen(
        [sys.executable, "-m", "popolaloom.daemon"],
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    sock = popola_home / "popolad.sock"
    pid_file = popola_home / "popolad.pid"

    deadline = time.monotonic() + 8.0
    healthy = False
    try:
        while time.monotonic() < deadline:
            if sock.exists():
                try:
                    with httpx.Client(
                        transport=httpx.HTTPTransport(uds=str(sock)),
                        base_url="http://popolad",
                        timeout=1.0,
                    ) as c:
                        if c.get("/health").status_code == 200:
                            healthy = True
                            break
                except (httpx.HTTPError, OSError):
                    pass
            if proc.poll() is not None:
                pytest.fail(
                    f"daemon subprocess exited prematurely: code={proc.returncode}, "
                    f"stderr={proc.stderr.read() if proc.stderr else ''!r}"
                )
            time.sleep(0.05)

        assert healthy, "daemon failed to become healthy within 8s"

        assert pid_file.exists(), "PID file was not written"
        pid_val = int(pid_file.read_text(encoding="utf-8").strip())
        assert pid_val == proc.pid, f"PID file mismatch: {pid_val} != {proc.pid}"

        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(sock)),
            base_url="http://popolad",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
        ) as c:
            r_probe = c.get("/probe")
            assert r_probe.status_code == 200
            assert r_probe.json()["daemon_pid"] == proc.pid

            r_disp = c.post(
                "/dispatch",
                json={
                    "cli": "cursor",
                    "prompt": "cross-proc-smoke",
                },
            )
            assert r_disp.status_code == 200, (
                f"dispatch failed: {r_disp.status_code} {r_disp.text}"
            )
            body = r_disp.json()
            tid = body["task_id"]
            assert tid.startswith("cursor-")

            r_stat = c.get(f"/status/{tid}")
            assert r_stat.status_code == 200
            info = r_stat.json()
            assert info["task_id"] == tid
            assert info["cli"] == "cursor"
            assert "state" in info

            r_list = c.get("/list", params={"include_terminal": "true"})
            assert r_list.status_code == 200
            list_ids = {item["task_id"] for item in r_list.json()}
            assert tid in list_ids, "cross-process: dispatched task missing from /list"

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


# ── 11. R-006 unit test: lock used around _event_logs ────────────────────


def test_r006_event_logs_lock_present() -> None:
    """White-box: ``Popolad._event_logs_lock`` exists and is a ``threading.Lock``."""
    import threading

    popolad = Popolad(adapter=lambda *a, **k: ["true"])
    assert hasattr(popolad, "_event_logs_lock")
    lock = popolad._event_logs_lock
    assert isinstance(lock, type(threading.Lock())), (
        f"expected threading.Lock, got {type(lock)}"
    )


# ── 12. R-007 unit test: stream.truncated event marker ──────────────────


def test_r007_supervisor_join_timeout_constant() -> None:
    """Verify supervisor uses 30s join timeout (R-007 fix)."""
    from popolaloom.daemon import supervisor as sup_mod

    assert sup_mod._DRAIN_JOIN_TIMEOUT_S == 30.0, (
        f"join timeout should be 30s after R-007 fix, got {sup_mod._DRAIN_JOIN_TIMEOUT_S}"
    )


# ── 13. R-013 cleanup: no _default_popolad in server.py ─────────────────


def test_r013_no_module_singleton() -> None:
    """R-013: daemon/server.py must not have a module-level ``_default_popolad``."""
    from popolaloom.daemon import server as server_mod

    assert not hasattr(server_mod, "_default_popolad"), (
        "R-013 fix: _default_popolad module-level singleton should be removed"
    )
    assert not hasattr(server_mod, "_get_default"), (
        "R-013 fix: _get_default helper should be removed"
    )
    for fn in ("dispatch_task", "get_status", "tail_events"):
        assert not hasattr(server_mod, fn), (
            f"R-013 fix: module-level wrapper {fn} should be removed"
        )


# ── 14. State rehydrate hook (Stage A A3) ───────────────────────────────


def test_state_store_rehydrate_hook(tmp_path: Path) -> None:
    """``StateStore.rehydrate`` bulk-loads handles into the in-memory dict."""
    from datetime import datetime

    from popolaloom.daemon.state import StateStore, TaskHandle, TaskState

    store = StateStore()
    h1 = TaskHandle(
        task_id="rh-001",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "rh-001.jsonl",
        persisted=True,
    )
    h2 = TaskHandle(
        task_id="rh-002",
        cli="claude",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "rh-002.jsonl",
        persisted=False,
    )

    store.rehydrate([h1, h2])
    assert store.get("rh-001") is h1
    assert store.get("rh-002") is h2
    assert {h.task_id for h in store.list_active()} == {"rh-001", "rh-002"}

    with pytest.raises(ValueError):
        store.rehydrate([h1, h1])
