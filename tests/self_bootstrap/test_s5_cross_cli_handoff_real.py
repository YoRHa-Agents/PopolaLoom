"""S5 self-bootstrap (real): real relay primitive 3-hop chain (v0.3.0 F5).

v0.3.0 real version replacing
:file:`tests/self_bootstrap/test_s5_cross_cli_handoff_mock.py`.

v0.9.0 (BL-v0.9.0-1) — the relay primitive now emits a canonical
:class:`popolaloom.handoff.HandoffEnvelope` instead of the legacy
v0.3.0 ``RelayHandoffEnvelope`` (Q-D-3 lock); the parent linkage field
is ``parent_task_id`` (was ``source_task_id`` on the legacy schema).

Differences from the mock:

- Uses the **F2 real** ``POST /relay`` RPC primitive (instead of the
  test code building the second/third dispatches by hand).
- Each child task carries a ``handoff_envelope`` in its ``extra`` bag
  pointing back to the parent (``parent_task_id`` chain).

Mock CLI binaries are still on PATH because we can't rely on real
cursor / claude / codex being installed in CI. The "real" part is
the relay primitive + handoff_envelope contract.
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


@contextlib.contextmanager
def _spawn_with_three_mocks(tmp_path: Path) -> Iterator[RealPopoladHandle]:
    bin_dir = tmp_path / "bin"
    install_mock_binaries(bin_dir)
    with spawn_real_popolad(tmp_path, extra_path=bin_dir) as handle:
        yield handle


def _post_dispatch(socket_path: Path, *, cli: str, prompt: str) -> str:
    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://popolad", timeout=15.0) as client:
        resp = client.post("/dispatch", json={"cli": cli, "prompt": prompt})
        resp.raise_for_status()
        return str(resp.json()["task_id"])


def _post_relay(
    socket_path: Path,
    *,
    source_task_id: str,
    target_cli: str,
    payload: dict,
    reason: str,
    source_cli: str | None = None,
) -> dict:
    transport = httpx.HTTPTransport(uds=str(socket_path))
    body = {
        "source_task_id": source_task_id,
        "target_cli": target_cli,
        "payload": payload,
        "reason": reason,
    }
    if source_cli is not None:
        body["source_cli"] = source_cli
    with httpx.Client(transport=transport, base_url="http://popolad", timeout=15.0) as client:
        resp = client.post("/relay", json=body)
        resp.raise_for_status()
        return resp.json()


def test_s5_real_three_hop_relay_chain(tmp_path: Path) -> None:
    """v0.3.0 F5 real S5: cursor → claude → codex via real /relay RPC."""
    with _spawn_with_three_mocks(tmp_path) as handle:
        env = handle.env.copy()

        # Hop 1: the user-initiated task.
        cursor_id = _post_dispatch(
            handle.socket_path,
            cli="cursor",
            prompt="round_num: 1\nimplement initial popola list --json flag",
        )
        final_cursor = _wait_for_terminal(env, cursor_id, timeout=30.0)
        assert "COMPLETED" in str(final_cursor.get("state", "")).upper()

        # Hop 2: relay cursor → claude (real RPC).
        relay_to_claude = _post_relay(
            handle.socket_path,
            source_task_id=cursor_id,
            target_cli="claude",
            payload={"artifact": "diff://cursor-output", "kind": "summary"},
            reason="Cross-CLI peer review by claude",
        )
        claude_id = relay_to_claude["child_task_id"]
        final_claude = _wait_for_terminal(env, claude_id, timeout=30.0)
        assert "COMPLETED" in str(final_claude.get("state", "")).upper()
        assert relay_to_claude["handoff_envelope"]["source_cli"] == "cursor"
        assert relay_to_claude["handoff_envelope"]["target_cli"] == "claude"
        assert relay_to_claude["handoff_envelope"]["parent_task_id"] == cursor_id

        # Hop 3: relay claude → codex (real RPC).
        relay_to_codex = _post_relay(
            handle.socket_path,
            source_task_id=claude_id,
            target_cli="codex",
            payload={"artifact": "diff://claude-review", "kind": "summary"},
            reason="Final pass by codex",
        )
        codex_id = relay_to_codex["child_task_id"]
        final_codex = _wait_for_terminal(env, codex_id, timeout=30.0)
        assert "COMPLETED" in str(final_codex.get("state", "")).upper()
        assert relay_to_codex["handoff_envelope"]["source_cli"] == "claude"
        assert relay_to_codex["handoff_envelope"]["target_cli"] == "codex"

        # Three distinct task ids.
        all_ids = {cursor_id, claude_id, codex_id}
        assert len(all_ids) == 3, f"task ids should be distinct: {all_ids}"

        # Three distinct NDJSON files.
        for tid in all_ids:
            log = handle.events_dir / f"{tid}.jsonl"
            assert log.exists(), f"missing event log for {tid}"

        # The handoff_envelope chain is the canonical proof — each
        # child carries its parent's id forward through ``parent_task_id``
        # (v0.9.0 BL-v0.9.0-1; v0.7.3+ HandoffEnvelope schema).
        assert relay_to_claude["handoff_envelope"]["parent_task_id"] == cursor_id
        assert relay_to_codex["handoff_envelope"]["parent_task_id"] == claude_id
