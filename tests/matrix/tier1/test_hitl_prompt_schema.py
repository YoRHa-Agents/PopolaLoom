"""Tier 1 schema tests for ``popolaloom.hitl`` Pydantic models.

Per testing-matrix.md §1.1 + §11.1 — schema-only validation locks down
the v0.3.0 F4 (HITL full stack) contract before the renderer ships.
Each invariant violation MUST raise :class:`pydantic.ValidationError`
(workspace rule "No Silent Failures").

10 cases (1 happy path + 9 invariant violations):

1. Happy path — valid HITLPrompt + ArtifactRef + 3 options + 5 channels.
2. Trigger must be one of the 5 enum values.
3. ``why`` must be non-empty.
4. ``what`` must be non-empty.
5. ``options`` requires ≥ 2 entries.
6. ``options`` ids must be distinct.
7. ``default_option_id`` must match an existing option.id.
8. ``channels`` requires ≥ 2 entries.
9. ``deadline_seconds`` must be > 0.
10. ``deadline_seconds`` must be ≤ 86400 (1 day cap).

Plus: ArtifactRef.type enum + ArtifactRef.uri non-blank.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from popolaloom.hitl import (
    ArtifactRef,
    HITLOption,
    HITLPrompt,
)


def _two_options() -> list[HITLOption]:
    return [
        HITLOption(id="yes", label="Yes"),
        HITLOption(id="no", label="No", default=True),
    ]


def test_happy_path_valid_prompt_with_artifact_passes() -> None:
    """Case 1: valid HITLPrompt with all fields, including 1 ArtifactRef."""
    prompt = HITLPrompt(
        trigger="approval",
        why="Confirm destructive merge into main",
        what="Press Approve to fast-forward; Block to reject",
        options=[
            HITLOption(id="approve", label="Approve"),
            HITLOption(id="block", label="Block"),
            HITLOption(id="defer", label="Defer 24h", default=True),
        ],
        default_option_id="defer",
        channels=["lark", "ide", "cli"],
        deadline_seconds=3600,
        artifacts=[
            ArtifactRef(type="diff", uri="git://main..feature/foo", label="PR diff"),
        ],
        prompt_id="hitl-001",
    )
    assert prompt.trigger == "approval"
    assert prompt.default_option_id == "defer"
    assert len(prompt.options) == 3
    assert len(prompt.channels) == 3
    assert len(prompt.artifacts) == 1
    assert prompt.artifacts[0].type == "diff"


def test_trigger_must_be_one_of_five_enum_values() -> None:
    """Case 2: trigger=`other` raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="other",  # type: ignore[arg-type]
            why="x",
            what="y",
            options=_two_options(),
            default_option_id="no",
            channels=["lark", "ide"],
            deadline_seconds=60,
        )
    assert "trigger" in str(excinfo.value).lower()


def test_why_must_be_non_empty() -> None:
    """Case 3: empty `why` raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="",
            what="not blank",
            options=_two_options(),
            default_option_id="no",
            channels=["lark", "ide"],
            deadline_seconds=60,
        )
    assert "why" in str(excinfo.value).lower()


def test_what_must_be_non_empty() -> None:
    """Case 4: empty `what` raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="ok",
            what="",
            options=_two_options(),
            default_option_id="no",
            channels=["lark", "ide"],
            deadline_seconds=60,
        )
    assert "what" in str(excinfo.value).lower()


def test_options_requires_at_least_two_entries() -> None:
    """Case 5: options=[1 entry] raises ValidationError (binary minimum)."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=[HITLOption(id="only", label="Only")],
            default_option_id="only",
            channels=["lark", "ide"],
            deadline_seconds=60,
        )
    assert "options" in str(excinfo.value).lower()


def test_option_ids_must_be_distinct() -> None:
    """Case 6: two options with same id raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=[
                HITLOption(id="dup", label="One"),
                HITLOption(id="dup", label="Two"),
            ],
            default_option_id="dup",
            channels=["lark", "ide"],
            deadline_seconds=60,
        )
    assert "distinct" in str(excinfo.value).lower() or "dup" in str(excinfo.value).lower()


def test_default_option_id_must_match_existing_option() -> None:
    """Case 7: default_option_id pointing to non-existent id raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=_two_options(),
            default_option_id="missing-id",
            channels=["lark", "ide"],
            deadline_seconds=60,
        )
    assert (
        "default_option_id" in str(excinfo.value).lower()
        or "missing-id" in str(excinfo.value)
    )


def test_channels_requires_at_least_two_entries() -> None:
    """Case 8: single-channel prompt raises (multi-channel rule per spec §12.8)."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=_two_options(),
            default_option_id="no",
            channels=["lark"],
            deadline_seconds=60,
        )
    assert "channels" in str(excinfo.value).lower()


def test_deadline_seconds_must_be_positive() -> None:
    """Case 9a: deadline_seconds=0 raises ValidationError (gt=0)."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=_two_options(),
            default_option_id="no",
            channels=["lark", "ide"],
            deadline_seconds=0,
        )
    assert "deadline_seconds" in str(excinfo.value).lower()


def test_deadline_seconds_must_be_at_most_one_day() -> None:
    """Case 9b: deadline_seconds=86401 (> 1 day) raises ValidationError (le=86400)."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=_two_options(),
            default_option_id="no",
            channels=["lark", "ide"],
            deadline_seconds=86401,
        )
    assert "deadline_seconds" in str(excinfo.value).lower()


def test_artifact_ref_type_enum_rejects_unknown_value() -> None:
    """Case 10: ArtifactRef.type="garbage" raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        ArtifactRef(type="garbage", uri="x://y")  # type: ignore[arg-type]
    assert "type" in str(excinfo.value).lower()


def test_artifact_ref_uri_must_be_non_blank() -> None:
    """Bonus: ArtifactRef.uri="   " raises ValidationError (custom validator)."""
    with pytest.raises(ValidationError):
        ArtifactRef(type="event_log", uri="   ")


def test_hitl_option_id_must_not_contain_whitespace() -> None:
    """Bonus: HITLOption.id="bad id" raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        HITLOption(id="bad id", label="Bad")
    assert "whitespace" in str(excinfo.value).lower() or "id" in str(excinfo.value).lower()


def test_channels_must_be_distinct() -> None:
    """Bonus: duplicate channels raise ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        HITLPrompt(
            trigger="approval",
            why="x",
            what="y",
            options=_two_options(),
            default_option_id="no",
            channels=["lark", "lark"],
            deadline_seconds=60,
        )
    assert "distinct" in str(excinfo.value).lower() or "channels" in str(excinfo.value).lower()


def test_artifact_ref_is_frozen_immutable() -> None:
    """Bonus: ArtifactRef is frozen — assignment raises ValidationError."""
    ref = ArtifactRef(type="event_log", uri="file:///tmp/x.jsonl")
    with pytest.raises(ValidationError):
        ref.uri = "other://"  # type: ignore[misc]
