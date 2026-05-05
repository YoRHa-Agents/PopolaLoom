"""Tier 2 — coverage boost for renderer dispatch paths (v0.3.0 F4).

Targets the lark renderer ``send_lark_card`` retry / failure paths and
the ide renderer ``dispatch_ide_notify`` platform branches via mock
runners (no subprocesses spawned).
"""

from __future__ import annotations

import subprocess
from typing import Any

from popolaloom.hitl import HITLOption, HITLPrompt
from popolaloom.hitl.renderers import ide
from popolaloom.hitl.renderers.lark import send_lark_card


def _approval_prompt(prompt_id: str = "hitl-cov-1") -> HITLPrompt:
    return HITLPrompt(
        trigger="approval",
        why="why",
        what="what",
        options=[
            HITLOption(id="yes", label="Yes"),
            HITLOption(id="no", label="No", default=True),
        ],
        default_option_id="no",
        channels=["lark", "ide"],
        deadline_seconds=3600,
        prompt_id=prompt_id,
    )


# ── send_lark_card paths ────────────────────────────────────────────────


def test_send_lark_card_no_target_returns_disabled() -> None:
    result = send_lark_card(
        _approval_prompt(),
        target_open_id=None,
        runner=lambda *args, **kw: None,
    )
    assert result.ok is False
    assert "LARK_HITL_TARGET_OPEN_ID" in (result.error or "")


def test_send_lark_card_filenotfound() -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("no lark-cli")

    result = send_lark_card(
        _approval_prompt(),
        target_open_id="ou_x",
        runner=runner,
        backoff_s=(0.0,),
    )
    assert result.ok is False
    assert "lark-cli not found" in (result.error or "")


def test_send_lark_card_timeout_then_succeed() -> None:
    attempts = 0

    class FakeResult:
        returncode = 0
        stdout = '{"message_id": "om_ok"}'
        stderr = ""

    def runner(argv: list[str], **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired(argv, 1)
        return FakeResult()

    result = send_lark_card(
        _approval_prompt(),
        target_open_id="ou_x",
        runner=runner,
        backoff_s=(0.001, 0.001, 0.001),
    )
    assert result.ok is True
    assert result.attempts == 2


def test_send_lark_card_unexpected_exception_retries() -> None:
    """A non-FileNotFoundError exception still retries up to max."""
    attempts = 0

    def runner(argv: list[str], **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise ValueError("boom")

    result = send_lark_card(
        _approval_prompt(),
        target_open_id="ou_x",
        runner=runner,
        backoff_s=(0.001, 0.001, 0.001),
    )
    assert result.ok is False
    assert attempts == 3


def test_send_lark_card_all_attempts_fail() -> None:
    class FakeResult:
        def __init__(self, rc: int) -> None:
            self.returncode = rc
            self.stdout = ""
            self.stderr = "transient"

    def runner(argv: list[str], **kwargs: Any) -> FakeResult:
        return FakeResult(rc=1)

    result = send_lark_card(
        _approval_prompt(),
        target_open_id="ou_x",
        runner=runner,
        backoff_s=(0.001, 0.001, 0.001),
    )
    assert result.ok is False
    assert result.attempts == 3


# ── ide dispatch paths ─────────────────────────────────────────────────


def test_ide_dispatch_returns_false_on_filenotfound(monkeypatch) -> None:
    def runner(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("notify-send missing")

    monkeypatch.setattr("shutil.which", lambda *a, **kw: "/usr/bin/notify-send")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    out = ide.dispatch_ide_notify(_approval_prompt(), runner=runner)
    assert out is False


def test_ide_dispatch_returns_false_on_timeout(monkeypatch) -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr("shutil.which", lambda *a, **kw: "/usr/bin/notify-send")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    out = ide.dispatch_ide_notify(_approval_prompt(), runner=runner)
    assert out is False


def test_ide_dispatch_returns_false_on_unexpected_exception(monkeypatch) -> None:
    def runner(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("subprocess crashed")

    monkeypatch.setattr("shutil.which", lambda *a, **kw: "/usr/bin/notify-send")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    out = ide.dispatch_ide_notify(_approval_prompt(), runner=runner)
    assert out is False


def test_ide_dispatch_returns_false_on_non_zero_rc(monkeypatch) -> None:
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "notify failed"

    monkeypatch.setattr("shutil.which", lambda *a, **kw: "/usr/bin/notify-send")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    out = ide.dispatch_ide_notify(_approval_prompt(), runner=lambda *a, **kw: FakeResult())
    assert out is False


def test_ide_dispatch_macos_uses_osascript(monkeypatch) -> None:
    """On Darwin, dispatch_ide_notify shells out to osascript."""
    monkeypatch.setattr("shutil.which", lambda *a, **kw: "/usr/bin/osascript")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    captured: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(argv: list[str], **kwargs: Any) -> FakeResult:
        captured.append(argv)
        return FakeResult()

    out = ide.dispatch_ide_notify(_approval_prompt(), runner=runner)
    assert out is True
    assert captured[0][0] == "osascript"


def test_ide_dispatch_unsupported_platform_returns_false(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **kw: None)
    monkeypatch.setattr("platform.system", lambda: "Plan9")
    out = ide.dispatch_ide_notify(_approval_prompt())
    assert out is False
