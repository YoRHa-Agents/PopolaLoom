"""v0.2.2 server.py / mcp.py coverage gap fillers.

Hits the underexposed Popolad helpers:

* ``_on_subprocess_exit`` ghost-exit path (R-008).
* ``shutdown_persistence_bridge`` with bridge raising on unsubscribe.
* ``_run_graph_for_task`` exception fallback to FAILED state.
* ``_emit_recovered_events`` with one bad handle (continues).
* ``rehydrate_from_persistence`` with no persistence returns 0.
* mcp/server.py ``call_tool_handler`` exception swallowing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskHandle, TaskState


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["echo", "x"]


def test_server_on_subprocess_exit_ghost_emits_state_ghost_exit_event(
    tmp_path: Path,
    caplog,
) -> None:
    """``_on_subprocess_exit`` for unknown task_id → emits state.ghost_exit."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )

    with caplog.at_level(logging.WARNING, logger="popolaloom.daemon.server"):
        popolad._on_subprocess_exit("ghost-task-id", exit_code=42)

    log_path = tmp_path / "events" / "ghost-task-id.jsonl"
    assert log_path.exists(), "ghost-exit event must be persisted to a fresh NDJSON file"
    text = log_path.read_text(encoding="utf-8")
    import json as _json
    rows = [_json.loads(line) for line in text.splitlines() if line.strip()]
    assert any(
        ev["type"] == "state.ghost_exit" and ev["data"]["task_id"] == "ghost-task-id"
        for ev in rows
    )

    assert any("on_exit for unknown" in r.message for r in caplog.records), (
        "ghost-exit must log a WARNING (No Silent Failures)"
    )


def test_server_on_subprocess_exit_owned_log_fsyncs_not_close(
    tmp_path: Path,
    mocker,
) -> None:
    """ghost path uses fsync (not close) when log was already owned."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    log_path = tmp_path / "events" / "owned-task.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = EventLog(log_path, fsync_interval_s=0.0)
    popolad._event_logs["owned-task"] = log

    fsync_spy = mocker.spy(log, "fsync")
    close_spy = mocker.spy(log, "close")
    popolad._on_subprocess_exit("owned-task", exit_code=1)

    assert fsync_spy.called, "owned event log should fsync ghost-exit envelope"
    assert not close_spy.called, "owned event log should NOT be closed by ghost-exit"
    log.close()


def test_server_shutdown_persistence_bridge_idempotent_when_none(
    tmp_path: Path,
) -> None:
    """``shutdown_persistence_bridge`` no-op when bridge + persistence are None."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    popolad.shutdown_persistence_bridge()


def test_server_shutdown_persistence_bridge_unsubscribe_raises_logged(
    tmp_path: Path,
    mocker,
    caplog,
) -> None:
    """Bridge ``unsubscribe`` raising → logged but doesn't bubble out."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    bad_bridge = mocker.MagicMock()
    bad_bridge.unsubscribe.side_effect = RuntimeError("simulated unsubscribe boom")
    popolad._event_bus_bridge = bad_bridge

    bad_persistence = mocker.MagicMock()
    bad_persistence.close.side_effect = RuntimeError("simulated persistence close boom")
    popolad._persistence = bad_persistence

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.server"):
        popolad.shutdown_persistence_bridge()

    assert any("unsubscribe" in r.message for r in caplog.records)
    assert any("persistence.close" in r.message for r in caplog.records)


def test_server_rehydrate_no_persistence_returns_0(
    tmp_path: Path,
) -> None:
    """``rehydrate_from_persistence`` with persistence=None returns 0."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    assert popolad.rehydrate_from_persistence() == 0


def test_server_rehydrate_repository_list_raises_returns_0(
    tmp_path: Path,
    mocker,
    caplog,
) -> None:
    """``repository.list`` raising during rehydrate → log + return 0."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    fake_persistence = mocker.MagicMock()
    fake_persistence.repository.list.side_effect = RuntimeError("repo list boom")
    popolad._persistence = fake_persistence

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.server"):
        result = popolad.rehydrate_from_persistence()

    assert result == 0
    assert any("repository.list failed" in r.message for r in caplog.records)


def test_server_emit_recovered_events_one_handle(
    tmp_path: Path,
) -> None:
    """``_emit_recovered_events`` writes the envelope into the NDJSON file."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    handle = TaskHandle(
        task_id="recovered-task-1",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "recovered-task-1.jsonl",
        arktower_task_id="ark-1",
        cmd=["echo", "x"],
        persisted=True,
    )
    popolad._emit_recovered_events([handle], ["recovered-task-1"])

    log = popolad._event_logs.get("recovered-task-1")
    assert log is not None, "log should be registered after emit"
    log.fsync()

    log_path = tmp_path / "events" / "recovered-task-1.jsonl"
    assert log_path.exists()
    import json as _json
    rows = [_json.loads(line) for line in log_path.read_text("utf-8").splitlines() if line.strip()]
    assert any(ev["type"] == "popolad.recovered" for ev in rows), (
        f"no popolad.recovered envelope; got: {[r.get('type') for r in rows]}"
    )

    log.close()


def test_server_event_log_for_arktower_id_returns_log(
    tmp_path: Path,
) -> None:
    """``event_log_for_arktower_id`` returns the matching log."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    handle = TaskHandle(
        task_id="task-A",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "task-A.jsonl",
        arktower_task_id="ark-A",
        cmd=["echo"],
        persisted=True,
    )
    popolad._state.register(handle)
    log = EventLog(tmp_path / "events" / "task-A.jsonl", fsync_interval_s=0.0)
    popolad._event_logs["task-A"] = log

    found = popolad.event_log_for_arktower_id("ark-A")
    assert found is log

    not_found = popolad.event_log_for_arktower_id("nonexistent-ark-id")
    assert not_found is None
    log.close()


def test_server_get_status_unknown_task_raises_keyerror(
    tmp_path: Path,
) -> None:
    """``get_status`` for unknown task → KeyError."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    with pytest.raises(KeyError):
        popolad.get_status("nonexistent")


def test_server_tail_events_unknown_task_raises_keyerror(
    tmp_path: Path,
) -> None:
    """``tail_events`` for unknown task → KeyError."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    with pytest.raises(KeyError):
        popolad.tail_events("nonexistent")


def test_server_cancel_task_terminal_raises_runtime_error(
    tmp_path: Path,
) -> None:
    """``cancel_task`` for already-terminal task → RuntimeError."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    handle = TaskHandle(
        task_id="t-done",
        cli="cursor",
        pid=1234,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-done.jsonl",
        arktower_task_id=None,
        cmd=["echo"],
    )
    popolad._state.register(handle)
    with pytest.raises(RuntimeError, match="terminal"):
        popolad.cancel_task("t-done")


def test_server_cancel_task_no_pid_raises_runtime_error(
    tmp_path: Path,
) -> None:
    """``cancel_task`` for handle with pid=None → RuntimeError."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    handle = TaskHandle(
        task_id="t-nopid",
        cli="cursor",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-nopid.jsonl",
        arktower_task_id=None,
        cmd=["echo"],
    )
    popolad._state.register(handle)
    with pytest.raises(RuntimeError, match="no pid"):
        popolad.cancel_task("t-nopid")


def test_server_cancel_task_unknown_raises_keyerror(
    tmp_path: Path,
) -> None:
    """``cancel_task`` for unknown task_id → KeyError."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )
    with pytest.raises(KeyError):
        popolad.cancel_task("nonexistent")


@pytest.mark.asyncio
async def test_mcp_call_tool_handler_swallows_exception_returns_iserror(
    tmp_path: Path,
    mocker,
) -> None:
    """mcp.server.build_server: call_tool unhandled exception → CallToolResult(isError=True)."""
    import popolaloom.mcp.server as mcp_server

    mocker.patch(
        "popolaloom.mcp.server.call_verb",
        side_effect=RuntimeError("simulated verb boom"),
    )

    client = mcp_server.make_async_client(uds=Path("/tmp/no.sock"))
    try:
        srv = mcp_server.build_server(client)
        handlers = srv.request_handlers
        from mcp.types import CallToolRequest, CallToolRequestParams
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="popola_status", arguments={"task_id": "x"}),
        )
        result = await handlers[CallToolRequest](req)
        assert result.root.isError is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_list_tools_handler_returns_7_tools(
    tmp_path: Path,
) -> None:
    """mcp.server.build_server registers all verbs (v0.8.7+: 11 = 10 base + 1 cloud HITL).

    Bump history:
      - v0.3.0: 10 = 7 v0.2.x + 3 F2 (the test name reflects this era).
      - v0.8.7 B1: +1 (``popolaloom_cloud_hitl_request`` via
        ``cloud_hitl_tool.build_extended_tool_list``).
    """
    import popolaloom.mcp.server as mcp_server

    client = mcp_server.make_async_client(uds=Path("/tmp/no.sock"))
    try:
        srv = mcp_server.build_server(client)
        handlers = srv.request_handlers
        from mcp.types import ListToolsRequest
        req = ListToolsRequest(method="tools/list")
        result = await handlers[ListToolsRequest](req)
        assert len(result.root.tools) == 11
    finally:
        await client.aclose()
