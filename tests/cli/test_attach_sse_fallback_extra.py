"""Coverage gap-filler for ``_run_cloud_sse_pump`` fall-through reasons (T4.1.1.b).

Per ``.local/.agent/active/v0.8.6-cloud-sse/COVERAGE.md`` §4.2, the
``popola attach`` cloud SSE pump in :mod:`popolaloom.cli.main` sits at
93.92 % default-lane coverage with the gap concentrated in the five
narrower fall-through reasons that the existing
``tests/cli/test_attach_sse_fallback.py`` (T2.2.1's owner-file) skipped
to keep that suite focused on the happy / 410 / network / cursor /
clean-stream paths:

* ``cursor_run_id``-invalid early return in
  :func:`_maybe_spawn_cloud_sse_thread` (status snapshot has
  ``cursor_agent_id`` but the ``cursor_run_id`` is ``None`` / wrong
  type — symmetric with the existing missing-agent_id test);
* ``client_init_failed`` — :class:`CloudCursorClient` raises ``ValueError``
  during construction (e.g., empty / non-string api_key);
* ``reader_init_failed`` — :class:`SSEReader` raises ``TypeError`` /
  ``AssertionError`` during construction;
* ``invalid_last_event_id`` — :meth:`SSEReader.pump` raises
  :class:`CursorCloudStreamInvalidLastEventIdError` (also covered in
  T2.2.1 but re-exercised here as the parametrize completeness anchor);
* ``unexpected_error`` — :meth:`SSEReader.pump` raises a generic
  ``RuntimeError`` (anything that is *not* a known network / cursor /
  invalid-LEI exception), which the broad ``except Exception`` clause
  catches and tags as ``unexpected_error``.

Mock pattern mirrors ``tests/cli/test_attach_sse_fallback.py`` (CliRunner
+ monkeypatched ``cli_main.SSEReader`` + ``cli_main.CloudCursorClient``)
so no real ``api.cursor.com`` request is ever issued and no real popolad
socket is touched.

T4.1.1.b — owned file (NEW), parallel-safe with W2.2.1's owner-file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from popolaloom.adapters.cursor_cloud import (
    CursorCloudStreamInvalidLastEventIdError,
)
from popolaloom.cli import main as cli_main

# ── shared fixtures (mirror tests/cli/test_attach_sse_fallback.py) ───────


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
def cursor_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-empty CURSOR_API_KEY so cloud SSE is not skipped."""
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
    import httpx

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
    cursor_agent_id: str | None = "bc-agent-extra",
    cursor_run_id: str | None = "run-extra-001",
    state: str = "running",
) -> dict[str, Any]:
    """Build a ``GET /status/{task_id}`` body shaped like the daemon emits."""
    body: dict[str, Any] = {
        "task_id": "tid-cloud-extra",
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


# ── 1. cursor_run_id-invalid → early return in _maybe_spawn_cloud_sse_thread ──


def test_missing_cursor_run_id_skips_cloud_sse_with_notice(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cursor_agent_id`` present, ``cursor_run_id`` absent → skip SSE + stderr notice.

    Symmetric with the existing missing-agent_id test in
    ``test_attach_sse_fallback.py``; covers the
    ``if not run_id or not isinstance(run_id, str)`` branch at
    ``cli/main.py:730-740`` (logger.warning → typer.echo → return None).
    No SSEReader / CloudCursorClient should be instantiated.
    """
    factory_calls: list[Any] = []

    def _tracking_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        return MagicMock()

    cloud_factory_calls: list[Any] = []

    def _tracking_cloud(*args: Any, **kwargs: Any) -> Any:
        cloud_factory_calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(cli_main, "SSEReader", _tracking_factory)
    monkeypatch.setattr(cli_main, "CloudCursorClient", _tracking_cloud)
    _wire_attach_mocks(
        monkeypatch,
        status_body=_cloud_status_body(cursor_run_id=None),
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

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud-extra"])
    assert result.exit_code == 0, _combined(result)
    assert factory_calls == [], (
        "missing cursor_run_id must short-circuit before SSEReader; "
        f"got {len(factory_calls)} call(s)"
    )
    assert cloud_factory_calls == [], (
        "missing cursor_run_id must short-circuit before CloudCursorClient; "
        f"got {len(cloud_factory_calls)} call(s)"
    )
    out = _combined(result)
    assert "[cloud sse]" in out and "cursor_run_id" in out, (
        f"expected stderr notice naming the missing field:\n{out}"
    )


def test_invalid_typed_cursor_run_id_skips_cloud_sse_with_notice(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cursor_run_id`` is a non-string (int) → still treated as invalid; skip SSE.

    Covers the ``not isinstance(run_id, str)`` half of the ``or`` at
    ``cli/main.py:730``.
    """
    factory_calls: list[Any] = []

    def _tracking_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(cli_main, "SSEReader", _tracking_factory)
    monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

    body = _cloud_status_body()
    body["cursor_run_id"] = 12345  # int not str — drives the isinstance check
    _wire_attach_mocks(
        monkeypatch,
        status_body=body,
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

    result = runner.invoke(cli_main.app, ["attach", "tid-cloud-extra"])
    assert result.exit_code == 0, _combined(result)
    assert factory_calls == []
    out = _combined(result)
    assert "[cloud sse]" in out and "cursor_run_id" in out


# ── 2. client_init_failed: CloudCursorClient.__init__ raises ValueError ──


def test_run_cloud_sse_pump_client_init_value_error_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CloudCursorClient(api_key)`` raising ``ValueError`` → ``client_init_failed``.

    Covers ``cli/main.py:802-808`` (the ``except (ValueError, CursorCloudError)``
    clause around the ``client = CloudCursorClient(...)`` construction).
    """
    def _raise_value_error(_api_key: str) -> Any:
        raise ValueError("simulated empty api_key rejected")

    monkeypatch.setattr(cli_main, "CloudCursorClient", _raise_value_error)
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="anything",
        task_id="tid-client-init-fail",
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
    assert payload["reason"] == "client_init_failed"
    assert payload["task_id"] == "tid-client-init-fail"
    assert "ValueError" in payload.get("error", "")
    assert "simulated empty api_key rejected" in payload.get("error", "")


# ── 3. reader_init_failed: SSEReader.__init__ raises ───────────────────


def test_run_cloud_sse_pump_reader_init_type_error_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SSEReader(...)`` raising ``TypeError`` → ``reader_init_failed``.

    Covers ``cli/main.py:818-822`` (the ``except (TypeError, AssertionError)``
    clause around the ``reader = SSEReader(...)`` construction).
    """
    monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

    def _raise_type_error(*_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("simulated bad ctor signature")

    monkeypatch.setattr(cli_main, "SSEReader", _raise_type_error)
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-reader-init-fail",
        agent_id="bc-x",
        run_id="run-y",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    payload = fallback[0]["data"]
    assert payload["reason"] == "reader_init_failed"
    assert payload["task_id"] == "tid-reader-init-fail"
    assert "TypeError" in payload.get("error", "")


def test_run_cloud_sse_pump_reader_init_assertion_error_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SSEReader(...)`` raising ``AssertionError`` → ``reader_init_failed``.

    The same ``except (TypeError, AssertionError)`` clause; we exercise
    the AssertionError half here because it is the runtime guard SSEReader
    uses to bar a :class:`StateStore` reference (``state-source-of-truth.md``
    §1.2 rule 1 — the I-2 sole-writer invariant).
    """
    monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

    def _raise_assert(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("SSEReader rejects state_store collaborators")

    monkeypatch.setattr(cli_main, "SSEReader", _raise_assert)
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-reader-assert",
        agent_id="bc-x",
        run_id="run-y",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    payload = fallback[0]["data"]
    assert payload["reason"] == "reader_init_failed"
    assert "AssertionError" in payload.get("error", "")


# ── 4. invalid_last_event_id: pump raises CursorCloudStreamInvalidLastEventIdError ──


def test_run_cloud_sse_pump_invalid_last_event_id_emits_fallback_with_kwarg_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pump(stop_event=...)`` raises invalid-LEI → ``invalid_last_event_id``.

    Covers the ``except CursorCloudStreamInvalidLastEventIdError`` clause
    at ``cli/main.py:826-833``. (This complements the existing
    ``test_run_cloud_sse_pump_invalid_last_event_id_emits_fallback`` in
    T2.2.1's file by additionally asserting the exception message is
    surfaced in the fallback envelope's ``error`` field — proving the
    No-Silent-Failures contract is honoured for this branch too.)
    """
    monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

    class _BadLeiReader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def pump(self, stop_event: threading.Event | None = None) -> None:
            _ = stop_event
            raise CursorCloudStreamInvalidLastEventIdError(
                "Last-Event-ID header malformed"
            )

    monkeypatch.setattr(cli_main, "SSEReader", _BadLeiReader)
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-bad-lei",
        agent_id="bc-x",
        run_id="run-y",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    payload = fallback[0]["data"]
    assert payload["reason"] == "invalid_last_event_id"
    assert payload["task_id"] == "tid-bad-lei"
    assert "Last-Event-ID header malformed" in payload.get("error", "")


# ── 5. unexpected_error: pump raises generic RuntimeError ──────────────


def test_run_cloud_sse_pump_unexpected_runtime_error_emits_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic ``RuntimeError`` from pump → ``unexpected_error``.

    Covers the broad ``except Exception`` clause at ``cli/main.py:846-849``
    — the catch-all that converts any *unrecognised* exception class into
    a ``cloud.sse.fallback_to_poll`` envelope so unknown errors never
    crash the worker thread (No-Silent-Failures rule).
    """
    monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

    class _BoomReader:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def pump(self, stop_event: threading.Event | None = None) -> None:
            _ = stop_event
            raise RuntimeError("simulated unexpected upstream blow-up")

    monkeypatch.setattr(cli_main, "SSEReader", _BoomReader)
    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()

    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id="tid-boom",
        agent_id="bc-x",
        run_id="run-y",
        sink=sink,
        stop_event=stop_event,
    )

    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1
    payload = fallback[0]["data"]
    assert payload["reason"] == "unexpected_error"
    assert payload["task_id"] == "tid-boom"
    assert "RuntimeError" in payload.get("error", "")
    assert "simulated unexpected upstream blow-up" in payload.get("error", "")


# ── 6. parametrize roll-up: every reason produces a single fallback envelope ──


@pytest.mark.parametrize(
    ("reason", "exception_factory"),
    [
        ("client_init_failed", lambda: ValueError("client ctor refused api_key")),
        ("reader_init_failed", lambda: TypeError("reader ctor signature")),
        (
            "invalid_last_event_id",
            lambda: CursorCloudStreamInvalidLastEventIdError("bad LEI"),
        ),
        ("unexpected_error", lambda: RuntimeError("untyped boom")),
    ],
)
def test_run_cloud_sse_pump_each_reason_emits_exactly_one_fallback(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    exception_factory: Any,
) -> None:
    """Roll-up: each fall-through reason produces exactly one envelope.

    Asserts the cardinality contract — multiple raised exceptions must
    not produce multiple ``cloud.sse.fallback_to_poll`` envelopes (the
    ``finally`` clause emits exactly one boundary marker per pump
    invocation, regardless of which catch arm fired).
    """
    if reason == "client_init_failed":
        def _raise(_api_key: str) -> Any:
            raise exception_factory()

        monkeypatch.setattr(cli_main, "CloudCursorClient", _raise)
        monkeypatch.setattr(cli_main, "SSEReader", MagicMock())
    elif reason == "reader_init_failed":
        monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

        def _raise_reader(*_args: Any, **_kwargs: Any) -> Any:
            raise exception_factory()

        monkeypatch.setattr(cli_main, "SSEReader", _raise_reader)
    else:  # invalid_last_event_id / unexpected_error → raise from pump
        monkeypatch.setattr(cli_main, "CloudCursorClient", MagicMock())

        class _RaisingReader:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def pump(self, stop_event: threading.Event | None = None) -> None:
                _ = stop_event
                raise exception_factory()

        monkeypatch.setattr(cli_main, "SSEReader", _RaisingReader)

    sink = cli_main._CloudSSEEventSink()
    stop_event = threading.Event()
    cli_main._run_cloud_sse_pump(
        api_key="fake",
        task_id=f"tid-{reason}",
        agent_id="bc-x",
        run_id="run-y",
        sink=sink,
        stop_event=stop_event,
    )
    fallback = [e for e in sink.events if e["type"] == "cloud.sse.fallback_to_poll"]
    assert len(fallback) == 1, (
        f"reason={reason} must emit exactly one fallback envelope; "
        f"got {len(fallback)}"
    )
    assert fallback[0]["data"]["reason"] == reason
