"""Default-lane tests for the canonical PopolaLoom SKILL.md (Stage S3 of v0.5.0).

Six contract tests that pin the wheel-bundled SKILL.md shape so any
future content edit fails fast if it drifts off the v0.5.0 plan §4
Stage S3 contract:

1. The file ships at the canonical wheel path that
   :func:`popolaloom.cli._skill_source.canonical_source_path` resolves.
2. Frontmatter parses as valid YAML and has the three Anthropic-baseline
   required keys (``name`` / ``version`` / ``description``).
3. ``version`` matches :data:`popolaloom.__version__` (CI-strict — Stage
   S5 will bump the wheel and the SKILL.md in lockstep, this test
   catches a one-side-only edit).
4. Body length is in ``[8 000, 16 000]`` characters — the documented
   ~ 2 800-token / ~ 11 KB budget (per research §B.3 + plan §S3.7) with
   ±50 % headroom so author-driven minor edits don't flap CI.
5. The body has all 7 expected canonical section headers required by
   the v0.5.0 plan §S3 (What / When-to-use / Quick reference /
   Workflows / Configuration / Reference / Version + upgrade).
6. No raw ``TODO`` literal survives anywhere in the file — the Stage S2
   placeholder used a "TODO: Stage S3 will replace this" sentinel; the
   final canonical content must not leak any leftover TODO marker.

Why default-lane: these are pure read-and-parse checks (no daemon, no
network, no real CLI), runtime well under 100 ms total.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from popolaloom import __version__
from popolaloom.cli._skill_source import canonical_source_path

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_PATH = (
    _REPO_ROOT / "src" / "popolaloom" / "skills" / "popola-loom" / "SKILL.md"
)

_EXPECTED_SECTION_HEADERS: tuple[str, ...] = (
    "## What is PopolaLoom?",
    "## When to use this Skill",
    "## Quick reference — common commands",
    "## Workflows",
    "## Configuration",
    "## Reference",
    "## Version + upgrade",
)


@pytest.fixture(scope="module")
def skill_text() -> str:
    """Return the canonical SKILL.md content as a UTF-8 string.

    Resolves via :func:`canonical_source_path` so the test passes both
    in editable / source checkouts and in installed wheels (the path
    differs but the content is byte-identical).  Falls back to the
    in-repo path so tests can run before ``pip install -e .`` lands.
    """
    canonical = canonical_source_path()
    if canonical is not None:
        return canonical.read_text(encoding="utf-8")
    return _CANONICAL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill_frontmatter(skill_text: str) -> dict[str, object]:
    """Return the parsed YAML frontmatter as a dict.

    Imports PyYAML lazily so the test surfaces a clear skip message when
    PyYAML is somehow unavailable (it ships transitively via
    fastapi / uvicorn so every supported install has it).
    """
    yaml = pytest.importorskip("yaml")
    match = _FRONTMATTER_RE.match(skill_text)
    if match is None:
        pytest.fail("SKILL.md does not start with a `---` YAML frontmatter block.")
    raw = match.group(1)
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        pytest.fail(f"SKILL.md frontmatter is not a mapping: {type(parsed).__name__}")
    return parsed


@pytest.fixture(scope="module")
def skill_body(skill_text: str) -> str:
    """Return the SKILL.md body (everything after the closing ``---``)."""
    match = _FRONTMATTER_RE.match(skill_text)
    if match is None:
        pytest.fail("SKILL.md missing frontmatter delimiter.")
    return match.group(2)


def test_skill_md_file_exists() -> None:
    """The canonical SKILL.md ships at the wheel-resolvable path.

    Stage S3 acceptance #1: ``importlib.resources``-resolved path is
    not None *and* the in-repo path resolves; either is sufficient
    proof that the file ships with the package.
    """
    canonical = canonical_source_path()
    assert canonical is not None or _CANONICAL_PATH.is_file(), (
        "canonical SKILL.md must exist at "
        f"{_CANONICAL_PATH} (or its wheel equivalent)"
    )
    if canonical is not None:
        assert canonical.is_file()
        assert canonical.stat().st_size > 0


def test_skill_md_frontmatter_yaml_required_keys(
    skill_frontmatter: dict[str, object],
) -> None:
    """Frontmatter parses as YAML and has the three Anthropic-baseline keys.

    The Anthropic baseline (per research §B.1) requires ``name``,
    ``version``, and ``description``; without these the host agent's
    Skill router will skip the skill.  All three values must be
    non-empty strings.
    """
    for key in ("name", "version", "description"):
        assert key in skill_frontmatter, (
            f"frontmatter missing required key {key!r}"
        )
        value = skill_frontmatter[key]
        assert isinstance(value, str) and value.strip(), (
            f"frontmatter[{key!r}] must be a non-empty string; got {value!r}"
        )

    assert skill_frontmatter["name"] == "popola-loom", (
        "skill `name` is locked at 'popola-loom' (Q5-1 lock — renamed "
        "from 'popolaloom' in v0.7.1+); changing it breaks every install "
        "path under .cursor/skills/popola-loom/."
    )


def test_skill_md_version_matches_package(
    skill_frontmatter: dict[str, object],
) -> None:
    """``frontmatter.version`` matches :data:`popolaloom.__version__`.

    Future-proof: written against ``popolaloom.__version__`` so Stage
    S5's wheel-version bump (0.4.1 → 0.5.0) makes the SKILL.md and the
    package version travel together.  A drift here means either the
    SKILL.md author edited only one side, or Stage S5 forgot to bump
    the SKILL.md frontmatter.
    """
    assert skill_frontmatter["version"] == __version__, (
        f"SKILL.md version {skill_frontmatter['version']!r} drifts from "
        f"popolaloom.__version__ {__version__!r}; bump both in lockstep."
    )


def test_skill_md_body_length_in_token_budget(skill_body: str) -> None:
    """Body length sits in ``[8 000, 20 000]`` chars (~ 2 000–5 000 tokens).

    Original Stage S3 window was ``[8 000, 16 000]`` (target ~ 11 000
    chars / ~ 2 800 tokens, per plan §S3.7).  v0.8.6 T2.3.2 docs sync
    added SSE ingest + 422 hint catalog content to the cloud workflow
    section, legitimately pushing the body to ~ 17 800 chars; the
    SKILL is the canonical user-facing reference and those additions
    are intentional, so the ceiling is bumped one-time 16 000 → 20 000
    (see COVERAGE.md §5).  Any further growth past 20 000 must
    re-trigger the trim-vs-bump discussion — do NOT bump again silently.
    """
    body_len = len(skill_body)
    # v0.8.6 bump: 16_000 → 20_000 (SSE + 422 hint additions; see COVERAGE.md §5)
    assert 8_000 <= body_len <= 20_000, (
        f"SKILL.md body length {body_len} chars is outside the "
        f"[8 000, 20 000] token-budget window (target ~ 11 000–17 800)."
    )


def test_skill_md_body_has_all_canonical_sections(skill_body: str) -> None:
    """The body has all 7 canonical section headers from the v0.5.0 plan.

    Each header is asserted as an exact substring (whitespace + em-dash
    sensitive) so a typo or rename surfaces here rather than at the
    host-agent's intent-routing phase.
    """
    missing = [h for h in _EXPECTED_SECTION_HEADERS if h not in skill_body]
    assert not missing, (
        f"SKILL.md body is missing canonical section headers: {missing!r}; "
        f"expected exactly: {list(_EXPECTED_SECTION_HEADERS)!r}"
    )


def test_skill_md_no_residual_todo_marker(skill_text: str) -> None:
    """Neither frontmatter nor body contains a leftover ``TODO`` marker.

    The Stage S2 placeholder used a literal ``"TODO: Stage S3 will
    replace this"`` sentinel; the canonical Stage S3 content must
    remove every ``TODO`` (case-sensitive — typical English ``Todo``
    or ``todo`` in a code example would not match).
    """
    assert "TODO" not in skill_text, (
        "Canonical SKILL.md still contains a `TODO` literal; remove the "
        "Stage S2 placeholder leftover or move it into a non-shipping doc."
    )
