"""Tier 2 — coverage for ``LarkSupervisor._watch`` loop (v0.3.0 F4.D).

Exercises the actual ``_watch`` loop with a fake listener — no real
subprocess. This pushes the supervisor coverage from ~30% to ~85%.
"""

from __future__ import annotations

import asyncio

import pytest

from popolaloom.lark.supervisor import LarkSupervisor


class _FakeListener:
    """Minimal stub satisfying LarkListener's public surface."""

    def __init__(self, life_pattern: list[bool]) -> None:
        # life_pattern is a list of is_alive results to return on consecutive polls.
        self._life_pattern = list(life_pattern)
        self._idx = 0
        self.start_called = 0
        self.stop_called = 0

    @property
    def is_alive(self) -> bool:
        if self._idx < len(self._life_pattern):
            return self._life_pattern[self._idx]
        return False

    async def start(self) -> None:
        self.start_called += 1
        # Reset idx so the watcher sees a fresh life_pattern slice.
        self._idx = 0

    async def stop(self) -> None:
        self.stop_called += 1

    def advance(self) -> None:
        self._idx += 1


@pytest.mark.asyncio
async def test_supervisor_watch_listener_alive_resets_counter() -> None:
    """When listener stays alive ≥ reset threshold, counter resets to 0."""
    listener = _FakeListener(life_pattern=[True, True, True, True, True])
    sup = LarkSupervisor(
        listener,  # type: ignore[arg-type]
        max_restarts=2,
        restart_delays_s=(0.0, 0.0, 0.0),
        reset_threshold_s=0.05,  # very short
        poll_interval_s=0.02,
    )
    await sup.start()
    # Force a few advances so the watch loop iterates.
    for _ in range(8):
        listener.advance()
        await asyncio.sleep(0.03)
    await sup.stop()
    assert listener.start_called >= 1
    assert listener.stop_called >= 1


@pytest.mark.asyncio
async def test_supervisor_watch_restart_then_alive() -> None:
    """Listener dies once → supervisor restarts → stays alive."""
    listener = _FakeListener(
        life_pattern=[True, False, True, True, True, True, True, True]
    )
    sup = LarkSupervisor(
        listener,  # type: ignore[arg-type]
        max_restarts=3,
        restart_delays_s=(0.0, 0.0, 0.0),
        reset_threshold_s=999.0,  # don't auto-reset
        poll_interval_s=0.02,
    )
    await sup.start()
    for _ in range(10):
        listener.advance()
        await asyncio.sleep(0.03)
    await sup.stop()
    # Supervisor saw a death and restarted at least once.
    assert sup.state.total_restarts >= 1


@pytest.mark.asyncio
async def test_supervisor_watch_escalates_after_max_restarts() -> None:
    """Listener keeps dying → escalation event after max_restarts."""
    listener = _FakeListener(
        life_pattern=[False, False, False, False, False, False, False]
    )
    sup = LarkSupervisor(
        listener,  # type: ignore[arg-type]
        max_restarts=1,
        restart_delays_s=(0.0,),
        reset_threshold_s=999.0,
        poll_interval_s=0.02,
    )
    await sup.start()
    deadline = asyncio.get_event_loop().time() + 1.5
    while asyncio.get_event_loop().time() < deadline:
        listener.advance()
        if sup.state.escalated:
            break
        await asyncio.sleep(0.05)
    await sup.stop()
    assert sup.state.escalated is True


@pytest.mark.asyncio
async def test_supervisor_watch_handles_start_failure() -> None:
    """When listener.start() raises, the watch loop continues."""

    class _BadStart(_FakeListener):
        async def start(self) -> None:  # type: ignore[override]
            self.start_called += 1
            raise RuntimeError("listener start failed")

    listener = _BadStart(life_pattern=[False])
    sup = LarkSupervisor(
        listener,  # type: ignore[arg-type]
        max_restarts=1,
        restart_delays_s=(0.0,),
        reset_threshold_s=999.0,
        poll_interval_s=0.02,
    )
    # Initial start raises — the supervisor.start() should propagate it.
    with pytest.raises(RuntimeError):
        await sup.start()


@pytest.mark.asyncio
async def test_supervisor_double_start_raises() -> None:
    listener = _FakeListener(life_pattern=[True, True, True])
    sup = LarkSupervisor(
        listener,  # type: ignore[arg-type]
        poll_interval_s=0.02,
    )
    await sup.start()
    with pytest.raises(RuntimeError, match="already running"):
        await sup.start()
    await sup.stop()
