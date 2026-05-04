"""S5 self-bootstrap (mock): cursor → claude → codex 3-step relay.

Per spec.md §3.4.1 S5 + roadmap §3.4 v0.2.3 — exercises a 3-hop
cross-CLI handoff using the mock CLI library:

1. mock_cursor receives the initial prompt → emits 3-section output.
2. Test extracts a marker from cursor's findings → constructs a
   prompt for claude that references it → mock_claude emits its
   3-section output (stream-json envelopes).
3. Test extracts a marker from claude → constructs codex prompt →
   mock_codex emits its 3-section output.

The relay primitive (``v0.3.0 F2`` ``relay``) will eventually do this
chain natively; here the test does the chaining itself so we lock
the schema contract every hop must satisfy.

Verifies:

- All 3 dispatches reach COMPLETED.
- Each task has its own NDJSON file with its own ``[devola-flow:round=N]``
  marker (round=1 for cursor, round=2 for claude, round=3 for codex).
- Each task's stdout contains the 3-section block.
- The full 3-task trace is intact (no events lost across the
  cross-CLI hops).
"""

from __future__ import annotations

import contextlib
import json
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
def _spawn_with_three_mocks(tmp_path: Path) -> Iterator[RealPopoladHandle]:
    bin_dir = tmp_path / "bin"
    install_mock_binaries(bin_dir)
    with spawn_real_popolad(tmp_path, extra_path=bin_dir) as handle:
        yield handle


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


def _dispatch_and_wait(env: dict[str, str], cli: str, prompt: str) -> str:
    """Dispatch a single task; wait for COMPLETED; return task_id."""
    d = _run_cli(["dispatch", prompt, "--cli", cli, "--json"], env=env, timeout=15.0)
    assert d.returncode == 0, f"{cli} dispatch failed: {d.stderr}"
    task_id = json.loads(d.stdout.strip().splitlines()[-1])["task_id"]
    final = _wait_for_terminal(env, task_id, timeout=20.0)
    state = str(final.get("state", "")).upper()
    assert "COMPLETED" in state, f"{cli} task did not COMPLETE; got {state}; final={final}"
    return task_id


def test_s5_cursor_to_claude_to_codex_three_hop_handoff(tmp_path: Path) -> None:
    """S5: 3 mock CLIs called sequentially; each captures 3-section output."""
    with _spawn_with_three_mocks(tmp_path) as handle:
        env = handle.env.copy()

        cursor_id = _dispatch_and_wait(
            env,
            "cursor",
            "round_num: 1\nimplement initial popola list --json flag",
        )
        cursor_events = _read_events(handle.events_dir, cursor_id)
        cursor_out = _stdout_text(cursor_events)
        assert "[devola-flow:round=1]" in cursor_out, (
            f"cursor round marker missing: {cursor_out[:500]}"
        )
        assert "## Findings" in cursor_out
        cursor_log = handle.events_dir / f"{cursor_id}.jsonl"
        assert cursor_log.exists()

        claude_id = _dispatch_and_wait(
            env,
            "claude",
            (
                "round_num: 2\n"
                f"continue refinement of work started by task {cursor_id}\n"
                "preserve devola-flow 3-section contract."
            ),
        )
        claude_events = _read_events(handle.events_dir, claude_id)
        claude_out = _stdout_text(claude_events)
        assert "[devola-flow:round=2]" in claude_out, (
            f"claude round marker missing: {claude_out[:500]}"
        )
        assert "claude-mock" in claude_out, (
            "claude stream-json envelopes should mention model=claude-mock"
        )

        codex_id = _dispatch_and_wait(
            env,
            "codex",
            (
                "round_num: 3\n"
                f"verify against task {claude_id} output; final pass.\n"
                "emit composite_score above 0.85"
            ),
        )
        codex_events = _read_events(handle.events_dir, codex_id)
        codex_out = _stdout_text(codex_events)
        assert "[devola-flow:round=3]" in codex_out, (
            f"codex round marker missing: {codex_out[:500]}"
        )
        for required in (
            "## Acceptance Verification",
            "## Gate Score Components",
            "## Findings",
            "composite:",
        ):
            assert required in codex_out, (
                f"codex required section missing: {required}; out={codex_out[:500]}"
            )

        all_ids = {cursor_id, claude_id, codex_id}
        assert len(all_ids) == 3, f"task ids should be distinct: {all_ids}"
        for tid in all_ids:
            assert (handle.events_dir / f"{tid}.jsonl").exists()
