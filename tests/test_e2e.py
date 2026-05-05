"""End-to-end smoke tests for the dispatch path (v0.2.0 Stage A).

Verifies the full chain works as a unit:

    popola dispatch ...   →   FastAPI POST /dispatch (httpx UDS in prod;
                              httpx ASGITransport in tests)
                          →   Popolad.dispatch_task   →   adapter.build_command
                          →   Supervisor.spawn (subprocess.Popen, setsid)
                          →   stdout/stderr drain threads + wait thread
                          →   EventLog NDJSON (CloudEvents 1.0 envelopes)
                          →   StateStore terminal transition (COMPLETED)
                          →   popola status / popola attach can read it back

Three complementary entry points are exercised:

- :func:`test_e2e_dispatch_via_popolad_facade` — direct Python API
  (constructs :class:`Popolad` against ``tmp_path``, registers the echo
  adapter, dispatches, polls until terminal, asserts events/state). This is
  the closest mirror to how programmatic callers will use popolad.

- :func:`test_e2e_dispatch_via_typer_clirunner` — invokes the actual
  :data:`popolaloom.cli.main.app` via Typer's :class:`CliRunner` after
  monkey-patching :func:`popolaloom.cli.main.make_sync_client` to use
  :class:`httpx.ASGITransport` against an in-process FastAPI app. This
  exercises the full HTTP-over-UDS contract without spawning a real
  daemon process (cross-process daemon path is exercised in
  ``test_daemon_rpc.py``).

- :func:`test_e2e_dispatch_failed_path` — bonus: dispatches an adapter that
  exits non-zero, asserts state transitions to FAILED with exit_code != 0
  and a ``task.failed`` terminal event.

All three tests:

- Snapshot + restore the global adapter registry to avoid bleeding ``echo``
  into other test modules (mirrors the ``isolated_registry`` pattern in
  ``test_adapters.py``).
- Use ``tmp_path`` for events_dir (never touches the user's
  ``~/.popola/events/``).
- Bound polling to ≤ 5s with explicit ``pytest.fail`` on timeout (No Silent
  Failures + avoids the 13-hour subprocess-hang trap from prior iteration).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from popolaloom.adapters import base as adapter_base
from popolaloom.adapters import build_command, register_adapter
from popolaloom.cli import main as cli_main
from popolaloom.daemon import Popolad
from popolaloom.daemon.rpc import create_app

# 任何 task 终态字符串集合 (与 TaskState StrEnum.value 对齐)
_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "canceled"})

# poll 上限 (秒). 5s 对秒退 python 子进程足够; 超时立即 pytest.fail 避免无限挂起
_POLL_TIMEOUT_S: float = 5.0
_POLL_INTERVAL_S: float = 0.05


# ── helpers + fixtures ───────────────────────────────────────────────────


class _EchoAdapter:
    """Test-only adapter — spawns ``python -c "print('echo:', <prompt>)"``."""

    name = "echo"
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
        return shutil.which(sys.executable) is not None or Path(sys.executable).exists()


class _FailingAdapter:
    """Test-only adapter that always exits with code 7 (no stdout)."""

    name = "echo_fail"
    binary = sys.executable

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(7)"]

    def is_available(self) -> bool:
        return shutil.which(sys.executable) is not None or Path(sys.executable).exists()


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Snapshot + restore global ``_REGISTRY`` (mirrors test_adapters.py pattern).

    Critical for two reasons:
    1. Re-registering the same name raises ``ValueError`` (No Silent Failures).
    2. Leaking the ``echo`` adapter would let later tests accidentally rely on
       it, masking bugs where production code expects only cursor/claude/codex.
    """
    saved = dict(adapter_base._REGISTRY)
    try:
        yield
    finally:
        adapter_base._REGISTRY.clear()
        adapter_base._REGISTRY.update(saved)


def _wait_terminal(popolad: Popolad, task_id: str) -> dict:
    """Poll ``get_status`` until terminal or timeout; pytest.fail on hang."""
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        last = popolad.get_status(task_id)
        if last["state"] in _TERMINAL_STATES:
            return last
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"task {task_id} did not reach terminal state in {_POLL_TIMEOUT_S}s; "
        f"last status={last}"
    )


# ── E2E 1: direct Python API (popolad_factory fixture) ───────────────────


def test_e2e_dispatch_via_popolad_facade(
    popolad_factory: Callable[..., Popolad],
    isolated_registry: None,
    tmp_path: Path,
) -> None:
    """End-to-end: register echo → dispatch → wait → assert state + events file.

    Uses :func:`popolad_factory` from ``tests/conftest.py`` to get a
    tmp_path-backed Popolad, but overrides the default noop adapter with the
    real :func:`build_command` facade so the registry path is exercised
    (instead of the conftest's bypass-the-registry direct-callback path).
    """
    register_adapter(_EchoAdapter())

    events_dir = tmp_path / "events"
    popolad = popolad_factory(events_dir, adapter=build_command)

    task_id = popolad.dispatch_task(cli="echo", prompt="hello world")
    assert task_id.startswith("echo-"), f"task_id should be prefixed with cli: {task_id}"

    status = _wait_terminal(popolad, task_id)
    assert status["state"] == "completed", f"final status: {status}"
    assert status["exit_code"] == 0
    assert status["completed_at"] is not None
    assert status["pid"] is not None and status["pid"] > 0

    events = popolad.tail_events(task_id)
    types = [ev["type"] for ev in events]
    assert types[0] == "task.dispatched"
    assert "process.started" in types
    assert "process.stdout" in types
    assert "task.completed" in types

    stdout_events = [ev for ev in events if ev["type"] == "process.stdout"]
    assert any("hello world" in (ev["data"].get("line") or "") for ev in stdout_events), (
        f"no stdout event contained 'hello world': "
        f"{[ev['data'] for ev in stdout_events]}"
    )

    events_file = events_dir / f"{task_id}.jsonl"
    assert events_file.exists(), f"NDJSON file missing: {events_file}"
    raw_lines = events_file.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == len(events), "tail_events / on-disk count mismatch"
    for line in raw_lines:
        envelope = json.loads(line)
        assert envelope["specversion"] == "1.0"
        assert envelope["id"].startswith("evt-")
        assert envelope["source"] == f"popola/{task_id}"
        assert envelope["time"].endswith("Z")
        assert "data" in envelope


# ── E2E 2: invoke the actual Typer app via CliRunner ─────────────────────


@pytest.fixture
def in_thread_daemon(
    tmp_path: Path,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Run a popolad uvicorn UDS server in a background thread for the test.

    Yields the socket path; the CLI's :func:`make_sync_client` is monkey-patched
    to bind to this socket via real UDS (proves the same code path as the
    production ``python -m popolaloom.daemon`` flow without spawning a
    separate Python interpreter).

    Cleanup: signals the server to stop, joins thread, removes UDS file.
    """
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

    def _run() -> None:
        try:
            asyncio.run(server.serve())
        except Exception as exc:  # pragma: no cover - debug aid
            pytest.fail(f"uvicorn thread crashed: {exc!r}")

    thread = threading.Thread(target=_run, daemon=True, name="popolad-uvicorn-test")
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
        time.sleep(0.05)
    else:
        pytest.fail("uvicorn UDS server did not become healthy within 5s")

    def _make_test_sync_client(_socket_path: Path | None = None) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=str(sock)),
            base_url="http://popolad",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
        )

    monkeypatch.setattr(cli_main, "make_sync_client", _make_test_sync_client)

    try:
        yield sock
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        try:
            if sock.exists():
                sock.unlink()
        except OSError:
            pass


def test_e2e_dispatch_via_typer_clirunner(
    in_thread_daemon: Path,
) -> None:
    """End-to-end through the real ``popola`` Typer app (dispatch → status → attach).

    v0.2.0 strategy:

    - Spin up a uvicorn UDS server in a background thread bound to a
      tmp_path UDS (see :func:`in_thread_daemon` fixture).
    - The CLI's :func:`make_sync_client` is monkey-patched to bind to that
      UDS, so each CliRunner invocation crosses the real HTTP-over-UDS
      boundary — exercising the FastAPI app + Pydantic schemas + UDS
      transport + httpx client end-to-end.
    - Invoke ``dispatch ... --wait --json``, parse task_id, then ``status``
      / ``attach --no-follow`` / ``list`` against the same task.
    """
    runner = CliRunner()

    r_disp = runner.invoke(
        cli_main.app,
        ["dispatch", "hello cli", "--cli", "echo", "--wait", "--timeout", "5", "--json"],
    )
    assert r_disp.exit_code == 0, (
        f"dispatch exit={r_disp.exit_code}, stdout={r_disp.stdout!r}, "
        f"exc={r_disp.exception!r}"
    )
    payload = json.loads(r_disp.stdout.strip().splitlines()[-1])
    task_id = payload["task_id"]
    assert task_id.startswith("echo-")
    assert payload["cli"] == "echo"
    assert payload["events_log"].endswith(f"{task_id}.jsonl")
    assert Path(payload["events_log"]).exists()

    r_stat = runner.invoke(cli_main.app, ["status", task_id, "--json"])
    assert r_stat.exit_code == 0, f"status exit={r_stat.exit_code}, out={r_stat.stdout!r}"
    info = json.loads(r_stat.stdout.strip().splitlines()[-1])
    assert info["state"] == "completed"
    assert info["exit_code"] == 0
    assert info["latest_event_index"] >= 4

    r_att = runner.invoke(cli_main.app, ["attach", task_id, "--no-follow"])
    assert r_att.exit_code == 0, (
        f"attach exit={r_att.exit_code}, out={r_att.stdout!r}, exc={r_att.exception!r}"
    )
    assert "task.dispatched" in r_att.stdout
    assert "process.stdout" in r_att.stdout
    assert "echo: hello cli" in r_att.stdout
    assert "task.completed" in r_att.stdout

    r_list = runner.invoke(cli_main.app, ["list", "--json"])
    assert r_list.exit_code == 0
    listed = json.loads(r_list.stdout.strip().splitlines()[-1] or "[]")
    assert all(item["task_id"] != task_id for item in listed), (
        f"terminal task should not appear in list_active: {listed}"
    )


# ── E2E 3 (bonus): failure path ──────────────────────────────────────────


def test_e2e_dispatch_failed_path(
    popolad_factory: Callable[..., Popolad],
    isolated_registry: None,
    tmp_path: Path,
) -> None:
    """Failure path: adapter exits 7 → state == FAILED + task.failed event."""
    register_adapter(_FailingAdapter())

    events_dir = tmp_path / "events_fail"
    popolad = popolad_factory(events_dir, adapter=build_command)

    task_id = popolad.dispatch_task(cli="echo_fail", prompt="will-fail")
    status = _wait_terminal(popolad, task_id)

    assert status["state"] == "failed", f"expected FAILED, got {status}"
    assert status["exit_code"] == 7
    assert status["completed_at"] is not None

    events = popolad.tail_events(task_id)
    types = [ev["type"] for ev in events]
    assert "task.dispatched" in types
    assert "process.started" in types
    assert "task.failed" in types
    assert "task.completed" not in types, "completed must not appear on failure path"

    failed_event = next(ev for ev in events if ev["type"] == "task.failed")
    assert failed_event["data"]["exit_code"] == 7
    assert failed_event["data"]["task_id"] == task_id
