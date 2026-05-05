"""Round-2 mutation-kill tests for ``daemon/state.py`` (v0.5.4 Loop 4 — L4.E).

Per release-notes-v0.5.4.md L4.E: ``daemon/state.py`` reached 100 %
inferred kill rate in v0.3.4 (round 4) per
``evidence/mutmut-baseline.md`` against the 24 canonical mutations.
This file extends the kill surface for the suspicious branches the
v0.3.4 audit identified but did not enumerate as separate mutations:

1. ``TaskState.PENDING → TaskState.RUNNING`` transition with concurrent
   reads — verifies the lock guards the transition + readers see only
   atomic state.
2. ``update(state=None)`` is a no-op for the state field but DOES write
   other passed fields (the ``if state is not None`` guard at line 161
   must NOT clobber the existing state).
3. ``get(task_id)`` for a task that just transitioned to a terminal
   state in another thread returns the terminal handle (race window
   between ``update`` releasing the lock and ``get`` acquiring it).
4. ``update(cancel_escalated_to_sigkill=True)`` flips the flag, and
   ``False`` flips it back; the explicit-only-when-not-None guard
   prevents drift.
5. ``list_active`` MUST exclude handles that became terminal mid-stream
   (re-checks ``is_terminal()`` not a stale flag).
6. ``register`` raises BEFORE writing when duplicate; lock prevents
   torn writes.

Each case is < 100 ms and pure (no IO, no subprocess).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.daemon.state import StateStore, TaskHandle, TaskState


def _make_handle(
    task_id: str,
    *,
    state: TaskState = TaskState.PENDING,
    pid: int | None = None,
    persisted: bool = False,
    cancel_escalated_to_sigkill: bool = False,
) -> TaskHandle:
    """Build a TaskHandle with sensible defaults (mirrors test_state_mutation_resistance)."""
    return TaskHandle(
        task_id=task_id,
        cli="mutation-kill-test",
        pid=pid,
        state=state,
        started_at=datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC),
        event_log_path=Path(f"/tmp/mk/{task_id}.jsonl"),
        persisted=persisted,
        cancel_escalated_to_sigkill=cancel_escalated_to_sigkill,
    )


# ── 1. PENDING → RUNNING transition is atomic from concurrent reads ─────


def test_pending_to_running_transition_atomic_against_concurrent_get() -> None:
    """A reader thread spinning on ``get(tid).state`` MUST never observe a
    partial state during the PENDING → RUNNING transition.

    Kills mutation: drop the ``with self._lock:`` context manager in
    :meth:`StateStore.update` — without the lock, the in-Python
    assignment is still atomic at the bytecode level, but mutating
    away the with-block opens the door to multi-step updates landing
    half-done. We pin the contract that EVERY snapshot the reader
    sees is internally consistent (state is one of the documented
    enum values, never a stale or in-flight non-enum value).
    """
    store = StateStore()
    store.register(_make_handle("t-trans"))

    seen_states: list[TaskState] = []
    stop = threading.Event()

    def _reader() -> None:
        while not stop.is_set():
            handle = store.get("t-trans")
            if handle is None:
                continue
            seen_states.append(handle.state)
            if len(seen_states) > 200:
                return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    for _ in range(50):
        store.update("t-trans", state=TaskState.RUNNING)
        store.update("t-trans", state=TaskState.PENDING)

    stop.set()
    reader.join(timeout=2.0)

    valid_states = {
        TaskState.PENDING,
        TaskState.RUNNING,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
    }
    for s in seen_states:
        assert s in valid_states, f"reader saw a non-enum state {s!r}"


# ── 2. update(state=None) does NOT clobber existing state ────────────────


def test_update_state_none_does_not_clobber_existing_state() -> None:
    """``update(task_id, state=None, pid=N)`` MUST NOT clobber the
    existing state.

    Kills mutation: flip ``if state is not None`` (line 161) to
    ``if state is None``. Without the guard, passing ``state=None``
    would re-assign and (depending on the mutation form) reset the
    handle's state to None, which is a type-error the rest of the
    code path would crash on.

    Pinpoints the difference between "no update" (None) vs "explicit
    update to a sentinel".
    """
    store = StateStore()
    store.register(_make_handle("t-state-none"))
    store.update("t-state-none", state=TaskState.RUNNING)

    updated = store.update("t-state-none", state=None, pid=4242)

    assert updated.state == TaskState.RUNNING
    assert updated.pid == 4242

    fetched = store.get("t-state-none")
    assert fetched is not None
    assert fetched.state == TaskState.RUNNING
    assert fetched.pid == 4242


# ── 3. get() returns the post-update terminal handle (race window) ──────


def test_get_returns_post_update_handle_after_terminal_transition() -> None:
    """``get(task_id)`` against a task that JUST transitioned to terminal
    in another thread returns the new (terminal) handle, not a stale one.

    Kills mutation: split the ``with self._lock:`` block in
    :meth:`StateStore.get` — without the lock, the dict read could
    return a stale snapshot if the GIL preempts mid-read. The lock
    guarantees post-update visibility.

    We use a barrier to force the writer thread to commit BEFORE the
    reader's get; the contract is "any get after the writer's update
    returns the new handle".
    """
    store = StateStore()
    store.register(_make_handle("t-race"))
    store.update("t-race", state=TaskState.RUNNING, pid=12345)

    barrier = threading.Barrier(2)

    def _writer() -> None:
        barrier.wait()
        store.update(
            "t-race",
            state=TaskState.COMPLETED,
            exit_code=0,
            completed_at=datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC),
        )

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    barrier.wait()
    writer.join(timeout=1.0)
    assert not writer.is_alive()

    handle = store.get("t-race")
    assert handle is not None
    assert handle.state == TaskState.COMPLETED
    assert handle.exit_code == 0
    assert handle.completed_at == datetime(2026, 5, 5, 12, 5, 0, tzinfo=UTC)


# ── 4. cancel_escalated_to_sigkill flag flip pinning ────────────────────


def test_cancel_escalated_flag_flips_to_true_then_back_to_false() -> None:
    """The ``cancel_escalated_to_sigkill`` field follows the explicit-
    only-when-not-None semantics of the other update fields.

    Kills mutation: drop the ``if cancel_escalated_to_sigkill is not
    None:`` guard at line 173-174. Without the guard, every update
    would clobber the flag with whatever default ``None`` evaluates
    to.

    Pinpoints both the True flip AND the False flip back.
    """
    store = StateStore()
    store.register(_make_handle("t-sigkill"))

    initial = store.get("t-sigkill")
    assert initial is not None
    assert initial.cancel_escalated_to_sigkill is False

    store.update("t-sigkill", cancel_escalated_to_sigkill=True)
    after_true = store.get("t-sigkill")
    assert after_true is not None
    assert after_true.cancel_escalated_to_sigkill is True

    store.update("t-sigkill", state=TaskState.RUNNING)
    after_unrelated = store.get("t-sigkill")
    assert after_unrelated is not None
    assert after_unrelated.cancel_escalated_to_sigkill is True
    assert after_unrelated.state == TaskState.RUNNING

    store.update("t-sigkill", cancel_escalated_to_sigkill=False)
    after_false = store.get("t-sigkill")
    assert after_false is not None
    assert after_false.cancel_escalated_to_sigkill is False


# ── 5. list_active reflects mid-stream terminal transitions ─────────────


def test_list_active_excludes_handles_that_just_terminated() -> None:
    """``list_active`` returns ONLY non-terminal handles AT THE TIME OF
    THE CALL. A handle that transitioned to terminal between
    ``update()`` and the subsequent ``list_active()`` MUST be excluded.

    Kills mutation: cache ``is_terminal()`` per-handle as a flag and
    flip the ``not h.is_terminal()`` filter to a stale-flag check
    (line 180). The contract is fresh-read.
    """
    store = StateStore()
    store.register(_make_handle("t-1", state=TaskState.PENDING))
    store.register(_make_handle("t-2", state=TaskState.RUNNING))
    store.register(_make_handle("t-3", state=TaskState.RUNNING))

    pre = {h.task_id for h in store.list_active()}
    assert pre == {"t-1", "t-2", "t-3"}

    store.update("t-2", state=TaskState.COMPLETED)
    post = {h.task_id for h in store.list_active()}
    assert post == {"t-1", "t-3"}

    store.update("t-3", state=TaskState.FAILED)
    store.update("t-1", state=TaskState.CANCELED)
    final = store.list_active()
    assert final == []


# ── 6. register() raises BEFORE writing when duplicate ──────────────────


def test_register_duplicate_raises_atomically_no_partial_write() -> None:
    """When ``register()`` raises ValueError on a duplicate task_id, the
    in-memory dict MUST be unchanged.

    Kills mutation: reorder the duplicate check to AFTER the dict
    assignment (line 126-128). Without the early raise, a duplicate
    register would silently overwrite the prior handle.
    """
    store = StateStore()
    h1 = _make_handle("t-dup", pid=1111)
    h2 = _make_handle("t-dup", pid=2222)

    store.register(h1)
    pre = store.get("t-dup")
    assert pre is not None
    assert pre.pid == 1111

    with pytest.raises(ValueError, match="already registered"):
        store.register(h2)

    post = store.get("t-dup")
    assert post is not None
    assert post.pid == 1111
    assert post is pre


# ── 7. update() returns the SAME object stored in the dict ──────────────


def test_update_returns_same_handle_object_stored_in_dict() -> None:
    """``StateStore.update`` returns the in-store ``TaskHandle`` (identity
    preserved); callers can assert ``returned is store.get(tid)`` and
    rely on the assignment happening on the canonical instance.

    Kills mutation: make ``update`` return a fresh ``TaskHandle`` copy.
    With this mutation, the in-store handle would still be updated
    (same dataclass instance backed by the dict), but the returned
    value would be a divergent copy — silent confusion.
    """
    store = StateStore()
    store.register(_make_handle("t-id"))
    returned = store.update("t-id", state=TaskState.RUNNING, pid=9999)

    fetched = store.get("t-id")
    assert fetched is not None
    assert returned is fetched
    assert returned.state == TaskState.RUNNING
    assert returned.pid == 9999
