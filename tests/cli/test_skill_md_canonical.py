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
    """Body length sits in ``[8 000, 40 000]`` chars (~ 2 000–10 000 tokens).

    Bump history:
      - Original Stage S3 window: ``[8 000, 16 000]`` (target ~ 11 000
        chars / ~ 2 800 tokens, per plan §S3.7).
      - v0.8.6 bump: 16 000 → 20 000 (SSE + 422 hint additions; see
        v0.8.6 COVERAGE.md §5).
      - v0.8.7 bump: 20 000 → 28 000 (cloud HITL Workflow 7 + γ
        deployment instructions + 22-item quick-reference table; see
        v0.8.7 REVIEW.md M-skill-md and the v0.8.7-cloud-hitl-prod
        change folder for context).
      - v0.8.8 bump: 28 000 → 32 000 (Workflow 8 — Cross-PR relay
        with Q-C-4 deviation callout + 5 mitigations + Workflow 9 —
        ``popola cloud runs`` walkthrough w/ multi-run lifecycle; see
        v0.8.8 plan §4.3 T2.3.1 + §4.4 T2.4.2 for AC and
        relay-auto-safety.md §6.
      - **v0.9.1 bump: 32 000 → 34 000** (Workflow 10 — Self-hosted
        worker handoff via ``popola cloud worker {debug,start,status,
        handoff}``; the SKILL is the canonical user-facing reference
        for cloud workloads and Workflow 10 documents the third
        dispatch lane v0.9.1 introduces alongside ``--cli=cursor`` and
        ``--cli=cursor-cloud``. Compressed to a single 14-line block
        (mental model + 4-verb summary + minimal command surface);
        full prose lives in ``docs/USER_GUIDE.md``).
      - **v1.1.0 bump: 34 000 → 40 000** (Workflow 11 guided dispatch
        Q&A + Workflow 12 Path-B advanced dispatch).
      - **v1.5.0 bump: 40 000 → 48 000** (No-Silent-Fallback invariant
        spec table + popolad 4-tier env injection chain doc + path-B
        self-hosted worker dispatch quick-reference. The three new
        subsections under ``## Configuration`` are the operator-facing
        reference for the v1.5.0 contract; trimming them would push
        operators back to spelunking the CHANGELOG. ~1.8 KB net growth
        per PLAN.md Phase J.)
      - **v1.6.0 bump: 48 000 → 54 000** (`feedback_for_v1.5.2.md`
        single-path self-hosted dispatch: rewritten Workflow 6 with
        explicit managed vs self-hosted code blocks, updated
        Workflow 10 / 12 to the single-path shape, new
        ``Verifying a self-hosted dispatch`` section with the
        ``view:`` URL contract, and the v1.6.0 No-Silent-Fallback
        4-row table. ~3.5 KB net growth — the new self-hosted
        examples replace the v1.5.x ``--auth-mode=session-jwt``
        boilerplate that operators had to copy across multiple
        sections).
    Any further growth past 54 000 must re-trigger the trim-vs-bump
    discussion — do NOT bump again silently.
    """
    body_len = len(skill_body)
    # v1.6.0 bump: 48_000 → 54_000 (single-path self-hosted Workflow 6/10/12
    # rewrites + Verifying a self-hosted dispatch + 4-row no-silent-fallback)
    assert 8_000 <= body_len <= 54_000, (
        f"SKILL.md body length {body_len} chars is outside the "
        f"[8 000, 54 000] token-budget window (target ~ 11 000–50 000)."
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


def test_skill_md_documents_ambiguity_protocol(skill_body: str) -> None:
    """v1.1.0 guided dispatch and Path-B docs are present.

    v1.6.0 renames the Workflow 12 header to
    ``Workflow 12 — Path-B self-hosted dispatch (v1.6.0 single path)``
    to reflect the single-canonical-path contract; the substring check
    below uses the v1.6.0 wording.
    """
    required = [
        "## Ambiguity Resolution Protocol",
        "target/model/thinking depth/special modes",
        "AskQuestion",
        "Workflow 13 — Guided dispatch with option-group Q&A",
        "Workflow 12 — Path-B self-hosted dispatch",
        "popola dispatch <prompt> --wizard",
        "schema_version = 2",
    ]
    for needle in required:
        assert needle in skill_body


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


# ── Tracked project-level skill files (PR1 v1.3.0 skill bump) ────────────
#
# Two project-level skill files are git-tracked at the repo root and must
# travel in lockstep with ``popolaloom.__version__``.  Their committed
# content is what new contributors clone, so a release that bumps the
# wheel-shipped ``src/popolaloom/skills/popola-loom/SKILL.md`` but forgets
# these tracked copies leaves ``popola doctor`` reporting DRIFT on a fresh
# clone (this is exactly what happened in PR #32 / PR #33 when the wheel
# went 1.1.0 → 1.1.1 → 1.3.0 but these two files stayed at 1.1.0).
#
# This release-process safeguard is the cheap-and-loud counterpart to the
# wheel-side ``test_skill_md_version_matches_package`` above: if a future
# minor bump (v1.4.0, v1.5.0, …) lands without re-running
# ``popola skill upgrade --target=<claude|copilot> --project`` on the
# release branch, the default-lane test suite fails before the tag is cut.

_TRACKED_PROJECT_SKILL_PATHS: tuple[tuple[str, Path], ...] = (
    (
        "claude-project",
        _REPO_ROOT / ".claude" / "skills" / "popola-loom" / "SKILL.md",
    ),
    (
        "copilot-project",
        _REPO_ROOT / ".github" / "copilot-instructions.md",
    ),
)


@pytest.mark.parametrize(
    "tracked_path",
    [pytest.param(path, id=label) for label, path in _TRACKED_PROJECT_SKILL_PATHS],
)
def test_tracked_project_skill_version_matches_package(tracked_path: Path) -> None:
    """Tracked project skill files travel with :data:`popolaloom.__version__`.

    Mirrors :func:`test_skill_md_version_matches_package` for the
    wheel-bundled skill but operates on the in-repo tracked copies.  See
    the module docstring above the parametrised paths for the full
    rationale; the short version is "PR #32 / PR #33 forgot to bump these
    files and ``popola doctor`` started reporting DRIFT".

    Remediation when this test fires:
        ``popola skill upgrade --target=<claude|copilot> --project``
    """
    assert tracked_path.is_file(), (
        f"tracked project skill missing: {tracked_path}; PR1 of the "
        f"v1.3.0 skill bump committed both files — restore via "
        f"`popola skill upgrade --target=<claude|copilot> --project`."
    )

    text = tracked_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    assert match is not None, (
        f"{tracked_path} does not start with a `---` YAML frontmatter block."
    )

    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(match.group(1))
    assert isinstance(parsed, dict), (
        f"{tracked_path} frontmatter is not a mapping: "
        f"{type(parsed).__name__}"
    )

    version = parsed.get("version")
    assert version == __version__, (
        f"{tracked_path} frontmatter version {version!r} drifts from "
        f"popolaloom.__version__ {__version__!r}; bump the tracked file "
        f"alongside the wheel-shipped SKILL.md "
        f"(`popola skill upgrade --target=<claude|copilot> --project`)."
    )


