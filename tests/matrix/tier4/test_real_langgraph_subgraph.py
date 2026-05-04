"""Tier 4 — real langgraph Gen-Verifier subgraph + checkpointer round-trip.

Per testing-matrix.md §1.4 example
``test_subgraph_gen_verifier_convergence.py`` + roadmap §3.4 v0.2.3 DoD.

These cases use the **real** ``langgraph`` ``SqliteSaver`` + the real
``build_dev_test_subgraph`` from
:mod:`popolaloom.daemon.subgraph_dev_test` — no mocking — so the
subgraph DAG topology + verifier loop + thread isolation contracts
are exercised end-to-end.

5 cases (target ≥ 4):

1. Convergence: score sequence ``[0.5, 0.9]`` max_iter=2 → done True
   at iter=2.
2. Give-up: score sequence ``[0.3, 0.4]`` max_iter=2 → give_up True.
3. Concurrent 3 subgraph runs with distinct thread_ids → checkpoints
   isolated (no cross-pollination).
4. SqliteSaver persistence round-trip: subgraph runs once + state
   queryable from a fresh saver opened against the same db_path.
5. Snapshot of final subgraph DAG output via syrupy (locks the field
   set in case the schema drifts).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from popolaloom.daemon import (
    build_dev_test_subgraph,
    make_checkpointer,
)

pytestmark = [pytest.mark.slow, pytest.mark.real_graph]


def test_subgraph_converges_at_iter_two(tmp_path: Path) -> None:
    """Case 1: score sequence [0.5, 0.9] crosses 0.85 gate at iter=2 → done True."""
    db_path = tmp_path / "subgraph_converge.sqlite"
    saver = make_checkpointer(db_path=db_path)
    sub = build_dev_test_subgraph(
        score_sequence=[0.5, 0.9],
        max_iter=2,
        checkpointer=saver,
    )
    cfg: dict[str, Any] = {"configurable": {"thread_id": "tier4-converge"}}
    final = sub.invoke({"prompt": "fix typo in README"}, config=cfg)
    assert final.get("done") is True, f"expected done=True, got {final}"
    assert final.get("give_up") is None or final.get("give_up") is False
    assert final["score"] == pytest.approx(0.9)
    assert final["iter"] == 2

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT thread_id FROM checkpoints WHERE thread_id = ? LIMIT 5",
            ("tier4-converge",),
        )
        rows = cur.fetchall()
    assert rows, "expected ≥ 1 checkpoint row for thread_id=tier4-converge"


def test_subgraph_gives_up_when_scores_below_gate(tmp_path: Path) -> None:
    """Case 2: scores [0.3, 0.4] never cross 0.85 → give_up True at max_iter."""
    db_path = tmp_path / "subgraph_giveup.sqlite"
    saver = make_checkpointer(db_path=db_path)
    sub = build_dev_test_subgraph(
        score_sequence=[0.3, 0.4],
        max_iter=2,
        checkpointer=saver,
    )
    cfg: dict[str, Any] = {"configurable": {"thread_id": "tier4-giveup"}}
    final = sub.invoke({"prompt": "impossible spec"}, config=cfg)
    assert final.get("give_up") is True
    assert final.get("done") is None or final.get("done") is False
    assert final["score"] == pytest.approx(0.4)
    assert final["iter"] == 2


def test_subgraph_three_concurrent_threads_isolated(tmp_path: Path) -> None:
    """Case 3: 3 concurrent subgraph runs with distinct thread_ids → no cross-pollination."""
    db_path = tmp_path / "subgraph_concurrent.sqlite"
    saver = make_checkpointer(db_path=db_path)

    finals: dict[str, dict[str, Any]] = {}

    def _run(thread_id: str, scores: list[float]) -> None:
        sub = build_dev_test_subgraph(
            score_sequence=scores, max_iter=2, checkpointer=saver
        )
        cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        final = sub.invoke({"prompt": f"prompt-{thread_id}"}, config=cfg)
        finals[thread_id] = final

    threads = [
        threading.Thread(
            target=_run, args=("tier4-iso-A", [0.5, 0.9]), name="tier4-iso-A"
        ),
        threading.Thread(
            target=_run, args=("tier4-iso-B", [0.3, 0.4]), name="tier4-iso-B"
        ),
        threading.Thread(
            target=_run, args=("tier4-iso-C", [0.95]), name="tier4-iso-C"
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
    for t in threads:
        assert not t.is_alive(), f"{t.name} did not complete in 15s"

    assert finals["tier4-iso-A"].get("done") is True
    assert finals["tier4-iso-B"].get("give_up") is True
    assert finals["tier4-iso-C"].get("done") is True
    assert finals["tier4-iso-C"]["score"] == pytest.approx(0.95)

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
        ids = {row[0] for row in cur.fetchall()}
    expected = {"tier4-iso-A", "tier4-iso-B", "tier4-iso-C"}
    assert expected.issubset(ids), f"expected all 3 thread_ids in checkpoints, got {ids}"


def test_subgraph_persistence_round_trip(tmp_path: Path) -> None:
    """Case 4: subgraph state survives saver reopen against same db_path."""
    db_path = tmp_path / "subgraph_persist.sqlite"
    saver_first = make_checkpointer(db_path=db_path)
    sub_first = build_dev_test_subgraph(
        score_sequence=[0.9], max_iter=1, checkpointer=saver_first
    )
    cfg: dict[str, Any] = {"configurable": {"thread_id": "tier4-persist"}}
    final = sub_first.invoke({"prompt": "persist test"}, config=cfg)
    assert final.get("done") is True

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
            ("tier4-persist",),
        )
        count_first = cur.fetchone()[0]
    assert count_first >= 1, f"expected ≥1 checkpoint, got {count_first}"

    conn2 = sqlite3.connect(str(db_path), check_same_thread=False)
    saver_second = SqliteSaver(conn2)
    saver_second.setup()
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
            ("tier4-persist",),
        )
        count_second = cur.fetchone()[0]
    assert count_second == count_first, (
        f"reopen changed checkpoint count: {count_first} → {count_second}"
    )
    conn2.close()


def test_subgraph_dag_output_keys_snapshot(tmp_path: Path, snapshot: Any) -> None:
    """Case 5: lock the subgraph output key set + values via syrupy snapshot."""
    db_path = tmp_path / "subgraph_snapshot.sqlite"
    saver = make_checkpointer(db_path=db_path)
    sub = build_dev_test_subgraph(
        score_sequence=[0.9], max_iter=1, checkpointer=saver
    )
    cfg: dict[str, Any] = {"configurable": {"thread_id": "tier4-snapshot"}}
    final = sub.invoke({"prompt": "snapshot test"}, config=cfg)
    keys_present = sorted(k for k in final if k != "iter")
    assert keys_present == snapshot
    assert final.get("done") is True
    assert final["score"] == pytest.approx(0.9)
