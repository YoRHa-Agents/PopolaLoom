"""Tier 1 — v0.3.4 round 4 mutation-resistance tests for ``daemon/state.py``.

Per testing-matrix.md §6 (mutation testing for v0.4.0): pre-validate
test quality on the smallest, most-mutation-prone module
(``daemon/state.py`` — pure FSM with 9 mutable branches).

Strategy
--------

Live ``mutmut run`` is currently blocked by mutmut 3.5's
``mutants/`` ↔ ``src/`` layering (mutmut copies tests to ``mutants/``
then changes CWD before running pytest, which clashes with our
editable install — see ``evidence/mutmut-baseline.md`` for the full
analysis).  We therefore did a **manual mutation audit** on
``daemon/state.py``, identified the 5 mutation classes whose existing
tests would NOT kill, and added the targeted tests below.

Mutations addressed (each test kills the specific mutation):

1. ``update(pid=...)`` body removal — drop the ``handle.pid = pid``
   assignment.  Existing FSM only mutates ``state``, never ``pid``.
2. ``update(exit_code=...)`` body removal — drop the
   ``handle.exit_code = exit_code`` assignment.
3. ``update(persisted=...)`` body removal — drop the
   ``handle.persisted = persisted`` assignment (new R-008 field;
   FSM doesn't touch it).
4. Explicit ``completed_at`` override is preserved (the
   ``handle.completed_at is None`` guard is critical; mutating away
   the ``elif`` would re-stamp the timestamp and clobber the explicit
   value).
5. ``rehydrate`` is authoritative (overwrites existing entries) —
   the docstring says "已存在的 task_id 会被覆盖" but no existing
   test pins this contract.

Each test is < 50 ms and pure (no IO, no subprocess).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from popolaloom.daemon.state import StateStore, TaskHandle, TaskState


def _make_handle(
    task_id: str,
    state: TaskState = TaskState.PENDING,
    pid: int | None = None,
    persisted: bool = False,
) -> TaskHandle:
    return TaskHandle(
        task_id=task_id,
        cli="mutation-test",
        pid=pid,
        state=state,
        started_at=datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC),
        event_log_path=Path(f"/tmp/mt/{task_id}.jsonl"),
        persisted=persisted,
    )


# ── pid update mutation kill ────────────────────────────────────────


def test_update_pid_writes_to_handle() -> None:
    """``update(task_id, pid=N)`` MUST write ``N`` into ``handle.pid``.

    Kills mutation: drop the ``handle.pid = pid`` assignment in
    :meth:`StateStore.update`.
    """
    store = StateStore()
    store.register(_make_handle("t-pid"))
    updated = store.update("t-pid", pid=4242)
    assert updated.pid == 4242
    fetched = store.get("t-pid")
    assert fetched is not None
    assert fetched.pid == 4242


# ── exit_code update mutation kill ───────────────────────────────────


def test_update_exit_code_writes_to_handle() -> None:
    """``update(task_id, exit_code=N)`` MUST write ``N`` into ``handle.exit_code``.

    Kills mutation: drop the ``handle.exit_code = exit_code`` assignment
    in :meth:`StateStore.update`.
    """
    store = StateStore()
    store.register(_make_handle("t-exit"))
    updated = store.update("t-exit", exit_code=137)
    assert updated.exit_code == 137
    fetched = store.get("t-exit")
    assert fetched is not None
    assert fetched.exit_code == 137


def test_update_exit_code_zero_distinguishable_from_none() -> None:
    """exit_code=0 (success) MUST be written exactly, NOT confused with None.

    Kills mutation: ``if exit_code is not None`` → ``if exit_code``;
    that flip would silently drop ``exit_code=0`` (a real Unix process
    success), so we explicitly probe the falsy edge.
    """
    store = StateStore()
    store.register(_make_handle("t-zero"))
    updated = store.update("t-zero", exit_code=0)
    assert updated.exit_code == 0  # NOT None
    fetched = store.get("t-zero")
    assert fetched is not None
    assert fetched.exit_code == 0


# ── persisted update mutation kill (R-008 field) ─────────────────────


def test_update_persisted_true_writes_to_handle() -> None:
    """``update(task_id, persisted=True)`` MUST write ``True`` (R-008 fix).

    Kills mutation: drop the ``handle.persisted = persisted`` assignment.
    The FSM tests don't touch ``persisted`` so this is a fresh kill.
    """
    store = StateStore()
    store.register(_make_handle("t-pers", persisted=False))
    updated = store.update("t-pers", persisted=True)
    assert updated.persisted is True
    fetched = store.get("t-pers")
    assert fetched is not None
    assert fetched.persisted is True


def test_update_persisted_false_after_true_writes_back() -> None:
    """``update(persisted=False)`` after a True must flip the field back.

    Kills mutation: ``if persisted is not None`` → ``if persisted``
    (the bool-on-None confusion); that would silently ignore
    ``persisted=False`` because `False` is falsy.
    """
    store = StateStore()
    store.register(_make_handle("t-flip", persisted=True))
    updated = store.update("t-flip", persisted=False)
    assert updated.persisted is False
    fetched = store.get("t-flip")
    assert fetched is not None
    assert fetched.persisted is False


# ── explicit completed_at override mutation kill ─────────────────────


def test_update_explicit_completed_at_is_preserved() -> None:
    """When the caller supplies an explicit ``completed_at``, the
    auto-stamp branch MUST NOT overwrite it.

    Kills mutation: drop the ``elif state in _TERMINAL_STATES and
    handle.completed_at is None`` branch's guard, OR drop the
    ``elif`` entirely (collapse the if/elif into two unconditional
    `if`s).
    """
    explicit = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    store = StateStore()
    store.register(_make_handle("t-explicit"))
    updated = store.update(
        "t-explicit", state=TaskState.COMPLETED, completed_at=explicit
    )
    assert updated.completed_at == explicit


def test_update_terminal_without_explicit_stamp_uses_now() -> None:
    """Companion to the above: when ``completed_at`` is omitted, the
    auto-stamp DOES fire and produces a UTC-aware timestamp close to ``now``.

    Kills mutation: ``handle.completed_at is None`` → ``handle.completed_at is not None``
    (the inverted guard would *only* stamp when already stamped, never
    on the natural completion path).
    """
    before = datetime.now(UTC) - timedelta(seconds=1)
    store = StateStore()
    store.register(_make_handle("t-auto"))
    updated = store.update("t-auto", state=TaskState.FAILED)
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert updated.completed_at is not None
    assert before <= updated.completed_at <= after


def test_update_non_terminal_state_does_not_stamp_completed_at() -> None:
    """Transitioning to RUNNING (non-terminal) must NOT auto-stamp.

    Kills mutation: ``elif state in _TERMINAL_STATES`` → ``elif state not in _TERMINAL_STATES``;
    that flip would stamp completed_at on RUNNING transitions which
    is logically wrong.
    """
    store = StateStore()
    store.register(_make_handle("t-running"))
    updated = store.update("t-running", state=TaskState.RUNNING)
    assert updated.completed_at is None


# ── rehydrate authoritative overwrite mutation kill ──────────────────


def test_rehydrate_overwrites_existing_entry() -> None:
    """rehydrate is authoritative: existing entries MUST be overwritten.

    Kills mutation: ``self._tasks[tid] = handle`` → ``self._tasks.setdefault(tid, handle)``
    in the rehydrate inner loop.  That mutation would silently drop the
    rehydrated state in favour of the in-memory one, which is exactly the
    bug ``rehydrate`` is meant to prevent on daemon restart.
    """
    store = StateStore()
    store.register(_make_handle("t-rehyd", state=TaskState.PENDING))
    new_handle = _make_handle("t-rehyd", state=TaskState.RUNNING)
    store.rehydrate([new_handle])
    after = store.get("t-rehyd")
    assert after is not None
    assert after.state == TaskState.RUNNING


def test_rehydrate_empty_iterable_is_noop() -> None:
    """Rehydrating an empty iterable MUST NOT clear the existing store.

    Kills mutation: change rehydrate body to ``self._tasks = {}``
    (truncate-and-replace).  An empty input must leave the store
    untouched (per docstring "rehydrate is authoritative" — the input
    IS the new state, not "additional state to merge").
    """
    store = StateStore()
    store.register(_make_handle("t-keep", state=TaskState.RUNNING))
    store.rehydrate([])
    after = store.get("t-keep")
    assert after is not None
    assert after.state == TaskState.RUNNING


# ── register error path mutation kill ───────────────────────────────


def test_register_duplicate_does_not_overwrite_existing() -> None:
    """When register raises ValueError, the original handle must remain.

    Kills mutation: in ``register``, the assignment
    ``self._tasks[handle.task_id] = handle`` reordered ABOVE the
    duplicate check; that would silently overwrite the original
    handle's state.
    """
    store = StateStore()
    store.register(_make_handle("t-orig", state=TaskState.RUNNING))
    with pytest.raises(ValueError, match="already registered"):
        store.register(_make_handle("t-orig", state=TaskState.FAILED))
    after = store.get("t-orig")
    assert after is not None
    assert after.state == TaskState.RUNNING


# ── update returning the SAME handle reference mutation kill ────────


def test_update_returns_handle_instance_used_for_storage() -> None:
    """``update`` returns the in-store handle (no copy).

    Kills mutation: ``return handle`` → ``return handle.__class__()``
    (returning a fresh empty handle).  The caller relies on the returned
    handle being the live one, e.g. to read freshly-set fields.
    """
    store = StateStore()
    store.register(_make_handle("t-ref"))
    returned = store.update("t-ref", pid=999)
    fetched = store.get("t-ref")
    assert fetched is returned  # SAME object — not a copy
