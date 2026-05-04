"""Tier 3 — Lark out send retry tests (v0.3.0 F4.D §12.8.1).

Per spec §12.8.4 + roadmap RV3-1 mitigation + v0.3.0-plan §4 Stage F4.3.

Verifies the 3-attempt exponential backoff (1s/3s/9s) in
:func:`popolaloom.hitl.renderers.lark.send_lark_card`.

≥ 1 case as required by AC #3 of the v0.3.0 task spec.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.renderers.lark import send_lark_card

## v0.3.0 F4.D: subprocess.run is mocked end-to-end so this test runs
## fast enough for the default lane — no real lark-cli invocation.


def _sample_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="x",
        what="y",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No"),
        ],
        default_option_id="no",
        channels=["lark", "ide"],
        deadline_seconds=3600,
    )


def test_send_lark_card_disabled_when_target_unset() -> None:
    """No target_open_id → returns ok=False with disabled message; no subprocess."""
    prompt = _sample_prompt()
    result = send_lark_card(
        prompt,
        target_open_id=None,
        runner=MagicMock(side_effect=AssertionError("should not call runner")),
    )
    assert result.ok is False
    assert "LARK_HITL_TARGET_OPEN_ID" in (result.error or "")


def test_send_lark_card_first_attempt_success() -> None:
    """First attempt returncode=0 → no retry; ok=True."""
    prompt = _sample_prompt()
    fake_proc = MagicMock(returncode=0, stdout='{"message_id": "om_1"}', stderr="")
    runner = MagicMock(return_value=fake_proc)
    result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
        backoff_s=(0.0, 0.0, 0.0),  # zero backoff for test speed
    )
    assert result.ok is True
    assert result.attempts == 1
    assert result.message_id == "om_1"
    assert runner.call_count == 1


def test_send_lark_card_retries_on_failure() -> None:
    """First two attempts return non-zero → retries up to 3 times."""
    prompt = _sample_prompt()
    fail = MagicMock(returncode=1, stdout="", stderr="rate limited")
    success = MagicMock(returncode=0, stdout='{"message_id": "om_x"}', stderr="")
    runner = MagicMock(side_effect=[fail, fail, success])
    result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
        backoff_s=(0.0, 0.0, 0.0),  # zero backoff
    )
    assert result.ok is True
    assert result.attempts == 3
    assert runner.call_count == 3


def test_send_lark_card_all_attempts_fail() -> None:
    """3 attempts all fail → ok=False with attempts=3."""
    prompt = _sample_prompt()
    fail = MagicMock(returncode=1, stdout="", stderr="boom")
    runner = MagicMock(return_value=fail)
    result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
        backoff_s=(0.0, 0.0, 0.0),  # zero backoff
    )
    assert result.ok is False
    assert result.attempts == 3
    assert runner.call_count == 3
    assert result.error is not None


def test_send_lark_card_handles_lark_cli_missing() -> None:
    """FileNotFoundError → returns immediately (no retry; renderer disabled)."""
    prompt = _sample_prompt()
    runner = MagicMock(side_effect=FileNotFoundError("lark-cli not found"))
    result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
    )
    assert result.ok is False
    assert "lark-cli" in (result.error or "")
    assert runner.call_count == 1


def test_send_lark_card_handles_subprocess_timeout() -> None:
    """subprocess.TimeoutExpired counted as a failed attempt; retry."""
    prompt = _sample_prompt()
    timeout = subprocess.TimeoutExpired(cmd=["lark-cli"], timeout=1.0)
    success = MagicMock(returncode=0, stdout='{"message_id": "om_z"}', stderr="")
    runner = MagicMock(side_effect=[timeout, success])
    result = send_lark_card(
        prompt,
        target_open_id="ou_test",
        runner=runner,
        backoff_s=(0.0, 0.0, 0.0),
    )
    assert result.ok is True
    assert result.attempts == 2
