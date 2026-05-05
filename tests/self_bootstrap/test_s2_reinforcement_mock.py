"""S2 self-bootstrap (mock): dev↔test reinforcement across rounds.

Per spec.md §3.4.1 S2 + roadmap §3.4 v0.2.3 — exercises the
reinforcement-rule injection pattern by:

1. Round 1: dispatch via mock_cursor with default content; mock emits
   the standard 3-section output.
2. Parse round 1's findings (info: ...) into a synthetic top-2
   reinforcement_rules list (this stands in for the v0.3.0 inner-gate
   parser).
3. Round 2: dispatch a second task with a prompt that **embeds the
   reinforcement_rules** in the body; mock_cursor reads the round_num
   marker out of the prompt (round=2) so the test can verify the
   round number propagated.
4. Verify both rounds reach COMPLETED + each has its own NDJSON event
   log + each captures the 3-section output with the right round
   number.

Real S2 (v0.3.0+) will plumb this through the daemon's
``WorkflowContext`` prepend in :mod:`popolaloom.evolution`; here the
test does the prepend manually so the schema-only v0.2.3 placeholder
is exercised end-to-end.
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

from popolaloom.evolution import WorkflowContext
from tests.fixtures.mock_cli import install_mock_binaries
from tests.fixtures.real_popolad import (
    RealPopoladHandle,
    spawn_real_popolad,
)

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[2]


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
def _spawn_with_mock(
    tmp_path: Path, extra_env: dict[str, str] | None = None
) -> Iterator[RealPopoladHandle]:
    """Boot popolad with mock CLIs on PATH and optional env injection."""
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
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _wait_for_terminal(
    env: dict[str, str], task_id: str, timeout: float = 20.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        result = _run_cli(["status", task_id, "--json"], env=env, timeout=15.0)
        if result.returncode == 0 and result.stdout.strip():
            with contextlib.suppress(json.JSONDecodeError, IndexError):
                last = json.loads(result.stdout.strip().splitlines()[-1])
            state = str(last.get("state", "")).upper()
            if "COMPLETED" in state or "FAILED" in state or "CANCELED" in state:
                return last
        time.sleep(0.2)
    return last


def _read_events(events_dir: Path, task_id: str) -> list[dict]:
    p = events_dir / f"{task_id}.jsonl"
    if not p.exists():
        return []
    events: list[dict] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return events


def _stdout_text(events: list[dict]) -> str:
    return "\n".join(
        e["data"].get("line", "") for e in events if e["type"] == "process.stdout"
    )


def test_s2_two_rounds_with_reinforcement_rules_propagation(tmp_path: Path) -> None:
    """S2: round 2 prompt embeds reinforcement rules from round 1 findings."""
    extra_env = {"MOCK_CURSOR_CONTENT": "round 1 mock body content"}
    with _spawn_with_mock(tmp_path, extra_env=extra_env) as handle:
        env = handle.env.copy()
        d1 = _run_cli(
            ["dispatch", "round 1 prompt round_num: 1", "--cli", "cursor", "--json"],
            env=env,
        )
        assert d1.returncode == 0, f"round 1 dispatch failed: {d1.stderr}"
        task_id_1 = json.loads(d1.stdout.strip().splitlines()[-1])["task_id"]
        final_1 = _wait_for_terminal(env, task_id_1)
        assert "COMPLETED" in str(final_1.get("state", "")).upper()

        events_1 = _read_events(handle.events_dir, task_id_1)
        out_1 = _stdout_text(events_1)
        assert "[devola-flow:round=1]" in out_1, (
            f"round 1 marker missing; stdout was:\n{out_1[:500]}"
        )
        assert "## Findings" in out_1
        finding_lines = [
            line.lstrip("- ").strip()
            for line in out_1.splitlines()
            if line.strip().startswith("- info:") or line.strip().startswith("- minor:")
        ]
        assert finding_lines, f"expected ≥ 1 finding line in round 1 stdout: {out_1[:500]}"

        ctx = WorkflowContext(
            round_num=2,
            max_rounds=5,
            prior_nines=0.886,
            reinforcement_rules=[
                f"From round 1: {finding_lines[0][:60]}",
                "Continue emitting 3-section devola-flow contract.",
            ],
        )
        prefix = ctx.render()
        prompt_2 = prefix + "\nRound 2: refine implementation based on round 1 findings.\n"

        d2 = _run_cli(
            ["dispatch", prompt_2, "--cli", "cursor", "--json"],
            env=env,
        )
        assert d2.returncode == 0, f"round 2 dispatch failed: {d2.stderr}"
        task_id_2 = json.loads(d2.stdout.strip().splitlines()[-1])["task_id"]
        final_2 = _wait_for_terminal(env, task_id_2)
        assert "COMPLETED" in str(final_2.get("state", "")).upper()

        events_2 = _read_events(handle.events_dir, task_id_2)
        out_2 = _stdout_text(events_2)
        assert "[devola-flow:round=2]" in out_2, (
            f"round 2 marker missing (mock should parse round_num from prompt); "
            f"stdout was:\n{out_2[:500]}"
        )
        assert task_id_1 != task_id_2
        log_1 = handle.events_dir / f"{task_id_1}.jsonl"
        log_2 = handle.events_dir / f"{task_id_2}.jsonl"
        assert log_1.exists() and log_2.exists()
        assert log_1.read_text() != log_2.read_text(), (
            "round 1 and round 2 NDJSON files should not be identical"
        )
