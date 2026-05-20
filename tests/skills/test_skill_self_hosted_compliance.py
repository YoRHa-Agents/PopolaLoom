"""Skill v1.5.2 single-path constraint #6 compliance tests (v1.6.1, Wave B3).

`.local/feedbacks/feedback_for_v1.5.2.md` constraint #6 demands that the
`popola-loom` Skill document describes the **single canonical Path-B
self-hosted dispatch surface**: no `--pool` / `--pool-name`, no
`--allow-fallback`, no `--auth-mode=rest` adjacent to
`--cloud-target=self-hosted`, no GitHub-App preflight instructions for
self-hosted, and (Wave B3) `agent login` instead of the legacy
`cursor login` spelling. This module enforces those invariants as
parametric pytest cases so future v1.x bumps cannot silently re-introduce
the deprecated surface.

Two copies of the Skill live in the repo (the wheel-shipped one under
`src/popolaloom/skills/popola-loom/SKILL.md` and the developer-edit copy
under `.claude/skills/popola-loom/SKILL.md`); each case is parametrised
over both paths so the wheel + developer surfaces stay aligned. The
final case asserts the two copies are byte-identical to lock the sync
contract end-to-end.

**Transient red note (Wave B4 dependency).** Wave B3 lands these tests
ahead of Wave B4's SKILL.md `cursor login` → `agent login` rewrite, so
`test_skill_uses_agent_login_not_cursor_login` is expected to FAIL until
Wave B4 merges. The byte-identical test
`test_skill_copies_byte_identical` should remain green throughout (both
copies share a build pipeline that keeps them in lockstep), but if Wave
B4 updates one copy in isolation that test would also flip red until the
sync is reasserted. Stage C's full pytest pass will validate the green
state once Wave B4 lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATHS: tuple[Path, ...] = (
    _REPO_ROOT / "src" / "popolaloom" / "skills" / "popola-loom" / "SKILL.md",
    _REPO_ROOT / ".claude" / "skills" / "popola-loom" / "SKILL.md",
)


def _heading_level(line: str) -> int | None:
    """Return the 1-based markdown heading depth of ``line``, or None."""
    match = re.match(r"^(#{1,6})\s+\S", line)
    return len(match.group(1)) if match else None


def _heading_text(line: str) -> str:
    """Return the heading text with the leading ``#`` prefix stripped."""
    return re.sub(r"^#+\s+", "", line).strip()


def _self_hosted_sections(text: str) -> list[tuple[str, list[str]]]:
    """Return ``(heading, body_lines)`` tuples for sections whose heading mentions "self-hosted".

    A section's body spans the lines immediately after its heading up to
    (but excluding) the next heading at the SAME or HIGHER level (shallower
    depth). Deeper-level subheadings (``####`` under ``###``) stay inside
    the parent section's body so subsection prose / code blocks are also
    scanned for the compliance invariants.
    """
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(lines):
        level = _heading_level(lines[i])
        if level is None:
            i += 1
            continue
        heading = _heading_text(lines[i])
        if "self-hosted" in heading.lower():
            body_start = i + 1
            j = body_start
            while j < len(lines):
                sub_level = _heading_level(lines[j])
                if sub_level is not None and sub_level <= level:
                    break
                j += 1
            sections.append((heading, lines[body_start:j]))
            i = j
        else:
            i += 1
    return sections


def _fenced_code_blocks(body_lines: list[str]) -> list[list[str]]:
    """Extract fenced code block contents (excluding the ```` ``` ```` fences)."""
    blocks: list[list[str]] = []
    current: list[str] = []
    in_block = False
    for line in body_lines:
        if re.match(r"^```", line):
            if in_block:
                blocks.append(current)
                current = []
                in_block = False
            else:
                in_block = True
        elif in_block:
            current.append(line)
    if in_block:
        blocks.append(current)
    return blocks


def test_self_hosted_section_detection_finds_at_least_one() -> None:
    """Sanity check: the section scanner finds the v1.6.0 self-hosted surface.

    Guards against accidental refactors that would drop ALL "self-hosted"
    headings (which would silently disarm cases 1-4 by giving them no
    sections to scan). Both Skill copies should expose at least three
    self-hosted sections post-v1.6.0 (Workflow 10 + Workflow 12 + the
    Configuration "Path-B self-hosted worker dispatch" reference).
    """
    for skill_path in SKILL_PATHS:
        text = skill_path.read_text(encoding="utf-8")
        sections = _self_hosted_sections(text)
        assert len(sections) >= 3, (
            f"{skill_path} expected ≥3 self-hosted sections, got "
            f"{len(sections)}: {[h for h, _ in sections]!r}"
        )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=lambda p: p.parts[-3])
def test_skill_no_pool_flag_in_self_hosted_examples(skill_path: Path) -> None:
    """v1.5.2 constraint #1: no ``--pool`` / ``--pool-name`` in self-hosted code blocks.

    v1.6.0 removed the ``--pool`` flag pair from
    :func:`popolaloom.cli.cloud_worker_cmd.worker_start_cmd`; the Skill
    must not show operators those flags in any self-hosted example. The
    rule only applies to FENCED CODE BLOCKS — prose may still reference
    the removed flags to explain the deprecation.
    """
    text = skill_path.read_text(encoding="utf-8")
    for heading, body in _self_hosted_sections(text):
        for block in _fenced_code_blocks(body):
            for line in block:
                assert "--pool" not in line, (
                    f"{skill_path.name} section {heading!r} contains a "
                    f"self-hosted example with the removed --pool flag: "
                    f"{line!r}"
                )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=lambda p: p.parts[-3])
def test_skill_no_allow_fallback_for_self_hosted(skill_path: Path) -> None:
    """v1.5.2 constraint #2: no ``--allow-fallback`` in self-hosted code blocks.

    ``--allow-fallback`` is a no-op + WARN under
    ``--cloud-target=self-hosted`` (v1.6.0). Listing it in a self-hosted
    example would mislead operators into thinking the fallback chain
    applies. Cursor-managed / local sections may still mention the flag —
    those don't trigger this scan because their headings don't match
    "self-hosted".
    """
    text = skill_path.read_text(encoding="utf-8")
    for heading, body in _self_hosted_sections(text):
        for block in _fenced_code_blocks(body):
            for line in block:
                assert "--allow-fallback" not in line, (
                    f"{skill_path.name} section {heading!r} contains a "
                    f"self-hosted example with the no-op --allow-fallback "
                    f"flag: {line!r}"
                )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=lambda p: p.parts[-3])
def test_skill_no_rest_auth_mode_for_self_hosted(skill_path: Path) -> None:
    """v1.5.2 constraint #5: ``--auth-mode=rest`` is rejected for self-hosted.

    Per-block rule: in any self-hosted section's fenced code block, IF
    a line contains ``--cloud-target=self-hosted``, THEN no line in the
    SAME code block may contain ``--auth-mode=rest``. Other code blocks
    inside the same self-hosted section (e.g. a sibling managed-cloud
    example sharing the section) are evaluated independently.
    """
    text = skill_path.read_text(encoding="utf-8")
    for heading, body in _self_hosted_sections(text):
        for block in _fenced_code_blocks(body):
            block_text = "\n".join(block)
            if "--cloud-target=self-hosted" not in block_text:
                continue
            assert "--auth-mode=rest" not in block_text, (
                f"{skill_path.name} section {heading!r} has a code block "
                f"that pairs --cloud-target=self-hosted with the "
                f"forbidden --auth-mode=rest: {block_text!r}"
            )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=lambda p: p.parts[-3])
def test_skill_no_github_preflight_for_self_hosted(skill_path: Path) -> None:
    """v1.5.2 constraint #3: self-hosted dispatch skips the GitHub-App preflight.

    Self-hosted workers hold their own workspace clone, so the Cursor
    GitHub-App allowlist / repository-probe gates do not run. The Skill
    must NOT instruct operators to invoke any of the
    ``GitHub-App preflight`` / ``GET /v1/repositories`` /
    ``GitHub-App probe`` / ``GitHub-App check`` surfaces in a
    self-hosted context. We scan BOTH prose and code in each self-hosted
    section and assert 0 occurrences — positive coverage that documents
    the gate as OFF should live in non-self-hosted sections (e.g. the
    "No-Silent-Fallback invariant" Configuration heading).
    """
    forbidden_phrases = (
        "GitHub-App preflight",
        "GET /v1/repositories",
        "GitHub-App probe",
        "GitHub-App check",
    )
    text = skill_path.read_text(encoding="utf-8")
    for heading, body in _self_hosted_sections(text):
        body_text = "\n".join(body)
        for phrase in forbidden_phrases:
            assert phrase not in body_text, (
                f"{skill_path.name} section {heading!r} mentions the "
                f"forbidden GitHub-App phrase {phrase!r} in a "
                f"self-hosted context"
            )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=lambda p: p.parts[-3])
def test_skill_uses_agent_login_not_cursor_login(skill_path: Path) -> None:
    """v1.6.1 Wave B4 — Skill instructs ``agent login`` (not legacy ``cursor login``).

    The Cursor CLI rebranded from ``cursor-agent`` to ``agent`` in
    2026.05; the Skill must use the canonical ``agent login`` spelling
    everywhere it mentions the JWT bootstrap command. This test will
    FAIL until Wave B4 lands the SKILL.md rewrite. Stage C re-runs the
    full pytest gate post-B4 to confirm green.
    """
    text = skill_path.read_text(encoding="utf-8")
    assert "cursor login" not in text, (
        f"{skill_path.name} still uses legacy `cursor login` spelling; "
        f"Wave B4 must rewrite to `agent login`"
    )
    assert "agent login" in text, (
        f"{skill_path.name} does not mention `agent login` — at least "
        f"one occurrence is required to document the JWT bootstrap "
        f"command for self-hosted operators"
    )


def test_skill_copies_byte_identical() -> None:
    """Both Skill copies are byte-identical so the wheel + developer surface stays in lockstep.

    Wave B4 SHOULD update both copies in a single coordinated rewrite,
    so this test should remain green throughout the v1.6.1 release. If
    it flips red, the developer-edit copy under ``.claude/skills`` has
    drifted from the wheel-shipped copy under ``src/popolaloom/skills``
    (or vice versa) — re-sync before merging.
    """
    primary_bytes = SKILL_PATHS[0].read_bytes()
    secondary_bytes = SKILL_PATHS[1].read_bytes()
    assert primary_bytes == secondary_bytes, (
        f"SKILL.md copies have drifted: "
        f"{SKILL_PATHS[0]} ({len(primary_bytes)} bytes) vs "
        f"{SKILL_PATHS[1]} ({len(secondary_bytes)} bytes)"
    )
