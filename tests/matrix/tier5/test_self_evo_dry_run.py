"""Tier 5 — end-to-end self-evolution dry-run (mock CLI, real popolad).

Per testing-matrix.md §1.5 example
``test_self_evo_round_dry_run.py::test_full_round_with_mock_cursor_emits_three_section_output_and_inner_gate_passes``.

These cases drive the full closed loop:

1. Real ``python -m popolaloom.daemon`` subprocess (via
   :func:`tests.fixtures.real_popolad.spawn_real_popolad`).
2. ``$PATH`` extended with mock binaries (``cursor-agent`` / ``claude``
   / ``codex``) installed by
   :func:`tests.fixtures.mock_cli.install_mock_binaries`.
3. CLI dispatch ``popola dispatch "..."`` → daemon spawns the mock
   binary → mock emits the devola-flow 3-section output → daemon
   captures stdout into NDJSON event log.
4. Verify the full chain: dispatch → attach → event log → ArkTower
   persistence → mock output contains the three section headers.

2 cases (target ≥ 2):

1. Success path — mock_cursor exit 0 + 3-section output → task COMPLETED.
2. Mock failure path — mock_cursor exit 1 → task FAILED + 3-section
   output still captured (no silent failure).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.mock_cli import install_mock_binaries
from tests.fixtures.real_popolad import (
    RealPopoladHandle,
    spawn_real_popolad,
)

pytestmark = [pytest.mark.e2e, pytest.mark.nightly]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_cli(
    args: list[str], env: dict[str, str], timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "popolaloom.cli.main", *args]
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO_ROOT),
    )


@contextlib.contextmanager
def _spawn_popolad_with_mock_cli(
    tmp_path: Path, extra_env: dict[str, str] | None = None
) -> Iterator[RealPopoladHandle]:
    """Spawn popolad with mock CLIs on PATH and optional env overrides.

    The daemon process inherits ``os.environ`` at spawn time, so any
    ``MOCK_*`` env vars MUST be set on this process **before** the
    spawn returns.  This helper does that with monkeypatch-style
    teardown so concurrent tests don't leak env state.
    """
    bin_dir = tmp_path / "bin"
    install_mock_binaries(bin_dir)
    saved: dict[str, str | None] = {}
    if extra_env:
        for k, v in extra_env.items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v
    try:
        with spawn_real_popolad(tmp_path, extra_path=bin_dir) as handle:
            yield handle
    finally:
        for k, original in saved.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original


def _wait_for_task_terminal(
    env: dict[str, str], task_id: str, timeout: float = 20.0
) -> dict[str, object]:
    """Poll ``popola status <task_id> --json`` until terminal or timeout."""
    deadline = time.monotonic() + timeout
    last_payload: dict[str, object] = {}
    while time.monotonic() < deadline:
        result = _run_cli(["status", task_id, "--json"], env=env, timeout=15.0)
        if result.returncode == 0 and result.stdout.strip():
            with contextlib.suppress(json.JSONDecodeError, IndexError):
                last_payload = json.loads(result.stdout.strip().splitlines()[-1])
            state = str(last_payload.get("state", "")).upper()
            if "COMPLETED" in state or "FAILED" in state or "CANCELED" in state:
                return last_payload
        time.sleep(0.2)
    return last_payload


def _read_events_file(events_dir: Path, task_id: str) -> list[dict]:
    """Return all NDJSON events from ``events_dir/<task_id>.jsonl`` (best effort)."""
    p = events_dir / f"{task_id}.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def test_full_self_evo_round_with_mock_cursor_success_path(tmp_path: Path) -> None:
    """Case 1: dispatch via mock_cursor → COMPLETED + 3-section output captured."""
    extra_env = {
        "MOCK_CURSOR_ROUND": "2",
        "MOCK_CURSOR_CONTENT": "implemented popola list --json flag (mock patch)",
    }
    with _spawn_popolad_with_mock_cli(tmp_path, extra_env=extra_env) as handle:
        env = handle.env.copy()
        dispatch = _run_cli(
            ["dispatch", "implement popola list --json", "--cli", "cursor", "--json"],
            env=env,
            timeout=15.0,
        )
        assert dispatch.returncode == 0, (
            f"dispatch failed: rc={dispatch.returncode}\n"
            f"{dispatch.stdout}\n{dispatch.stderr}"
        )
        payload = json.loads(dispatch.stdout.strip().splitlines()[-1])
        task_id = payload["task_id"]
        assert task_id

        final = _wait_for_task_terminal(env, task_id, timeout=20.0)
        state = str(final.get("state", "")).upper()
        assert "COMPLETED" in state, (
            f"expected COMPLETED, got state={state!r}, full={final!r}"
        )

        events = _read_events_file(handle.events_dir, task_id)
        types = [e["type"] for e in events]
        assert "task.dispatched" in types
        assert "task.completed" in types

        stdout_lines = [
            e["data"].get("line", "")
            for e in events
            if e["type"] == "process.stdout"
        ]
        full_stdout = "\n".join(stdout_lines)
        assert "[devola-flow:round=2]" in full_stdout, (
            f"first-line marker missing; stdout was:\n{full_stdout[:1000]!r}"
        )
        for required in (
            "## Acceptance Verification",
            "## Gate Score Components",
            "## Findings",
            "composite:",
        ):
            assert required in full_stdout, (
                f"required section missing: {required!r}; "
                f"stdout was:\n{full_stdout[:1000]!r}"
            )

        ark_id = final.get("arktower_task_id")
        assert ark_id, "ArkTower persistence must produce an arktower_task_id"


def test_full_self_evo_round_with_mock_cursor_failure_path(tmp_path: Path) -> None:
    """Case 2: mock_cursor exit_code=1 → task FAILED + sections still captured."""
    extra_env = {
        "MOCK_CURSOR_EXIT_CODE": "1",
        "MOCK_CURSOR_ROUND": "5",
        "MOCK_CURSOR_CONTENT": "mock failure path; emitting 3-section anyway",
    }
    with _spawn_popolad_with_mock_cli(tmp_path, extra_env=extra_env) as handle:
        env = handle.env.copy()
        dispatch = _run_cli(
            ["dispatch", "force failure", "--cli", "cursor", "--json"],
            env=env,
            timeout=15.0,
        )
        assert dispatch.returncode == 0
        payload = json.loads(dispatch.stdout.strip().splitlines()[-1])
        task_id = payload["task_id"]

        final = _wait_for_task_terminal(env, task_id, timeout=20.0)
        state = str(final.get("state", "")).upper()
        assert "FAILED" in state, (
            f"expected FAILED with exit_code=1; got state={state!r}, full={final!r}"
        )

        events = _read_events_file(handle.events_dir, task_id)
        types = [e["type"] for e in events]
        assert "task.failed" in types

        stdout_lines = [
            e["data"].get("line", "")
            for e in events
            if e["type"] == "process.stdout"
        ]
        full_stdout = "\n".join(stdout_lines)
        assert "[devola-flow:round=5]" in full_stdout
        assert "## Findings" in full_stdout, (
            "Findings section must be present even on failure (No Silent Failures)"
        )
