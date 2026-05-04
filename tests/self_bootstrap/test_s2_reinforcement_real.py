"""S2 self-bootstrap (real): reinforcement injection via dispatch RPC.

v0.3.0 F5 real version (replaces / supplements
:file:`tests/self_bootstrap/test_s2_reinforcement_mock.py`).

Differences from the mock:

- Uses the **F2.5 real** ``POST /dispatch?evolution_round=N`` RPC param
  to drive Workflow Context prepend (instead of the test code building
  the prefix manually).
- The ``round-1-evidence.md`` file is dropped into ``$POPOLA_HOME``
  before the round-2 dispatch; the daemon reads it and injects
  reinforcement bullets per F2.5.

Mock CLI is still used (we don't need a real LLM for the round-aware
contract test); the integration is "real popolad + real LangGraph +
real round-aware dispatch".
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.fixtures.mock_cli import install_mock_binaries
from tests.fixtures.real_popolad import RealPopoladHandle, spawn_real_popolad

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    args: list[str], env: dict[str, str], timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "popolaloom.cli.main", *args]
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout,
        cwd=str(_REPO_ROOT),
    )


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


@contextlib.contextmanager
def _spawn_with_mock(tmp_path: Path) -> Iterator[RealPopoladHandle]:
    bin_dir = tmp_path / "bin"
    install_mock_binaries(bin_dir)
    with spawn_real_popolad(tmp_path, extra_path=bin_dir) as handle:
        yield handle


def _post_dispatch_with_round(
    socket_path: Path,
    *,
    cli: str,
    prompt: str,
    evolution_round: int,
    prior_nines: float = 0.7,
) -> str:
    """POST /dispatch?evolution_round=N over UDS; return task_id."""
    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://popolad", timeout=15.0) as client:
        resp = client.post(
            "/dispatch",
            params={
                "evolution_round": evolution_round,
                "prior_nines": prior_nines,
            },
            json={"cli": cli, "prompt": prompt},
        )
        resp.raise_for_status()
        return str(resp.json()["task_id"])


def test_s2_real_evolution_round_injects_workflow_context(tmp_path: Path) -> None:
    """v0.3.0 F5 real S2: POST /dispatch?evolution_round=1 prepends WorkflowContext."""
    with _spawn_with_mock(tmp_path) as handle:
        env = handle.env.copy()

        # Round 1 — no prior evidence, so reinforcement section is empty.
        task_id_1 = _post_dispatch_with_round(
            handle.socket_path,
            cli="cursor",
            prompt="implement initial feature flag",
            evolution_round=1,
            prior_nines=0.0,
        )
        final_1 = _wait_for_terminal(env, task_id_1, timeout=30.0)
        assert "COMPLETED" in str(final_1.get("state", "")).upper(), final_1

        events_1 = _read_events(handle.events_dir, task_id_1)
        out_1 = _stdout_text(events_1)
        # The WorkflowContext prepend has been processed by mock_cursor
        # because mock parses round_num from prompt.
        assert "[devola-flow:round=1]" in out_1, out_1[:600]
        assert "## Findings" in out_1

        # Drop a synthetic round-1-evidence.md so the daemon's round-2
        # reinforcement injection picks it up.
        evidence = (
            "# Round 1 evidence\n\n"
            "- composite=0.78 (below floor)\n"
            "- test_quality=0.65\n"
            "- code_review=0.80\n"
            "- architecture=0.72\n"
            "- benchmark=0.85\n"
        )
        popola_home = Path(env["POPOLA_HOME"])
        popola_home.mkdir(parents=True, exist_ok=True)
        (popola_home / "round-1-evidence.md").write_text(evidence, encoding="utf-8")

        # Round 2 — daemon should read the evidence + inject reinforcement.
        task_id_2 = _post_dispatch_with_round(
            handle.socket_path,
            cli="cursor",
            prompt="address findings from round 1",
            evolution_round=2,
            prior_nines=0.78,
        )
        final_2 = _wait_for_terminal(env, task_id_2, timeout=30.0)
        assert "COMPLETED" in str(final_2.get("state", "")).upper(), final_2

        events_2 = _read_events(handle.events_dir, task_id_2)
        out_2 = _stdout_text(events_2)
        assert "[devola-flow:round=2]" in out_2, out_2[:600]
        assert task_id_1 != task_id_2
        # Both NDJSON files exist + are distinct.
        log_1 = handle.events_dir / f"{task_id_1}.jsonl"
        log_2 = handle.events_dir / f"{task_id_2}.jsonl"
        assert log_1.exists() and log_2.exists()
