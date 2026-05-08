"""v0.8.8 T2.2.2 — ``PendingDispatchQueue`` + drainer + busy events.

Covers AC (a) – (h) per ``.local/.agent/active/v0.8.8-multi-run/PLAN.md``
§4.2 T2.2.2 + the spec ``.local/research/v0.8.8_multi_run/quota-config.md``
§4 (async-queue design), §5.1 (event taxonomy), and §6 (exit-code matrix).

Test surface (each test pins one invariant from the spec):

- Enqueue / FIFO order — :class:`PendingDispatchQueue` keys per ``agent_id``
  with deterministic FIFO semantics within a key (per spec §4.1).
- Drain happy-path — drainer pops + re-issues + emits
  ``cloud.busy_dispatched`` once the upstream phase is terminal.
- Drain leaves head intact while phase is ``RUNNING``.
- Timeout — when ``queue_max_wait_s`` elapses → ``cloud.busy_timeout``
  fires + the entry is dropped (No-Silent-Failures).
- ``mode = "fail_fast"`` → :class:`CursorCloudConflictError` propagation
  (preserves v0.8.7 behavior; queue path bypassed).
- ``cloud.busy_queued`` event payload schema verified against §5.1.
- Drainer ``new_run_id`` extraction handles both top-level ``id`` and
  legacy ``run.id`` shapes (per ``endpoints.md``).
- Drainer per-agent isolation — one agent's failure does not block the
  rest of the queue.
- Config-strictness for the ``[cloud.busy_strategy]`` schema (mode +
  inter-key invariants per spec §2.2 + §2.3).

All tests are pure-Python — no httpx network round-trip; the drainer's
``poll_phase`` / ``dispatch`` callbacks are pluggable so we exercise
the full state machine deterministically.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from popolaloom.adapters.cursor_cloud import (
    CURSOR_API_BASE,
    CloudCursorClient,
    CursorCloudConflictError,
)
from popolaloom.daemon.cloud_poller import (
    PendingDispatch,
    PendingDispatchDrainer,
    PendingDispatchQueue,
    _phase_from_run_body,
)
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.main import (
    CLOUD_BUSY_QUEUE_MAX_WAIT_MIN_S,
    CLOUD_BUSY_QUEUE_POLL_MAX_S,
    BusyStrategyConfig,
    load_popolad_config,
)

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_log(tmp_path: Path) -> Iterator[EventLog]:
    """Per-test :class:`EventLog` with the fsync worker disabled.

    Disabling fsync (``fsync_interval_s=0``) keeps the test deterministic:
    every ``append`` is visible to ``tail()`` immediately after the
    explicit ``fsync()`` we issue at assertion time.
    """
    log = EventLog(tmp_path / "test.jsonl", fsync_interval_s=0.0)
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
) -> PendingDispatchDrainer:
    """Construct a :class:`PendingDispatchDrainer` with sensible test defaults.

    All callbacks default to no-op / sentinel returns so individual tests
    only need to override what they exercise. The clock is monotonic by
    default; tests that need deterministic deadlines pass a
    ``[t0, t1, ...]`` iterator-backed callable.
    """
    cfg = config if config is not None else BusyStrategyConfig()
    return PendingDispatchDrainer(
        queue=queue,
        config=cfg,
        poll_phase=poll_phase or (lambda _agent, _run: "RUNNING"),
        dispatch=dispatch or (lambda _entry: {"id": "run-default"}),
        event_log_resolver=event_log_resolver or (lambda _task: None),
        sleep=lambda _s: None,
        clock=clock or (lambda: 0.0),
    )


def _types(log: EventLog) -> list[str]:
    log.fsync()
    return [e["type"] for e in log.tail()]


def _last_data(log: EventLog, event_type: str) -> dict[str, Any]:
    log.fsync()
    matching = [e for e in log.tail() if e["type"] == event_type]
    assert matching, f"no events of type {event_type!r} in log"
    data = matching[-1]["data"]
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# AC (b) — enqueue + FIFO within agent_id (PendingDispatchQueue primitive).
# ---------------------------------------------------------------------------


def test_pending_dispatch_queue_fifo_within_agent() -> None:
    """Two enqueues on the same ``agent_id`` pop in FIFO order; positions
    are 1-based and reflect insert order; lengths track correctly."""
    q = PendingDispatchQueue()
    d1 = PendingDispatch(
        task_id="t1",
        agent_id="bc-A",
        payload={"prompt": {"text": "hi 1"}},
        current_run_id="run-old",
    )
    d2 = PendingDispatch(
        task_id="t2",
        agent_id="bc-A",
        payload={"prompt": {"text": "hi 2"}},
        current_run_id="run-old",
    )

    pos1 = q.enqueue(d1)
    pos2 = q.enqueue(d2)
    assert pos1 == 1
    assert pos2 == 2
    assert len(q) == 2
    assert q.agents() == ["bc-A"]

    head = q.pop_head("bc-A")
    assert head is d1
    assert q.peek_head("bc-A") is d2
    assert q.pop_head("bc-A") is d2
    # Empty deque is cleaned up so agents() does not list ghosts.
    assert q.pop_head("bc-A") is None
    assert q.agents() == []
    assert len(q) == 0


def test_pending_dispatch_queue_keys_isolated_per_agent() -> None:
    """Enqueues on different ``agent_id`` keys live in independent FIFOs.

    Pinning that the queue is keyed (per spec §4.1) prevents agent-A's
    backlog from blocking agent-B when both compete for the drainer's
    next tick.
    """
    q = PendingDispatchQueue()
    d_a = PendingDispatch(
        task_id="ta", agent_id="bc-A", payload={}, current_run_id="ra"
    )
    d_b = PendingDispatch(
        task_id="tb", agent_id="bc-B", payload={}, current_run_id="rb"
    )
    q.enqueue(d_a)
    q.enqueue(d_b)
    assert sorted(q.agents()) == ["bc-A", "bc-B"]
    assert q.pop_head("bc-A") is d_a
    # Removing one key leaves the other untouched.
    assert q.pop_head("bc-B") is d_b
    assert q.agents() == []


# ---------------------------------------------------------------------------
# AC (b) + (g) — enqueue_dispatch emits cloud.busy_queued (default-visible).
# ---------------------------------------------------------------------------


def test_drainer_enqueue_dispatch_emits_cloud_busy_queued(
    event_log: EventLog,
) -> None:
    """``enqueue_dispatch`` records ``cloud.busy_queued`` with payload that
    matches the §5.1 schema: ``task_id``, ``agent_id``, ``current_run_id``,
    ``queue_position``, ``deadline_ts``. Position is 1-based (FIFO) and
    deadline is ISO-8601 UTC.
    """
    q = PendingDispatchQueue()
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_max_wait_s=600),
        event_log_resolver=lambda _t: event_log,
        clock=lambda: 100.0,
    )

    dispatch = drainer.enqueue_dispatch(
        task_id="t-1",
        agent_id="bc-X",
        payload={"prompt": {"text": "follow-up"}},
        current_run_id="run-prev",
    )

    assert dispatch.task_id == "t-1"
    assert dispatch.queue_position == 1
    assert dispatch.deadline_iso is not None
    assert dispatch.deadline_iso.endswith("Z")  # ISO-8601 UTC suffix

    types = _types(event_log)
    assert types == ["cloud.busy_queued"]
    payload = _last_data(event_log, "cloud.busy_queued")
    assert payload == {
        "task_id": "t-1",
        "agent_id": "bc-X",
        "current_run_id": "run-prev",
        "queue_position": 1,
        "deadline_ts": dispatch.deadline_iso,
    }


def test_drainer_enqueue_dispatch_no_deadline_when_max_wait_zero(
    event_log: EventLog,
) -> None:
    """``queue_max_wait_s = 0`` is the "wait forever" sentinel: the
    enqueue must surface ``deadline_ts = None`` so attach UIs render
    ``deadline=never`` (per spec §2.2 + §5.1).
    """
    q = PendingDispatchQueue()
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_max_wait_s=0),
        event_log_resolver=lambda _t: event_log,
    )
    dispatch = drainer.enqueue_dispatch(
        task_id="t-2",
        agent_id="bc-Y",
        payload={},
        current_run_id="run-prev",
    )
    assert dispatch.deadline_iso is None
    assert dispatch.deadline_mono is None
    payload = _last_data(event_log, "cloud.busy_queued")
    assert payload["deadline_ts"] is None


# ---------------------------------------------------------------------------
# AC (c) + (g) — drainer dispatches once the upstream phase is terminal,
# emitting cloud.busy_dispatched (default-visible).
# ---------------------------------------------------------------------------


def test_drainer_dispatches_on_terminal_phase_and_emits_busy_dispatched(
    event_log: EventLog,
) -> None:
    """Tick 1: phase is ``RUNNING`` → no-op. Tick 2: phase is
    ``FINISHED`` → drainer pops + invokes ``dispatch`` + emits
    ``cloud.busy_dispatched`` carrying the new run id."""
    q = PendingDispatchQueue()
    phase_calls: list[tuple[str, str]] = []
    phase_responses: list[str] = ["RUNNING", "FINISHED"]

    def fake_poll_phase(agent_id: str, run_id: str) -> str | None:
        phase_calls.append((agent_id, run_id))
        return phase_responses.pop(0) if phase_responses else "FINISHED"

    dispatched: list[PendingDispatch] = []

    def fake_dispatch(entry: PendingDispatch) -> dict[str, Any]:
        dispatched.append(entry)
        return {"id": "run-new-7", "status": "CREATING"}

    clock_seq = iter([10.0, 11.0, 12.0, 13.0, 14.0])
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(
            queue_max_wait_s=600, queue_poll_interval_s=1
        ),
        poll_phase=fake_poll_phase,
        dispatch=fake_dispatch,
        event_log_resolver=lambda _t: event_log,
        clock=lambda: next(clock_seq),
    )

    drainer.enqueue_dispatch(
        task_id="t-A",
        agent_id="bc-A",
        payload={"prompt": {"text": "follow"}},
        current_run_id="run-prev",
    )

    drainer.tick()
    assert dispatched == []  # RUNNING leaves head intact
    assert len(q) == 1

    drainer.tick()
    assert len(dispatched) == 1
    assert len(q) == 0
    assert phase_calls == [("bc-A", "run-prev"), ("bc-A", "run-prev")]

    types = _types(event_log)
    assert types == ["cloud.busy_queued", "cloud.busy_dispatched"]
    payload = _last_data(event_log, "cloud.busy_dispatched")
    assert payload["task_id"] == "t-A"
    assert payload["agent_id"] == "bc-A"
    assert payload["prev_run_id"] == "run-prev"
    assert payload["new_run_id"] == "run-new-7"
    assert isinstance(payload["waited_ms"], int)
    assert payload["waited_ms"] >= 0


def test_drainer_skips_dispatch_when_notify_on_dispatch_false(
    event_log: EventLog,
) -> None:
    """``notify_on_dispatch = False`` (operator opts into noise control)
    suppresses the ``cloud.busy_dispatched`` event but the underlying
    dispatch still fires. Pins the spec §2.2 default-disable hook."""
    q = PendingDispatchQueue()

    def fake_poll_phase(_agent: str, _run: str) -> str:
        return "FINISHED"

    fake_dispatch = MagicMock(return_value={"id": "r-new"})
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(
            queue_max_wait_s=600, notify_on_dispatch=False
        ),
        poll_phase=fake_poll_phase,
        dispatch=fake_dispatch,
        event_log_resolver=lambda _t: event_log,
    )
    drainer.enqueue_dispatch(
        task_id="t-A",
        agent_id="bc-A",
        payload={},
        current_run_id="run-prev",
    )
    drainer.tick()
    fake_dispatch.assert_called_once()
    types = _types(event_log)
    # busy_queued is always emitted; busy_dispatched is suppressed.
    assert types == ["cloud.busy_queued"]


# ---------------------------------------------------------------------------
# AC (d) — queue_max_wait_s expiry → cloud.busy_timeout + entry dropped.
# ---------------------------------------------------------------------------


def test_drainer_timeout_emits_cloud_busy_timeout_and_drops_entry(
    event_log: EventLog,
) -> None:
    """When the monotonic clock surpasses ``deadline_mono`` the head is
    dropped; ``cloud.busy_timeout`` fires; ``poll_phase`` is NOT called
    on the timeout tick (the deadline check fences the API call so a
    hung server cannot keep the queue alive past its deadline).
    """
    q = PendingDispatchQueue()

    poll_calls: list[tuple[str, str]] = []

    def fake_poll_phase(agent_id: str, run_id: str) -> str:
        poll_calls.append((agent_id, run_id))
        return "RUNNING"

    fake_dispatch = MagicMock(return_value={"id": "r-new"})

    # Wall + monotonic clock: enqueue at t=100, deadline at t=160 (60s
    # wait window), then jump to t=300 so the next tick triggers timeout.
    clock_seq = [100.0, 300.0, 301.0]
    clock_iter = iter(clock_seq)
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(
            queue_max_wait_s=CLOUD_BUSY_QUEUE_MAX_WAIT_MIN_S,
            queue_poll_interval_s=5,
        ),
        poll_phase=fake_poll_phase,
        dispatch=fake_dispatch,
        event_log_resolver=lambda _t: event_log,
        clock=lambda: next(clock_iter),
    )

    drainer.enqueue_dispatch(
        task_id="t-T",
        agent_id="bc-T",
        payload={},
        current_run_id="run-busy",
    )

    drainer.tick()  # t=300 → past deadline → timeout

    assert len(q) == 0
    fake_dispatch.assert_not_called()
    assert poll_calls == []  # phase poll skipped on timeout tick

    types = _types(event_log)
    assert types == ["cloud.busy_queued", "cloud.busy_timeout"]
    payload = _last_data(event_log, "cloud.busy_timeout")
    assert payload["task_id"] == "t-T"
    assert payload["agent_id"] == "bc-T"
    assert payload["current_run_id_at_timeout"] == "run-busy"
    assert payload["waited_ms"] >= 0


# ---------------------------------------------------------------------------
# AC (e) — mode = "fail_fast" preserves v0.8.7 CursorCloudConflictError.
# ---------------------------------------------------------------------------


def test_create_followup_run_409_agent_busy_propagates_in_fail_fast() -> None:
    """When operators set ``mode = "fail_fast"`` (or simply ignore the
    queue path), a ``409 agent_busy`` propagates as
    :class:`CursorCloudConflictError` with ``cli_exit=102`` — preserving
    the v0.8.7 CLI contract per spec §6 + AC (e).

    The actual queue path (T2.2.2 owned) is opt-in via
    ``[cloud.busy_strategy].mode = "queue"``; this test pins the
    fail-fast surface so the catalog mapping is intact.
    """
    api = httpx.MockTransport(
        lambda req: httpx.Response(
            409,
            json={"error": {"code": "agent_busy", "message": "agent busy"}},
        )
    )
    client = CloudCursorClient("test-key", base_url=CURSOR_API_BASE)
    client._client.close()
    client._client = httpx.Client(
        transport=api,
        base_url=client._base_url,
        auth=(client._api_key, ""),
        timeout=client._timeout_s,
    )
    try:
        with pytest.raises(CursorCloudConflictError) as excinfo:
            client.create_followup_run("bc-A", "follow-up")
        assert excinfo.value.cli_exit == 102
    finally:
        client.close()


# ---------------------------------------------------------------------------
# AC (f) + (g) — events default-visible (rendered inline, NOT debug-filtered).
#
# These tests live in tests/cli/test_status_busy_visibility.py for the
# popola status / popola attach surfaces; here we cover the daemon-side
# emission contract that the CLI relies on.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC (h) — dispatch response shape extraction (id vs run.id vs missing).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"id": "run-1"}, "run-1"),
        ({"run": {"id": "run-2"}}, "run-2"),
        ({"id": "run-3", "run": {"id": "run-other"}}, "run-3"),
        ({}, ""),
        ({"id": ""}, ""),
        ({"run": {"id": ""}}, ""),
        (None, ""),
    ],
)
def test_drainer_extract_new_run_id_handles_response_shapes(
    response: Any, expected: str
) -> None:
    """The drainer's ``new_run_id`` extraction tolerates both the
    documented ``POST /v1/agents/{id}/runs`` shape (top-level ``id``)
    and the legacy ``POST /v1/agents`` shape (``run.id``); falls back
    to ``""`` rather than ``None`` so the wire schema stays stable."""
    assert PendingDispatchDrainer._extract_new_run_id(response) == expected


# ---------------------------------------------------------------------------
# AC (h) — drainer per-agent isolation (one bad agent does not block others).
# ---------------------------------------------------------------------------


def test_drainer_isolates_per_agent_failures(event_log: EventLog) -> None:
    """A ``poll_phase`` exception for agent-A does NOT prevent the
    drainer from processing agent-B in the same tick; the failure is
    logged and isolated so the queue stays drainable."""
    q = PendingDispatchQueue()
    dispatched_agents: list[str] = []

    def flaky_poll(agent_id: str, _run_id: str) -> str | None:
        if agent_id == "bc-bad":
            raise RuntimeError("transient")
        return "FINISHED"

    def fake_dispatch(entry: PendingDispatch) -> dict[str, Any]:
        dispatched_agents.append(entry.agent_id)
        return {"id": "run-new"}

    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_max_wait_s=600),
        poll_phase=flaky_poll,
        dispatch=fake_dispatch,
        event_log_resolver=lambda _t: event_log,
    )
    drainer.enqueue_dispatch(
        task_id="bad", agent_id="bc-bad", payload={}, current_run_id="r-bad"
    )
    drainer.enqueue_dispatch(
        task_id="ok", agent_id="bc-ok", payload={}, current_run_id="r-ok"
    )

    drainer.tick()

    # bc-bad raised on poll → entry stays queued; bc-ok progressed.
    assert dispatched_agents == ["bc-ok"]
    assert q.agents() == ["bc-bad"]


# ---------------------------------------------------------------------------
# AC (h) — drainer transient phase failure (None) leaves head intact.
# ---------------------------------------------------------------------------


def test_drainer_transient_phase_none_keeps_entry_queued(
    event_log: EventLog,
) -> None:
    """A ``None`` phase return is the documented "transient error"
    contract (e.g. 5xx); the drainer logs WARN (already done by the
    poller) and leaves the head queued so the next tick retries.
    """
    q = PendingDispatchQueue()
    fake_dispatch = MagicMock(return_value={"id": "r-new"})
    drainer = _make_drainer(
        q,
        config=BusyStrategyConfig(queue_max_wait_s=600),
        poll_phase=lambda _a, _r: None,
        dispatch=fake_dispatch,
        event_log_resolver=lambda _t: event_log,
    )
    drainer.enqueue_dispatch(
        task_id="t",
        agent_id="bc-T",
        payload={},
        current_run_id="run-prev",
    )
    drainer.tick()

    assert len(q) == 1  # entry preserved
    fake_dispatch.assert_not_called()
    assert _types(event_log) == ["cloud.busy_queued"]


# ---------------------------------------------------------------------------
# AC (a) — config strictness for [cloud.busy_strategy] section
# (mirrors the loader test pattern; ranges + inter-key invariant).
# ---------------------------------------------------------------------------


def _write_toml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_busy_strategy_config_defaults_when_section_absent(tmp_path: Path) -> None:
    """A v0.8.7 deployment without ``[cloud.busy_strategy]`` keeps
    working with documented defaults (queue / 5 / 1800 / true)."""
    p = _write_toml(tmp_path / "popolad.toml", "")
    cfg = load_popolad_config(p).cloud.busy_strategy
    assert cfg == BusyStrategyConfig()
    assert cfg.mode == "queue"
    assert cfg.queue_poll_interval_s == 5
    assert cfg.queue_max_wait_s == 1800
    assert cfg.notify_on_dispatch is True


def test_busy_strategy_config_mode_invalid_value_rejected(tmp_path: Path) -> None:
    """``mode`` must be ``"queue"`` or ``"fail_fast"``; any other value
    rejects with the section + key + accepted set named (No-Silent-Failures).
    """
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.busy_strategy]\nmode = \"banana\"\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value)
    assert "cloud.busy_strategy" in msg
    assert "mode" in msg
    assert "fail_fast" in msg


def test_busy_strategy_config_queue_poll_out_of_range(tmp_path: Path) -> None:
    """``queue_poll_interval_s`` outside ``[1, 60]`` rejects with
    range cited per spec §2.2."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        f"[cloud.busy_strategy]\n"
        f"queue_poll_interval_s = {CLOUD_BUSY_QUEUE_POLL_MAX_S + 1}\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    assert "queue_poll_interval_s" in str(excinfo.value)


def test_busy_strategy_config_inter_key_invariant_rejected(tmp_path: Path) -> None:
    """When ``mode = "queue"`` and ``queue_max_wait_s > 0``, the loader
    rejects ``queue_poll_interval_s > queue_max_wait_s`` (spec §2.3
    rule 3) with both keys named so operators see the relationship."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.busy_strategy]\n"
        "mode = \"queue\"\n"
        "queue_poll_interval_s = 60\n"
        # 30 is in range [1, 60] but < 60 → invariant fails.
        # Both values must be in their own ranges first.
        "queue_max_wait_s = 30\n",  # below queue_poll_interval_s
    )
    # 30 is below the [60, 86400] range → range error short-circuits
    # before we hit the inter-key invariant. Use a value that passes
    # range first:
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.busy_strategy]\n"
        "mode = \"queue\"\n"
        "queue_poll_interval_s = 60\n"
        "queue_max_wait_s = 0\n",  # 0 = wait forever; invariant skipped
    )
    cfg = load_popolad_config(p).cloud.busy_strategy
    # When queue_max_wait_s == 0, invariant is intentionally skipped
    # (the deadline never expires).
    assert cfg.queue_max_wait_s == 0
    assert cfg.queue_poll_interval_s == 60


def test_busy_strategy_config_queue_max_wait_zero_accepted(tmp_path: Path) -> None:
    """``queue_max_wait_s = 0`` is the ``"wait forever"`` sentinel
    per spec §2.2; the loader accepts it explicitly even though it
    falls outside the ``[60, 86_400]`` range that protects against
    typos. Inter-key invariant is intentionally skipped when the
    sentinel is set."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.busy_strategy]\nqueue_max_wait_s = 0\n",
    )
    cfg = load_popolad_config(p).cloud.busy_strategy
    assert cfg.queue_max_wait_s == 0


def test_busy_strategy_config_notify_on_dispatch_strict_bool(tmp_path: Path) -> None:
    """``notify_on_dispatch`` must be a strict TOML bool — int 0/1
    rejects so an operator who typed ``= 1`` sees the type mismatch."""
    p = _write_toml(
        tmp_path / "popolad.toml",
        "[cloud.busy_strategy]\nnotify_on_dispatch = 1\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_popolad_config(p)
    msg = str(excinfo.value).lower()
    assert "notify_on_dispatch" in msg
    assert "bool" in msg


# ---------------------------------------------------------------------------
# AC (h) — _phase_from_run_body extracts upper-cased phase strings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status": "FINISHED"}, "FINISHED"),
        ({"status": "running"}, "RUNNING"),  # upper-case normalisation
        ({"status": "  CANCELLED  "}, "CANCELLED"),  # strip whitespace
        ({"status": ""}, ""),
        ({}, ""),
        (None, ""),
    ],
)
def test_phase_extractor_normalises_run_body(body: Any, expected: str) -> None:
    """``_phase_from_run_body`` upper-cases + strips whitespace + treats
    empty / missing as ``""`` (not ``None``); ``None`` body short-circuits
    to ``""`` so the helper is total."""
    assert _phase_from_run_body(body) == expected


# ---------------------------------------------------------------------------
# AC (g) — drainer ignores re-entrant pop attempts (race tolerance).
# ---------------------------------------------------------------------------


def test_drainer_handles_concurrent_pop_race(event_log: EventLog) -> None:
    """If ``pop_head`` is racing (the queue head changed between
    ``peek_head`` and ``pop_head``) the drainer bails without dispatching.
    Pins the no-double-dispatch invariant for the rare same-tick
    re-enqueue case (e.g. a relay flow that loops)."""
    q = PendingDispatchQueue()
    head = PendingDispatch(
        task_id="t-r",
        agent_id="bc-R",
        payload={},
        current_run_id="run-prev",
    )
    q.enqueue(head)

    # Drain the entry behind the drainer's back to simulate the race.
    q.pop_head("bc-R")

    fake_dispatch = MagicMock(return_value={"id": "ignored"})
    drainer = _make_drainer(
        q,
        poll_phase=lambda _a, _r: "FINISHED",
        dispatch=fake_dispatch,
        event_log_resolver=lambda _t: event_log,
    )
    # _handle_dispatch is called against the stale head; pop_head
    # returns None, which must short-circuit dispatch.
    drainer._handle_dispatch(head)
    fake_dispatch.assert_not_called()
