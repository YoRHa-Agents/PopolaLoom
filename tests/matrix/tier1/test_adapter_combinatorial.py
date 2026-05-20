"""Tier 1 / A3 — parametrized 3-adapter × extras × cwd combinatorial matrix.

Per the L3 brief: 3 adapters (cursor / claude / codex) × 5 extras-key
combinations × 3 cwd values gives a 45-cell matrix; pytest parametrize
collapses the matrix into ≥ 10 distinct CASES with deterministic
assertions: ``argv[0] == adapter.binary`` and the requested extra keys
are reflected in the argv list.

Each adapter has its own per-key contract (validated by the existing
``tests/test_adapters.py`` smoke tests); here we focus on:

- determinism: calling ``build_command`` twice with the same inputs
  yields identical output (PURE per CommandBuilder Protocol).
- ``argv[0]`` always equals ``adapter.binary``.
- the extra keys flow through to argv when applicable.
- the cwd kwarg never crashes the builder (cursor optionally embeds it
  via ``--cwd``; claude / codex don't use it but must accept it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from popolaloom.adapters import (
    ClaudeAdapter,
    CodexAdapter,
    CommandBuilder,
    CursorAdapter,
)

# ── adapter parametrization helpers ──────────────────────────────────────


def _adapters() -> list[CommandBuilder]:
    """Return one fresh instance per default adapter.

    Pure factory — using *fresh* instances avoids cross-test residue from
    a shared module-global registry."""
    return [CursorAdapter(), ClaudeAdapter(), CodexAdapter()]


_EXTRAS_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("empty", {}),
    ("output_format_text", {"output_format": "text"}),
    ("session_id_only", {"session_id": "sess-abcdef"}),
    ("max_turns_only", {"max_turns": 7}),
    ("sandbox_workspace_write", {"sandbox": "workspace-write"}),
]
_CWD_VARIANTS: list[tuple[str, Path | None]] = [
    ("cwd_none", None),
    ("cwd_simple", Path("/tmp/foo")),
    ("cwd_with_spaces", Path("/tmp/dir with spaces")),
]


# ── tests: parametrized matrix ───────────────────────────────────────────


@pytest.mark.parametrize("adapter", _adapters(), ids=lambda a: a.name)
@pytest.mark.parametrize(
    ("extras_id", "extras"),
    _EXTRAS_VARIANTS,
    ids=[v[0] for v in _EXTRAS_VARIANTS],
)
@pytest.mark.parametrize(
    ("cwd_id", "cwd"),
    _CWD_VARIANTS,
    ids=[v[0] for v in _CWD_VARIANTS],
)
def test_build_command_argv0_is_binary(
    adapter: CommandBuilder,
    extras_id: str,
    extras: dict[str, Any],
    cwd_id: str,
    cwd: Path | None,
) -> None:
    """For any (adapter, extras-variant, cwd) triple where the adapter accepts the
    extras, ``argv[0] == adapter.binary``.

    Skips combinations whose extras don't apply to the adapter:
    e.g. ``sandbox`` is codex-only; ``max_turns`` is claude-only;
    ``output_format`` is cursor-only.
    """
    if "sandbox" in extras and adapter.name != "codex":
        pytest.skip("sandbox is codex-only")
    if "max_turns" in extras and adapter.name != "claude":
        pytest.skip("max_turns is claude-only")
    if "output_format" in extras and adapter.name != "cursor":
        pytest.skip("output_format is cursor-only")

    argv = adapter.build_command("p", cwd=cwd, extra=extras)
    assert isinstance(argv, list)
    assert argv, "argv must be non-empty"
    assert argv[0] == adapter.binary


@pytest.mark.parametrize("adapter", _adapters(), ids=lambda a: a.name)
def test_build_command_is_deterministic(adapter: CommandBuilder) -> None:
    """Calling build_command twice with identical args yields identical argv (PURE)."""
    extra: dict[str, Any] | None = None
    if adapter.name == "cursor":
        extra = {"session_id": "X", "output_format": "text"}
    elif adapter.name == "claude":
        extra = {"session_id": "X", "max_turns": 3}
    elif adapter.name == "codex":
        extra = {"sandbox": "read-only"}

    a1 = adapter.build_command("hello", cwd=Path("/tmp/d"), extra=dict(extra) if extra else None)
    a2 = adapter.build_command("hello", cwd=Path("/tmp/d"), extra=dict(extra) if extra else None)
    assert a1 == a2


@pytest.mark.parametrize("adapter", _adapters(), ids=lambda a: a.name)
def test_build_command_with_unknown_extras_does_not_crash(adapter: CommandBuilder) -> None:
    """Unknown extras keys are ignored by all adapters — no exception, argv contains binary."""
    argv = adapter.build_command("p", extra={"completely_unknown_key": 42, "another": "ok"})
    assert argv[0] == adapter.binary


# ── extras-key reflection (each adapter's documented keys) ────────────────


def test_cursor_session_id_appears_in_argv() -> None:
    """CursorAdapter reflects ``session_id`` extra into ``--session-id <id>``."""
    argv = CursorAdapter().build_command("p", extra={"session_id": "chat-7"})
    assert "--session-id" in argv
    idx = argv.index("--session-id")
    assert argv[idx + 1] == "chat-7"


def test_cursor_output_format_stream_json_replaces_default_text() -> None:
    """``output_format=stream-json`` replaces the default ``text`` after ``--output-format``."""
    argv = CursorAdapter().build_command("p", extra={"output_format": "stream-json"})
    assert "stream-json" in argv
    assert "text" not in argv


def test_cursor_cwd_flag_adds_cwd_argument() -> None:
    """``cwd_flag=True`` + non-None cwd injects ``--cwd <cwd>``."""
    argv = CursorAdapter().build_command("p", cwd=Path("/x"), extra={"cwd_flag": True})
    assert "--cwd" in argv
    idx = argv.index("--cwd")
    assert argv[idx + 1] == "/x"


def test_cursor_cwd_flag_without_cwd_skips_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cwd_flag=True`` with cwd=None must NOT inject ``--cwd`` (logs warning, no crash).

    v1.6.1 (``feedback_for_v1.6.0.md`` Q-3): pin ``agent`` as ``argv[0]``
    via a hermetic ``shutil.which`` monkeypatch — the resolver in
    :func:`CursorAdapter._resolve_binary` returns whichever of
    ``("agent", "cursor-agent")`` is found first on PATH, with a
    fall-through to ``cls.binary == "agent"`` when neither resolves,
    so the assertion must not depend on the test machine's PATH.
    """
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
    argv = CursorAdapter().build_command("p", cwd=None, extra={"cwd_flag": True})
    assert "--cwd" not in argv
    assert argv[0] == "agent"


def test_claude_max_turns_appears_with_int_value() -> None:
    """ClaudeAdapter coerces ``max_turns`` int to str in argv."""
    argv = ClaudeAdapter().build_command("p", extra={"max_turns": 5})
    assert "--max-turns" in argv
    idx = argv.index("--max-turns")
    assert argv[idx + 1] == "5"


def test_codex_sandbox_appears_in_argv() -> None:
    """CodexAdapter appends ``--sandbox <mode>`` when sandbox extra is set."""
    argv = CodexAdapter().build_command("p", extra={"sandbox": "danger-full-access"})
    assert "--sandbox" in argv
    idx = argv.index("--sandbox")
    assert argv[idx + 1] == "danger-full-access"


# ── ValueError paths (invalid enum-like extras) ──────────────────────────


def test_cursor_invalid_output_format_raises_value_error() -> None:
    """Non-whitelisted output_format raises (No Silent Failures contract)."""
    with pytest.raises(ValueError, match="output_format"):
        CursorAdapter().build_command("p", extra={"output_format": "yaml"})


def test_codex_invalid_sandbox_raises_value_error() -> None:
    """Non-whitelisted sandbox value raises ValueError mentioning the key."""
    with pytest.raises(ValueError, match="sandbox"):
        CodexAdapter().build_command("p", extra={"sandbox": "not-a-mode"})


# ── prompt placement contract ─────────────────────────────────────────────


@pytest.mark.parametrize("adapter", _adapters(), ids=lambda a: a.name)
def test_prompt_appears_somewhere_in_argv(adapter: CommandBuilder) -> None:
    """The prompt text always lands somewhere in argv (positional or after ``-p``)."""
    argv = adapter.build_command("a unique prompt 7531")
    assert "a unique prompt 7531" in argv
