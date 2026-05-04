"""Tier 2 coverage gap-fillers for v0.2.3 (target 86 → 90 %).

Each test exercises a previously-uncovered branch in
``src/popolaloom/`` so the v0.2.3 90 % gate is met.  No new src
behaviour — these are pure assertions over existing code paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import (
    GraphTaskState,
    Popolad,
    build_main_graph,
)
from popolaloom.evaluation.popola_dimensions import (
    DIMENSIONS,
    AttachCorrectness,
    CrossCliHandoff,
    CycleConvergence,
    DispatchIsolation,
    EventLogCompleteness,
    HitlLatency,
    SingleThreadedWrites,
    TokenBudgetCompliance,
    _placeholder,
)

# ── 1. popola_dimensions.py — cover all 8 dimension scorers ────────────


def test_dispatch_isolation_pgid_distinct_returns_one() -> None:
    s = DispatchIsolation()
    assert s.score({"daemon_pgid": 100, "cli_pgid": 200}) == 1.0


def test_dispatch_isolation_pgid_same_returns_zero() -> None:
    s = DispatchIsolation()
    assert s.score({"daemon_pgid": 100, "cli_pgid": 100}) == 0.0


def test_dispatch_isolation_falls_back_to_pid_when_pgid_missing() -> None:
    s = DispatchIsolation()
    assert s.score({"daemon_pid": 1, "cli_pid": 2}) == 1.0
    assert s.score({"daemon_pid": 5, "cli_pid": 5}) == 0.0


def test_dispatch_isolation_no_evidence_returns_placeholder() -> None:
    s = DispatchIsolation()
    assert s.score({}) == _placeholder()


def test_cycle_convergence_iter_paths() -> None:
    s = CycleConvergence()
    assert s.score({"cycle_demo_present": False}) == _placeholder()
    assert s.score({"cycle_demo_present": True, "cycle_demo_iters": 2}) == 1.0
    assert s.score({"cycle_demo_present": True, "cycle_demo_iters": 4}) == 0.5
    assert s.score({"cycle_demo_present": True, "cycle_demo_iters": 99}) == 0.0


def test_hitl_latency_thresholds() -> None:
    s = HitlLatency()
    assert s.score({}) == _placeholder()
    assert s.score({"hitl_round_trip_seconds": 0.1}) == 1.0
    assert s.score({"hitl_round_trip_seconds": 3.0}) == 0.7
    assert s.score({"hitl_round_trip_seconds": 20.0}) == 0.5
    assert s.score({"hitl_round_trip_seconds": 200.0}) == 0.3
    assert s.score({"hitl_round_trip_seconds": 600.0}) == 0.0


def test_hitl_latency_invalid_value_returns_placeholder() -> None:
    s = HitlLatency()
    assert s.score({"hitl_round_trip_seconds": "not-a-number"}) == _placeholder()
    assert s.score({"hitl_round_trip_seconds": object()}) == _placeholder()


def test_attach_correctness_ratio_paths() -> None:
    s = AttachCorrectness()
    assert s.score({}) == _placeholder()
    assert s.score({"attach_total_count": 10, "attach_complete_count": 10}) == 1.0
    assert s.score({"attach_total_count": 10, "attach_complete_count": 5}) == 0.5
    assert s.score({"attach_total_count": 10, "attach_complete_count": 0}) == 0.0


def test_attach_correctness_invalid_value_returns_placeholder() -> None:
    s = AttachCorrectness()
    assert s.score({"attach_total_count": 0, "attach_complete_count": 0}) == _placeholder()
    assert (
        s.score({"attach_total_count": "x", "attach_complete_count": 1})
        == _placeholder()
    )


def test_cross_cli_handoff_paths() -> None:
    s = CrossCliHandoff()
    assert s.score({}) == 0.5
    assert s.score({"handoff_successful_count": 1}) == 1.0
    assert s.score({"handoff_successful_count": 0}) == 0.0
    assert s.score({"handoff_successful_count": "bad"}) == 0.5


def test_single_threaded_writes_lock_paths() -> None:
    s = SingleThreadedWrites()
    assert s.score({}) == 1.0
    full = ["_event_logs_lock", "state_store_lock", "event_log_lock"]
    assert s.score({"locks_present": full}) == 1.0
    assert (
        s.score({"locks_present": ["_event_logs_lock", "state_store_lock"]})
        == 0.66
    )
    assert s.score({"locks_present": ["_event_logs_lock"]}) == 0.33
    assert s.score({"locks_present": ["unknown1", "unknown2", "unknown3"]}) == 0.0


def test_single_threaded_writes_invalid_locks_returns_placeholder() -> None:
    s = SingleThreadedWrites()
    assert s.score({"locks_present": 5}) == _placeholder()


def test_event_log_completeness_paths() -> None:
    s = EventLogCompleteness()
    assert s.score({}) == _placeholder()
    assert s.score({"event_count_before_recovery": 5}) == _placeholder()
    assert (
        s.score(
            {
                "event_count_before_recovery": 5,
                "event_count_after_recovery": 8,
                "recovered_count": 2,
            }
        )
        == 1.0
    )
    assert (
        s.score(
            {
                "event_count_before_recovery": 0,
                "event_count_after_recovery": 0,
                "recovered_count": 1,
            }
        )
        == 0.0
    )


def test_event_log_completeness_invalid_returns_placeholder() -> None:
    s = EventLogCompleteness()
    assert (
        s.score(
            {
                "event_count_before_recovery": "bad",
                "event_count_after_recovery": 1,
                "recovered_count": 1,
            }
        )
        == _placeholder()
    )


def test_token_budget_compliance_paths() -> None:
    s = TokenBudgetCompliance()
    assert s.score({}) == _placeholder()
    assert s.score({"token_budget_violations": 0}) == 1.0
    assert s.score({"token_budget_violations": 1}) == 0.0
    assert s.score({"token_budget_violations": "x"}) == _placeholder()


def test_dimensions_canonical_list_has_eight_scorers() -> None:
    """Sanity: DIMENSIONS list contains all 8 expected scorers in order.

    v0.3.0 F4.E swap (D3.10): ``token_budget_compliance`` →
    ``hitl_handleability`` at the same 0.10 weight.  The
    TokenBudgetCompliance class remains importable for backward compat
    but is no longer in the canonical DIMENSIONS list.
    """
    names = [d.name for d in DIMENSIONS]
    assert names == [
        "dispatch_isolation",
        "cycle_convergence",
        "hitl_latency",
        "attach_correctness",
        "cross_cli_handoff",
        "single_threaded_writes",
        "event_log_completeness",
        "hitl_handleability",
    ]


# ── 2. graph.py — cover dispatch_node validation branches ──────────────


class _MinimalCallbacks:
    """Bare callbacks impl that records nothing — for validation-error cases."""

    def __init__(self, exit_code: int = 0) -> None:
        self._exit = exit_code

    def adapter_build_command(
        self, cli: str, prompt: str, cwd: Path | None, extra: dict[str, Any] | None
    ) -> list[str]:
        return ["echo", "x"]

    def supervisor_spawn(
        self, task_id: str, cmd: list[str], cwd: Path | None, env: dict[str, str] | None
    ) -> int:
        return 999

    def supervisor_wait(self, task_id: str) -> tuple[int, int]:
        return self._exit, 1

    def event_log_emit(
        self, task_id: str, type_: str, data: dict[str, Any]
    ) -> None:
        pass


def test_graph_dispatch_node_rejects_empty_task_id() -> None:
    cb = _MinimalCallbacks()
    graph = build_main_graph(callbacks=cb, checkpointer=None)
    state = GraphTaskState(task_id="", cli="cursor", prompt="hi", cmd=["echo"])
    with pytest.raises(ValueError, match="state.task_id is required"):
        graph.invoke(state)


def test_graph_dispatch_node_rejects_empty_prompt() -> None:
    cb = _MinimalCallbacks()
    graph = build_main_graph(callbacks=cb, checkpointer=None)
    state = GraphTaskState(task_id="t", cli="cursor", prompt="", cmd=["echo"])
    with pytest.raises(ValueError, match="state.prompt is required"):
        graph.invoke(state)


def test_graph_dispatch_node_rejects_invalid_adapter_cmd() -> None:
    """When state.cmd is empty AND adapter returns invalid, dispatch_node raises."""

    class _BadCb(_MinimalCallbacks):
        def adapter_build_command(
            self,
            cli: str,
            prompt: str,
            cwd: Path | None,
            extra: dict[str, Any] | None,
        ) -> list[str]:
            return []

    cb = _BadCb()
    graph = build_main_graph(callbacks=cb, checkpointer=None)
    state = GraphTaskState(task_id="t", cli="cursor", prompt="hi")
    with pytest.raises(ValueError, match="invalid cmd"):
        graph.invoke(state)


# ── 3. server.py / Popolad — cover error paths ─────────────────────────


def test_popolad_dispatch_without_adapter_raises_runtime_error(tmp_path: Path) -> None:
    """Popolad without injected adapter + dispatch without adapter = clean error."""
    p = Popolad(events_dir=tmp_path / "events", adapter=None, use_graph=False)
    with pytest.raises(RuntimeError, match="No adapter provided"):
        p.dispatch_task(cli="cursor", prompt="x")


def test_popolad_get_status_missing_task_raises_keyerror(tmp_path: Path) -> None:
    p = Popolad(events_dir=tmp_path / "events", adapter=lambda *a, **kw: ["echo"], use_graph=False)
    with pytest.raises(KeyError, match="task_id not found"):
        p.get_status("nonexistent-task-id")


def test_popolad_dispatch_invalid_adapter_return_raises_value_error(tmp_path: Path) -> None:
    def bad_adapter(cli: str, prompt: str, cwd: Path | None, extra: Any = None) -> list[str]:
        return []  # type: ignore[return-value]

    p = Popolad(events_dir=tmp_path / "events", adapter=bad_adapter, use_graph=False)
    with pytest.raises(ValueError, match="invalid cmd"):
        p.dispatch_task(cli="cursor", prompt="x")


# ── 4. mock_cli — cover mock-side branches ─────────────────────────────


def test_mock_cursor_round_num_default_is_one() -> None:
    from tests.fixtures.mock_cli.mock_cursor import run_mock_cursor

    out = run_mock_cursor("no workflow context here")
    assert out.stdout.startswith("[devola-flow:round=1]")


def test_mock_claude_text_format_returns_three_section() -> None:
    from tests.fixtures.mock_cli.mock_claude import run_mock_claude

    out = run_mock_claude("test prompt", round_num=2, output_format="text")
    assert "[devola-flow:round=2]" in out.stdout
    assert "## Acceptance Verification" in out.stdout


def test_mock_codex_invalid_sandbox_value_emits_stderr() -> None:
    from tests.fixtures.mock_cli.mock_codex import run_mock_codex

    out = run_mock_codex("test", sandbox="bad-mode")
    assert out.returncode == 2
    assert "invalid --sandbox" in out.stderr
