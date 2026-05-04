"""LangGraph StateGraph for popolad dispatch flow (v0.2.0 Stage B B1).

Wraps the existing ``dispatch → spawn → wait → emit_terminal`` flow in a
LangGraph :class:`StateGraph` so that:

- Every dispatch produces a checkpointed thread keyed by
  ``thread_id = task_id`` (ADR-0002 §2.1)
- Subgraph composition (Gen-Verifier dev↔test loop in
  :mod:`popolaloom.daemon.subgraph_dev_test`) reuses the same
  ``BaseCheckpointSaver`` namespace
- HITL ``interrupt() + Command(resume=...)`` is a first-class primitive
  (see :mod:`popolaloom.daemon.interrupt`)
- ``graph.step`` events join the existing CloudEvents stream so consumers
  see node-level timing in addition to subprocess events (AC #5)

Boundary contract (ADR-0002 §2.4 reds):

- **AP-1**: only the main DAG ``dispatch → spawn → wait → emit_terminal``
  is here; cycles live in subgraphs. We forbid adding self-edges to the
  main graph nodes.
- **AP-2**: there is no ``interrupt()`` call in this main graph (HITL goes
  through a separate node — see :mod:`popolaloom.daemon.interrupt`); side
  effects on each node (state writes, supervisor.spawn) are *idempotent*
  in the sense that the upstream :class:`Popolad` does its own per-task
  registration before the graph starts.
- **AP-4 / AP-5**: all node functions are deterministic + read only from
  ``state``; no ``time.time()`` / ``random.random()`` inside; the
  ``started_at`` / ``completed_at`` timestamps come from
  :func:`datetime.now` which is acceptable because they are *outputs*
  not gating-decision inputs.

Workspace rules honored:

- *No Silent Failures*: node errors raise out of the node function;
  callers (Popolad._run_graph_for_task) catch and translate to
  ``state.status='failed'``; every catch logs.
- *Mandatory Verification*: see ``tests/test_graph.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    _Saver = BaseCheckpointSaver[Any]
    _Compiled = CompiledStateGraph[Any, Any, Any, Any]


# ── State schema ─────────────────────────────────────────────────────────


class TaskState(BaseModel):
    """Pydantic v2 schema for the main dispatch graph (ADR-0002 §2.1).

    Distinct from :class:`popolaloom.daemon.state.TaskState` (a ``StrEnum``
    of in-memory FSM labels). This ``BaseModel`` carries the *plan-level*
    state that LangGraph checkpoints in SQLite at every super-step; the
    StrEnum is the *Popolad runtime* per-task handle.

    Status values intentionally narrower than ArkTower's 10-state FSM
    because LangGraph just needs to know "are we still running?" — the
    full 10-state lifecycle is owned by ArkTower TaskService (per
    ADR-0002 §2.2).

    Attributes:
        task_id: Unique dispatch id; equal to LangGraph ``thread_id``.
        cli: Adapter name (``cursor`` / ``claude`` / ``codex`` / test).
        cwd: Optional working directory for the subprocess.
        prompt: Raw prompt forwarded to the chosen CLI.
        extra: Adapter-specific extras (e.g. ``{"output_format": "stream-json"}``).
        status: Coarse FSM label — narrower than ArkTower 10-state FSM.
        subprocess_pid: ``None`` until ``spawn_node`` returns the pid.
        events_count: Tail length of the per-task NDJSON file at terminal.
        final_message: Human-readable summary written by ``emit_terminal_node``.
        error: Exception repr() if any node raised; else ``None``.
        started_at: UTC timestamp set by ``dispatch_node``.
        completed_at: UTC timestamp set by ``emit_terminal_node``.
        exit_code: Subprocess exit code captured by ``wait_node``.
        cmd: Argv list from ``adapter.build_command``; carried between nodes.
    """

    task_id: str
    cli: str
    cwd: Path | None = None
    prompt: str
    extra: dict[str, Any] = Field(default_factory=dict)
    status: Literal[
        "pending",
        "running",
        "completed",
        "failed",
        "interrupted",
    ] = "pending"
    subprocess_pid: int | None = None
    events_count: int = 0
    final_message: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    cmd: list[str] = Field(default_factory=list)


# ── Callback Protocol ────────────────────────────────────────────────────


@runtime_checkable
class GraphCallbacks(Protocol):
    """Side-effect surface that the graph nodes call into.

    Kept as a :class:`typing.Protocol` so unit tests can pass a plain mock
    object (no langgraph involvement needed) and production code wires
    these to :class:`popolaloom.daemon.Popolad` helpers (supervisor / event
    log / adapter facade).

    All methods are synchronous because the main graph runs sync via
    ``graph.invoke`` inside a background thread spawned by Popolad
    (the alternative — ``ainvoke`` + ``aiosqlite`` — adds an asyncio
    requirement on the supervisor thread plumbing without buying us
    anything for v0.2.0).
    """

    def adapter_build_command(
        self,
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None,
    ) -> list[str]:
        """Translate (cli, prompt, cwd, extra) into an ``argv`` list."""
        ...

    def supervisor_spawn(
        self,
        task_id: str,
        cmd: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
    ) -> int:
        """Spawn the subprocess; return its pid."""
        ...

    def supervisor_wait(self, task_id: str) -> tuple[int, int]:
        """Block until the subprocess exits.

        Returns:
            tuple ``(exit_code, events_count)``. ``events_count`` is the
            current length of the per-task NDJSON file at the moment of
            the wait completion; the graph stores it in ``state.events_count``.
        """
        ...

    def event_log_emit(
        self,
        task_id: str,
        type_: str,
        data: dict[str, Any],
    ) -> None:
        """Append a single CloudEvents envelope to the per-task NDJSON log."""
        ...


# ── Node factory ─────────────────────────────────────────────────────────


def _make_nodes(
    callbacks: GraphCallbacks,
) -> tuple[
    Any,  # dispatch_node
    Any,  # spawn_node
    Any,  # wait_node
    Any,  # emit_terminal_node
]:
    """Build closures over ``callbacks`` for each of the 4 graph nodes.

    Each node returns a partial-state ``dict`` that LangGraph merges into
    the channel store at super-step boundary; we never mutate the input
    state object.
    """

    def dispatch_node(state: TaskState) -> dict[str, Any]:
        if not state.task_id:
            raise ValueError("dispatch_node: state.task_id is required")
        if not state.cli:
            raise ValueError("dispatch_node: state.cli is required")
        if not state.prompt:
            raise ValueError("dispatch_node: state.prompt is required")

        cmd = state.cmd
        if not cmd:
            cmd = callbacks.adapter_build_command(
                state.cli, state.prompt, state.cwd, state.extra or None
            )
            if not isinstance(cmd, list) or not cmd:
                raise ValueError(
                    f"adapter_build_command returned invalid cmd: {cmd!r}"
                )

        callbacks.event_log_emit(
            state.task_id,
            "graph.step",
            {
                "node": "dispatch",
                "task_id": state.task_id,
                "cli": state.cli,
            },
        )
        return {
            "status": "running",
            "started_at": datetime.now(UTC),
            "cmd": cmd,
        }

    def spawn_node(state: TaskState) -> dict[str, Any]:
        if not state.cmd:
            raise RuntimeError(
                "spawn_node: state.cmd unset; dispatch_node must run first"
            )
        pid = callbacks.supervisor_spawn(
            state.task_id, state.cmd, state.cwd, None
        )
        callbacks.event_log_emit(
            state.task_id,
            "graph.step",
            {"node": "spawn", "task_id": state.task_id, "pid": pid},
        )
        return {"subprocess_pid": pid}

    def wait_node(state: TaskState) -> dict[str, Any]:
        exit_code, events_count = callbacks.supervisor_wait(state.task_id)
        callbacks.event_log_emit(
            state.task_id,
            "graph.step",
            {
                "node": "wait",
                "task_id": state.task_id,
                "exit_code": exit_code,
                "events_count": events_count,
            },
        )
        return {"exit_code": exit_code, "events_count": events_count}

    def emit_terminal_node(state: TaskState) -> dict[str, Any]:
        if state.exit_code is None:
            raise RuntimeError(
                "emit_terminal_node: exit_code unset; wait_node must run first"
            )
        if state.exit_code == 0:
            new_status: Literal["completed", "failed"] = "completed"
            final_message = "task completed successfully"
        else:
            new_status = "failed"
            final_message = f"task failed with exit_code={state.exit_code}"

        callbacks.event_log_emit(
            state.task_id,
            "graph.step",
            {
                "node": "emit_terminal",
                "task_id": state.task_id,
                "status": new_status,
                "exit_code": state.exit_code,
            },
        )
        return {
            "status": new_status,
            "completed_at": datetime.now(UTC),
            "final_message": final_message,
        }

    return dispatch_node, spawn_node, wait_node, emit_terminal_node


# ── Graph builder ────────────────────────────────────────────────────────


def build_main_graph(
    *,
    callbacks: GraphCallbacks,
    checkpointer: _Saver | None = None,
) -> _Compiled:
    """Compile the ``dispatch → spawn → wait → emit_terminal`` StateGraph.

    Args:
        callbacks: side-effect bundle (see :class:`GraphCallbacks`).
        checkpointer: any ``BaseCheckpointSaver`` (typically a
            :class:`langgraph.checkpoint.sqlite.SqliteSaver` from
            :func:`popolaloom.daemon.checkpoint.make_checkpointer`).
            ``None`` is allowed for in-memory tests; the graph then
            cannot resume across process restarts.

    Returns:
        ``CompiledStateGraph`` ready for ``invoke(initial_state, config={
        "configurable": {"thread_id": task_id}})``.
    """
    dispatch_node, spawn_node, wait_node, emit_terminal_node = _make_nodes(callbacks)

    graph = StateGraph(TaskState)
    graph.add_node("dispatch", dispatch_node)
    graph.add_node("spawn", spawn_node)
    graph.add_node("wait", wait_node)
    graph.add_node("emit_terminal", emit_terminal_node)

    graph.add_edge(START, "dispatch")
    graph.add_edge("dispatch", "spawn")
    graph.add_edge("spawn", "wait")
    graph.add_edge("wait", "emit_terminal")
    graph.add_edge("emit_terminal", END)

    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "GraphCallbacks",
    "TaskState",
    "build_main_graph",
]
