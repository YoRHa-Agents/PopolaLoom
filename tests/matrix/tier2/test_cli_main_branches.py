"""Tier 2 / Coverage — extra ``cli/main.py`` branch tests.

Mocks :func:`make_sync_client` so we can drive every error path the
existing ``test_cli_httpx.py`` integration tests don't exercise:

- version + list-cli (no daemon needed) — coverage of those subcommands.
- _format_event / _summarize_data branches (every event type).
- list with --json + state filter.
- list with non-200 → exit 1.
- cancel 404 / 409 / unexpected status.
- probe non-200.
- _wait_for_terminal warning on non-200 status.
- _wait_for_terminal timeout warning.
- _parse_cli_flags JSON value parsing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main


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


# ── version (no daemon) ──────────────────────────────────────────────────


def test_version_prints_package_version() -> None:
    """``popola version`` prints ``popolaloom <version>`` (no daemon required)."""
    from popolaloom import __version__

    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["version"])
    assert r.exit_code == 0
    assert __version__ in r.stdout


# ── list-cli (no daemon) ─────────────────────────────────────────────────


def test_list_cli_renders_default_three_adapters() -> None:
    """``list-cli`` lists cursor / claude / codex (default registered adapters)."""
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["list-cli"])
    assert r.exit_code == 0
    for name in ("cursor", "claude", "codex"):
        assert name in r.stdout


# ── _format_event / _summarize_data branches ─────────────────────────────


def test_format_event_task_dispatched() -> None:
    """task.dispatched gets a ``cli=... prompt=...`` summary."""
    line = cli_main._format_event(
        {
            "time": "2026-05-04T10:00:00Z",
            "type": "task.dispatched",
            "data": {"cli": "cursor", "prompt": "go"},
        }
    )
    assert "task.dispatched" in line
    assert "cursor" in line


def test_format_event_process_stdout() -> None:
    """process.stdout summary is just the line text."""
    line = cli_main._format_event(
        {
            "time": "2026-05-04T10:00:00Z",
            "type": "process.stdout",
            "data": {"line": "hello world"},
        }
    )
    assert "hello world" in line


def test_format_event_task_completed() -> None:
    """task.completed summary is exit_code=N."""
    line = cli_main._format_event(
        {"time": "T", "type": "task.completed", "data": {"exit_code": 0}}
    )
    assert "exit_code=0" in line


def test_format_event_task_failed() -> None:
    line = cli_main._format_event(
        {"time": "T", "type": "task.failed", "data": {"exit_code": 7}}
    )
    assert "exit_code=7" in line


def test_format_event_process_started() -> None:
    line = cli_main._format_event(
        {
            "time": "T",
            "type": "process.started",
            "data": {"pid": 4242, "session_id": 4242},
        }
    )
    assert "4242" in line


def test_format_event_stream_truncated() -> None:
    line = cli_main._format_event(
        {
            "time": "T",
            "type": "stream.truncated",
            "data": {"stream": "stdout", "actual_lines": 10, "reason": "join_timeout_30s"},
        }
    )
    assert "stdout" in line
    assert "10" in line


def test_format_event_state_ghost_exit() -> None:
    line = cli_main._format_event(
        {
            "time": "T",
            "type": "state.ghost_exit",
            "data": {"reason": "cancel race", "exit_code": 0},
        }
    )
    assert "cancel race" in line


def test_format_event_unknown_type_falls_back_to_json() -> None:
    line = cli_main._format_event(
        {
            "time": "T",
            "type": "totally.new.event",
            "data": {"k": "v", "n": 5},
        }
    )
    assert "totally.new.event" in line
    assert "v" in line


def test_format_event_data_not_dict_uses_repr() -> None:
    """When data is non-dict (rare but defensive), repr() is used."""
    line = cli_main._format_event({"time": "T", "type": "weird", "data": [1, 2, 3]})
    assert "[1, 2, 3]" in line


def test_format_event_long_json_is_truncated() -> None:
    """Long JSON serialisation gets ellipsised at 117+3 chars."""
    big_data = {"k": "x" * 200}
    line = cli_main._format_event({"time": "T", "type": "weird", "data": big_data})
    assert "..." in line


# ── _parse_cli_flags edge cases ──────────────────────────────────────────


def test_parse_cli_flags_json_int() -> None:
    parsed = cli_main._parse_cli_flags(["count=42"])
    assert parsed == {"count": 42}


def test_parse_cli_flags_json_object() -> None:
    parsed = cli_main._parse_cli_flags(['nested={"k":1}'])
    assert parsed == {"nested": {"k": 1}}


def test_parse_cli_flags_string_fallback() -> None:
    parsed = cli_main._parse_cli_flags(["plain=just_a_string"])
    assert parsed == {"plain": "just_a_string"}


# ── list (CLI) error paths ───────────────────────────────────────────────


def test_list_non_200_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /list`` returning 500 → exit 1 with error message."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["list"])
    assert r.exit_code == 1


def test_list_with_state_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--state running`` filters items client-side."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"task_id": "t1", "cli": "cursor", "state": "running", "pid": 1, "started_at": ""},
                {"task_id": "t2", "cli": "claude", "state": "pending", "pid": 2, "started_at": ""},
            ],
        )

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["list", "--state", "running", "--json"])
    assert r.exit_code == 0
    items = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(items) == 1
    assert items[0]["task_id"] == "t1"


# ── cancel error paths ───────────────────────────────────────────────────


def test_cancel_404_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /cancel/{id}`` 404 → "task not found" + exit 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "missing"})

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["cancel", "missing"])
    assert r.exit_code == 1
    output = r.stdout + (getattr(r, "stderr", "") or "")
    assert "task not found" in output


def test_cancel_409_already_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /cancel/{id}`` 409 → "cannot cancel" + exit 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already terminal"})

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert r.exit_code == 1


def test_cancel_unexpected_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /cancel/{id}`` 500 → unexpected status path."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert r.exit_code == 1


# ── probe error path ─────────────────────────────────────────────────────


def test_probe_non_200_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /probe`` non-200 → exit 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["probe"])
    assert r.exit_code == 1


# ── status non-200 ───────────────────────────────────────────────────────


def test_status_non_200_other_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /status/{id}`` 500 → unexpected status path."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    runner = CliRunner()
    r = runner.invoke(cli_main.app, ["status", "tid"])
    assert r.exit_code == 1


# ── _wait_for_terminal (used by --wait) ──────────────────────────────────


def test_wait_for_terminal_warns_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """When status returns non-200, _wait_for_terminal logs a warning + returns."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    cli_main._wait_for_terminal("tid", timeout_s=1.0)


def test_wait_for_terminal_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When state never goes terminal, _wait_for_terminal times out and warns."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "running"})

    monkeypatch.setattr(cli_main, "make_sync_client", _client_factory(handler))
    monkeypatch.setattr(cli_main, "_POLL_INTERVAL_S", 0.01)
    cli_main._wait_for_terminal("tid", timeout_s=0.05)
