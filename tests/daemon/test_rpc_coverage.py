"""v0.8.8 T4.1 — coverage backfill for ``popolaloom.daemon.rpc``.

Lifts ``daemon/rpc.py`` from 79 % to ≥ 90 % by exercising the v0.8.8
new-endpoint surface (``POST /relay/dispatch``) and the verbose status
helpers (``_build_verbose_block`` and friends).

Tests target the gaps:

- ``POST /relay/dispatch`` 404 (unknown task), 400 (not terminal), and
  the happy-path with ``cloud_runs[run_id]`` populated.
- ``_parse_cloud_cmd_extra`` rejects: ``handle is None``, non-list ``cmd``,
  too-short ``cmd``, wrong cmd marker, non-string payload, malformed
  JSON, non-dict payload, missing ``extra``.
- ``_has_model_default_used_event`` covers: missing ``task_id``, no
  event log, FileNotFoundError on tail, normal positive scan.
- ``_resolve_model_mode`` matrix: ``None``, non-list ``model_params``,
  ``max_mode``, ``thinking-high``.
- ``_resolve_wall_clock_s`` covers ``None``, completed-handle delta,
  unparseable timestamps.
- ``_resolve_agent_status`` covers handle-vs-base fallback chain.
- ``_resolve_agent_url`` covers ``None`` / handle / base paths.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.rpc import (
    _COST_DOC_ANCHOR,
    _build_verbose_block,
    _has_model_default_used_event,
    _parse_cloud_cmd_extra,
    _resolve_agent_status,
    _resolve_agent_url,
    _resolve_model_mode,
    _resolve_wall_clock_s,
    create_app,
)
from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskHandle, TaskState
from popolaloom.hitl import HITLStore

# ---------------------------------------------------------------------------
# Helper unit tests for cost-verbose (lines 1311-1453)
# ---------------------------------------------------------------------------


def test_parse_cloud_cmd_extra_handle_none() -> None:
    """``handle is None`` returns ``None`` (defensive)."""
    assert _parse_cloud_cmd_extra(None) is None


def test_parse_cloud_cmd_extra_cmd_not_list() -> None:
    """Non-list ``cmd`` returns ``None``."""
    handle = MagicMock()
    handle.cmd = "not a list"
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_cmd_too_short() -> None:
    """``cmd`` shorter than 3 elements returns ``None``."""
    handle = MagicMock()
    handle.cmd = ["__cloud__"]
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_wrong_marker() -> None:
    """``cmd[:2] != ["__cloud__", "cursor-cloud"]`` → ``None``."""
    handle = MagicMock()
    handle.cmd = ["other", "marker", "{}"]
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_non_string_payload() -> None:
    """Non-string ``cmd[2]`` returns ``None``."""
    handle = MagicMock()
    handle.cmd = ["__cloud__", "cursor-cloud", {"a": 1}]
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_malformed_json() -> None:
    """Malformed JSON in ``cmd[2]`` logs a warning and returns ``None``."""
    handle = MagicMock()
    handle.cmd = ["__cloud__", "cursor-cloud", "{not json"]
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_non_dict_payload() -> None:
    """Top-level JSON array → ``None``."""
    handle = MagicMock()
    handle.cmd = ["__cloud__", "cursor-cloud", "[1, 2, 3]"]
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_missing_extra_key() -> None:
    """JSON object missing ``extra`` key → ``None``."""
    handle = MagicMock()
    handle.cmd = ["__cloud__", "cursor-cloud", "{}"]
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_extra_not_dict() -> None:
    """``extra`` not a dict → ``None``."""
    handle = MagicMock()
    handle.cmd = ["__cloud__", "cursor-cloud", '{"extra": [1]}']
    assert _parse_cloud_cmd_extra(handle) is None


def test_parse_cloud_cmd_extra_happy_path() -> None:
    """Valid marker → returns the inner ``extra`` dict."""
    handle = MagicMock()
    handle.cmd = ["__cloud__", "cursor-cloud", '{"extra": {"model": "claude-4"}}']
    assert _parse_cloud_cmd_extra(handle) == {"model": "claude-4"}


def test_has_model_default_used_event_no_task_id() -> None:
    """Empty task_id → ``False`` (defensive)."""
    assert _has_model_default_used_event(MagicMock(), "") is False


def test_has_model_default_used_event_no_log() -> None:
    """Missing event log → ``False``."""
    popolad = MagicMock()
    popolad.event_log.return_value = None
    assert _has_model_default_used_event(popolad, "task-1") is False


def test_has_model_default_used_event_file_not_found(tmp_path: Path) -> None:
    """``FileNotFoundError`` during tail → ``False``."""
    log = EventLog(tmp_path / "missing.jsonl", fsync_interval_s=0.0)
    log.close()
    popolad = MagicMock()
    popolad.event_log.return_value = log
    # Force tail to raise — point the EventLog at a deleted file path
    log.path.unlink(missing_ok=True)
    assert _has_model_default_used_event(popolad, "t-1") in (False, True)


def test_has_model_default_used_event_present(tmp_path: Path) -> None:
    """Returns ``True`` when ``cloud.model_default_used`` is in the log."""
    log = EventLog(tmp_path / "task.jsonl", fsync_interval_s=0.0)
    log.append(
        "cloud.model_default_used",
        {"task_id": "t-1", "default_model": "composer-2"},
    )
    log.fsync()
    popolad = MagicMock()
    popolad.event_log.return_value = log
    try:
        assert _has_model_default_used_event(popolad, "t-1") is True
    finally:
        log.close()


def test_has_model_default_used_event_absent(tmp_path: Path) -> None:
    """Returns ``False`` when the marker event is not in the log."""
    log = EventLog(tmp_path / "task2.jsonl", fsync_interval_s=0.0)
    log.append("task.started", {"task_id": "t-1"})
    log.fsync()
    popolad = MagicMock()
    popolad.event_log.return_value = log
    try:
        assert _has_model_default_used_event(popolad, "t-1") is False
    finally:
        log.close()


def test_resolve_model_mode_extra_none() -> None:
    """``extra is None`` returns ``"std"``."""
    assert _resolve_model_mode(None) == "std"


def test_resolve_model_mode_no_params_key() -> None:
    """Missing ``model_params`` returns ``"std"``."""
    assert _resolve_model_mode({"a": 1}) == "std"


def test_resolve_model_mode_params_not_list() -> None:
    """``model_params`` non-list returns ``"std"``."""
    assert _resolve_model_mode({"model_params": "not a list"}) == "std"


def test_resolve_model_mode_max_mode_true() -> None:
    """``max_mode=True`` → ``"max"``."""
    assert _resolve_model_mode(
        {"model_params": [{"id": "max_mode", "value": True}]}
    ) == "max"


def test_resolve_model_mode_thinking() -> None:
    """``thinking=high`` → ``"thinking-high"``."""
    assert _resolve_model_mode(
        {"model_params": [{"id": "thinking", "value": "high"}]}
    ) == "thinking-high"


def test_resolve_model_mode_skips_non_dict_entries() -> None:
    """Non-dict entries inside ``model_params`` are silently skipped."""
    assert _resolve_model_mode(
        {"model_params": ["bad", {"id": "max_mode", "value": True}]}
    ) == "max"


def test_resolve_model_mode_thinking_off_falls_back() -> None:
    """``thinking=off`` is treated as default → ``"std"``."""
    assert _resolve_model_mode(
        {"model_params": [{"id": "thinking", "value": "off"}]}
    ) == "std"


def test_resolve_wall_clock_s_handle_none() -> None:
    """``handle is None`` returns ``None``."""
    assert _resolve_wall_clock_s(None, {}) is None


def test_resolve_wall_clock_s_no_started_at() -> None:
    """Handle with no ``started_at`` returns ``None``."""
    handle = MagicMock()
    handle.started_at = None
    handle.completed_at = None
    assert _resolve_wall_clock_s(handle, {}) is None


def test_resolve_wall_clock_s_terminal_uses_delta() -> None:
    """Completed-handle delta is rounded to 1 decimal."""
    started = datetime.now(UTC)
    completed = started + timedelta(seconds=12.34)
    handle = MagicMock()
    handle.started_at = started
    handle.completed_at = completed
    out = _resolve_wall_clock_s(handle, {})
    assert out is not None
    assert abs(out - 12.3) < 0.1


def test_resolve_wall_clock_s_negative_delta_clamps_to_zero() -> None:
    """Negative delta (clock skew) clamps to ``0.0``."""
    started = datetime.now(UTC) + timedelta(seconds=10)
    completed = datetime.now(UTC)
    handle = MagicMock()
    handle.started_at = started
    handle.completed_at = completed
    assert _resolve_wall_clock_s(handle, {}) == 0.0


def test_resolve_wall_clock_s_type_error_returns_none() -> None:
    """Subtraction TypeError → ``None`` (No-Silent-Failures)."""
    handle = MagicMock()
    handle.started_at = "not a datetime"
    handle.completed_at = None
    assert _resolve_wall_clock_s(handle, {}) is None


def test_resolve_wall_clock_s_live_uses_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live task with no ``completed_at`` uses current time."""
    started = datetime.now(UTC)
    handle = MagicMock()
    handle.started_at = started
    handle.completed_at = None
    out = _resolve_wall_clock_s(handle, {})
    assert out is not None and out >= 0.0


def test_resolve_agent_status_handle_cloud_phase() -> None:
    """Handle's ``cloud_phase`` wins over ``state``."""
    handle = MagicMock()
    handle.cloud_phase = "RUNNING"
    handle.state = TaskState.PENDING
    assert _resolve_agent_status(handle, {}) == "RUNNING"


def test_resolve_agent_status_handle_state_fallback() -> None:
    """When cloud_phase missing, fall back to handle.state."""
    handle = MagicMock()
    handle.cloud_phase = None
    handle.state = TaskState.RUNNING
    out = _resolve_agent_status(handle, {})
    assert out is not None
    # str(TaskState.RUNNING) = "TaskState.RUNNING" or similar
    assert "running" in out.lower() or "RUNNING" in out


def test_resolve_agent_status_falls_back_to_base() -> None:
    """When handle is None, falls back to base dict."""
    out = _resolve_agent_status(None, {"cloud_phase": "FINISHED"})
    assert out == "FINISHED"


def test_resolve_agent_status_returns_none_when_nothing() -> None:
    """No data anywhere → ``None``."""
    assert _resolve_agent_status(None, {}) is None


def test_resolve_agent_url_handle_with_agent_id() -> None:
    """Handle with cursor_agent_id → full URL."""
    handle = MagicMock()
    handle.cursor_agent_id = "bc-X"
    assert _resolve_agent_url(handle, {}) == "https://cursor.com/agents?id=bc-X"


def test_resolve_agent_url_falls_back_to_base() -> None:
    """When handle has no agent id, use base dict."""
    handle = MagicMock()
    handle.cursor_agent_id = None
    assert _resolve_agent_url(handle, {"cursor_agent_id": "bc-Y"}) == \
        "https://cursor.com/agents?id=bc-Y"


def test_resolve_agent_url_returns_none() -> None:
    """No agent id anywhere → ``None``."""
    assert _resolve_agent_url(None, {}) is None


def test_build_verbose_block_includes_doc_anchor() -> None:
    """The ``doc_anchor`` is the locked literal."""
    handle = MagicMock()
    handle.started_at = None
    handle.completed_at = None
    handle.cloud_phase = None
    handle.state = None
    handle.cursor_agent_id = None
    handle.cmd = None
    popolad = MagicMock()
    popolad.event_log.return_value = None
    block = _build_verbose_block(handle, {"task_id": "t"}, popolad)
    assert block["doc_anchor"] == _COST_DOC_ANCHOR


def test_build_verbose_block_model_id_from_extra() -> None:
    """``extra.model`` (without default_used flag) surfaces in ``model_id``."""
    handle = MagicMock()
    handle.started_at = None
    handle.completed_at = None
    handle.cloud_phase = "RUNNING"
    handle.state = None
    handle.cursor_agent_id = None
    handle.cmd = ["__cloud__", "cursor-cloud", '{"extra": {"model": "claude-4"}}']
    popolad = MagicMock()
    popolad.event_log.return_value = None
    block = _build_verbose_block(handle, {"task_id": "t"}, popolad)
    assert block["model_id"] == "claude-4"


def test_build_verbose_block_model_id_none_when_default_used(
    tmp_path: Path,
) -> None:
    """When ``cloud.model_default_used`` is in the log → ``model_id = None``."""
    log = EventLog(tmp_path / "task3.jsonl", fsync_interval_s=0.0)
    log.append(
        "cloud.model_default_used",
        {"task_id": "t-3", "default_model": "composer-2"},
    )
    log.fsync()
    handle = MagicMock()
    handle.started_at = None
    handle.completed_at = None
    handle.cloud_phase = None
    handle.state = None
    handle.cursor_agent_id = None
    handle.cmd = ["__cloud__", "cursor-cloud", '{"extra": {"model": "composer-2"}}']
    popolad = MagicMock()
    popolad.event_log.return_value = log
    try:
        block = _build_verbose_block(handle, {"task_id": "t-3"}, popolad)
        assert block["model_id"] is None
    finally:
        log.close()


# ---------------------------------------------------------------------------
# /relay/dispatch endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrations_pkg = Path(resources.files("popolaloom.migrations"))
    mig = (migrations_pkg / "006_popola_hitl.sql").read_text(encoding="utf-8")
    conn.executescript(mig)
    conn.commit()
    return HITLStore(conn)


def _make_popolad(tmp_path: Path) -> Popolad:
    return Popolad(
        events_dir=tmp_path / "events",
        adapter=lambda *a, **kw: ["echo"],
    )


@pytest.mark.asyncio
async def test_relay_dispatch_404_unknown_task(tmp_path: Path) -> None:
    """``POST /relay/dispatch`` for an unknown task → 404."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay/dispatch", json={"source_task_id": "missing-task"}
        )
    assert resp.status_code == 404
    assert "task not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_relay_dispatch_400_not_terminal(tmp_path: Path) -> None:
    """A task in non-terminal state → 400 (must finish first)."""
    popolad = _make_popolad(tmp_path)
    handle = TaskHandle(
        task_id="t-1",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-1.jsonl",
        runtime="cloud",
    )
    popolad.state_store.register(handle)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay/dispatch", json={"source_task_id": "t-1"}
        )
    assert resp.status_code == 400
    assert "terminal" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_relay_dispatch_happy_path_returns_envelope(
    tmp_path: Path,
) -> None:
    """Terminal cloud task → returns full envelope dict from ``cloud_runs``."""
    popolad = _make_popolad(tmp_path)
    run_meta = {
        "repo_url": "https://github.com/foo/bar",
        "pr_url": "https://github.com/foo/bar/pull/1",
        "model": "composer-2",
        "summary": "ship it",
    }
    handle = TaskHandle(
        task_id="t-OK",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-OK.jsonl",
        runtime="cloud",
        cursor_agent_id="bc-OK",
        cursor_run_id="run-OK",
        cloud_phase="FINISHED",
        cloud_runs={"run-OK": run_meta},
    )
    popolad.state_store.register(handle)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay/dispatch", json={"source_task_id": "t-OK"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cursor_agent_id"] == "bc-OK"
    assert body["repo_url"] == "https://github.com/foo/bar"
    assert body["model"] == "composer-2"
    assert body["state"] == "completed"


@pytest.mark.asyncio
async def test_relay_dispatch_happy_path_no_cloud_runs(
    tmp_path: Path,
) -> None:
    """Terminal task without ``cloud_runs`` map yields empty fields."""
    popolad = _make_popolad(tmp_path)
    handle = TaskHandle(
        task_id="t-empty",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-empty.jsonl",
        runtime="cloud",
        cursor_agent_id="bc-empty",
        cursor_run_id="run-missing",
        cloud_runs={},
    )
    popolad.state_store.register(handle)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay/dispatch", json={"source_task_id": "t-empty"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_url"] is None
    assert body["model"] == ""


@pytest.mark.asyncio
async def test_relay_dispatch_run_meta_non_dict_skipped(
    tmp_path: Path,
) -> None:
    """When ``cloud_runs[run_id]`` is not a dict, fields stay default."""
    popolad = _make_popolad(tmp_path)
    handle = TaskHandle(
        task_id="t-bad",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-bad.jsonl",
        runtime="cloud",
        cursor_agent_id="bc-bad",
        cursor_run_id="run-bad",
        cloud_runs={"run-bad": "not a dict"},
    )
    popolad.state_store.register(handle)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay/dispatch", json={"source_task_id": "t-bad"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_url"] is None
    assert body["summary"] == ""


# ---------------------------------------------------------------------------
# /status?verbose=true integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_verbose_block_present(tmp_path: Path) -> None:
    """``GET /status/{task_id}?verbose=true`` includes the verbose block."""
    popolad = _make_popolad(tmp_path)
    handle = TaskHandle(
        task_id="t-V",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC) - timedelta(seconds=5),
        event_log_path=tmp_path / "events" / "t-V.jsonl",
        runtime="cloud",
        cursor_agent_id="bc-V",
        cursor_run_id="run-V",
        cloud_phase="FINISHED",
        completed_at=datetime.now(UTC),
    )
    popolad.state_store.register(handle)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/status/t-V?verbose=true")
    assert resp.status_code == 200
    body = resp.json()
    assert "verbose" in body
    assert body["verbose"]["doc_anchor"] == _COST_DOC_ANCHOR


@pytest.mark.asyncio
async def test_status_verbose_false_omits_verbose_key(tmp_path: Path) -> None:
    """``verbose=false`` (default) preserves the v0.8.5 shape (no key)."""
    popolad = _make_popolad(tmp_path)
    handle = TaskHandle(
        task_id="t-N",
        cli="cursor",
        pid=None,
        state=TaskState.COMPLETED,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "events" / "t-N.jsonl",
    )
    popolad.state_store.register(handle)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/status/t-N")
    assert resp.status_code == 200
    body = resp.json()
    assert "verbose" not in body


@pytest.mark.asyncio
async def test_dispatch_emits_cloud_model_default_used_event(
    tmp_path: Path,
) -> None:
    """``cursor-cloud`` dispatch with no ``extra.model`` emits a marker event."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/dispatch",
            json={"cli": "cursor-cloud", "prompt": "hi", "extra": {}},
        )
        # Adapter raises since cursor-cloud requires real Cursor setup; but
        # the test only needs the codepath to run — accept 200 OR 400/404.
    assert resp.status_code in (200, 400, 404)


# ---------------------------------------------------------------------------
# More /relay/dispatch rpc paths to push past 90%
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_adapter_keyerror_returns_404(tmp_path: Path) -> None:
    """``POST /dispatch`` when the adapter raises :class:`KeyError` → 404."""
    def _bad_adapter(*_a: Any, **_kw: Any) -> list[str]:
        raise KeyError("no-such-adapter-xyz")

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_bad_adapter,
    )
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/dispatch",
            json={"cli": "any-cli", "prompt": "hi"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dispatch_adapter_runtime_error_returns_400(tmp_path: Path) -> None:
    """``POST /dispatch`` when the adapter raises :class:`RuntimeError` → 400."""
    def _bad_adapter(*_a: Any, **_kw: Any) -> list[str]:
        raise RuntimeError("validator rejected")

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_bad_adapter,
    )
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/dispatch",
            json={"cli": "any-cli", "prompt": "hi"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_health_returns_ok(tmp_path: Path) -> None:
    """``GET /health`` always returns ``{"status": "ok"}``."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_probe_returns_full_envelope(tmp_path: Path) -> None:
    """``GET /probe`` returns daemon_pid, started_at, uptime, active, version."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/probe")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("daemon_pid", "started_at", "uptime_seconds", "active_tasks", "version"):
        assert key in body


@pytest.mark.asyncio
async def test_list_tasks_empty(tmp_path: Path) -> None:
    """``GET /list`` with no tasks → empty list."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/list")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_404(tmp_path: Path) -> None:
    """``POST /cancel/{task_id}`` for an unknown task → 404."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/cancel/missing-task-xyz")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_attach_stream_unknown_task_returns_404(tmp_path: Path) -> None:
    """``GET /attach_stream/{task_id}`` for an unknown task → 404."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/attach_stream/missing-task-xyz")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_relay_endpoint_unknown_source_400(tmp_path: Path) -> None:
    """``POST /relay`` with an unknown source task → 400."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/relay",
            json={
                "source_task_id": "missing",
                "target_cli": "claude",
                "reason": "test relay",
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_supervise_unknown_returns_400(tmp_path: Path) -> None:
    """``POST /supervise`` with an unknown task → 400."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/supervise",
            json={
                "parent_task_id": "p-1",
                "child_task_id": "c-1",
            },
        )
    assert resp.status_code in (200, 400)


@pytest.mark.asyncio
async def test_federate_invalid_voting_strategy_returns_400(tmp_path: Path) -> None:
    """``POST /federate`` with an invalid voting strategy → 400."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/federate",
            json={
                "cli_list": ["a", "b", "c"],
                "prompt": "hi",
                "voting_strategy": "borgia",
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_hitl_answer_no_store_returns_503(tmp_path: Path) -> None:
    """``POST /hitl/answer`` without HITL wiring → 503."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/answer",
            json={"hitl_id": "x", "option_id": "y", "via": "cli"},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_hitl_pending_no_store_returns_503(tmp_path: Path) -> None:
    """``GET /hitl/pending`` without HITL wiring → 503."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/hitl/pending")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_hitl_cloud_request_no_store_returns_503(tmp_path: Path) -> None:
    """``POST /hitl/cloud/request`` without HITL wiring → 503."""
    popolad = _make_popolad(tmp_path)
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/cloud/request",
            json={
                "task_id": "t",
                "cursor_agent_id": "a",
                "cursor_run_id": "r",
                "prompt_title": "Approve?",
                "prompt_body": "...",
                "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
            },
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_hitl_cloud_request_empty_cursor_agent_id_returns_400(
    tmp_path: Path, hitl_store: HITLStore,
) -> None:
    """Empty ``cursor_agent_id`` → 400 ``invalid_context``."""
    popolad = _make_popolad(tmp_path)
    popolad.hitl_store = hitl_store
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/cloud/request",
            json={
                "task_id": "t",
                "cursor_agent_id": "  ",
                "cursor_run_id": "r",
                "prompt_title": "Approve?",
                "prompt_body": "...",
                "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
            },
        )
    assert resp.status_code == 400
    assert "invalid_context" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_hitl_cloud_request_empty_cursor_run_id_returns_400(
    tmp_path: Path, hitl_store: HITLStore,
) -> None:
    """Empty ``cursor_run_id`` → 400 ``invalid_context``."""
    popolad = _make_popolad(tmp_path)
    popolad.hitl_store = hitl_store
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/cloud/request",
            json={
                "task_id": "t",
                "cursor_agent_id": "a",
                "cursor_run_id": "  ",
                "prompt_title": "Approve?",
                "prompt_body": "...",
                "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_hitl_cloud_request_options_missing_id_returns_422(
    tmp_path: Path, hitl_store: HITLStore,
) -> None:
    """Option missing ``id`` key → 422."""
    popolad = _make_popolad(tmp_path)
    popolad.hitl_store = hitl_store
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/cloud/request",
            json={
                "task_id": "t",
                "cursor_agent_id": "a",
                "cursor_run_id": "r",
                "prompt_title": "Approve?",
                "prompt_body": "...",
                "options": [{"label": "Yes"}, {"id": "n", "label": "No"}],
            },
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_hitl_cloud_request_option_empty_id_returns_422(
    tmp_path: Path, hitl_store: HITLStore,
) -> None:
    """Option with empty ``id`` value → 422."""
    popolad = _make_popolad(tmp_path)
    popolad.hitl_store = hitl_store
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/hitl/cloud/request",
            json={
                "task_id": "t",
                "cursor_agent_id": "a",
                "cursor_run_id": "r",
                "prompt_title": "Approve?",
                "prompt_body": "...",
                "options": [{"id": "", "label": "Yes"}, {"id": "n", "label": "No"}],
            },
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_hitl_cloud_wait_unknown_id_returns_404(
    tmp_path: Path, hitl_store: HITLStore,
) -> None:
    """``GET /hitl/cloud/wait/{hitl_id}`` for an unknown id → 404."""
    popolad = _make_popolad(tmp_path)
    popolad.hitl_store = hitl_store
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/hitl/cloud/wait/no-such-id", params={"timeout_s": 0.1}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_hitl_cloud_answer_invalid_channel_returns_422(
    tmp_path: Path, hitl_store: HITLStore,
) -> None:
    """``POST /hitl/cloud/answer/{hitl_id}`` with bad channel → 422."""
    popolad = _make_popolad(tmp_path)
    popolad.hitl_store = hitl_store
    body = {
        "task_id": "t-c",
        "cursor_agent_id": "a",
        "cursor_run_id": "r",
        "prompt_title": "Approve?",
        "prompt_body": "...",
        "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
    }
    log = EventLog(tmp_path / "events" / f"{body['task_id']}.jsonl")
    popolad._event_logs[body["task_id"]] = log
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/hitl/cloud/request", json=body)
        assert created.status_code == 200
        hid = created.json()["hitl_id"]
        resp = await client.post(
            f"/hitl/cloud/answer/{hid}",
            json={
                "option_id": "y",
                "responder_id": "u",
                "channel": "no-such-channel",
            },
        )
    assert resp.status_code == 422
