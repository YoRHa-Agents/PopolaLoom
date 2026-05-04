"""Additional v0.2.2 coverage gap fillers — push from ~83% to ≥85%.

Targets:

* :mod:`popolaloom.cli.main` connect-error / wait-loop / SSE consumer.
* :mod:`popolaloom.daemon.supervisor` proc.wait failure + stream
  truncation paths.
* :mod:`popolaloom.daemon.rpc` cancel / status 404 + 409 paths +
  attach SSE 404.
* :mod:`popolaloom.cli.popolad` start happy/error paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import popolaloom.cli.main as cli_main
import popolaloom.cli.popolad as popolad_cli
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.rpc import create_app
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.supervisor import Supervisor


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["python", "-c", "print('cov')"]


# ── cli.main helpers ─────────────────────────────────────────────────────


def test_cli_main_render_connect_error_exits_1() -> None:
    """``_render_connect_error`` raises typer.Exit(1) (via click.exceptions.Exit)."""
    import click
    with pytest.raises(click.exceptions.Exit) as exc_info:
        cli_main._render_connect_error(httpx.ConnectError("test"))
    assert exc_info.value.exit_code == 1


def test_cli_main_summarize_data_non_dict_returns_repr() -> None:
    """``_summarize_data`` with non-dict returns ``repr(data)``."""
    assert cli_main._summarize_data("any", [1, 2]) == "[1, 2]"
    assert cli_main._summarize_data("any", "string") == "'string'"


def test_cli_main_summarize_data_long_dict_truncated() -> None:
    """``_summarize_data`` truncates >120 char serialisations."""
    big = {"k": "v" * 200}
    out = cli_main._summarize_data("unknown.type", big)
    assert out.endswith("...")
    assert len(out) <= 121


def test_cli_main_summarize_data_handles_known_event_types() -> None:
    """``_summarize_data`` formats every recognised event_type."""
    for event_type, data in [
        ("process.stdout", {"line": "hi"}),
        ("process.stderr", {"line": "err"}),
        ("task.dispatched", {"cli": "cursor", "prompt": "p"}),
        ("task.completed", {"exit_code": 0}),
        ("task.failed", {"exit_code": 1}),
        ("process.started", {"pid": 1234, "session_id": 5}),
        ("stream.truncated", {"stream": "stdout", "actual_lines": 10, "reason": "x"}),
        ("state.ghost_exit", {"reason": "lost", "exit_code": -1}),
    ]:
        out = cli_main._summarize_data(event_type, data)
        assert isinstance(out, str)
        assert out


def test_cli_main_format_event_full() -> None:
    """``_format_event`` joins time, type and summary."""
    env = {
        "time": "2026-05-04T00:00:00Z",
        "type": "task.dispatched",
        "data": {"cli": "cursor", "prompt": "test"},
    }
    out = cli_main._format_event(env)
    assert "2026-05-04T00:00:00Z" in out
    assert "task.dispatched" in out


def test_cli_main_parse_cli_flags_value_json_decoding() -> None:
    """``--cli-flag yolo=true`` parses to bool True via JSON."""
    out = cli_main._parse_cli_flags(["yolo=true", "name=cursor", "n=42"])
    assert out == {"yolo": True, "name": "cursor", "n": 42}


def test_cli_main_parse_cli_flags_missing_eq_raises() -> None:
    """Missing ``=`` → typer.BadParameter."""
    import typer
    with pytest.raises(typer.BadParameter):
        cli_main._parse_cli_flags(["just_a_flag"])


def test_cli_main_parse_cli_flags_missing_key_raises() -> None:
    """Empty key (e.g. ``=value``) → typer.BadParameter."""
    import typer
    with pytest.raises(typer.BadParameter):
        cli_main._parse_cli_flags(["=value"])


# ── supervisor failure paths ─────────────────────────────────────────────


def test_supervisor_proc_wait_raises_emits_task_failed_with_error(
    tmp_path: Path,
    mocker,
) -> None:
    """``proc.wait`` raising → emit ``task.failed`` with ``error`` field."""
    sup = Supervisor()
    log = EventLog(tmp_path / "sup.jsonl", fsync_interval_s=0.0)
    fake_proc = mocker.MagicMock()
    fake_proc.pid = 9999
    fake_proc.wait.side_effect = OSError(99, "wait failed")

    callback_args: list[tuple[str, int]] = []

    def _on_exit(tid: str, exit_code: int) -> None:
        callback_args.append((tid, exit_code))

    sup._wait_and_finalize(
        task_id="t-wait-fail",
        proc=fake_proc,
        event_log=log,
        stdout_thread=mocker.MagicMock(),
        stderr_thread=mocker.MagicMock(),
        on_exit=_on_exit,
    )

    log.close()
    events = log.tail()
    failed = [e for e in events if e["type"] == "task.failed"]
    assert failed, f"expected task.failed event in: {events}"
    assert failed[0]["data"]["exit_code"] == -1
    assert "error" in failed[0]["data"]
    assert callback_args == [("t-wait-fail", -1)]


def test_supervisor_safe_on_exit_swallows_callback_exception(
    caplog,
) -> None:
    """``_safe_on_exit`` catches callback exception (logs but doesn't re-raise)."""
    def _bad(_tid: str, _ec: int) -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.supervisor"):
        Supervisor._safe_on_exit(_bad, "t-safe", 0)

    assert any("on_exit callback failed" in r.message for r in caplog.records)


def test_supervisor_emit_stream_truncated_writes_event(
    tmp_path: Path,
) -> None:
    """``_emit_stream_truncated`` writes a ``stream.truncated`` envelope."""
    sup = Supervisor()
    log = EventLog(tmp_path / "trunc.jsonl", fsync_interval_s=0.0)
    sup._line_counts["t-trunc"] = {"stdout": 17, "stderr": 0}
    sup._emit_stream_truncated("t-trunc", "stdout", log)
    log.close()
    events = log.tail()
    assert any(
        e["type"] == "stream.truncated" and e["data"]["actual_lines"] == 17
        for e in events
    )


def test_supervisor_join_returns_true_when_no_workers(tmp_path: Path) -> None:
    """``join`` with unknown task_id returns True (no work to do)."""
    sup = Supervisor()
    assert sup.join("never-spawned") is True


# ── rpc / fastapi 404 / 409 paths ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rpc_status_unknown_task_returns_404(tmp_path: Path) -> None:
    """``GET /status/<unknown>`` → 404."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.get("/status/nonexistent")
        assert r.status_code == 404
        assert "task not found" in r.json()["detail"]


@pytest.mark.asyncio
async def test_rpc_cancel_unknown_task_returns_404(tmp_path: Path) -> None:
    """``POST /cancel/<unknown>`` → 404."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.post("/cancel/nonexistent")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_rpc_attach_stream_unknown_task_returns_404(tmp_path: Path) -> None:
    """``GET /attach_stream/<unknown>`` → 404."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.get("/attach_stream/nonexistent")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_rpc_health_endpoint_returns_ok(tmp_path: Path) -> None:
    """``GET /health`` → 200 ``{"status": "ok"}``."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_rpc_probe_endpoint_includes_version(tmp_path: Path) -> None:
    """``GET /probe`` returns the package version."""
    from popolaloom import __version__
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.get("/probe")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == __version__


@pytest.mark.asyncio
async def test_rpc_dispatch_keyerror_returns_404(tmp_path: Path) -> None:
    """When dispatch_task raises KeyError → 404."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)

    def _raise_keyerror(*args, **kwargs):
        raise KeyError("ghost-cli")

    popolad.dispatch_task = _raise_keyerror  # type: ignore[assignment]

    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.post(
            "/dispatch",
            json={"cli": "ghost", "prompt": "x", "cwd": None, "extra": None},
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_rpc_dispatch_value_error_returns_400(tmp_path: Path) -> None:
    """When dispatch_task raises ValueError → 400."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)

    def _raise_value(*args, **kwargs):
        raise ValueError("bad cmd")

    popolad.dispatch_task = _raise_value  # type: ignore[assignment]
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://popolad") as client:
        r = await client.post(
            "/dispatch",
            json={"cli": "x", "prompt": "y", "cwd": None, "extra": None},
        )
        assert r.status_code == 400


# ── popolad CLI ────────────────────────────────────────────────────────────


def test_popolad_status_pid_file_unreadable_records_error(
    monkeypatch, tmp_path: Path
) -> None:
    """``popolad status`` with bad PID file records ``pid_file_error`` in JSON."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    pid_file = tmp_path / "popolad.pid"
    pid_file.write_text("not-a-number", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(popolad_cli.app, ["status", "--json"])
    parsed = json.loads(result.output.strip())
    assert "pid_file_error" in parsed
    assert parsed["pid"] is None


def test_popolad_can_connect_returns_false_for_missing_socket(
    tmp_path: Path,
) -> None:
    """``_can_connect`` returns False when socket missing."""
    sock = tmp_path / "no.sock"
    assert popolad_cli._can_connect(sock) is False


def test_popolad_pid_alive_zero_or_negative_returns_false() -> None:
    """``_pid_alive`` rejects ≤ 0."""
    assert popolad_cli._pid_alive(0) is False
    assert popolad_cli._pid_alive(-1) is False


def test_popolad_pid_alive_for_self_returns_true() -> None:
    """Test process is alive (sanity check)."""
    import os
    assert popolad_cli._pid_alive(os.getpid()) is True


def test_popolad_cleanup_files_handles_missing_paths(tmp_path: Path) -> None:
    """``_cleanup_files`` no-ops when both files missing."""
    pid = tmp_path / "no.pid"
    sock = tmp_path / "no.sock"
    popolad_cli._cleanup_files(pid, sock)


def test_popolad_log_path_creates_log_dir(monkeypatch, tmp_path: Path) -> None:
    """``_log_path`` ensures the log dir exists."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    log_path = popolad_cli._log_path()
    assert log_path.parent.exists()
    assert log_path.parent.name == "log"


# ── cli main version + list-cli ──────────────────────────────────────────


def test_cli_main_version_shows_pkg_version() -> None:
    """``popola version`` prints ``popolaloom <version>``."""
    from popolaloom import __version__
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_main_list_cli_no_adapters_returns_exit_1(
    monkeypatch,
    isolated_adapter_registry,
) -> None:
    """``popola list-cli`` with empty registry → exit 1."""
    from popolaloom.adapters import base as adapter_base
    adapter_base._REGISTRY.clear()
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["list-cli"])
    assert result.exit_code == 1
    assert "no adapters" in result.output


def test_cli_main_list_cli_with_adapters_renders_table() -> None:
    """``popola list-cli`` lists registered adapters."""
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["list-cli"])
    assert result.exit_code == 0
    assert "cursor" in result.output


def test_cli_main_socket_path_env_override(monkeypatch, tmp_path: Path) -> None:
    """``$POPOLA_HOME`` flows into ``_socket_path()``."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    sp = cli_main._socket_path()
    assert sp == tmp_path / "popolad.sock"


def test_cli_main_make_async_client_returns_async_client() -> None:
    """``make_async_client`` returns an ``httpx.AsyncClient``."""
    import asyncio
    client = cli_main.make_async_client()
    assert isinstance(client, httpx.AsyncClient)
    asyncio.run(client.aclose())


def test_cli_main_make_sync_client_returns_sync_client() -> None:
    """``make_sync_client`` returns an ``httpx.Client``."""
    client = cli_main.make_sync_client()
    assert isinstance(client, httpx.Client)
    client.close()
