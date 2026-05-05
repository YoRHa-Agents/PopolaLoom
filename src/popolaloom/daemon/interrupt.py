"""HITL ``interrupt() + Command(resume=...)`` placeholder (v0.2.0 Stage B B4).

Wraps the LangGraph 1.x interrupt protocol so that any subgraph can drop
in ``human_input_required`` as a node and *pause* execution until the
caller sends a ``Command(resume=value)`` keyed by the same ``thread_id``.

Per ADR-0002 §2.1 + spec.md §3.5.4:

- The interrupt payload must be JSON-serializable (AP-4): we send a small
  dict with ``schema`` (JSONSchema, typically ``{"enum": [...]}``),
  ``message`` (human-readable question), and the originating ``task_id``.
- The producer (Lark / IDE / signal channel) collects the answer and calls
  :func:`apply_resume` with the value.
- All side effects *before* this node must be idempotent (AP-2): on resume
  LangGraph re-runs only the interrupted node body from the ``interrupt``
  call site; nothing earlier in that super-step.

This module is *intentionally* small: Stage D popola_supply_feedback verb
is the consumer; Stage E Lark-channel handoff is the producer; here we
just expose the canonical building block.

Workspace rules honored:

- *No Silent Failures*: ``apply_resume`` propagates any LangGraph error;
  callers must catch and translate to the user-facing channel.
- *Mandatory Verification*: see ``tests/test_graph.py::test_interrupt_resume``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.types import Command, interrupt

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    _Compiled = CompiledStateGraph[Any, Any, Any, Any]


def human_input_required(state: dict[str, Any]) -> dict[str, Any]:
    """Node body that pauses the graph until ``Command(resume=...)`` arrives.

    Args:
        state: Mutable graph state (any TypedDict / Pydantic model). We
            look up:

            - ``state["question"]`` — optional string for the prompt;
              defaults to ``"Continue?"``
            - ``state["task_id"]`` — optional task id; embedded in the
              interrupt payload so the human-side UI knows which task

    Returns:
        On resume: a partial state ``{"human_response": <resume_value>}``;
        the caller is free to widen the schema.

    Notes:
        The :func:`langgraph.types.interrupt` raises a special internal
        signal that bubbles up through ``graph.invoke`` and lands in the
        returned state under ``__interrupt__``. The function below appears
        to "return" only on resume — that's the LangGraph protocol.
    """
    user_response = interrupt(
        {
            "schema": {"enum": ["yes", "no", "abort"]},
            "message": state.get("question", "Continue?"),
            "task_id": state.get("task_id"),
        }
    )
    return {"human_response": user_response}


def apply_resume(
    graph: _Compiled,
    thread_id: str,
    resume_value: Any,
) -> dict[str, Any]:
    """Send a ``Command(resume=...)`` to a paused graph keyed by ``thread_id``.

    Args:
        graph: Compiled LangGraph instance previously paused via :func:`interrupt`.
        thread_id: Identifier matching the ``configurable.thread_id`` used
            in the original ``invoke`` call (popolad: ``= task_id``).
        resume_value: The value to inject as the return of :func:`interrupt`.
            Must be JSON-serializable (AP-4 reds in ADR-0002 §2.4).

    Returns:
        The post-resume final state from ``graph.invoke``.
    """
    return graph.invoke(
        Command(resume=resume_value),
        config={"configurable": {"thread_id": thread_id}},
    )


__all__ = [
    "apply_resume",
    "human_input_required",
]
