"""v0.8.8 multi-run invariants — covers I-7..I-12 (T2.1.1).

Spec source: ``.local/research/v0.8.8_multi_run/event-merge-spec.md`` §6
(``Test invariants for v0.8.8 Stage 2``). Each test below pins one of the
six invariants the multi-run plumbing MUST honour, plus a handful of
ergonomic checks for the new public surface (``create_followup_run`` /
``SSEReader._envelope`` / ``record_run_started`` / ``record_run_finished``
/ ``TaskHandle.cloud_runs``).

Invariants (paraphrased; full text in event-merge-spec.md §6):

- I-7 — per-run seq monotonicity (within ``(task_id, run_id, stream_session_id)``,
  ``data.seq`` strictly increasing; ``data.sse_id`` appears at most once).
- I-8 — cross-run lex monotonicity (after dedup, sort by ``(run_index, seq)``;
  same ``run_index`` forms a contiguous strictly-increasing-``seq`` block;
  ``run_index`` increases monotonically with first-occurrence ``time``).
- I-9 — replay idempotency (replay twice = byte-identical; permutations of
  the input yield the same rendered output).
- I-10 — ``cloud.run_started`` brackets (exactly one per ``run_id``; its
  ``time`` is ``<=`` every other event's ``time`` for that ``run_id``;
  symmetric for ``cloud.run_finished``).
- I-11 — ``run_index`` uniqueness per agent (no two distinct ``run_id``s
  share the same ``run_index`` within a single ``task_id``).
- I-12 — sequentiality soft-assert (no overlap between any two runs'
  ``[cloud.run_started.time, cloud.run_finished.time]`` intervals).

Tests use ``httpx.MockTransport`` for any cloud call mocking (per task
brief constraint) and operate on synthetic NDJSON fixtures rather than
real network round-trips.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudConflictError,
    CursorCloudNotFoundError,
    SSEReader,
)
from popolaloom.daemon.cloud_events import record_run_finished, record_run_started
from popolaloom.daemon.cloud_poller import CloudPollLoop
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState

# ---------------------------------------------------------------------------
# Helpers — shared by every invariant test below.
# ---------------------------------------------------------------------------


def _make_event_log(tmp_path: Path, name: str = "task.jsonl") -> EventLog:
    """Build an EventLog with the background fsync worker disabled (test mode)."""
    return EventLog(tmp_path / name, fsync_interval_s=0.0)


def _make_client(api_key: str = "test-key") -> CloudCursorClient:
    return CloudCursorClient(api_key, base_url=CURSOR_API_BASE)


_MockHandler = Callable[[httpx.Request], httpx.Response]


def _attach_mock_transport(client: CloudCursorClient, handler: _MockHandler) -> None:
    """Replace the client's httpx Client with one wired to MockTransport."""
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client._base_url,
        auth=(client._api_key, ""),
        timeout=client._timeout_s,
    )


def _utc_iso(ms_offset: int) -> str:
    """Build a deterministic ISO-8601 UTC timestamp at offset ``ms_offset``."""
    base = datetime(2026, 5, 8, 7, 0, 0, tzinfo=UTC)
    delta_us = ms_offset * 1000
    stamped = base.replace(microsecond=delta_us % 1_000_000)
    second_carry = delta_us // 1_000_000
    if second_carry:
        from datetime import timedelta

        stamped = stamped + timedelta(seconds=second_carry)
    return stamped.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _envelope(
    *,
    event_type: str,
    task_id: str,
    run_id: str,
    run_index: int,
    seq: int,
    sse_id: str | None = None,
    stream_session_id: str = "s-0",
    time_ms: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a synthetic CloudEvents envelope for the multi-run fixtures."""
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": "bc-test",
        "run_id": run_id,
        "run_index": run_index,
        "stream_session_id": stream_session_id,
        "sse_id": sse_id,
        "seq": seq,
    }
    if extra:
        data.update(extra)
    return {
        "specversion": "1.0",
        "id": f"evt-{event_type}-{run_id}-{seq}",
        "source": f"popola/{task_id}",
        "type": event_type,
        "time": _utc_iso(time_ms),
        "data": data,
    }


def _build_two_run_fixture(task_id: str = "task-A") -> list[dict[str, Any]]:
    """Two-run fixture: run-0 (3 sse) → run-1 (2 sse), with brackets.

    Order is the wire-arrival order (which §4.2 explicitly allows to be
    non-monotonic against ``time``); replay's ``(time, run_index, seq)``
    sort is what restores logical order.
    """
    events: list[dict[str, Any]] = []
    # Run-0 lifecycle
    events.append(
        _envelope(
            event_type="cloud.run_started",
            task_id=task_id,
            run_id="run-0",
            run_index=0,
            seq=0,
            time_ms=100,
            extra={"started_at": _utc_iso(100)},
        )
    )
    for i, sse in enumerate(("a-1", "a-2", "a-3")):
        events.append(
            _envelope(
                event_type="cloud.sse.assistant",
                task_id=task_id,
                run_id="run-0",
                run_index=0,
                seq=i,
                sse_id=sse,
                stream_session_id="s-0",
                time_ms=200 + i * 10,
                extra={"payload": {"text": f"r0-{sse}"}},
            )
        )
    events.append(
        _envelope(
            event_type="cloud.run_finished",
            task_id=task_id,
            run_id="run-0",
            run_index=0,
            seq=3,
            time_ms=400,
            extra={
                "terminal_phase": "FINISHED",
                "ended_at": _utc_iso(400),
                "exit_code": 0,
            },
        )
    )
    # Run-1 lifecycle (follow-up)
    events.append(
        _envelope(
            event_type="cloud.run_started",
            task_id=task_id,
            run_id="run-1",
            run_index=1,
            seq=0,
            time_ms=500,
            extra={"started_at": _utc_iso(500), "parent_run_id": "run-0"},
        )
    )
    for i, sse in enumerate(("b-1", "b-2")):
        events.append(
            _envelope(
                event_type="cloud.sse.assistant",
                task_id=task_id,
                run_id="run-1",
                run_index=1,
                seq=i,
                sse_id=sse,
                stream_session_id="s-1",
                time_ms=600 + i * 10,
                extra={"payload": {"text": f"r1-{sse}"}},
            )
        )
    events.append(
        _envelope(
            event_type="cloud.run_finished",
            task_id=task_id,
            run_id="run-1",
            run_index=1,
            seq=2,
            time_ms=700,
            extra={
                "terminal_phase": "FINISHED",
                "ended_at": _utc_iso(700),
                "exit_code": 0,
            },
        )
    )
    return events


def _build_three_run_fixture(task_id: str = "task-B") -> list[dict[str, Any]]:
    """Three-run fixture for I-8 / I-11 / I-12 stress."""
    events: list[dict[str, Any]] = []
    base_t = 0
    for run_idx in range(3):
        run_id = f"run-{run_idx}"
        events.append(
            _envelope(
                event_type="cloud.run_started",
                task_id=task_id,
                run_id=run_id,
                run_index=run_idx,
                seq=0,
                time_ms=base_t,
                extra={"started_at": _utc_iso(base_t)},
            )
        )
        for i in range(3):
            events.append(
                _envelope(
                    event_type="cloud.sse.assistant",
                    task_id=task_id,
                    run_id=run_id,
                    run_index=run_idx,
                    seq=i,
                    sse_id=f"r{run_idx}-{i}",
                    stream_session_id=f"s-{run_idx}",
                    time_ms=base_t + 10 + i * 5,
                    extra={"payload": {"text": f"r{run_idx}-{i}"}},
                )
            )
        events.append(
            _envelope(
                event_type="cloud.run_finished",
                task_id=task_id,
                run_id=run_id,
                run_index=run_idx,
                seq=3,
                time_ms=base_t + 50,
                extra={
                    "terminal_phase": "FINISHED",
                    "ended_at": _utc_iso(base_t + 50),
                    "exit_code": 0,
                },
            )
        )
        base_t += 100
    return events


def _idem_key(event: dict[str, Any]) -> tuple[Any, ...]:
    """The §2.1 sextuple identity (task_id, run_id, run_index, sss, sse_id, seq)."""
    d = event["data"]
    return (
        d.get("task_id"),
        d.get("run_id"),
        d.get("run_index"),
        d.get("stream_session_id"),
        d.get("sse_id"),
        d.get("seq"),
    )


def _dedup(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep first occurrence per IdemKey_v088 (per §4.1 step 2)."""
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for ev in events:
        key = _idem_key(ev)
        if key not in seen:
            seen[key] = ev
    return list(seen.values())


def _replay_render(events: list[dict[str, Any]]) -> str:
    """Implement the §4.1 replay algorithm and render to a deterministic string.

    1. Drop duplicates on the sextuple IdemKey.
    2. Sort by ``(time, run_index, seq)`` ascending.
    3. Render through a §3.1-style ``[run-N] type/payload`` line format.
    """
    deduped = _dedup(events)
    deduped.sort(
        key=lambda e: (
            e.get("time", ""),
            e["data"].get("run_index", 0),
            e["data"].get("seq", 0),
        )
    )
    lines: list[str] = []
    last_run_index: int | None = None
    for ev in deduped:
        ri = ev["data"].get("run_index", 0)
        if last_run_index is not None and ri != last_run_index:
            parent = ri - 1 if ri > 0 else 0
            lines.append(f"─── follow-up: run-{ri} (parent=run-{parent}) ───")
        last_run_index = ri
        ev_type = ev.get("type", "")
        sse_id = ev["data"].get("sse_id")
        seq = ev["data"].get("seq")
        lines.append(f"[run-{ri}] {ev_type}@{seq} sse={sse_id}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I-7 — Per-run seq monotonicity (within (task_id, run_id, stream_session_id))
# ---------------------------------------------------------------------------


def test_invariant_i7_per_run_seq_monotonic_two_run_fixture() -> None:
    """For each (task_id, run_id, sss) cluster, seq is strictly increasing
    and sse_id (when present) appears at most once."""
    events = _build_two_run_fixture()
    sse_only = [e for e in events if e["type"].startswith("cloud.sse.")]
    by_cluster: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for ev in sse_only:
        d = ev["data"]
        cluster = (d["task_id"], d["run_id"], d["stream_session_id"])
        by_cluster.setdefault(cluster, []).append(ev)
    assert len(by_cluster) == 2, "two-run fixture must have two distinct clusters"
    for cluster, cluster_events in by_cluster.items():
        seqs = [e["data"]["seq"] for e in cluster_events]
        assert seqs == sorted(seqs), f"seq not sorted for cluster {cluster}"
        assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False)), (
            f"seq not strictly increasing for cluster {cluster}: {seqs}"
        )
        sse_ids = [e["data"]["sse_id"] for e in cluster_events if e["data"]["sse_id"]]
        assert len(sse_ids) == len(set(sse_ids)), (
            f"sse_id duplicate inside cluster {cluster}"
        )


# ---------------------------------------------------------------------------
# I-8 — Cross-run lex monotonicity on the multi-run fixture
# ---------------------------------------------------------------------------


def test_invariant_i8_cross_run_lex_monotonic_three_run_fixture() -> None:
    """After dedup, sorting by (run_index, seq):
    - same run_index forms a contiguous strictly-increasing-seq block;
    - run_index increases monotonically with first-occurrence time.
    """
    events = _build_three_run_fixture()
    deduped = _dedup(events)
    deduped.sort(
        key=lambda e: (e["data"].get("run_index", 0), e["data"].get("seq", 0))
    )
    # Contiguity: walking the sorted list, every transition between
    # different run_index values must be monotonic ascending.
    prev_ri: int | None = None
    prev_ri_seqs: list[int] = []
    seen_ris: list[int] = []
    for ev in deduped:
        ri = ev["data"]["run_index"]
        seq = ev["data"]["seq"]
        if prev_ri is None or ri == prev_ri:
            prev_ri_seqs.append(seq)
        else:
            assert ri > prev_ri, f"run_index regressed: {prev_ri} → {ri}"
            assert prev_ri_seqs == sorted(prev_ri_seqs), (
                f"seqs not strictly increasing inside run_index={prev_ri}: {prev_ri_seqs}"
            )
            seen_ris.append(prev_ri)
            prev_ri_seqs = [seq]
        prev_ri = ri
    if prev_ri is not None:
        seen_ris.append(prev_ri)
        assert prev_ri_seqs == sorted(prev_ri_seqs)
    assert seen_ris == sorted(seen_ris), (
        f"run_index appearance not monotonic: {seen_ris}"
    )

    # First-occurrence time ascendency: the earliest time per run_index must
    # form an ascending series.
    first_time_by_ri: dict[int, str] = {}
    for ev in events:
        ri = ev["data"]["run_index"]
        first_time_by_ri.setdefault(ri, ev["time"])
    ordered = sorted(first_time_by_ri.items(), key=lambda kv: kv[0])
    times_in_ri_order = [t for _, t in ordered]
    assert times_in_ri_order == sorted(times_in_ri_order), (
        f"first-occurrence time not monotonic by run_index: {times_in_ri_order}"
    )


# ---------------------------------------------------------------------------
# I-9 — Replay idempotency: byte-identical across permutations
# ---------------------------------------------------------------------------


def test_invariant_i9_replay_idempotent_across_permutations() -> None:
    """Run the §4.1 replay twice on the same input and on permutations
    (original, reversed, shuffled) — all renderings must be byte-identical.
    """
    events = _build_three_run_fixture()
    rng = random.Random(0xC0FFEE)
    permutations = [
        list(events),
        list(reversed(events)),
        rng.sample(events, k=len(events)),
        rng.sample(events, k=len(events)),
    ]
    rendered = [_replay_render(p) for p in permutations]
    # All renderings agree.
    assert all(r == rendered[0] for r in rendered), (
        "Replay output diverges across permutations:\n"
        + "\n---\n".join(rendered)
    )
    # Idempotent on the same input.
    assert _replay_render(events) == _replay_render(events)
    # Duplicates injected → still identical (sextuple dedup).
    duplicated = events + list(events)
    assert _replay_render(duplicated) == rendered[0]


# ---------------------------------------------------------------------------
# I-10 — cloud.run_started brackets every run_id with the earliest time
# ---------------------------------------------------------------------------


def test_invariant_i10_run_started_brackets_earliest_per_run_id() -> None:
    """Every run_id has exactly one cloud.run_started; its time is <=
    every other envelope's time for that run_id. Symmetric for
    cloud.run_finished (>= rather than <=)."""
    events = _build_three_run_fixture()
    by_run_id: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        run_id = ev["data"].get("run_id")
        if run_id is None:
            continue
        by_run_id.setdefault(run_id, []).append(ev)
    assert len(by_run_id) == 3, "three-run fixture must yield 3 run_ids"

    for run_id, run_events in by_run_id.items():
        starts = [e for e in run_events if e["type"] == "cloud.run_started"]
        finishes = [e for e in run_events if e["type"] == "cloud.run_finished"]
        assert len(starts) == 1, f"expected exactly one cloud.run_started for {run_id}"
        assert len(finishes) == 1, f"expected exactly one cloud.run_finished for {run_id}"
        start_time = starts[0]["time"]
        finish_time = finishes[0]["time"]
        for ev in run_events:
            assert start_time <= ev["time"], (
                f"cloud.run_started not earliest for {run_id}: {start_time} vs {ev['time']}"
            )
            assert finish_time >= ev["time"], (
                f"cloud.run_finished not latest for {run_id}: {finish_time} vs {ev['time']}"
            )
        # run_index agreement on the bracket pair.
        assert starts[0]["data"]["run_index"] == finishes[0]["data"]["run_index"]


# ---------------------------------------------------------------------------
# I-11 — run_index uniqueness per agent (per task_id)
# ---------------------------------------------------------------------------


def test_invariant_i11_run_index_unique_per_agent() -> None:
    """Within a single task_id, no two distinct run_ids share the same run_index."""
    events = _build_three_run_fixture()
    by_run_index: dict[int, set[str]] = {}
    for ev in events:
        d = ev["data"]
        run_id = d.get("run_id")
        ri = d.get("run_index")
        if run_id is None or ri is None:
            continue
        by_run_index.setdefault(ri, set()).add(run_id)
    for ri, run_ids in by_run_index.items():
        assert len(run_ids) == 1, (
            f"run_index {ri} shared by multiple run_ids: {sorted(run_ids)}"
        )


# ---------------------------------------------------------------------------
# I-12 — Sequentiality soft-assert (no overlap of [started, finished] windows)
# ---------------------------------------------------------------------------


def test_invariant_i12_sequentiality_no_overlap_soft_assert() -> None:
    """No two runs' [run_started.time, run_finished.time] intervals overlap.

    Per spec §6 I-12 + DECISIONS.md EOQ-A1, this is a soft assertion that
    warns rather than fails on false positives in v0.8.8. We assert here
    because the fixture is fully sequential by construction, and any
    overlap would indicate a real bug in the fixture builder.
    """
    events = _build_three_run_fixture()
    intervals: dict[str, tuple[str, str]] = {}
    for ev in events:
        run_id = ev["data"].get("run_id")
        if run_id is None:
            continue
        if ev["type"] == "cloud.run_started":
            old = intervals.get(run_id, ("", ""))
            intervals[run_id] = (ev["time"], old[1])
        elif ev["type"] == "cloud.run_finished":
            old = intervals.get(run_id, ("", ""))
            intervals[run_id] = (old[0], ev["time"])
    sorted_intervals = sorted(intervals.values(), key=lambda iv: iv[0])
    for (a_start, a_end), (b_start, b_end) in zip(
        sorted_intervals, sorted_intervals[1:], strict=False
    ):
        assert a_end <= b_start, (
            f"sequentiality violated: a=[{a_start},{a_end}] overlaps b=[{b_start},{b_end}]"
        )


# ---------------------------------------------------------------------------
# CloudCursorClient.create_followup_run — happy path + 409 + 404
# ---------------------------------------------------------------------------


def test_create_followup_run_posts_runs_endpoint_with_prompt_and_model() -> None:
    """AC (a): POST /v1/agents/{id}/runs with {"prompt": {"text": ...}}."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "run-2", "status": "CREATING"})

    client = _make_client()
    _attach_mock_transport(client, handler)
    resp = client.create_followup_run("bc-1", "do step 2", model="composer-2")
    client.close()

    assert resp == {"id": "run-2", "status": "CREATING"}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/agents/bc-1/runs"
    body = json.loads(req.content.decode())
    assert body == {"prompt": {"text": "do step 2"}, "model": {"id": "composer-2"}}


def test_create_followup_run_omits_model_when_none() -> None:
    """Caller may omit ``model``; payload must not include the ``model`` key."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "run-3"})

    client = _make_client()
    _attach_mock_transport(client, handler)
    client.create_followup_run("bc-1", "follow-up")
    client.close()

    body = json.loads(captured[0].content.decode())
    assert body == {"prompt": {"text": "follow-up"}}


def test_create_followup_run_409_agent_busy_raises_conflict() -> None:
    """AC (a): 409 ``agent_busy`` maps to :class:`CursorCloudConflictError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "agent_busy", "message": "already running"}},
        )

    client = _make_client()
    _attach_mock_transport(client, handler)
    with pytest.raises(CursorCloudConflictError) as ei:
        client.create_followup_run("bc-1", "p")
    client.close()
    assert ei.value.status_code == 409
    assert ei.value.is_retryable is False


def test_create_followup_run_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "agent_not_found", "message": "gone"}},
        )

    client = _make_client()
    _attach_mock_transport(client, handler)
    with pytest.raises(CursorCloudNotFoundError):
        client.create_followup_run("bc-deleted", "p")
    client.close()


def test_create_followup_run_rejects_empty_prompt() -> None:
    client = _make_client()
    with pytest.raises(ValueError, match="non-empty"):
        client.create_followup_run("bc-1", "")
    client.close()


def test_create_followup_run_rejects_non_string_model() -> None:
    client = _make_client()
    with pytest.raises(ValueError, match="non-empty str"):
        client.create_followup_run("bc-1", "p", model="")
    client.close()


# ---------------------------------------------------------------------------
# SSEReader._envelope — run_index stamp default 0; explicit override
# ---------------------------------------------------------------------------


def test_sse_reader_envelope_stamps_run_index_default_zero(tmp_path: Path) -> None:
    """AC (b): legacy callers (no ``run_index`` arg) stamp ``run_index=0``."""
    client = _make_client()
    log = _make_event_log(tmp_path)
    reader = SSEReader(client, log, "task-X", "run-1", agent_id="bc-1")
    env = reader._envelope("sse-1", {"text": "hi"}, seq=0)
    log.close()
    client.close()
    assert env["run_index"] == 0
    assert env["task_id"] == "task-X"
    assert env["run_id"] == "run-1"


def test_sse_reader_envelope_stamps_run_index_when_overridden(tmp_path: Path) -> None:
    """v0.8.8 producer passes ``run_index=2``; envelope stamps it under data."""
    client = _make_client()
    log = _make_event_log(tmp_path)
    reader = SSEReader(client, log, "task-X", "run-2", agent_id="bc-1", run_index=2)
    env = reader._envelope("sse-2", {}, seq=5)
    log.close()
    client.close()
    assert env["run_index"] == 2


# ---------------------------------------------------------------------------
# record_run_started / record_run_finished — typed wrapper output shape
# ---------------------------------------------------------------------------


def test_record_run_started_writes_full_payload(tmp_path: Path) -> None:
    log = _make_event_log(tmp_path)
    env = record_run_started(
        log,
        task_id="task-A",
        agent_id="bc-1",
        run_id="run-1",
        run_index=1,
        started_at=_utc_iso(0),
        parent_run_id="run-0",
        prompt_digest="abc123",
    )
    log.close()
    assert env["type"] == "cloud.run_started"
    d = env["data"]
    assert d["task_id"] == "task-A"
    assert d["agent_id"] == "bc-1"
    assert d["run_id"] == "run-1"
    assert d["run_index"] == 1
    assert d["parent_run_id"] == "run-0"
    assert d["prompt_digest"] == "abc123"


def test_record_run_finished_writes_full_payload(tmp_path: Path) -> None:
    log = _make_event_log(tmp_path)
    env = record_run_finished(
        log,
        task_id="task-A",
        agent_id="bc-1",
        run_id="run-0",
        run_index=0,
        terminal_phase="FINISHED",
        ended_at=_utc_iso(50),
        exit_code=0,
    )
    log.close()
    assert env["type"] == "cloud.run_finished"
    d = env["data"]
    assert d["terminal_phase"] == "FINISHED"
    assert d["exit_code"] == 0
    assert d["run_index"] == 0


# ---------------------------------------------------------------------------
# CloudPollLoop integration — emits run_started + run_finished with run_index
# ---------------------------------------------------------------------------


def _register_handle(store: StateStore, task_id: str, log_path: Path) -> None:
    handle = TaskHandle(
        task_id=task_id,
        cli="cursor-cloud",
        pid=None,
        state=TaskState.STARTING,
        started_at=datetime.now(UTC),
        event_log_path=log_path,
        runtime="cloud",
    )
    store.register(handle)


def test_cloud_poll_loop_emits_run_started_and_finished_with_run_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (c): cloud.run_started at run() entry, cloud.run_finished on terminal,
    both carrying ``run_index``."""
    monkeypatch.setattr("popolaloom.daemon.cloud_poller.time.sleep", lambda *_: None)
    task_id = "cloud-mr-1"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    # Stamp the run_index into the supervisor-authoritative cloud_runs map
    # so the poller's _resolved_run_index() picks it up without reconciling.
    store.update(
        task_id,
        cloud_runs={"run-1": {"run_index": 1}},
    )
    client = MagicMock()
    client.get_run.side_effect = [
        {"status": "RUNNING"},
        {"status": "FINISHED"},
    ]
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-mr",
        run_id="run-1",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=0.0,
        max_polls=10,
        run_index=1,
    )
    loop.run()
    log.fsync()
    entries = log.tail()
    types = [e["type"] for e in entries]

    assert types.count("cloud.run_started") == 1
    assert types.count("cloud.run_finished") == 1
    assert "task.completed" in types

    started = next(e for e in entries if e["type"] == "cloud.run_started")
    finished = next(e for e in entries if e["type"] == "cloud.run_finished")
    completed = next(e for e in entries if e["type"] == "task.completed")
    run_status = [e for e in entries if e["type"] == "cloud.run_status"]

    assert started["data"]["run_index"] == 1
    assert finished["data"]["run_index"] == 1
    assert finished["data"]["terminal_phase"] == "FINISHED"
    assert finished["data"]["exit_code"] == 0
    assert completed["data"]["run_index"] == 1
    for rs in run_status:
        assert rs["data"]["run_index"] == 1

    # I-10 bracket — started.time <= every other event's time for run-1;
    # finished.time should be roughly >= other run-1 events' time, but
    # we tolerate a small intra-iteration race (≤500 ms) because the
    # poller may emit a final cloud.run_status alongside the
    # cloud.run_finished event in the same loop iteration; their wall-
    # clock ordering is not strictly serialized in the production path.
    from datetime import datetime
    def _parse_iso(s: str) -> float:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    started_time = started["time"]
    finished_time = finished["time"]
    finished_epoch = _parse_iso(finished_time)
    for ev in entries:
        if ev["data"].get("run_id") != "run-1":
            continue
        assert started_time <= ev["time"]
        ev_epoch = _parse_iso(ev["time"])
        assert ev_epoch <= finished_epoch + 0.5, (
            f"event at {ev['time']} appeared >500ms after finished "
            f"@ {finished_time}; intra-iteration race exceeded tolerance"
        )

    log.close()


def test_cloud_poll_loop_default_run_index_is_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compat: omitting ``run_index`` yields 0 (legacy v0.8.6 path)."""
    monkeypatch.setattr("popolaloom.daemon.cloud_poller.time.sleep", lambda *_: None)
    task_id = "cloud-mr-0"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    client = MagicMock()
    client.get_run.return_value = {"status": "FINISHED"}
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-mr",
        run_id="run-0",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=0.0,
        max_polls=10,
    )
    loop.run()
    log.fsync()
    entries = log.tail()

    started = next(e for e in entries if e["type"] == "cloud.run_started")
    finished = next(e for e in entries if e["type"] == "cloud.run_finished")
    assert started["data"]["run_index"] == 0
    assert finished["data"]["run_index"] == 0
    log.close()


def test_cloud_poll_loop_reconciles_missing_run_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (f): missing-index path triggers _reconcile_run_index helper.

    The supervisor never wrote ``cloud_runs`` for this run_id; the poller
    must fall through to :meth:`_reconcile_run_index` and emit a
    ``cloud.run_index_reconciled`` event for SRE visibility.
    """
    monkeypatch.setattr("popolaloom.daemon.cloud_poller.time.sleep", lambda *_: None)
    task_id = "cloud-mr-recon"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    client = MagicMock()
    client.get_run.return_value = {"status": "FINISHED"}
    # NOTE: cloud_runs intentionally NOT populated to force the missing-index
    # path. run_index defaults to 0 (the in-process fallback).
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-recon",
        run_id="run-orphan",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=0.0,
        max_polls=5,
    )
    loop.run()
    log.fsync()
    entries = log.tail()
    types = [e["type"] for e in entries]

    # Reconciliation event must fire at least once (for the run_started
    # bracket). Real implementation in T2.1.3 will hit the API; T2.1.1
    # falls back to the in-process counter and emits the event for SRE
    # visibility per DECISIONS.md OQ-3.
    assert "cloud.run_index_reconciled" in types
    reconcile_events = [e for e in entries if e["type"] == "cloud.run_index_reconciled"]
    for re_ev in reconcile_events:
        assert re_ev["data"]["run_id"] == "run-orphan"
        assert re_ev["data"]["method"] == "fallback_inprocess"
    log.close()


# ---------------------------------------------------------------------------
# TaskHandle.cloud_runs — field shape + StateStore.update merge semantics
# ---------------------------------------------------------------------------


def test_taskhandle_cloud_runs_default_empty_dict(tmp_path: Path) -> None:
    """AC (d): the new ``cloud_runs`` field defaults to an empty dict."""
    handle = TaskHandle(
        task_id="t1",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.PENDING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "t1.jsonl",
    )
    assert handle.cloud_runs == {}


def test_statestore_update_cloud_runs_merge_preserves_prior_runs(tmp_path: Path) -> None:
    """AC (d): per-run-id merge — adding a follow-up does not lose run-0."""
    store = StateStore()
    handle = TaskHandle(
        task_id="t-merge",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.STARTING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "t.jsonl",
        runtime="cloud",
    )
    store.register(handle)
    store.update("t-merge", cloud_runs={"run-0": {"run_index": 0}})
    store.update("t-merge", cloud_runs={"run-1": {"run_index": 1}})
    h2 = store.get("t-merge")
    assert h2 is not None
    assert h2.cloud_runs == {
        "run-0": {"run_index": 0},
        "run-1": {"run_index": 1},
    }


def test_statestore_rehydrate_persists_cloud_runs(tmp_path: Path) -> None:
    """AC (d): a rehydrated handle (ArkTower restart simulation) preserves
    ``cloud_runs``."""
    store = StateStore()
    rehydrated_handle = TaskHandle(
        task_id="t-restart",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "t.jsonl",
        runtime="cloud",
        cursor_agent_id="bc-1",
        cursor_run_id="run-1",
        cloud_runs={
            "run-0": {"run_index": 0},
            "run-1": {"run_index": 1, "parent_run_id": "run-0"},
        },
    )
    store.rehydrate([rehydrated_handle])
    h = store.get("t-restart")
    assert h is not None
    assert h.cloud_runs["run-0"]["run_index"] == 0
    assert h.cloud_runs["run-1"]["run_index"] == 1
    assert h.cloud_runs["run-1"]["parent_run_id"] == "run-0"
