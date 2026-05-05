"""skill_inject — devola-flow skill detection + Workflow Context prepend (v0.3.0 F2.5).

Per [v0.3.0-plan.md §4 Stage F2.5](../../../.local/memory/specs/popolaloom/v0.3.0-plan.md)
+ [roadmap §11.1-§11.4](/root/.cursor/plans/popolaloom_v0.2-v0.4_roadmap_e3d38a10.plan.md):

When dispatch is invoked with ``--evolution-round=N`` (RPC ``evolution_round``
query parameter), the daemon must:

1. Check whether ``~/.cursor/skills/devola-flow/SKILL.md`` or
   ``~/.claude/skills/devola-flow/SKILL.md`` is present (the L3 agent
   needs this to render its 3-section output: Acceptance Verification,
   Gate Score Components, Findings).
2. Build a :class:`WorkflowContext` (round_num, max_rounds, prior_nines,
   reinforcement_rules, gate_threshold).
3. Prepend ``WorkflowContext.render()`` to the user prompt.

When the skill is missing, we still prepend the context (so the L3
prompt is at least *informed* about the round / threshold / prior
nines), but emit a structured warning event ``skill.missing`` so
operators can detect the regression in NDJSON event logs.

Workspace rule "No Silent Failures": skill missing is ALWAYS logged
(via ``logger.warning``) AND surfaced to callers via the
:class:`SkillCheckResult` return so the dispatcher can write a
``skill.missing`` envelope to the per-task event log.

v0.5.0 Stage S4 extension — :data:`SKILL_TARGETS` registry
-----------------------------------------------------------

Stage S4 introduces ``popola skill install / doctor / upgrade`` (CLI
verbs at :mod:`popolaloom.cli.skill_cmd`) and ``popola doctor`` (the
aggregate health probe at :mod:`popolaloom.cli.doctor_cmd`). All three
new verbs and the underlying library APIs in
:mod:`popolaloom.evolution.skill_install` /
:mod:`popolaloom.evolution.skill_doctor` /
:mod:`popolaloom.evolution.skill_upgrade` consume a single shared
registry of (target × scope → SKILL.md path) resolvers exposed here as
:data:`SKILL_TARGETS`.  The legacy ``CURSOR_SKILL_PATH`` /
``CLAUDE_SKILL_PATH`` tuples — which describe the devola-flow skill
detection paths used during evolution-round dispatch — remain at the
top of the module as aliases so v0.3 / v0.4 callers continue to work
without import changes.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from popolaloom.evolution import WorkflowContext

logger = logging.getLogger(__name__)


CURSOR_SKILL_PATH: tuple[str, ...] = (".cursor", "skills", "devola-flow", "SKILL.md")
"""Relative path components for the cursor-agent skill location.

Legacy v0.3.0 alias: refers to the *devola-flow* SKILL.md path, used by
:func:`check_skill_present` at evolution-round dispatch time.  The new
v0.5.0 ``popola skill install`` registry (for the *popolaloom* skill)
lives at :data:`SKILL_TARGETS` below.
"""

CLAUDE_SKILL_PATH: tuple[str, ...] = (".claude", "skills", "devola-flow", "SKILL.md")
"""Relative path components for the claude code skill location.

Legacy v0.3.0 alias — see :data:`CURSOR_SKILL_PATH`.
"""


# ── v0.5.0 Stage S4 — SKILL_TARGETS registry (popolaloom skill) ──────────
#
# Each registry value is a zero-argument callable returning the absolute
# Path to that target's ``SKILL.md`` install location.  Callables (rather
# than pre-computed Path constants) keep the registry test-friendly:
# ``Path.home()`` and ``Path.cwd()`` are resolved at lookup time, so
# ``monkeypatch.setattr(Path, "home", ...)`` in test fixtures works
# without re-importing the module.
#
# The four targets mirror the ``popola init`` verb matrix locked in plan
# §S2 (cursor / claude / codex / copilot); supported scopes per target:
#
#   cursor   — global (~/.cursor/...)         + project (<cwd>/.cursor/...)
#   claude   — global (~/.claude/...)         + project (<cwd>/.claude/...)
#   codex    — global ($CODEX_HOME or ~/.codex/...) — codex has no project scope
#   copilot  — project (<cwd>/.github/copilot-instructions.md) — single-file, project-only
#
# Operators occasionally pass ``--global`` to a target that only supports
# project (e.g. ``popola skill install --target=copilot --global``); the
# CLI verb resolves this to ``project`` and prints a warning, mirroring
# the ``popola init copilot`` fallback.


_POPOLALOOM_SKILL_RELATIVE: tuple[str, ...] = ("skills", "popolaloom", "SKILL.md")
"""Path components appended under each IDE's home / project root."""


def _cursor_global_target() -> Path:
    return Path.home() / ".cursor" / Path(*_POPOLALOOM_SKILL_RELATIVE)


def _cursor_project_target() -> Path:
    return Path.cwd() / ".cursor" / Path(*_POPOLALOOM_SKILL_RELATIVE)


def _claude_global_target() -> Path:
    return Path.home() / ".claude" / Path(*_POPOLALOOM_SKILL_RELATIVE)


def _claude_project_target() -> Path:
    return Path.cwd() / ".claude" / Path(*_POPOLALOOM_SKILL_RELATIVE)


def _codex_global_target() -> Path:
    """Resolve the codex SKILL.md install path.

    Honours ``$CODEX_HOME`` per the Codex CLI convention; falls back to
    ``~/.codex`` when unset (mirrors :func:`popolaloom.cli.init_cmd.codex_target_path`).
    """
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home / Path(*_POPOLALOOM_SKILL_RELATIVE)


def _copilot_project_target() -> Path:
    return Path.cwd() / ".github" / "copilot-instructions.md"


SKILL_TARGETS: dict[str, dict[str, Callable[[], Path]]] = {
    "cursor": {
        "global": _cursor_global_target,
        "project": _cursor_project_target,
    },
    "claude": {
        "global": _claude_global_target,
        "project": _claude_project_target,
    },
    "codex": {
        "global": _codex_global_target,
    },
    "copilot": {
        "project": _copilot_project_target,
    },
}
"""Registry of popolaloom-skill install targets keyed by ``target`` × ``scope``.

Shape: ``dict[str, dict[str, Callable[[], Path]]]`` — each leaf is a
zero-argument callable that returns the absolute :class:`Path` to that
target's ``SKILL.md`` (or, for copilot, the single-file
``copilot-instructions.md``).  Callables are resolved at lookup time so
test fixtures that patch :meth:`Path.home` / :meth:`Path.cwd` see fresh
values without needing to re-import this module.

Consumed by:

* :func:`popolaloom.evolution.skill_install.install_skill`
* :func:`popolaloom.evolution.skill_doctor.check_skill_health`
* :func:`popolaloom.evolution.skill_upgrade.upgrade_skill`
* The Typer ``popola skill {install,doctor,upgrade}`` verbs at
  :mod:`popolaloom.cli.skill_cmd`.
* The aggregated ``popola doctor`` health probe at
  :mod:`popolaloom.cli.doctor_cmd`.

The ``popola init`` family (Stage S2 — :mod:`popolaloom.cli.init_cmd`)
predates this registry and uses its own per-IDE path resolvers; both
sets agree on the canonical install paths but operate independently
so that a refactor to the install verbs cannot accidentally break the
init-time scaffolder (and vice versa).
"""


def resolve_target_path(target: str, scope: str) -> Path:
    """Resolve ``(target, scope)`` against :data:`SKILL_TARGETS`.

    Args:
        target: one of ``cursor`` / ``claude`` / ``codex`` / ``copilot``.
        scope: ``global`` or ``project`` (codex supports only ``global``,
            copilot supports only ``project``; passing the unsupported
            scope raises :class:`KeyError`).

    Returns:
        Path: absolute path to that target's ``SKILL.md``.

    Raises:
        KeyError: when ``target`` is unknown OR the ``(target, scope)``
            pair is not in the registry.  Per workspace rule "No Silent
            Failures" the caller must handle the exception explicitly
            rather than getting a fallback path.
    """
    if target not in SKILL_TARGETS:
        valid = ", ".join(sorted(SKILL_TARGETS))
        raise KeyError(
            f"unknown skill target {target!r}; valid targets: {valid}"
        )
    scope_table = SKILL_TARGETS[target]
    if scope not in scope_table:
        valid_scopes = ", ".join(sorted(scope_table))
        raise KeyError(
            f"target {target!r} does not support scope {scope!r}; "
            f"valid scopes for this target: {valid_scopes}"
        )
    return scope_table[scope]()


def supported_scopes(target: str) -> list[str]:
    """Return the list of scopes registered for ``target`` (sorted alphabetically)."""
    if target not in SKILL_TARGETS:
        return []
    return sorted(SKILL_TARGETS[target])


@dataclass(frozen=True)
class SkillCheckResult:
    """Result of :func:`check_skill_present` (immutable + serialisable).

    Attributes:
        present:    ``True`` iff at least one skill location was found.
        cursor_skill_path: Path checked for cursor (always set, even
            when ``present=False`` so callers can log "checked X").
        claude_skill_path: Path checked for claude (same as above).
        found_paths: list of Paths that were actually present (may be
            empty on a miss, may have ≥ 1 entries on a hit).
    """

    present: bool
    cursor_skill_path: Path
    claude_skill_path: Path
    found_paths: list[Path]


def _home_path() -> Path:
    """Resolve the user home directory respecting ``$HOME`` env override.

    Tests set ``$HOME`` via ``monkeypatch.setenv`` to point at a tmp
    directory; in production this is the daemon user's home.  Pulled
    out into a helper so tests can mock it without monkey-patching
    :class:`pathlib.Path.home` (which has quirky platform behavior).
    """
    home = os.environ.get("HOME")
    if home:
        return Path(home).expanduser()
    return Path.home()


def check_skill_present(home: Path | None = None) -> SkillCheckResult:
    """Return whether the devola-flow skill is installed for cursor / claude.

    Args:
        home: optional override for the user home directory; defaults
            to ``$HOME`` env or :func:`Path.home`.  Tests pass a tmp
            path directly to keep the check hermetic.

    Returns:
        SkillCheckResult: ``present`` is True if **either** location
        contains ``SKILL.md``.
    """
    base = home if home is not None else _home_path()
    cursor_path = base.joinpath(*CURSOR_SKILL_PATH)
    claude_path = base.joinpath(*CLAUDE_SKILL_PATH)
    found = [p for p in (cursor_path, claude_path) if p.is_file()]
    if not found:
        logger.warning(
            "skill.missing: devola-flow SKILL.md not found at %s OR %s; "
            "L3 dispatch will degrade to prompt-prefix-only mode",
            cursor_path,
            claude_path,
        )
    return SkillCheckResult(
        present=bool(found),
        cursor_skill_path=cursor_path,
        claude_skill_path=claude_path,
        found_paths=found,
    )


def prepend_workflow_context(
    prompt: str,
    *,
    round_num: int,
    max_rounds: int = 5,
    prior_nines: float = 0.0,
    reinforcement: str = "",
    gate_threshold: float = 0.85,
    plan_id: str | None = None,
) -> str:
    """Prepend a Workflow Context section to ``prompt`` for round ``N`` dispatch.

    The output structure is::

        ## Workflow Context (devola-flow)
        round_num: N
        max_rounds: M
        prior_nines: 0.xy
        gate_threshold: 0.85
        reinforcement_rules:
          - (rendered by reinforcement.py)

        ---

        <reinforcement section if non-empty>

        ---

        <original prompt>

    Args:
        prompt: original user prompt (passed to the L3 CLI).
        round_num: ≥ 1; current round number.
        max_rounds: total round budget; default 5 per
            :data:`popolaloom.evolution.WorkflowContext`.
        prior_nines: composite from previous round (0..1).
        reinforcement: optional pre-rendered Markdown reinforcement
            section; when empty, no reinforcement block is appended.
        gate_threshold: inner gate floor; default 0.85.
        plan_id: optional plan correlation id.

    Returns:
        str: ``prompt`` with the Workflow Context (and optional
        reinforcement) section prepended.

    Raises:
        ValueError: when :class:`WorkflowContext` validation fails
            (e.g. ``round_num > max_rounds``).
    """
    rules_list: list[str] = []
    if reinforcement.strip():
        for line in reinforcement.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                rules_list.append(stripped[2:].strip())

    rules_list = rules_list[:5]

    ctx = WorkflowContext(
        round_num=round_num,
        max_rounds=max_rounds,
        prior_nines=max(0.0, min(1.0, prior_nines)),
        reinforcement_rules=rules_list,
        gate_threshold=max(0.0, min(1.0, gate_threshold)),
        plan_id=plan_id,
    )

    sections: list[str] = [ctx.render().rstrip()]

    if reinforcement.strip():
        if not reinforcement.lstrip().startswith("##"):
            sections.append("\n---\n\n## Reinforcement\n" + reinforcement.rstrip())
        else:
            sections.append("\n---\n\n" + reinforcement.rstrip())

    sections.append("\n---\n\n" + prompt.rstrip())

    return "\n".join(sections) + "\n"


def emit_skill_check_event(
    *,
    event_log: Any,
    round_num: int,
    result: SkillCheckResult,
) -> None:
    """Append ``skill.checked`` (and ``skill.missing`` if applicable) events.

    Best-effort writer: when ``event_log`` is None or the append raises,
    we log + continue (the dispatcher should never fail just because
    the audit trail couldn't be written).

    Args:
        event_log: anything with an ``append(type_, data)`` method
            (typically :class:`popolaloom.daemon.event_log.EventLog`);
            ``None`` becomes a no-op.
        round_num: current round number (≥ 1).
        result: the :class:`SkillCheckResult` from
            :func:`check_skill_present`.
    """
    if event_log is None:
        return
    try:
        event_log.append(
            "skill.checked",
            {
                "round_num": round_num,
                "skill_present": result.present,
                "cursor_path": str(result.cursor_skill_path),
                "claude_path": str(result.claude_skill_path),
                "found": [str(p) for p in result.found_paths],
            },
        )
        if not result.present:
            event_log.append(
                "skill.missing",
                {
                    "round_num": round_num,
                    "checked_paths": [
                        str(result.cursor_skill_path),
                        str(result.claude_skill_path),
                    ],
                    "degraded_to": "prompt-prefix-only",
                },
            )
    except Exception:
        logger.exception("emit_skill_check_event: append() failed; ignoring")


__all__ = [
    "CLAUDE_SKILL_PATH",
    "CURSOR_SKILL_PATH",
    "SKILL_TARGETS",
    "SkillCheckResult",
    "check_skill_present",
    "emit_skill_check_event",
    "prepend_workflow_context",
    "resolve_target_path",
    "supported_scopes",
]
