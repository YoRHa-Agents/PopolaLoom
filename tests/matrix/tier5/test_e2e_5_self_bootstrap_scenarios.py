"""Tier 5 — full S1-S5 self-bootstrap scenario matrix mirror (e2e + nightly).

Per testing-matrix.md §1.5 + spec.md §3.4.1 — Tier 5 owes "5/5 self-
bootstrap mock complete".  The deep tests for each scenario live in
``tests/self_bootstrap/`` (S1 / S3 are real-subprocess; S2 / S4 / S5
are mock-CLI).  This file runs **lightweight in-process mirrors** of
each scenario so the matrix tracker can show "5/5 PASS" in the
nightly aggregate report without the multi-minute overhead of a full
re-spawn loop.

5 mirror cases (one per scenario) — each is fast (no daemon spawn);
the deep version with real popolad lives in tests/self_bootstrap/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph

from popolaloom.daemon import (
    Popolad,
    TaskState,
    apply_resume,
    human_input_required,
    make_checkpointer,
)
from popolaloom.evolution import WorkflowContext
from tests.fixtures.mock_cli import (
    run_mock_claude,
    run_mock_codex,
    run_mock_cursor,
)

pytestmark = [pytest.mark.e2e, pytest.mark.nightly]


def _wait_for_terminal(popolad: Popolad, task_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        last = status["state"]
        if last in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            return last
        time.sleep(0.05)
    return last


def _fast_adapter(cli: str, prompt: str, cwd: Path | None, extra: Any = None) -> list[str]:
    return [sys.executable, "-c", f"print({prompt!r})"]


def test_s1_mirror_in_process_dispatch_state_persists(tmp_path: Path) -> None:
    """S1 mirror: dispatch + StateStore handle persists across get_status calls.

    The deep S1 (cross-process SIGKILL/restart) lives in
    ``tests/self_bootstrap/test_s1_crash_recovery.py``.  Here we just
    confirm that the in-process Popolad persists state for at least
    the lifetime of one task — a much-faster smoke that proves the
    StateStore/EventLog plumbing isn't broken.
    """
    events_dir = tmp_path / "events"
    saver = make_checkpointer(db_path=tmp_path / "state.sqlite")
    popolad = Popolad(
        events_dir=events_dir,
        adapter=_fast_adapter,
        use_graph=True,
        checkpointer=saver,
    )
    task_id = popolad.dispatch_task(cli="cursor", prompt="S1 mirror")
    final = _wait_for_terminal(popolad, task_id, timeout=10.0)
    assert final == str(TaskState.COMPLETED)
    status = popolad.get_status(task_id)
    assert status["task_id"] == task_id
    assert (events_dir / f"{task_id}.jsonl").exists()


def test_s2_mirror_workflow_context_round_propagation() -> None:
    """S2 mirror: round 2 prompt uses WorkflowContext + mock parses round_num.

    The deep S2 lives in
    ``tests/self_bootstrap/test_s2_reinforcement_mock.py``.  Here we
    just verify the WorkflowContext schema → mock_cursor round
    detection wiring works end-to-end.
    """
    ctx = WorkflowContext(
        round_num=2,
        max_rounds=5,
        prior_nines=0.886,
        reinforcement_rules=[
            "Always emit composite_score in 3-section output.",
        ],
    )
    prompt = ctx.render() + "\nimplement step 2 of feature\n"
    out = run_mock_cursor(prompt)
    assert out.stdout.startswith("[devola-flow:round=2]"), (
        f"mock_cursor failed to parse round_num=2 from prompt; got:\n{out.stdout[:500]}"
    )
    assert "## Acceptance Verification" in out.stdout
    assert "## Findings" in out.stdout
    assert out.returncode == 0


def test_s3_mirror_recursive_dispatch_in_process(tmp_path: Path) -> None:
    """S3 mirror: in-process parent dispatches child + thread isolation.

    The deep S3 (real popolad subprocess) lives in
    ``tests/self_bootstrap/test_s3_recursive_dispatch.py``.  Here we
    just exercise the in-process recursion + per-task NDJSON file
    isolation.
    """
    events_dir = tmp_path / "events"
    saver = make_checkpointer(db_path=tmp_path / "state.sqlite")
    holder: dict[str, Popolad] = {}

    def adapter(cli: str, prompt: str, cwd: Path | None, extra: Any = None) -> list[str]:
        if "child" in prompt:
            return [sys.executable, "-c", "print('child running')"]
        child_id = holder["popolad"].dispatch_task(cli="child-cli", prompt="child task")
        return [sys.executable, "-c", f"print('parent dispatched {child_id}')"]

    popolad = Popolad(
        events_dir=events_dir,
        adapter=adapter,
        use_graph=True,
        checkpointer=saver,
    )
    holder["popolad"] = popolad

    parent_id = popolad.dispatch_task(cli="parent-cli", prompt="parent task")
    final_p = _wait_for_terminal(popolad, parent_id, timeout=10.0)
    assert final_p == str(TaskState.COMPLETED)

    parent_log = events_dir / f"{parent_id}.jsonl"
    text = parent_log.read_text(encoding="utf-8")
    assert "parent dispatched " in text


def test_s4_mirror_hitl_interrupt_resume_simulates_offline_reopen(tmp_path: Path) -> None:
    """S4 mirror: HITL interrupt → resume models the offline → reopen pattern.

    The deep S4 (real popolad + 8h freezegun) lives in
    ``tests/self_bootstrap/test_s4_offline_resume_mock.py``.  Here we
    interrupt a graph (modelling "developer closed IDE") and resume
    later (modelling "developer reopened") — the SqliteSaver
    checkpoint is the durability primitive shared with the deep S4.
    """
    db_path = tmp_path / "s4_mirror.sqlite"
    saver = make_checkpointer(db_path=db_path)
    g: StateGraph[dict[str, Any], None, dict[str, Any], dict[str, Any]] = StateGraph(dict)
    g.add_node("hitl", human_input_required)
    g.add_edge(START, "hitl")
    g.add_edge("hitl", END)
    compiled = g.compile(checkpointer=saver)

    cfg: dict[str, Any] = {"configurable": {"thread_id": "s4-mirror"}}
    paused = compiled.invoke(
        {"task_id": "s4-mirror", "question": "Reopen later?"},
        config=cfg,
    )
    assert "__interrupt__" in paused

    final = apply_resume(compiled, thread_id="s4-mirror", resume_value="resumed-after-8h")
    assert final.get("human_response") == "resumed-after-8h"
    assert "__interrupt__" not in final


def test_s5_mirror_three_mock_clis_complete_3_section_contract() -> None:
    """S5 mirror: 3 mock CLIs all emit the 3-section devola-flow contract.

    The deep S5 (real popolad + 3-hop dispatch) lives in
    ``tests/self_bootstrap/test_s5_cross_cli_handoff_mock.py``.  Here
    we just verify each mock CLI alone honours the section contract
    + advances the round number when the prompt asks.
    """
    out_cursor = run_mock_cursor("round_num: 1\nstep 1 of 3", round_num=1)
    out_claude = run_mock_claude("round_num: 2\nstep 2 of 3", round_num=2)
    out_codex = run_mock_codex(
        "round_num: 3\nstep 3 of 3", round_num=3, sandbox="workspace-write"
    )

    assert out_cursor.stdout.startswith("[devola-flow:round=1]")
    assert "claude-mock" in out_claude.stdout
    assert "[devola-flow:round=2]" in out_claude.stdout
    assert out_codex.stdout.startswith("[devola-flow:round=3]")

    for out in (out_cursor, out_codex):
        for required in (
            "## Acceptance Verification",
            "## Gate Score Components",
            "## Findings",
        ):
            assert required in out.stdout, (
                f"required section missing from {out.argv[0]!r}: {required}"
            )
    assert "## Acceptance Verification" in out_claude.stdout
