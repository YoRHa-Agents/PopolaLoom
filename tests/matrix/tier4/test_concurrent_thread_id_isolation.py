"""Tier 4 — concurrent dispatch via real Popolad + thread_id isolation.

Per testing-matrix.md §1.4 — exercises 5 concurrent dispatches against
a single Popolad instance with a real ``SqliteSaver`` checkpointer and
verifies each task lands in its own ``thread_id`` row without
cross-contamination.

3 cases (target ≥ 3):

1. 5 concurrent dispatches → all reach terminal + each task gets a
   distinct ``thread_id`` row in the SqliteSaver checkpoint table.
2. Per-task NDJSON event logs are file-isolated (no cross-writes).
3. Snapshot of the multi-thread checkpoint state via syrupy (locks
   the column shape).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import (
    Popolad,
    TaskState,
    make_checkpointer,
)

pytestmark = [pytest.mark.slow, pytest.mark.real_graph]


def _wait_for_terminal(popolad: Popolad, task_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        last_state = status["state"]
        if last_state in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            return last_state
        time.sleep(0.05)
    return last_state


def _fast_adapter(cli: str, prompt: str, cwd: Path | None, extra: Any = None) -> list[str]:
    """Tiny shim that prints + exits 0 quickly so concurrent test stays fast."""
    return [sys.executable, "-c", f"print({prompt!r})"]


def test_five_concurrent_dispatches_all_terminal_and_isolated(tmp_path: Path) -> None:
    """Case 1: 5 parallel dispatches → all COMPLETED + 5 distinct thread_ids."""
    events_dir = tmp_path / "events"
    db_path = tmp_path / "state_concurrent.sqlite"
    saver = make_checkpointer(db_path=db_path)
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_fast_adapter,
        use_graph=True,
        checkpointer=saver,
    )

    task_ids: list[str] = []
    lock = threading.Lock()

    def _dispatch(idx: int) -> None:
        tid = popolad.dispatch_task(cli=f"cli-{idx}", prompt=f"prompt-{idx}")
        with lock:
            task_ids.append(tid)

    threads = [
        threading.Thread(target=_dispatch, args=(i,), name=f"dispatcher-{i}")
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    for t in threads:
        assert not t.is_alive(), f"{t.name} hung"

    assert len(task_ids) == 5
    assert len(set(task_ids)) == 5, f"task_id collisions: {task_ids}"

    for tid in task_ids:
        state = _wait_for_terminal(popolad, tid, timeout=15.0)
        assert state == str(TaskState.COMPLETED), (
            f"task {tid} did not COMPLETE; final state={state}"
        )

    expected_ids = set(task_ids)
    deadline = time.monotonic() + 5.0
    ids: set[str] = set()
    while time.monotonic() < deadline:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
            ids = {row[0] for row in cur.fetchall()}
        if expected_ids.issubset(ids):
            break
        time.sleep(0.1)
    missing = expected_ids - ids
    assert not missing, (
        f"task_ids missing from checkpoints after 5s wait: {sorted(missing)}; "
        f"saw {sorted(ids)}"
    )


def test_per_task_event_logs_are_file_isolated(tmp_path: Path) -> None:
    """Case 2: per-task NDJSON files don't share content (file-level isolation)."""
    events_dir = tmp_path / "events_iso"
    saver = make_checkpointer(db_path=tmp_path / "state_iso.sqlite")
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_fast_adapter,
        use_graph=True,
        checkpointer=saver,
    )

    task_ids = [
        popolad.dispatch_task(cli=f"cli-{i}", prompt=f"unique-marker-{i}")
        for i in range(3)
    ]
    for tid in task_ids:
        s = _wait_for_terminal(popolad, tid, timeout=15.0)
        assert s == str(TaskState.COMPLETED), f"task {tid} not COMPLETED: {s}"

    for i, tid in enumerate(task_ids):
        log_path = events_dir / f"{tid}.jsonl"
        assert log_path.exists(), f"event log missing for {tid}"
        text = log_path.read_text(encoding="utf-8")
        for j in range(3):
            if i == j:
                assert f"unique-marker-{i}" in text, (
                    f"marker for own task missing from log {tid}: {text[:500]}"
                )
            else:
                assert f"unique-marker-{j}" not in text, (
                    f"cross-contamination: log {tid} contains marker for task {j}"
                )


def test_concurrent_checkpoint_state_snapshot(
    tmp_path: Path, snapshot: Any
) -> None:
    """Case 3: snapshot of the column shape + thread count of a 3-task run."""
    events_dir = tmp_path / "events_snap"
    db_path = tmp_path / "state_snap.sqlite"
    saver = make_checkpointer(db_path=db_path)
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_fast_adapter,
        use_graph=True,
        checkpointer=saver,
    )

    task_ids = [
        popolad.dispatch_task(cli="snap-cli", prompt=f"snap-{i}")
        for i in range(3)
    ]
    for tid in task_ids:
        _wait_for_terminal(popolad, tid, timeout=15.0)

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(checkpoints)")
        cols = sorted(row[1] for row in cur.fetchall())
        cur.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints")
        distinct_threads = int(cur.fetchone()[0])

    payload = {
        "checkpoint_columns": cols,
        "distinct_thread_count": distinct_threads,
    }
    assert payload == snapshot
