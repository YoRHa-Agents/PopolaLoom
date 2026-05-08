"""``popola status --verbose`` cost surface tests (v0.8.8 T2.1.2 / Q-C-2).

Per ``.local/.agent/active/v0.8.8-multi-run/PLAN.md`` §4.1 T2.1.2 +
``.local/research/v0.8.8_multi_run/cost-fields.md`` §3 surface, this
test module pins:

* (a) ``--verbose`` rendering produces the §3.1 one-liner with literal
  ``cost: n/a``, the ``model:`` segment, the optional ``mode:``
  segment, ``wall: NN.Ns`` and ``link: <agent.url>``.
* (b) Default rendering (no ``--verbose``) does NOT include the cost
  block at all (key absent in JSON, line missing in text).
* (c) ``--json --verbose`` produces a ``verbose`` block matching the
  §3.2 schema verbatim (10 keys); ``--json`` without ``--verbose``
  omits the key entirely.
* (d) ``model: -`` is rendered when the daemon's hard-coded default
  was substituted (mocked via ``model_id: null`` in the daemon's
  response per the §3.1 spec mapping).
* (e) End-to-end against an in-process popolad instance — emitting
  ``cloud.model_default_used`` on dispatch flips the verbose surface
  to ``model: -``.

The default-lane mock pattern follows ``tests/cli/test_list_runtime_column.py``:
:class:`CliRunner` invokes the Typer app, ``make_sync_client`` is
monkeypatched to return a ``MagicMock`` shaped like an
:class:`httpx.Client`, no real popolad daemon or socket touched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main
from popolaloom.daemon.rpc import _build_verbose_block
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskHandle, TaskState


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


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the module-level Rich Console to 200x50 so substring asserts hold."""
    monkeypatch.setattr(cli_main, "_console_out", Console(width=200, height=50))


def _combined(result: object) -> str:
    """Best-effort ``stdout + stderr`` extraction."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        parts.append(value)
    return "".join(parts)


def _make_response(*, status_code: int, body: Any) -> MagicMock:
    """Build a MagicMock shaped like an :class:`httpx.Response`."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


def _make_sync_client(*, on_get: Any, capture: dict[str, Any] | None = None) -> MagicMock:
    """Build a context-manager-shaped sync httpx client double for GET /status."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def _get(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> MagicMock:
        if capture is not None:
            capture["url"] = url
            capture["params"] = params
        return on_get

    client.get.side_effect = _get
    return client


# ── helpers for daemon-bound tests ──────────────────────────────────────────


def _make_handle(
    *,
    task_id: str,
    cli: str,
    started_at: datetime,
    completed_at: datetime | None = None,
    cursor_agent_id: str | None = None,
    cursor_run_id: str | None = None,
    cloud_phase: str | None = None,
    cmd: list[str] | None = None,
    state: TaskState = TaskState.RUNNING,
    runtime: str = "local",
    event_log_path: Path | None = None,
) -> TaskHandle:
    return TaskHandle(
        task_id=task_id,
        cli=cli,
        pid=None,
        state=state,
        started_at=started_at,
        event_log_path=event_log_path or Path("/tmp/x"),
        runtime=runtime,
        cursor_agent_id=cursor_agent_id,
        cursor_run_id=cursor_run_id,
        cloud_phase=cloud_phase,
        completed_at=completed_at,
        cmd=cmd or [],
    )


def _build_cmd_marker(extra: dict[str, Any]) -> list[str]:
    """Mirror ``CursorCloudAdapter.build_command`` minus normalize."""
    payload = {"cwd": None, "extra": extra, "prompt": "demo"}
    return ["__cloud__", "cursor-cloud", json.dumps(payload, sort_keys=True)]


# ── (b) default rendering: NO verbose block in text or JSON ─────────────────


def test_status_default_omits_cost_block_in_table(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola status <id>`` without ``--verbose`` does NOT print ``cost:``."""
    body = {
        "task_id": "task-default-1",
        "cli": "cursor-cloud",
        "state": "running",
        "pid": None,
        "exit_code": None,
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "completed_at": None,
        "latest_event_index": 0,
        "arktower_task_id": None,
        "persisted": False,
        "runtime": "cloud",
        "cursor_agent_id": "bc-abc",
        "cursor_run_id": "run-1",
        "cloud_phase": "RUNNING",
    }
    response = _make_response(status_code=200, body=body)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response, capture=captured),
    )
    result = runner.invoke(cli_main.app, ["status", "task-default-1"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "cost:" not in out, f"cost block leaked into default render:\n{out}"
    assert "model:" not in out
    assert "wall:" not in out
    assert "link:" not in out
    assert captured.get("params") == {}, (
        f"default status must not request verbose; got params={captured.get('params')!r}"
    )


def test_status_default_json_omits_verbose_key(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola status <id> --json`` (no ``--verbose``) MUST omit ``verbose``.

    AC (b): ``--json`` without ``--verbose`` MUST omit the entire ``verbose``
    block (key absent, NOT null) so accidental ``jq .verbose.cost_estimate_usd``
    fails loudly rather than silently nulling.
    """
    body = {
        "task_id": "task-default-2",
        "cli": "cursor",
        "state": "completed",
        "started_at": "2026-05-08T10:00:00.000+00:00",
    }
    response = _make_response(status_code=200, body=body)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", "task-default-2", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert "verbose" not in payload, (
        f"--json without --verbose must omit 'verbose' key; got {payload!r}"
    )


# ── (a) verbose rendering: cost line present with locked literals ───────────


def test_status_verbose_renders_cost_block_in_table(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola status <id> --verbose`` appends the §3.1 one-liner."""
    body = {
        "task_id": "task-verbose-1",
        "cli": "cursor-cloud",
        "state": "completed",
        "pid": None,
        "exit_code": 0,
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "completed_at": "2026-05-08T10:00:41.200+00:00",
        "latest_event_index": 12,
        "arktower_task_id": None,
        "persisted": True,
        "runtime": "cloud",
        "cursor_agent_id": "bc-xyz",
        "cursor_run_id": "run-1",
        "cloud_phase": "FINISHED",
        "verbose": {
            "cost_estimate_usd": None,
            "model_id": "composer-2",
            "model_mode": "std",
            "tokens_input": None,
            "tokens_output": None,
            "tokens_total": None,
            "wall_clock_s": 41.2,
            "agent_status": "FINISHED",
            "agent_url": "https://cursor.com/agents?id=bc-xyz",
            "doc_anchor": (
                "https://cursor.com/docs/cloud-agent/api/endpoints.md#get-a-run"
            ),
        },
    }
    response = _make_response(status_code=200, body=body)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response, capture=captured),
    )
    result = runner.invoke(cli_main.app, ["status", "task-verbose-1", "--verbose"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "cost: n/a" in out
    assert "model: composer-2" in out
    assert "wall: 41.2s" in out
    assert "link: https://cursor.com/agents?id=bc-xyz" in out
    # ``mode: std`` is suppressed per §3.1 ("don't render mode: std")
    assert "mode: std" not in out
    assert captured.get("params") == {"verbose": "true"}


def test_status_verbose_renders_dash_when_model_default_used(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model: -`` when the daemon reports ``model_id=null`` (default substituted)."""
    body = {
        "task_id": "task-default-model",
        "cli": "cursor-cloud",
        "state": "running",
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "verbose": {
            "cost_estimate_usd": None,
            "model_id": None,
            "model_mode": "std",
            "tokens_input": None,
            "tokens_output": None,
            "tokens_total": None,
            "wall_clock_s": 12.5,
            "agent_status": "RUNNING",
            "agent_url": "https://cursor.com/agents?id=bc-default",
            "doc_anchor": (
                "https://cursor.com/docs/cloud-agent/api/endpoints.md#get-a-run"
            ),
        },
    }
    response = _make_response(status_code=200, body=body)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", "task-default-model", "--verbose"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "model: -" in out, f"expected 'model: -' for default-substituted; got:\n{out}"


def test_status_verbose_renders_max_mode_segment(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode: max`` segment is rendered when ``model_mode != 'std'`` (§3.1)."""
    body = {
        "task_id": "task-max",
        "cli": "cursor-cloud",
        "state": "completed",
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "verbose": {
            "cost_estimate_usd": None,
            "model_id": "claude-4-sonnet-thinking",
            "model_mode": "max",
            "tokens_input": None,
            "tokens_output": None,
            "tokens_total": None,
            "wall_clock_s": 312.7,
            "agent_status": "FINISHED",
            "agent_url": "https://cursor.com/agents?id=bc-max",
            "doc_anchor": "https://cursor.com/docs/cloud-agent/api/endpoints.md#get-a-run",
        },
    }
    response = _make_response(status_code=200, body=body)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", "task-max", "--verbose"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "mode: max" in out
    assert "wall: 312.7s" in out
    assert "model: claude-4-sonnet-thinking" in out


# ── (c) JSON schema validation ──────────────────────────────────────────────


_COST_DOC_ANCHOR_LITERAL = (
    "https://cursor.com/docs/cloud-agent/api/endpoints.md#get-a-run"
)

_REQUIRED_VERBOSE_KEYS: frozenset[str] = frozenset(
    {
        "cost_estimate_usd",
        "model_id",
        "model_mode",
        "tokens_input",
        "tokens_output",
        "tokens_total",
        "wall_clock_s",
        "agent_status",
        "agent_url",
        "doc_anchor",
    }
)


def test_status_json_verbose_contains_full_schema(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json --verbose`` payload contains all 10 §3.2 keys verbatim."""
    body = {
        "task_id": "task-json-verbose",
        "cli": "cursor-cloud",
        "state": "completed",
        "started_at": "2026-05-08T10:00:00.000+00:00",
        "verbose": {
            "cost_estimate_usd": None,
            "model_id": "composer-2",
            "model_mode": "std",
            "tokens_input": None,
            "tokens_output": None,
            "tokens_total": None,
            "wall_clock_s": 41.2,
            "agent_status": "FINISHED",
            "agent_url": "https://cursor.com/agents?id=bc-xyz",
            "doc_anchor": _COST_DOC_ANCHOR_LITERAL,
        },
    }
    response = _make_response(status_code=200, body=body)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(
        cli_main.app, ["status", "task-json-verbose", "--json", "--verbose"]
    )
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert "verbose" in payload, "missing 'verbose' top-level key"
    verbose = payload["verbose"]
    missing = _REQUIRED_VERBOSE_KEYS - set(verbose.keys())
    assert not missing, f"verbose missing schema keys: {sorted(missing)}"
    assert verbose["cost_estimate_usd"] is None, (
        "Q-C-2: cost_estimate_usd MUST be null in v0.8.8 (no fabricated numbers)"
    )
    assert verbose["doc_anchor"] == _COST_DOC_ANCHOR_LITERAL


# ── (e) end-to-end: cloud.model_default_used ➜ model: - ─────────────────────


def test_build_verbose_block_renders_recorded_model(tmp_path: Path) -> None:
    """When user passes ``model=foo``, verbose block surfaces ``model_id='foo'``."""
    popolad = Popolad(events_dir=tmp_path / "events")
    started = datetime(2026, 5, 8, 10, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 5, 8, 10, 0, 41, tzinfo=UTC)
    cmd = _build_cmd_marker(
        {
            "model": "claude-4-sonnet",
            "repo_url": "https://github.com/x/y",
            "starting_ref": "main",
        }
    )
    handle = _make_handle(
        task_id="t-recorded",
        cli="cursor-cloud",
        started_at=started,
        completed_at=finished,
        cursor_agent_id="bc-rec",
        cursor_run_id="run-1",
        cloud_phase="FINISHED",
        runtime="cloud",
        cmd=cmd,
        state=TaskState.COMPLETED,
        event_log_path=tmp_path / "events" / "t-recorded.jsonl",
    )
    popolad.state_store.register(handle)
    base = popolad.get_status("t-recorded")
    block = _build_verbose_block(handle, base, popolad)
    assert block["model_id"] == "claude-4-sonnet"
    assert block["wall_clock_s"] == 41.0
    assert block["agent_url"] == "https://cursor.com/agents?id=bc-rec"
    assert block["agent_status"] == "FINISHED"
    assert block["cost_estimate_usd"] is None
    assert block["tokens_input"] is None


def test_build_verbose_block_renders_dash_when_default_event_present(
    tmp_path: Path,
) -> None:
    """``cloud.model_default_used`` event ➜ ``model_id=None`` (renders ``-``)."""
    popolad = Popolad(events_dir=tmp_path / "events")
    started = datetime(2026, 5, 8, 10, 0, 0, tzinfo=UTC)
    cmd = _build_cmd_marker(
        {
            "model": "composer-2",
            "repo_url": "https://github.com/x/y",
        }
    )
    event_path = tmp_path / "events" / "t-default.jsonl"
    handle = _make_handle(
        task_id="t-default",
        cli="cursor-cloud",
        started_at=started,
        runtime="cloud",
        cursor_agent_id="bc-def",
        cursor_run_id="run-1",
        cloud_phase="RUNNING",
        cmd=cmd,
        event_log_path=event_path,
    )
    popolad.state_store.register(handle)
    log = popolad._ensure_task_event_log("t-default", handle)  # type: ignore[attr-defined]
    log.append(
        "cloud.model_default_used",
        {"task_id": "t-default", "default_model": "composer-2"},
    )
    log.fsync()

    base = popolad.get_status("t-default")
    block = _build_verbose_block(handle, base, popolad)
    assert block["model_id"] is None, (
        "cloud.model_default_used must blank the verbose model_id"
    )
    assert block["model_mode"] == "std"
    assert block["agent_status"] == "RUNNING"


def test_build_verbose_block_omits_url_for_local_runtime(tmp_path: Path) -> None:
    """Local-runtime tasks have no Cursor dashboard link — ``agent_url`` is ``None``."""
    popolad = Popolad(events_dir=tmp_path / "events")
    started = datetime(2026, 5, 8, 10, 0, 0, tzinfo=UTC)
    handle = _make_handle(
        task_id="t-local",
        cli="cursor",
        started_at=started,
        runtime="local",
        event_log_path=tmp_path / "events" / "t-local.jsonl",
    )
    popolad.state_store.register(handle)
    base = popolad.get_status("t-local")
    block = _build_verbose_block(handle, base, popolad)
    assert block["agent_url"] is None
    assert block["model_id"] is None  # local runtime has no cmd marker
    assert block["cost_estimate_usd"] is None


def test_build_verbose_block_max_mode_segment(tmp_path: Path) -> None:
    """``model_params=[{id:max_mode,value:true}]`` ➜ ``model_mode='max'``."""
    popolad = Popolad(events_dir=tmp_path / "events")
    started = datetime(2026, 5, 8, 10, 0, 0, tzinfo=UTC)
    cmd = _build_cmd_marker(
        {
            "model": "claude-4-sonnet-thinking",
            "model_params": [{"id": "max_mode", "value": True}],
            "repo_url": "https://github.com/x/y",
        }
    )
    handle = _make_handle(
        task_id="t-max",
        cli="cursor-cloud",
        started_at=started,
        cursor_agent_id="bc-mx",
        cursor_run_id="run-1",
        cloud_phase="RUNNING",
        runtime="cloud",
        cmd=cmd,
        event_log_path=tmp_path / "events" / "t-max.jsonl",
    )
    popolad.state_store.register(handle)
    base = popolad.get_status("t-max")
    block = _build_verbose_block(handle, base, popolad)
    assert block["model_mode"] == "max"


def test_format_cost_line_handles_missing_block() -> None:
    """``_format_verbose_cost_line(None)`` → all-dash placeholder line.

    Defensive path: when the daemon is older than v0.8.8 (returns no
    ``verbose`` block even on ``--verbose``) we render dashes rather than
    crash; the user can grep stderr for daemon-version skew.
    """
    rendered = cli_main._format_verbose_cost_line(None)
    assert "cost: n/a" in rendered
    assert "model: -" in rendered
    assert "wall: -" in rendered
    assert "link: -" in rendered
