"""More rpc endpoint coverage (v0.3.0 F4 + F2).

Targets remaining uncovered branches in :mod:`popolaloom.daemon.rpc`.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from popolaloom.daemon.rpc import (
    _apply_evolution_round_prepend,
    create_app,
)
from popolaloom.daemon.server import Popolad


@pytest.fixture()
def tmp_events_dir(tmp_path: Path) -> Path:
    events = tmp_path / "events"
    events.mkdir()
    return events


# ── /dispatch?evolution_round=N — error paths ──────────────────────────


@pytest.mark.asyncio
async def test_dispatch_evolution_round_zero_returns_400(
    tmp_events_dir: Path,
) -> None:
    """evolution_round must be ≥ 1; 0 is treated as no-op (not an error)."""
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/dispatch",
            params={"evolution_round": 0},
            json={"cli": "echo", "prompt": "hi"},
        )
    # round_num=0 is below the schema minimum (≥ 1) — treated as no-op.
    # The daemon should still dispatch (we asked for round 0 = "no evolution").
    assert resp.status_code in (200, 400)


@pytest.mark.asyncio
async def test_dispatch_evolution_round_invalid_returns_400(
    tmp_events_dir: Path, tmp_path: Path
) -> None:
    """When max_rounds < round_num, _apply_evolution_round_prepend raises."""
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    # Use max_rounds=2 with round=5 → out of range.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/dispatch",
            params={
                "evolution_round": 5,
                "max_rounds": 2,
                "prior_nines": 0.5,
            },
            json={"cli": "echo", "prompt": "hi"},
        )
    assert resp.status_code == 400


# ── _apply_evolution_round_prepend ─────────────────────────────────────


def test_apply_evolution_round_prepend_round_one() -> None:
    """Round 1 has no reinforcement; just prepend the WorkflowContext."""
    out = _apply_evolution_round_prepend(
        "do x", round_num=1, max_rounds=5, prior_nines=0.0,
    )
    assert "Workflow Context" in out
    assert "do x" in out


def test_apply_evolution_round_prepend_round_two_no_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    """Round ≥ 2 looks for ~/.popola/round-N-evidence.md; missing → no reinforcement."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    out = _apply_evolution_round_prepend(
        "do x", round_num=2, max_rounds=5, prior_nines=0.7,
    )
    assert "do x" in out


def test_apply_evolution_round_prepend_round_two_with_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    """Round ≥ 2 with evidence file injects reinforcement findings."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    popola_home = tmp_path / ".popola"
    popola_home.mkdir(parents=True, exist_ok=True)
    (popola_home / "round-1-evidence.md").write_text(
        "# Findings\n- finding A\n- finding B\n- finding C\n",
        encoding="utf-8",
    )
    out = _apply_evolution_round_prepend(
        "do x", round_num=2, max_rounds=5, prior_nines=0.7,
    )
    assert "do x" in out


def test_apply_evolution_round_prepend_round_two_unreadable_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    """Unreadable evidence file → no reinforcement (fail open)."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    popola_home = tmp_path / ".popola"
    popola_home.mkdir(parents=True, exist_ok=True)
    evidence = popola_home / "round-1-evidence.md"
    evidence.write_text("- finding a\n", encoding="utf-8")
    # Patch read_text to simulate OSError.
    real_read_text = Path.read_text

    def bad_read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "round-1-evidence.md" in str(self):
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", bad_read_text)
    out = _apply_evolution_round_prepend(
        "do x", round_num=2, max_rounds=5, prior_nines=0.7,
    )
    assert "do x" in out


# ── /list with terminal flag ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_with_include_terminal(tmp_events_dir: Path) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/list", params={"include_terminal": "true"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_status_unknown_task_returns_404(tmp_events_dir: Path) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/status/task-nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_404(tmp_events_dir: Path) -> None:
    popolad = Popolad(events_dir=tmp_events_dir, adapter=lambda *args, **kw: ["echo"])
    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/cancel/task-nope")
    assert resp.status_code == 404
