"""Stage Impl-2 tests for ``popolaloom.adapters`` (Day-1 命令构造层).

Coverage targets (≥ 7 tests, all binary-independent — none of
``cursor-agent`` / ``claude`` / ``codex`` need to be installed for the suite
to pass):

- 3 default adapters auto-registered on import.
- ``get_adapter`` raises a helpful KeyError listing available names.
- per-CLI ``build_command`` basic + ``extra``-key variants
  (``cursor`` / ``claude`` / ``codex``).
- duplicate ``register_adapter`` raises ValueError.
- ``build_command`` facade equals direct ``Adapter().build_command(...)``.
- ``is_available`` returns False when ``shutil.which`` returns None
  (monkeypatched).
- All 3 adapters satisfy the runtime-checkable :class:`Adapter` Protocol.

每个测试 < 50ms, 全套 < 1s.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from popolaloom.adapters import (
    Adapter,
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    build_command,
    get_adapter,
    list_registered,
    register_adapter,
)
from popolaloom.adapters import base as adapter_base


@pytest.fixture
def isolated_registry() -> Any:
    """Snapshot + restore global ``_REGISTRY`` around test (避免污染下一个测试)."""
    saved = dict(adapter_base._REGISTRY)
    yield
    adapter_base._REGISTRY.clear()
    adapter_base._REGISTRY.update(saved)


# ── Registry primitives ──────────────────────────────────────────────────


def test_registry_has_three_defaults() -> None:
    """``import popolaloom.adapters`` 触发自动注册 cursor/claude/codex."""
    names = set(list_registered())
    assert {"cursor", "claude", "codex"}.issubset(names), (
        f"expected cursor/claude/codex in registry, got {sorted(names)}"
    )


def test_get_adapter_unknown_lists_options() -> None:
    """``get_adapter('vim')`` 报错信息含 cursor/claude/codex 三个名字 + 失败键名."""
    with pytest.raises(KeyError) as excinfo:
        get_adapter("vim")
    msg = str(excinfo.value)
    assert "cursor" in msg
    assert "claude" in msg
    assert "codex" in msg
    assert "vim" in msg


def test_register_duplicate_raises(isolated_registry: None) -> None:
    """同名 adapter 重复注册必须 raise ValueError (No Silent Failures)."""

    class FakeAdapter:
        name = "fake_dup"
        binary = "no-such-binary"

        def build_command(
            self,
            prompt: str,
            cwd: Path | None = None,
            extra: dict[str, Any] | None = None,
        ) -> list[str]:
            return [self.binary, prompt]

        def is_available(self) -> bool:
            return False

    fake = FakeAdapter()
    register_adapter(fake)
    assert "fake_dup" in list_registered()
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(fake)


# ── CursorAdapter ────────────────────────────────────────────────────────


def test_cursor_build_command_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """``agent agent --print --output-format text "<prompt>"`` 字段断言.

    v1.6.1 (``feedback_for_v1.6.0.md`` Q-3): the canonical Cursor CLI
    binary is now ``agent``; ``cursor-agent`` is kept as a legacy
    alias that the adapter still resolves when the modern binary is
    absent. Force the test to assert against the canonical name by
    pretending ``agent`` resolves on PATH so
    :func:`CursorAdapter._resolve_binary` returns ``"agent"``
    deterministically (hermetic — the test runner may or may not
    have either binary installed).
    """
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    adapter = CursorAdapter()
    cmd = adapter.build_command("say hi")
    assert cmd[0] == "agent"
    assert "agent" in cmd
    assert "--print" in cmd
    assert "--output-format" in cmd
    assert "text" in cmd
    assert "say hi" in cmd

    cmd2 = adapter.build_command("say hi", extra={"output_format": "stream-json"})
    assert "stream-json" in cmd2
    assert "text" not in cmd2

    cmd3 = adapter.build_command(
        "say hi", extra={"session_id": "chat-7", "output_format": "text"}
    )
    assert "--session-id" in cmd3
    assert "chat-7" in cmd3
    sid_idx = cmd3.index("--session-id")
    assert cmd3[sid_idx + 1] == "chat-7"

    cmd4 = adapter.build_command("say hi", cwd=Path("/tmp/x"), extra={"cwd_flag": True})
    assert "--cwd" in cmd4
    assert "/tmp/x" in cmd4
    cwd_idx = cmd4.index("--cwd")
    assert cmd4[cwd_idx + 1] == "/tmp/x"


def test_cursor_invalid_output_format_raises() -> None:
    """非法 ``output_format`` 必须 raise ValueError (No Silent Failures)."""
    adapter = CursorAdapter()
    with pytest.raises(ValueError, match="output_format"):
        adapter.build_command("hi", extra={"output_format": "yaml"})


def test_cursor_adapter_resolves_agent_first_then_cursor_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.6.1 — when both binaries exist, ``agent`` wins (canonical name).

    Mirrors the worker-side resolver in
    :func:`popolaloom.cli.cloud_worker_cmd._resolve_agent_binary` so the
    local subprocess adapter and the worker wrapper agree on the
    canonical Cursor CLI name (per ``feedback_for_v1.6.0.md`` Q-3 —
    modern installs ship ``agent`` as the canonical binary with
    ``cursor-agent`` kept as a legacy symlink).
    """
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/local/bin/{name}"
    )
    assert CursorAdapter._resolve_binary() == "agent"


def test_cursor_adapter_resolves_cursor_agent_when_only_legacy_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.6.1 — legacy installs that only ship ``cursor-agent`` still work.

    The resolver tries ``agent`` first, then falls through to
    ``cursor-agent`` so operators on a stale Cursor install (pre-
    2026.05.07) don't need to upgrade just to use PopolaLoom.
    """

    def which_legacy_only(name: str) -> str | None:
        if name == "agent":
            return None
        if name == "cursor-agent":
            return "/usr/bin/cursor-agent"
        return None

    monkeypatch.setattr(shutil, "which", which_legacy_only)
    assert CursorAdapter._resolve_binary() == "cursor-agent"


def test_cursor_adapter_is_available_with_either_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.6.1 — ``is_available()`` is True iff ANY known binary is on PATH.

    Covers the cross-product of (agent present, cursor-agent present)
    so a legacy-only install, a modern-only install, both-present, and
    neither-present each route to the right boolean.
    """

    def which_only_legacy(name: str) -> str | None:
        return "/usr/bin/cursor-agent" if name == "cursor-agent" else None

    monkeypatch.setattr(shutil, "which", which_only_legacy)
    assert CursorAdapter().is_available() is True

    def which_only_modern(name: str) -> str | None:
        return "/usr/local/bin/agent" if name == "agent" else None

    monkeypatch.setattr(shutil, "which", which_only_modern)
    assert CursorAdapter().is_available() is True

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert CursorAdapter().is_available() is False


# ── ClaudeAdapter ────────────────────────────────────────────────────────


def test_claude_build_command_basic() -> None:
    """``claude -p "<prompt>" --output-format stream-json --verbose`` 字段断言."""
    adapter = ClaudeAdapter()
    cmd = adapter.build_command("hi there")
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "hi there" in cmd
    assert "stream-json" in cmd
    assert "--verbose" in cmd
    p_idx = cmd.index("-p")
    assert cmd[p_idx + 1] == "hi there"

    cmd2 = adapter.build_command("hi", extra={"session_id": "abc-123"})
    assert "--session-id" in cmd2
    assert "abc-123" in cmd2

    cmd3 = adapter.build_command("hi", extra={"max_turns": 5})
    assert "--max-turns" in cmd3
    assert "5" in cmd3


# ── CodexAdapter ─────────────────────────────────────────────────────────


def test_codex_build_command_basic() -> None:
    """``codex exec <prompt>`` 基础 + ``--sandbox`` 变种."""
    adapter = CodexAdapter()
    cmd = adapter.build_command("noop")
    assert cmd == ["codex", "exec", "noop"]

    cmd2 = adapter.build_command("noop", extra={"sandbox": "workspace-write"})
    assert "--sandbox" in cmd2
    assert "workspace-write" in cmd2

    with pytest.raises(ValueError, match="sandbox"):
        adapter.build_command("noop", extra={"sandbox": "yolo"})


# ── facade build_command + is_available + Protocol ────────────────────────


def test_build_command_facade() -> None:
    """``build_command('cursor', ...)`` 等价于 ``CursorAdapter().build_command(...)``."""
    assert build_command("cursor", "test") == CursorAdapter().build_command("test")
    assert build_command("claude", "test") == ClaudeAdapter().build_command("test")
    assert build_command("codex", "test") == CodexAdapter().build_command("test")

    with pytest.raises(KeyError):
        build_command("does-not-exist", "test")


def test_is_available_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 cursor-agent / claude / codex 时 ``is_available`` 必须返回 False."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert CursorAdapter().is_available() is False
    assert ClaudeAdapter().is_available() is False
    assert CodexAdapter().is_available() is False


def test_protocol_runtime_satisfaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 个 adapter 均满足 ``runtime_checkable`` :class:`Adapter` Protocol.

    v1.6.1: :class:`CursorAdapter` consults :func:`shutil.which` lazily
    via :meth:`CursorAdapter._resolve_binary` during ``build_command``,
    so we pin the resolver to the canonical ``agent`` name by patching
    PATH lookups to None (resolver falls back to ``cls.binary``). This
    keeps the Protocol invariant ``cmd[0] == adapter.binary`` hermetic
    regardless of which Cursor CLI variant the test runner happens to
    have installed.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    for adapter in (CursorAdapter(), ClaudeAdapter(), CodexAdapter()):
        assert isinstance(adapter, Adapter)
        assert isinstance(adapter.name, str) and adapter.name
        assert isinstance(adapter.binary, str) and adapter.binary
        cmd = adapter.build_command("p")
        assert isinstance(cmd, list)
        assert all(isinstance(x, str) for x in cmd)
        assert cmd[0] == adapter.binary
