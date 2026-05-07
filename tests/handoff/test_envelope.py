"""Unit tests for :mod:`popolaloom.handoff.envelope`.

Coverage targets:

- Pydantic validation: required fields, ``min_length=1`` rejections,
  ``extra="forbid"`` (No Silent Failures invariant).
- ``to_markdown`` invariants: opening fence, body separation,
  ``created_at`` ISO-8601 stringification, ``None`` preservation.
- ``from_markdown`` round-trip equivalence over 3+ shapes (minimal /
  fully populated / special-character prompt with embedded ``---``).
- ``from_markdown`` error paths: missing fence / YAML error / non-mapping
  YAML / empty body / schema validation failure (all ``ValueError``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError

from popolaloom.handoff.envelope import (
    HANDOFF_SCHEMA_VERSION,
    HandoffEnvelope,
)

NOW = datetime(2026, 5, 6, 22, 0, tzinfo=UTC)


def _minimal_envelope() -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id="cursor-fix-bug-3a7f9c1d",
        created_at=NOW,
        target_cli="cursor",
        prompt="fix the bug",
    )


def _full_envelope() -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id="claude-relay-from-cursor-deadbeef",
        created_at=NOW,
        source_cli="cursor",
        target_cli="claude",
        parent_task_id="task-parent-001",
        prompt="please code-review the diff at /tmp/foo.diff",
        cwd="/home/agent/workspace",
        adapter_extra={"model": "opus", "max_tokens": 8000, "中文键": "中文值"},
        constraints={"timeout": 120, "allowed_paths": ["/tmp", "/home"]},
        reason="cross-CLI handoff for code review",
        tags=["review", "high-priority", "v0.7.1"],
    )


def _special_chars_envelope() -> HandoffEnvelope:
    body = (
        "first line of multi-line prompt\n"
        "second line with quotes \"hi\" and 'bye'\n"
        "blank line follows\n"
        "\n"
        "line containing literal triple-dash: ---\n"
        "line ending with backslash \\\n"
        "emoji 🚀 中文 unicode\n"
        "末行没有换行符"
    )
    return HandoffEnvelope(
        handoff_id="echo-special-chars-cafef00d",
        created_at=NOW,
        target_cli="echo",
        prompt=body,
        adapter_extra={"key with spaces": "value: with colons"},
        tags=["---", "edge"],
    )


# ─────────────────── Pydantic schema validation ───────────────────


def test_required_fields_missing_raises_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        HandoffEnvelope(target_cli="cursor")  # type: ignore[call-arg]
    missing = {err["loc"][0] for err in exc_info.value.errors() if err["type"] == "missing"}
    assert {"handoff_id", "created_at", "prompt"}.issubset(missing)


def test_extra_forbid_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError) as exc_info:
        HandoffEnvelope(
            handoff_id="x-y-12345678",
            created_at=NOW,
            target_cli="cursor",
            prompt="hi",
            unknown_field="should-fail",  # type: ignore[call-arg]
        )
    assert any(err["type"] == "extra_forbidden" for err in exc_info.value.errors())


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"target_cli": ""},
        {"prompt": ""},
        {"handoff_id": ""},
    ],
)
def test_min_length_one_rejected(field_overrides: dict[str, str]) -> None:
    base = {
        "handoff_id": "ok-id-12345678",
        "created_at": NOW,
        "target_cli": "cursor",
        "prompt": "fine",
    }
    base.update(field_overrides)
    with pytest.raises(ValidationError):
        HandoffEnvelope(**base)


def test_default_values_match_spec() -> None:
    env = _minimal_envelope()
    assert env.schema_version == HANDOFF_SCHEMA_VERSION == "1"
    assert env.source_cli is None
    assert env.parent_task_id is None
    assert env.cwd is None
    assert env.reason is None
    assert env.adapter_extra == {}
    assert env.constraints == {}
    assert env.tags == []


# ─────────────────── to_markdown invariants ───────────────────


def test_to_markdown_starts_with_fence() -> None:
    md = _minimal_envelope().to_markdown()
    assert md.startswith("---\n"), f"first 12 chars: {md[:12]!r}"


def test_to_markdown_separates_front_matter_and_body() -> None:
    env = _minimal_envelope()
    md = env.to_markdown()
    # closing fence + body
    assert "\n---\n" in md
    closing_idx = md.index("\n---\n", 4)  # skip opening "---\n"
    body = md[closing_idx + len("\n---\n") :]
    assert body.rstrip("\n") == env.prompt


def test_to_markdown_does_not_put_prompt_in_front_matter() -> None:
    env = _minimal_envelope()
    md = env.to_markdown()
    # split out front-matter
    closing_idx = md.index("\n---\n", 4)
    front_matter = md[4:closing_idx]
    parsed = yaml.safe_load(front_matter)
    assert "prompt" not in parsed


def test_to_markdown_created_at_is_iso_string() -> None:
    md = _minimal_envelope().to_markdown()
    closing_idx = md.index("\n---\n", 4)
    front_matter = md[4:closing_idx]
    parsed = yaml.safe_load(front_matter)
    assert parsed["created_at"] == NOW.isoformat()
    assert isinstance(parsed["created_at"], str)


def test_to_markdown_preserves_none_as_null() -> None:
    md = _minimal_envelope().to_markdown()
    assert "source_cli: null" in md
    assert "parent_task_id: null" in md
    assert "cwd: null" in md
    assert "reason: null" in md


def test_to_markdown_canonicalizes_trailing_newlines_to_one() -> None:
    env = HandoffEnvelope(
        handoff_id="echo-x-12345678",
        created_at=NOW,
        target_cli="echo",
        prompt="body\n\n\n\n",
    )
    md = env.to_markdown()
    assert md.endswith("body\n")
    assert not md.endswith("body\n\n")


# ─────────────────── round-trip equivalence ───────────────────


@pytest.mark.parametrize(
    "factory",
    [_minimal_envelope, _full_envelope, _special_chars_envelope],
    ids=["minimal", "full", "special-chars"],
)
def test_roundtrip_from_markdown_to_markdown_equal(factory) -> None:
    original = factory()
    md = original.to_markdown()
    reparsed = HandoffEnvelope.from_markdown(md)
    assert reparsed == original


def test_roundtrip_preserves_multiline_prompt_with_embedded_fence() -> None:
    env = _special_chars_envelope()
    reparsed = HandoffEnvelope.from_markdown(env.to_markdown())
    assert reparsed.prompt == env.prompt
    assert "---" in reparsed.prompt


def test_roundtrip_preserves_unicode_in_adapter_extra() -> None:
    env = _full_envelope()
    reparsed = HandoffEnvelope.from_markdown(env.to_markdown())
    assert reparsed.adapter_extra["中文键"] == "中文值"


def test_roundtrip_preserves_full_envelope_field_by_field() -> None:
    env = _full_envelope()
    reparsed = HandoffEnvelope.from_markdown(env.to_markdown())
    # field-by-field assertion (in addition to the model __eq__ check)
    assert reparsed.handoff_id == env.handoff_id
    assert reparsed.created_at == env.created_at
    assert reparsed.source_cli == env.source_cli
    assert reparsed.target_cli == env.target_cli
    assert reparsed.parent_task_id == env.parent_task_id
    assert reparsed.cwd == env.cwd
    assert reparsed.adapter_extra == env.adapter_extra
    assert reparsed.constraints == env.constraints
    assert reparsed.reason == env.reason
    assert reparsed.tags == env.tags


# ─────────────────── from_markdown error paths ───────────────────


def test_from_markdown_missing_opening_fence_raises_value_error() -> None:
    with pytest.raises(ValueError, match=r"missing opening fence"):
        HandoffEnvelope.from_markdown("no fence here\nbody\n")


def test_from_markdown_missing_closing_fence_raises_value_error() -> None:
    with pytest.raises(ValueError, match=r"missing closing fence"):
        HandoffEnvelope.from_markdown("---\nschema_version: '1'\nno-closing-fence\n")


def test_from_markdown_yaml_parse_error_raises_value_error() -> None:
    bad = "---\n: ::not valid yaml [unclosed\n---\nbody\n"
    with pytest.raises(ValueError, match=r"YAML parse error"):
        HandoffEnvelope.from_markdown(bad)


def test_from_markdown_non_mapping_root_raises_value_error() -> None:
    with pytest.raises(ValueError, match=r"front-matter must be a YAML mapping"):
        HandoffEnvelope.from_markdown("---\n- just\n- a\n- list\n---\nbody\n")


def test_from_markdown_empty_body_raises_value_error() -> None:
    md = "---\nschema_version: '1'\n---\n"
    with pytest.raises(ValueError, match=r"prompt body is empty"):
        HandoffEnvelope.from_markdown(md)


def test_from_markdown_schema_validation_failure_wraps_into_value_error() -> None:
    md = (
        "---\n"
        "schema_version: '1'\n"
        "handoff_id: hid-12345678\n"
        f"created_at: '{NOW.isoformat()}'\n"
        # missing required target_cli → ValidationError → wrapped ValueError
        "---\n"
        "body text\n"
    )
    with pytest.raises(ValueError, match=r"schema validation failed"):
        HandoffEnvelope.from_markdown(md)


def test_from_markdown_extra_field_in_front_matter_wraps_into_value_error() -> None:
    md = (
        "---\n"
        "schema_version: '1'\n"
        "handoff_id: hid-12345678\n"
        f"created_at: '{NOW.isoformat()}'\n"
        "target_cli: cursor\n"
        "rogue_unknown_field: surprise\n"
        "---\n"
        "body text\n"
    )
    with pytest.raises(ValueError, match=r"schema validation failed"):
        HandoffEnvelope.from_markdown(md)
