"""Tier 1 / A5 — registry + ``build_command`` facade + ``is_available`` gating tests.

Per the L3 brief:

- ``build_command(cli, prompt, cwd, extra)`` equals
  ``<Adapter>().build_command(prompt, cwd, extra)`` for the 3 defaults.
- 2 unknown adapter names raise :class:`KeyError` whose message lists
  the available registered names.
- ``register_adapter`` then ``get_adapter`` then ``list_registered``
  works end-to-end (with the isolated_adapter_registry fixture).
- ``is_available`` returns False for every default adapter when
  :func:`shutil.which` is mocked to None.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from popolaloom.adapters import (
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    build_command,
    get_adapter,
    list_registered,
    register_adapter,
)

# ── facade-vs-class equivalence ──────────────────────────────────────────


def test_build_command_facade_matches_cursor_class() -> None:
    """build_command('cursor', ...) equals CursorAdapter().build_command(...)."""
    direct = CursorAdapter().build_command("p", cwd=None, extra={"output_format": "text"})
    via_facade = build_command("cursor", "p", cwd=None, extra={"output_format": "text"})
    assert via_facade == direct


def test_build_command_facade_matches_claude_class() -> None:
    direct = ClaudeAdapter().build_command("hi", extra={"max_turns": 3})
    via_facade = build_command("claude", "hi", extra={"max_turns": 3})
    assert via_facade == direct


def test_build_command_facade_matches_codex_class() -> None:
    direct = CodexAdapter().build_command("p", extra={"sandbox": "read-only"})
    via_facade = build_command("codex", "p", extra={"sandbox": "read-only"})
    assert via_facade == direct


# ── unknown cli raises KeyError listing options ──────────────────────────


def test_unknown_cli_raises_keyerror_with_available_names() -> None:
    """First unknown name path: error mentions available adapter names."""
    with pytest.raises(KeyError) as excinfo:
        build_command("vim-mode-cli", "p")
    msg = str(excinfo.value)
    assert "vim-mode-cli" in msg
    for default_name in ("cursor", "claude", "codex"):
        assert default_name in msg


def test_second_unknown_cli_also_raises_keyerror() -> None:
    """Second unknown name path: same contract holds for any unknown."""
    with pytest.raises(KeyError) as excinfo:
        get_adapter("totally-unknown-cli-name")
    msg = str(excinfo.value)
    assert "totally-unknown-cli-name" in msg
    assert "cursor" in msg


# ── register_adapter + get_adapter + list_registered round-trip ──────────


class _FakeAdapter:
    """Minimal CommandBuilder Protocol implementation for registry tests."""

    name: str = "fake_round_trip"
    binary: str = "fake-bin-not-on-path"

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        return [self.binary, "fake-arg", prompt]

    def is_available(self) -> bool:
        return False


def test_register_then_get_then_list(isolated_adapter_registry: None) -> None:
    """register → get → list round-trip lets the new adapter be addressable."""
    adapter = _FakeAdapter()
    register_adapter(adapter)
    fetched = get_adapter("fake_round_trip")
    assert fetched is adapter
    names = list_registered()
    assert "fake_round_trip" in names
    assert sorted(names) == names, "list_registered must be sorted"


def test_register_duplicate_name_raises(isolated_adapter_registry: None) -> None:
    """Re-registering an existing name raises ValueError mentioning both adapter classes."""
    register_adapter(_FakeAdapter())
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(_FakeAdapter())


# ── is_available shutil.which gating ─────────────────────────────────────


def test_is_available_false_when_shutil_which_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``shutil.which`` returns None, all 3 default adapters report unavailable."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert CursorAdapter().is_available() is False
    assert ClaudeAdapter().is_available() is False
    assert CodexAdapter().is_available() is False


def test_is_available_true_when_shutil_which_returns_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``shutil.which`` returns a non-None path, the adapter is available."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert CursorAdapter().is_available() is True
    assert ClaudeAdapter().is_available() is True
    assert CodexAdapter().is_available() is True


# ── facade KeyError vs default registry stability ────────────────────────


def test_default_registry_contains_expected_three() -> None:
    """The default registry (after import) always has cursor/claude/codex."""
    names = set(list_registered())
    assert {"cursor", "claude", "codex"}.issubset(names), (
        f"missing default adapters; got {sorted(names)}"
    )
