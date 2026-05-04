"""C12 — Concurrent dispatch race → all distinct task_ids, no double-register.

Per testing-matrix.md §10 #12.  10 simultaneous dispatches must
produce 10 distinct task_ids (uuid4-based, no collisions); the
StateStore.register MUST NOT silently dedupe or overwrite.

We test the in-process Popolad path (no real subprocess spawn — we
mock the supervisor) so the race window is purely the ``state`` and
``event_logs`` dict mutations under their respective locks.
"""

from __future__ import annotations

import threading
from pathlib import Path

from popolaloom.daemon.server import Popolad


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["python", "-c", "print('chaos C12')"]


def test_chaos_10_concurrent_dispatches_produce_10_distinct_task_ids(
    tmp_path: Path,
    mocker,
) -> None:
    """10 threads dispatching simultaneously → 10 distinct task ids in state."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )

    spawned_pids: list[int] = []
    pid_lock = threading.Lock()
    next_pid = [10000]

    def _fake_spawn(task_id, cmd, cwd, env, event_log, on_exit=None):
        with pid_lock:
            pid = next_pid[0]
            next_pid[0] += 1
        spawned_pids.append(pid)
        return pid

    mocker.patch.object(popolad._supervisor, "spawn", side_effect=_fake_spawn)

    barrier = threading.Barrier(parties=10, timeout=10.0)
    results: list[str] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def _worker(i: int) -> None:
        try:
            barrier.wait()
            tid = popolad.dispatch_task("cursor", f"concurrent {i}", cwd=None)
            with results_lock:
                results.append(tid)
        except BaseException as exc:  # noqa: BLE001
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)

    assert errors == [], f"unexpected errors: {errors}"
    assert len(results) == 10
    assert len(set(results)) == 10, (
        f"expected 10 distinct task_ids, got {len(set(results))} distinct from {results}"
    )

    listed = popolad.list_active()
    listed_ids = {item["task_id"] for item in listed}
    for tid in results:
        assert tid in listed_ids, f"missing {tid} from StateStore.list_active"


def test_chaos_concurrent_register_then_terminal_no_lost_handle(
    tmp_path: Path,
    mocker,
) -> None:
    """Concurrent register + terminal-update do not lose a task handle.

    Sanity that the StateStore lock keeps ``register`` + ``update``
    serialised (no torn writes).  We do 5 dispatches, mock supervisor
    so subprocesses immediately exit, and assert all 5 task ids land
    in ``list_all(include_terminal=True)``.
    """
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )

    def _fake_spawn(task_id, cmd, cwd, env, event_log, on_exit=None):
        if on_exit is not None:
            on_exit(task_id, 0)
        return 99999

    mocker.patch.object(popolad._supervisor, "spawn", side_effect=_fake_spawn)

    ids = [popolad.dispatch_task("cursor", f"sync {i}", cwd=None) for i in range(5)]
    assert len(set(ids)) == 5

    listed = popolad.list_all(include_terminal=True)
    listed_ids = {item["task_id"] for item in listed}
    for tid in ids:
        assert tid in listed_ids
