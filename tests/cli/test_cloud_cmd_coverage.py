"""v0.8.8 T4.1 — coverage backfill for ``popolaloom.cli.cloud_cmd``.

This file extends :file:`test_cloud_runs.py` (T2.4.1) with the
remaining branches needed to lift ``cli/cloud_cmd.py`` from 79 % to
≥ 90 %. Each test isolates one branch via ``httpx.MockTransport`` /
``MagicMock`` doubles; no real network IO, no real popolad UDS.

Coverage gaps targeted (one test per row):

- ``_resolve_agent_model`` non-Cursor exception path (broad
  ``Exception`` handler — line 425-432).
- ``_resolve_agent_model`` returning model from nested ``model.id``
  dict shape (line 439-443).
- ``_inject_events_summary`` non-list ``items`` early return / row
  with no ``id`` / generic ``Exception`` handler (lines 459-484).
- ``_summarise_run_events`` fallback branch when no events list
  exists (lines 535-545).
- ``_emit_table`` empty-rows path with and without ``next_cursor``
  (lines 810-815).
- ``_format_wall_clock`` HH:MM:SS branch + negative-clamp branch
  (lines 866-868).
- ``_handle_cloud_error`` 410 ``stream_expired`` falls into default
  exit 75 path (line 610).
- daemon-down / non-200 daemon response edge cases.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudError,
)
from popolaloom.cli import cloud_cmd

# ---------------------------------------------------------------------------
# Fixtures (mirror test_cloud_runs.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the cloud_cmd Rich Console wide so substring asserts hold."""
    monkeypatch.setattr(
        cloud_cmd, "_console_out", Console(width=200, height=50)
    )


def _combined(result: Any) -> str:
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


def _make_response(*, status_code: int, body: Any) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body) if body is not None else ""
    response.content = response.text.encode("utf-8") if response.text else b""
    return response


def _build_status_payload(
    *,
    runtime: str = "cloud",
    cursor_agent_id: str | None = "bc-test-001",
) -> dict[str, Any]:
    return {
        "task_id": "t-001",
        "cli": "cursor-cloud" if runtime == "cloud" else "cursor",
        "state": "running",
        "pid": None,
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "runtime": runtime,
        "cursor_agent_id": cursor_agent_id,
        "cursor_run_id": "r-001",
        "cloud_phase": "RUNNING",
        "exit_code": None,
        "completed_at": None,
        "latest_event_index": 0,
        "arktower_task_id": None,
        "persisted": False,
    }


def _make_daemon_client(
    *,
    response: MagicMock | None = None,
    on_get: Any = None,
) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    if on_get is not None:
        client.get.side_effect = on_get
    elif response is not None:
        client.get.return_value = response
    else:
        raise ValueError("must provide one of `response` or `on_get`")
    return client


def _build_cloud_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> CloudCursorClient:
    client = CloudCursorClient("test-api-key", base_url=CURSOR_API_BASE)
    client._client.close()
    client._client = httpx.Client(
        base_url=client._base_url,
        auth=(client._api_key, ""),
        transport=httpx.MockTransport(handler),
        timeout=client._timeout_s,
    )
    return client


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daemon_client: MagicMock,
    cloud_client: CloudCursorClient | None = None,
    api_key: str | None = "test-api-key",
) -> None:
    monkeypatch.setattr(
        cloud_cmd, "_make_sync_client", lambda *a, **kw: daemon_client
    )
    if cloud_client is not None:
        monkeypatch.setattr(
            cloud_cmd, "_build_cloud_client", lambda key: cloud_client
        )
    if api_key is None:
        monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    else:
        monkeypatch.setenv("CURSOR_API_KEY", api_key)


# ---------------------------------------------------------------------------
# Helper unit tests (no daemon / cloud)
# ---------------------------------------------------------------------------


def test_socket_path_uses_popola_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$POPOLA_HOME`` overrides the default ``~/.popola`` socket path."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    assert cloud_cmd._socket_path() == tmp_path / "popolad.sock"


def test_socket_path_default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``$POPOLA_HOME`` we resolve to ``~/.popola/popolad.sock``."""
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/some/home")
    sock = cloud_cmd._socket_path()
    assert sock.name == "popolad.sock"


def test_make_sync_client_returns_client(tmp_path: Path) -> None:
    """``_make_sync_client`` returns a :class:`httpx.Client` over UDS."""
    client = cloud_cmd._make_sync_client(socket_path=tmp_path / "missing.sock")
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_build_cloud_client_returns_client() -> None:
    """``_build_cloud_client`` returns a :class:`CloudCursorClient`."""
    client = cloud_cmd._build_cloud_client("test-api-key")
    try:
        assert isinstance(client, CloudCursorClient)
    finally:
        client.close()


def test_format_wall_clock_negative_seconds_clamps_to_zero() -> None:
    """A negative input clamps to ``0.0s`` and renders as ``0.0s``."""
    assert cloud_cmd._format_wall_clock(-5.0, is_live=False) == "0.0s"


def test_format_wall_clock_above_60_renders_hms() -> None:
    """``seconds >= 60`` renders as ``HH:MM:SS`` (no decimal)."""
    assert cloud_cmd._format_wall_clock(3661.0, is_live=False) == "01:01:01"


def test_format_wall_clock_live_appends_ellipsis() -> None:
    """``is_live=True`` suffixes the formatted base with ``…``."""
    assert cloud_cmd._format_wall_clock(5.0, is_live=True).endswith("…")


def test_parse_iso8601_handles_z_suffix() -> None:
    """The ``Z`` suffix is rewritten to ``+00:00`` before parsing."""
    dt = cloud_cmd._parse_iso8601("2026-05-08T10:00:00.000Z")
    assert dt is not None and dt.tzinfo is not None


def test_parse_iso8601_returns_none_on_garbage() -> None:
    """Malformed input → ``None`` (caller handles via 0.0 fallback)."""
    assert cloud_cmd._parse_iso8601("not a date") is None


def test_parse_iso8601_returns_none_on_empty() -> None:
    """Empty string → ``None``."""
    assert cloud_cmd._parse_iso8601("") is None


def test_parse_iso8601_naive_dt_gets_utc_tzinfo() -> None:
    """Naive ISO string is upgraded to tz-aware UTC (defensive)."""
    dt = cloud_cmd._parse_iso8601("2026-05-08T10:00:00")
    assert dt is not None and dt.tzinfo is not None


def test_compute_wall_clock_unparseable_returns_zero() -> None:
    """``_compute_wall_clock`` returns ``(0.0, False)`` on garbage input."""
    assert cloud_cmd._compute_wall_clock("garbage", "garbage", "running") == (
        0.0, False
    )


def test_str_field_returns_empty_for_non_string() -> None:
    """``_str_field`` returns ``""`` when the value is not a string."""
    assert cloud_cmd._str_field({"k": 42}, "k") == ""
    assert cloud_cmd._str_field({"k": "hi"}, "k") == "hi"


def test_summarise_run_events_returns_none_for_non_dict() -> None:
    """``_summarise_run_events`` on a non-dict returns ``None``."""
    assert cloud_cmd._summarise_run_events("not a dict") is None
    assert cloud_cmd._summarise_run_events(None) is None


def test_summarise_run_events_fallback_when_no_events_list() -> None:
    """Body without ``events`` falls back to status-derived summary."""
    out = cloud_cmd._summarise_run_events(
        {"status": "ERROR", "createdAt": "t1", "updatedAt": "t2"}
    )
    assert out is not None
    assert out["had_error"] is True
    assert out["tool_call_count"] == 0


def test_summarise_run_events_with_events_list_counts_types() -> None:
    """Body with events list counts tool / assistant / errors."""
    out = cloud_cmd._summarise_run_events(
        {
            "events": [
                {"type": "tool_call", "createdAt": "t1"},
                {"type": "assistant_message", "createdAt": "t2"},
                {"type": "error_event", "createdAt": "t3"},
            ]
        }
    )
    assert out is not None
    assert out["tool_call_count"] == 1
    assert out["assistant_message_count"] == 1
    assert out["had_error"] is True


def test_summarise_run_events_skips_non_dict_events() -> None:
    """Non-dict event entries are ignored (no IndexError)."""
    out = cloud_cmd._summarise_run_events(
        {"events": ["not a dict", None, {"type": "tool_call", "createdAt": "t"}]}
    )
    assert out is not None
    assert out["tool_call_count"] == 1


# ---------------------------------------------------------------------------
# _resolve_agent_model branches
# ---------------------------------------------------------------------------


def test_resolve_agent_model_returns_id_from_nested_dict() -> None:
    """``model: {"id": "..."}`` shape returns the inner id string."""
    def _h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": {"id": "claude-4"}})

    client = _build_cloud_mock_client(_h)
    try:
        assert cloud_cmd._resolve_agent_model(client, "bc-1") == "claude-4"
    finally:
        client.close()


def test_resolve_agent_model_returns_none_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Cursor :class:`Exception` from ``get_agent`` returns ``None`` + logs."""
    client = MagicMock(spec=CloudCursorClient)
    client.get_agent.side_effect = RuntimeError("not a cursor error")
    assert cloud_cmd._resolve_agent_model(client, "bc-1") is None


def test_resolve_agent_model_returns_none_on_non_dict_body() -> None:
    """A non-dict body from ``get_agent`` → ``None``."""
    client = MagicMock(spec=CloudCursorClient)
    client.get_agent.return_value = ["not", "a", "dict"]
    assert cloud_cmd._resolve_agent_model(client, "bc-1") is None


def test_resolve_agent_model_returns_none_when_no_model_field() -> None:
    """Body missing ``model`` key → ``None``."""
    client = MagicMock(spec=CloudCursorClient)
    client.get_agent.return_value = {"id": "bc-1"}
    assert cloud_cmd._resolve_agent_model(client, "bc-1") is None


def test_resolve_agent_model_returns_none_when_nested_id_empty() -> None:
    """``model: {"id": ""}`` → ``None`` (defensive empty-string guard)."""
    client = MagicMock(spec=CloudCursorClient)
    client.get_agent.return_value = {"model": {"id": ""}}
    assert cloud_cmd._resolve_agent_model(client, "bc-1") is None


def test_resolve_agent_model_cursor_error_returns_none() -> None:
    """A :class:`CursorCloudError` from ``get_agent`` is swallowed → ``None``."""
    client = MagicMock(spec=CloudCursorClient)
    client.get_agent.side_effect = CursorCloudError("boom", status_code=500)
    assert cloud_cmd._resolve_agent_model(client, "bc-1") is None


# ---------------------------------------------------------------------------
# _inject_events_summary branches
# ---------------------------------------------------------------------------


def test_inject_events_summary_returns_when_items_not_list() -> None:
    """Early return when ``items`` is not a list."""
    body: dict[str, Any] = {"items": "not a list"}
    cloud_cmd._inject_events_summary(MagicMock(spec=CloudCursorClient), "bc", body)
    assert body == {"items": "not a list"}


def test_inject_events_summary_skips_non_dict_rows() -> None:
    """Rows that are not dicts are silently skipped."""
    body: dict[str, Any] = {"items": ["not a dict", None]}
    client = MagicMock(spec=CloudCursorClient)
    cloud_cmd._inject_events_summary(client, "bc", body)
    client.get_run.assert_not_called()


def test_inject_events_summary_marks_row_when_id_missing() -> None:
    """A row without a string ``id`` gets ``events_summary = None`` (no API call)."""
    body: dict[str, Any] = {"items": [{"id": ""}, {"id": 42}]}
    client = MagicMock(spec=CloudCursorClient)
    cloud_cmd._inject_events_summary(client, "bc", body)
    for row in body["items"]:
        assert row["events_summary"] is None
    client.get_run.assert_not_called()


def test_inject_events_summary_unexpected_error_degrades_to_null(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-Cursor :class:`Exception` per-row degrades to ``events_summary=None``."""
    body: dict[str, Any] = {"items": [{"id": "r-1"}]}
    client = MagicMock(spec=CloudCursorClient)
    client.get_run.side_effect = RuntimeError("non-cursor boom")
    cloud_cmd._inject_events_summary(client, "bc", body)
    assert body["items"][0]["events_summary"] is None
    captured = capsys.readouterr()
    assert "warning" in captured.err


# ---------------------------------------------------------------------------
# _emit_table empty-rows paths
# ---------------------------------------------------------------------------


def test_emit_table_empty_no_cursor_renders_no_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``items=[]`` + no ``nextCursor`` → ``No runs for task ...`` line."""
    cloud_cmd._emit_table(
        task_id="t-1",
        agent_id="bc-1",
        body={"items": [], "nextCursor": None},
        model_id=None,
    )
    out = capsys.readouterr().out
    assert "No runs for task t-1" in out


def test_emit_table_empty_with_cursor_renders_no_runs_in_page(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``items=[]`` + non-null ``nextCursor`` → ``No runs in this page`` + footer."""
    cloud_cmd._emit_table(
        task_id="t-1",
        agent_id="bc-1",
        body={"items": [], "nextCursor": "next-page"},
        model_id=None,
    )
    out = capsys.readouterr().out
    assert "No runs in this page" in out
    assert "more available" in out


# ---------------------------------------------------------------------------
# Daemon-side error paths (cloud_cmd.runs)
# ---------------------------------------------------------------------------


def test_cloud_runs_daemon_500_exits_1(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon non-200/non-404 status → exit ``1`` (mirrors ``popola status``)."""
    daemon = _make_daemon_client(
        response=_make_response(status_code=500, body={"detail": "boom"}),
    )
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001"])
    assert result.exit_code == 1, _combined(result)
    assert "status unexpected 500" in _combined(result)


def test_cloud_runs_daemon_non_dict_response_exits_1(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon 200 with non-dict body → exit ``1`` (defensive guard)."""
    daemon = _make_daemon_client(
        response=_make_response(status_code=200, body=["a", "b"]),
    )
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001"])
    assert result.exit_code == 1, _combined(result)
    assert "not a JSON object" in _combined(result)


def test_cloud_runs_missing_cursor_agent_id_exits_4(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cursor_agent_id`` missing on a cloud-runtime task → exit ``4`` (retry hint)."""
    payload = _build_status_payload(cursor_agent_id=None)
    daemon = _make_daemon_client(
        response=_make_response(status_code=200, body=payload),
    )
    _wire(monkeypatch, daemon_client=daemon, cloud_client=None)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001"])
    assert result.exit_code == 4, _combined(result)
    assert "cursor_agent_id not yet populated" in _combined(result)


# ---------------------------------------------------------------------------
# _handle_cloud_error default path
# ---------------------------------------------------------------------------


def test_handle_cloud_error_generic_uses_cli_exit_attr(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic 410 ``stream_expired`` falls through to the default path."""
    daemon = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_status_payload()),
    )

    def _h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/runs"):
            return httpx.Response(
                410,
                json={"error": {"code": "stream_expired", "message": "x"}},
            )
        return httpx.Response(200, json={"id": "bc-1", "model": "x"})

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001"])
    # ``stream_expired`` catalog cli_exit = 75 → default branch returns 75.
    assert result.exit_code == 75, _combined(result)


def test_handle_cloud_error_403_role_forbidden_exits_77(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``role_forbidden`` (auth subclass) → exit 77 (auth path)."""
    daemon = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_status_payload()),
    )

    def _h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/runs"):
            return httpx.Response(
                403,
                json={"error": {"code": "role_forbidden", "message": "x"}},
            )
        return httpx.Response(200, json={"id": "bc-1", "model": "x"})

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001"])
    assert result.exit_code == 77, _combined(result)


def test_cloud_runs_no_args_shows_help_or_error(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola cloud runs`` with no task_id → typer prints help / exits non-zero."""
    _wire(monkeypatch, daemon_client=MagicMock(), cloud_client=None)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs"])
    assert result.exit_code != 0


def test_cloud_runs_render_terminal_state_no_ellipsis(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FINISHED row renders ``wall_clock`` without the ``…`` suffix."""
    payload = _build_status_payload()
    daemon = _make_daemon_client(
        response=_make_response(status_code=200, body=payload),
    )

    def _h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/runs"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "r1",
                            "status": "FINISHED",
                            "createdAt": "2026-05-08T09:00:00.000Z",
                            "updatedAt": "2026-05-08T09:00:05.000Z",
                        }
                    ],
                    "nextCursor": None,
                },
            )
        return httpx.Response(200, json={"id": "bc", "model": "m"})

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001"])
    assert result.exit_code == 0, _combined(result)
    # Terminal row → no ellipsis on wall_clock.
    out = _combined(result)
    assert "5.0s" in out


def test_cloud_runs_emit_json_with_no_items(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` with empty ``items`` returns a valid JSON object."""
    payload = _build_status_payload()
    daemon = _make_daemon_client(
        response=_make_response(status_code=200, body=payload),
    )

    def _h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/runs"):
            return httpx.Response(200, json={"items": [], "nextCursor": None})
        return httpx.Response(200, json={"id": "bc"})

    cloud = _build_cloud_mock_client(_h)
    _wire(monkeypatch, daemon_client=daemon, cloud_client=cloud)
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "runs", "t-001", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload_out = json.loads(_combined(result).strip().splitlines()[-1])
    assert payload_out["runs"] == []
    assert payload_out["has_more"] is False


def test_cloud_runs_skips_non_dict_items_in_build_table() -> None:
    """``_build_runs_table`` skips non-dict items (defensive parse)."""
    rows = cloud_cmd._build_runs_table(
        ["not a dict", None, {"id": "r1", "status": "FINISHED",
                             "createdAt": "2026-05-08T09:00:00.000Z",
                             "updatedAt": "2026-05-08T09:00:01.000Z"}],
        model_id=None,
    )
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r1"


def test_cloud_runs_render_table_with_dash_for_missing_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A row with empty ``state`` renders ``-`` per spec §3.1."""
    cloud_cmd._emit_table(
        task_id="t",
        agent_id="bc",
        body={
            "items": [
                {
                    "id": "r1",
                    "status": "",
                    "createdAt": "2026-05-08T09:00:00.000Z",
                    "updatedAt": "2026-05-08T09:00:01.000Z",
                }
            ],
            "nextCursor": None,
        },
        model_id=None,
    )
    out = capsys.readouterr().out
    assert "-" in out
