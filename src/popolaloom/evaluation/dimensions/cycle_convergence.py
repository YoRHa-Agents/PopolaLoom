"""cycle_convergence — Gen-Verifier subgraph dev↔test loop converges (v0.3.0 F1.2).

Real measurement (v0.3.0 upgrade from v0.2.0 mvp):

The v0.2.0 mvp simply read ``cycle_demo_iters`` from the evidence dict
(filled by an off-band probe).  v0.3.0 actually **runs** the
:func:`popolaloom.daemon.subgraph_dev_test.build_dev_test_subgraph`
demo with a deterministic score sequence ``[0.5, 0.9]`` and verifies
the verifier converges in ≤ 2 iterations.

Score grid (per task spec F1.2):

- ``1.0`` — converges (``done=True``) within 2 iters
- ``0.5`` — hits ``give_up`` (max_iter exhausted without converging)
- ``0.0`` — graph failed to compile/invoke (e.g. missing langgraph)

Evidence override: when the caller supplies ``cycle_demo_iters`` directly
(e.g. tests that don't want to spin a real graph), we honour that value
and short-circuit the live invocation — preserves v0.2.0 backward-compat
for unit tests built around the evidence dict.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _run_subgraph_score_pair() -> tuple[bool, bool, int]:
    """Run the [0.5, 0.9] convergence demo, return (done, give_up, iter).

    Isolated in a helper so it can be patched in tests + so import errors
    on langgraph are caught lazily (cycle_convergence is the only scorer
    that needs langgraph at scoring time).

    Returns:
        tuple (done, give_up, iter):
            - ``done``    — True if verifier converged (score ≥ 0.85)
            - ``give_up`` — True if max_iter exhausted without converging
            - ``iter``    — final iteration count (1-based)
    """
    try:
        from popolaloom.daemon.subgraph_dev_test import build_dev_test_subgraph
    except Exception:
        logger.exception("cycle_convergence: cannot import subgraph_dev_test")
        return False, False, 0

    try:
        graph = build_dev_test_subgraph(score_sequence=[0.5, 0.9], max_iter=2)
        result = graph.invoke({"prompt": "cycle_convergence smoke"})
    except Exception:
        logger.exception("cycle_convergence: subgraph invoke failed")
        return False, False, 0

    done = bool(result.get("done", False))
    give_up = bool(result.get("give_up", False))
    iter_count = int(result.get("iter", 0))
    return done, give_up, iter_count


class CycleConvergence:
    """Gen-Verifier subgraph dev↔test loop converges in ≤ 2 iterations.

    v0.3.0 F1.2 real measurement: invokes the dev_test subgraph with a
    deterministic score sequence and verifies it converges; falls back
    to evidence-supplied ``cycle_demo_iters`` when present (test path).
    """

    name = "cycle_convergence"

    def score(self, evidence: dict[str, Any]) -> float:
        """``1.0`` if subgraph converges within 2 iters, ``0.5`` on give_up."""
        if "cycle_demo_iters" in evidence and evidence["cycle_demo_iters"] is not None:
            iters = evidence.get("cycle_demo_iters")
            if iters is None:
                return 0.5
            try:
                iters_i = int(iters)
            except (TypeError, ValueError):
                return 0.5
            if iters_i <= 2:
                return 1.0
            if iters_i <= 5:
                return 0.5
            return 0.0

        if not evidence.get("cycle_demo_present", True):
            return 0.5

        done, give_up, iter_count = _run_subgraph_score_pair()
        if done and iter_count <= 2:
            return 1.0
        if give_up:
            return 0.5
        if iter_count == 0:
            return 0.0
        return 0.5
