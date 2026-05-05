"""Tier 5 — Lark real e2e smoke (v0.3.0 F4.D).

Per AC #3 of the v0.3.0 task spec: tests marked ``real_lark`` MUST
default-skip when ``LARK_HITL_TARGET_OPEN_ID`` is unset (no real bot
credentials in CI).

This file ships ≥ 1 case; richer real_lark suite (3+) lands when
the bot env is provisioned (post-v0.3.0 release).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.lark.card_templates import build_card_send_argv


@pytest.mark.real_lark
@pytest.mark.slow
@pytest.mark.skipif(
    not os.getenv("LARK_HITL_TARGET_OPEN_ID"),
    reason="real Lark bot credentials not configured (LARK_HITL_TARGET_OPEN_ID)",
)
def test_real_send_card_to_target_open_id() -> None:
    """Send a real card to the configured Lark target open_id."""
    if shutil.which("lark-cli") is None:
        pytest.skip("lark-cli not on PATH")

    prompt = HITLPrompt(
        trigger="info_request",
        why="v0.3.0 F4 real_lark smoke test",
        what="Acknowledge by clicking any option (auto-defaults to A in 30s).",
        options=[
            HITLOption(id="ack_a", label="A"),
            HITLOption(id="ack_b", label="B"),
        ],
        default_option_id="ack_a",
        channels=["lark", "ide"],
        deadline_seconds=30,
        prompt_id="hitl-real-1",
    )
    target = os.environ["LARK_HITL_TARGET_OPEN_ID"]
    argv = build_card_send_argv(prompt, target)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"lark-cli stderr: {result.stderr}"
    # The CLI prints a JSON line containing message_id when successful.
    found_message_id = False
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("message_id"):
            found_message_id = True
            break
    assert found_message_id, f"no message_id in stdout: {result.stdout}"
