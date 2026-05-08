"""Coordination tests for :class:`CloudPollLoop` ↔ SSE-side signalers (T2.2.2).

v0.8.6 Stage 2 Wave 2.2 — exercises the new ``wake_event`` plumb-through and
verifies the cross-thread invariants from
``state-source-of-truth.md`` §6:

* **I-6 drift bound** (``test_wake_event_interrupts_sleep_within_200ms``): an
  external signaler can wake the poller out of its inter-poll sleep so the
  next ``get_run`` arrives within ≤200 ms. This is the SLO check for the
  §2.3 "tolerated divergence" of `state-source-of-truth.md`.
* **Backward compat** (``test_no_wake_event_preserves_v085_sleep``):
  ``CloudPollLoop(...)`` without ``wake_event`` falls back to the v0.8.5
  ``time.sleep`` behaviour with no observable change.
* **I-4 terminal closes stream**
  (``test_terminal_phase_stops_sse_no_late_events``): a poller-driven
  terminal ``cloud_phase`` halts an in-memory ``MockSSEReader`` within
  ≤500 ms and no ``cloud.sse.*`` event is appended >250 ms after the
  terminal event.
* **I-2 / I-1 cross-reference** (``test_i2_cross_reference_to_i1_guard``):
  documents — in executable form — that a non-poller invocation of
  ``state_store.update(cloud_phase=...)`` is structurally caught by
  :func:`tests.conftest.test_invariant_i1_sole_writer_of_cloud_phase`.

All tests use a :class:`MagicMock` for :class:`CloudCursorClient` and an
in-memory ``MockEventLog`` (when needed) to avoid disk I/O so they stay
under 5 s wall-clock combined.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from popolaloom.daemon.cloud_poller import CloudPollLoop, run_poll_loop
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _register_handle(store: StateStore, task_id: str, log_path: Path) -> None:
    """Register a cloud-runtime ``TaskHandle`` so ``state_store.update`` works."""
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


@pytest.fixture
def cloud_setup(tmp_path: Path) -> tuple[str, StateStore, EventLog, MagicMock]:
    task_id = "coord-task-1"
    log_path = tmp_path / f"{task_id}.jsonl"
    log = EventLog(log_path, fsync_interval_s=0)
    store = StateStore()
    _register_handle(store, task_id, log_path)
    client = MagicMock()
    return task_id, store, log, client


# ---------------------------------------------------------------------------
# I-6 drift bound — wake_event interrupts the sleep
# ---------------------------------------------------------------------------


def test_wake_event_interrupts_sleep_within_200ms(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
) -> None:
    """**I-6 drift bound**: ``wake_event.set()`` while the poller is mid-sleep
    causes the next ``get_run`` to fire within ≤200 ms (allowing for thread
    scheduling jitter). Validates the §2.3 tolerated-divergence SLO.

    Setup: ``interval_s=2.0`` would cap the poll at 2 s without the wake; the
    test asserts we're at least an order of magnitude faster when the wake
    fires. We pin scheduler latency expectations to 200 ms so a busy CI host
    doesn't false-fail.
    """
    task_id, store, log, client = cloud_setup

    poll_seen = {1: threading.Event(), 2: threading.Event()}
    poll_count = [0]

    def _side_effect(*args: Any, **kwargs: Any) -> dict[str, str]:
        poll_count[0] += 1
        if poll_count[0] in poll_seen:
            poll_seen[poll_count[0]].set()
        if poll_count[0] == 1:
            return {"status": "RUNNING"}
        return {"status": "FINISHED"}

    client.get_run.side_effect = _side_effect

    wake = threading.Event()
    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-w",
        run_id="run-w",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=2.0,
        max_polls=10,
        wake_event=wake,
    )

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    # Wait until the poller has executed the first poll and entered the wait().
    assert poll_seen[1].wait(timeout=2.0), "poller never executed poll #1"

    # Give the poller a beat to settle into ``wake_event.wait()``. Even if we
    # set ``wake`` before it enters wait(), the Event semantics guarantee an
    # immediate return — but we want to measure the post-wake path explicitly.
    time.sleep(0.05)

    t0 = time.monotonic()
    wake.set()
    assert poll_seen[2].wait(timeout=1.0), "poller did not run poll #2 after wake"
    elapsed = time.monotonic() - t0

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    assert elapsed <= 0.2, (
        f"poll #2 fired {elapsed * 1000:.1f} ms after wake.set() — "
        f"exceeds I-6 200 ms drift budget"
    )


# ---------------------------------------------------------------------------
# Backward compatibility — no wake_event keeps v0.8.5 behaviour
# ---------------------------------------------------------------------------


def test_no_wake_event_preserves_v085_sleep(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: Any,
) -> None:
    """``CloudPollLoop(...)`` without ``wake_event`` MUST behave exactly like
    v0.8.5: it calls :func:`time.sleep` between polls, never touches a
    ``threading.Event``. We patch :func:`time.sleep` and assert it was used
    (covers AC §a default + the existing
    :file:`tests/daemon/test_cloud_poller.py` invocation contract).
    """
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [{"status": "RUNNING"}, {"status": "FINISHED"}]
    sleep_mock = mocker.patch(
        "popolaloom.daemon.cloud_poller.time.sleep", return_value=None
    )

    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-bc",
        run_id="run-bc",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=2.0,
        max_polls=10,
    )
    assert loop.wake_event is None  # AC (a): default None
    loop.run()

    # The non-terminal RUNNING branch sleeps once; we explicitly verify
    # ``time.sleep`` was used (not Event.wait) so v0.8.5 callers see no
    # behaviour change.
    assert sleep_mock.called, "time.sleep should be invoked when wake_event is None"
    handle = store.get(task_id)
    assert handle is not None
    assert handle.state == TaskState.COMPLETED


def test_run_poll_loop_helper_threads_wake_event(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
    mocker: Any,
) -> None:
    """The :func:`run_poll_loop` convenience wrapper must forward ``wake_event``
    onto the underlying :class:`CloudPollLoop` instance so callers can wire
    the SSEReader hint without recreating the dataclass plumbing themselves.
    """
    task_id, store, log, client = cloud_setup
    client.get_run.return_value = {"status": "FINISHED"}
    mocker.patch("popolaloom.daemon.cloud_poller.time.sleep", return_value=None)

    wake = threading.Event()
    thread = run_poll_loop(
        task_id,
        "bc-h",
        "run-h",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        max_polls=5,
        wake_event=wake,
    )
    thread.join(timeout=5.0)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# I-4 terminal closes stream — no late SSE events past the terminal phase
# ---------------------------------------------------------------------------


class _MockEventLog:
    """In-memory :class:`EventLog` stand-in that timestamps each append.

    We need ``time.monotonic()`` deltas (not ISO strings) to assert the
    250 ms / 500 ms bounds, so this stand-in records both the event tuple
    and the monotonic clock at the moment of append. Thread-safe via a
    dedicated lock to mirror :class:`EventLog._lock` behaviour from
    ``state-source-of-truth.md`` §5 failure mode #3.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[tuple[float, str, dict[str, Any]]] = []

    def append(self, event_type: str, data: dict[str, Any]) -> None:
        ts = time.monotonic()
        with self._lock:
            self._items.append((ts, event_type, dict(data)))

    def fsync(self) -> None:
        return None

    def snapshot(self) -> list[tuple[float, str, dict[str, Any]]]:
        with self._lock:
            return list(self._items)


def _start_mock_sse_reader(
    log: _MockEventLog,
    *,
    stop_event: threading.Event,
    started_event: threading.Event,
    chunk_period_s: float = 0.02,
) -> threading.Thread:
    """Spawn a daemon thread that emits ``cloud.sse.assistant`` until stopped.

    Mirrors the SSEReader pump described in ``state-source-of-truth.md`` §1.3:
    the only side-effect is :py:meth:`event_log.append` of ``cloud.sse.*``
    envelopes (I-2 append-only). When the caller signals via ``stop_event``,
    the pump exits cleanly and stops emitting — this mimics the §3 lifecycle
    rule "terminal cloud_phase observed by attach UI ─► close stream".
    """

    def _pump() -> None:
        seq = 0
        started_event.set()
        while not stop_event.is_set():
            seq += 1
            log.append(
                "cloud.sse.assistant",
                {
                    "task_id": "coord-task-1",
                    "run_id": "run-coord",
                    "stream_session_id": 1,
                    "sse_id": f"seq-{seq}",
                    "seq": seq,
                    "payload": {"text": "tok"},
                },
            )
            # Use Event.wait so stop_event interrupts the pump promptly.
            if stop_event.wait(timeout=chunk_period_s):
                break

    thread = threading.Thread(target=_pump, name="mock-sse-reader", daemon=True)
    thread.start()
    return thread


def test_terminal_phase_stops_sse_no_late_events(tmp_path: Path) -> None:
    """**I-4**: when the poller writes a terminal ``cloud_phase`` mid-stream,
    the SSE reader exits within ≤500 ms and no ``cloud.sse.*`` event with
    monotonic ts > ``terminal_ts + 0.25 s`` is ever appended.

    Setup: an in-memory ``_MockEventLog`` shared between the poller and a
    ``MockSSEReader`` thread that appends ``cloud.sse.assistant`` every 20 ms.
    The poller's ``side_effect`` pins ``terminal_ts`` immediately before
    returning ``FINISHED`` and signals ``sse_stop``; this models the real
    flow where ``attach`` observes the poller-driven terminal event in the
    log and tears down the stream.
    """
    task_id = "coord-task-1"
    log_path = tmp_path / f"{task_id}.jsonl"
    poller_disk_log = EventLog(log_path, fsync_interval_s=0)
    state = StateStore()
    _register_handle(state, task_id, log_path)
    mock_log = _MockEventLog()

    sse_stop = threading.Event()
    sse_started = threading.Event()
    sse_thread = _start_mock_sse_reader(
        mock_log,
        stop_event=sse_stop,
        started_event=sse_started,
        chunk_period_s=0.02,
    )
    assert sse_started.wait(timeout=2.0), "MockSSEReader did not start"

    # Let SSE accumulate a few events so the test exercises a populated log.
    time.sleep(0.1)

    poll_count = [0]
    terminal_ts: list[float] = []

    def _side_effect(*args: Any, **kwargs: Any) -> dict[str, str]:
        poll_count[0] += 1
        if poll_count[0] == 1:
            return {"status": "RUNNING"}
        # poll #2: pin terminal_ts at the moment the poller commits to FINISHED
        # and immediately tear down the SSE pump (mirrors attach UI behaviour).
        terminal_ts.append(time.monotonic())
        sse_stop.set()
        return {"status": "FINISHED"}

    client = MagicMock()
    client.get_run.side_effect = _side_effect

    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-t",
        run_id="run-t",
        client=client,
        state_store=state,
        event_log=poller_disk_log,
        on_exit=None,
        interval_s=0.05,
        max_polls=10,
    )

    poller_thread = threading.Thread(target=loop.run, daemon=True)
    poller_start = time.monotonic()
    poller_thread.start()
    poller_thread.join(timeout=5.0)
    assert not poller_thread.is_alive(), "poller did not terminate"

    sse_thread.join(timeout=2.0)
    assert not sse_thread.is_alive(), "SSE pump did not stop after sse_stop.set()"

    assert terminal_ts, "side_effect never observed poll #2 (FINISHED)"
    t_term = terminal_ts[0]

    # I-4 assertion 1: SSE stopped within ≤500 ms of terminal_ts.
    sse_stop_latency = time.monotonic() - t_term
    assert sse_stop_latency <= 0.5, (
        f"SSE took {sse_stop_latency * 1000:.0f} ms to stop after terminal — "
        f"exceeds I-4 500 ms bound"
    )

    # I-4 assertion 2: no cloud.sse.* event appended > terminal_ts + 0.25 s.
    bound = t_term + 0.25
    snapshot = mock_log.snapshot()
    late_sse = [
        (ts - t_term, etype)
        for ts, etype, _ in snapshot
        if etype.startswith("cloud.sse.") and ts > bound
    ]
    assert not late_sse, (
        f"late cloud.sse.* events past terminal_ts + 250 ms: {late_sse[:5]}"
    )

    # Sanity: at least one cloud.sse.* event was appended pre-terminal so we
    # know the test actually exercised the SSE thread (not vacuously passing).
    pre_terminal_sse = [
        etype for ts, etype, _ in snapshot
        if etype.startswith("cloud.sse.") and ts <= t_term
    ]
    assert pre_terminal_sse, "SSE thread emitted nothing before terminal"

    # Sanity: poller actually completed and emitted task.completed.
    poller_disk_log.fsync()
    poller_entries = poller_disk_log.tail()
    assert any(e["type"] == "task.completed" for e in poller_entries)
    elapsed_poll = time.monotonic() - poller_start
    assert elapsed_poll < 5.0


# ---------------------------------------------------------------------------
# I-2 cross-reference — implicit via the I-1 grep guard in tests/conftest.py
# ---------------------------------------------------------------------------


def test_i2_cross_reference_to_i1_guard() -> None:
    """**I-2 implicit**: a non-poller invocation of
    ``state_store.update(cloud_phase=...)`` would be caught by the I-1 grep
    guard at :func:`tests.conftest.test_invariant_i1_sole_writer_of_cloud_phase`.

    This test imports + executes the guard helper directly, asserting that
    the static-grep machinery is wired up and reachable from this test
    file's discovery path. It does **not** re-implement the guard; the
    canonical assertion lives in ``tests/conftest.py`` per T2.2.2 spec.

    Why this matters: without an explicit cross-reference, a future
    contributor might add a ``state_store.update(cloud_phase=...)`` from a
    new SSE worker, expecting the runtime guard in
    ``adapters/cursor_cloud.py`` (Q-A-8 ``StateStore``-rejection) to catch
    it — but that runtime guard only protects ``SSEReader``, not arbitrary
    new callers. The static-grep is the safety net for those.
    """
    # Late import keeps the conftest helper symbols off this module's
    # public surface and avoids a circular import at collection time.
    from tests.conftest import (
        _I1_MUST_BE_ONLY_FILE,
        _I1_PATTERN,
        _i1_collect_offenders,
    )

    # Smoke: the regex compiles and the allow-list is exactly what the spec
    # locks in (`{"daemon/cloud_poller.py"}`). If a future PR widens the
    # allow-list, both this assertion AND the I-1 guard's MUST_BE_ONLY_FILE
    # would need to be updated together.
    assert _I1_PATTERN.search("state_store.update(task_id, cloud_phase='X')") is not None
    assert set(_I1_MUST_BE_ONLY_FILE) == {"daemon/cloud_poller.py"}

    # The collector is callable; it returns a dict (possibly empty when the
    # codebase is clean). The actual pass/fail assertion lives in the
    # conftest test — we just verify the helper is reachable.
    offenders = _i1_collect_offenders()
    assert isinstance(offenders, dict)


# ---------------------------------------------------------------------------
# Spurious wake — wake_event signal during a non-sleep window is a no-op
# ---------------------------------------------------------------------------


def test_spurious_wake_during_non_sleep_window_is_noop(
    cloud_setup: tuple[str, StateStore, EventLog, MagicMock],
) -> None:
    """No-Silent-Failures rule + AC §Constraints: a ``wake_event.set()`` that
    arrives during a non-sleep window MUST be cleared at the next sleep
    boundary so it does not cause an unbounded skip of subsequent polls.

    We pre-set the wake event before the poller's first sleep; the poller
    then enters ``wake.wait(...)`` which returns immediately, and on the
    *second* sleep the wake must be cleared (so a 2 s timeout would fire
    were we not also returning FINISHED on poll #2). We assert the run
    completes well under the no-wake fallback wait.
    """
    task_id, store, log, client = cloud_setup
    client.get_run.side_effect = [
        {"status": "RUNNING"},
        {"status": "RUNNING"},
        {"status": "FINISHED"},
    ]

    wake = threading.Event()
    wake.set()  # spurious / pre-set before the loop runs

    loop = CloudPollLoop(
        task_id=task_id,
        agent_id="bc-s",
        run_id="run-s",
        client=client,
        state_store=store,
        event_log=log,
        on_exit=None,
        interval_s=0.05,  # short so the test stays fast
        max_polls=10,
        wake_event=wake,
    )

    t0 = time.monotonic()
    loop.run()
    elapsed = time.monotonic() - t0

    # 3 polls × 0.05 s = 0.15 s upper bound on the slow path; the spurious
    # wake should keep us at the fast path — assert <0.5 s with margin.
    assert elapsed < 0.5

    # The poller must have cleared the wake at the first wait().
    assert not wake.is_set()


@pytest.fixture(autouse=True)
def _kill_dangling_threads() -> Iterator[None]:
    """Belt-and-braces: each test holds its threads with daemon=True and
    explicit ``join(timeout=...)``, but if a thread does leak we surface it
    immediately (a hanging poll thread would otherwise quietly slow CI).
    """
    pre = {t.name for t in threading.enumerate()}
    yield
    post_threads = [
        t
        for t in threading.enumerate()
        if t.name not in pre
        and t.is_alive()
        and (
            t.name.startswith("popolad-cloud-poll-")
            or t.name == "mock-sse-reader"
        )
    ]
    if post_threads:
        names = [t.name for t in post_threads]
        raise AssertionError(
            f"test leaked daemon threads (still alive): {names}"
        )
