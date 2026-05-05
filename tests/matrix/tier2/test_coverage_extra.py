"""Tier 2 / Coverage — extra surgical tests pushing daemon/server, mcp/tools,
cli/main, daemon/event_log, evaluation paths past 80% line coverage.

Each case is fast (<200 ms) and independent; no real subprocess except
where the SUT requires it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main
from popolaloom.daemon import EventLog, Popolad, TaskHandle, TaskState
from popolaloom.daemon.event_log import EventLog as _EventLog
from popolaloom.mcp import tools as mcp_tools


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[Path | None], httpx.Client]:
    def _factory(_path: Path | None = None) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://popolad",
            timeout=5.0,
        )

    return _factory


# ── popola_attach_stream snapshot path with mock SSE events ──────────────


def test_popola_attach_stream_snapshot_returns_recent_events() -> None:
    """attach_stream consumes mock SSE frames and returns them as a snapshot."""
    frames = [
        {"type": "task.dispatched", "data": {"x": 1}},
        {"type": "process.stdout", "data": {"line": "hi"}},
        {"type": "task.completed", "data": {"exit_code": 0}},
    ]
    sse_payload = b"".join(
        b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/status/"):
            return httpx.Response(
                200,
                json={"task_id": "tid", "state": "completed", "latest_event_index": 3},
            )
        if req.url.path.startswith("/attach_stream/"):
            return httpx.Response(
                200,
                content=sse_payload,
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(404)

    async def run() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://popolad",
        )
        try:
            return await mcp_tools.popola_attach_stream(
                client, {"task_id": "tid", "last_n": 5}
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["task_id"] == "tid"
    assert payload["count"] >= 1
    types = [e.get("type") for e in payload["events"]]
    assert "task.dispatched" in types or "task.completed" in types


def test_popola_attach_stream_status_500_returns_error() -> None:
    """attach_stream when status endpoint returns 500 → http_error path."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def run() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://popolad"
        )
        try:
            return await mcp_tools.popola_attach_stream(
                client, {"task_id": "tid", "last_n": 5}
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


def test_popola_supply_feedback_returns_not_implemented() -> None:
    """popola_supply_feedback returns isError=True with v0.3.0 deferred message."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_supply_feedback(client, {"task_id": "t"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True
    assert "v0.3.0" in result.content[0].text


def test_popola_inject_subtask_returns_not_implemented() -> None:
    """popola_inject_subtask returns isError=True with v0.3.0 F2 deferred message."""

    async def run() -> Any:
        client = httpx.AsyncClient(base_url="http://x")
        try:
            return await mcp_tools.popola_inject_subtask(client, {})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


def test_popola_cancel_409_idempotent_success() -> None:
    """popola_cancel 409 → success with already_terminal=True (idempotent semantics)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "task already terminal"})

    async def run() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://popolad"
        )
        try:
            return await mcp_tools.popola_cancel(client, {"task_id": "tid"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["already_terminal"] is True


def test_popola_cancel_500_returns_error() -> None:
    """popola_cancel 500 → http_error path."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def run() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://popolad"
        )
        try:
            return await mcp_tools.popola_cancel(client, {"task_id": "tid"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.isError is True


# ── cli/main.py attach paths via mock httpx ──────────────────────────────


def test_cli_attach_one_shot_404_status_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``popola attach --no-follow`` on missing task → exit 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/status/"):
            return httpx.Response(404, json={"detail": "missing"})
        return httpx.Response(500)

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["attach", "missing", "--no-follow"])
    assert r.exit_code == 1


def test_cli_attach_streaming_404_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``popola attach`` (default --follow) on missing task → exit 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/status/"):
            return httpx.Response(404, json={"detail": "missing"})
        return httpx.Response(500)

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["attach", "missing"])
    assert r.exit_code == 1


# ── EventLog.tail with corrupt NDJSON line ────────────────────────────────


def test_event_log_tail_skips_corrupt_lines(tmp_path: Path) -> None:
    """tail() skips lines that fail JSON decode (logs warning)."""
    log_path = tmp_path / "corrupt.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    log.append("a", {"k": 1})
    log.close()

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("{not-valid-json\n")
        fh.write(json.dumps({"specversion": "1.0", "id": "evt-x", "type": "b", "data": {}}) + "\n")

    log2 = EventLog(log_path, fsync_interval_s=0)
    try:
        events = log2.tail()
        assert len(events) >= 2
        assert any(e.get("type") == "a" for e in events)
        assert any(e.get("type") == "b" for e in events)
    finally:
        log2.close()


def test_event_log_tail_handles_missing_file(tmp_path: Path) -> None:
    """tail() returns [] when the file doesn't exist (rare race window)."""
    log = EventLog(tmp_path / "x.jsonl", fsync_interval_s=0)
    try:
        log.path.unlink()
        result = log.tail()
        assert result == []
    finally:
        log.close()


def test_event_log_len_handles_missing_file(tmp_path: Path) -> None:
    """__len__ returns 0 when the file doesn't exist."""
    log = EventLog(tmp_path / "n.jsonl", fsync_interval_s=0)
    try:
        log.path.unlink()
        assert len(log) == 0
    finally:
        log.close()


def test_event_log_fsync_after_close_is_no_op(tmp_path: Path) -> None:
    """After close(), fsync() is a no-op (doesn't raise)."""
    log = EventLog(tmp_path / "f.jsonl", fsync_interval_s=0)
    log.close()
    log.fsync()


# ── Popolad cancel error paths ───────────────────────────────────────────


def test_cancel_unknown_task_id_raises_keyerror(tmp_path: Path) -> None:
    """cancel_task on an unknown id raises KeyError."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable], use_graph=False)
    with pytest.raises(KeyError, match="not found"):
        pop.cancel_task("ghost")


def test_cancel_terminal_task_raises_runtime_error(tmp_path: Path) -> None:
    """cancel_task on a terminal task raises RuntimeError."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable], use_graph=False)
    from datetime import UTC, datetime

    handle = TaskHandle(
        task_id="done",
        cli="x",
        pid=42,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "done.jsonl",
    )
    pop.state_store.register(handle)
    with pytest.raises(RuntimeError, match="already in terminal"):
        pop.cancel_task("done")


def test_cancel_no_pid_raises_runtime_error(tmp_path: Path) -> None:
    """cancel_task on a task with no pid yet raises RuntimeError."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable], use_graph=False)
    from datetime import UTC, datetime

    handle = TaskHandle(
        task_id="nopid",
        cli="x",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "nopid.jsonl",
    )
    pop.state_store.register(handle)
    with pytest.raises(RuntimeError, match="no pid yet"):
        pop.cancel_task("nopid")


def test_get_status_unknown_task_raises_keyerror(tmp_path: Path) -> None:
    """get_status on an unknown id raises KeyError."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    with pytest.raises(KeyError, match="not found"):
        pop.get_status("ghost")


def test_tail_events_unknown_task_raises_keyerror(tmp_path: Path) -> None:
    """tail_events on an unknown id raises KeyError."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    with pytest.raises(KeyError, match="not found"):
        pop.tail_events("ghost")


def test_event_log_for_arktower_id_returns_none_when_no_match(tmp_path: Path) -> None:
    """event_log_for_arktower_id returns None when no popola task tracks the id."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    assert pop.event_log_for_arktower_id("does-not-exist") is None


def test_list_active_returns_empty_when_no_tasks(tmp_path: Path) -> None:
    """list_active() returns empty list on a fresh Popolad."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    assert pop.list_active() == []
    assert pop.list_all() == []


def test_list_all_with_include_terminal_includes_completed(tmp_path: Path) -> None:
    """list_all(include_terminal=True) includes completed tasks."""
    from datetime import UTC, datetime

    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    handle = TaskHandle(
        task_id="completed-1",
        cli="x",
        pid=42,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "c.jsonl",
    )
    pop.state_store.register(handle)
    summaries = pop.list_all(include_terminal=True)
    assert any(s["task_id"] == "completed-1" for s in summaries)
    summaries_active = pop.list_all(include_terminal=False)
    assert not any(s["task_id"] == "completed-1" for s in summaries_active)


def test_shutdown_persistence_bridge_no_op_when_unset(tmp_path: Path) -> None:
    """shutdown_persistence_bridge() is safe when no persistence/bridge was injected."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    pop.shutdown_persistence_bridge()


def test_rehydrate_from_persistence_returns_zero_when_no_persistence(tmp_path: Path) -> None:
    """rehydrate_from_persistence returns 0 when persistence is None."""
    pop = Popolad(events_dir=tmp_path, adapter=lambda *a, **kw: [sys.executable])
    assert pop.rehydrate_from_persistence() == 0


# ── ensure unused imports stay live ───────────────────────────────────────
_ = (_EventLog, threading)
