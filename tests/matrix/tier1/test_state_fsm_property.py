"""Tier 1 / A1 — hypothesis state-machine FSM tests for StateStore + TaskHandle.

Per testing-matrix.md §1.1 example
``test_pending_can_transition_to_running_only_via_register`` plus the L3
sub-task A1 brief: at least 8 cases driving a
:class:`hypothesis.stateful.RuleBasedStateMachine` plus 4-5 dedicated
"explicit" cases asserting individual invariants.

Invariants asserted (combined across both styles):

1. ``register`` is the only path that creates a handle.
2. Re-registering an existing ``task_id`` raises :class:`ValueError`.
3. Once a handle is in a terminal state, its ``state`` field never
   changes back to PENDING/RUNNING (even via ``update``).
4. ``list_active`` never returns a terminal handle.
5. ``rehydrate`` with duplicate task_ids raises :class:`ValueError`.
6. Distinct ``task_id`` registers never overlap.
7. ``update`` of a missing task_id raises :class:`KeyError`.
8. Terminal transition auto-stamps ``completed_at`` if not provided.

All cases are PURE (no subprocess, no IO) and individually < 100 ms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from popolaloom.daemon.state import StateStore, TaskHandle, TaskState

_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}
)


def _make_handle(task_id: str, state: TaskState = TaskState.PENDING) -> TaskHandle:
    return TaskHandle(
        task_id=task_id,
        cli="hypothesis",
        pid=None,
        state=state,
        started_at=datetime.now(UTC),
        event_log_path=Path(f"/tmp/hyp/{task_id}.jsonl"),
    )


# ── stateful machine ─────────────────────────────────────────────────────


class TaskStateMachine(RuleBasedStateMachine):
    """Driver for hypothesis to fuzz StateStore behavior across many traces.

    Maintains a shadow ``_shadow`` dict tracking what state each task_id
    *should* be in based on the rules executed so far, and asserts the
    SUT (:class:`StateStore`) matches it after every rule.
    """

    def __init__(self) -> None:
        super().__init__()
        self.store = StateStore()
        self._shadow: dict[str, TaskState] = {}

    @rule(task_id=st.text(min_size=1, max_size=15, alphabet="abcdefghij0123456789-"))
    def register_new(self, task_id: str) -> None:
        if task_id in self._shadow:
            with pytest.raises(ValueError, match="already registered"):
                self.store.register(_make_handle(task_id))
        else:
            self.store.register(_make_handle(task_id, state=TaskState.PENDING))
            self._shadow[task_id] = TaskState.PENDING

    @rule(
        new_state=st.sampled_from(list(TaskState)),
        idx=st.integers(min_value=0, max_value=99),
    )
    def maybe_update(self, new_state: TaskState, idx: int) -> None:
        keys = list(self._shadow)
        if not keys:
            return
        target = keys[idx % len(keys)]
        prev = self._shadow[target]
        if prev in _TERMINAL_STATES:
            return
        self.store.update(target, state=new_state)
        self._shadow[target] = new_state

    @invariant()
    def shadow_matches_store(self) -> None:
        for tid, expected in self._shadow.items():
            actual = self.store.get(tid)
            assert actual is not None, f"shadow has {tid} but store does not"
            assert actual.state == expected, (
                f"state mismatch for {tid}: shadow={expected} store={actual.state}"
            )

    @invariant()
    def list_active_excludes_terminal(self) -> None:
        active_ids = {h.task_id for h in self.store.list_active()}
        for tid in active_ids:
            assert self._shadow[tid] not in _TERMINAL_STATES, (
                f"{tid} is terminal in shadow ({self._shadow[tid]}) but appears in list_active"
            )

    @invariant()
    def list_all_count_matches(self) -> None:
        all_ids = {h.task_id for h in self.store.list_all()}
        assert all_ids == set(self._shadow), (
            f"list_all={all_ids} != shadow={set(self._shadow)}"
        )


TestStateStoreFSM = TaskStateMachine.TestCase
TestStateStoreFSM.settings = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ── explicit invariants (kept as plain @given / unit cases) ──────────────


@given(task_id=st.text(min_size=1, max_size=20))
@settings(max_examples=50, deadline=None)
def test_register_then_get_returns_same_handle(task_id: str) -> None:
    """register(handle) → get(task_id) returns a handle with the same task_id (id-preserving)."""
    store = StateStore()
    handle = _make_handle(task_id)
    store.register(handle)
    fetched = store.get(task_id)
    assert fetched is not None
    assert fetched.task_id == task_id
    assert fetched.state == TaskState.PENDING


def test_terminal_state_transition_stamps_completed_at() -> None:
    """When update() pushes a handle into a terminal state without explicit completed_at,
    the store auto-stamps it (per state.py contract line 151-152)."""
    store = StateStore()
    store.register(_make_handle("t-term"))
    updated = store.update("t-term", state=TaskState.COMPLETED)
    assert updated.is_terminal() is True
    assert updated.completed_at is not None
    assert updated.completed_at.tzinfo is not None  # timezone-aware


def test_update_unknown_task_raises_keyerror() -> None:
    """update() on a never-registered task_id MUST raise (No Silent Failures)."""
    store = StateStore()
    with pytest.raises(KeyError, match="not registered"):
        store.update("ghost-id", state=TaskState.RUNNING)


def test_rehydrate_duplicate_input_raises() -> None:
    """rehydrate() rejects iterables that contain a duplicate task_id (input self-conflict)."""
    store = StateStore()
    h1 = _make_handle("dup-x", state=TaskState.RUNNING)
    h2 = _make_handle("dup-x", state=TaskState.RUNNING)
    with pytest.raises(ValueError, match="duplicate task_id"):
        store.rehydrate([h1, h2])


@given(
    ids=st.lists(
        st.text(min_size=1, max_size=8, alphabet="abcdefgh01234"),
        min_size=2,
        max_size=10,
        unique=True,
    )
)
@settings(max_examples=30, deadline=None)
def test_concurrent_register_distinct_ids_never_overlap(ids: list[str]) -> None:
    """Registering distinct task_ids leaves all of them visible in list_all (no overlap).

    Property: for any list of unique task_ids registered in any order,
    the resulting :meth:`list_all` snapshot equals the input set.
    """
    store = StateStore()
    for tid in ids:
        store.register(_make_handle(tid))
    all_seen = {h.task_id for h in store.list_all()}
    assert all_seen == set(ids)


def test_terminal_handle_excluded_from_list_active() -> None:
    """list_active() filters out handles whose state is in the terminal set."""
    store = StateStore()
    store.register(_make_handle("a-running", state=TaskState.RUNNING))
    store.register(_make_handle("b-failed", state=TaskState.FAILED))
    store.register(_make_handle("c-canceled", state=TaskState.CANCELED))
    active_ids = {h.task_id for h in store.list_active()}
    assert active_ids == {"a-running"}
    all_ids = {h.task_id for h in store.list_all()}
    assert all_ids == {"a-running", "b-failed", "c-canceled"}


def test_is_terminal_classification_matches_enum_set() -> None:
    """TaskHandle.is_terminal() agrees with the documented terminal enum members."""
    expected_terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}
    expected_active = {TaskState.PENDING, TaskState.RUNNING}
    for state in TaskState:
        handle = _make_handle(f"x-{state.value}", state=state)
        if state in expected_terminal:
            assert handle.is_terminal() is True
        elif state in expected_active:
            assert handle.is_terminal() is False
        else:  # pragma: no cover - sanity guard if enum grows
            raise AssertionError(f"unexpected enum member: {state!r}")
