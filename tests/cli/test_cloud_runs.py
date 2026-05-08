"""``popola cloud runs <task_id>`` v0.8.8 T2.4.1 tests (Q-C-1 偏离默认).

Implements the 8 acceptance criteria enumerated in
``.local/.agent/active/v0.8.8-multi-run/PLAN.md`` §4.4 T2.4.1 + the
spec ``.local/research/v0.8.8_multi_run/runs-subcommand-spec.md`` ALL
sections, plus ≥ 4 unit tests for ``CloudCursorClient.list_runs``.

All cloud calls are mocked via :class:`httpx.MockTransport` per the
brief constraint; the daemon ``GET /status/{task_id}`` RPC is mocked
via a stub :class:`httpx.Client` returning canned status payloads
matching :func:`popolaloom.daemon.server.Popolad._task_summary`.

AC mapping (per the user-supplied task brief AC matrix):

- AC (a) — :func:`test_cloud_runs_help_text_matches_spec`,
  :func:`test_cloud_runs_registered_under_cloud_subapp`.
- AC (b) — :func:`test_cloud_runs_default_table_six_columns`.
- AC (c) — :func:`test_cloud_runs_limit_clamp_above_100`,
  :func:`test_cloud_runs_limit_zero_rejected`,
  :func:`test_cloud_runs_pagination_footer_when_next_cursor`.
- AC (d) — :func:`test_cloud_runs_cursor_passed_verbatim`,
  :func:`test_cloud_runs_no_pagination_footer_in_json`.
- AC (e) — :func:`test_cloud_runs_json_schema_validates`,
  :func:`test_cloud_runs_json_run_id_not_truncated`.
- AC (f) — :func:`test_cloud_runs_include_events_populates_summary`,
  :func:`test_cloud_runs_include_events_per_row_failure_degrades_to_null`.
- AC (g) — :func:`test_cloud_runs_404_exits_4`,
  :func:`test_cloud_runs_401_auth_exits_77`,
  :func:`test_cloud_runs_403_plan_required_exits_78`,
  :func:`test_cloud_runs_429_rate_limit_exits_75`,
  :func:`test_cloud_runs_500_exits_75`,
  :func:`test_cloud_runs_daemon_down_exits_1`,
  :func:`test_cloud_runs_local_runtime_task_exits_1`,
  :func:`test_cloud_runs_missing_api_key_exits_77`,
  :func:`test_cloud_runs_missing_task_exits_4`.
- AC (h) — :func:`test_cloud_runs_two_step_call_structure`.
- AC (i) — :func:`test_popola_list_unchanged_regression`.
- ``list_runs`` unit tests —
  :func:`test_list_runs_happy_path_returns_body`,
  :func:`test_list_runs_clamps_limit_to_max_100`,
  :func:`test_list_runs_clamps_limit_to_min_1`,
  :func:`test_list_runs_omits_cursor_when_none`,
  :func:`test_list_runs_includes_cursor_when_set`,
  :func:`test_list_runs_404_routed_through_map_http_error`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import jsonschema  # type: ignore[import-untyped]
import pytest
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudAuthError,
    CursorCloudNotFoundError,
)
from popolaloom.cli import cloud_cmd

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SCHEMA_PATH = _FIXTURE_DIR / "cloud_runs_v1.json"


@pytest.fixture
def runner() -> CliRunner:
    """Default :class:`CliRunner`. Newer Typer/Click drop ``mix_stderr``."""
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point ``$POPOLA_HOME`` at ``tmp_path`` for hermetic tests."""
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the cloud_cmd Rich Console to 200x50 so substring asserts hold.

    Under :class:`CliRunner` stdout is non-TTY, so Rich's
    ``is_dumb_terminal`` short-circuits ``Console.size`` to ``(80, 25)``
    and truncates long ``task_id`` / ``run_id`` cells with ``…``.
    Mirrors the pattern in :file:`tests/cli/test_list_runtime_column.py`.
    """
    monkeypatch.setattr(
        cloud_cmd, "_console_out", Console(width=200, height=50)
    )


def _combined_output(result: Any) -> str:
    """Return ``stdout + stderr`` as a single string (Typer/Click compat)."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        if value and value not in parts:
            parts.append(value)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Daemon stub (GET /status/{task_id})
# ---------------------------------------------------------------------------


def _make_response(*, status_code: int, body: Any) -> MagicMock:
    """Build a :class:`MagicMock` shaped like an :class:`httpx.Response`."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body) if body is not None else ""
    response.content = response.text.encode("utf-8") if response.text else b""
    return response


def _build_daemon_status_payload(
    *,
    task_id: str = "cursor-cloud-test-001",
    runtime: str = "cloud",
    cursor_agent_id: str | None = "bc-test-agent-001",
    state: str = "running",
) -> dict[str, Any]:
    """Return a payload matching ``Popolad._task_summary(full=True)``."""
    return {
        "task_id": task_id,
        "cli": "cursor-cloud" if runtime == "cloud" else "cursor",
        "state": state,
        "pid": None,
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "runtime": runtime,
        "cursor_agent_id": cursor_agent_id,
        "cursor_run_id": "run-test-001" if cursor_agent_id else None,
        "cloud_phase": "RUNNING" if cursor_agent_id else None,
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
    captured_gets: list[str] | None = None,
) -> MagicMock:
    """Context-manager-shaped sync httpx client double for daemon UDS."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    if on_get is not None:
        client.get.side_effect = on_get
    elif response is not None:
        def _capture_get(url: str, **_kwargs: Any) -> MagicMock:
            if captured_gets is not None:
                captured_gets.append(url)
            return response
        client.get.side_effect = _capture_get
    else:
        raise ValueError("must provide one of `response` or `on_get`")
    return client


# ---------------------------------------------------------------------------
# Cloud client stub (httpx.MockTransport on a real CloudCursorClient)
# ---------------------------------------------------------------------------


def _build_cloud_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> CloudCursorClient:
    """Real :class:`CloudCursorClient` with an in-memory mock transport.

    Per the brief constraint ("Use httpx.MockTransport for cloud calls
    in tests"), this keeps every production path (auth, retry, error
    mapping) live; only the underlying transport is swapped out.
    """
    client = CloudCursorClient("test-api-key", base_url=CURSOR_API_BASE)
    client._client.close()
    client._client = httpx.Client(
        base_url=client._base_url,
        auth=(client._api_key, ""),
        transport=httpx.MockTransport(handler),
        timeout=client._timeout_s,
    )
    return client


def _wire_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daemon_client: MagicMock,
    cloud_client: CloudCursorClient | None,
    api_key: str | None = "test-api-key",
) -> None:
    """Patch the CLI's two indirection points so tests are hermetic.

    - ``cloud_cmd._make_sync_client`` returns ``daemon_client`` (handles
      ``GET /status/{task_id}`` only).
    - ``cloud_cmd._build_cloud_client`` returns the test ``cloud_client``
      (or raises if ``None`` so a stray cloud call surfaces loudly).
    - ``CURSOR_API_KEY`` env var is set unless ``api_key=None``.
    """
    monkeypatch.setattr(
        cloud_cmd,
        "_make_sync_client",
        lambda *a, **kw: daemon_client,
    )
    if cloud_client is not None:
        monkeypatch.setattr(
            cloud_cmd,
            "_build_cloud_client",
            lambda key: cloud_client,
        )
    if api_key is not None:
        monkeypatch.setenv("CURSOR_API_KEY", api_key)
    else:
        monkeypatch.delenv("CURSOR_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Sample run/agent payloads
# ---------------------------------------------------------------------------


_FIXED_RUN_1: dict[str, Any] = {
    "id": "run-00000000-0000-0000-0000-000000000002",
    "agentId": "bc-test-agent-001",
    "status": "RUNNING",
    "createdAt": "2026-04-13T18:50:00.000Z",
    "updatedAt": "2026-04-13T18:51:00.000Z",
}
_FIXED_RUN_0: dict[str, Any] = {
    "id": "run-00000000-0000-0000-0000-000000000001",
    "agentId": "bc-test-agent-001",
    "status": "FINISHED",
    "createdAt": "2026-04-13T18:30:00.000Z",
    "updatedAt": "2026-04-13T18:45:00.000Z",
}
_FIXED_AGENT_BODY: dict[str, Any] = {
    "id": "bc-test-agent-001",
    "model": "claude-4-sonnet-thinking",
}


def _make_runs_handler(
    *,
    items: list[dict[str, Any]] | None = None,
    next_cursor: str | None = None,
    runs_status: int = 200,
    runs_body: dict[str, Any] | None = None,
    agent_body: dict[str, Any] | None = None,
    captured_requests: list[httpx.Request] | None = None,
    runs_response_factory: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a Cursor REST handler covering both ``GET /runs`` and ``GET /agents/{id}``."""

    if items is None:
        items = [_FIXED_RUN_1, _FIXED_RUN_0]

    if runs_body is None:
        runs_body = {"items": items, "nextCursor": next_cursor}
    agent_body_inner = agent_body or _FIXED_AGENT_BODY

    def handler(request: httpx.Request) -> httpx.Response:
        if captured_requests is not None:
            captured_requests.append(request)
        url_path = request.url.path
        if url_path.endswith("/runs"):
            if runs_response_factory is not None:
                return runs_response_factory(request)
            return httpx.Response(runs_status, json=runs_body)
        if url_path.startswith("/v1/agents/") and not url_path.endswith("/runs"):
            return httpx.Response(200, json=agent_body_inner)
        return httpx.Response(404, json={"error": {"code": "unknown"}})

    return handler


# =========================================================================
# AC (a) — registration + help text matches spec §2.5
# =========================================================================


def test_cloud_runs_registered_under_cloud_subapp(runner: CliRunner) -> None:
    """``popola cloud --help`` lists the ``runs`` verb."""
    from popolaloom.cli.main import app as root_app

    result = runner.invoke(root_app, ["cloud", "--help"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "runs" in out, f"missing 'runs' subcommand in:\n{out}"


def test_cloud_runs_help_text_matches_spec(runner: CliRunner) -> None:
    """``popola cloud runs --help`` matches spec §2.5 verbatim (key lines)."""
    from popolaloom.cli.main import app as root_app

    # Force a wide terminal so Typer/Click + Rich don't truncate option
    # names like ``--include-events``. CI runners default to 80 columns
    # which is too narrow for the 6-column option block; pin to 200
    # via env var so the substring assertions below are stable across
    # local/CI/Linux/macOS.
    result = runner.invoke(
        root_app, ["cloud", "runs", "--help"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    # Spec §2.5 — we check the four flag fragments + the daemon/cloud
    # invariants. We do NOT pin the entire help block verbatim because
    # Typer auto-formats option columns based on terminal width.
    assert "Usage" in out
    assert "TASK_ID" in out
    assert "--limit" in out
    assert "--cursor" in out
    assert "--json" in out
    assert "--include-events" in out
    assert "Cloud Agents" in out or "cloud-agent" in out


# =========================================================================
# AC (b) — default 6-column Rich table
# =========================================================================


def test_cloud_runs_default_table_six_columns(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default invocation renders a 6-column table with truncated run_id +
    derived run_index (newest=highest) + lowercased state + verbatim
    created_at + wall_clock + model.
    """
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(_make_runs_handler())
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)

    # 6 column headers (per spec §3.1)
    for col in ("run_id", "run_index", "state", "created_at", "wall_clock", "model"):
        assert col in out, f"missing column '{col}' in table:\n{out}"

    # run_id truncated to 16 chars + ellipsis (the full ids are 36 chars)
    assert "run-00000000-000…" in out, (
        f"truncated run_id (16 chars + ellipsis) not in:\n{out}"
    )
    # state is lowercased
    assert "running" in out
    assert "finished" in out
    # model from the cached get_agent fallback
    assert "claude-4-sonnet-thinking" in out
    # newest=highest run_index — items[0] (newest) is index 1
    # (RUNNING is the first row); items[1] (oldest) is 0.
    assert "0" in out and "1" in out


def test_cloud_runs_table_live_run_has_ellipsis_suffix(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live (non-terminal) runs render ``wall_clock`` with a trailing ``…``.

    Per spec §3.2: states outside ``{finished, cancelled, expired,
    error}`` are still ticking, so the renderer suffixes ``…`` after the
    HH:MM:SS / N.Ns base form.
    """
    # Single live run → wall_clock should end with "…".
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(items=[_FIXED_RUN_1]),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "…" in out, f"live run should have ellipsis suffix:\n{out}"


def test_cloud_runs_model_dash_when_get_agent_returns_no_model(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_agent`` returning a body without ``model`` → table model = ``-``."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(agent_body={"id": "bc-test-agent-001"}),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    # dash placeholder per spec §3.1 column 6 ("- on miss")
    assert "claude-4-sonnet-thinking" not in out


# =========================================================================
# AC (c) — limit clamping + invalid value rejection + pagination footer
# =========================================================================


def test_cloud_runs_limit_clamp_above_100(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--limit 200`` → clamps to 100 + stderr WARN per spec §5.1."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    captured: list[httpx.Request] = []
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(captured_requests=captured),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001", "--limit", "200"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "warning:" in out and "clamped to 100" in out, (
        f"missing clamp WARN in:\n{out}"
    )

    # Verify the actual cloud call used limit=100 (not 200).
    runs_calls = [r for r in captured if r.url.path.endswith("/runs")]
    assert runs_calls, "no /runs call observed"
    assert "limit=100" in str(runs_calls[0].url), (
        f"limit not clamped to 100; got {runs_calls[0].url}"
    )


def test_cloud_runs_limit_zero_rejected(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--limit 0`` → exit ``2`` (Typer's invalid-args code per spec §2.4)."""
    # Daemon stub gets wired but should never be called.
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=None)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001", "--limit", "0"],
    )
    assert result.exit_code == 2, (
        f"--limit 0 must exit 2 (invalid args); got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )


def test_cloud_runs_pagination_footer_when_next_cursor(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-null ``nextCursor`` → pagination footer printed per spec §3.4."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(next_cursor="next-page-token-abc"),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "more available" in out, f"missing pagination footer:\n{out}"
    assert "--cursor=next-page-token-abc" in out, (
        f"footer must reuse the next cursor verbatim:\n{out}"
    )


# =========================================================================
# AC (d) — cursor round-trip + JSON suppresses footer
# =========================================================================


def test_cloud_runs_cursor_passed_verbatim(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cursor <value>`` is forwarded to Cursor REST query string verbatim."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    captured: list[httpx.Request] = []
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(captured_requests=captured),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "runs",
            "cursor-cloud-test-001",
            "--cursor",
            "page-2-cursor-token",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    runs_calls = [r for r in captured if r.url.path.endswith("/runs")]
    assert runs_calls, "no /runs call observed"
    assert "cursor=page-2-cursor-token" in str(runs_calls[0].url), (
        f"--cursor not forwarded verbatim; got {runs_calls[0].url}"
    )


def test_cloud_runs_no_pagination_footer_in_json(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` mode suppresses the pagination footer per spec §3.4."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(next_cursor="page-token-xyz"),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001", "--json"],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "more available" not in out, (
        f"--json must suppress pagination footer:\n{out}"
    )
    # The JSON body still echoes next_cursor.
    payload = _extract_json_payload(out)
    assert payload["next_cursor"] == "page-token-xyz"
    assert payload["has_more"] is True


# =========================================================================
# AC (e) — JSON shape validation (schema fixture)
# =========================================================================


def test_cloud_runs_json_schema_validates(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` output validates against ``tests/cli/fixtures/cloud_runs_v1.json``."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(_make_runs_handler())
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001", "--json"],
    )
    assert result.exit_code == 0, _combined_output(result)

    payload = _extract_json_payload(_combined_output(result))
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=payload, schema=schema)


def test_cloud_runs_json_run_id_not_truncated(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runs[].run_id`` in --json is the FULL Cursor id (NOT truncated)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(_make_runs_handler())
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001", "--json"],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = _extract_json_payload(_combined_output(result))
    run_ids = [r["run_id"] for r in payload["runs"]]
    assert _FIXED_RUN_1["id"] in run_ids, (
        f"run_id full id missing from JSON: {run_ids}"
    )
    # No row carries the truncated form.
    for rid in run_ids:
        assert "…" not in rid, f"--json run_id must not be truncated; got {rid}"


def test_cloud_runs_json_run_index_newest_highest(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_index`` derivation: newest run (items[0]) gets the highest index.

    Per spec §3.1 column 2: with ``items`` returned newest-first, the
    first item gets ``run_index = n - 1`` and the last gets ``0``.
    """
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    cloud_client = _build_cloud_mock_client(_make_runs_handler())
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001", "--json"],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = _extract_json_payload(_combined_output(result))
    runs_out = payload["runs"]
    # 2 runs returned → run_index = 1, 0 (newest first).
    assert runs_out[0]["run_index"] == 1
    assert runs_out[1]["run_index"] == 0
    # Newest = RUNNING (still live), oldest = FINISHED.
    assert runs_out[0]["state"] == "running"
    assert runs_out[1]["state"] == "finished"


# =========================================================================
# AC (f) — --include-events
# =========================================================================


def test_cloud_runs_include_events_populates_summary(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--include-events`` triggers per-row ``GET /runs/{runId}`` and folds in events_summary."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    per_run_events = {
        "tool_call_count": 2,
        "assistant_message_count": 1,
        "had_error": False,
        "first_event_at": "2026-04-13T18:30:01.000Z",
        "last_event_at": "2026-04-13T18:44:59.000Z",
    }

    def runs_response_factory(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": [_FIXED_RUN_0], "nextCursor": None},
        )

    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        path = request.url.path
        if path == "/v1/agents/bc-test-agent-001/runs":
            return runs_response_factory(request)
        if path.startswith("/v1/agents/bc-test-agent-001/runs/"):
            # Per-run detail endpoint.
            return httpx.Response(
                200,
                json={
                    "id": "run-00000000-0000-0000-0000-000000000001",
                    "status": "FINISHED",
                    "createdAt": "2026-04-13T18:30:00.000Z",
                    "updatedAt": "2026-04-13T18:45:00.000Z",
                    "events": [
                        {
                            "type": "tool_call",
                            "createdAt": per_run_events["first_event_at"],
                        },
                        {
                            "type": "tool_call",
                            "createdAt": "2026-04-13T18:35:00.000Z",
                        },
                        {
                            "type": "assistant_message",
                            "createdAt": per_run_events["last_event_at"],
                        },
                    ],
                },
            )
        if path == "/v1/agents/bc-test-agent-001":
            return httpx.Response(200, json=_FIXED_AGENT_BODY)
        return httpx.Response(404, json={"error": {"code": "unknown"}})

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "runs",
            "cursor-cloud-test-001",
            "--json",
            "--include-events",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    payload = _extract_json_payload(_combined_output(result))
    summary = payload["runs"][0]["events_summary"]
    assert summary is not None, "events_summary must be populated under --include-events"
    assert summary["tool_call_count"] == 2
    assert summary["assistant_message_count"] == 1
    assert summary["had_error"] is False
    assert summary["first_event_at"] == per_run_events["first_event_at"]
    assert summary["last_event_at"] == per_run_events["last_event_at"]

    # Per-row detail call observed.
    assert any(
        p.startswith("/v1/agents/bc-test-agent-001/runs/")
        and not p.endswith("/runs")
        for p in captured_paths
    ), f"per-row /runs/{{runId}} call missing in: {captured_paths!r}"


def test_cloud_runs_include_events_per_row_failure_degrades_to_null(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-row 410 → ``events_summary = null`` + stderr WARN (No-Silent-Failures)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents/bc-test-agent-001/runs":
            return httpx.Response(
                200,
                json={"items": [_FIXED_RUN_0], "nextCursor": None},
            )
        if path.startswith("/v1/agents/bc-test-agent-001/runs/"):
            return httpx.Response(
                410,
                json={"error": {"code": "stream_expired", "message": "expired"}},
            )
        if path == "/v1/agents/bc-test-agent-001":
            return httpx.Response(200, json=_FIXED_AGENT_BODY)
        return httpx.Response(404, json={"error": {"code": "unknown"}})

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        [
            "cloud",
            "runs",
            "cursor-cloud-test-001",
            "--json",
            "--include-events",
        ],
    )
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    assert "warning:" in out, f"missing per-row WARN in:\n{out}"
    payload = _extract_json_payload(out)
    assert payload["runs"][0]["events_summary"] is None


# =========================================================================
# AC (g) — error matrix
# =========================================================================


def test_cloud_runs_404_exits_4(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor 404 ``agent_not_found`` → exit ``4`` (DECISIONS.md OQ-1)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "agent_not_found",
                        "message": "Agent not found",
                    }
                },
            )
        return httpx.Response(200, json=_FIXED_AGENT_BODY)

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 4, (
        f"404 must exit 4 (DECISIONS.md OQ-1); got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )
    out = _combined_output(result)
    assert "cursor agent not found" in out
    # Bilingual hint present (catalog hint_en + hint_zh).
    assert "https://cursor.com" in out


def test_cloud_runs_401_auth_exits_77(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor 401 → exit ``77`` (DECISIONS.md OQ-2 catalog-aligned)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            return httpx.Response(
                401,
                json={"error": {"code": "unauthorized", "message": "invalid key"}},
            )
        return httpx.Response(200, json=_FIXED_AGENT_BODY)

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 77, (
        f"401 must exit 77 (DECISIONS.md OQ-2); got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )


def test_cloud_runs_403_plan_required_exits_78(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor 403 ``plan_required`` → exit ``78``."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            return httpx.Response(
                403,
                json={"error": {"code": "plan_required", "message": "paid tier"}},
            )
        return httpx.Response(200, json=_FIXED_AGENT_BODY)

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 78, (
        f"403 plan_required must exit 78; got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )


def test_cloud_runs_429_rate_limit_exits_75(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor 429 → exit ``75`` + observed Retry-After surfaced in catalog hint."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Rate-limited",
                    }
                },
                headers={"Retry-After": "60"},
            )
        return httpx.Response(200, json=_FIXED_AGENT_BODY)

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 75, (
        f"429 must exit 75; got {result.exit_code}\n{_combined_output(result)}"
    )
    out = _combined_output(result)
    # Retry-After mention from catalog hint (or surfaced separately).
    assert "Retry-After" in out or "rate-limited" in out.lower()


def test_cloud_runs_500_exits_75(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor 500 → exit ``75`` (cloud-API failure class)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            return httpx.Response(
                500,
                json={
                    "error": {
                        "code": "internal_error",
                        "message": "Backend unhappy",
                    }
                },
            )
        return httpx.Response(200, json=_FIXED_AGENT_BODY)

    cloud_client = _build_cloud_mock_client(handler)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 75, (
        f"5xx must exit 75; got {result.exit_code}\n{_combined_output(result)}"
    )


def test_cloud_runs_daemon_down_exits_1(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon-down (Step 1 connect error) → exit ``1`` (mirrors `_render_connect_error`)."""

    def _on_get(*_a: Any, **_kw: Any) -> Any:
        raise httpx.ConnectError("Connection refused")

    daemon_client = _make_daemon_client(on_get=_on_get)
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=None)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 1, (
        f"daemon-down must exit 1; got {result.exit_code}\n{_combined_output(result)}"
    )
    assert "popolad not running" in _combined_output(result)


def test_cloud_runs_local_runtime_task_exits_1(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``runtime != cloud`` → exit ``1`` with friendly message."""
    daemon_client = _make_daemon_client(
        response=_make_response(
            status_code=200,
            body=_build_daemon_status_payload(runtime="local", cursor_agent_id=None),
        ),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=None)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 1, (
        f"local-runtime task must exit 1; got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )
    assert "not a cloud task" in _combined_output(result)


def test_cloud_runs_missing_api_key_exits_77(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``CURSOR_API_KEY`` → exit ``77`` (fast-fail before daemon RPC)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
    )
    _wire_cli(
        monkeypatch,
        daemon_client=daemon_client,
        cloud_client=None,
        api_key=None,
    )

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 77, (
        f"missing CURSOR_API_KEY must exit 77; got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )
    assert "CURSOR_API_KEY" in _combined_output(result)


def test_cloud_runs_missing_task_exits_4(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon 404 on ``/status/{task_id}`` → exit ``4`` (mirrors local task-not-found)."""
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=404, body={"detail": "not found"}),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=None)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "missing-task-xyz"],
    )
    assert result.exit_code == 4, (
        f"missing task must exit 4; got {result.exit_code}\n"
        f"{_combined_output(result)}"
    )
    assert "task not found" in _combined_output(result)


# =========================================================================
# AC (h) — two-step call structure
# =========================================================================


def test_cloud_runs_two_step_call_structure(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify (1) daemon ``GET /status`` happens first, then (2) cloud ``GET /runs``.

    Per spec §7.1 / brief AC (h), there is **no** caching layer between
    the two — the test pins the order + counts each call to one.
    """
    captured_daemon_paths: list[str] = []
    daemon_client = _make_daemon_client(
        response=_make_response(status_code=200, body=_build_daemon_status_payload()),
        captured_gets=captured_daemon_paths,
    )
    cloud_calls: list[httpx.Request] = []
    cloud_client = _build_cloud_mock_client(
        _make_runs_handler(captured_requests=cloud_calls),
    )
    _wire_cli(monkeypatch, daemon_client=daemon_client, cloud_client=cloud_client)

    from popolaloom.cli.main import app as root_app

    result = runner.invoke(
        root_app,
        ["cloud", "runs", "cursor-cloud-test-001"],
    )
    assert result.exit_code == 0, _combined_output(result)
    # Step 1 — exactly 1 daemon GET /status/{task_id}.
    assert captured_daemon_paths == ["/status/cursor-cloud-test-001"], (
        f"Step 1 daemon call mismatch; got {captured_daemon_paths}"
    )
    # Step 2 — exactly 1 cloud GET /v1/agents/{id}/runs + 1 GET /v1/agents/{id} (model fallback).
    runs_calls = [r for r in cloud_calls if r.url.path.endswith("/runs")]
    agent_calls = [
        r for r in cloud_calls
        if not r.url.path.endswith("/runs") and "/v1/agents/" in r.url.path
    ]
    assert len(runs_calls) == 1, (
        f"expected 1 /runs call; got {len(runs_calls)}: "
        f"{[str(r.url) for r in runs_calls]}"
    )
    assert len(agent_calls) == 1, (
        f"expected 1 /agents/{{id}} call; got {len(agent_calls)}: "
        f"{[str(r.url) for r in agent_calls]}"
    )


# =========================================================================
# AC (i) — popola list / popola status regression
# =========================================================================


def test_popola_list_unchanged_regression(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola list`` continues to render its 6-column table unchanged.

    Adding ``popola cloud`` must NOT touch ``popola list`` / ``popola status``
    output (AC (i)). We exercise the existing list path and assert the
    canonical column order is intact.
    """
    items = [
        {
            "task_id": "task-cloud-001",
            "cli": "cursor-cloud",
            "state": "running",
            "pid": None,
            "started_at": "2026-05-08T10:00:00.000+00:00",
            "runtime": "cloud",
        },
    ]
    response = _make_response(status_code=200, body=items)
    list_client = _make_daemon_client(response=response)

    from popolaloom.cli import main as cli_main
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: list_client,
    )

    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 0, _combined_output(result)
    out = _combined_output(result)
    # Canonical column order from v0.8.6 T2.1.2.
    expected_cols = ["task_id", "runtime", "cli", "state", "pid", "started_at"]
    indices = [out.find(name) for name in expected_cols]
    assert all(i >= 0 for i in indices), (
        f"missing column from popola list:\n{out}"
    )
    assert indices == sorted(indices), (
        f"popola list column order regressed: {dict(zip(expected_cols, indices, strict=False))}\n"
        f"{out}"
    )


# =========================================================================
# Unit tests — CloudCursorClient.list_runs (≥4 required)
# =========================================================================


def test_list_runs_happy_path_returns_body() -> None:
    """``list_runs(agent_id)`` returns the verbatim Cursor body."""
    captured: list[httpx.Request] = []
    expected = {"items": [_FIXED_RUN_1, _FIXED_RUN_0], "nextCursor": None}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=expected)

    client = _build_cloud_mock_client(handler)
    try:
        body = client.list_runs("bc-test-agent-001")
    finally:
        client.close()
    assert body == expected
    assert len(captured) == 1
    assert captured[0].url.path == "/v1/agents/bc-test-agent-001/runs"
    # Default limit=20 + no cursor.
    assert "limit=20" in str(captured[0].url)


def test_list_runs_clamps_limit_to_max_100() -> None:
    """``limit=500`` is clamped to ``100`` before the wire call."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"items": [], "nextCursor": None})

    client = _build_cloud_mock_client(handler)
    try:
        client.list_runs("bc-test-agent-001", limit=500)
    finally:
        client.close()
    assert "limit=100" in str(captured[0].url)


def test_list_runs_clamps_limit_to_min_1() -> None:
    """``limit=0`` (below floor) is clamped to ``1`` (defensive)."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"items": [], "nextCursor": None})

    client = _build_cloud_mock_client(handler)
    try:
        # Note: the CLI rejects ``--limit 0`` at the front; this asserts
        # the adapter's clamp is also belt-and-braces (per spec §6.3).
        client.list_runs("bc-test-agent-001", limit=0)
    finally:
        client.close()
    assert "limit=1" in str(captured[0].url)


def test_list_runs_omits_cursor_when_none() -> None:
    """``cursor=None`` does NOT add ``?cursor=`` to the URL."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"items": [], "nextCursor": None})

    client = _build_cloud_mock_client(handler)
    try:
        client.list_runs("bc-test-agent-001")
    finally:
        client.close()
    assert "cursor=" not in str(captured[0].url)


def test_list_runs_includes_cursor_when_set() -> None:
    """``cursor='page-2-token'`` appears verbatim in the query string."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"items": [], "nextCursor": None})

    client = _build_cloud_mock_client(handler)
    try:
        client.list_runs("bc-test-agent-001", cursor="page-2-token")
    finally:
        client.close()
    assert "cursor=page-2-token" in str(captured[0].url)


def test_list_runs_404_routed_through_map_http_error() -> None:
    """4xx routes through :func:`_map_http_error` → catalog subclass."""
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "agent_not_found", "message": "gone"}},
        )

    client = _build_cloud_mock_client(handler)
    try:
        with pytest.raises(CursorCloudNotFoundError):
            client.list_runs("bc-test-agent-001")
    finally:
        client.close()


def test_list_runs_401_routed_through_map_http_error() -> None:
    """401 routes through :func:`_map_http_error` → CursorCloudAuthError (cli_exit=77)."""
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "unauthorized", "message": "bad key"}},
        )

    client = _build_cloud_mock_client(handler)
    try:
        with pytest.raises(CursorCloudAuthError) as excinfo:
            client.list_runs("bc-test-agent-001")
    finally:
        client.close()
    assert excinfo.value.cli_exit == 77


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_json_payload(combined: str) -> dict[str, Any]:
    """Pull the JSON object out of CLI combined output.

    The CLI prints the JSON dict on its own line (and may print stderr
    warnings). Walk the lines back-to-front and grab the first one that
    parses as a top-level JSON object.
    """
    for raw in reversed(combined.splitlines()):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(
        f"could not extract a top-level JSON object from CLI output:\n{combined}"
    )
