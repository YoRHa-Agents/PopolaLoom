"""Default-lane tests for ``popola`` CLI error paths (v0.5.1 coverage push).

Per [v0.5.1 Loop 1 §L1.B](../../release-notes-v0.5.1.md): the v0.5.0 GA
shipped at 91.15 % default-lane coverage, with the bulk of the missed
branches concentrated in :mod:`popolaloom.cli.main` non-happy paths
(dispatch / status / list / attach / cancel / probe HTTP non-200 +
``httpx.ConnectError`` rendering).  These tests close those branches
with pure ``unittest.mock`` HTTP doubles — no real ``popolad`` daemon
or live socket is required, so they live in the default lane (the live
daemon scenarios remain in ``tests/test_cli_httpx.py`` which is also
default-lane but uses an in-thread uvicorn fixture).

Coverage targets in this module:

* ``dispatch``     — 404 unknown CLI, 400 bad request, 500 unexpected,
                     ``httpx.ConnectError`` (lines 270, 273-280).
* ``status``       — 404 not found, 500 unexpected,
                     ``httpx.ConnectError`` (lines 306-308).
* ``list``         — 500 unexpected, ``httpx.ConnectError`` (491-493).
* ``cancel``       — 404 / 409 / 500 / connect (543-545, 547-555).
* ``probe``        — 500 / connect / ``--json`` (577-579, 587-588).
* ``attach``       — both follow/no-follow paths: 404 status, 500
                     status, attach_stream non-200, connect,
                     KeyboardInterrupt (390-433, 436, 438).
* ``_consume_sse`` — comment line, un-parsable JSON line (454-462).
* ``_wait_for_terminal`` — ``httpx.ConnectError`` (627-628).
* ``list-cli``     — adapter ``is_available()`` False renders ``missing``
                     (line 210).
* ``main()``       — entry-point invocation (line 697).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Point the CLI at a tmp socket path so no real daemon is touched."""
    sock = tmp_path / "popolad.sock"
    monkeypatch.setattr(cli_main, "_socket_path", lambda: sock)
    yield sock


def _combined(result: object) -> str:
    """Best-effort ``stdout + stderr`` extraction (click 8.x compat)."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        parts.append(value)
    return "".join(parts)


def _make_response(*, status_code: int, body: Any, text: str | None = None) -> MagicMock:
    """Build a ``MagicMock`` shaped like an :class:`httpx.Response`."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = text if text is not None else json.dumps(body)
    return response


def _make_sync_client(*, on_get: Any = None, on_post: Any = None) -> MagicMock:
    """Build a context-manager-shaped sync httpx client double."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    if on_get is not None:
        if isinstance(on_get, BaseException) or (
            isinstance(on_get, type) and issubclass(on_get, BaseException)
        ):
            client.get.side_effect = on_get
        else:
            client.get.return_value = on_get
    if on_post is not None:
        if isinstance(on_post, BaseException) or (
            isinstance(on_post, type) and issubclass(on_post, BaseException)
        ):
            client.post.side_effect = on_post
        else:
            client.post.return_value = on_post
    return client


# ── dispatch error paths ──────────────────────────────────────────────────


def test_dispatch_unknown_cli_404_renders_friendly_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola dispatch --cli=bad`` → 404 from daemon → exit 1 + ``unknown cli`` msg."""
    response = _make_response(status_code=404, body={"detail": "adapter not registered: 'no_cli'"})
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["dispatch", "p", "--cli", "no_cli"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "unknown cli" in out
    assert "no_cli" in out


def test_dispatch_400_validation_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /dispatch`` 400 → ``error: dispatch failed`` + exit 1."""
    response = _make_response(status_code=400, body={"detail": "bad cwd"})
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["dispatch", "p", "--cli", "cursor"])
    assert result.exit_code == 1
    assert "dispatch failed" in _combined(result)


def test_dispatch_unexpected_500_status(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /dispatch`` returning 500 → exit 1 + ``unexpected status`` msg."""
    response = _make_response(status_code=500, body={}, text="boom")
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["dispatch", "p", "--cli", "cursor"])
    assert result.exit_code == 1
    assert "unexpected status 500" in _combined(result)


def test_dispatch_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola dispatch`` with daemon down → ``popolad not running`` + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_post=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["dispatch", "p", "--cli", "cursor"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


# ── status error paths ────────────────────────────────────────────────────


def test_status_404_not_found(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola status <id>`` → 404 from daemon → ``task not found`` + exit 1."""
    response = _make_response(status_code=404, body={"detail": "missing"})
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["status", "no-such"])
    assert result.exit_code == 1
    assert "task not found" in _combined(result)
    assert "no-such" in _combined(result)


def test_status_unexpected_500(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola status`` non-200 (not 404) → ``status unexpected`` + exit 1."""
    response = _make_response(status_code=502, body={}, text="bad gw")
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["status", "tid"])
    assert result.exit_code == 1
    assert "status unexpected 502" in _combined(result)


def test_status_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola status`` with daemon down → ``popolad not running`` + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["status", "tid"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


# ── list error paths ──────────────────────────────────────────────────────


def test_list_unexpected_500(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola list`` non-200 → ``list unexpected`` + exit 1."""
    response = _make_response(status_code=503, body={}, text="busy")
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 1
    assert "list unexpected 503" in _combined(result)


def test_list_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola list`` with daemon down → ``popolad not running`` + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


def test_list_empty_renders_no_active_message(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola list`` with empty body → friendly ``No active tasks.`` line."""
    response = _make_response(status_code=200, body=[])
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 0
    assert "No active tasks." in _combined(result)


def test_list_with_state_filter_drops_other_states(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--state pending`` filters out non-matching state rows."""
    items = [
        {"task_id": "a", "cli": "cursor", "state": "running", "pid": 1, "started_at": "x"},
        {"task_id": "b", "cli": "claude", "state": "pending", "pid": None, "started_at": "y"},
    ]
    response = _make_response(status_code=200, body=items)
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["list", "--state", "pending", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert {row["task_id"] for row in payload} == {"b"}


# ── cancel error paths ────────────────────────────────────────────────────


def test_cancel_404_not_found(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola cancel`` 404 → ``task not found`` + exit 1."""
    response = _make_response(status_code=404, body={"detail": "missing"})
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert result.exit_code == 1
    assert "task not found" in _combined(result)


def test_cancel_409_already_terminal(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola cancel`` 409 → ``cannot cancel`` + exit 1."""
    response = _make_response(status_code=409, body={"detail": "already terminal"})
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert result.exit_code == 1
    assert "cannot cancel" in _combined(result)


def test_cancel_unexpected_500(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola cancel`` 500 → ``cancel unexpected`` + exit 1."""
    response = _make_response(status_code=500, body={}, text="boom")
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert result.exit_code == 1
    assert "cancel unexpected 500" in _combined(result)


def test_cancel_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola cancel`` with daemon down → ``popolad not running`` + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_post=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


def test_cancel_success_text_format(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola cancel`` happy path → human-readable ``cancel requested for ...`` line."""
    response = _make_response(
        status_code=200,
        body={
            "task_id": "tid",
            "requested_signal": "SIGTERM",
            "escalated_to_sigkill": True,
            "pid": 4242,
            "result": "completed",
        },
    )
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_post=response)
    )
    result = runner.invoke(cli_main.app, ["cancel", "tid"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "cancel requested for tid" in out
    assert "SIGTERM" in out
    assert "escalated to SIGKILL" in out


# ── probe error paths + JSON ──────────────────────────────────────────────


def test_probe_unexpected_500(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola probe`` non-200 → ``probe unexpected`` + exit 1."""
    response = _make_response(status_code=500, body={}, text="boom")
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["probe"])
    assert result.exit_code == 1
    assert "probe unexpected 500" in _combined(result)


def test_probe_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola probe`` with daemon down → ``popolad not running`` + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["probe"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


def test_probe_json_output_short_circuits_table(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola probe --json`` emits the JSON envelope and skips the rich table."""
    body = {
        "daemon_pid": 1234,
        "started_at": "2026-01-01T00:00:00",
        "uptime_seconds": 12.5,
        "active_tasks": 0,
        "version": "0.5.1",
    }
    response = _make_response(status_code=200, body=body)
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    result = runner.invoke(cli_main.app, ["probe", "--json"])
    assert result.exit_code == 0
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert payload["daemon_pid"] == 1234
    assert payload["version"] == "0.5.1"


# ── attach error paths (both follow and no-follow) ────────────────────────


def _stream_response(*, status_code: int, lines: list[str] | None = None) -> MagicMock:
    """Build a context-manager-shaped streaming response double.

    ``httpx.Response`` is itself a context manager, so the ``with
    client.stream(...) as stream`` block needs ``__enter__`` /
    ``__exit__``.  We build a plain ``MagicMock`` (no spec) and assign
    those magic methods directly to support the ``with`` protocol.
    """
    stream = MagicMock()
    stream.status_code = status_code
    stream.iter_lines.return_value = iter(lines or [])
    stream.__enter__.return_value = stream
    stream.__exit__.return_value = False
    return stream


def _attach_client_factory(
    *, status_response: MagicMock, stream_response_obj: MagicMock | None
) -> Any:
    """Build a sync-client factory whose ``stream(...)`` returns the given stream."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = status_response
    client.stream = MagicMock(
        return_value=stream_response_obj if stream_response_obj is not None else MagicMock()
    )
    return lambda *a, **kw: client


def test_attach_no_follow_status_404(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach --no-follow`` → 404 status → ``task not found`` + exit 1."""
    status_response = _make_response(status_code=404, body={})
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=None),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid", "--no-follow"])
    assert result.exit_code == 1
    assert "task not found" in _combined(result)


def test_attach_no_follow_status_500(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach --no-follow`` → 500 status → ``error: status`` + exit 1."""
    status_response = _make_response(status_code=500, body={}, text="boom")
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=None),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid", "--no-follow"])
    assert result.exit_code == 1
    assert "status 500" in _combined(result)


def test_attach_no_follow_stream_non_200(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach --no-follow`` stream returns 502 → exit 1."""
    status_response = _make_response(status_code=200, body={"state": "running"})
    stream = _stream_response(status_code=502)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=stream),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid", "--no-follow"])
    assert result.exit_code == 1
    assert "attach_stream 502" in _combined(result)


def test_attach_no_follow_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach --no-follow`` daemon down → friendly error + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid", "--no-follow"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


def test_attach_follow_status_404(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach`` (default --follow) → 404 → ``task not found`` + exit 1."""
    status_response = _make_response(status_code=404, body={})
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=None),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid"])
    assert result.exit_code == 1
    assert "task not found" in _combined(result)


def test_attach_follow_status_500(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach`` (follow) 500 → ``error: status 500`` + exit 1."""
    status_response = _make_response(status_code=500, body={}, text="boom")
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=None),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid"])
    assert result.exit_code == 1
    assert "status 500" in _combined(result)


def test_attach_follow_stream_non_200(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach`` (follow) stream returns 503 → exit 1."""
    status_response = _make_response(status_code=200, body={"state": "running"})
    stream = _stream_response(status_code=503)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=stream),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid"])
    assert result.exit_code == 1
    assert "attach_stream 503" in _combined(result)


def test_attach_follow_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach`` (follow) daemon down → friendly error + exit 1."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=httpx.ConnectError("conn")),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid"])
    assert result.exit_code == 1
    assert "popolad not running" in _combined(result)


def test_attach_follow_keyboard_interrupt_returns_cleanly(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach`` (follow) Ctrl-C → graceful exit 0 (no traceback)."""
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=KeyboardInterrupt()),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid"])
    assert result.exit_code == 0


def test_attach_follow_streams_events_and_terminates(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola attach`` (follow) renders SSE frames + comments + ignores garbage."""
    status_response = _make_response(status_code=200, body={"state": "running"})
    stream = _stream_response(
        status_code=200,
        lines=[
            ":heartbeat",
            "garbage_no_prefix",
            "data: " + json.dumps(
                {"time": "t1", "type": "task.dispatched", "data": {"cli": "echo", "prompt": "p"}}
            ),
            "data: not-json-bytes",
            "data: " + json.dumps(
                {"time": "t2", "type": "task.completed", "data": {"exit_code": 0}}
            ),
            "",
        ],
    )
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(status_response=status_response, stream_response_obj=stream),
    )
    result = runner.invoke(cli_main.app, ["attach", "tid"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "task.dispatched" in out
    assert "task.completed" in out


# ── _consume_sse comment / un-parsable line edge cases ───────────────────


def test_consume_sse_skips_comment_and_unparsable() -> None:
    """``_consume_sse`` ignores ``:`` comment lines and JSON-decode failures."""
    response = MagicMock(spec=httpx.Response)
    response.iter_lines.return_value = iter(
        [
            "",
            ":heartbeat",
            "data: not-json",
            "data: " + json.dumps(
                {"time": "t1", "type": "task.completed", "data": {"exit_code": 0}}
            ),
            "garbage",
        ]
    )
    cli_main._consume_sse(response, terminate_on_terminal=True)


# ── _wait_for_terminal ConnectError ──────────────────────────────────────


def test_wait_for_terminal_connect_error(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--wait`` losing the daemon mid-poll → typer.Exit(1) propagates."""
    import typer as _typer

    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=httpx.ConnectError("conn")),
    )
    with pytest.raises(_typer.Exit) as excinfo:
        cli_main._wait_for_terminal("tid", timeout_s=0.1)
    assert excinfo.value.exit_code == 1


def test_wait_for_terminal_warns_on_non_200_status(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--wait`` with status returning 500 → renders warning and returns (no exit)."""
    response = _make_response(status_code=500, body={}, text="boom")
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    cli_main._wait_for_terminal("tid", timeout_s=0.1)


def test_wait_for_terminal_times_out_when_state_never_terminal(
    isolated_socket: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--wait`` deadline expires before terminal → renders timeout warning + returns."""
    response = _make_response(
        status_code=200,
        body={"state": "running"},
    )
    monkeypatch.setattr(
        cli_main, "make_sync_client", lambda *a, **kw: _make_sync_client(on_get=response)
    )
    cli_main._wait_for_terminal("tid", timeout_s=0.0)


# ── list-cli adapter unavailable branch ──────────────────────────────────


def test_list_cli_renders_missing_for_unavailable_adapter(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola list-cli`` shows ``missing`` for adapters whose ``is_available()`` is False."""
    fake_adapter = MagicMock()
    fake_adapter.binary = "/no/such/bin"
    fake_adapter.is_available.return_value = False

    monkeypatch.setattr(cli_main, "list_registered", lambda: ["fakecli"])
    monkeypatch.setattr(cli_main, "get_adapter", lambda _: fake_adapter)

    result = runner.invoke(cli_main.app, ["list-cli"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "fakecli" in out
    assert "missing" in out


def test_list_cli_no_adapters_registered_errors(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``popola list-cli`` with empty registry → exit 1 + ``no adapters registered`` msg."""
    monkeypatch.setattr(cli_main, "list_registered", lambda: [])
    result = runner.invoke(cli_main.app, ["list-cli"])
    assert result.exit_code == 1
    assert "no adapters registered" in _combined(result)


# ── _format_event / _summarize_data branch coverage ───────────────────────


def test_summarize_data_handles_non_dict_payloads() -> None:
    """``_summarize_data`` returns ``repr(data)`` when the envelope ``data`` is not a dict."""
    assert "[1, 2, 3]" in cli_main._summarize_data("any.type", [1, 2, 3])
    assert "'string'" in cli_main._summarize_data("any.type", "string")


def test_summarize_data_truncates_long_serialized_payload() -> None:
    """Generic event types get a JSON dump that's truncated past 120 chars."""
    big = {"x" + str(i): "y" * 50 for i in range(10)}
    out = cli_main._summarize_data("custom.type", big)
    assert out.endswith("...")
    assert len(out) == 120


def test_summarize_data_renders_known_event_types() -> None:
    """Each known event_type branch renders a stable summary fragment."""
    assert (
        cli_main._summarize_data("process.stdout", {"line": "hello"}) == "hello"
    )
    assert (
        cli_main._summarize_data("task.dispatched", {"cli": "cursor", "prompt": "p"})
        == "cli='cursor' prompt='p'"
    )
    assert "exit_code=0" in cli_main._summarize_data("task.completed", {"exit_code": 0})
    assert "pid=42" in cli_main._summarize_data(
        "process.started", {"pid": 42, "session_id": "s"}
    )
    assert "stream=stdout" in cli_main._summarize_data(
        "stream.truncated", {"stream": "stdout", "actual_lines": 5, "reason": "cap"}
    )
    assert "ghost" in cli_main._summarize_data(
        "state.ghost_exit", {"reason": "ghost", "exit_code": -1}
    )


# ── main() entry point smoke (line 697) ──────────────────────────────────


def test_main_entry_point_invokes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cli.main.main()`` calls the Typer app (covers line 697)."""
    called: dict[str, bool] = {}

    def fake_app() -> None:
        called["yes"] = True

    monkeypatch.setattr(cli_main, "app", fake_app)
    cli_main.main()
    assert called.get("yes") is True


# ── version verb (no-daemon happy path) ───────────────────────────────────


def test_version_command_prints_version(runner: CliRunner) -> None:
    """``popola version`` prints ``popolaloom <semver>`` without touching the daemon."""
    result = runner.invoke(cli_main.app, ["version"])
    assert result.exit_code == 0
    out = _combined(result)
    assert "popolaloom" in out
    from popolaloom import __version__

    assert __version__ in out


# ── make_*_client default-path smoke (no kwargs) ─────────────────────────


def test_make_sync_client_default_path_returns_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``make_sync_client()`` builds an httpx.Client when ``socket_path`` is omitted."""
    with patch.object(cli_main, "_socket_path", return_value=Path("/no/such/sock")):
        client = cli_main.make_sync_client()
        assert isinstance(client, httpx.Client)
        client.close()


def test_make_async_client_default_path_returns_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``make_async_client()`` builds an httpx.AsyncClient when ``socket_path`` is omitted."""
    import asyncio

    async def go() -> None:
        with patch.object(cli_main, "_socket_path", return_value=Path("/no/such/sock")):
            client = cli_main.make_async_client()
            assert isinstance(client, httpx.AsyncClient)
            await client.aclose()

    asyncio.run(go())
