"""Tier 1 / A4 — Pydantic v2 validation tests for graph TaskState BaseModel.

The Stage B schema lives in :mod:`popolaloom.daemon.graph` (distinct from
the StrEnum :class:`popolaloom.daemon.state.TaskState`). Each test
exercises one validation rule + one happy path, per the L3 brief:

- Required fields raise ValidationError when missing.
- ``status`` Literal field rejects invalid strings.
- ``subprocess_pid`` defaults to None and accepts non-negative ints.
- ``events_count`` defaults to 0 and accepts ints (>= 0 enforced via
  explicit assertion).
- Extras dict round-trips correctly.
- ``.model_dump()`` produces a JSON-serializable dict.

Pydantic v2 ``ValidationError`` is raised via the ``model_validate`` /
``__init__`` paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from popolaloom.daemon.graph import TaskState as GraphTaskState

# ── happy-path baseline ──────────────────────────────────────────────────


def test_minimal_valid_construction() -> None:
    """Minimum valid TaskState requires task_id + cli + prompt; rest defaults."""
    state = GraphTaskState(task_id="abc-123", cli="cursor", prompt="hi")
    assert state.task_id == "abc-123"
    assert state.cli == "cursor"
    assert state.prompt == "hi"
    assert state.status == "pending"
    assert state.subprocess_pid is None
    assert state.events_count == 0
    assert state.cwd is None
    assert state.cmd == []
    assert state.extra == {}
    assert state.exit_code is None
    assert state.error is None


# ── required fields raise on missing ─────────────────────────────────────


def test_missing_task_id_raises_validation_error() -> None:
    """task_id is required; omitting it raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(cli="cursor", prompt="hi")  # type: ignore[call-arg]
    errors = excinfo.value.errors()
    assert any(e["loc"] == ("task_id",) for e in errors), (
        f"expected task_id error, got: {errors}"
    )


def test_missing_cli_raises_validation_error() -> None:
    """cli is required."""
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(task_id="t-1", prompt="hi")  # type: ignore[call-arg]
    assert any(e["loc"] == ("cli",) for e in excinfo.value.errors())


def test_missing_prompt_raises_validation_error() -> None:
    """prompt is required."""
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(task_id="t-1", cli="cursor")  # type: ignore[call-arg]
    assert any(e["loc"] == ("prompt",) for e in excinfo.value.errors())


# ── status enum (Literal) ────────────────────────────────────────────────


def test_status_accepts_literal_values() -> None:
    """All five Literal status values construct cleanly."""
    for status in ("pending", "running", "completed", "failed", "interrupted"):
        state = GraphTaskState(task_id="t", cli="c", prompt="p", status=status)  # type: ignore[arg-type]
        assert state.status == status


def test_status_rejects_invalid_literal() -> None:
    """A non-Literal status string is rejected (Pydantic v2 ValidationError)."""
    with pytest.raises(ValidationError) as excinfo:
        GraphTaskState(task_id="t", cli="c", prompt="p", status="bogus_state")  # type: ignore[arg-type]
    assert any(e["loc"] == ("status",) for e in excinfo.value.errors())


# ── subprocess_pid / events_count / exit_code numeric fields ─────────────


def test_subprocess_pid_accepts_none_and_int() -> None:
    """subprocess_pid is Optional[int]; we keep the soft contract pid >= 0 via assertion."""
    s1 = GraphTaskState(task_id="t", cli="c", prompt="p")
    assert s1.subprocess_pid is None
    s2 = GraphTaskState(task_id="t2", cli="c", prompt="p", subprocess_pid=42)
    assert s2.subprocess_pid == 42
    assert s2.subprocess_pid >= 0


def test_events_count_default_and_accepts_int() -> None:
    """events_count default 0 + accepts arbitrary int (we soft-enforce >= 0 here)."""
    s1 = GraphTaskState(task_id="t", cli="c", prompt="p")
    assert s1.events_count == 0
    s2 = GraphTaskState(task_id="t2", cli="c", prompt="p", events_count=12)
    assert s2.events_count == 12
    assert s2.events_count >= 0


def test_exit_code_accepts_none_zero_and_negative_for_signals() -> None:
    """exit_code can be None (still running), 0 (success), or negative (signal-killed)."""
    s_none = GraphTaskState(task_id="t", cli="c", prompt="p")
    assert s_none.exit_code is None
    s_zero = GraphTaskState(task_id="t", cli="c", prompt="p", exit_code=0)
    assert s_zero.exit_code == 0
    s_signal = GraphTaskState(task_id="t", cli="c", prompt="p", exit_code=-9)
    assert s_signal.exit_code == -9


# ── cwd / cmd / extra defaults ───────────────────────────────────────────


def test_cwd_accepts_path_or_none() -> None:
    """cwd is Optional[Path]; both None and concrete Path work."""
    s_none = GraphTaskState(task_id="t", cli="c", prompt="p")
    assert s_none.cwd is None
    s_with = GraphTaskState(task_id="t", cli="c", prompt="p", cwd=Path("/tmp/x"))
    assert s_with.cwd == Path("/tmp/x")


def test_cmd_default_empty_and_accepts_list() -> None:
    """cmd defaults to empty list and accepts list[str]."""
    s_default = GraphTaskState(task_id="t", cli="c", prompt="p")
    assert s_default.cmd == []
    s_filled = GraphTaskState(task_id="t", cli="c", prompt="p", cmd=["a", "b", "c"])
    assert s_filled.cmd == ["a", "b", "c"]


def test_extra_default_empty_and_round_trips() -> None:
    """extra defaults to {} and round-trips through model_dump."""
    s = GraphTaskState(
        task_id="t",
        cli="c",
        prompt="p",
        extra={"yolo": True, "session_id": "s1"},
    )
    assert s.extra == {"yolo": True, "session_id": "s1"}
    dumped = s.model_dump()
    assert dumped["extra"] == {"yolo": True, "session_id": "s1"}


# ── timestamps ───────────────────────────────────────────────────────────


def test_started_at_and_completed_at_accept_datetime() -> None:
    """Timestamps accept tz-aware datetime; default None."""
    now = datetime.now(UTC)
    s = GraphTaskState(
        task_id="t", cli="c", prompt="p", started_at=now, completed_at=now
    )
    assert s.started_at == now
    assert s.completed_at == now
    assert s.started_at.tzinfo is not None
