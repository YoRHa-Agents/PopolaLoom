"""F2.5 evolution_round dispatch RPC tests (≥3 cases).

Per v0.3.0-plan.md §4 Stage F2.5.4 — verifies that POST /dispatch
with the optional ``evolution_round=N`` query parameter prepends a
Workflow Context section + (optional) reinforcement section before
the user prompt.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from popolaloom.daemon import Popolad
from popolaloom.daemon.rpc import create_app


def _capture_adapter_factory() -> tuple[list[list[str]], object]:
    """Return (calls_list, adapter) — adapter records full prompt arg.

    The dispatch RPC writes the prompt to ``state.prompt`` (visible in
    the per-task event log).  We use this adapter to introspect the
    actual prompt the daemon passed downstream so tests can assert
    the prepend transformation happened.
    """
    captured: list[list[str]] = []

    def adapter(
        cli: str, prompt: str, cwd: Any = None, extra: Any = None  # type: ignore[name-defined]
    ) -> list[str]:
        import sys

        captured.append([cli, prompt])
        return [
            sys.executable,
            "-c",
            "print('ok'); import sys; sys.exit(0)",
        ]

    return captured, adapter


from typing import Any  # noqa: E402  (helper above types it)


@pytest.fixture
async def http_client(tmp_path: Path):
    captured: list[tuple[str, str]] = []

    def adapter(
        cli: str, prompt: str, cwd: Any = None, extra: Any = None
    ) -> list[str]:
        import sys

        captured.append((cli, prompt))
        return [
            sys.executable,
            "-c",
            "print('ok'); import sys; sys.exit(0)",
        ]

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=adapter,
        use_graph=False,
    )
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://_"
    ) as client:
        yield client, captured, tmp_path


@pytest.mark.asyncio
async def test_dispatch_with_evolution_round_one_prepends_workflow_context(
    http_client,
) -> None:
    """evolution_round=1 prepends a Workflow Context section."""
    client, captured, _tmp = http_client
    response = await client.post(
        "/dispatch",
        json={"cli": "cursor", "prompt": "build feature X"},
        params={
            "evolution_round": 1,
            "max_rounds": 5,
            "prior_nines": 0.0,
        },
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    cli_seen, prompt_seen = captured[0]
    assert cli_seen == "cursor"
    assert "## Workflow Context (devola-flow)" in prompt_seen
    assert "round_num: 1" in prompt_seen
    assert "build feature X" in prompt_seen


@pytest.mark.asyncio
async def test_dispatch_with_evolution_round_two_includes_reinforcement(
    http_client, tmp_path: Path, monkeypatch
) -> None:
    """When ~/.popola/round-1-evidence.md exists, round-2 prompt has reinforcement."""
    client, captured, base_tmp = http_client

    fake_home = base_tmp / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    evidence_path = fake_home / ".popola" / "round-1-evidence.md"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        "## Findings (round 1)\n"
        "- [blocker] data race in event log writer\n"
        "- [critical] missing rollback in supervisor\n",
        encoding="utf-8",
    )

    response = await client.post(
        "/dispatch",
        json={"cli": "claude", "prompt": "address findings"},
        params={
            "evolution_round": 2,
            "max_rounds": 5,
            "prior_nines": 0.83,
        },
    )
    assert response.status_code == 200, response.text
    cli_seen, prompt_seen = captured[-1]
    assert cli_seen == "claude"
    assert "round_num: 2" in prompt_seen
    assert "Reinforcement" in prompt_seen
    assert "data race in event log writer" in prompt_seen


@pytest.mark.asyncio
async def test_dispatch_with_evolution_round_two_no_prior_evidence_no_crash(
    http_client, tmp_path: Path, monkeypatch
) -> None:
    """Round 2 without prior evidence file → empty reinforcement, no crash."""
    client, captured, base_tmp = http_client
    fake_home = base_tmp / "fake_home_nofile"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    response = await client.post(
        "/dispatch",
        json={"cli": "codex", "prompt": "round-2 work"},
        params={
            "evolution_round": 2,
            "max_rounds": 5,
            "prior_nines": 0.85,
        },
    )
    assert response.status_code == 200, response.text
    _cli, prompt_seen = captured[-1]
    assert "round_num: 2" in prompt_seen
    assert "round-2 work" in prompt_seen


@pytest.mark.asyncio
async def test_dispatch_without_evolution_round_unchanged_prompt(
    http_client,
) -> None:
    """Dispatch without evolution_round leaves prompt verbatim (back-compat)."""
    client, captured, _tmp = http_client
    response = await client.post(
        "/dispatch",
        json={"cli": "cursor", "prompt": "regular prompt"},
    )
    assert response.status_code == 200, response.text
    _cli, prompt_seen = captured[-1]
    assert prompt_seen == "regular prompt"
    assert "## Workflow Context (devola-flow)" not in prompt_seen
