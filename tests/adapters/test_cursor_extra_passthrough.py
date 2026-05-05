"""Tests for the cursor adapter ``--cli-flag cli_args`` / ``cmd_args``
passthrough (v0.6.0 L6.B closure).

Closes the carry-over from the v0.5.{3,4,5} known-limitations chain that
SKILL.md v0.5.3 Workflow 4 documented as:

    > 注意 popolaloom 的 cursor adapter 当前**不**透传任意 cmd_args；需要
    > cursor-agent 自定义 flag 时走 popolaloom._vendored 二开或等 v0.6+
    > 的 --passthrough 项

v0.6.0 (Phase 2 step 1, deliverable L6.B per release-notes-v0.6.0.md)
implements the passthrough proper. The contract:

1. ``cli_args`` is the canonical key name (mirrors the user-facing
   ``popola dispatch --cli-flag cli_args=...`` syntax).
2. ``cmd_args`` is accepted as an alias for back-compat with the
   v0.5.3 SKILL.md Workflow 4 example (which used ``cmd_args`` in its
   shell-quoting tip).
3. Value may be either ``list[str]`` (preferred — explicit) or ``str``
   (split via :func:`shlex.split` so quoted compound tokens survive).
4. Tokens land between the ``--print --output-format <fmt>`` core flags
   and the ``<prompt>`` positional, so cursor-agent recognises them as
   flags rather than prompt content.
5. Type errors raise :class:`ValueError` with a key-pinned message
   (No Silent Failures workspace rule).

These tests pin the contract so any future refactor of
``CursorAdapter.build_command`` cannot silently break the
``--trust`` / ``--no-color`` / future-flag use case.

Default-lane: pure ``CursorAdapter().build_command(...)`` calls, no
subprocess, no daemon, no network. Each test < 5 ms.
"""

from __future__ import annotations

import pytest

from popolaloom.adapters import CursorAdapter

# ── happy paths ──────────────────────────────────────────────────────────


def test_cursor_cli_args_string_propagates_trust_flag() -> None:
    """The single most-requested case: ``cli_args="--trust"`` lands in argv.

    Mirrors what ``popola dispatch ... --cli=cursor --cli-flag
    cli_args=--trust`` produces after :func:`_parse_cli_flags` in
    ``cli/main.py`` JSON-decodes the value (``"--trust"`` is not valid
    JSON so it falls through as a raw string per R-012 contract).
    """
    argv = CursorAdapter().build_command("p", extra={"cli_args": "--trust"})
    assert "--trust" in argv


def test_cursor_cli_args_list_propagates_multiple_flags() -> None:
    """``cli_args=["--trust", "--no-color"]`` lands both tokens in order."""
    argv = CursorAdapter().build_command(
        "p", extra={"cli_args": ["--trust", "--no-color"]}
    )
    assert "--trust" in argv
    assert "--no-color" in argv
    trust_idx = argv.index("--trust")
    no_color_idx = argv.index("--no-color")
    assert trust_idx < no_color_idx, (
        "cli_args list order must be preserved in argv"
    )


def test_cursor_cmd_args_alias_works_for_skillmd_v053_compat() -> None:
    """``cmd_args`` alias accepted for v0.5.3 SKILL.md Workflow 4 example.

    The v0.5.3 SKILL.md Tip cited
    ``--cli-flag 'cmd_args="--foo --bar"'`` as a shell-quoting example;
    that key must keep working after the v0.6.0 closure.
    """
    argv = CursorAdapter().build_command("p", extra={"cmd_args": "--trust"})
    assert "--trust" in argv


def test_cursor_cli_args_string_with_spaces_splits_via_shlex() -> None:
    """Whitespace-separated string form splits into multiple tokens."""
    argv = CursorAdapter().build_command(
        "p", extra={"cli_args": "--foo bar baz"}
    )
    assert "--foo" in argv
    assert "bar" in argv
    assert "baz" in argv


def test_cursor_cli_args_quoted_compound_token_survives_shlex() -> None:
    """``cli_args='--name "alice bob"'`` keeps ``alice bob`` as ONE token."""
    argv = CursorAdapter().build_command(
        "p", extra={"cli_args": '--name "alice bob"'}
    )
    assert "--name" in argv
    assert "alice bob" in argv


# ── argv positioning contract ────────────────────────────────────────────


def test_cursor_cli_args_inserted_before_prompt_not_after() -> None:
    """``cli_args`` must land in the flag region, NOT after the prompt.

    Otherwise cursor-agent would read ``--trust`` as part of the prompt
    text rather than as a flag — the whole point of the passthrough is
    to put it where the CLI parser sees it as an option.
    """
    argv = CursorAdapter().build_command(
        "my-prompt", extra={"cli_args": "--trust"}
    )
    trust_idx = argv.index("--trust")
    prompt_idx = argv.index("my-prompt")
    assert trust_idx < prompt_idx, (
        f"--trust at index {trust_idx} must precede prompt at index {prompt_idx}"
    )


def test_cursor_cli_args_after_output_format_block() -> None:
    """``cli_args`` lands AFTER ``--output-format <fmt>`` so the core
    cursor-agent contract (``agent --print --output-format <fmt> ...``)
    stays at the head of argv.
    """
    argv = CursorAdapter().build_command("p", extra={"cli_args": "--trust"})
    fmt_idx = argv.index("--output-format")
    trust_idx = argv.index("--trust")
    assert trust_idx > fmt_idx + 1, (
        "cli_args must come after --output-format <fmt> pair"
    )


def test_cursor_cli_args_compose_with_session_id_and_cwd() -> None:
    """``cli_args`` plays well with the existing ``session_id`` + ``cwd_flag``
    extras — no positional clash, all three end up in argv."""
    from pathlib import Path

    argv = CursorAdapter().build_command(
        "p",
        cwd=Path("/tmp/x"),
        extra={
            "cli_args": ["--trust"],
            "session_id": "chat-9",
            "cwd_flag": True,
        },
    )
    assert "--trust" in argv
    assert "--cwd" in argv
    assert "/tmp/x" in argv
    assert "--session-id" in argv
    assert "chat-9" in argv


# ── No Silent Failures (validation) ──────────────────────────────────────


def test_cursor_cli_args_invalid_int_type_raises_value_error() -> None:
    """``cli_args=123`` must raise — refusing to silently coerce."""
    with pytest.raises(ValueError, match="cli_args"):
        CursorAdapter().build_command("p", extra={"cli_args": 123})


def test_cursor_cli_args_list_with_non_string_element_raises() -> None:
    """A list containing non-strings must raise (No Silent Failures)."""
    with pytest.raises(ValueError, match="cli_args list"):
        CursorAdapter().build_command(
            "p", extra={"cli_args": ["--ok", 5, "--also-ok"]}
        )


def test_cursor_cli_args_dict_type_raises_value_error() -> None:
    """``cli_args={...}`` (a dict) must raise."""
    with pytest.raises(ValueError, match="cli_args"):
        CursorAdapter().build_command("p", extra={"cli_args": {"foo": "bar"}})


# ── empty / no-op edges ──────────────────────────────────────────────────


def test_cursor_cli_args_empty_list_is_noop() -> None:
    """``cli_args=[]`` is a no-op — argv is identical to no-extras call."""
    argv_with = CursorAdapter().build_command("p", extra={"cli_args": []})
    argv_without = CursorAdapter().build_command("p")
    assert argv_with == argv_without


def test_cursor_cli_args_empty_string_is_noop() -> None:
    """``cli_args=""`` (shlex.split returns []) is a no-op."""
    argv_with = CursorAdapter().build_command("p", extra={"cli_args": ""})
    argv_without = CursorAdapter().build_command("p")
    assert argv_with == argv_without


def test_cursor_no_cli_args_key_preserves_legacy_argv_shape() -> None:
    """Without ``cli_args`` (or ``cmd_args``) the v0.5.x argv shape is unchanged.

    Pins the v0.5.x → v0.6.0 contract that the new feature is purely
    opt-in: callers that never set the new key see byte-identical argv
    to what v0.5.5 produced.
    """
    argv = CursorAdapter().build_command("p")
    assert argv == [
        "cursor-agent",
        "agent",
        "--print",
        "--output-format",
        "text",
        "p",
    ]


def test_cursor_cli_args_takes_precedence_over_cmd_args_alias() -> None:
    """When BOTH ``cli_args`` and ``cmd_args`` are set, ``cli_args`` wins.

    Pins the alias-resolution order: canonical name beats legacy alias
    so users incrementally migrating from ``cmd_args`` (v0.5.x SKILL.md
    docs) to ``cli_args`` (v0.6.0+ canonical) get unambiguous behaviour.
    """
    argv = CursorAdapter().build_command(
        "p",
        extra={
            "cli_args": "--from-canonical",
            "cmd_args": "--from-alias",
        },
    )
    assert "--from-canonical" in argv
    assert "--from-alias" not in argv
