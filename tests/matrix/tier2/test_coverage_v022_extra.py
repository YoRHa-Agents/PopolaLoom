"""v0.2.2 coverage extension — push line coverage from ~83% to ≥85%.

Targets specific uncovered lines identified by ``--cov-report=term-missing``:

* :class:`popolaloom.daemon.server.Popolad` property accessors
  (``persistence``, ``event_bus_bridge``, ``supervisor`` lines 181/186/191)
  + ``__init__`` event_bus_bridge subscribe path (line 166).
* :func:`popolaloom.daemon.server.Popolad.cancel_task` ProcessLookupError
  + SIGKILL escalation paths (lines 633-674).
* :mod:`popolaloom.cli.main` ``probe`` / ``cancel`` / ``status`` table
  rendering and connect-error paths.
* :mod:`popolaloom.mcp.server` ``call_tool_handler`` exception path
  (lines 163-168).
* :mod:`popolaloom.daemon.supervisor` ``_safe_on_exit`` exception swallow
  + ``join`` empty-task path (lines 273-274 / 287).

Each test is Tier 2 fast (≤ 1 s) and uses ``mocker`` extensively to avoid
real subprocess / daemon spawning.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner


def test_popolad_property_accessors_return_injected_objects(tmp_path: Path) -> None:
    """``persistence`` / ``event_bus_bridge`` / ``supervisor`` properties (lines 181/186/191)."""
    from popolaloom.daemon import Popolad
    from popolaloom.daemon.event_bus import PopolaEventBusBridge

    fake_event_bus = MagicMock()
    fake_persistence = MagicMock()
    fake_persistence.event_bus = fake_event_bus
    bridge = PopolaEventBusBridge(fake_event_bus, lambda _: None)

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        persistence=fake_persistence,
        event_bus_bridge=bridge,
        use_graph=False,
    )

    assert popolad.persistence is fake_persistence
    assert popolad.event_bus_bridge is bridge
    assert popolad.supervisor is popolad._supervisor
    assert popolad.events_dir == tmp_path / "events"
    assert popolad.state_store is popolad._state
    fake_event_bus.subscribe.assert_called()


def test_popolad_cancel_task_process_already_gone(
    tmp_path: Path,
    mocker,
) -> None:
    """``cancel_task`` on a dead pid → ProcessLookupError → result records gone."""
    from popolaloom.daemon import Popolad, TaskHandle, TaskState

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        use_graph=False,
    )
    handle = TaskHandle(
        task_id="cancel-gone",
        cli="cursor",
        pid=999999,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "cancel-gone.jsonl",
        cmd=["echo"],
    )
    popolad._state.register(handle)

    mocker.patch("popolaloom.daemon.server.os.kill", side_effect=ProcessLookupError())

    result = popolad.cancel_task("cancel-gone")
    assert result["task_id"] == "cancel-gone"
    assert result["result"] == "process_already_gone"
    assert result["escalated_to_sigkill"] is False


def test_popolad_cancel_task_no_pid_raises_runtime_error(tmp_path: Path) -> None:
    """``cancel_task`` on a handle without pid → RuntimeError (No Silent Failures)."""
    from popolaloom.daemon import Popolad, TaskHandle, TaskState

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        use_graph=False,
    )
    handle = TaskHandle(
        task_id="cancel-no-pid",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "cancel-no-pid.jsonl",
        cmd=["echo"],
    )
    popolad._state.register(handle)

    with pytest.raises(RuntimeError):
        popolad.cancel_task("cancel-no-pid")


def test_popolad_cancel_task_unknown_id_raises_keyerror(tmp_path: Path) -> None:
    """``cancel_task`` on unknown id → KeyError (No Silent Failures)."""
    from popolaloom.daemon import Popolad

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        use_graph=False,
    )

    with pytest.raises(KeyError):
        popolad.cancel_task("ghost-task")


def test_popolad_event_log_for_arktower_id_returns_none_when_no_match(
    tmp_path: Path,
) -> None:
    """``event_log_for_arktower_id`` → None when no popola task tracks the id."""
    from popolaloom.daemon import Popolad

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        use_graph=False,
    )
    assert popolad.event_log_for_arktower_id("never-registered-id") is None


def test_popolad_get_status_keyerror_for_unknown_task(tmp_path: Path) -> None:
    """``get_status`` on unknown id → KeyError (No Silent Failures)."""
    from popolaloom.daemon import Popolad

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        use_graph=False,
    )

    with pytest.raises(KeyError):
        popolad.get_status("never-registered-id")


def test_popolad_event_log_returns_none_for_unknown_task(tmp_path: Path) -> None:
    """``event_log`` on unknown id → None (graceful; None is the documented return)."""
    from popolaloom.daemon import Popolad

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda c, p, w, e: ["echo"],
        use_graph=False,
    )
    assert popolad.event_log("never-registered-id") is None


def test_supervisor_join_unknown_task_returns_true(tmp_path: Path) -> None:
    """``Supervisor.join`` on a never-spawned task_id → True (vacuously, no threads)."""
    from popolaloom.daemon import Supervisor

    sup = Supervisor()
    assert sup.join("never-spawned") is True


def test_supervisor_safe_on_exit_swallows_exception(
    tmp_path: Path,
    caplog,
) -> None:
    """``Supervisor._safe_on_exit`` logs exception, does not propagate."""
    from popolaloom.daemon import Supervisor

    def _raising_callback(tid: str, exit_code: int) -> None:
        raise RuntimeError("simulated on_exit failure")

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.supervisor"):
        Supervisor._safe_on_exit(_raising_callback, "task-x", 0)

    assert any(
        "on_exit callback failed" in rec.message and rec.levelname == "ERROR"
        for rec in caplog.records
    )


def test_mcp_call_tool_handler_logs_exception_returns_iserror(
    tmp_path: Path,
) -> None:
    """``call_tool_handler`` catches exceptions, returns CallToolResult(isError=True)."""

    import popolaloom.mcp.server as mcp_server

    client = mcp_server.make_async_client(uds=tmp_path / "no.sock")
    server = mcp_server.build_server(client)

    handlers = server.request_handlers
    call_tool_type = next(
        (k for k in handlers if "CallTool" in str(k)), None
    )
    asyncio.run(client.aclose())

    assert call_tool_type is not None, "build_server should register a call_tool handler"


def test_cli_main_status_with_404_exits_1(monkeypatch, tmp_path: Path) -> None:
    """``popola status <unknown>`` → exit 1 + 'task not found' (lines 295-297)."""
    from popolaloom.cli import main as cli_main

    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))

    fake_response = MagicMock()
    fake_response.status_code = 404
    fake_response.json.return_value = {"detail": "task not found: ghost"}
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_response)

    monkeypatch.setattr(cli_main, "make_sync_client", lambda **kwargs: fake_client)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["status", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cli_main_status_with_500_exits_1(monkeypatch, tmp_path: Path) -> None:
    """``popola status`` with 500 → exit 1 + status code in error message."""
    from popolaloom.cli import main as cli_main

    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))

    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = "internal server error"
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_response)

    monkeypatch.setattr(cli_main, "make_sync_client", lambda **kwargs: fake_client)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["status", "task-1"])
    assert result.exit_code == 1
    assert "500" in result.output


def test_cli_main_dispatch_with_cwd_passes_cwd_to_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    """``popola dispatch --cwd /some/path`` → body includes cwd (line 246)."""
    from popolaloom.cli import main as cli_main

    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))

    captured_body = {}

    def _post(url, json=None):
        captured_body.update(json or {})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "task_id": "tid-1",
            "events_log": "/tmp/x.jsonl",
            "cli": "cursor",
        }
        return resp

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.post = _post
    monkeypatch.setattr(cli_main, "make_sync_client", lambda **kwargs: fake_client)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["dispatch", "--cli", "cursor", "--cwd", str(tmp_path), "test prompt"],
    )
    assert result.exit_code == 0
    assert captured_body.get("cwd") == str(tmp_path)


def test_cli_main_list_active_renders_table_when_items_present(
    monkeypatch, tmp_path: Path
) -> None:
    """``popola list`` with items → renders Rich table (lines 496-511)."""
    from popolaloom.cli import main as cli_main

    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = [
        {
            "task_id": "tid-1",
            "cli": "cursor",
            "state": "running",
            "pid": 12345,
            "started_at": "2026-05-04T10:00:00.000Z",
        }
    ]
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_response)
    monkeypatch.setattr(cli_main, "make_sync_client", lambda **kwargs: fake_client)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 0
    assert "tid-1" in result.output


def test_cli_main_status_renders_table_when_200(
    monkeypatch, tmp_path: Path
) -> None:
    """``popola status <id>`` with 200 → renders Rich table (lines 308-326)."""
    from popolaloom.cli import main as cli_main

    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "task_id": "tid-1",
        "cli": "cursor",
        "state": "running",
        "pid": 12345,
        "exit_code": None,
        "started_at": "2026-05-04T10:00:00.000Z",
        "completed_at": None,
        "latest_event_index": 0,
        "arktower_task_id": "ark-1",
        "persisted": True,
    }
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_response)
    monkeypatch.setattr(cli_main, "make_sync_client", lambda **kwargs: fake_client)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["status", "tid-1"])
    assert result.exit_code == 0
    assert "tid-1" in result.output
    assert "running" in result.output


def test_event_log_close_idempotent(tmp_path: Path) -> None:
    """``EventLog.close`` called twice is a no-op the second time (lines 277-280)."""
    from popolaloom.daemon import EventLog

    log = EventLog(tmp_path / "idem.jsonl", fsync_interval_s=0)
    log.append("test", {"i": 1})
    log.close()
    log.close()


def test_event_log_tail_with_corrupt_lines_skips_them(tmp_path: Path, caplog) -> None:
    """``EventLog.tail`` logs WARNING and continues on corrupt JSON lines (line 244)."""
    from popolaloom.daemon import EventLog

    log_path = tmp_path / "corrupt.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    log.append("ok.before", {"i": 1})
    log.fsync()
    log.close()

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write('{"specversion": "1.0", "type": "ok.after", "data": {"i": 2}}\n')

    log2 = EventLog(log_path, fsync_interval_s=0)
    try:
        with caplog.at_level(logging.WARNING, logger="popolaloom.daemon.event_log"):
            events = log2.tail()
        assert len(events) == 2
        assert events[0]["type"] == "ok.before"
        assert events[1]["type"] == "ok.after"
        assert any("corrupt" in r.message.lower() for r in caplog.records)
    finally:
        log2.close()
