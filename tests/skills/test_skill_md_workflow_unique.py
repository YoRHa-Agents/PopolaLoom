"""Lint canonical Skill workflow headings for uniqueness and continuity."""

from __future__ import annotations

import re
from pathlib import Path


def test_skill_md_workflow_numbers_are_unique_and_contiguous() -> None:
    skill_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "popolaloom"
        / "skills"
        / "popola-loom"
        / "SKILL.md"
    )
    body = skill_path.read_text(encoding="utf-8")

    numbers = [int(match) for match in re.findall(r"^### Workflow (\d+) ", body, re.M)]

    assert numbers, "expected at least one Workflow heading"
    assert len(numbers) == len(set(numbers)), f"duplicate Workflow headings: {numbers}"
    assert numbers == list(range(1, max(numbers) + 1))
