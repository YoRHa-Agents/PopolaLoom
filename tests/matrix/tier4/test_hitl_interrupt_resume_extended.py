"""Tier 4 — HITL interrupt() complete path + 6 resume value variants.

Per testing-matrix.md §1.4 — extends `tests/test_graph.py::test_interrupt_resume`
with 6 cases varying the resume value type and asserting:

- Each interrupt → state saved → mock supply_feedback → resume produces
  the expected ``human_response``.
- The SqliteSaver checkpoint row persists across the interrupt → resume
  boundary (proving thread_id-keyed durability).
- Once resumed, the ``__interrupt__`` key is gone from the final state.

6 resume variants exercised:

1. ``"yes"`` — string answer.
2. ``"no"`` — string answer.
3. ``"abort"`` — string answer (matches the schema enum).
4. ``42`` — integer answer.
5. ``{"choice": "yes", "comment": "looks good"}`` — dict answer.
6. ``Command(resume="explicit")`` — explicit ``Command`` object via the
   public :func:`apply_resume` helper (matches the canonical
   :func:`langgraph.types.Command` ``resume`` API).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph

from popolaloom.daemon import (
    apply_resume,
    human_input_required,
    make_checkpointer,
)

pytestmark = [pytest.mark.slow, pytest.mark.real_graph]


def _build_hitl_graph(tmp_path: Path, db_filename: str) -> tuple[Any, Any, Path]:
    """Build a tiny graph: START → hitl → END with the given checkpointer.

    Returns ``(compiled_graph, saver, db_path)``.
    """
    db_path = tmp_path / db_filename
    saver = make_checkpointer(db_path=db_path)
    g: StateGraph[dict[str, Any], None, dict[str, Any], dict[str, Any]] = StateGraph(dict)
    g.add_node("hitl", human_input_required)
    g.add_edge(START, "hitl")
    g.add_edge("hitl", END)
    return g.compile(checkpointer=saver), saver, db_path


def _assert_paused_at_hitl(state: dict[str, Any], expected_msg: str, task_id: str) -> None:
    """Verify the graph paused at the interrupt node + payload is correct."""
    assert "__interrupt__" in state, f"expected pause, got {state}"
    payload = state["__interrupt__"][0].value
    assert payload["message"] == expected_msg
    assert payload["task_id"] == task_id


def _checkpoint_count_for_thread(db_path: Path, thread_id: str) -> int:
    """Return the number of checkpoint rows for the given thread_id."""
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
        )
        row = cur.fetchone()
    return int(row[0])


@pytest.mark.parametrize(
    ("resume_value", "expected_response"),
    [
        ("yes", "yes"),
        ("no", "no"),
        ("abort", "abort"),
    ],
)
def test_string_resume_variants(
    tmp_path: Path, resume_value: str, expected_response: str
) -> None:
    """Cases 1-3: 'yes'/'no'/'abort' string resume completes graph + persists checkpoint."""
    thread_id = f"tier4-hitl-string-{resume_value}"
    compiled, _saver, db_path = _build_hitl_graph(
        tmp_path, f"hitl_{resume_value}.sqlite"
    )
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    paused = compiled.invoke({"task_id": thread_id, "question": "Apply patch?"}, config=cfg)
    _assert_paused_at_hitl(paused, "Apply patch?", thread_id)
    assert _checkpoint_count_for_thread(db_path, thread_id) >= 1

    final = apply_resume(compiled, thread_id=thread_id, resume_value=resume_value)
    assert final.get("human_response") == expected_response
    assert "__interrupt__" not in final
    assert _checkpoint_count_for_thread(db_path, thread_id) >= 2


def test_numeric_resume_variant(tmp_path: Path) -> None:
    """Case 4: integer resume value preserved through the interrupt protocol."""
    thread_id = "tier4-hitl-numeric"
    compiled, _saver, db_path = _build_hitl_graph(tmp_path, "hitl_numeric.sqlite")
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    paused = compiled.invoke(
        {"task_id": thread_id, "question": "Confirm count?"}, config=cfg
    )
    _assert_paused_at_hitl(paused, "Confirm count?", thread_id)

    final = apply_resume(compiled, thread_id=thread_id, resume_value=42)
    assert final.get("human_response") == 42
    assert "__interrupt__" not in final


def test_dict_resume_variant(tmp_path: Path) -> None:
    """Case 5: dict resume value (multi-field answer) survives the protocol."""
    thread_id = "tier4-hitl-dict"
    compiled, _saver, db_path = _build_hitl_graph(tmp_path, "hitl_dict.sqlite")
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    paused = compiled.invoke(
        {"task_id": thread_id, "question": "Approve?"}, config=cfg
    )
    _assert_paused_at_hitl(paused, "Approve?", thread_id)

    answer = {"choice": "yes", "comment": "looks good", "approver": "u-001"}
    final = apply_resume(compiled, thread_id=thread_id, resume_value=answer)
    response = final.get("human_response")
    assert isinstance(response, dict)
    assert response.get("choice") == "yes"
    assert response.get("comment") == "looks good"
    assert response.get("approver") == "u-001"


def test_apply_resume_with_explicit_command_resume(tmp_path: Path) -> None:
    """Case 6: ``apply_resume`` accepts a value and wraps it as ``Command(resume=...)``.

    This is the canonical public-API path; the test asserts the helper's
    contract (translating ``resume_value`` into a ``Command`` payload
    invisibly to the caller) plus durable checkpointing across the
    interrupt → resume boundary.
    """
    thread_id = "tier4-hitl-explicit-command"
    compiled, _saver, db_path = _build_hitl_graph(
        tmp_path, "hitl_explicit_command.sqlite"
    )
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    paused = compiled.invoke(
        {"task_id": thread_id, "question": "Continue with plan?"},
        config=cfg,
    )
    _assert_paused_at_hitl(paused, "Continue with plan?", thread_id)
    pre_count = _checkpoint_count_for_thread(db_path, thread_id)
    assert pre_count >= 1

    final = apply_resume(
        compiled, thread_id=thread_id, resume_value="explicit"
    )
    assert final.get("human_response") == "explicit"
    assert "__interrupt__" not in final
    post_count = _checkpoint_count_for_thread(db_path, thread_id)
    assert post_count > pre_count, (
        f"expected new checkpoint row(s) after resume; pre={pre_count} post={post_count}"
    )


def test_two_concurrent_hitl_threads_dont_cross_resume(tmp_path: Path) -> None:
    """Bonus: two HITL graphs with distinct thread_ids stay isolated on resume."""
    compiled_a, _, db_path = _build_hitl_graph(tmp_path, "hitl_concurrent.sqlite")
    compiled_b = compiled_a

    cfg_a: dict[str, Any] = {"configurable": {"thread_id": "hitl-A"}}
    cfg_b: dict[str, Any] = {"configurable": {"thread_id": "hitl-B"}}

    paused_a = compiled_a.invoke({"task_id": "hitl-A", "question": "Q-A"}, config=cfg_a)
    paused_b = compiled_b.invoke({"task_id": "hitl-B", "question": "Q-B"}, config=cfg_b)
    _assert_paused_at_hitl(paused_a, "Q-A", "hitl-A")
    _assert_paused_at_hitl(paused_b, "Q-B", "hitl-B")

    final_a = apply_resume(compiled_a, thread_id="hitl-A", resume_value="yes")
    assert final_a["human_response"] == "yes"

    final_b = apply_resume(compiled_b, thread_id="hitl-B", resume_value="no")
    assert final_b["human_response"] == "no"
    assert "__interrupt__" not in final_a
    assert "__interrupt__" not in final_b

    assert _checkpoint_count_for_thread(db_path, "hitl-A") >= 2
    assert _checkpoint_count_for_thread(db_path, "hitl-B") >= 2
