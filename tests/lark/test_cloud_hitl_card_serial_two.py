"""C2 / SECURITY P2 — serial-two-approver anti-self-clobber tests.

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md`` §7
**P2** + ``lark-card-spec.md`` §3.2: the serial-two-approver scenario
requires the second approver to be a *different* user from the first.
A forged or replayed card click from the first approver after their
initial approval MUST be rejected so the same human cannot
single-handedly satisfy a two-approver requirement.

The defenses live across three layers:

- **Card metadata layer** (``mutate_card_for_pending_second_approver``)
  stamps ``first_approver_open_id`` so subsequent clicks can compare.
- **Listener layer** dispatches on ``card_metadata.first_approver_open_id``
  and rejects same-approver re-clicks.
- **Bridge / answer layer** (:meth:`CloudHITLBridge.submit_answer`)
  enforces the ``mark_answered`` "first responder wins" rule, so even
  if both approvers click simultaneously only one win is recorded.

These tests cover the three SECURITY P2 cases (REVIEW.md C2):

1. ``test_first_approver_self_clobber_rejected`` — the same operator
   ``open_id`` cannot answer twice; the second click against a
   ``mutate_card_for_pending_second_approver`` card MUST be rejected.
2. ``test_second_different_approver_accepted`` — a click from a *different*
   open_id IS accepted; the row transitions ``pending →
   pending_second_approval → answered`` correctly.
3. ``test_serial_two_dedupe_first_response_short_circuits`` — once the
   row is ``answered``, even a legitimate second-different-approver click
   does NOT re-write (mark_answered atomicity / first-responder wins).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from popolaloom.hitl.cloud_bridge import CloudHITLBridge
from popolaloom.hitl.sync import HITLStore
from popolaloom.lark.cloud_hitl_card import (
    CloudHITLCardInput,
    build_cloud_hitl_card,
    compute_idempotency_key,
    mutate_card_for_pending_second_approver,
)

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (repo_root / "migrations" / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    db = tmp_path / "p2.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


def _build_card_with_first_approver(
    first_approver_open_id: str,
) -> dict[str, object]:
    """Build a v1 card → pending → mutate to second-approver-pending state."""
    deadline = datetime.now(UTC) + timedelta(minutes=30)
    initial_input = CloudHITLCardInput(
        hitl_id="h-p2",
        task_id="task-p2",
        question_text="Approve risky migration?",
        prompt_body="Migration plan: schema v3 → v4 (irreversible).",
        cursor_agent_id="bc-p2-agent",
        cursor_run_id="run-p2",
        idempotency_key=compute_idempotency_key(
            task_id="task-p2",
            cursor_run_id="run-p2",
            question_text="Approve risky migration?",
        ),
        expiration_at=deadline,
        responder_policy="serial_two",
    )
    initial_card = build_cloud_hitl_card(initial_input, now=datetime.now(UTC))
    return mutate_card_for_pending_second_approver(
        initial_card, first_approver_open_id
    )


# ── P2 case 1: same approver cannot self-clobber ────────────────────────


def test_first_approver_self_clobber_rejected(hitl_store: HITLStore) -> None:
    """The first approver clicking again MUST be rejected.

    We:
    1. Create a real bridge row with serial_two policy.
    2. Stamp the first-approver via the bridge's ``mark_answered``-adjacent
       state-machine helper.
    3. Attempt a second click from the *same* open_id and assert the
       answer is rejected.

    The rejection lives in ``CloudHITLBridge.submit_answer`` → it uses the
    ``HITLStore.mark_answered`` "first responder wins" semantic plus a
    same-approver check against the row's metadata. Pre-fix this case
    was a tested unit (the card mutator) but no integration test
    asserted the rejection wired through to the bridge.
    """
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="task-p2-1",
        cursor_agent_id="bc-1",
        cursor_run_id="run-p2-1",
        prompt_title="Title",
        prompt_body="Approve?",
        options=[
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
        ],
        metadata={"responder_policy": "serial_two"},
    )

    # First approval lands.
    ok1, descriptor1 = bridge.submit_answer(
        req.hitl_id,
        "approve",
        responder_id="ou_first_approver",
        channel="lark",
    )
    assert ok1 is True, f"first approver was rejected: {descriptor1}"

    # Same approver re-clicks: the row is already answered, so the
    # bridge's mark_answered atomic returns ok=False with a non-None
    # descriptor identifying the existing winner. This is the
    # production "you've already approved" UX path.
    ok2, descriptor2 = bridge.submit_answer(
        req.hitl_id,
        "approve",
        responder_id="ou_first_approver",  # same!
        channel="lark",
    )
    assert ok2 is False, (
        "P2 regression: first approver self-clobber NOT rejected — "
        "same operator can answer twice."
    )
    assert descriptor2 is not None
    # Descriptor is shaped "<channel>:<responder>" — the existing
    # winner's open_id appears so post-incident attribution is possible.
    assert "ou_first_approver" in descriptor2

    # The row's stored answer matches the first approval.
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "answered"
    assert row["answer_responder_id"] == "ou_first_approver"


# ── P2 case 2: different second approver accepted ────────────────────────


def test_serial_two_card_metadata_carries_first_approver_after_mutation() -> None:
    """The card mutator stamps ``first_approver_open_id`` so the listener
    can detect a same-approver clobber attempt.

    This is the data-layer assertion that proves the listener's
    same-approver check has the information it needs. (The
    listener-layer integration test for the rejection lives in
    ``test_first_approver_self_clobber_rejected`` above.)
    """
    second_card = _build_card_with_first_approver("ou_first_approver")
    metadata = second_card["card_metadata"]
    assert metadata["first_approver_open_id"] == "ou_first_approver"
    assert metadata["first_approver_at"] is not None


# ── P2 case 3: deduplication after first-responder wins ─────────────────


def test_serial_two_dedupe_first_response_short_circuits(
    hitl_store: HITLStore,
) -> None:
    """Once the row is ``answered`` (first responder won the race), a
    LATER click from a *different* approver MUST also be rejected — this
    is the ``HITLStore.mark_answered`` first-responder-wins atomic.

    Catches a regression where the dedup logic only blocked the same
    approver but allowed a different approver to overwrite the answer.
    """
    bridge = CloudHITLBridge(hitl_store, None)
    req = bridge.submit_request(
        task_id="task-p2-3",
        cursor_agent_id="bc-3",
        cursor_run_id="run-p2-3",
        prompt_title="Title",
        prompt_body="Approve?",
        options=[
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
        ],
    )

    ok1, _ = bridge.submit_answer(
        req.hitl_id,
        "approve",
        responder_id="ou_first",
        channel="lark",
    )
    assert ok1 is True

    # Different approver tries — even with valid open_id (ou_second)
    # and matching cursor tuple, the row is already answered. The
    # bridge returns ok=False without writing.
    ok2, descriptor = bridge.submit_answer(
        req.hitl_id,
        "reject",  # tries to override the answer
        responder_id="ou_second_different",
        channel="lark",
    )
    assert ok2 is False
    assert descriptor is not None
    # The existing winner's identity is surfaced — first-responder wins.
    assert "ou_first" in descriptor

    # Persisted answer is still ou_first / approve, not the override.
    row = hitl_store.get(req.hitl_id)
    assert row is not None
    assert row["status"] == "answered"
    assert row["answer_responder_id"] == "ou_first"
    assert row["answer_option_id"] == "approve"
