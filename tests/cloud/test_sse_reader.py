"""Unit tests for the v0.8.6 SSE reader (T2.1.1).

Owned by L3 Subagent T2.1.1 — covers:

- Module-level :func:`popolaloom.adapters.cursor_cloud.iter_events` SSE
  parser (multi-line ``data:``, ``id:``, all 8 Cursor event types).
- :meth:`CloudCursorClient.stream_run` HTTP wrapping (``Last-Event-ID``
  header, 410 ``stream_expired``, 410 ``invalid_last_event_id``).
- :class:`SSEReader` pumping into an :class:`EventLog`: the
  ``(task_id, run_id, stream_session_id, sse_id, seq)`` envelope quintuple,
  per-session ``seq`` monotonicity, ``(run_id, sse_event_id)`` LRU dedup
  with ``cloud.sse.dedup_drop`` summary, ``cloud.sse.parse_error`` on
  malformed frames, ``cloud.sse.stream_expired`` on 410, and the
  Q-A-8 sole-writer ``StateStore`` rejection.

Sources:

- ``.local/research/v0.8.6_sse/sse-event-schema.md`` §3 mapping table
- ``.local/research/v0.8.6_sse/state-source-of-truth.md`` §1 writer
  contract, §2.1 idempotency key, §6 invariants I-3 / I-5
- ``.local/.agent/active/v0.8.6-cloud-sse/DECISIONS.md`` OQ-5 / OQ-6
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudStreamExpiredError,
    CursorCloudStreamInvalidLastEventIdError,
    SSEReader,
    iter_events,
)
from popolaloom.daemon.event_log import EventLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MockHandler = Callable[[httpx.Request], httpx.Response]


def _attach_mock_transport(client: CloudCursorClient, handler: _MockHandler) -> None:
    """Replace ``client._client`` with a ``MockTransport``-backed httpx Client.

    The CloudCursorClient owns a real :class:`httpx.Client` in its
    constructor; for tests we close it and substitute one wired to
    :class:`httpx.MockTransport`. Internal-only — never used in production.
    """
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client._base_url,
        auth=(client._api_key, ""),
        timeout=client._timeout_s,
    )


def _stream_chunks_handler(
    chunks_per_call: list[list[bytes]],
    *,
    on_request: list[httpx.Request] | None = None,
    status_per_call: list[int] | None = None,
    json_per_call: list[dict[str, Any] | None] | None = None,
) -> _MockHandler:
    """Build a handler that returns one streamed body per call.

    Each call to the transport pops the next entry from ``chunks_per_call``;
    when ``json_per_call[i]`` is non-None it is used instead of streamed
    chunks (for synthetic 4xx error bodies). ``status_per_call`` defaults
    to ``[200, 200, ...]`` if omitted.
    """
    calls = {"i": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        i = calls["i"]
        calls["i"] += 1
        if on_request is not None:
            on_request.append(request)
        status = 200
        if status_per_call is not None and i < len(status_per_call):
            status = status_per_call[i]
        if json_per_call is not None and i < len(json_per_call) and json_per_call[i] is not None:
            return httpx.Response(status, json=json_per_call[i])
        chunks = chunks_per_call[i] if i < len(chunks_per_call) else []
        return httpx.Response(status, content=iter(chunks))

    return _handler


def _make_event_log(tmp_path: Path, name: str = "task.jsonl") -> EventLog:
    """Build an EventLog with the background fsync worker disabled (test mode)."""
    return EventLog(tmp_path / name, fsync_interval_s=0.0)


def _make_client(api_key: str = "test-key") -> CloudCursorClient:
    return CloudCursorClient(api_key, base_url=CURSOR_API_BASE)


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


# ---------------------------------------------------------------------------
# 1. iter_events — all 8 SSE event types parsed (sse-event-schema.md §3)
# ---------------------------------------------------------------------------


def test_iter_events_parses_all_8_cursor_event_types() -> None:
    """Each of the 8 Cursor SSE event types is parsed into the expected tuple."""
    lines = [
        "event: status",
        'data: {"runId":"r1","status":"RUNNING"}',
        "id: id-1",
        "",
        "event: assistant",
        'data: {"text":"hi"}',
        "id: id-2",
        "",
        "event: thinking",
        'data: {"text":"plan"}',
        "id: id-3",
        "",
        "event: tool_call",
        'data: {"tool":"shell","args":{"cmd":"ls"}}',
        "id: id-4",
        "",
        "event: heartbeat",
        "data: {}",
        "",
        "event: result",
        'data: {"runId":"r1","status":"FINISHED"}',
        "id: id-6",
        "",
        "event: error",
        'data: {"code":"upstream_error","message":"oops"}',
        "id: id-7",
        "",
        "event: done",
        "data: {}",
        "id: id-8",
        "",
    ]
    parsed = list(iter_events(lines))
    types = [event_type for event_type, _, _ in parsed]
    assert types == [
        "status",
        "assistant",
        "thinking",
        "tool_call",
        "heartbeat",
        "result",
        "error",
        "done",
    ]
    by_type = {event_type: (data, sse_id) for event_type, data, sse_id in parsed}
    assert by_type["status"][0]["status"] == "RUNNING"
    assert by_type["assistant"][1] == "id-2"
    assert by_type["thinking"][0]["text"] == "plan"
    assert by_type["tool_call"][0]["tool"] == "shell"
    assert by_type["heartbeat"][1] is None
    assert by_type["result"][0]["status"] == "FINISHED"
    assert by_type["error"][0]["code"] == "upstream_error"
    assert by_type["done"][0] == {}
    assert by_type["done"][1] == "id-8"


# ---------------------------------------------------------------------------
# 2. iter_events — multi-line data: concatenated with `\n`
# ---------------------------------------------------------------------------


def test_iter_events_handles_multiline_data_concatenation() -> None:
    """Multi-line ``data:`` lines must be joined with literal ``\\n``.

    The test uses pretty-printed JSON across three ``data:`` lines so that
    the join-with-newline still yields *valid* JSON (whitespace between
    tokens is allowed). Each line carries one segment of the object.
    """
    lines = [
        "event: assistant",
        "data: {",
        'data:   "text": "hello",',
        'data:   "extra": "world"',
        "data: }",
        "id: ml-1",
        "",
    ]
    parsed = list(iter_events(lines))
    assert len(parsed) == 1
    event_type, data, sse_id = parsed[0]
    assert event_type == "assistant"
    assert sse_id == "ml-1"
    assert data == {"text": "hello", "extra": "world"}


# ---------------------------------------------------------------------------
# 3. iter_events — id: lines / absent → None
# ---------------------------------------------------------------------------


def test_iter_events_id_line_present_and_absent() -> None:
    lines = [
        "event: status",
        'data: {"phase":"a"}',
        "id: with-id",
        "",
        "event: heartbeat",
        "data: {}",
        "",
    ]
    parsed = list(iter_events(lines))
    assert parsed == [
        ("status", {"phase": "a"}, "with-id"),
        ("heartbeat", {}, None),
    ]


def test_iter_events_strips_optional_leading_space_and_handles_comments() -> None:
    """Leading ' ' after ':' is stripped per SSE spec; ':comment' lines are no-ops."""
    lines = [
        ": this is a comment",
        "event: status",
        'data:{"a":1}',
        ": keepalive",
        "id:no-space",
        "",
    ]
    parsed = list(iter_events(lines))
    assert parsed == [("status", {"a": 1}, "no-space")]


# ---------------------------------------------------------------------------
# 4. stream_run — 410 stream_expired raises CursorCloudStreamExpiredError
# ---------------------------------------------------------------------------


def test_stream_run_410_stream_expired_raises_no_reconnect() -> None:
    requests_seen: list[httpx.Request] = []
    handler = _stream_chunks_handler(
        chunks_per_call=[[]],
        on_request=requests_seen,
        status_per_call=[410],
        json_per_call=[
            {"error": {"code": "stream_expired", "message": "retention elapsed"}},
        ],
    )
    client = _make_client()
    _attach_mock_transport(client, handler)

    with pytest.raises(CursorCloudStreamExpiredError) as exc_info:
        list(client.stream_run("agent-1", "run-1"))

    assert exc_info.value.status_code == 410
    # No-reconnect contract — the mock transport was hit exactly once.
    assert len(requests_seen) == 1
    assert requests_seen[0].url.path == "/v1/agents/agent-1/runs/run-1/stream"
    client.close()


# ---------------------------------------------------------------------------
# 5. stream_run — 410 invalid_last_event_id raises recoverable, retry succeeds
# ---------------------------------------------------------------------------


def test_stream_run_410_invalid_last_event_id_is_recoverable_then_retry_succeeds() -> None:
    requests_seen: list[httpx.Request] = []
    second_call_chunks = [
        b"event: status\ndata: {\"runId\":\"r1\",\"status\":\"RUNNING\"}\nid: 1\n\n",
    ]
    handler = _stream_chunks_handler(
        chunks_per_call=[[], second_call_chunks],
        on_request=requests_seen,
        status_per_call=[410, 200],
        json_per_call=[
            {"error": {"code": "invalid_last_event_id", "message": "stale cursor"}},
            None,
        ],
    )
    client = _make_client()
    _attach_mock_transport(client, handler)

    with pytest.raises(CursorCloudStreamInvalidLastEventIdError) as exc_info:
        list(client.stream_run("a1", "r1", last_event_id="stale-id"))
    assert exc_info.value.status_code == 410
    assert exc_info.value.is_retryable is True

    retried = list(client.stream_run("a1", "r1"))
    assert len(retried) == 1
    event_type, data, sse_id = retried[0]
    assert event_type == "status"
    assert data["status"] == "RUNNING"
    assert sse_id == "1"

    assert requests_seen[0].headers.get("Last-Event-ID") == "stale-id"
    assert requests_seen[1].headers.get("Last-Event-ID") is None
    client.close()


# ---------------------------------------------------------------------------
# 6. stream_run — Last-Event-ID header is sent on resume
# ---------------------------------------------------------------------------


def test_stream_run_sends_last_event_id_header_on_resume() -> None:
    requests_seen: list[httpx.Request] = []
    chunks = [b"event: heartbeat\ndata: {}\n\n"]
    handler = _stream_chunks_handler(
        chunks_per_call=[chunks],
        on_request=requests_seen,
    )
    client = _make_client()
    _attach_mock_transport(client, handler)

    list(client.stream_run("a1", "r1", last_event_id="resume-7"))
    list(client.stream_run("a1", "r1"))  # noqa: F841 — no header expected

    assert requests_seen[0].headers.get("Last-Event-ID") == "resume-7"
    assert "Last-Event-ID" not in requests_seen[1].headers
    assert requests_seen[0].headers.get("Accept") == "text/event-stream"
    client.close()


# ---------------------------------------------------------------------------
# 7. SSEReader — LRU dedup across two streams + cloud.sse.dedup_drop summary
# ---------------------------------------------------------------------------


def test_pump_lru_dedup_across_two_streams_emits_single_envelope_and_summary(
    tmp_path: Path,
) -> None:
    stream_one = [
        b"event: assistant\ndata: {\"text\":\"a\"}\nid: 1\n\n",
        b"event: assistant\ndata: {\"text\":\"b\"}\nid: 2\n\n",
        b"event: assistant\ndata: {\"text\":\"c\"}\nid: 3\n\n",
    ]
    stream_two = [
        b"event: assistant\ndata: {\"text\":\"b'\"}\nid: 2\n\n",
        b"event: assistant\ndata: {\"text\":\"c'\"}\nid: 3\n\n",
        b"event: assistant\ndata: {\"text\":\"d\"}\nid: 4\n\n",
    ]
    handler = _stream_chunks_handler(chunks_per_call=[stream_one, stream_two])
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    reader = SSEReader(client, log, "task-A", "run-1", agent_id="agent-1")
    reader.pump()
    reader.pump()

    events = log.tail()
    types = _types(events)
    assistants = [e for e in events if e["type"] == "cloud.sse.assistant"]
    dedup_summaries = [e for e in events if e["type"] == "cloud.sse.dedup_drop"]

    assert len(assistants) == 4, types
    seen_ids = sorted([e["data"]["sse_id"] for e in assistants])
    assert seen_ids == ["1", "2", "3", "4"]

    assert len(dedup_summaries) >= 1
    total_dropped = sum(int(e["data"]["count"]) for e in dedup_summaries)
    assert total_dropped == 2
    assert any(e["data"]["first_id"] == "2" for e in dedup_summaries)
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 8. SSEReader — rejects StateStore-shaped collaborators (Q-A-8)
# ---------------------------------------------------------------------------


def test_sse_reader_rejects_state_store_shaped_collaborator(tmp_path: Path) -> None:
    """Q-A-8: passing a class named ``StateStore`` raises AssertionError/TypeError.

    We mimic the daemon-state contract by defining a local class also named
    ``StateStore`` — :class:`SSEReader` checks the *type name*, not the
    module path, so this is the right shape for the structural barrier.
    """

    class StateStore:  # noqa: N801 — intentionally mirrors daemon.state.StateStore
        def update(self, *args: Any, **kwargs: Any) -> None: ...
        def get(self, task_id: str) -> None: ...

    fake_store = StateStore()
    client = _make_client()
    log = _make_event_log(tmp_path)

    with pytest.raises((AssertionError, TypeError)) as exc_info:
        SSEReader(
            client=fake_store,  # type: ignore[arg-type]
            event_log=log,
            task_id="t",
            run_id="r",
            agent_id="a",
        )
    assert "StateStore" in str(exc_info.value) or "Q-A-8" in str(exc_info.value)

    with pytest.raises((AssertionError, TypeError)):
        SSEReader(
            client=client,
            event_log=fake_store,  # type: ignore[arg-type]
            task_id="t",
            run_id="r",
            agent_id="a",
        )
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 9. SSEReader — seq monotonicity per session (state-source-of-truth.md I-3)
# ---------------------------------------------------------------------------


def test_pump_seq_starts_at_zero_and_is_strictly_increasing(tmp_path: Path) -> None:
    chunks = [
        b"event: assistant\ndata: {\"text\":\"a\"}\nid: a-1\n\n",
        b"event: assistant\ndata: {\"text\":\"b\"}\nid: a-2\n\n",
        b"event: assistant\ndata: {\"text\":\"c\"}\nid: a-3\n\n",
        b"event: assistant\ndata: {\"text\":\"d\"}\nid: a-4\n\n",
        b"event: assistant\ndata: {\"text\":\"e\"}\nid: a-5\n\n",
    ]
    handler = _stream_chunks_handler(chunks_per_call=[chunks])
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    reader = SSEReader(client, log, "task-S", "run-1", agent_id="agent-1")
    reader.pump()

    events = log.tail()
    assistants = [e for e in events if e["type"] == "cloud.sse.assistant"]
    assert len(assistants) == 5
    seqs = [e["data"]["seq"] for e in assistants]
    assert seqs == [0, 1, 2, 3, 4]
    sids = [e["data"]["stream_session_id"] for e in assistants]
    assert len(set(sids)) == 1, "stream_session_id must be stable per pump session"
    assert all(e["data"]["task_id"] == "task-S" for e in assistants)
    assert all(e["data"]["run_id"] == "run-1" for e in assistants)
    assert all(e["data"]["agent_id"] == "agent-1" for e in assistants)
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 10. SSEReader — parse_error path: malformed JSON → cloud.sse.parse_error
# ---------------------------------------------------------------------------


def test_pump_emits_parse_error_for_malformed_json_and_continues(tmp_path: Path) -> None:
    chunks = [
        b"event: assistant\ndata: {not valid json\nid: pe-1\n\n",
        b"event: assistant\ndata: {\"text\":\"after-error\"}\nid: pe-2\n\n",
    ]
    handler = _stream_chunks_handler(chunks_per_call=[chunks])
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    reader = SSEReader(client, log, "task-PE", "run-1", agent_id="agent-1")
    reader.pump()

    events = log.tail()
    types = _types(events)
    assert "cloud.sse.parse_error" in types
    assert types.count("cloud.sse.assistant") == 1, types

    parse_errors = [e for e in events if e["type"] == "cloud.sse.parse_error"]
    pe = parse_errors[0]
    assert pe["data"]["sse_id"] == "pe-1"
    assert "raw_chunk_b64" in pe["data"]
    assert pe["data"]["error_type"] in {"JSONDecodeError", "ValueError"}

    asst = next(e for e in events if e["type"] == "cloud.sse.assistant")
    assert asst["data"]["sse_id"] == "pe-2"
    assert asst["data"]["payload"]["text"] == "after-error"
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 11. SSEReader — stream_expired emits cloud.sse.stream_expired (OQ-6)
# ---------------------------------------------------------------------------


def test_pump_on_stream_expired_emits_envelope_and_returns(tmp_path: Path) -> None:
    handler = _stream_chunks_handler(
        chunks_per_call=[[]],
        status_per_call=[410],
        json_per_call=[{"error": {"code": "stream_expired", "message": "expired"}}],
    )
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    reader = SSEReader(client, log, "task-X", "run-1", agent_id="agent-1")
    reader.pump()

    events = log.tail()
    types = _types(events)
    assert types == ["cloud.sse.stream_expired"]
    se = events[0]
    assert se["data"]["task_id"] == "task-X"
    assert se["data"]["run_id"] == "run-1"
    assert se["data"]["agent_id"] == "agent-1"
    assert se["data"]["reason"] == "stream_expired"
    assert se["data"]["seq"] == 0
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 12. SSEReader — heartbeat (no id) is emitted, dedup is skipped, seq advances
# ---------------------------------------------------------------------------


def test_pump_heartbeats_without_id_are_emitted_with_advancing_seq(tmp_path: Path) -> None:
    chunks = [
        b"event: heartbeat\ndata: {}\n\n",
        b"event: heartbeat\ndata: {}\n\n",
        b"event: heartbeat\ndata: {}\n\n",
    ]
    handler = _stream_chunks_handler(chunks_per_call=[chunks])
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    reader = SSEReader(client, log, "task-HB", "run-1", agent_id="agent-1")
    reader.pump()

    events = log.tail()
    heartbeats = [e for e in events if e["type"] == "cloud.sse.heartbeat"]
    assert len(heartbeats) == 3
    seqs = [e["data"]["seq"] for e in heartbeats]
    assert seqs == [0, 1, 2]
    assert all(e["data"]["sse_id"] is None for e in heartbeats)
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 13. SSEReader — terminal_hint() returns a public threading.Event handle
# ---------------------------------------------------------------------------


def test_terminal_hint_is_a_threading_event_handle(tmp_path: Path) -> None:
    import threading

    client = _make_client()
    log = _make_event_log(tmp_path)
    reader = SSEReader(client, log, "task-T", "run-1", agent_id="agent-1")
    hint = reader.terminal_hint
    assert isinstance(hint, threading.Event)
    assert hint.is_set() is False
    hint.set()
    assert reader.terminal_hint.is_set() is True
    assert reader.terminal_hint is hint
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 14. SSEReader — last_event_id advances; envelope quintuple is present
# ---------------------------------------------------------------------------


def test_last_event_id_advances_and_envelope_carries_quintuple(tmp_path: Path) -> None:
    chunks = [
        b"event: assistant\ndata: {\"text\":\"x\"}\nid: q-1\n\n",
        b"event: assistant\ndata: {\"text\":\"y\"}\nid: q-2\n\n",
        b"event: heartbeat\ndata: {}\n\n",
    ]
    handler = _stream_chunks_handler(chunks_per_call=[chunks])
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    reader = SSEReader(client, log, "task-Q", "run-Q", agent_id="agent-Q")
    assert reader.last_event_id is None
    reader.pump()
    assert reader.last_event_id == "q-2", "must track last non-None sse_id seen"

    events = log.tail()
    for ev in events:
        d = ev["data"]
        for key in ("task_id", "run_id", "stream_session_id", "sse_id", "seq"):
            assert key in d, f"{ev['type']} envelope missing {key!r}"
        assert d["task_id"] == "task-Q"
        assert d["run_id"] == "run-Q"
        assert d["agent_id"] == "agent-Q"
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 15. SSEReader — stop_event causes a clean exit mid-pump
# ---------------------------------------------------------------------------


def test_pump_clean_exits_on_stop_event(tmp_path: Path) -> None:
    import threading

    chunks = [
        b"event: assistant\ndata: {\"text\":\"a\"}\nid: s-1\n\n",
        b"event: assistant\ndata: {\"text\":\"b\"}\nid: s-2\n\n",
        b"event: assistant\ndata: {\"text\":\"c\"}\nid: s-3\n\n",
    ]
    handler = _stream_chunks_handler(chunks_per_call=[chunks])
    client = _make_client()
    _attach_mock_transport(client, handler)
    log = _make_event_log(tmp_path)

    stop = threading.Event()
    stop.set()  # pre-set: pump should bail out after the first iter step at most
    reader = SSEReader(client, log, "task-S", "run-1", agent_id="agent-1")
    reader.pump(stop_event=stop)

    events = log.tail()
    assistants = [e for e in events if e["type"] == "cloud.sse.assistant"]
    assert len(assistants) == 0
    log.close()
    client.close()


# ---------------------------------------------------------------------------
# 16. iter_events — empty data lines yield empty dict (heartbeat + done {})
# ---------------------------------------------------------------------------


def test_iter_events_empty_data_dict_for_no_data_lines() -> None:
    parsed = list(iter_events(["event: done", "id: end-1", ""]))
    assert parsed == [("done", {}, "end-1")]
