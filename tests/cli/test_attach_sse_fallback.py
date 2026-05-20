"""Default-lane tests for ``popola attach`` cloud SSE fallback (v0.8.6 T2.2.1).

Per [v0.8.6 PLAN.md §4.2 T2.2.1](../../.local/.agent/active/v0.8.6-cloud-sse/PLAN.md):
``popola attach --follow`` for ``runtime=cloud`` tasks layers a Cursor SSE
pump on top of the existing daemon ``/attach_stream`` consumer. The new
SSE thread is auto-fallback: on ``CursorCloudStreamExpiredError``,
``httpx.ReadError`` / ``httpx.ConnectError`` / ``httpx.TimeoutException``,
missing API key, or any unexpected exception, it surfaces a
``cloud.sse.fallback_to_poll`` boundary event + a ``[cloud sse] ...``
one-liner on stderr (No-Silent-Failures) and the renderer falls back to
the legacy poll-driven view (the daemon's ``/attach_stream`` stays the
canonical phase source per ``state-source-of-truth.md`` §4).

These tests cover every AC in ``PLAN.md §4.2 T2.2.1``:

* (a) cloud runtime → SSE thread spawned, events feed the renderer.
* (b) 410 / network error → fallback engaged; legacy poll path takes over;
  user sees fallback notice (event + stderr).
* (c) ``--no-stream`` → SSE thread NOT spawned even on cloud runtime.
* (d) reconciliation: SSE renderer NEVER calls ``state_store.update(...)``
  (static text inspection of ``cli/main.py``).
* (f) ``runtime=local`` (or unset) → SSE thread NOT spawned; existing
  attach behaviour preserved.
* (g) Ctrl-C / SIGINT → main thread exits 0; the daemon-thread SSE worker
  is daemonic and joins within the 2 s timeout in the ``finally`` block.

Plus boundary cases for missing ``cursor_agent_id`` / ``CURSOR_API_KEY``
(both surface a stderr notice and skip the SSE thread) and direct unit
tests of ``_run_cloud_sse_pump`` for ``httpx.ConnectError`` /
``CursorCloudStreamInvalidLastEventIdError`` / clean stream-end paths.

Mock pattern is shared with ``tests/cli/test_main_error_paths.py`` and
``tests/cli/test_attach_no_follow_eof.py``: ``CliRunner`` invokes the
Typer app and ``make_sync_client`` is monkeypatched to a context-manager-
shaped mock so no real popolad daemon or socket is touched. ``SSEReader``
and ``CloudCursorClient`` are monkeypatched at the ``cli_main`` namespace
so no real ``api.cursor.com`` request is issued.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from popolaloom.adapters.cursor_cloud import (
    CursorCloudError,
    CursorCloudStreamInvalidLastEventIdError,
)
from popolaloom.cli import main as cli_main

# ── shared fixtures ──────────────────────────────────────────────────────


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
def patch_cloud_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace :class:`CloudCursorClient` with a ``MagicMock`` factory.

    The cloud SSE worker constructs ``CloudCursorClient(api_key)`` once per
    attach session and calls ``client.close()`` in the ``finally`` block.
    A bare ``MagicMock`` satisfies both shapes and ensures no real
    ``httpx.Client`` (and therefore no real network connection) is built.
    """
    fake = MagicMock(name="CloudCursorClientFactory")
    monkeypatch.setattr(cli_main, "CloudCursorClient", fake)
    return fake


@pytest.fixture(autouse=True)
def cursor_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-empty CURSOR_API_KEY so cloud SSE is not skipped.

    Tests that explicitly need the "missing API key" path can override
    this with ``monkeypatch.delenv("CURSOR_API_KEY", raising=False)``.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "test-api-key-do-not-use")


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


def _make_response(*, status_code: int, body: Any) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


def _make_stream(
    iter_lines_value: Any,
    *,
    status_code: int = 200,
) -> MagicMock:
    """Build a context-manager-shaped streaming response double."""
    stream = MagicMock()
    stream.status_code = status_code
    stream.iter_lines.return_value = iter_lines_value
    stream.__enter__.return_value = stream
    stream.__exit__.return_value = False
    return stream


def _make_sync_client_factory(
    *,
    status_response: MagicMock,
    stream_response_obj: MagicMock,
) -> Any:
    """Mimic :func:`cli_main.make_sync_client` for status + stream calls."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = status_response
    client.stream = MagicMock(return_value=stream_response_obj)
    return lambda *_a, **_kw: client


def _cloud_status_body(
    *,
    runtime: str = "cloud",
    cursor_agent_id: str | None = "bc-agent-001",
    cursor_run_id: str | None = "run-xyz-001",
    state: str = "running",
) -> dict[str, Any]:
    """Build a ``GET /status/{task_id}`` body shaped like the daemon emits."""
    body: dict[str, Any] = {
        "task_id": "tid-cloud",
        "cli": "cursor-cloud",
        "state": state,
        "pid": None,
        "runtime": runtime,
        "started_at": "2026-05-08T10:00:00.000+00:00",
    }
    if cursor_agent_id is not None:
        body["cursor_agent_id"] = cursor_agent_id
    if cursor_run_id is not None:
        body["cursor_run_id"] = cursor_run_id
    return body


def _wire_attach_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_body: dict[str, Any],
    stream_lines: list[str] | None = None,
    stream_status: int = 200,
) -> None:
    """Install a ``make_sync_client`` mock that returns the given fixtures."""
    status_response = _make_response(status_code=200, body=status_body)
    stream = _make_stream(
        iter(stream_lines or []),
        status_code=stream_status,
    )
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _make_sync_client_factory(
            status_response=status_response,
            stream_response_obj=stream,
        ),
    )


# ── fake SSEReader for monkeypatching ───────────────────────────────────


class _RecordingFakeReader:
    """Test double mimicking ``SSEReader``'s observable API.

    Configured via :func:`_make_fake_reader_factory`; on ``pump()`` it
    plays back a prerecorded event sequence (``append`` calls on the
    sink) and either returns cleanly or raises a configured exception
    (so tests can drive the worker through every fallback branch).
    """

    instances: list[_RecordingFakeReader] = []

    def __init__(
        self,
        client: Any,
        event_log: Any,
        task_id: str,
        run_id: str,
        *,
        agent_id: str,
        events_to_emit: list[tuple[str, dict[str, Any]]],
        exception_to_raise: BaseException | None,
        sleep_until_stop: bool,
    ) -> None:
        self.client = client
        self.event_log = event_log
        self.task_id = task_id
        self.run_id = run_id
        self.agent_id = agent_id
        self.events_to_emit = events_to_emit
        self.exception_to_raise = exception_to_raise
        self.sleep_until_stop = sleep_until_stop
        self.pump_called = False
        self.stop_event_observed: threading.Event | None = None
        type(self).instances.append(self)

    def pump(self, stop_event: threading.Event | None = None) -> None:
        self.pump_called = True
        self.stop_event_observed = stop_event
        for event_type, data in self.events_to_emit:
            if stop_event is not None and stop_event.is_set():
                return
            self.event_log.append(event_type, data)
        if self.exception_to_raise is not None:
            raise self.exception_to_raise
        if self.sleep_until_stop and stop_event is not None:
            stop_event.wait(timeout=5.0)


def _make_fake_reader_factory(
    *,
    events_to_emit: list[tuple[str, dict[str, Any]]] | None = None,
    exception_to_raise: BaseException | None = None,
    sleep_until_stop: bool = False,
) -> Any:
    """Return a callable mimicking the ``SSEReader(...)`` constructor signature."""
    captured_events = list(events_to_emit or [])

    def _factory(
        client: Any,
        event_log: Any,
        task_id: str,
        run_id: str,
        *,
        agent_id: str,
    ) -> _RecordingFakeReader:
        return _RecordingFakeReader(
            client,
            event_log,
            task_id,
            run_id,
            agent_id=agent_id,
            events_to_emit=captured_events,
            exception_to_raise=exception_to_raise,
            sleep_until_stop=sleep_until_stop,
        )

    return _factory


@pytest.fixture(autouse=True)
def reset_fake_reader_instances() -> Iterator[None]:
    """Clear class-level recorded instances between tests."""
    _RecordingFakeReader.instances = []
    yield
    _RecordingFakeReader.instances = []


# ── 1. Happy path: runtime=cloud delivers SSE events to the renderer ───


def _delayed_terminal_lines(*, delay_s: float = 0.25) -> Iterator[str]:
    """Yield a single terminal frame after ``delay_s`` seconds.

    The delay gives the cloud SSE worker time to emit its events (and the
    ``cloud.sse.fallback_to_poll`` boundary marker on stream end) BEFORE
    the main thread observes the daemon's terminal event and signals
    ``stop_event``. Without this synchronisation, the main thread can
    finish its mocked ``/attach_stream`` loop in microseconds — long
    before the worker thread is even scheduled — producing a race
    condition that intermittently misses the cloud SSE assertions.
    The 0.25 s budget is empirically generous (the fake worker runs in
    well under 50 ms) and adds negligible test latency.
    """
    time.sleep(delay_s)
    yield "data: " + json.dumps(
        {
            "time": "t-end",
            "type": "task.completed",
            "data": {"exit_code": 0},
        }
    )


def test_cloud_runtime_engages_sse_and_renders_events(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (a): runtime=cloud + happy SSE → cloud.sse.* events appear in stdout.

    The fake reader emits two ``cloud.sse.*`` events; the daemon's
    ``/attach_stream`` mock then drains a single terminal frame after a
    short delay (see :func:`_delayed_terminal_lines`) so the worker
    thread has time to run end-to-end. Both feeds end up serialised
    through the same ``_format_event`` pipeline so substring asserts on
    stdout suffice.
    """
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(
            events_to_emit=[
                (
                    "cloud.sse.assistant",
                    {
                        "task_id": "tid-cloud",
                        "agent_id": "bc-agent-001",
                        "run_id": "run-xyz-001",
                        "stream_session_id": "sess-1",
                        "sse_id": "0",
                        "seq": 0,
                        "payload": {"text": "hello world"},
                    },
                ),
                (
                    "cloud.sse.tool_call",
                    {
                        "task_id": "tid-cloud",
                        "agent_id": "bc-agent-001",
                        "run_id": "run-xyz-001",
                        "stream_session_id": "sess-1",
                        "sse_id": "1",
                        "seq": 1,
                        "payload": {"tool": "shell", "args": "ls"},
                    },
                ),
            ],
        ),
    )
    status_response = _make_response(status_code=200, body=_cloud_status_body())
    stream = _make_stream(_delayed_terminal_lines())
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _make_sync_client_factory(
            status_response=status_response,
            stream_response_obj=stream,
        ),
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "cloud.sse.assistant" in out, (
        f"expected cloud SSE assistant event in output:\n{out}"
    )
    assert "cloud.sse.tool_call" in out, (
        f"expected cloud SSE tool_call event in output:\n{out}"
    )
    assert "task.completed" in out, (
        f"expected daemon /attach_stream terminal event in output:\n{out}"
    )
    assert len(_RecordingFakeReader.instances) == 1, (
        "expected exactly one SSEReader to be constructed"
    )


# ── 2. 410 stream_expired triggers fallback to poll ────────────────────


def test_cloud_runtime_410_emits_fallback_marker_and_stderr_notice(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (b)+(e): 410 stream_expired → cloud.sse.fallback_to_poll + stderr notice.

    The real ``SSEReader.pump`` catches :class:`CursorCloudStreamExpiredError`
    *internally*, emits a ``cloud.sse.stream_expired`` envelope, and
    returns cleanly. We script that behaviour here and assert that the
    CLI worker then emits a separate ``cloud.sse.fallback_to_poll``
    boundary marker per ``DECISIONS.md`` (canonical event name) and a
    one-liner ``[cloud sse] ...`` notice on stderr (No-Silent-Failures).
    """
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(
            events_to_emit=[
                (
                    "cloud.sse.stream_expired",
                    {
                        "task_id": "tid-cloud",
                        "agent_id": "bc-agent-001",
                        "run_id": "run-xyz-001",
                        "stream_session_id": "sess-2",
                        "sse_id": None,
                        "seq": 0,
                        "payload": {},
                        "reason": "stream_expired",
                    },
                ),
            ],
        ),
    )
    status_response = _make_response(status_code=200, body=_cloud_status_body())
    stream = _make_stream(_delayed_terminal_lines())
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _make_sync_client_factory(
            status_response=status_response,
            stream_response_obj=stream,
        ),
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "cloud.sse.stream_expired" in out, (
        f"expected cloud.sse.stream_expired in output:\n{out}"
    )
    assert "cloud.sse.fallback_to_poll" in out, (
        f"expected cloud.sse.fallback_to_poll boundary marker:\n{out}"
    )
    assert "[cloud sse]" in out, (
        f"expected stderr notice with [cloud sse] prefix:\n{out}"
    )
    assert "switching to poll" in out, (
        f"expected fallback transition message:\n{out}"
    )
    assert "task.completed" in out, (
        "legacy poll path must continue to render daemon events after fallback"
    )


# ── 3. --no-stream forces the legacy poll-only path ────────────────────


def test_no_stream_flag_skips_cloud_sse_thread(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (c): ``--no-stream`` skips the SSE thread even on a cloud task.

    With ``--no-stream`` set, ``_maybe_spawn_cloud_sse_thread`` must not
    be invoked: ``SSEReader`` constructor should never be called.
    """
    factory_calls: list[Any] = []

    def _tracking_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(cli_main, "SSEReader", _tracking_factory)
    _wire_attach_mocks(
        monkeypatch,
        status_body=_cloud_status_body(),
        stream_lines=[
            "data: " + json.dumps(
                {
                    "time": "t-end",
                    "type": "task.completed",
                    "data": {"exit_code": 0},
                }
            ),
        ],
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud", "--no-stream"])
    assert result.exit_code == 0, _combined(result)
    assert factory_calls == [], (
        "--no-stream must prevent SSEReader from being instantiated; "
        f"got {len(factory_calls)} call(s)"
    )
    out = _combined(result)
    assert "cloud.sse." not in out, (
        f"--no-stream must suppress all cloud SSE output:\n{out}"
    )
    assert "task.completed" in out, (
        "daemon /attach_stream must still render with --no-stream"
    )


# ── 4. runtime=local skips cloud SSE entirely ──────────────────────────


def test_local_runtime_skips_cloud_sse_thread(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (f): runtime=local → no cloud SSE thread; existing behaviour preserved."""
    factory_calls: list[Any] = []

    def _tracking_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(cli_main, "SSEReader", _tracking_factory)
    _wire_attach_mocks(
        monkeypatch,
        status_body=_cloud_status_body(runtime="local"),
        stream_lines=[
            "data: " + json.dumps(
                {
                    "time": "t1",
                    "type": "process.stdout",
                    "data": {"line": "local hello"},
                }
            ),
            "data: " + json.dumps(
                {
                    "time": "t-end",
                    "type": "task.completed",
                    "data": {"exit_code": 0},
                }
            ),
        ],
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud"])
    assert result.exit_code == 0, _combined(result)
    assert factory_calls == [], (
        "runtime=local must NOT instantiate SSEReader; "
        f"got {len(factory_calls)} call(s)"
    )
    out = _combined(result)
    assert "local hello" in out
    assert "task.completed" in out
    assert "cloud.sse." not in out, (
        f"local runtime must produce zero cloud.sse.* events:\n{out}"
    )


# ── 5. Ctrl-C / SIGINT → clean exit; the SSE worker is daemonic ───────


def test_keyboard_interrupt_during_attach_exits_cleanly(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (g): Ctrl-C mid-attach → exit 0; the SSE worker stops cleanly.

    The SSE worker is a daemon thread that respects ``stop_event``. We
    script the daemon's ``_consume_sse`` to raise ``KeyboardInterrupt``
    after a single frame; the outer ``except KeyboardInterrupt`` returns
    cleanly and ``finally`` sets ``stop_event``, joining the worker
    within the 2 s window.
    """
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(
            events_to_emit=[
                (
                    "cloud.sse.assistant",
                    {
                        "task_id": "tid-cloud",
                        "agent_id": "bc-agent-001",
                        "run_id": "run-xyz-001",
                        "stream_session_id": "sess-3",
                        "sse_id": "0",
                        "seq": 0,
                        "payload": {"text": "before sigint"},
                    },
                ),
            ],
            sleep_until_stop=True,
        ),
    )

    def _gen_lines() -> Iterator[str]:
        yield "data: " + json.dumps(
            {
                "time": "t1",
                "type": "process.stdout",
                "data": {"line": "before sigint"},
            }
        )
        raise KeyboardInterrupt()

    status_response = _make_response(status_code=200, body=_cloud_status_body())
    stream = _make_stream(_gen_lines())
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _make_sync_client_factory(
            status_response=status_response,
            stream_response_obj=stream,
        ),
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud"])
    assert result.exit_code == 0, (
        f"Ctrl-C must exit 0; got {result.exit_code} exc={result.exception!r} "
        f"out={_combined(result)!r}"
    )
    if _RecordingFakeReader.instances:
        reader = _RecordingFakeReader.instances[0]
        assert reader.stop_event_observed is not None
        assert reader.stop_event_observed.is_set(), (
            "main thread must set stop_event before joining the worker so "
            "the SSE thread shuts down cleanly (no zombie threads on Ctrl-C)"
        )


# ── 6. Renderer never calls state_store.update (static guard) ──────────


def test_cli_main_never_writes_cloud_phase_via_state_store_update() -> None:
    """AC (d): SSE-side renderer in cli/main.py must never promote cloud_phase.

    Mirrors the package-wide CI static-grep guard from T2.2.2 (PLAN.md
    §5 Invariant I-1) but scoped to the file owned by this task. Sole
    writer of ``cloud_phase`` remains ``daemon/cloud_poller.py``.
    """
    src = Path(cli_main.__file__).read_text(encoding="utf-8")
    matches = re.findall(r"state_store\s*\.\s*update\s*\([^)]*cloud_phase\s*=", src)
    assert matches == [], (
        f"cli/main.py must NEVER call state_store.update(... cloud_phase=...); "
        f"found {len(matches)} match(es): {matches!r}"
    )
    assert "from popolaloom.daemon.state import StateStore" not in src, (
        "cli/main.py must not import StateStore (sole writer is cloud_poller)"
    )


# ── 7. Missing cursor_agent_id → fall back without spawning thread ─────


def test_missing_cursor_agent_id_skips_cloud_sse_with_notice(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud runtime but no cursor_agent_id → skip SSE + stderr notice."""
    factory_calls: list[Any] = []

    def _tracking_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(cli_main, "SSEReader", _tracking_factory)
    _wire_attach_mocks(
        monkeypatch,
        status_body=_cloud_status_body(cursor_agent_id=None),
        stream_lines=[
            "data: " + json.dumps(
                {
                    "time": "t-end",
                    "type": "task.completed",
                    "data": {"exit_code": 0},
                }
            ),
        ],
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud"])
    assert result.exit_code == 0, _combined(result)
    assert factory_calls == [], (
        f"missing cursor_agent_id must short-circuit before SSEReader; "
        f"got {len(factory_calls)} call(s)"
    )
    out = _combined(result)
    assert "[cloud sse]" in out and "cursor_agent_id" in out, (
        f"expected stderr notice naming the missing field:\n{out}"
    )


# ── 8. Missing CURSOR_API_KEY → skip without spawning thread ───────────


def test_missing_api_key_skips_cloud_sse_with_notice(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud runtime + agent/run ids present but no CURSOR_API_KEY → skip."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    # Isolate the OS keyring precedence slot so a developer-stored
    # CURSOR_API_KEY in the system keychain doesn't satisfy
    # `resolve_cursor_api_key` and force the SSE thread to spawn — which
    # would fail this test that asserts the cloud-SSE branch short-circuits
    # cleanly when no key is configured anywhere.
    monkeypatch.setattr(
        "popolaloom.credentials._import_keyring", lambda: None
    )
    factory_calls: list[Any] = []

    def _tracking_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(cli_main, "SSEReader", _tracking_factory)
    _wire_attach_mocks(
        monkeypatch,
        status_body=_cloud_status_body(),
        stream_lines=[
            "data: " + json.dumps(
                {
                    "time": "t-end",
                    "type": "task.completed",
                    "data": {"exit_code": 0},
                }
            ),
        ],
    )

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud"])
    assert result.exit_code == 0, _combined(result)
    assert factory_calls == [], (
        f"missing CURSOR_API_KEY must short-circuit before SSEReader; "
        f"got {len(factory_calls)} call(s)"
    )
    out = _combined(result)
    assert "[cloud sse]" in out and "CURSOR_API_KEY" in out, (
        f"expected stderr notice naming the missing env var:\n{out}"
    )


# ── 9. Direct unit test: pump network error → fallback envelope ────────


def test_run_cloud_sse_pump_network_error_emits_fallback_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (b)+(g): ``httpx.ConnectError`` from pump → fallback envelope + WARNING log.

    Drives ``_run_cloud_sse_pump`` directly (no Typer / threading) so the
    exact envelope shape and ``data.reason`` payload are observable.
    """
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(
            exception_to_raise=httpx.ConnectError("simulated"),
        ),
    )
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-x",
        agent_id="bc-x",
        run_id="run-y",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1, (
        f"expected exactly one cloud.sse.fallback_to_poll envelope; "
        f"got {len(fallback)} (events={sink.events!r})"
    )
    payload = fallback[0]["data"]
    assert payload["reason"] == "network_error"
    assert payload["task_id"] == "tid-x"
    assert "ConnectError" in payload.get("error", ""), (
        f"expected error context to mention exception type; got {payload!r}"
    )


def test_run_cloud_sse_pump_invalid_last_event_id_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid Last-Event-ID exception → fallback with reason=invalid_last_event_id."""
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(
            exception_to_raise=CursorCloudStreamInvalidLastEventIdError("bad id"),
        ),
    )
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-y",
        agent_id="bc-y",
        run_id="run-z",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    assert fallback[0]["data"]["reason"] == "invalid_last_event_id"


def test_run_cloud_sse_pump_clean_stream_end_still_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean ``pump()`` return → fallback envelope with reason=stream_ended.

    The boundary marker is intentionally always emitted when the SSE
    thread bows out (unless the main thread requested a graceful stop)
    so downstream consumers can tell that ``cloud.sse.*`` events have
    stopped flowing for this run.
    """
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(),
    )
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-z",
        agent_id="bc-z",
        run_id="run-w",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    assert fallback[0]["data"]["reason"] == "stream_ended"


def test_run_cloud_sse_pump_stop_event_set_skips_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graceful main-thread stop → no fallback envelope (the renderer is exiting too).

    The fallback boundary marker is only meaningful when the SSE channel
    bows out *while the renderer is still running*. If the main thread
    has already signalled shutdown via ``stop_event``, suppressing the
    notice avoids the log noise of two concurrent goodbyes.
    """
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(),
    )
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()
    stop_event.set()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-q",
        agent_id="bc-q",
        run_id="run-q",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert fallback == [], (
        f"stop_event already set must suppress the fallback notice; "
        f"got envelopes={fallback!r}"
    )


# ── 10. Sink shape: source / type / data are preserved ─────────────────


def test_cloud_sse_event_sink_envelope_shape() -> None:
    """``_CloudSSEEventSink.append`` returns the CloudEvents envelope it built.

    Keys must match ``EventLog.append``'s contract so downstream consumers
    don't have to special-case CLI-side events.
    """
    sink = cli_main._CloudSSEEventSink()
    envelope = sink.append("cloud.sse.assistant", {"text": "hi", "seq": 0})
    for key in ("specversion", "id", "source", "type", "time", "data"):
        assert key in envelope, f"missing required key {key!r}: {envelope!r}"
    assert envelope["type"] == "cloud.sse.assistant"
    assert envelope["data"] == {"text": "hi", "seq": 0}
    assert envelope["source"].startswith("popola/")
    assert envelope["specversion"] == "1.0"
    assert sink.events == [envelope]


# ── 11. CursorCloudError raised from pump → cursor_error fallback ──────


def test_run_cloud_sse_pump_cursor_error_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic ``CursorCloudError`` from pump → fallback with reason=cursor_error."""
    monkeypatch.setattr(
        cli_main,
        "SSEReader",
        _make_fake_reader_factory(
            exception_to_raise=CursorCloudError("upstream said no"),
        ),
    )
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-q",
        agent_id="bc-q",
        run_id="run-q",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    assert fallback[0]["data"]["reason"] == "cursor_error"
    assert "CursorCloudError" in fallback[0]["data"]["error"]
