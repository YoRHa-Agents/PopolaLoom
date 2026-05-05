"""Tests for popolad LangGraph integration (v0.2.0 Stage B B1-B5).

Coverage targets (≥ 6 cases per task spec):

1. ``test_state_schema_validates_required_fields`` — Pydantic v2 raises
   on missing ``task_id`` / ``cli`` / ``prompt``.
2. ``test_main_graph_single_dispatch_flow`` — main graph invoke with
   mock callbacks emits ``graph.step`` for dispatch / spawn / wait /
   emit_terminal in order; final ``state.status == "completed"``.
3. ``test_subgraph_dev_test_converges`` — score sequence ``[0.5, 0.9]``
   with max_iter=2 → ``state.done=True`` at iter=2.
4. ``test_subgraph_dev_test_gives_up`` — score sequence ``[0.3, 0.4]``
   with max_iter=2 → ``state.give_up=True``.
5. ``test_interrupt_resume`` — graph with interrupt node pauses on first
   invoke; ``apply_resume("yes")`` continues to END; SqliteSaver thread_id
   checkpoint is queryable across calls.
6. ``test_thread_id_isolation`` — two concurrent invokes with distinct
   thread_ids land in independent checkpoint rows.

Bonus (pre-existing wiring smoke):

7. ``test_popolad_dispatch_via_graph_emits_graph_steps`` — full Popolad
   dispatch with ``POPOLA_USE_GRAPH=1`` (the default) produces the
   legacy NDJSON stream **plus** at least one ``graph.step`` event.
8. ``test_popolad_use_graph_off_skips_graph_steps`` — same setup with
   ``POPOLA_USE_GRAPH=0`` falls back to the legacy direct path
   (no ``graph.step`` events).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from popolaloom.daemon import (
    GraphCallbacks,
    GraphTaskState,
    Popolad,
    TaskState,
    apply_resume,
    build_dev_test_subgraph,
    build_main_graph,
    human_input_required,
    make_checkpointer,
)

# ── helpers ──────────────────────────────────────────────────────────────


class _RecordingCallbacks:
    """Mock :class:`GraphCallbacks` impl that records all calls.

    ``adapter_build_command`` returns a deterministic argv;
    ``supervisor_spawn`` returns a fake pid; ``supervisor_wait`` returns
    the (exit_code, events_count) it was constructed with;
    ``event_log_emit`` accumulates envelopes in ``self.events``.
    """

    def __init__(self, exit_code: int = 0, events_count: int = 7) -> None:
        self._exit_code = exit_code
        self._events_count = events_count
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.spawn_calls: list[tuple[str, list[str]]] = []
        self.wait_calls: list[str] = []
        self.adapter_calls: list[tuple[str, str]] = []

    def adapter_build_command(
        self,
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None,
    ) -> list[str]:
        self.adapter_calls.append((cli, prompt))
        return ["mocked", cli, prompt]

    def supervisor_spawn(
        self,
        task_id: str,
        cmd: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
    ) -> int:
        self.spawn_calls.append((task_id, cmd))
        return 4242

    def supervisor_wait(self, task_id: str) -> tuple[int, int]:
        self.wait_calls.append(task_id)
        return self._exit_code, self._events_count

    def event_log_emit(
        self,
        task_id: str,
        type_: str,
        data: dict[str, Any],
    ) -> None:
        self.events.append((task_id, type_, data))


# ── 1. State schema validation ──────────────────────────────────────────


def test_state_schema_validates_required_fields() -> None:
    """Pydantic v2 must raise ``ValidationError`` if any required field is absent."""
    # All present → ok
    s = GraphTaskState(task_id="t-1", cli="cursor", prompt="hello")
    assert s.task_id == "t-1"
    assert s.status == "pending"
    assert s.cwd is None

    # Missing task_id
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(cli="cursor", prompt="hi")  # type: ignore[call-arg]
    assert "task_id" in str(excinfo.value)

    # Missing cli
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(task_id="t-2", prompt="hi")  # type: ignore[call-arg]
    assert "cli" in str(excinfo.value)

    # Missing prompt
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(task_id="t-3", cli="cursor")  # type: ignore[call-arg]
    assert "prompt" in str(excinfo.value)


# ── 2. Main graph happy path ─────────────────────────────────────────────


def test_main_graph_single_dispatch_flow() -> None:
    """Build main graph with mock callbacks → invoke → assert ordering + final state."""
    callbacks = _RecordingCallbacks(exit_code=0, events_count=11)
    assert isinstance(callbacks, GraphCallbacks)

    graph = build_main_graph(callbacks=callbacks, checkpointer=None)
    initial = GraphTaskState(
        task_id="t-flow",
        cli="cursor",
        prompt="implement hello.py",
        cmd=["echo", "hi"],
    )
    final = graph.invoke(initial)

    assert final["status"] == "completed"
    assert final["exit_code"] == 0
    assert final["events_count"] == 11
    assert final["subprocess_pid"] == 4242
    assert final["started_at"] is not None
    assert final["completed_at"] is not None
    assert final["final_message"] == "task completed successfully"

    node_order = [data["node"] for (_, type_, data) in callbacks.events if type_ == "graph.step"]
    assert node_order == ["dispatch", "spawn", "wait", "emit_terminal"], (
        f"graph.step nodes out of order: {node_order}"
    )
    assert callbacks.spawn_calls == [("t-flow", ["echo", "hi"])]
    assert callbacks.wait_calls == ["t-flow"]


def test_main_graph_failed_path_sets_status_failed() -> None:
    """Non-zero exit_code → status='failed'; final_message references the code."""
    callbacks = _RecordingCallbacks(exit_code=42, events_count=3)
    graph = build_main_graph(callbacks=callbacks, checkpointer=None)
    initial = GraphTaskState(
        task_id="t-fail",
        cli="claude",
        prompt="will fail",
        cmd=["false"],
    )
    final = graph.invoke(initial)

    assert final["status"] == "failed"
    assert final["exit_code"] == 42
    assert final["final_message"] is not None
    assert "exit_code=42" in final["final_message"]


def test_main_graph_dispatch_node_validates_state() -> None:
    """Empty cli/prompt at runtime must raise from dispatch_node (No Silent Failures)."""
    callbacks = _RecordingCallbacks()
    graph = build_main_graph(callbacks=callbacks, checkpointer=None)

    # Construct a state that has empty cli — Pydantic accepts but our node rejects.
    initial = GraphTaskState(
        task_id="t-bad",
        cli="",  # empty after strip
        prompt="anything",
        cmd=["echo"],
    )
    with pytest.raises(ValueError, match="state.cli is required"):
        graph.invoke(initial)


# ── 3. Subgraph converges ────────────────────────────────────────────────


def test_subgraph_dev_test_converges() -> None:
    """Score sequence [0.5, 0.9] → done=True at iter=2."""
    sub = build_dev_test_subgraph(score_sequence=[0.5, 0.9], max_iter=2)
    final = sub.invoke({"prompt": "fix typo in README"})
    assert final.get("done") is True
    assert final.get("give_up") is None or final.get("give_up") is False
    assert final["score"] == pytest.approx(0.9)
    assert final["iter"] == 2


# ── 4. Subgraph gives up ─────────────────────────────────────────────────


def test_subgraph_dev_test_gives_up() -> None:
    """Score sequence [0.3, 0.4] → give_up=True (never crosses 0.85)."""
    sub = build_dev_test_subgraph(score_sequence=[0.3, 0.4], max_iter=2)
    final = sub.invoke({"prompt": "impossible spec"})
    assert final.get("give_up") is True
    assert final.get("done") is None or final.get("done") is False
    assert final["score"] == pytest.approx(0.4)
    assert final["iter"] == 2


# ── 5. Interrupt + resume + SqliteSaver persistence ─────────────────────


def test_interrupt_resume(tmp_path: Path) -> None:
    """Graph with interrupt node pauses on invoke; apply_resume completes it.

    Also asserts the SqliteSaver checkpoint row is queryable across both
    calls (proving thread_id persistence per ADR-0002 §2.1).
    """
    db_path = tmp_path / "interrupt_state.sqlite"
    saver = make_checkpointer(db_path=db_path)

    g: StateGraph[dict[str, Any], None, dict[str, Any], dict[str, Any]] = StateGraph(dict)
    g.add_node("hitl", human_input_required)
    g.add_edge(START, "hitl")
    g.add_edge("hitl", END)
    compiled = g.compile(checkpointer=saver)

    cfg: dict[str, Any] = {"configurable": {"thread_id": "interrupt-1"}}
    paused = compiled.invoke(
        {"task_id": "interrupt-1", "question": "Apply patch?"}, config=cfg
    )
    assert "__interrupt__" in paused, f"expected pause, got {paused}"
    interrupt_payload = paused["__interrupt__"][0].value
    assert interrupt_payload["message"] == "Apply patch?"
    assert interrupt_payload["task_id"] == "interrupt-1"

    # Verify the SqliteSaver wrote at least one checkpoint row for this thread.
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT thread_id FROM checkpoints WHERE thread_id = ? LIMIT 5",
            ("interrupt-1",),
        )
        rows = cur.fetchall()
    assert rows, "no checkpoint row found for interrupt-1 thread_id"
    assert all(r[0] == "interrupt-1" for r in rows)

    final = apply_resume(compiled, thread_id="interrupt-1", resume_value="yes")
    assert final.get("human_response") == "yes"
    assert "__interrupt__" not in final


# ── 6. Thread isolation across concurrent invokes ───────────────────────


def test_thread_id_isolation(tmp_path: Path) -> None:
    """Two concurrent invokes with distinct thread_ids → independent checkpoints."""
    db_path = tmp_path / "isolation_state.sqlite"
    saver: SqliteSaver = make_checkpointer(db_path=db_path)

    callbacks_a = _RecordingCallbacks(exit_code=0, events_count=2)
    callbacks_b = _RecordingCallbacks(exit_code=1, events_count=5)

    graph_a = build_main_graph(callbacks=callbacks_a, checkpointer=saver)
    graph_b = build_main_graph(callbacks=callbacks_b, checkpointer=saver)

    state_a = GraphTaskState(task_id="iso-a", cli="cursor", prompt="A", cmd=["a"])
    state_b = GraphTaskState(task_id="iso-b", cli="claude", prompt="B", cmd=["b"])

    final_a: dict[str, Any] = {}
    final_b: dict[str, Any] = {}
    cfg_a: dict[str, Any] = {"configurable": {"thread_id": "iso-a"}}
    cfg_b: dict[str, Any] = {"configurable": {"thread_id": "iso-b"}}

    def _run_a() -> None:
        final_a.update(graph_a.invoke(state_a, config=cfg_a))

    def _run_b() -> None:
        final_b.update(graph_b.invoke(state_b, config=cfg_b))

    ta = threading.Thread(target=_run_a)
    tb = threading.Thread(target=_run_b)
    ta.start()
    tb.start()
    ta.join(timeout=10.0)
    tb.join(timeout=10.0)
    assert not ta.is_alive() and not tb.is_alive()

    assert final_a["status"] == "completed"
    assert final_a["exit_code"] == 0
    assert final_b["status"] == "failed"
    assert final_b["exit_code"] == 1

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
        ids = {row[0] for row in cur.fetchall()}
    assert "iso-a" in ids
    assert "iso-b" in ids
    assert ids.issuperset({"iso-a", "iso-b"})


# ── 7. Popolad wiring smoke (graph default ON) ──────────────────────────


def test_popolad_dispatch_via_graph_emits_graph_steps(tmp_path: Path) -> None:
    """End-to-end Popolad dispatch with default ``POPOLA_USE_GRAPH=1``.

    Asserts the legacy NDJSON contract is preserved (``task.dispatched`` /
    ``process.started`` / ``task.completed`` still present) and that at
    least one ``graph.step`` event is added (AC #5 from Stage B spec).
    Uses tmp_path-scoped checkpointer to keep ``~/.popola/state.sqlite``
    pristine.
    """
    events_dir = tmp_path / "events"

    def fake_adapter(cli: str, prompt: str, cwd: Path | None) -> list[str]:
        return [sys.executable, "-c", f"print({prompt!r})"]

    isolated_checkpointer = make_checkpointer(db_path=tmp_path / "state.sqlite")
    popolad = Popolad(
        events_dir=events_dir,
        adapter=fake_adapter,
        use_graph=True,
        checkpointer=isolated_checkpointer,
    )
    task_id = popolad.dispatch_task(cli="testcli", prompt="graph-on")
    assert task_id.startswith("testcli-")

    # Poll for both terminal state AND emit_terminal graph.step in the same loop
    # so we don't stop reading too early when the graph thread is still running
    # (race between supervisor's wait-thread setting state=COMPLETED and the
    # graph thread writing the emit_terminal graph.step event).
    deadline = time.monotonic() + 10.0
    events: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        events = popolad.tail_events(task_id)
        types = [ev["type"] for ev in events]
        graph_step_nodes = [
            ev["data"].get("node")
            for ev in events
            if ev["type"] == "graph.step"
        ]
        terminal = status["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}
        if terminal and "task.completed" in types and "emit_terminal" in graph_step_nodes:
            break
        time.sleep(0.05)
    else:
        pytest.fail(
            f"task did not reach terminal+emit_terminal in 10s: state={status['state']} "
            f"types={types} graph_steps={graph_step_nodes}"
        )

    types = [ev["type"] for ev in events]
    assert types[0] == "task.dispatched"
    assert "process.started" in types
    assert "task.completed" in types

    graph_steps = [ev for ev in events if ev["type"] == "graph.step"]
    assert graph_steps, f"expected at least one graph.step event, got types={types}"

    nodes_seen = [ev["data"]["node"] for ev in graph_steps]
    assert nodes_seen[0] == "dispatch"
    assert "emit_terminal" in nodes_seen


def test_popolad_use_graph_off_skips_graph_steps(tmp_path: Path) -> None:
    """``use_graph=False`` selects the legacy direct path — no ``graph.step``."""
    events_dir = tmp_path / "events_legacy"

    def fake_adapter(cli: str, prompt: str, cwd: Path | None) -> list[str]:
        return [sys.executable, "-c", f"print({prompt!r})"]

    popolad = Popolad(events_dir=events_dir, adapter=fake_adapter, use_graph=False)
    task_id = popolad.dispatch_task(cli="legacycli", prompt="graph-off")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        if status["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            break
        time.sleep(0.05)

    events = popolad.tail_events(task_id)
    types = [ev["type"] for ev in events]
    assert "task.completed" in types
    assert all(t != "graph.step" for t in types), f"unexpected graph.step in legacy: {types}"
