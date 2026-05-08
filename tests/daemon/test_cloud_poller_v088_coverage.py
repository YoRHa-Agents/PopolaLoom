"""v0.8.8 T4.1 — coverage backfill for ``popolaloom.daemon.cloud_poller``.

Lifts ``daemon/cloud_poller.py`` from 82 % to ≥ 90 % by exercising the
v0.8.8 T2.2.2 ``PendingDispatchQueue`` / ``PendingDispatchDrainer``
surfaces that :file:`test_busy_queue.py` did not yet cover:

- ``PendingDispatchQueue.remove_dispatch`` happy path / no-such-agent /
  no-such-entry / cleanup-on-empty branches.
- ``PendingDispatchQueue.list_dispatches`` / ``snapshot`` / ``__len__``.
- ``PendingDispatchDrainer.start`` is idempotent; ``stop`` joins.
- ``_run_forever`` exits when the stop-event fires (one-tick loop).
- ``_tick_agent`` peek-head returns None / phase=None (transient) / non-terminal
  / dispatch-invoker raises / event-log-resolver returns None paths.
- ``_handle_timeout`` event-log raises (logged, swallowed) path.
- ``_handle_dispatch`` event-log raises (logged, swallowed) path.
- ``CloudPollLoop._poll_run_body`` non-retryable failure / retry-exhausted.
- ``_resolved_run_index`` reconciliation path (handle missing /
  cloud_runs map missing).
- ``_phase_from_run_body`` invalid body / missing status.

Each test is short (≤ 20 lines), uses pluggable callbacks for the drainer.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from popolaloom.adapters.cursor_cloud import CursorCloudError
from popolaloom.daemon.cloud_poller import (
    CloudPollLoop,
    PendingDispatch,
    PendingDispatchDrainer,
    PendingDispatchQueue,
    _phase_from_run_body,
    _utc_iso,
)
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.main import BusyStrategyConfig
from popolaloom.daemon.state import StateStore, TaskState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "task.jsonl", fsync_interval_s=0.0)
    yield log
    log.close()


def _make_drainer(
    queue: PendingDispatchQueue,
    *,
    config: BusyStrategyConfig | None = None,
    poll_phase: Any = None,
    dispatch: Any = None,
    event_log_resolver: Any = None,
    clock: Any = None,
    sleep: Any = None,
) -> PendingDispatchDrainer:
    cfg = config if config is not None else BusyStrategyConfig()
    return PendingDispatchDrainer(
        queue=queue,
        config=cfg,
        poll_phase=poll_phase or (lambda _agent, _run: "RUNNING"),
        dispatch=dispatch or (lambda _entry: {"id": "run-default"}),
        event_log_resolver=event_log_resolver or (lambda _task: None),
        sleep=sleep or (lambda _s: None),
        clock=clock or (lambda: 0.0),
    )


# ---------------------------------------------------------------------------
# Queue mutation paths
# ---------------------------------------------------------------------------


def test_queue_remove_dispatch_returns_false_for_unknown_agent() -> None:
    """``remove_dispatch`` on a never-enqueued agent returns ``False``."""
    q = PendingDispatchQueue()
    d = PendingDispatch(
        task_id="t", agent_id="bc-missing",
        payload={}, current_run_id="r",
    )
    assert q.remove_dispatch(d) is False


def test_queue_remove_dispatch_returns_false_for_unknown_entry() -> None:
    """``remove_dispatch`` for an entry not in queue returns ``False``."""
    q = PendingDispatchQueue()
    d_a = PendingDispatch(
        task_id="t1", agent_id="bc-A", payload={}, current_run_id="r1",
    )
    d_b = PendingDispatch(
        task_id="t2", agent_id="bc-A", payload={}, current_run_id="r2",
    )
    q.enqueue(d_a)
    assert q.remove_dispatch(d_b) is False


def test_queue_remove_dispatch_succeeds_and_cleans_empty() -> None:
    """``remove_dispatch`` succeeds and cleans the empty agent deque."""
    q = PendingDispatchQueue()
    d = PendingDispatch(
        task_id="t", agent_id="bc-A", payload={}, current_run_id="r",
    )
    q.enqueue(d)
    assert q.remove_dispatch(d) is True
    assert q.agents() == []


def test_queue_remove_dispatch_keeps_agent_when_others_remain() -> None:
    """Removing one of two leaves the agent's deque intact."""
    q = PendingDispatchQueue()
    d_a = PendingDispatch(
        task_id="t1", agent_id="bc-A", payload={}, current_run_id="r1",
    )
    d_b = PendingDispatch(
        task_id="t2", agent_id="bc-A", payload={}, current_run_id="r2",
    )
    q.enqueue(d_a)
    q.enqueue(d_b)
    assert q.remove_dispatch(d_a) is True
    assert q.peek_head("bc-A") is d_b


def test_queue_list_dispatches_returns_empty_for_unknown_agent() -> None:
    """``list_dispatches`` for an unknown agent returns ``[]``."""
    q = PendingDispatchQueue()
    assert q.list_dispatches("missing") == []


def test_queue_list_dispatches_returns_snapshot_copy() -> None:
    """``list_dispatches`` returns a copy that does not mutate the queue."""
    q = PendingDispatchQueue()
    d = PendingDispatch(
        task_id="t", agent_id="bc-A", payload={}, current_run_id="r",
    )
    q.enqueue(d)
    snap = q.list_dispatches("bc-A")
    snap.append("bogus")
    assert len(q.list_dispatches("bc-A")) == 1


def test_queue_snapshot_returns_full_dict() -> None:
    """``snapshot`` returns ``{agent_id: [dispatches]}`` for all agents."""
    q = PendingDispatchQueue()
    d_a = PendingDispatch(
        task_id="ta", agent_id="bc-A", payload={}, current_run_id="r",
    )
    d_b = PendingDispatch(
        task_id="tb", agent_id="bc-B", payload={}, current_run_id="r",
    )
    q.enqueue(d_a)
    q.enqueue(d_b)
    snap = q.snapshot()
    assert sorted(snap.keys()) == ["bc-A", "bc-B"]


def test_queue_pop_head_unknown_agent_returns_none() -> None:
    """``pop_head`` for an unknown agent returns ``None``."""
    q = PendingDispatchQueue()
    assert q.pop_head("bc-missing") is None


def test_queue_peek_head_unknown_agent_returns_none() -> None:
    """``peek_head`` for an unknown agent returns ``None``."""
    q = PendingDispatchQueue()
    assert q.peek_head("bc-missing") is None


def test_queue_len_zero_when_empty() -> None:
    """``len(q) == 0`` when no enqueues."""
    q = PendingDispatchQueue()
    assert len(q) == 0


# ---------------------------------------------------------------------------
# Drainer happy-path edge branches
# ---------------------------------------------------------------------------


def test_drainer_tick_no_op_for_empty_agent_list() -> None:
    """Ticking with an empty queue does nothing (early return)."""
    q = PendingDispatchQueue()
    drainer = _make_drainer(q)
    drainer.tick()  # no exception, no events


def test_drainer_tick_skips_non_terminal_phase() -> None:
    """``poll_phase`` returning ``RUNNING`` leaves the entry in place."""
    q = PendingDispatchQueue()
    poll_calls: list[tuple[str, str]] = []

    def fake_poll(agent_id: str, run_id: str) -> str:
        poll_calls.append((agent_id, run_id))
        return "RUNNING"

    drainer = _make_drainer(q, poll_phase=fake_poll)
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-A",
        payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert len(q) == 1
    assert poll_calls == [("bc-A", "r-prev")]


def test_drainer_tick_phase_none_keeps_head() -> None:
    """``poll_phase`` returning ``None`` (transient) leaves head intact."""
    q = PendingDispatchQueue()

    def fake_poll(_agent: str, _run: str) -> None:
        return None  # transient error

    drainer = _make_drainer(q, poll_phase=fake_poll)
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-A", payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert len(q) == 1


def test_drainer_dispatch_invoker_exception_drops_entry(
    event_log: EventLog, caplog: pytest.LogCaptureFixture,
) -> None:
    """A :class:`Exception` from the dispatch invoker is logged + entry dropped."""
    q = PendingDispatchQueue()

    def boom(_entry: PendingDispatch) -> dict[str, Any]:
        raise RuntimeError("dispatch failure")

    drainer = _make_drainer(
        q,
        poll_phase=lambda _a, _r: "FINISHED",
        dispatch=boom,
        event_log_resolver=lambda _t: event_log,
    )
    drainer.enqueue_dispatch(
        task_id="t-fail", agent_id="bc-X",
        payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert len(q) == 0
    assert "dispatch failed" in caplog.text


def test_drainer_dispatch_no_event_log_skips_emission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``event_log_resolver`` returns ``None``, no event is recorded."""
    q = PendingDispatchQueue()
    drainer = _make_drainer(
        q,
        poll_phase=lambda _a, _r: "FINISHED",
        dispatch=lambda _e: {"id": "r-new"},
        event_log_resolver=lambda _t: None,
    )
    drainer.enqueue_dispatch(
        task_id="t-noevent", agent_id="bc-A",
        payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert len(q) == 0


def test_drainer_per_entry_on_dispatch_overrides_default(
    event_log: EventLog,
) -> None:
    """Per-entry ``on_dispatch`` callback wins over the drainer's default."""
    q = PendingDispatchQueue()

    overrides: list[PendingDispatch] = []

    def custom(entry: PendingDispatch) -> dict[str, Any]:
        overrides.append(entry)
        return {"id": "custom-r"}

    def default(_entry: PendingDispatch) -> dict[str, Any]:
        raise AssertionError("default must NOT be called when on_dispatch is set")

    drainer = _make_drainer(
        q,
        poll_phase=lambda _a, _r: "FINISHED",
        dispatch=default,
        event_log_resolver=lambda _t: event_log,
    )
    drainer.enqueue_dispatch(
        task_id="t-cust", agent_id="bc-A",
        payload={}, current_run_id="r-prev",
        on_dispatch=custom,
    )
    drainer.tick()
    assert len(overrides) == 1


def test_drainer_dispatch_event_log_raises_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError raising from ``record_busy_dispatched`` is logged + swallowed."""
    q = PendingDispatchQueue()
    bad_log = MagicMock()
    bad_log.append.side_effect = OSError("disk full")
    drainer = _make_drainer(
        q,
        poll_phase=lambda _a, _r: "FINISHED",
        dispatch=lambda _e: {"id": "r-new"},
        event_log_resolver=lambda _t: bad_log,
    )
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-A",
        payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert "cloud.busy_dispatched emit failed" in caplog.text


def test_drainer_timeout_event_log_raises_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError raising from ``record_busy_timeout`` is logged + swallowed."""
    q = PendingDispatchQueue()
    call_count = {"n": 0}

    def flaky_log_resolver(_t: str) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # enqueue path: no log
        bad_log = MagicMock()
        bad_log.append.side_effect = OSError("disk full")
        return bad_log

    clocks = iter([100.0, 5000.0, 5001.0])
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_max_wait_s=300, queue_poll_interval_s=5),
        poll_phase=lambda _a, _r: "RUNNING",
        event_log_resolver=flaky_log_resolver,
        clock=lambda: next(clocks),
    )
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-A", payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert "cloud.busy_timeout emit failed" in caplog.text


def test_drainer_enqueue_event_log_raises_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError from the enqueue's ``cloud.busy_queued`` is logged."""
    q = PendingDispatchQueue()
    bad_log = MagicMock()
    bad_log.append.side_effect = OSError("disk full")
    drainer = _make_drainer(
        q, event_log_resolver=lambda _t: bad_log,
    )
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-A", payload={}, current_run_id="r-prev",
    )
    assert "cloud.busy_queued emit failed" in caplog.text


def test_drainer_start_is_idempotent_when_alive() -> None:
    """A second ``start`` returns the same alive thread."""
    q = PendingDispatchQueue()
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_poll_interval_s=10),
        sleep=lambda _s: None,
    )
    t1 = drainer.start()
    t2 = drainer.start()
    try:
        assert t1 is t2
        assert t1.is_alive()
    finally:
        drainer.stop(timeout=1.0)


def test_drainer_stop_works_when_never_started() -> None:
    """``stop`` on a never-started drainer is a no-op (no error)."""
    q = PendingDispatchQueue()
    drainer = _make_drainer(q)
    drainer.stop(timeout=0.1)


def test_drainer_run_forever_exits_on_stop_event() -> None:
    """The internal ``_run_forever`` loop terminates when ``_stop_event`` fires."""
    q = PendingDispatchQueue()
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_poll_interval_s=10),
    )
    thread = drainer.start()
    drainer.stop(timeout=2.0)
    assert not thread.is_alive()


def test_drainer_outer_tick_swallows_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raise from ``poll_phase`` does not crash the agent isolation."""
    q = PendingDispatchQueue()

    def boom_poll(_a: str, _r: str) -> str:
        raise RuntimeError("transient")

    drainer = _make_drainer(q, poll_phase=boom_poll)
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-X", payload={}, current_run_id="r",
    )
    drainer.tick()  # MUST NOT raise
    assert "PendingDispatchDrainer tick failed" in caplog.text


# ---------------------------------------------------------------------------
# CloudPollLoop helper coverage
# ---------------------------------------------------------------------------


def test_cloud_poll_loop_resolved_run_index_handle_missing(
    tmp_path: Path,
) -> None:
    """When the state store has no handle, returns the loop's ``run_index``."""
    log = EventLog(tmp_path / "tx.jsonl", fsync_interval_s=0.0)
    state = StateStore()
    loop = CloudPollLoop(
        task_id="t-X",
        agent_id="bc",
        run_id="r",
        client=MagicMock(),
        state_store=state,
        event_log=log,
        on_exit=None,
        run_index=7,
    )
    try:
        assert loop._resolved_run_index() == 7
    finally:
        log.close()


def test_cloud_poll_loop_resolved_run_index_no_run_meta(
    tmp_path: Path,
) -> None:
    """Handle present but ``cloud_runs`` empty → reconcile fallback emits event."""
    log = EventLog(tmp_path / "ty.jsonl", fsync_interval_s=0.0)
    state = StateStore()
    from popolaloom.daemon.state import TaskHandle

    handle = TaskHandle(
        task_id="t-Y",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "ty.jsonl",
        runtime="cloud",
        cloud_runs={},
    )
    state.register(handle)
    loop = CloudPollLoop(
        task_id="t-Y",
        agent_id="bc",
        run_id="r-untracked",
        client=MagicMock(),
        state_store=state,
        event_log=log,
        on_exit=None,
        run_index=3,
    )
    try:
        assert loop._resolved_run_index() == 3
        log.fsync()
        events = [e for e in log.tail() if e["type"] == "cloud.run_index_reconciled"]
        assert len(events) == 1
    finally:
        log.close()


def test_cloud_poll_loop_resolved_run_index_uses_cached(
    tmp_path: Path,
) -> None:
    """When ``cloud_runs[run_id]`` has cached ``run_index``, no reconcile fires."""
    log = EventLog(tmp_path / "tz.jsonl", fsync_interval_s=0.0)
    state = StateStore()
    from popolaloom.daemon.state import TaskHandle

    handle = TaskHandle(
        task_id="t-Z",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "tz.jsonl",
        runtime="cloud",
        cloud_runs={"r-known": {"run_index": 5}},
    )
    state.register(handle)
    loop = CloudPollLoop(
        task_id="t-Z",
        agent_id="bc",
        run_id="r-known",
        client=MagicMock(),
        state_store=state,
        event_log=log,
        on_exit=None,
        run_index=99,
    )
    try:
        assert loop._resolved_run_index() == 5
    finally:
        log.close()


def test_cloud_poll_loop_poll_run_body_non_retryable(
    tmp_path: Path,
) -> None:
    """A non-retryable :class:`CursorCloudError` from ``get_run`` propagates immediately."""
    log = EventLog(tmp_path / "tn.jsonl", fsync_interval_s=0.0)
    state = StateStore()
    client = MagicMock()
    client.get_run.side_effect = CursorCloudError(
        "401", status_code=401, is_retryable=False,
    )
    loop = CloudPollLoop(
        task_id="t",
        agent_id="bc",
        run_id="r",
        client=client,
        state_store=state,
        event_log=log,
        on_exit=None,
        retry_max=3,
    )
    try:
        with pytest.raises(CursorCloudError):
            loop._poll_run_body()
    finally:
        log.close()


def test_cloud_poll_loop_poll_run_body_retry_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retryable error that exhausts ``retry_max`` propagates the last exception."""
    monkeypatch.setattr("popolaloom.daemon.cloud_poller.time.sleep", lambda _s: None)
    log = EventLog(tmp_path / "te.jsonl", fsync_interval_s=0.0)
    state = StateStore()
    client = MagicMock()
    client.get_run.side_effect = CursorCloudError(
        "503", status_code=503, is_retryable=True,
    )
    loop = CloudPollLoop(
        task_id="t",
        agent_id="bc",
        run_id="r",
        client=client,
        state_store=state,
        event_log=log,
        on_exit=None,
        retry_max=2,
    )
    try:
        with pytest.raises(CursorCloudError):
            loop._poll_run_body()
        assert client.get_run.call_count == 2
    finally:
        log.close()


def test_phase_from_run_body_handles_non_dict() -> None:
    """``_phase_from_run_body`` returns ``""`` for non-dict input."""
    assert _phase_from_run_body(None) == ""
    assert _phase_from_run_body([1, 2, 3]) == ""


def test_phase_from_run_body_missing_status() -> None:
    """``_phase_from_run_body`` returns ``""`` when ``status`` key is absent."""
    assert _phase_from_run_body({}) == ""


def test_phase_from_run_body_lower_status_is_uppercased() -> None:
    """``_phase_from_run_body`` upper-cases the returned phase."""
    assert _phase_from_run_body({"status": "running"}) == "RUNNING"


def test_utc_iso_naive_datetime_promoted_to_utc() -> None:
    """``_utc_iso`` adds UTC tzinfo to naive inputs (defensive)."""
    dt = datetime(2026, 5, 8, 10, 0, 0)  # naive
    out = _utc_iso(dt)
    assert out.endswith("Z")


def test_safe_on_exit_callback_none_is_noop() -> None:
    """``_safe_on_exit`` with ``callback=None`` is a no-op."""
    from popolaloom.daemon.cloud_poller import _safe_on_exit
    _safe_on_exit(None, "t", 0)


def test_safe_on_exit_callback_exception_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_safe_on_exit`` swallows + logs callback exceptions."""
    from popolaloom.daemon.cloud_poller import _safe_on_exit

    def boom(_t: str, _e: int) -> None:
        raise RuntimeError("boom")

    _safe_on_exit(boom, "t", 1)
    assert "on_exit callback failed" in caplog.text


def test_drainer_tick_pop_head_race_returns_silently() -> None:
    """``_handle_dispatch`` bails when the head pops out from under it."""
    q = PendingDispatchQueue()

    def racy_poll(_a: str, _r: str) -> str:
        # Race: another thread pops the head between peek and dispatch.
        # We simulate this by emptying the queue inside the poll callback.
        if q.agents():
            q.pop_head(q.agents()[0])
        return "FINISHED"

    drainer = _make_drainer(
        q,
        poll_phase=racy_poll,
        dispatch=lambda _e: {"id": "should-not-fire"},
    )
    drainer.enqueue_dispatch(
        task_id="t", agent_id="bc-X", payload={}, current_run_id="r-prev",
    )
    drainer.tick()
    assert len(q) == 0


def test_drainer_extract_new_run_id_handles_empty_legacy() -> None:
    """Legacy ``run.id`` shape with empty string falls back to ``""``."""
    assert PendingDispatchDrainer._extract_new_run_id({"run": {"id": ""}}) == ""


def test_drainer_extract_new_run_id_legacy_run_no_id() -> None:
    """Legacy ``run`` dict without ``id`` falls back to ``""``."""
    assert PendingDispatchDrainer._extract_new_run_id({"run": {}}) == ""


def test_drainer_extract_new_run_id_non_dict_response() -> None:
    """Non-dict response → ``""``."""
    assert PendingDispatchDrainer._extract_new_run_id("not a dict") == ""


# ---------------------------------------------------------------------------
# CloudPollLoop end-to-end terminal phase happy paths
# ---------------------------------------------------------------------------


def test_cloud_poll_loop_run_terminal_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal ``FINISHED`` → emits ``task.completed`` + run-bracket events."""
    monkeypatch.setattr("popolaloom.daemon.cloud_poller.time.sleep", lambda _s: None)
    log = EventLog(tmp_path / "tterm.jsonl", fsync_interval_s=0.0)
    state = StateStore()
    from popolaloom.daemon.state import TaskHandle

    handle = TaskHandle(
        task_id="t-OK",
        cli="cursor-cloud",
        pid=None,
        state=TaskState.RUNNING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "tterm.jsonl",
        runtime="cloud",
    )
    state.register(handle)
    client = MagicMock()
    client.get_run.return_value = {"status": "FINISHED"}
    loop = CloudPollLoop(
        task_id="t-OK",
        agent_id="bc",
        run_id="r",
        client=client,
        state_store=state,
        event_log=log,
        on_exit=None,
        interval_s=0.001,
        max_polls=3,
    )
    try:
        loop.run()
        log.fsync()
        types = [e["type"] for e in log.tail()]
        assert "task.completed" in types
    finally:
        log.close()
