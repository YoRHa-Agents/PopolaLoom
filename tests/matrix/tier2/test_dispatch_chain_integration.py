"""Tier 2 / B2 — direct Popolad facade dispatch chain integration tests.

Per testing-matrix.md §1.2 example
``test_dispatch_supervisor_event_log_chain_emits_in_order`` and the L3
brief: in-process Popolad (no UDS / no uvicorn) drives the dispatch
chain end-to-end with a fake adapter, then asserts the full NDJSON
event sequence + state transitions.

Cases:

1. dispatch via fake adapter that prints ``{prompt}`` → completed,
   NDJSON has stdout line + completed.
2. dispatch with extras dict → adapter receives the extra payload.
3. dispatch with cwd → adapter receives the cwd path.
4. dispatch fails (adapter raises) → caller sees ValueError; no
   silent swallow; state is never registered.
5. graph mode ON (POPOLA_USE_GRAPH != "0") → emits ``graph.step``
   events for each of the 4 nodes.
6. graph mode OFF (use_graph=False) → no ``graph.step`` events
   (legacy direct path).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import Popolad, TaskState


def _wait_terminal(popolad: Popolad, task_id: str, timeout_s: float = 4.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = popolad.get_status(task_id)
        if last["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            return last
        time.sleep(0.05)
    pytest.fail(f"task {task_id} never reached terminal state; last={last}")


# ── 1: happy path with stdout passthrough ────────────────────────────────


def test_dispatch_chain_completes_with_stdout_event(tmp_path: Path) -> None:
    """Fake adapter prints prompt → process.stdout has the line + task.completed at end."""

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", f"print({prompt!r})"]

    popolad = Popolad(events_dir=tmp_path / "events", adapter=adapter, use_graph=False)
    task_id = popolad.dispatch_task(cli="dispatchcli", prompt="hello world")
    final = _wait_terminal(popolad, task_id)
    assert final["state"] == str(TaskState.COMPLETED)

    events = popolad.tail_events(task_id)
    types = [e["type"] for e in events]
    assert types[0] == "task.dispatched"
    assert "process.stdout" in types
    assert "task.completed" in types
    stdout_lines = [e for e in events if e["type"] == "process.stdout"]
    assert any(e["data"]["line"] == "hello world" for e in stdout_lines)


# ── 2: extras dict propagated into adapter ───────────────────────────────


def test_dispatch_passes_extras_to_adapter(tmp_path: Path) -> None:
    """``dispatch_task(extra={...})`` reaches the adapter callback verbatim."""
    seen_extras: list[dict[str, Any] | None] = []

    def capture_adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        seen_extras.append(dict(extra) if extra is not None else None)
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    popolad = Popolad(events_dir=tmp_path, adapter=capture_adapter, use_graph=False)
    popolad.dispatch_task(cli="cli2", prompt="p", extra={"yolo": True, "n": 3})
    assert len(seen_extras) == 1
    assert seen_extras[0] == {"yolo": True, "n": 3}


# ── 3: cwd flows through to adapter ──────────────────────────────────────


def test_dispatch_passes_cwd_to_adapter(tmp_path: Path) -> None:
    """The ``cwd`` arg to dispatch_task is delivered as Path to the adapter."""
    seen_cwds: list[Path | None] = []

    def capture_adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        seen_cwds.append(cwd)
        return [sys.executable, "-c", "pass"]

    target = tmp_path / "subdir"
    target.mkdir()
    popolad = Popolad(events_dir=tmp_path, adapter=capture_adapter, use_graph=False)
    popolad.dispatch_task(cli="cli3", prompt="p", cwd=target)
    assert len(seen_cwds) == 1
    assert seen_cwds[0] == target


# ── 4: adapter raise → propagates synchronously, no state registered ────


def test_dispatch_adapter_raise_propagates(tmp_path: Path) -> None:
    """When the adapter raises, dispatch_task surfaces the exception (No Silent Failures)."""

    def boom_adapter(*args: Any, **kw: Any) -> list[str]:
        raise ValueError("adapter exploded for test")

    popolad = Popolad(events_dir=tmp_path, adapter=boom_adapter, use_graph=False)
    with pytest.raises(ValueError, match="adapter exploded"):
        popolad.dispatch_task(cli="bad", prompt="p")
    assert popolad.state_store.list_active() == []


# ── 5: graph mode ON — graph.step events for 4 nodes ─────────────────────


def test_graph_mode_on_emits_graph_step_events(tmp_path: Path) -> None:
    """``use_graph=True`` (Stage B default) → 4 ``graph.step`` events appear.

    Isolates the LangGraph checkpointer at ``tmp_path/state.sqlite`` so we
    never touch the real ``~/.popola/state.sqlite`` (which would slow other
    tests sharing that file — see daemon/checkpoint.py default path).
    """

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    from popolaloom.daemon.checkpoint import make_checkpointer

    checkpointer = make_checkpointer(tmp_path / "state.sqlite")
    popolad = Popolad(
        events_dir=tmp_path,
        adapter=adapter,
        use_graph=True,
        checkpointer=checkpointer,
    )
    task_id = popolad.dispatch_task(cli="graphtest", prompt="p")
    _wait_terminal(popolad, task_id, timeout_s=8.0)

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        events = popolad.tail_events(task_id)
        graph_steps = [e for e in events if e["type"] == "graph.step"]
        nodes = {e["data"].get("node") for e in graph_steps}
        if {"dispatch", "spawn", "wait", "emit_terminal"}.issubset(nodes):
            break
        time.sleep(0.1)
    else:
        all_events = popolad.tail_events(task_id)
        pytest.fail(
            f"graph.step nodes incomplete; saw nodes={nodes}, events={all_events}"
        )


# ── 6: graph mode OFF — zero graph.step events ───────────────────────────


def test_graph_mode_off_emits_no_graph_step_events(tmp_path: Path) -> None:
    """``use_graph=False`` (legacy path) emits zero ``graph.step`` envelopes."""

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    popolad = Popolad(events_dir=tmp_path, adapter=adapter, use_graph=False)
    task_id = popolad.dispatch_task(cli="legacyonly", prompt="p")
    _wait_terminal(popolad, task_id)
    events = popolad.tail_events(task_id)
    types = [e["type"] for e in events]
    assert "graph.step" not in types


# ── 7: dispatch without adapter raises RuntimeError (No Silent Failures) ─


def test_dispatch_without_adapter_raises_runtime_error(tmp_path: Path) -> None:
    """Popolad without injected adapter and without per-call adapter raises."""
    popolad = Popolad(events_dir=tmp_path, adapter=None, use_graph=False)
    with pytest.raises(RuntimeError, match="No adapter provided"):
        popolad.dispatch_task(cli="cli", prompt="p")


# ── 8: adapter returning non-list raises ValueError ──────────────────────


def test_dispatch_adapter_returns_invalid_cmd_raises(tmp_path: Path) -> None:
    """Adapter returning a non-list / empty list raises ValueError + no state."""

    def bad_adapter(*args: Any, **kw: Any) -> list[str]:
        return []

    popolad = Popolad(events_dir=tmp_path, adapter=bad_adapter, use_graph=False)
    with pytest.raises(ValueError, match="adapter returned invalid cmd"):
        popolad.dispatch_task(cli="cli", prompt="p")


# ── 9: cancel on a long-running task transitions to CANCELED ─────────────


def test_cancel_long_running_task_transitions_to_canceled(tmp_path: Path) -> None:
    """``cancel_task`` on an in-flight task moves the handle to CANCELED."""

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    popolad = Popolad(events_dir=tmp_path, adapter=adapter, use_graph=False)
    task_id = popolad.dispatch_task(cli="long", prompt="p")
    time.sleep(0.2)
    result = popolad.cancel_task(task_id, sigterm_grace_s=0.5)
    assert result["task_id"] == task_id
    assert result["requested_signal"] == "SIGTERM"

    time.sleep(0.2)
    final = popolad.get_status(task_id)
    assert final["state"] == str(TaskState.CANCELED)
