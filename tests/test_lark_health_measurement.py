"""Tier 1+2 — v0.3.3 round 3: lark_health real-fixture measurement.

Per roadmap §12.7 the ``hitl_handleability`` dimension reads the four
sub-scores (schema_completeness, reply_parse_success, cross_channel_sync,
**lark_health**); v0.3.0 shipped the formula with a placeholder lark_health
because no evidence pipeline scanned for ``lark.send.*`` /
``lark.listener.*`` event types.  v0.3.3 round 3 closes that gap:

1. :func:`popolaloom.evaluation.runner.collect_evidence` now scans NDJSON
   logs for ``lark.send.{ok,failed}`` (success rate) and
   ``lark.listener.{started,died,restarted,escalated}`` (uptime).
2. :func:`popolaloom.evaluation.runner._compute_lark_uptime` rolls up
   the listener event timeline into (total, alive) windows.
3. Pre-existing ``hitl_round_trips`` populates the latency component.
4. :func:`popolaloom.evaluation.dimensions.hitl_handleability._compute_lark_health`
   composites the three components per the spec formula:
   send×0.5 + uptime×0.3 + latency×0.2.

Tests cover:

- Per-component rolling (3 cases: send / uptime / latency)
- Composite weighting (3 cases: each weight applied)
- No-evidence ``None`` fallback (2 cases)
- 4-restart escalation chaos test (1 case using the real
  :class:`popolaloom.lark.supervisor.LarkSupervisor`)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from popolaloom.evaluation.dimensions.hitl_handleability import (
    HitlHandleability,
    _compute_lark_health,
)
from popolaloom.evaluation.runner import (
    _compute_lark_uptime,
    collect_evidence,
)

# ── _compute_lark_uptime — pure helper ─────────────────────────────────


def test_compute_lark_uptime_empty_returns_zero() -> None:
    """0 events → (0.0, 0.0) — insufficient evidence."""
    total, alive = _compute_lark_uptime([])
    assert total == 0.0
    assert alive == 0.0


def test_compute_lark_uptime_single_event_returns_zero() -> None:
    """1 event has no gap to span → (0.0, 0.0)."""
    total, alive = _compute_lark_uptime([(1000.0, True)])
    assert total == 0.0
    assert alive == 0.0


def test_compute_lark_uptime_alive_then_dead() -> None:
    """started @ 0 + died @ 100 → 100s total, 100s alive (fully alive segment)."""
    total, alive = _compute_lark_uptime([(0.0, True), (100.0, False)])
    assert total == 100.0
    assert alive == 100.0


def test_compute_lark_uptime_dead_segment_contributes_zero() -> None:
    """started @ 0, died @ 100, restarted @ 200 → total=200, alive=100."""
    events = [(0.0, True), (100.0, False), (200.0, True)]
    total, alive = _compute_lark_uptime(events)
    assert total == 200.0
    assert alive == 100.0


def test_compute_lark_uptime_unsorted_input_handled() -> None:
    """Unordered timestamps don't break the rollup (we sort internally)."""
    events = [(200.0, True), (0.0, True), (100.0, False)]
    total, alive = _compute_lark_uptime(events)
    assert total == 200.0
    assert alive == 100.0


def test_compute_lark_uptime_negative_span_clamps_to_zero() -> None:
    """All events at the same instant → 0 span → (0, 0)."""
    total, alive = _compute_lark_uptime([(50.0, True), (50.0, False)])
    assert total == 0.0
    assert alive == 0.0


# ── _compute_lark_health — composite formula ──────────────────────────


def test_compute_lark_health_no_evidence_returns_none() -> None:
    assert _compute_lark_health({}) is None


def test_compute_lark_health_send_only() -> None:
    """Only ``lark_send_*`` populated → 100 % weight on send component."""
    score = _compute_lark_health(
        {"lark_send_total": 10, "lark_send_ok": 8}
    )
    assert score == pytest.approx(0.8, abs=0.001)


def test_compute_lark_health_all_components() -> None:
    """Send 100 %, uptime 50 %, latency 100 % → 0.5×1.0 + 0.3×0.5 + 0.2×1.0 = 0.85."""
    score = _compute_lark_health(
        {
            "lark_send_total": 5,
            "lark_send_ok": 5,
            "lark_listener_uptime_total_s": 100.0,
            "lark_listener_uptime_alive_s": 50.0,
            "lark_roundtrip_total": 4,
            "lark_roundtrip_under_10s": 4,
        }
    )
    assert score == pytest.approx(0.85, abs=0.001)


def test_compute_lark_health_zero_send_total_skipped() -> None:
    """``lark_send_total = 0`` → component skipped (insufficient denom)."""
    score = _compute_lark_health(
        {
            "lark_send_total": 0,
            "lark_send_ok": 0,
            "lark_listener_uptime_total_s": 60.0,
            "lark_listener_uptime_alive_s": 60.0,
        }
    )
    assert score == pytest.approx(1.0, abs=0.001)


# ── collect_evidence — scans NDJSON for lark.* events ─────────────────


def _write_event(path: Path, type_: str, time_str: str, **data: object) -> None:
    """Append one CloudEvents-shaped envelope to ``path``."""
    envelope = {
        "id": f"ev-{type_}-{time_str}",
        "type": type_,
        "time": time_str,
        "data": dict(data),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope) + "\n")


def test_collect_evidence_scans_lark_send_events(tmp_path: Path) -> None:
    """``lark.send.{ok,failed}`` event types roll up into send_total/ok."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "task-1.jsonl"
    for i in range(7):
        _write_event(log, "lark.send.ok", f"2026-05-04T12:00:0{i}.000Z")
    for i in range(3):
        _write_event(log, "lark.send.failed", f"2026-05-04T12:01:0{i}.000Z")

    evidence = collect_evidence(events_dir)
    assert evidence["lark_send_total"] == 10
    assert evidence["lark_send_ok"] == 7


def test_collect_evidence_scans_listener_uptime(tmp_path: Path) -> None:
    """``lark.listener.{started,died,restarted}`` rolls up into uptime."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "task-1.jsonl"
    _write_event(log, "lark.listener.started", "2026-05-04T12:00:00.000Z")
    _write_event(log, "lark.listener.died", "2026-05-04T12:01:00.000Z")
    _write_event(log, "lark.listener.restarted", "2026-05-04T12:01:30.000Z")
    _write_event(log, "lark.listener.died", "2026-05-04T12:02:30.000Z")

    evidence = collect_evidence(events_dir)
    assert evidence["lark_listener_uptime_total_s"] == pytest.approx(150.0, abs=0.1)
    # Alive segments: 0..60 (started→died) + 90..150 (restarted→died) = 120s
    assert evidence["lark_listener_uptime_alive_s"] == pytest.approx(120.0, abs=0.1)


def test_collect_evidence_lark_roundtrip_under_10s(tmp_path: Path) -> None:
    """``hitl_round_trips`` (already collected by v0.2.0) → roundtrip_under_10s."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "task-1.jsonl"
    _write_event(log, "task.elicited", "2026-05-04T12:00:00.000Z")
    _write_event(log, "human.responded", "2026-05-04T12:00:05.000Z")  # 5s ≤ 10s
    _write_event(log, "task.elicited", "2026-05-04T13:00:00.000Z")
    _write_event(log, "human.responded", "2026-05-04T13:00:15.000Z")  # 15s > 10s

    evidence = collect_evidence(events_dir)
    assert evidence["lark_roundtrip_total"] == 2
    assert evidence["lark_roundtrip_under_10s"] == 1


def test_collect_evidence_no_lark_events_keeps_none(tmp_path: Path) -> None:
    """Empty events dir → all lark_* keys stay None (placeholder behaviour)."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    evidence = collect_evidence(events_dir)
    assert evidence["lark_send_total"] is None
    assert evidence["lark_listener_uptime_total_s"] is None
    assert evidence["lark_roundtrip_total"] is None


# ── HitlHandleability composite with real lark_health evidence ───────


def test_hitl_handleability_with_lark_health_lifts_score() -> None:
    """Adding lark_health=0.95 (perfect Lark) lifts the composite."""
    base = HitlHandleability().score({})
    rich = HitlHandleability().score(
        {
            "lark_send_total": 100,
            "lark_send_ok": 100,
            "lark_listener_uptime_total_s": 100.0,
            "lark_listener_uptime_alive_s": 100.0,
            "lark_roundtrip_total": 5,
            "lark_roundtrip_under_10s": 5,
        }
    )
    assert base == 0.5
    assert rich == pytest.approx(1.0, abs=0.001)
    assert rich > base


def test_hitl_handleability_with_partial_lark_health() -> None:
    """80 % send rate alone → 80 % lark_health → composite drops by 0.2 weight × 0.2 below 1.0."""
    score = HitlHandleability().score(
        {"lark_send_total": 10, "lark_send_ok": 8}
    )
    assert score == pytest.approx(0.8, abs=0.001)


# ── 4-restart escalation chaos test ───────────────────────────────────


class _FakeListener:
    """Listener stub that "dies" once per ``start()`` call.

    The supervisor will see ``is_alive=False`` immediately after each
    restart, count it as a death, and (per ``MAX_RESTARTS=3``) escalate
    on the 4th cycle.  The state machine matches what a real broken
    lark-cli subprocess would do.
    """

    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self._alive = False

    async def start(self) -> None:
        self.start_calls += 1
        self._alive = True
        # Schedule death on the next event-loop tick so the supervisor
        # sees one alive read followed by one dead read per cycle.
        loop = asyncio.get_running_loop()
        loop.call_later(0.005, self._kill)

    def _kill(self) -> None:
        self._alive = False

    async def stop(self, timeout_s: float = 5.0) -> None:
        self.stop_calls += 1
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive


@pytest.mark.asyncio
async def test_lark_supervisor_escalates_after_3_restarts() -> None:
    """4 consecutive listener deaths → supervisor escalates to HITL.

    Per roadmap §12.8.2 + RV3-5: after 3 failed restart attempts the
    supervisor stops trying and emits ``listener.escalated``.  The
    listener enters a "down + escalated" state that the operator must
    manually resolve.
    """
    from popolaloom.lark.supervisor import LarkSupervisor

    fake = _FakeListener()
    events: list[dict[str, str]] = []

    async def _capture(event: dict[str, str]) -> None:
        events.append(event)

    sup = LarkSupervisor(
        listener=fake,  # type: ignore[arg-type]
        on_event=_capture,
        max_restarts=3,
        restart_delays_s=(0.001, 0.001, 0.001),  # tiny delays for fast test
        reset_threshold_s=999.0,  # never reset counter during the test
        poll_interval_s=0.01,
    )

    await sup.start()
    # Wait long enough for: start + 3 restarts + escalation
    deadline = asyncio.get_event_loop().time() + 2.0
    while not sup.state.escalated and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    await sup.stop()

    assert sup.state.escalated, (
        f"supervisor failed to escalate after {fake.start_calls} starts; "
        f"events seen: {[e.get('event') for e in events]}"
    )
    assert sup.state.restart_count == 3
    death_count = sum(1 for e in events if e.get("event") == "listener.died")
    assert death_count >= 4
    escalation_count = sum(1 for e in events if e.get("event") == "listener.escalated")
    assert escalation_count == 1
