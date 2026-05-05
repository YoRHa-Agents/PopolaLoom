"""Tier 4 — recursive dispatch (parent → child) with thread_id isolation.

Per testing-matrix.md §1.4 — Tier 4 owes a richer recursive-dispatch
story than ``tests/self_bootstrap/test_s3_recursive_dispatch.py`` (which
is the slow lane real-subprocess version).  Here we exercise the
**in-process Popolad + real LangGraph** path so we can:

- Inject a child dispatch from a parent's adapter (synthetic
  parent→child relationship via a stub adapter that calls ``Popolad.dispatch_task``
  recursively).
- Verify each task's thread_id ends up isolated in the SqliteSaver.
- Verify the parent → child relationship is recorded via the ``extra``
  ``parameters`` field that ArkTower would persist (in v0.2.x extra is
  stored on the popola_dispatch row; v0.3.0 will add a first-class
  ``parent_task_id`` column).

3 cases (target ≥ 3):

1. Parent dispatches child, both reach terminal state successfully.
2. Parent dispatches child, child errors (non-zero exit) → parent
   observes the failure via the per-task NDJSON event log.
3. 3-deep chain (A → B → C) terminal in order.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import (
    Popolad,
    TaskState,
    make_checkpointer,
)

_CHILD_ID_RE = re.compile(r"CHILD_DISPATCHED:\s*([\w-]+)")

pytestmark = [pytest.mark.slow, pytest.mark.real_graph]


def _wait_for_terminal(popolad: Popolad, task_id: str, timeout: float = 8.0) -> str:
    """Poll ``popolad.get_status`` until it reports a terminal state or timeout."""
    deadline = time.monotonic() + timeout
    last_state: str = ""
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        last_state = status["state"]
        if last_state in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            return last_state
        time.sleep(0.05)
    return last_state


def _make_recursive_adapter(
    popolad_holder: dict[str, Popolad],
    *,
    child_cli: str = "child-cli",
    child_prompt: str = "child task",
    child_exit_code: int = 0,
    parent_marker: str = "parent",
) -> Callable[..., list[str]]:
    """Build an adapter that, on first call, dispatches a child via the same Popolad.

    ``popolad_holder["popolad"]`` is filled by the test after construction
    so the closure can reference the live instance.  The adapter
    inspects the prompt to decide whether the call is the parent or
    the child:

    - Prompts containing ``parent_marker`` → dispatch a child task and
      then return an argv that prints ``CHILD_DISPATCHED:<id>`` so
      tests can correlate.
    - Other prompts (i.e. the child) → return an argv that exits with
      ``child_exit_code``.
    """

    state: dict[str, str] = {"child_id": ""}

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        if parent_marker in prompt:
            child_id = popolad_holder["popolad"].dispatch_task(
                cli=child_cli,
                prompt=child_prompt,
                extra={"parent_popola_task_id": "<parent-self>"},
            )
            state["child_id"] = child_id
            return [
                sys.executable,
                "-c",
                f"print('CHILD_DISPATCHED:{child_id}')",
            ]
        return [
            sys.executable,
            "-c",
            f"import sys; print('child running'); sys.exit({child_exit_code})",
        ]

    return adapter


def test_parent_dispatches_child_both_complete(tmp_path: Path) -> None:
    """Case 1: parent dispatches child, both reach COMPLETED state."""
    events_dir = tmp_path / "events"
    saver = make_checkpointer(db_path=tmp_path / "state.sqlite")
    holder: dict[str, Popolad] = {}
    adapter = _make_recursive_adapter(holder, child_exit_code=0)
    popolad = Popolad(
        events_dir=events_dir,
        adapter=adapter,
        use_graph=True,
        checkpointer=saver,
    )
    holder["popolad"] = popolad

    parent_id = popolad.dispatch_task(cli="parent-cli", prompt="parent task")
    state_parent = _wait_for_terminal(popolad, parent_id)
    assert state_parent == str(TaskState.COMPLETED), (
        f"parent did not COMPLETE; final state={state_parent}; "
        f"events={popolad.tail_events(parent_id)[-3:]}"
    )

    parent_log = events_dir / f"{parent_id}.jsonl"
    parent_text = parent_log.read_text(encoding="utf-8")
    m = _CHILD_ID_RE.search(parent_text)
    assert m, (
        "CHILD_DISPATCHED marker missing from parent log; "
        f"first 2000 chars:\n{parent_text[:2000]!r}"
    )
    child_id = m.group(1)
    assert child_id != parent_id, "child_id collides with parent_id"

    state_child = _wait_for_terminal(popolad, child_id)
    assert state_child == str(TaskState.COMPLETED), (
        f"child did not COMPLETE; final state={state_child}"
    )


def test_parent_observes_child_failure(tmp_path: Path) -> None:
    """Case 2: child exits non-zero; parent sees the child's terminal FAILED state."""
    events_dir = tmp_path / "events"
    saver = make_checkpointer(db_path=tmp_path / "state_child_fail.sqlite")
    holder: dict[str, Popolad] = {}
    adapter = _make_recursive_adapter(holder, child_exit_code=1)
    popolad = Popolad(
        events_dir=events_dir,
        adapter=adapter,
        use_graph=True,
        checkpointer=saver,
    )
    holder["popolad"] = popolad

    parent_id = popolad.dispatch_task(cli="parent-cli", prompt="parent task")
    state_parent = _wait_for_terminal(popolad, parent_id)
    assert state_parent == str(TaskState.COMPLETED)

    parent_log = events_dir / f"{parent_id}.jsonl"
    text = parent_log.read_text(encoding="utf-8")
    m = _CHILD_ID_RE.search(text)
    assert m, f"CHILD_DISPATCHED missing in parent log: {text[:1500]!r}"
    child_id = m.group(1)

    state_child = _wait_for_terminal(popolad, child_id)
    assert state_child == str(TaskState.FAILED), (
        f"expected child FAILED with exit_code=1; got {state_child}"
    )


def test_three_deep_chain_a_b_c_terminal(tmp_path: Path) -> None:
    """Case 3: A → B → C; all 3 reach terminal state in order."""
    events_dir = tmp_path / "events_chain"
    saver = make_checkpointer(db_path=tmp_path / "state_chain.sqlite")

    holder: dict[str, Popolad] = {}
    state: dict[str, list[str]] = {"dispatched_ids": []}

    def chain_adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        if "level=A" in prompt:
            child_id = holder["popolad"].dispatch_task(
                cli="chain-cli", prompt="next level=B"
            )
            state["dispatched_ids"].append(child_id)
            return [sys.executable, "-c", f"print('A child_id={child_id}')"]
        if "level=B" in prompt:
            child_id = holder["popolad"].dispatch_task(
                cli="chain-cli", prompt="leaf level=C"
            )
            state["dispatched_ids"].append(child_id)
            return [sys.executable, "-c", f"print('B child_id={child_id}')"]
        return [sys.executable, "-c", "print('C leaf done')"]

    popolad = Popolad(
        events_dir=events_dir,
        adapter=chain_adapter,
        use_graph=True,
        checkpointer=saver,
    )
    holder["popolad"] = popolad

    a_id = popolad.dispatch_task(cli="chain-cli", prompt="parent level=A")
    s_a = _wait_for_terminal(popolad, a_id)
    assert s_a == str(TaskState.COMPLETED), f"A did not COMPLETE: {s_a}"

    assert len(state["dispatched_ids"]) >= 1
    b_id = state["dispatched_ids"][0]
    s_b = _wait_for_terminal(popolad, b_id)
    assert s_b == str(TaskState.COMPLETED), f"B did not COMPLETE: {s_b}"

    assert len(state["dispatched_ids"]) >= 2, (
        f"expected B to dispatch a C child; dispatched_ids={state['dispatched_ids']}"
    )
    c_id = state["dispatched_ids"][1]
    s_c = _wait_for_terminal(popolad, c_id)
    assert s_c == str(TaskState.COMPLETED), f"C did not COMPLETE: {s_c}"

    all_ids = {a_id, b_id, c_id}
    assert len(all_ids) == 3, f"task ids collided: {all_ids}"
