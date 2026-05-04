"""F2 primitive tests — relay / supervise / federate (v0.3.0 Stage F2).

Per v0.3.0-plan.md §4 Stage F2 + the L3 task spec acceptance criteria
(≥9 cases, 3 per primitive).

Test coverage:

- relay: dispatch chain, parent_task_id link, handoff envelope schema
- supervise: register + on_complete callback, unsubscribe idempotency,
  multiple parents on same child
- federate: 3-CLI dispatch, majority vote computation, unanimous strict mode
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from popolaloom.daemon import Popolad
from popolaloom.daemon.primitives import (
    FederateConfig,
    FederateResult,
    RelayHandoffEnvelope,
    SuperviseRegistry,
    federate,
    relay,
    tally_votes,
)
from popolaloom.daemon.primitives.federate import VoteOutcome

# ── helpers / fixtures ───────────────────────────────────────────────────


def _echo_adapter(
    cli: str, prompt: str, cwd: Any = None, extra: Any = None
) -> list[str]:
    """Tiny adapter that prints the prompt and exits 0."""
    import sys

    return [
        sys.executable,
        "-c",
        f"print({prompt!r}); import sys; sys.exit(0)",
    ]


@pytest.fixture
def popolad(tmp_path: Path) -> Popolad:
    return Popolad(events_dir=tmp_path / "events", adapter=_echo_adapter, use_graph=False)


# ── F2.2 — relay ─────────────────────────────────────────────────────────


def test_relay_dispatches_child_with_parent_task_id_link(popolad: Popolad) -> None:
    """relay() spawns a child task linked to the source via parent_task_id."""
    parent_task_id = popolad.dispatch_task(cli="cursor", prompt="parent step")

    child_task_id = relay(
        popolad,
        source_task_id=parent_task_id,
        target_cli="claude",
        payload={"diff": "abc"},
        reason="code review handoff",
    )

    assert child_task_id != parent_task_id
    child_handle = popolad.state_store.get(child_task_id)
    assert child_handle is not None
    assert child_handle.cli == "claude"


def test_relay_handoff_envelope_validates_with_extra_forbid() -> None:
    """RelayHandoffEnvelope is strict on unknown fields (No Silent Failures)."""
    valid = RelayHandoffEnvelope(
        source_cli="cursor",
        target_cli="claude",
        source_task_id="cursor-abc",
        payload={"x": 1},
        reason="r",
        constraints={"timeout": 1800},
    )
    assert valid.target_cli == "claude"

    with pytest.raises(ValidationError):
        RelayHandoffEnvelope(
            source_cli="cursor",
            target_cli="claude",
            source_task_id="cursor-abc",
            payload={},
            reason="r",
            unknown_field="x",
        )


def test_relay_unknown_source_raises_value_error(popolad: Popolad) -> None:
    """relay() with non-existent source_task_id raises ValueError clearly."""
    with pytest.raises(ValueError, match="not found in popolad state"):
        relay(
            popolad,
            source_task_id="bogus-task-id",
            target_cli="claude",
            payload={},
            reason="should fail",
        )


def test_relay_handoff_envelope_fixture_loads() -> None:
    """Fixture file matches RelayHandoffEnvelope schema (round-trips OK)."""
    fixture_path = Path(__file__).parent / "fixtures" / "handoff_envelope.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    envelope = RelayHandoffEnvelope.model_validate(data)
    assert envelope.source_cli == "cursor"
    assert envelope.target_cli == "claude"
    assert envelope.payload["next_step"] == "review"
    assert envelope.constraints["timeout"] == 1800


# ── F2.3 — supervise ─────────────────────────────────────────────────────


def test_supervise_registers_and_fires_on_complete() -> None:
    """on_complete callback fires when child terminates with completed."""
    reg = SuperviseRegistry()
    fired: list[tuple[str, str, dict[str, Any]]] = []

    def on_complete(parent: str, child: str, payload: dict[str, Any]) -> None:
        fired.append((parent, child, payload))

    handle = reg.subscribe(
        parent_task_id="parent-1",
        child_task_id="child-1",
        on_complete=on_complete,
    )
    assert reg.has_subscription(handle.subscription_id)

    fired_count = reg.on_child_terminal("child-1", "completed", {"exit_code": 0})
    assert fired_count == 1
    assert len(fired) == 1
    assert fired[0][0] == "parent-1"
    assert fired[0][1] == "child-1"
    assert fired[0][2]["outcome"] == "completed"


def test_supervise_unsubscribe_removes_handler_idempotently() -> None:
    """Calling unsubscribe twice is safe; first removes, second is no-op."""
    reg = SuperviseRegistry()
    handle = reg.subscribe(
        parent_task_id="parent-2",
        child_task_id="child-2",
        on_complete=lambda *args, **kw: None,
    )
    assert handle.unsubscribe() is True
    assert reg.has_subscription(handle.subscription_id) is False
    assert handle.unsubscribe() is False


def test_supervise_multiple_parents_on_same_child_all_fire() -> None:
    """Two parent subscriptions on the same child both receive notifications."""
    reg = SuperviseRegistry()
    fired_a: list[tuple[str, str]] = []
    fired_b: list[tuple[str, str]] = []

    reg.subscribe(
        parent_task_id="parent-A",
        child_task_id="child-shared",
        on_complete=lambda p, c, _: fired_a.append((p, c)),
    )
    reg.subscribe(
        parent_task_id="parent-B",
        child_task_id="child-shared",
        on_complete=lambda p, c, _: fired_b.append((p, c)),
    )

    fired_count = reg.on_child_terminal("child-shared", "completed")
    assert fired_count == 2
    assert fired_a == [("parent-A", "child-shared")]
    assert fired_b == [("parent-B", "child-shared")]


def test_supervise_on_fail_callback_fires_for_failed_outcome() -> None:
    """on_fail (not on_complete) fires for failed/canceled outcomes."""
    reg = SuperviseRegistry()
    failed: list[str] = []
    completed: list[str] = []
    reg.subscribe(
        parent_task_id="p",
        child_task_id="c",
        on_complete=lambda *a, **kw: completed.append("done"),
        on_fail=lambda *a, **kw: failed.append("fail"),
    )
    reg.on_child_terminal("c", "failed", {"exit_code": 1})
    assert failed == ["fail"]
    assert completed == []

    canceled = SuperviseRegistry()
    fail_only: list[str] = []
    canceled.subscribe(
        parent_task_id="p2",
        child_task_id="c2",
        on_fail=lambda *a, **kw: fail_only.append("cancel"),
    )
    canceled.on_child_terminal("c2", "canceled")
    assert fail_only == ["cancel"]


# ── F2.4 — federate ──────────────────────────────────────────────────────


def test_federate_dispatches_three_children_for_three_clis(popolad: Popolad) -> None:
    """federate() with 3 CLIs spawns 3 child tasks tagged with federate_id."""
    result = federate(
        popolad,
        prompt="research storage backends",
        cli_list=["cursor", "claude", "codex"],
    )
    assert isinstance(result, FederateResult)
    assert len(result.child_task_ids) == 3
    assert result.voting_strategy == "majority"
    assert result.federate_id

    for child_id in result.child_task_ids:
        handle = popolad.state_store.get(child_id)
        assert handle is not None


def test_federate_majority_vote_picks_most_common_output() -> None:
    """tally_votes(majority) picks the bucket with > 50% votes."""
    outputs = {
        "cursor-1": "answer A",
        "claude-1": "answer A",
        "codex-1": "answer B",
    }
    outcome = tally_votes(outputs, voting_strategy="majority")
    assert isinstance(outcome, VoteOutcome)
    assert outcome.winning_output == "answer A"
    assert outcome.passed is True
    assert outcome.total == 3


def test_federate_unanimous_strict_mode_requires_full_agreement() -> None:
    """unanimous voting fails when any output disagrees."""
    same_outputs = {
        "cursor-1": "agreed",
        "claude-1": "agreed",
        "codex-1": "agreed",
    }
    outcome_pass = tally_votes(same_outputs, voting_strategy="unanimous")
    assert outcome_pass.passed is True
    assert outcome_pass.winning_output == "agreed"

    mixed_outputs = {
        "cursor-1": "answer A",
        "claude-1": "answer A",
        "codex-1": "different",
    }
    outcome_fail = tally_votes(mixed_outputs, voting_strategy="unanimous")
    assert outcome_fail.passed is False


def test_federate_config_rejects_fewer_than_three_clis() -> None:
    """FederateConfig requires ≥ 3 CLIs (per task spec)."""
    with pytest.raises(ValidationError, match="≥ 3"):
        FederateConfig(cli_list=["cursor", "claude"], prompt="too few")


def test_federate_first_to_finish_picks_first_nonempty() -> None:
    """first_to_finish strategy returns the first non-empty entry."""
    outputs = {
        "claude-1": "",
        "cursor-1": "first real answer",
        "codex-1": "later",
    }
    outcome = tally_votes(outputs, voting_strategy="first_to_finish")
    assert outcome.passed is True
    assert outcome.winning_output == "first real answer"


def test_supervise_subscribe_blank_ids_raises() -> None:
    """Blank parent or child id raises ValueError (No Silent Failures)."""
    reg = SuperviseRegistry()
    with pytest.raises(ValueError):
        reg.subscribe("", "child", on_complete=lambda *_a: None)
    with pytest.raises(ValueError):
        reg.subscribe("parent", "", on_complete=lambda *_a: None)


def test_supervise_subscribe_without_callbacks_succeeds() -> None:
    """v0.3.0 allows subscribe() without callbacks (RPC use-case)."""
    reg = SuperviseRegistry()
    handle = reg.subscribe("p", "c")
    assert handle.subscription_id
    fired = reg.on_child_terminal("c", "completed")
    assert fired == 0, "no callbacks → no firings"


def test_supervise_on_child_terminal_invalid_outcome_raises() -> None:
    """outcome must be one of completed/failed/canceled."""
    reg = SuperviseRegistry()
    with pytest.raises(ValueError, match="completed/failed/canceled"):
        reg.on_child_terminal("any-child", outcome="weird")


def test_supervise_callback_exception_is_logged_then_reraised() -> None:
    """First raising callback's exception bubbles after all peers fire."""
    reg = SuperviseRegistry()
    fired: list[str] = []
    reg.subscribe(
        "parent-1",
        "child-1",
        on_complete=lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    reg.subscribe(
        "parent-2",
        "child-1",
        on_complete=lambda *_: fired.append("ok"),
    )
    with pytest.raises(RuntimeError, match="boom"):
        reg.on_child_terminal("child-1", "completed")
    assert fired == ["ok"], "peer callback fired despite earlier exception"


def test_supervise_list_subscriptions_returns_handles() -> None:
    """list_subscriptions returns SubscriptionHandle snapshots."""
    reg = SuperviseRegistry()
    h1 = reg.subscribe("p1", "c1", on_complete=lambda *_a: None)
    h2 = reg.subscribe("p2", "c2", on_fail=lambda *_a: None)
    handles = reg.list_subscriptions()
    assert len(handles) == 2
    sub_ids = {h.subscription_id for h in handles}
    assert h1.subscription_id in sub_ids
    assert h2.subscription_id in sub_ids


def test_supervise_unsubscribe_via_handle_calls_registry() -> None:
    """SubscriptionHandle.unsubscribe() proxies to the registry."""
    reg = SuperviseRegistry()
    handle = reg.subscribe("p", "c", on_complete=lambda *_a: None)
    assert reg.has_subscription(handle.subscription_id) is True
    handle.unsubscribe()
    assert reg.has_subscription(handle.subscription_id) is False
