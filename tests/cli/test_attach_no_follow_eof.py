"""Regression tests for BUG-C — ``popola attach --no-follow`` EOF handling.

Per ``.local/feedbacks/feedback_for_v0.7.0.md`` item #6 (raw lines 13–16):

    [BUG] ``popola attach --no-follow`` 出现 ``httpx.ReadTimeout``

    复现：``popola attach <task_id> --no-follow`` 在事件流量较大时
    (>200 events) 会 hang ~16s 然后 raise ``ReadTimeout: timed out``,
    exit code 1. 完整 traceback 来自
    ``httpx/_transports/default.py:118 map_httpcore_exceptions``,
    调用栈 ``_client.py:153 → _decoder.py:953``.

The v0.7.1 fix lives in :func:`popolaloom.cli.main._consume_sse`:

1. **Approach (a) — terminal-event break.** When
   ``terminate_on_terminal=True`` (always set by ``--no-follow``) and a
   terminal event (``task.completed`` / ``task.failed`` /
   ``task.canceled``) is observed, **break** out of the iteration
   immediately so the ``with client.stream(...)`` context manager closes
   the connection from the client side and no further read attempts are
   made. This was a literal ``continue`` bug pre-v0.7.1: the loop
   continued iterating past the terminal event and then waited for more
   data that never arrived. Recognising a server-side
   ``event: end-of-stream`` marker is also handled forward-compat.

2. **Approach (b) — defensive ReadTimeout swallow.** As a belt-and-
   suspenders fallback (in case the server doesn't send a terminal
   event on a particular path, or the OS-level connection close races
   with the client's last read), :class:`httpx.ReadTimeout` is caught
   inside ``_consume_sse``. If at least one terminal event was already
   rendered, the timeout is treated as clean stream-end and ``True`` is
   returned; otherwise the timeout is logged + re-raised (genuine
   server-hung-mid-stream is still surfaced as an error per the
   workspace "No Silent Failures" rule).

These tests intentionally do **not** spawn a real daemon or sleep
waiting on network I/O — they drive ``_consume_sse`` and the ``attach``
Typer command directly via mocked ``iter_lines`` generators that emit
the exact pathological pattern users reported.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

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


def _mock_response(*, status_code: int, body: Any) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


def _mock_stream(iter_lines_return: Any, *, status_code: int = 200) -> MagicMock:
    """Build a context-manager-shaped streaming response double.

    ``iter_lines_return`` may be either a list (of pre-baked frames) or
    a generator (so we can ``raise httpx.ReadTimeout`` mid-iteration to
    simulate the BUG-C scenario).
    """
    stream = MagicMock()
    stream.status_code = status_code
    stream.iter_lines.return_value = iter_lines_return
    stream.__enter__.return_value = stream
    stream.__exit__.return_value = False
    return stream


def _attach_client_factory(
    *, status_response: MagicMock, stream_response_obj: MagicMock
) -> Any:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = status_response
    client.stream = MagicMock(return_value=stream_response_obj)
    return lambda *a, **kw: client


# ── 1. unit: _consume_sse breaks on terminal event (the literal continue bug) ──


def test_consume_sse_breaks_immediately_on_terminal_event() -> None:
    """BUG-C primary fix: terminal event must terminate iteration.

    Pre-v0.7.1 the code path used ``continue`` after seeing a terminal
    event, so the loop kept advancing the iterator. We assert that
    ``iter_lines`` is **not** drained past the terminal frame: a side
    effect of the regression that produced 16s hangs.
    """
    response = MagicMock(spec=httpx.Response)

    side_effect_calls: list[str] = []

    def _gen() -> Iterator[str]:
        for i in range(3):
            line = "data: " + json.dumps(
                {
                    "time": f"t{i}",
                    "type": "process.stdout",
                    "data": {"line": f"line{i}"},
                }
            )
            side_effect_calls.append(f"yielded:{i}")
            yield line
        side_effect_calls.append("yielded:terminal")
        yield "data: " + json.dumps(
            {"time": "t-end", "type": "task.completed", "data": {"exit_code": 0}}
        )
        side_effect_calls.append("post_terminal_should_not_be_reached")
        yield "data: should_not_appear"

    response.iter_lines.return_value = _gen()
    saw_terminal = cli_main._consume_sse(response, terminate_on_terminal=True)

    assert saw_terminal is True
    assert "post_terminal_should_not_be_reached" not in side_effect_calls, (
        "BUG-C regression: _consume_sse iterated past the terminal event "
        "(the original `continue` bug); should `break` instead."
    )


def test_consume_sse_breaks_on_event_end_of_stream_marker() -> None:
    """Forward-compat: server-side ``event: end-of-stream`` marker breaks loop.

    The v0.7.1 daemon does not yet emit this marker (the server simply
    returns from the producer and closes the connection). Recognising
    it here is a no-op now but lets a future server release ship the
    explicit marker without a coupled CLI change. The marker also
    counts as a "terminal" observation for ReadTimeout swallowing.
    """
    response = MagicMock(spec=httpx.Response)

    def _gen() -> Iterator[str]:
        yield "data: " + json.dumps(
            {"time": "t1", "type": "process.stdout", "data": {"line": "x"}}
        )
        yield "event: end-of-stream"
        yield "data: should_not_be_seen"

    response.iter_lines.return_value = _gen()
    saw_terminal = cli_main._consume_sse(response, terminate_on_terminal=True)
    assert saw_terminal is True


# ── 2. unit: defensive ReadTimeout handling (approach b — belt-and-braces) ──


def test_consume_sse_swallows_read_timeout_after_terminal_event() -> None:
    """ReadTimeout fired AFTER a terminal event ⇒ treat as clean stream-end.

    The pathological scenario users reported: server-side producer
    returned cleanly, but ``httpx`` misclassified the OS-level EOF as a
    ``ReadTimeout`` (likely a buffer/connection-pool race surfaced by
    high frame counts). Regression: caller sees ``exit code 1`` even
    though all events were received intact.

    To exercise this path in isolation we configure ``terminate_on_terminal=False``
    so the loop keeps iterating past the terminal frame and runs into
    the simulated ReadTimeout.
    """
    response = MagicMock(spec=httpx.Response)

    def _gen() -> Iterator[str]:
        yield "data: " + json.dumps(
            {"time": "t1", "type": "task.completed", "data": {"exit_code": 0}}
        )
        raise httpx.ReadTimeout("server closed but httpx misclassified EOF")

    response.iter_lines.return_value = _gen()
    saw_terminal = cli_main._consume_sse(response, terminate_on_terminal=False)
    assert saw_terminal is True


def test_consume_sse_propagates_read_timeout_before_any_terminal_event() -> None:
    """ReadTimeout BEFORE any terminal event ⇒ re-raise (genuine server hang).

    No Silent Failures rule: if the daemon truly hangs mid-stream we
    must not swallow it. ``--no-follow`` callers that depend on the
    exit-code semantics need this to stay an error.
    """
    response = MagicMock(spec=httpx.Response)

    def _gen() -> Iterator[str]:
        yield "data: " + json.dumps(
            {
                "time": "t1",
                "type": "process.stdout",
                "data": {"line": "partial"},
            }
        )
        raise httpx.ReadTimeout("genuine server hang mid-stream")

    response.iter_lines.return_value = _gen()
    with pytest.raises(httpx.ReadTimeout):
        cli_main._consume_sse(response, terminate_on_terminal=True)


# ── 3. integration: full ``attach --no-follow`` exit-0 with high-volume EOF ──


def test_attach_no_follow_high_volume_with_terminal_then_eof_exits_zero(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end BUG-C reproducer: 200 events + terminal + simulated EOF/timeout.

    Pre-fix: ``--no-follow`` would hang ~16s on the read timeout and
    exit 1 because the post-terminal ``continue`` kept driving
    ``iter_lines()`` until ``httpx.ReadTimeout`` fired.

    Post-fix: terminal event is observed, ``break`` exits the loop, the
    stream context manager closes the connection, and the CLI exits 0.
    The test also stages a final ``ReadTimeout`` raise so that even if
    something slipped past the break we'd still be guarded by the
    defensive ``except`` (approach b). Either fix lane would alone be
    enough to pass this test, which is exactly the safety we want.
    """
    status_response = _mock_response(
        status_code=200, body={"state": "completed", "task_id": "tid"}
    )

    def _gen_lines() -> Iterator[str]:
        for i in range(200):
            yield "data: " + json.dumps(
                {
                    "time": f"t{i}",
                    "type": "process.stdout",
                    "data": {"line": f"line{i}"},
                }
            )
        yield "data: " + json.dumps(
            {"time": "t-end", "type": "task.completed", "data": {"exit_code": 0}}
        )
        raise httpx.ReadTimeout("post-terminal EOF misclassified by httpx")

    stream = _mock_stream(_gen_lines())
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        _attach_client_factory(
            status_response=status_response, stream_response_obj=stream
        ),
    )

    result = runner.invoke(cli_main.app, ["attach", "tid", "--no-follow"])
    assert result.exit_code == 0, (
        f"BUG-C regression: --no-follow should exit 0 after seeing terminal "
        f"event even when the post-EOF read raises ReadTimeout. "
        f"exit={result.exit_code} out={_combined(result)!r} "
        f"exc={result.exception!r}"
    )
    out = _combined(result)
    assert "task.completed" in out
    assert "line0" in out
    assert "line199" in out
