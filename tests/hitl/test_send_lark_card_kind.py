"""v0.4.1 Stage L1.C — :func:`send_lark_card` ``kind`` parameter test.

Per the v0.4.1 task spec L1.D #3 (~ 1 case): assert that calling
``send_lark_card(..., kind="terminal")`` emits a log record matching
``lark.send.ok kind=terminal target=...`` so the daemon's terminal
notifier (Stage L2) can be distinguished from the v0.3.0 HITL channel
when grepping ``daemon.log``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.renderers.lark import LarkCardKind, send_lark_card


class _StubCompletedProcess:
    """Minimal :class:`subprocess.CompletedProcess` stand-in for the runner seam."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_prompt() -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[
            HITLOption(id="yes", label="Approve"),
            HITLOption(id="no", label="Block", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide", "cli"],
        deadline_seconds=3600,
        prompt_id="hitl-test-kind",
    )


def test_send_lark_card_kind_terminal_logs_origin_tag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``send_lark_card(..., kind="terminal")`` logs the ``kind=terminal`` tag.

    Per v0.4.1 Stage L1.C: the success log line includes
    ``lark.send.ok kind=<kind> target=<open_id> ...`` so terminal
    notifications (Stage L2) can be filtered out of HITL traffic when
    operators grep daemon logs.
    """
    fake_message_id = '{"message_id": "om_xxx_kind_test"}'
    runs: list[list[str]] = []

    def stub_runner(argv: list[str], **_kw: Any) -> _StubCompletedProcess:
        runs.append(argv)
        return _StubCompletedProcess(returncode=0, stdout=fake_message_id)

    target = "ou_terminal_test_target"
    expected_kind: LarkCardKind = "terminal"

    with caplog.at_level(logging.INFO, logger="popolaloom.hitl.renderers.lark"):
        result = send_lark_card(
            _make_prompt(),
            target_open_id=target,
            runner=stub_runner,
            kind=expected_kind,
        )

    assert result.ok is True
    assert result.message_id == "om_xxx_kind_test"
    assert len(runs) == 1

    log_messages = [rec.getMessage() for rec in caplog.records]
    pattern = re.compile(
        rf"lark\.send\.ok kind={expected_kind} target={re.escape(target)}"
    )
    assert any(pattern.search(msg) for msg in log_messages), (
        f"expected log line matching {pattern.pattern!r}; got: {log_messages}"
    )
