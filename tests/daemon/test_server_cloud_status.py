"""Status / list summaries include cloud-runtime fields (v0.8.5 Stage 2 T2.B)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from popolaloom.daemon.server import Popolad
from popolaloom.daemon.state import TaskHandle, TaskState


def _mk(
    tmp_path: Path,
    *,
    task_id: str,
    runtime: str = "local",
    cursor_agent_id: str | None = None,
    cursor_run_id: str | None = None,
    cloud_phase: str | None = None,
    state: TaskState = TaskState.RUNNING,
) -> Popolad:
    popolad = Popolad(events_dir=tmp_path / "events")
    path = tmp_path / "events" / f"{task_id}.jsonl"
    h = TaskHandle(
        task_id=task_id,
        cli="cursor-cloud" if runtime == "cloud" else "cursor",
        pid=123 if runtime == "local" else None,
        state=state,
        started_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        event_log_path=path,
        runtime=runtime,
        cursor_agent_id=cursor_agent_id,
        cursor_run_id=cursor_run_id,
        cloud_phase=cloud_phase,
    )
    popolad.state_store.register(h)
    return popolad


def test_get_status_includes_runtime_field(tmp_path: Path) -> None:
    p = _mk(tmp_path, task_id="t1", runtime="cloud", cursor_agent_id="a", cursor_run_id="r")
    st = p.get_status("t1")
    assert st["runtime"] == "cloud"


def test_get_status_local_runtime_default(tmp_path: Path) -> None:
    p = _mk(tmp_path, task_id="t-loc", runtime="local")
    st = p.get_status("t-loc")
    assert st["runtime"] == "local"


def test_get_status_includes_cursor_ids_when_present(tmp_path: Path) -> None:
    p = _mk(
        tmp_path,
        task_id="t-ids",
        runtime="cloud",
        cursor_agent_id="bc-xyz",
        cursor_run_id="run-99",
    )
    st = p.get_status("t-ids")
    assert st["cursor_agent_id"] == "bc-xyz"
    assert st["cursor_run_id"] == "run-99"


def test_get_status_includes_cloud_phase(tmp_path: Path) -> None:
    p = _mk(
        tmp_path,
        task_id="t-ph",
        runtime="cloud",
        cursor_agent_id="a",
        cursor_run_id="r",
        cloud_phase="CREATING",
    )
    st = p.get_status("t-ph")
    assert st["cloud_phase"] == "CREATING"


def test_get_status_cloud_fields_none_for_local(tmp_path: Path) -> None:
    """Local tasks expose the same keys; cloud-specific ids are ``None``."""
    p = _mk(tmp_path, task_id="t-loc2", runtime="local")
    st = p.get_status("t-loc2")
    assert st["cursor_agent_id"] is None
    assert st["cursor_run_id"] is None
    assert st["cloud_phase"] is None


def test_list_includes_runtime_field(tmp_path: Path) -> None:
    p = _mk(tmp_path, task_id="la1", runtime="cloud", cursor_agent_id="a", cursor_run_id="r")
    act = p.list_active()
    assert len(act) == 1
    assert act[0]["runtime"] == "cloud"
    all_rows = p.list_all(include_terminal=True)
    assert any(r["task_id"] == "la1" and r["runtime"] == "cloud" for r in all_rows)


def test_summary_backward_compat_keys_unchanged(tmp_path: Path) -> None:
    p = _mk(tmp_path, task_id="bk", runtime="cloud", cursor_agent_id="a", cursor_run_id="r")
    full = p.get_status("bk")
    for key in (
        "task_id",
        "cli",
        "state",
        "pid",
        "started_at",
        "exit_code",
        "completed_at",
        "latest_event_index",
        "arktower_task_id",
        "persisted",
    ):
        assert key in full


def test_status_json_serialization_roundtrip(tmp_path: Path) -> None:
    p = _mk(
        tmp_path,
        task_id="js1",
        runtime="cloud",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        cloud_phase="RUNNING",
    )
    raw = json.dumps(p.get_status("js1"))
    assert "cloud" in raw
    assert "js1" in raw
