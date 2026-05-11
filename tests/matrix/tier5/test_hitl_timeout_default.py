"""Tier 5 — HITL deadline timeout default-option behaviour (v0.3.0 F4).

Per spec §12 deadline rule: when ``deadline_at`` passes without a
reply, ``HITLStore.process_timeout`` applies the prompt's
``default_option_id`` and marks the row as ``timeout`` (distinct from
``answered`` so audit shows the synthetic reply).

≥ 3 cases.
"""

from __future__ import annotations

from importlib import resources
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt, HITLStore
from popolaloom.hitl.triggers import create_round_floor_prompt


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db_path = tmp_path / "hitl.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migration_sql = (Path(resources.files("popolaloom.migrations")) / "006_popola_hitl.sql").read_text(encoding="utf-8")
    conn.executescript(migration_sql)
    conn.commit()
    return HITLStore(conn)


@pytest.mark.e2e
@pytest.mark.slow
def test_overdue_row_listed_by_list_overdue(hitl_store: HITLStore) -> None:
    prompt = HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No"),
        ],
        default_option_id="no",
        channels=["lark", "ide"],
        deadline_seconds=1,
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    hitl_id = hitl_store.create(prompt, deadline_at=past)
    overdue = hitl_store.list_overdue()
    assert any(r["hitl_id"] == hitl_id for r in overdue)


@pytest.mark.e2e
@pytest.mark.slow
def test_process_timeout_applies_default_option(hitl_store: HITLStore) -> None:
    prompt = HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide"],
        deadline_seconds=1,
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    hitl_id = hitl_store.create(prompt, deadline_at=past)
    transitioned = hitl_store.process_timeout(hitl_id)
    assert transitioned is True
    row = hitl_store.get(hitl_id)
    assert row is not None
    assert row["status"] == "timeout"
    assert row["answer_option_id"] == "no"
    assert "deadline reached" in (row["answer_reason"] or "")


@pytest.mark.e2e
@pytest.mark.slow
def test_process_timeout_does_not_apply_to_answered(hitl_store: HITLStore) -> None:
    """Already-answered rows are NOT re-stamped by timeout processor."""
    prompt = create_round_floor_prompt(
        round_num=1, blockers=["x"], evidence_paths=[]
    )
    past = datetime.now(UTC) - timedelta(hours=2)
    hitl_id = hitl_store.create(prompt, deadline_at=past)
    hitl_store.mark_answered(hitl_id, option_id="override", via="cli")
    transitioned = hitl_store.process_timeout(hitl_id)
    assert transitioned is False
    row = hitl_store.get(hitl_id)
    assert row["status"] == "answered"  # type: ignore[index]
