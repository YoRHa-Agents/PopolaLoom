"""``popola status`` / ``popola attach`` busy-line visibility (Q-C-7).

v0.8.8 T2.2.2 — pins the default-visible binding from
``.local/research/v0.8.8_multi_run/quota-config.md`` §5.2:

- (f) ``cloud.queued_quota_exceeded`` event default-visible: ``popola
  status`` surfaces ``WAITING: rate_limit retry N/M next=~Xs`` until
  ``cloud.queue_exit outcome="success"`` arrives; ``popola attach``
  prints inline (NOT debug-filtered).
- (g) ``cloud.busy_queued`` / ``cloud.busy_dispatched`` /
  ``cloud.busy_timeout`` similarly default-visible (per §5.2 symmetry).

Covers the three Q-C-7 acceptance criteria:

1. **Status surface** — the ``WAITING:`` line surfaces below the table
   when the latest unmatched ``cloud.queued_quota_exceeded`` /
   ``cloud.busy_queued`` event is recorded, and clears once
   ``cloud.queue_exit outcome="success"`` /
   ``cloud.busy_dispatched`` / ``cloud.busy_timeout`` arrives.

2. **Attach inline rendering** — :func:`_summarize_data` returns the
   user-facing strings (``WAITING: ...`` / ``DISPATCHED: ...`` /
   ``TIMEOUT: ...`` / ``QUEUE_EXIT ...``) for the four event types
   without any debug filter.

3. **JSON mode** — ``--json`` still emits the WAITING line on stderr
   so machine consumers (status pollers) see the same signal as humans.

Mirrors :file:`tests/cli/test_status_cost.py`'s mock pattern: ``CliRunner``
invokes the Typer app, ``make_sync_client`` is monkeypatched to a
context-manager-shaped :class:`MagicMock`, no real popolad daemon
or socket is touched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main
from popolaloom.cli.main import (
    _busy_line_from_events,
    _format_busy_queue_line,
    _format_quota_waiting_line,
    _summarize_data,
)
from popolaloom.daemon.event_log import EventLog


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Point ``$POPOLA_HOME`` at a tmp dir so ``_events_path_for_task``
    + ``_socket_path`` resolve into the test sandbox.
    """
    monkeypatch.setenv("POPOLA_HOME", str(tmp_path))
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    yield tmp_path


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Rich Console to 200x50 so substring asserts hold."""
    monkeypatch.setattr(cli_main, "_console_out", Console(width=200, height=50))


def _combined(result: object) -> str:
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        parts.append(value)
    return "".join(parts)


def _make_response(*, status_code: int, body: Any) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


def _make_sync_client(*, on_get: Any) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = on_get
    return client


def _events_log(home: Path, task_id: str) -> EventLog:
    """Open the per-task NDJSON event log under ``$POPOLA_HOME/events/``."""
    return EventLog(home / "events" / f"{task_id}.jsonl", fsync_interval_s=0.0)


def _basic_status_body(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
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


# ---------------------------------------------------------------------------
# (1) Status surface — WAITING: rate_limit when cloud.queued_quota_exceeded
# is the latest unmatched event (no cloud.queue_exit yet).
# ---------------------------------------------------------------------------


def test_status_surfaces_waiting_rate_limit_line(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending ``cloud.queued_quota_exceeded`` (no matching
    ``cloud.queue_exit``) surfaces ``WAITING: rate_limit retry N/M
    next=~Xs`` per spec §5.2."""
    task_id = "task-quota-1"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.queued_quota_exceeded",
            {
                "task_id": task_id,
                "status": 429,
                "retry_after_ms": 2500,
                "max_retries": 5,
                "ts": "2026-05-08T10:00:00.000Z",
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING: rate_limit" in out
    assert "retry 1/5" in out
    assert "next=~2.5s" in out


def test_status_clears_waiting_after_queue_exit_success(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cloud.queue_exit outcome="success"`` clears the rate-limit
    WAITING line so a subsequent successful run is not flagged."""
    task_id = "task-quota-2"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.queued_quota_exceeded",
            {
                "task_id": task_id,
                "status": 429,
                "retry_after_ms": 1500,
                "max_retries": 3,
                "ts": "2026-05-08T10:00:00.000Z",
            },
        )
        log.append(
            "cloud.queue_exit",
            {
                "task_id": task_id,
                "attempts": 2,
                "total_wait_ms": 3500,
                "outcome": "success",
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING: rate_limit" not in out
    assert "WAITING: agent_busy" not in out


# ---------------------------------------------------------------------------
# (1) Status surface — WAITING: agent_busy when cloud.busy_queued is the
# latest unmatched event.
# ---------------------------------------------------------------------------


def test_status_surfaces_waiting_agent_busy_line(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending ``cloud.busy_queued`` surfaces
    ``WAITING: agent_busy agent=<id> position=<n> deadline=<iso>`` per
    spec §5.2 + §4.4 CLI contract."""
    task_id = "task-busy-1"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.busy_queued",
            {
                "task_id": task_id,
                "agent_id": "bc-busy",
                "current_run_id": "run-prev",
                "queue_position": 2,
                "deadline_ts": "2026-05-08T10:30:00.000Z",
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING: agent_busy" in out
    assert "agent=bc-busy" in out
    assert "position=2" in out
    assert "deadline=2026-05-08T10:30:00.000Z" in out


def test_status_clears_waiting_after_busy_dispatched(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cloud.busy_dispatched`` clears the agent_busy WAITING line
    so the attach UI can dismiss its "queued" badge per spec §4.4."""
    task_id = "task-busy-2"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.busy_queued",
            {
                "task_id": task_id,
                "agent_id": "bc-busy",
                "current_run_id": "run-prev",
                "queue_position": 1,
                "deadline_ts": "2026-05-08T11:00:00.000Z",
            },
        )
        log.append(
            "cloud.busy_dispatched",
            {
                "task_id": task_id,
                "agent_id": "bc-busy",
                "prev_run_id": "run-prev",
                "new_run_id": "run-new",
                "waited_ms": 4200,
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING:" not in out


def test_status_clears_waiting_after_busy_timeout(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cloud.busy_timeout`` clears the agent_busy WAITING line — the
    eventual ``task.failed`` rendering takes over for the user-facing
    surface (CLI exit 75 surfaces from the dispatch path, not status)."""
    task_id = "task-busy-3"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.busy_queued",
            {
                "task_id": task_id,
                "agent_id": "bc-busy",
                "current_run_id": "run-prev",
                "queue_position": 1,
                "deadline_ts": "2026-05-08T11:00:00.000Z",
            },
        )
        log.append(
            "cloud.busy_timeout",
            {
                "task_id": task_id,
                "agent_id": "bc-busy",
                "waited_ms": 1_800_000,
                "current_run_id_at_timeout": "run-still-busy",
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING:" not in out


# ---------------------------------------------------------------------------
# (1) Status surface — no busy events ⇒ no WAITING line.
# ---------------------------------------------------------------------------


def test_status_no_waiting_line_for_clean_task(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task with no busy / quota events renders without any WAITING
    line — the surface is opt-in based on the daemon's per-task event
    stream."""
    task_id = "task-clean-1"
    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING:" not in out


# ---------------------------------------------------------------------------
# (1) Status surface — agent_busy beats rate_limit when both uncleared.
# ---------------------------------------------------------------------------


def test_status_busy_beats_quota_when_both_uncleared(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both ``cloud.queued_quota_exceeded`` AND ``cloud.busy_queued``
    pending → the longer wait window (busy queue) takes priority per
    spec §5.2."""
    task_id = "task-double-1"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.queued_quota_exceeded",
            {
                "task_id": task_id,
                "status": 429,
                "retry_after_ms": 1000,
                "max_retries": 5,
                "ts": "2026-05-08T10:00:00.000Z",
            },
        )
        log.append(
            "cloud.busy_queued",
            {
                "task_id": task_id,
                "agent_id": "bc-double",
                "current_run_id": "run-prev",
                "queue_position": 1,
                "deadline_ts": "2026-05-08T10:30:00.000Z",
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )
    result = runner.invoke(cli_main.app, ["status", task_id])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "WAITING: agent_busy" in out
    assert "WAITING: rate_limit" not in out


# ---------------------------------------------------------------------------
# (1) Status surface — JSON mode also emits the WAITING line on stderr.
# ---------------------------------------------------------------------------


def test_status_json_mode_surfaces_waiting_on_stderr(
    isolated_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` mode emits the JSON body to stdout AND the WAITING
    line to stderr — machine consumers (status pollers) see the same
    signal humans do without polluting the JSON stream (Q-C-7).

    The test pins the routing by capturing stdout + stderr separately via
    :func:`capsys`-style monkeypatched :func:`typer.echo` so both
    streams are independently inspectable (``CliRunner`` in newer
    Typer versions no longer exposes ``mix_stderr``).
    """
    task_id = "task-json-1"
    log = _events_log(isolated_home, task_id)
    try:
        log.append(
            "cloud.queued_quota_exceeded",
            {
                "task_id": task_id,
                "status": 429,
                "retry_after_ms": 500,
                "max_retries": 5,
                "ts": "2026-05-08T10:00:00.000Z",
            },
        )
    finally:
        log.close()

    response = _make_response(status_code=200, body=_basic_status_body(task_id))
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )

    # Wire a stream-aware ``typer.echo`` capture so we can verify the
    # WAITING line lands on stderr and the JSON body lands on stdout.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    real_echo = cli_main.typer.echo

    def _capture_echo(message: str = "", err: bool = False, **_kw: Any) -> None:
        target = stderr_lines if err else stdout_lines
        target.append(str(message))
        # Forward to the real echo so CliRunner still captures something
        # — keeps the result.output sane for diagnostic on assert failure.
        real_echo(message, err=err)

    monkeypatch.setattr(cli_main.typer, "echo", _capture_echo)

    result = runner.invoke(cli_main.app, ["status", task_id, "--json"])
    assert result.exit_code == 0, _combined(result)
    # JSON body lands on stdout, WAITING line on stderr (Q-C-7 default-visible
    # but disjoint streams so the JSON output is unaffected).
    stdout_text = "\n".join(stdout_lines)
    stderr_text = "\n".join(stderr_lines)
    json_lines = [
        line
        for line in stdout_text.splitlines()
        if line.strip().startswith("{")
    ]
    assert json_lines, f"no JSON body on stdout: {stdout_text!r}"
    payload = json.loads(json_lines[0])
    assert payload["task_id"] == task_id
    assert "WAITING: rate_limit" in stderr_text
    assert "WAITING:" not in stdout_text


# ---------------------------------------------------------------------------
# (2) Attach inline rendering — _summarize_data returns the literal
# WAITING:/DISPATCHED:/TIMEOUT:/QUEUE_EXIT prefixes (NOT debug-filtered).
# ---------------------------------------------------------------------------


def test_attach_inline_renders_cloud_busy_queued() -> None:
    """``cloud.busy_queued`` renders inline as the ``WAITING: agent_busy``
    line — same string the status surface produces, so attach output
    matches status output for the same event."""
    payload = {
        "task_id": "t-1",
        "agent_id": "bc-A",
        "current_run_id": "run-prev",
        "queue_position": 3,
        "deadline_ts": "2026-05-08T11:00:00.000Z",
    }
    summary = _summarize_data("cloud.busy_queued", payload)
    assert summary.startswith("WAITING: agent_busy")
    assert "agent=bc-A" in summary
    assert "position=3" in summary
    assert "deadline=2026-05-08T11:00:00.000Z" in summary


def test_attach_inline_renders_cloud_busy_dispatched() -> None:
    """``cloud.busy_dispatched`` renders as the literal ``DISPATCHED:``
    line so attach UIs can match on the prefix (Q-C-7 default-visible)."""
    payload = {
        "task_id": "t-1",
        "agent_id": "bc-A",
        "prev_run_id": "run-prev",
        "new_run_id": "run-new",
        "waited_ms": 4500,
    }
    summary = _summarize_data("cloud.busy_dispatched", payload)
    assert summary.startswith("DISPATCHED:")
    assert "agent=bc-A" in summary
    assert "prev_run=run-prev" in summary
    assert "new_run=run-new" in summary
    assert "waited=4.5s" in summary


def test_attach_inline_renders_cloud_busy_timeout() -> None:
    """``cloud.busy_timeout`` renders as ``TIMEOUT: agent_busy ...`` so
    operators see the wait expired (and exit 75 incoming)."""
    payload = {
        "task_id": "t-1",
        "agent_id": "bc-A",
        "current_run_id_at_timeout": "run-still-busy",
        "waited_ms": 1_800_000,
    }
    summary = _summarize_data("cloud.busy_timeout", payload)
    assert summary.startswith("TIMEOUT: agent_busy")
    assert "agent=bc-A" in summary
    assert "current_run=run-still-busy" in summary
    assert "waited=1800.0s" in summary


def test_attach_inline_renders_cloud_queued_quota_exceeded() -> None:
    """``cloud.queued_quota_exceeded`` renders as the literal
    ``WAITING: rate_limit retry N/M next=~Xs`` line so attach output
    matches what status surfaces (default-visible per Q-C-7)."""
    payload = {
        "task_id": "t-1",
        "status": 429,
        "retry_after_ms": 7500,
        "max_retries": 4,
        "ts": "2026-05-08T10:00:00.000Z",
    }
    summary = _summarize_data("cloud.queued_quota_exceeded", payload)
    assert summary == "WAITING: rate_limit retry 1/4 next=~7.5s"


def test_attach_inline_renders_cloud_queue_exit() -> None:
    """``cloud.queue_exit`` renders as the literal ``QUEUE_EXIT
    outcome=<o> attempts=<n> total_wait=<x>s`` line so attach UIs can
    bracket the WAITING/QUEUE_EXIT pair."""
    payload = {
        "task_id": "t-1",
        "attempts": 3,
        "total_wait_ms": 4500,
        "outcome": "success",
    }
    summary = _summarize_data("cloud.queue_exit", payload)
    assert summary.startswith("QUEUE_EXIT")
    assert "outcome=success" in summary
    assert "attempts=3" in summary
    assert "total_wait=4.5s" in summary


# ---------------------------------------------------------------------------
# (3) Format helper unit tests — pin the exact line shape per spec §5.2.
# ---------------------------------------------------------------------------


def test_format_quota_waiting_line_with_retry_after() -> None:
    """``_format_quota_waiting_line`` produces the spec example shape:
    ``WAITING: rate_limit retry 1/<max> next=~<retry_after>s``."""
    line = _format_quota_waiting_line(
        {"max_retries": 5, "retry_after_ms": 2500, "status": 429}
    )
    assert line == "WAITING: rate_limit retry 1/5 next=~2.5s"


def test_format_quota_waiting_line_missing_retry_after() -> None:
    """``retry_after_ms = None`` (header absent / unparseable) drops the
    ``next=~Xs`` segment but keeps the ``retry N/M`` part."""
    line = _format_quota_waiting_line(
        {"max_retries": 3, "retry_after_ms": None, "status": 429}
    )
    assert line == "WAITING: rate_limit retry 1/3"


def test_format_busy_queue_line_no_deadline_renders_never() -> None:
    """``deadline_ts = None`` is the wait-forever sentinel; the line
    surfaces ``deadline=never`` so the operator sees the difference
    between "no deadline" and "deadline missing from payload"."""
    line = _format_busy_queue_line(
        {
            "agent_id": "bc-X",
            "queue_position": 1,
            "deadline_ts": None,
        }
    )
    assert "WAITING: agent_busy" in line
    assert "agent=bc-X" in line
    assert "position=1" in line
    assert "deadline=never" in line


def test_busy_line_from_events_returns_none_for_empty() -> None:
    """An empty event stream → ``None`` (no WAITING line). Pins the
    "non-throttled tasks render as before" promise from spec §5.2."""
    assert _busy_line_from_events([]) is None


def test_busy_line_from_events_terminal_task_clears_waiting() -> None:
    """A ``task.completed`` / ``task.failed`` / ``task.canceled`` event
    clears any pending WAITING line — the task is over, no further
    WAITING is appropriate."""
    events: list[dict[str, Any]] = [
        {
            "type": "cloud.queued_quota_exceeded",
            "time": "2026-05-08T10:00:00.000Z",
            "data": {"max_retries": 5, "retry_after_ms": 1000},
        },
        {
            "type": "task.completed",
            "time": "2026-05-08T10:00:30.000Z",
            "data": {"task_id": "t-1", "exit_code": 0},
        },
    ]
    assert _busy_line_from_events(events) is None
