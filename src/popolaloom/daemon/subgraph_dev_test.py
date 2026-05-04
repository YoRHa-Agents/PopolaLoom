"""Gen-Verifier dev↔test subgraph demo (v0.2.0 Stage B B3).

Demo of ADR-0002 §2.1 SCC condensation principle: a 3-node subgraph
``dev → test → verifier`` whose verifier loops back to ``dev`` until the
score crosses a threshold or the iteration cap is hit. The outer task
DAG (built by :func:`popolaloom.daemon.graph.build_main_graph`) sees
this subgraph as a single node — cycles never leak into the outer DAG
(AP-1 hard NO).

Demo only — does not invoke real ``cursor agent`` or ``pytest``. Production
Gen-Verifier (planned Stage E + Day 6) will:

- Replace ``dev_node`` with a dispatch into ``cursor agent --print --output-format=stream-json``
- Replace ``test_node`` with a dispatch into ``codex exec --sandbox=workspace-write pytest -q``
- Replace ``verifier_node`` with the DevolaFlow ``gate`` composite_score
  formula (per spec.md §4.1 row "gate" + ADR-0002 §5.4)

Workspace rules honored:

- *No Silent Failures*: ``score_sequence`` exhaustion raises ``StopIteration``
  rather than silently looping with stale score; tests must exercise both
  the converging and the give-up paths.
- *Mandatory Verification*: ``tests/test_graph.py`` covers both paths.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, TypedDict

from langgraph.graph import END, START, StateGraph

if TYPE_CHECKING:
    from typing import Any

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    _Saver = BaseCheckpointSaver[Any]
    _Compiled = CompiledStateGraph[Any, Any, Any, Any]


class DevTestState(TypedDict, total=False):
    """State passed between the 3 nodes of the dev↔test subgraph.

    ``total=False`` because LangGraph's reducer fills missing keys from
    earlier super-steps, and the subgraph caller only needs to seed
    ``prompt``.

    Keys:
        prompt:      Initial dispatch prompt (immutable through the loop).
        patch:       Generated patch contents (set by ``dev_node``).
        test_output: Output from the mock verifier (set by ``test_node``).
        score:       Score in ``[0, 1]`` (set by ``test_node``).
        iter:        1-based iteration counter (incremented by ``dev_node``).
        done:        ``True`` once verifier scores ≥ threshold.
        give_up:     ``True`` once iter ≥ ``max_iter`` without converging.
    """

    prompt: str
    patch: str
    test_output: str
    score: float
    iter: int
    done: bool
    give_up: bool


_DEFAULT_GATE_THRESHOLD: float = 0.85
"""Verifier score gate (per spec.md §4.1 row "gate" + ADR-0002 §2.1 example)."""


def build_dev_test_subgraph(
    *,
    score_sequence: list[float] | None = None,
    max_iter: int = 2,
    gate_threshold: float = _DEFAULT_GATE_THRESHOLD,
    checkpointer: _Saver | None = None,
) -> _Compiled:
    """Compile the dev↔test 3-node subgraph with a deterministic mock verifier.

    Args:
        score_sequence: Per-iteration verifier score; mock for tests.
            Default ``[0.5, 0.9]`` — converges at iter=2.
        max_iter: Hard cap on iterations before flipping ``give_up=True``.
            Default ``2`` per task spec; production will use ``10`` per
            ADR-0002 §2.1 example.
        gate_threshold: Score above which we consider the dev result
            acceptable; default ``0.85`` per ADR-0002 §2.1 + spec §4.1.
        checkpointer: Optional :class:`BaseCheckpointSaver`; when shared
            with the parent graph, subgraph checkpoints land in the same
            SQLite DB under a nested namespace.

    Returns:
        ``CompiledStateGraph`` ready for ``invoke({"prompt": "..."})``.
    """
    sequence: Iterator[float] = iter(score_sequence or [0.5, 0.9])

    def dev_node(state: DevTestState) -> dict[str, object]:
        new_iter = state.get("iter", 0) + 1
        prompt = state.get("prompt", "")
        patch = f"# patch v{new_iter}: " + prompt[:30]
        return {"patch": patch, "iter": new_iter}

    def test_node(state: DevTestState) -> dict[str, object]:
        try:
            score = next(sequence)
        except StopIteration as exc:
            raise RuntimeError(
                "score_sequence exhausted; tests must declare enough scores "
                f"for max_iter={max_iter}"
            ) from exc
        return {
            "test_output": f"score={score} for {state.get('patch', '<no-patch>')}",
            "score": score,
        }

    def verifier_node(state: DevTestState) -> dict[str, object]:
        if state.get("score", 0.0) >= gate_threshold:
            return {"done": True}
        if state.get("iter", 0) >= max_iter:
            return {"give_up": True}
        return {}

    def route_after_verifier(state: DevTestState) -> str:
        if state.get("done"):
            return "done"
        if state.get("give_up"):
            return "give_up"
        return "dev"

    graph: StateGraph[DevTestState, None, DevTestState, DevTestState] = StateGraph(DevTestState)
    graph.add_node("dev", dev_node)
    graph.add_node("test", test_node)
    graph.add_node("verifier", verifier_node)

    graph.add_edge(START, "dev")
    graph.add_edge("dev", "test")
    graph.add_edge("test", "verifier")
    graph.add_conditional_edges(
        "verifier",
        route_after_verifier,
        {"done": END, "give_up": END, "dev": "dev"},
    )

    return graph.compile(checkpointer=checkpointer)


# Type alias re-exported for tests that want to type-annotate score factories.
ScoreFactory = Callable[[], float]


__all__ = [
    "DevTestState",
    "ScoreFactory",
    "build_dev_test_subgraph",
]
