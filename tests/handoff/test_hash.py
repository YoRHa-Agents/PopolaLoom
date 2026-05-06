"""Unit tests for :mod:`popolaloom.handoff.hash`.

Coverage targets:

- :func:`slugify_prompt` corner cases (English / Chinese / emoji / empty /
  whitespace / oversized / multiline / numeric).
- :func:`content_hash` determinism, sensitivity, key-order independence,
  default=str fallback for non-JSON-native types.
- :func:`generate_handoff_id` regex shape, idempotency, prompt sensitivity,
  ``None`` ↔ ``{}`` collapsing for adapter_extra/constraints, and a 1k
  collision probe.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from popolaloom.handoff.hash import (
    content_hash,
    generate_handoff_id,
    slugify_prompt,
)

ID_REGEX = re.compile(r"^[a-z0-9-]+-[0-9a-f]{8}$")
"""Loose acceptance regex per task spec: cli + slug section + 8 hex tail."""

ID_STRICT_REGEX = re.compile(r"^[a-z0-9]+-[a-z0-9-]+-[0-9a-f]{8}$")
"""Tighter AC7 regex: cli (no hyphens) - slug (with hyphens) - 8 hex."""


# ──────────────────────── slugify_prompt ────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("fix the BUG in foo.py!", "fix-the-bug-in-foo-py"),
        ("UPPERCASE", "uppercase"),
        ("hello-world", "hello-world"),
        ("    leading spaces", "leading-spaces"),
        ("a__b__c", "a-b-c"),
        ("--many--dashes--", "many-dashes"),
        ("Spawn  ?  multi   spaces", "spawn-multi-spaces"),
    ],
)
def test_slugify_prompt_basic_english(raw: str, expected: str) -> None:
    assert slugify_prompt(raw) == expected


@pytest.mark.parametrize("raw", ["", "    ", "\n\n\n", "\t\t"])
def test_slugify_prompt_empty_or_whitespace_falls_back_to_task(raw: str) -> None:
    assert slugify_prompt(raw) == "task"


def test_slugify_prompt_non_ascii_only_falls_back_to_task() -> None:
    assert slugify_prompt("中文标题") == "task"
    assert slugify_prompt("🚀 🎉 ✨") == "task"


def test_slugify_prompt_takes_only_first_line() -> None:
    multiline = "first line title\nsecond line is ignored\nthird"
    assert slugify_prompt(multiline) == "first-line-title"


def test_slugify_prompt_truncates_to_max_chars() -> None:
    long_input = "a" * 80
    assert slugify_prompt(long_input, max_chars=30) == "a" * 30


def test_slugify_prompt_truncation_strips_trailing_dash() -> None:
    raw = "abcdefghijk----too-long-to-fit"
    out = slugify_prompt(raw, max_chars=11)
    assert out == "abcdefghijk"
    assert not out.endswith("-")


def test_slugify_prompt_max_chars_one_keeps_first_alnum() -> None:
    assert slugify_prompt("hello", max_chars=1) == "h"


def test_slugify_prompt_invalid_max_chars_raises() -> None:
    with pytest.raises(ValueError, match=r"max_chars must be >= 1"):
        slugify_prompt("anything", max_chars=0)
    with pytest.raises(ValueError):
        slugify_prompt("anything", max_chars=-5)


def test_slugify_prompt_numeric_kept() -> None:
    assert slugify_prompt("v0.7.1 release") == "v0-7-1-release"


# ──────────────────────── content_hash ────────────────────────


def test_content_hash_returns_8_hex_chars() -> None:
    h = content_hash({"a": 1, "b": "x"})
    assert isinstance(h, str)
    assert len(h) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", h) is not None


def test_content_hash_deterministic_100_runs() -> None:
    payload = {"target_cli": "cursor", "prompt": "fix the bug", "n": 42}
    expected = content_hash(payload)
    for _ in range(100):
        assert content_hash(payload) == expected


def test_content_hash_sensitive_to_prompt_change() -> None:
    base = {"prompt": "fix the bug"}
    tweaked = {"prompt": "fix the bug "}
    assert content_hash(base) != content_hash(tweaked)


def test_content_hash_independent_of_dict_insertion_order() -> None:
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "x": 1, "y": 2}
    c = {"y": 2, "z": 3, "x": 1}
    assert content_hash(a) == content_hash(b) == content_hash(c)


def test_content_hash_independent_of_nested_dict_order() -> None:
    a = {"adapter_extra": {"a": 1, "b": 2}}
    b = {"adapter_extra": {"b": 2, "a": 1}}
    assert content_hash(a) == content_hash(b)


def test_content_hash_handles_non_json_native_types_via_default_str() -> None:
    payload = {
        "when": dt.datetime(2026, 5, 6, 22, 0, tzinfo=dt.UTC),
        "where": Path("/tmp/x"),
    }
    h = content_hash(payload)
    assert re.fullmatch(r"[0-9a-f]{8}", h) is not None


def test_content_hash_unicode_preserved_not_escaped() -> None:
    a = {"reason": "中文"}
    b = {"reason": "\u4e2d\u6587"}
    assert content_hash(a) == content_hash(b)


# ──────────────────────── generate_handoff_id ────────────────────────


def test_generate_handoff_id_matches_loose_regex() -> None:
    hid = generate_handoff_id("cursor", "fix the bug in foo.py")
    assert ID_REGEX.fullmatch(hid) is not None, f"id={hid!r}"


def test_generate_handoff_id_matches_strict_regex_for_clean_cli() -> None:
    hid = generate_handoff_id("cursor", "fix the bug in foo.py")
    assert ID_STRICT_REGEX.fullmatch(hid) is not None, f"id={hid!r}"


def test_generate_handoff_id_starts_with_cli_slug() -> None:
    hid = generate_handoff_id("claude", "review pr #42")
    assert hid.startswith("claude-")


def test_generate_handoff_id_is_idempotent() -> None:
    hid1 = generate_handoff_id("cursor", "x", parent_task_id="p", adapter_extra={"k": 1})
    hid2 = generate_handoff_id("cursor", "x", parent_task_id="p", adapter_extra={"k": 1})
    assert hid1 == hid2


def test_generate_handoff_id_none_and_empty_dict_collapse_for_passthrough() -> None:
    hid_none = generate_handoff_id("cursor", "x")
    hid_empty = generate_handoff_id("cursor", "x", adapter_extra={}, constraints={})
    assert hid_none == hid_empty


def test_generate_handoff_id_changes_with_parent_task_id() -> None:
    base = generate_handoff_id("cursor", "x")
    with_parent = generate_handoff_id("cursor", "x", parent_task_id="parent-abc")
    assert base != with_parent


def test_generate_handoff_id_truncates_long_target_cli_slug() -> None:
    long_cli = "supercalifragilisticexpialidocious"
    hid = generate_handoff_id(long_cli, "x")
    cli_part = hid.rsplit("-", 2)[0]
    assert len(cli_part) <= 12


def test_generate_handoff_id_falls_back_to_task_for_emoji_only() -> None:
    hid = generate_handoff_id("cursor", "🚀🎉")
    assert "-task-" in hid


def test_generate_handoff_id_1000_unique_prompts_no_collision() -> None:
    """Birthday-bound probe: 1k unique prompts → expected ~0 collisions
    in 32-bit hash space."""
    seen: set[str] = set()
    for i in range(1000):
        hid = generate_handoff_id("cursor", f"task number {i:04d} variant text")
        seen.add(hid)
    assert len(seen) == 1000, f"collision detected: only {len(seen)} unique"
