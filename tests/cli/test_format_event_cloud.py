"""Tests for CLI event summarization (cloud attach / task.failed richness)."""

from __future__ import annotations

import json

from popolaloom.cli.main import _summarize_data


def test_format_task_failed_includes_error_kind() -> None:
    s = _summarize_data(
        "task.failed",
        {"exit_code": 1, "error_kind": "missing_api_key"},
    )
    assert "exit_code=1" in s
    assert "error_kind=missing_api_key" in s


def test_format_task_failed_includes_runtime_when_cloud() -> None:
    s = _summarize_data(
        "task.failed",
        {
            "exit_code": 1,
            "runtime": "cloud",
            "error_kind": "missing_api_key",
        },
    )
    assert "runtime=cloud" in s
    assert "error_kind=missing_api_key" in s


def test_format_task_failed_omits_runtime_when_local() -> None:
    s = _summarize_data(
        "task.failed",
        {"exit_code": 0, "runtime": "local"},
    )
    assert "exit_code=0" in s
    assert "runtime=local" not in s


def test_format_task_failed_with_error_dict() -> None:
    s = _summarize_data(
        "task.failed",
        {
            "exit_code": 1,
            "error": {
                "error_type": "CursorCloudAuthError",
                "is_retryable": False,
                "message": "unauthorized",
            },
        },
    )
    assert "error_type=CursorCloudAuthError" in s


def test_format_task_completed_local_unchanged() -> None:
    s = _summarize_data("task.completed", {"exit_code": 0})
    assert s == "exit_code=0"


def test_format_cloud_queued() -> None:
    s = _summarize_data(
        "cloud.queued",
        {
            "task_id": "t1",
            "agent_id": "bc-abc",
            "run_id": "run-xyz",
            "initial_phase": "CREATING",
        },
    )
    assert "agent_id='bc-abc'" in s
    assert "run_id='run-xyz'" in s
    assert "initial_phase='CREATING'" in s


def test_format_cloud_queued_no_data() -> None:
    s = _summarize_data("cloud.queued", {})
    assert json.loads(s) == {}


def test_format_task_completed_includes_runtime_when_cloud() -> None:
    s = _summarize_data(
        "task.completed",
        {"exit_code": 0, "runtime": "cloud"},
    )
    assert "exit_code=0" in s
    assert "runtime=cloud" in s
