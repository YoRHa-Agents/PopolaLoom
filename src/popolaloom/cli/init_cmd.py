"""``popola init`` subcommand group — install PopolaLoom skill into IDEs.

Stage S2 of v0.5.0 (closes the v0.4.1 → v0.5.0 plan §4 Stage S2).
Mirrors DevolaFlow's ``devola-init`` 14-row verb + 8-modifier flag
matrix (per Q5-2 lock); the reference implementation lives at
``/root/miniforge/lib/python3.12/site-packages/devolaflow/init_project.py``
(read-only).

v0.9.0 W2.4 (Q-D-4 偏离默认: 必做): adds ``popola init --target=cloud-only``
which scaffolds a project skeleton optimised for **Cursor Cloud Agent
dispatch** (no local CLI runtime, no HITL local components). The new
mode is mutually exclusive with the verb subcommands; ``--target=full``
(or no ``--target``) preserves the existing default behaviour byte-for-byte.
The cloud-only scaffold drops three files at the project root:
``popolad.toml`` (cloud-only sections), ``.env.example``
(``CURSOR_API_KEY`` placeholder), and ``Makefile`` (cloud-flow
shortcuts: ``make dispatch``, ``make status``, ``make relay``).

v0.5.5 (Loop 5 of v0.5.x → v0.6.0 self-improvement) adds an
``--interactive`` flag to the root ``popola init`` callback for
human-driven setup: when set, walks the operator through a wizard
(detect IDEs → confirm install per IDE → choose scope → confirm
plan → execute) using :func:`typer.confirm` + :func:`typer.prompt`
for I/O. The flag is mutually-deferential with the verb subcommands
(``init`` with no verb + ``--interactive`` enters the wizard; verbs
take their flags as before).

Verb matrix (8 rows, mirrored from the locked plan §4):

+----+--------------------------------+------------------------------------+
| #  | Invocation                     | Behaviour                          |
+====+================================+====================================+
| 1  | ``popola init`` (no args)      | ``_auto_detect`` + dispatch each   |
+----+--------------------------------+------------------------------------+
| 2  | ``popola init cursor``         | install Cursor target              |
+----+--------------------------------+------------------------------------+
| 3  | ``popola init claude``         | install Claude Code target         |
+----+--------------------------------+------------------------------------+
| 4  | ``popola init copilot``        | install Copilot single-file target |
+----+--------------------------------+------------------------------------+
| 5  | ``popola init codex``          | install Codex target               |
+----+--------------------------------+------------------------------------+
| 6  | ``popola init local``          | scaffold .local/ workspace         |
+----+--------------------------------+------------------------------------+
| 7  | ``popola init all``            | every entry except ``local``       |
+----+--------------------------------+------------------------------------+
| 8  | ``popola init --list``         | print detected tools + exit        |
+----+--------------------------------+------------------------------------+

Modifier matrix (8 flags):

* ``--global`` / ``--project`` — install scope (default project except
  copilot which is project-only).
* ``--no-compile`` — skip rule compile after ``local`` scaffold (Stage
  S2 has no rule compile yet so the flag is accepted + recorded but
  is a no-op; the .rules/ compile chain is deferred to v0.6.0 per the
  plan §S2 risk note).
* ``--with-examples`` / ``--no-with-examples`` — seed example tasks
  during ``local`` scaffold.
* ``--mode={core,standard,full}`` — shorthand: ``core`` ⇒
  ``--no-compile --no-with-examples``; ``full`` ⇒ ``--with-examples``
  + compile-on; ``standard`` ⇒ defaults.  Individual flags override
  ``--mode`` (explicit-beats-implicit, mirrors DevolaFlow §165-191).
* ``--dry-run`` — print every path that *would* be written without
  touching the filesystem.

Idempotency contract: every install verb gates writes by
``Path.exists()``; second runs print ``SKIP <path> (already
installed)`` instead of overwriting.  Tests ``test_init_cmd.py``
verify the contract end-to-end with ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from popolaloom.cli._skill_source import resolve_skill_source

__all__ = ["InitTarget", "app"]


class InitTarget(StrEnum):
    """``--target`` selector for :func:`init_callback` (v0.9.0 W2.4).

    * :attr:`FULL` (default) — keep the existing 14-row verb + 8-modifier
      matrix (auto-detect IDEs, scaffold ``.local/``, etc.).  No behaviour
      change vs v0.8.x; the option is accepted explicitly for symmetry
      with ``--target=cloud-only`` + scripted invocations.
    * :attr:`CLOUD_ONLY` (Q-D-4 偏离默认: 必做) — scaffold a minimal
      cloud-dispatch-only project skeleton (``popolad.toml`` with cloud
      sections, ``.env.example`` with ``CURSOR_API_KEY`` placeholder,
      ``Makefile`` with cloud-flow shortcuts).  No local CLI shims, no
      local HITL stubs — every code path the scaffold encourages routes
      through ``popola dispatch --cli=cursor-cloud``.

    The wire format uses kebab-case (``cloud-only``) per the workspace
    CLI convention; Typer's :class:`Enum` auto-validation surfaces an
    explicit error on unknown values (exit code 2) instead of silently
    falling back to ``full`` (No Silent Failures).

    Inherits from :class:`enum.StrEnum` (Python 3.11+) so ``InitTarget.FULL``
    interpolates as ``"full"`` in CLI help text and Typer's choice list,
    while still allowing :attr:`InitTarget.FULL.value` lookups in tests.
    The same pattern is used by :class:`popolaloom.daemon.state.TaskState`
    and ruff UP042 (no ``str, Enum`` MRO).
    """

    FULL = "full"
    CLOUD_ONLY = "cloud-only"


app = typer.Typer(
    name="init",
    help=(
        "Install + register the PopolaLoom skill into IDEs and scaffold the "
        ".local/ workspace.\n\n"
        "8 verb forms (mirrors DevolaFlow per Q5-2 lock):\n"
        "  popola init                — auto-detect targets from cwd\n"
        "  popola init cursor         — install Cursor target\n"
        "  popola init claude         — install Claude Code target\n"
        "  popola init copilot        — install Copilot target (project only)\n"
        "  popola init codex          — install Codex target ($CODEX_HOME)\n"
        "  popola init local          — scaffold .local/ workspace\n"
        "  popola init all            — every target except local\n"
        "  popola init --list         — print detected tools + exit\n"
    ),
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)


_console_out = Console()


VALID_MODES: frozenset[str] = frozenset({"core", "standard", "full"})

VALID_TARGETS: frozenset[str] = frozenset(
    {"cursor", "claude", "copilot", "codex", "local"}
)


# ── path resolvers ────────────────────────────────────────────────────────


def cursor_target_path(scope: str, cwd: Path | None = None) -> Path:
    """Resolve the Cursor SKILL.md install path for ``scope``.

    Args:
        scope: ``"global"`` → ``~/.cursor/skills/popola-loom/SKILL.md``;
            ``"project"`` (default) → ``<cwd>/.cursor/skills/popola-loom/SKILL.md``.
        cwd: project root (defaults to :func:`Path.cwd`).
    """
    base_dir = Path.home() / ".cursor" if scope == "global" else (cwd or Path.cwd()) / ".cursor"
    return base_dir / "skills" / "popola-loom" / "SKILL.md"


def claude_target_path(scope: str, cwd: Path | None = None) -> Path:
    """Resolve the Claude Code SKILL.md install path for ``scope``."""
    base_dir = Path.home() / ".claude" if scope == "global" else (cwd or Path.cwd()) / ".claude"
    return base_dir / "skills" / "popola-loom" / "SKILL.md"


def copilot_target_path(cwd: Path | None = None) -> Path:
    """Resolve the GitHub Copilot single-file install path.

    Copilot is *always* project-local: ``<cwd>/.github/copilot-instructions.md``.
    The ``--global`` modifier prints a warning + falls back to project
    (mirrors DevolaFlow `init_project.py:222-226`).
    """
    return (cwd or Path.cwd()) / ".github" / "copilot-instructions.md"


def codex_target_path() -> Path:
    """Resolve the Codex SKILL.md install path.

    Honours ``$CODEX_HOME`` per the Codex CLI convention; falls back to
    ``~/.codex/`` when unset.
    """
    import os

    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home / "skills" / "popola-loom" / "SKILL.md"


# ── auto-detect ──────────────────────────────────────────────────────────


def _auto_detect(cwd: Path) -> list[str]:
    """Return the list of targets to install based on filesystem clues.

    Mirrors DevolaFlow `init_project.py:707-727`:

    * ``.cursor/`` exists ⇒ ``cursor``
    * ``.claude/`` exists ⇒ ``claude``
    * ``.github/`` exists ⇒ ``copilot``
    * ``~/.codex/`` exists ⇒ ``codex``
    * ``.local/`` is absent ⇒ ``local`` (so a fresh repo gets scaffolded
      on the first ``popola init`` invocation; idempotent thereafter)
    """
    found: list[str] = []
    if (cwd / ".cursor").is_dir():
        found.append("cursor")
    if (cwd / ".claude").is_dir():
        found.append("claude")
    if (cwd / ".github").is_dir():
        found.append("copilot")
    if Path.home().joinpath(".codex").is_dir():
        found.append("codex")
    if not (cwd / ".local").is_dir():
        found.append("local")
    return found


# ── mode resolver ────────────────────────────────────────────────────────


def _resolve_mode(
    mode: str | None,
    *,
    no_compile_flag: bool,
    with_examples_flag: bool | None,
) -> tuple[bool, bool]:
    """Return ``(no_compile, with_examples)`` after applying ``--mode``.

    Individual flags OVERRIDE the mode-derived default
    (explicit-beats-implicit; mirrors DevolaFlow `init_project.py:165-191`).

    * ``--mode=core``     → defaults ``(no_compile=True,  with_examples=False)``
    * ``--mode=standard`` → defaults ``(no_compile=False, with_examples=False)``
    * ``--mode=full``     → defaults ``(no_compile=False, with_examples=True)``
    * ``mode=None``       → defaults ``(no_compile=False, with_examples=False)``

    Args:
        mode: the parsed ``--mode=`` value (``None`` when absent).
        no_compile_flag: ``True`` iff ``--no-compile`` was explicitly
            passed (overrides the mode default).
        with_examples_flag: ``True`` iff ``--with-examples`` was passed,
            ``False`` iff ``--no-with-examples`` was passed, ``None``
            iff neither (use the mode default).

    Raises:
        typer.BadParameter: when ``mode`` is non-``None`` and not in
            :data:`VALID_MODES` (S-5: explicit error state, never a
            silent fallback to ``standard``).
    """
    if mode is not None and mode not in VALID_MODES:
        valid = ", ".join(sorted(VALID_MODES))
        raise typer.BadParameter(f"--mode must be one of {valid} (got {mode!r})")

    if mode == "core":
        default_no_compile, default_with_examples = True, False
    elif mode == "full":
        default_no_compile, default_with_examples = False, True
    elif mode == "standard":
        default_no_compile, default_with_examples = False, False
    else:
        default_no_compile, default_with_examples = False, False

    no_compile = no_compile_flag or default_no_compile
    with_examples = (
        default_with_examples if with_examples_flag is None else with_examples_flag
    )
    return no_compile, with_examples


# ── core install helpers ─────────────────────────────────────────────────


def _write_skill(target: Path, content: str, *, dry_run: bool) -> str:
    """Write ``content`` to ``target`` and return the action taken.

    Returns one of the literal strings ``"OK"`` (wrote new file),
    ``"SKIP"`` (target already exists, no overwrite — idempotent), or
    ``"DRY"`` (``dry_run=True``, no write occurred).  Tests assert on
    the literal so behavioural changes surface as test diffs.
    """
    if dry_run:
        typer.echo(f"  DRY  {target}")
        return "DRY"
    if target.exists():
        typer.echo(f"  SKIP {target} (already installed)")
        return "SKIP"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    typer.echo(f"  OK   {target}")
    return "OK"


def _write_marker(install_dir: Path, *, dry_run: bool) -> None:
    """Write a ``.popola-loom-version`` marker beside the SKILL.md.

    The marker stores the running wheel version so a future ``popola
    doctor`` (Stage S4) can detect drift between the installed skill
    and the live install.
    """
    from popolaloom import __version__

    marker = install_dir / ".popola-loom-version"
    if dry_run:
        typer.echo(f"  DRY  {marker}")
        return
    if marker.exists():
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{__version__}\n", encoding="utf-8")


def _install_target(
    target: str,
    *,
    scope: str,
    cwd: Path,
    dry_run: bool,
) -> str:
    """Install one IDE target; return the literal action (OK/SKIP/DRY)."""
    content, _is_real = resolve_skill_source()

    if target == "cursor":
        path = cursor_target_path(scope, cwd=cwd)
    elif target == "claude":
        path = claude_target_path(scope, cwd=cwd)
    elif target == "copilot":
        if scope == "global":
            typer.echo(
                "  warning: Copilot does not support --global; falling back to --project."
            )
        path = copilot_target_path(cwd=cwd)
    elif target == "codex":
        path = codex_target_path()
    else:
        raise typer.BadParameter(f"unknown install target: {target!r}")

    typer.echo(f"\n  {target.capitalize()} ({scope}) -> {path}")
    action = _write_skill(path, content, dry_run=dry_run)
    if action == "OK":
        _write_marker(path.parent, dry_run=dry_run)
    return action


# ── local scaffold ───────────────────────────────────────────────────────


_LOCAL_README: str = (
    "# .local/ workspace\n\n"
    "Scaffolded by `popola init local` (PopolaLoom v0.5.0+).  Mirrors the\n"
    "DevolaFlow .local/ contract; see the project root README + the\n"
    "v0.5.0 plan for the canonical surface.\n"
)

_TRACKER_MD: str = (
    "# Feedback Tracker\n\n"
    "| ID | Created | Topic | State |\n"
    "|---|---|---|---|\n"
    "<!-- append a new row when you file feedback under .local/feedbacks/ -->\n"
)

_MEMORY_MD: str = (
    "# Memory Index\n\n"
    "Long-lived knowledge captured across PopolaLoom sessions.  Use\n"
    "`.local/memory/specs/` for source-of-truth specs and\n"
    "`.local/memory/research/` for research dossiers.\n"
)

_INDEX_MD: str = (
    "# .local/ workspace index\n\n"
    "Auto-generated directory listing.\n\n"
    "- `.agent/`\n"
    "- `feedbacks/`\n"
    "- `memory/`\n"
    "- `tasks/`\n"
)


def _scaffold_path(
    target: Path,
    *,
    is_dir: bool,
    content: str | None = None,
    dry_run: bool,
) -> str:
    """Create ``target`` (file or dir) idempotently.

    Returns ``"OK"``, ``"SKIP"``, or ``"DRY"`` (matches :func:`_write_skill`).
    """
    if dry_run:
        kind = "dir " if is_dir else "file"
        typer.echo(f"  DRY  {kind} {target}")
        return "DRY"
    if is_dir:
        if target.exists():
            typer.echo(f"  SKIP {target} (already exists)")
            return "SKIP"
        target.mkdir(parents=True, exist_ok=True)
        typer.echo(f"  OK   {target}/")
        return "OK"
    if target.exists():
        typer.echo(f"  SKIP {target} (already exists)")
        return "SKIP"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or "", encoding="utf-8", newline="\n")
    typer.echo(f"  OK   {target}")
    return "OK"


def _install_local(
    cwd: Path,
    *,
    no_compile: bool,
    with_examples: bool,
    dry_run: bool,
) -> None:
    """Scaffold the canonical ``.local/`` workspace surface.

    Creates the 8 canonical paths from the v0.5.0 plan §S2.B; every
    existing path is left untouched (idempotent).  Honours ``--dry-run``
    by printing every path that would be touched without writing.

    The ``no_compile`` flag is recorded but is a no-op for Stage S2: the
    ``.rules/`` compile chain landed in DevolaFlow but is deferred to
    PopolaLoom v0.6.0 per the plan §S2 risk note.  The flag is still
    accepted so CI scripts authored against the DevolaFlow surface keep
    working.
    """
    typer.echo(f"\n  Local workspace -> {cwd / '.local/'}")

    _scaffold_path(cwd / ".local" / "feedbacks", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / "feedbacks" / "TRACKER.md",
        is_dir=False,
        content=_TRACKER_MD,
        dry_run=dry_run,
    )
    _scaffold_path(
        cwd / ".local" / "feedbacks" / "README.md",
        is_dir=False,
        content="# .local/feedbacks/\n\nFile feedback drops here.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / "tasks", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / "tasks" / "README.md",
        is_dir=False,
        content="# .local/tasks/\n\nTask briefs land here.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / "memory", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / "memory" / "MEMORY.md",
        is_dir=False,
        content=_MEMORY_MD,
        dry_run=dry_run,
    )
    _scaffold_path(
        cwd / ".local" / "memory" / "README.md",
        is_dir=False,
        content="# .local/memory/\n\nLong-lived memory store.\n",
        dry_run=dry_run,
    )
    _scaffold_path(
        cwd / ".local" / "index.md",
        is_dir=False,
        content=_INDEX_MD,
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / ".agent" / "active", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / ".agent" / "active" / "README.md",
        is_dir=False,
        content="# .local/.agent/active/\n\nIn-flight change folders.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / ".agent" / "handoff", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / ".agent" / "handoff" / "README.md",
        is_dir=False,
        content="# .local/.agent/handoff/\n\nAppend-only handoff envelopes.\n",
        dry_run=dry_run,
    )
    _scaffold_path(cwd / ".local" / ".agent" / "archive", is_dir=True, dry_run=dry_run)
    _scaffold_path(
        cwd / ".local" / ".agent" / "archive" / "README.md",
        is_dir=False,
        content="# .local/.agent/archive/\n\nClosed change folders.\n",
        dry_run=dry_run,
    )

    if no_compile:
        typer.echo("  SKIP compile (--no-compile flag set; .rules/ compile is v0.6.0)")
    else:
        typer.echo("  NOTE compile chain is deferred to v0.6.0 (plan §S2 risk note)")

    if with_examples:
        _seed_local_examples(cwd, dry_run=dry_run)


_EXAMPLE_TASK_MD: str = (
    "# Example Task: dispatch-cursor-task\n\n"
    "Worked-trace fixture seeded by `popola init local --with-examples`\n"
    "(v0.5.0 PV-S2).  Demonstrates how to delegate a long-running\n"
    "task to a local agent CLI via PopolaLoom.\n\n"
    "## Steps\n\n"
    "1. `popola popolad start` — start the daemon.\n"
    "2. `popola dispatch \"refactor module X\" --cli cursor --cwd .`\n"
    "3. `popola attach <task_id> --follow` to watch live events.\n\n"
    "Delete this file once you author your first real task.\n"
)


def _seed_local_examples(cwd: Path, *, dry_run: bool) -> None:
    """Seed example task files under ``.local/`` (mode=full)."""
    typer.echo(f"\n  Example seed -> {cwd / '.local' / 'tasks' / 'example-dispatch.md'}")
    _scaffold_path(
        cwd / ".local" / "tasks" / "example-dispatch.md",
        is_dir=False,
        content=_EXAMPLE_TASK_MD,
        dry_run=dry_run,
    )


# ── cloud-only scaffold (v0.9.0 W2.4) ────────────────────────────────────


_CLOUD_ONLY_POPOLAD_TOML: str = """\
# popolad.toml — cloud-only PopolaLoom configuration
# Generated by `popola init --target=cloud-only` (v0.9.0+).
#
# This scaffold targets Cursor Cloud Agent dispatch only — no local
# subprocess CLI shims, no local HITL listeners. See:
#   docs/USER_GUIDE.md → "Cloud Agent dispatch (v0.8.5+)"
#   docs/USER_GUIDE.md → "popola init --target=cloud-only (v0.9.0+)"
#
# To add the local-tier surface (subprocess CLIs, IDE skill installs,
# .local/ workspace), re-run with `--target=full` in a separate
# checkout — the two layouts are intentionally disjoint.

[hitl.cloud]
# Cloud-tier HITL bridge — the daemon's POST /hitl/cloud/{request,wait,answer}
# endpoints are gated by this section being present. The local-tier
# [hitl] block from `--target=full` is intentionally absent: cloud-only
# init never registers a local Lark listener nor an MCP stdio worker.
enabled = true
default_timeout_s = 1800
lark_channel = ""

[cloud.backoff]
# Rate-limit backoff applied when Cursor returns 429 quota_exceeded.
# Per quota-config.md §3.2 (v0.8.8+): the daemon emits a single
# cloud.queued_quota_exceeded → cloud.queue_exit pair per sequence.
max_retries = 5
initial_backoff_ms = 1000
max_backoff_ms = 30000
jitter_factor = 0.25

[cloud.busy_strategy]
# 409 agent_busy strategy — see quota-config.md §4.
# mode = "queue" parks the new run on the agent and waits for the
# current run to finish (default for cloud-only). Other modes:
# "fail_fast" (immediately fail with cloud.busy_timeout) and
# "queue_with_deadline" (queue with explicit deadline_ts).
mode = "queue"
queue_max_wait_s = 1800

[cloud.relay]
# popola relay <task_a> defaults — see USER_GUIDE.md §"popola relay".
# preview_only = true means relay prints the dispatch payload and
# exits; auto_dispatch = true means relay POSTs immediately
# (Q-C-4 偏离默认 — opt-in only for cloud-only init).
auto_dispatch = false
preview_only = true
"""


_CLOUD_ONLY_ENV_EXAMPLE: str = """\
# .env.example — cloud-only PopolaLoom environment
# Generated by `popola init --target=cloud-only` (v0.9.0+).
#
# Copy to `.env` (or source from your shell rc) and fill in
# CURSOR_API_KEY before running the cloud-only flow.
#
# Used by:
#   popola dispatch --cli=cursor-cloud --prompt "<task>"
#   popola attach   <task_id> --follow         (SSE auth)
#   popola cloud runs <agent_id>               (multi-run history)

# REQUIRED — Cursor Cloud Agents REST API key.
# Get yours from: https://cursor.com/dashboard → "API Keys".
# Without this, `popola dispatch --cli=cursor-cloud` fails availability
# checks at the adapter layer (No Silent Failures).
CURSOR_API_KEY=

# OPTIONAL — override the events / sockets directory (default: ~/.popola).
#POPOLA_HOME=~/.popola

# OPTIONAL — override the Cursor REST base URL (default: https://api.cursor.com).
#CURSOR_API_BASE=https://api.cursor.com

# OPTIONAL — override the handoff envelope dir
# (default: $POPOLA_HOME/handoff if set, else .local/.agent/handoff).
#POPOLA_HANDOFF_DIR=
"""


_CLOUD_ONLY_MAKEFILE: str = """\
# Makefile — cloud-only PopolaLoom shortcuts.
# Generated by `popola init --target=cloud-only` (v0.9.0+).
#
# These targets wrap `popola dispatch --cli=cursor-cloud` flows so a
# fresh operator can smoke-test cloud dispatch without memorising every
# flag. Override variables on the command line, e.g.:
#
#   make dispatch PROMPT="Plan database migration scaffolding"
#   make status   TASK_ID=cursor-cloud-deadbeef
#   make relay    TASK_ID=cursor-cloud-deadbeef

PROMPT  ?= Describe the cloud task here
CWD     ?= .
TASK_ID ?=

.PHONY: help dispatch status attach relay

help: ## Show this help.
\t@echo "PopolaLoom cloud-only Makefile (v0.9.0+):"
\t@echo "  make dispatch PROMPT=\\"...\\"   — popola dispatch --cli=cursor-cloud"
\t@echo "  make status   TASK_ID=...      — popola status <id>"
\t@echo "  make attach   TASK_ID=...      — popola attach <id> --follow"
\t@echo "  make relay    TASK_ID=...      — popola relay <id>"

dispatch: ## Dispatch a new cloud task via cursor-cloud.
\tpopola dispatch "$(PROMPT)" --cli=cursor-cloud --cwd "$(CWD)"

status: ## Show task status.
\t@if [ -z "$(TASK_ID)" ]; then echo "error: TASK_ID is required" >&2; exit 2; fi
\tpopola status "$(TASK_ID)"

attach: ## Tail task events (SSE-by-default for cloud tasks).
\t@if [ -z "$(TASK_ID)" ]; then echo "error: TASK_ID is required" >&2; exit 2; fi
\tpopola attach "$(TASK_ID)" --follow

relay: ## Relay a completed cloud task into a new run.
\t@if [ -z "$(TASK_ID)" ]; then echo "error: TASK_ID is required" >&2; exit 2; fi
\tpopola relay "$(TASK_ID)"
"""


_CLOUD_ONLY_FILES: tuple[tuple[str, str], ...] = (
    ("popolad.toml", _CLOUD_ONLY_POPOLAD_TOML),
    (".env.example", _CLOUD_ONLY_ENV_EXAMPLE),
    ("Makefile", _CLOUD_ONLY_MAKEFILE),
)
"""Ordered file list emitted by ``popola init --target=cloud-only``.

Tuples are ``(relative_path, content)``; the first entry
(``popolad.toml``) doubles as the "primary marker" for the
already-installed message in :func:`_install_cloud_only` — when it is
present (and ``--force`` is absent) we surface the canonical
"scaffold already exists; use --force to overwrite" line and exit
without modifying any file (preserves the existing init idempotency
contract).
"""


def _write_cloud_file(target: Path, content: str, *, dry_run: bool, force: bool) -> str:
    """Write a single cloud-only scaffold file; return ``OK / SKIP / DRY``.

    Mirrors :func:`_write_skill` but adds the ``force`` parameter so
    ``popola init --target=cloud-only --force`` overwrites pre-existing
    files in-place (the SKILL.md install path never overwrites; cloud
    scaffold has its own override semantics because the files belong to
    the operator's project, not to PopolaLoom's wheel-bundled assets).
    """
    if dry_run:
        typer.echo(f"  DRY  {target}")
        return "DRY"
    if target.exists() and not force:
        typer.echo(f"  SKIP {target} (already exists; use --force to overwrite)")
        return "SKIP"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    typer.echo(f"  OK   {target}")
    return "OK"


def _install_cloud_only(
    cwd: Path,
    *,
    dry_run: bool,
    force: bool,
    configure_cursor_auth: bool = False,
    cursor_api_key: str | None = None,
) -> None:
    """Scaffold the cloud-only project skeleton at ``cwd`` (v0.9.0 W2.4).

    Drops three files at the project root:

    * ``popolad.toml`` — daemon config with cloud-only sections only
      (``[hitl.cloud]`` / ``[cloud.backoff]`` / ``[cloud.busy_strategy]``
      / ``[cloud.relay]``); the local-tier ``[hitl]`` block from
      ``--target=full`` is deliberately omitted.
    * ``.env.example`` — ``CURSOR_API_KEY`` placeholder + comment
      pointing at ``popola dispatch --cli=cursor-cloud`` (the only
      meaningful entrypoint in cloud-only mode).
    * ``Makefile`` — cloud-flow shortcuts (``make dispatch`` /
      ``make status`` / ``make attach`` / ``make relay``).

    Idempotency contract (AC e+f): if ``popolad.toml`` already exists at
    ``cwd`` and ``--force`` was NOT passed, we surface the canonical
    "scaffold already exists; use --force to overwrite" line, then
    delegate to :func:`_write_cloud_file` for each entry — every file
    individually prints ``SKIP <path> (already exists; use --force to
    overwrite)``. With ``--force`` set we write all three files
    unconditionally (including overwriting any existing copy).

    v0.9.2 ``--configure-cursor-auth`` extension: when the flag is set
    AND ``dry_run`` is False, additionally walk the operator through
    :func:`_offer_cursor_credential_setup` after the scaffold is on
    disk. The credential setup itself is gated by an interactive
    ``typer.confirm`` so a non-interactive caller (e.g. a CI test)
    will not hang. ``dry_run`` short-circuits the credential step
    entirely (per the workspace **No Silent Failures** rule for
    secrets — never prompt during dry-run).

    v0.9.5 ``--cursor-api-key`` / ``--cursor-api-key-file`` extension:
    when ``cursor_api_key`` is non-None the helper persists the value
    via :func:`_persist_cursor_api_key_noninteractive` instead of
    prompting (no operator interaction needed). Mutually deferential
    with ``configure_cursor_auth`` — the resolved value wins so we do
    not prompt twice.

    Args:
        cwd: project root to scaffold into (``Path.cwd()`` from the
            top-level callback; tests override via ``monkeypatch.chdir``).
        dry_run: when True, print every path that would be written
            without touching the filesystem.
        force: when True, overwrite pre-existing files; when False,
            existing files print SKIP and the operator's content is
            preserved (mirrors the behaviour of every other init verb).
        configure_cursor_auth: opt-in flag (v0.9.2+); when True, prompt
            the operator to store the Cursor API key in the OS keyring
            after the scaffold is written. Ignored on ``dry_run`` so
            no prompt is shown for previews.
        cursor_api_key: pre-resolved Cursor API key (v0.9.5+) provided
            via ``--cursor-api-key`` or ``--cursor-api-key-file`` on the
            init root. When set, the helper persists it via the
            non-interactive path and never prompts.
    """
    typer.echo("popola init — target: cloud-only")
    typer.echo(
        "  scaffolding cloud-only project skeleton "
        "(no local CLI shims, no local HITL stubs)"
    )
    typer.echo(f"  cwd: {cwd}")

    primary_marker = cwd / _CLOUD_ONLY_FILES[0][0]
    if primary_marker.exists() and not force and not dry_run:
        typer.echo(
            f"  scaffold already exists at {primary_marker}; "
            "use --force to overwrite"
        )

    for relative_path, content in _CLOUD_ONLY_FILES:
        target = cwd / relative_path
        _write_cloud_file(target, content, dry_run=dry_run, force=force)

    if dry_run:
        if cursor_api_key is not None or configure_cursor_auth:
            typer.echo(_DRY_RUN_CREDENTIAL_SKIP_MSG)
        return

    typer.echo(
        "\nNext steps:\n"
        "  1. cp .env.example .env && edit .env to set CURSOR_API_KEY\n"
        "  2. popola popolad start          (boot the daemon)\n"
        "  3. make dispatch PROMPT=\"...\"    "
        "(or: popola dispatch ... --cli=cursor-cloud)"
    )

    if cursor_api_key is not None:
        _persist_cursor_api_key_noninteractive(cursor_api_key)
    elif configure_cursor_auth:
        _offer_cursor_credential_setup()


# ── credential setup helpers (v0.9.2+ / v0.9.5+) ────────────────────────


_DRY_RUN_CREDENTIAL_SKIP_MSG: str = (
    "\n  credential setup skipped during dry-run preview "
    "(--dry-run is set; secret persistence requires a real install)"
)
"""Operator-facing one-liner emitted when ``--dry-run`` is paired with any
of ``--configure-cursor-auth`` / ``--cursor-api-key`` / ``--cursor-api-key-file``.

Per the workspace **No Silent Failures** rule and v0.9.2 Secret-Handling
Invariants, we never prompt for or persist a secret during a dry-run
preview. The skip message is explicit so operators see exactly why the
credential step was elided. The literal is reused across the cloud-only,
interactive, auto-detect, and per-verb subcommand paths so a future
edit changes a single source of truth.
"""


def _resolve_cursor_api_key_input(
    *,
    value: str | None,
    file: Path | None,
) -> str | None:
    """Resolve the v0.9.5 init-time Cursor API key intake.

    Returns the resolved (stripped) key string when one of the inputs is
    provided, ``None`` when both are unset, and raises
    :class:`typer.BadParameter` when the inputs are mutually exclusive,
    when the inline value is empty/whitespace-only, or when the file is
    missing or empty (per the workspace **No Silent Failures** rule —
    never silently treat a malformed flag as "operator did not pass it").

    Args:
        value: literal value of ``--cursor-api-key`` (already stripped
            once by Typer's option parsing); ``None`` when the flag was
            absent. Whitespace-only is rejected.
        file: ``Path`` from ``--cursor-api-key-file``. The file is read
            with ``encoding="utf-8"``; the first non-empty line (after
            ``str.strip()``) is treated as the key. Missing or empty
            files raise :class:`typer.BadParameter`.

    Raises:
        typer.BadParameter: any of the conditions above.
    """
    if value is not None and file is not None:
        raise typer.BadParameter(
            "--cursor-api-key and --cursor-api-key-file are mutually exclusive; "
            "pass only one"
        )
    if value is not None:
        stripped = value.strip()
        if not stripped:
            raise typer.BadParameter(
                "--cursor-api-key value must not be empty or whitespace-only"
            )
        return stripped
    if file is not None:
        try:
            text = file.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise typer.BadParameter(
                f"--cursor-api-key-file path not found: {file}"
            ) from exc
        except OSError as exc:
            raise typer.BadParameter(
                f"--cursor-api-key-file could not be read: {file} ({exc})"
            ) from exc
        first_line: str | None = None
        for raw_line in text.splitlines():
            stripped_line = raw_line.strip()
            if stripped_line:
                first_line = stripped_line
                break
        if not first_line:
            raise typer.BadParameter(
                f"--cursor-api-key-file is empty or contains only whitespace: {file}"
            )
        return first_line
    return None


def _persist_cursor_api_key_noninteractive(raw_key: str) -> None:
    """Persist ``raw_key`` in the OS keyring without prompting (v0.9.5+).

    Used by the init-time non-interactive intake (``--cursor-api-key`` /
    ``--cursor-api-key-file``). Best-effort when the optional keyring
    extra is missing: prints an actionable hint pointing at the
    ``credentials`` extra and the env-var fallback, then returns
    without exiting non-zero — the install path itself succeeded; only
    the secret persistence is degraded.

    The literal key value is never echoed; the printed line carries
    only the backend label and a 12-hex-char fingerprint so operators
    can confirm the round-trip without leaking entropy.

    Args:
        raw_key: the resolved key string (already stripped by
            :func:`_resolve_cursor_api_key_input` or by the cloud-only
            init path).
    """
    from popolaloom.credentials import (
        CredentialBackendError,
        compute_fingerprint,
        is_keyring_available,
        store_cursor_api_key,
    )

    typer.echo("\nSecure Cursor API key storage (v0.9.5 init-time intake):")
    if not is_keyring_available():
        typer.echo(
            "  WARN: OS keyring backend unavailable; the install path "
            "succeeded but credential storage was skipped.",
            err=True,
        )
        typer.echo(
            "        Install the optional extra to enable secure storage:",
            err=True,
        )
        typer.echo(
            "          `pip install popolaloom[credentials]`",
            err=True,
        )
        typer.echo(
            "        Until then, set CURSOR_API_KEY in your shell or "
            ".env file (the env var is the documented fallback).",
            err=True,
        )
        return

    try:
        status = store_cursor_api_key(raw_key)
    except CredentialBackendError as exc:
        typer.echo(
            f"  ERROR: keyring store failed: {exc}",
            err=True,
        )
        typer.echo(
            "  Falling back to env var path: set CURSOR_API_KEY in your shell.",
            err=True,
        )
        return
    except ValueError as exc:
        typer.echo(
            f"  ERROR: invalid api key value: {exc}",
            err=True,
        )
        return

    fp = compute_fingerprint(raw_key) or "(unknown)"
    typer.echo(
        f"  Stored Cursor API key. backend={status.backend_name}  fingerprint={fp}"
    )


def _handle_credential_intake_after_install(
    *,
    resolved_key: str | None,
    configure_cursor_auth: bool,
    dry_run: bool,
) -> None:
    """Run the right credential helper after install completes (v0.9.5+).

    Branch table:

    * ``resolved_key is None`` AND ``configure_cursor_auth is False``
      → no-op (operator did not opt in).
    * ``dry_run is True`` AND any of the credential intake flags set
      → print the canonical skip message and return; never prompt or
      persist (per **No Silent Failures** dry-run rule for secrets).
    * ``resolved_key is not None`` → persist via the non-interactive
      helper (no prompting; the value was already collected from
      ``--cursor-api-key`` or ``--cursor-api-key-file``).
    * Otherwise (``configure_cursor_auth is True`` AND
      ``resolved_key is None``) → walk the operator through the
      existing :func:`_offer_cursor_credential_setup` interactive
      prompt path.
    """
    if resolved_key is None and not configure_cursor_auth:
        return
    if dry_run:
        typer.echo(_DRY_RUN_CREDENTIAL_SKIP_MSG)
        return
    if resolved_key is not None:
        _persist_cursor_api_key_noninteractive(resolved_key)
        return
    _offer_cursor_credential_setup()


def _offer_cursor_credential_setup() -> None:
    """Walk the operator through `popola auth cursor set` interactively.

    Invoked from:

    * ``popola init --target=cloud-only --configure-cursor-auth`` (after
      the scaffold is on disk).
    * The interactive wizard's optional credential step.

    Skips silently when the keyring extra is unavailable so the operator
    sees an actionable hint rather than a hard failure (the scaffold
    is valuable even without the keyring backend — they can fall back
    to ``CURSOR_API_KEY`` in ``.env``).

    Per the v0.9.2 plan §"Secret Handling Invariants", the prompt uses
    :func:`typer.prompt(hide_input=True)` so the typed key never
    re-echoes; the stored fingerprint is printed afterwards so the
    operator can confirm the round-trip.
    """
    from popolaloom.credentials import (
        CredentialBackendError,
        compute_fingerprint,
        is_keyring_available,
        store_cursor_api_key,
    )

    typer.echo("\nSecure Cursor API key storage (v0.9.2+):")
    if not is_keyring_available():
        typer.echo(
            "  WARN: OS keyring backend unavailable; install the optional "
            "extra to enable secure storage:"
        )
        typer.echo("        `pip install popolaloom[credentials]`")
        typer.echo(
            "        Until then, set CURSOR_API_KEY in your shell or "
            ".env file (the env var is the documented fallback)."
        )
        return

    if not typer.confirm(
        "  Store a Cursor API key in the OS keyring now?",
        default=False,
    ):
        typer.echo("  Skipped. You can run `popola auth cursor set` later.")
        return

    raw = typer.prompt(
        "  Cursor API key (input hidden; stored in OS keyring only)",
        hide_input=True,
        confirmation_prompt=False,
    )
    raw = (raw or "").strip()
    if not raw:
        typer.echo("  Empty input; skipping. Run `popola auth cursor set` to retry.")
        return

    try:
        status = store_cursor_api_key(raw)
    except CredentialBackendError as exc:
        typer.echo(f"  ERROR: {exc}")
        typer.echo(
            "  Falling back to env var path: set CURSOR_API_KEY in your shell."
        )
        return
    except ValueError as exc:
        typer.echo(f"  ERROR: {exc}")
        return

    fp = compute_fingerprint(raw) or "(unknown)"
    typer.echo(
        f"  Stored. backend={status.backend_name}  fingerprint={fp}"
    )
    typer.echo(
        "  Next dispatches will resolve via OS keyring (precedence: "
        "CURSOR_API_KEY env > keyring)."
    )


# ── --list ───────────────────────────────────────────────────────────────


def _print_list(cwd: Path) -> None:
    """Print the detected tools / scope / agent_dir / SKILL.md status."""
    detected = _auto_detect(cwd)
    content, is_real = resolve_skill_source()

    table = Table(title="popola init — detected targets", show_header=True, header_style="bold")
    table.add_column("target")
    table.add_column("scope")
    table.add_column("install path")
    table.add_column("present?")

    for target in ("cursor", "claude", "copilot", "codex"):
        if target == "cursor":
            path = cursor_target_path("project", cwd=cwd)
        elif target == "claude":
            path = claude_target_path("project", cwd=cwd)
        elif target == "copilot":
            path = copilot_target_path(cwd=cwd)
        else:
            path = codex_target_path()
        scope = "project" if target != "codex" else "global"
        table.add_row(
            target,
            scope,
            str(path),
            "yes" if path.exists() else "no",
        )
    table.add_row(
        "local",
        "project",
        str(cwd / ".local"),
        "yes" if (cwd / ".local").is_dir() else "no",
    )
    _console_out.print(table)

    typer.echo(f"\nDetected by auto-detect: {', '.join(detected) if detected else '(none)'}")
    typer.echo(f"Skill source: {'wheel-bundled (S3+)' if is_real else 'placeholder stub (S2)'}")
    typer.echo(f"Skill bytes:  {len(content)}")


# ── Typer wiring ─────────────────────────────────────────────────────────


@app.callback()
def init_callback(
    ctx: typer.Context,
    list_only: bool = typer.Option(
        False,
        "--list",
        help="Print detected tools + install paths + skill source state and exit.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print every path that would be written without touching disk.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help=(
            "Walk through an interactive setup wizard (detect IDEs → confirm "
            "per-IDE install → choose scope → confirm plan → execute). "
            "Other modifiers (mode, target, --list, --dry-run when combined) "
            "are ignored once --interactive is set; the wizard collects them "
            "from the operator instead."
        ),
    ),
    target: InitTarget = typer.Option(  # noqa: B008
        InitTarget.FULL,
        "--target",
        case_sensitive=False,
        help=(
            "v0.9.0 W2.4 (Q-D-4 偏离默认: 必做): scaffold profile selector. "
            "'full' (default) keeps the existing 14-row verb + 8-modifier "
            "matrix (auto-detect IDEs, scaffold .local/, etc.). 'cloud-only' "
            "drops a minimal cloud-dispatch-only skeleton: popolad.toml "
            "(cloud sections), .env.example (CURSOR_API_KEY), Makefile "
            "(make dispatch / status / relay). Unknown values exit 2."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite pre-existing scaffold files when --target=cloud-only "
            "is set. Without --force, second invocations print SKIP per file "
            "and preserve any operator edits (default; mirrors every other "
            "init verb's idempotency contract). The flag is currently a "
            "no-op for --target=full (existing verbs already preserve user "
            "edits via SKIP — pass --force only if cloud-only mode misfired "
            "the first time)."
        ),
    ),
    configure_cursor_auth: bool = typer.Option(
        False,
        "--configure-cursor-auth",
        help=(
            "v0.9.2+: prompt to securely store a Cursor API key in the OS "
            "keyring after the install completes. v0.9.5+ accepts the flag "
            "on every init path (auto-detect, verb subcommand, "
            "--target=cloud-only, --interactive). Hidden-input prompt; "
            "the literal value is never echoed. No-op when --dry-run is "
            "set (No Silent Failures: never prompt for secrets during "
            "a preview)."
        ),
    ),
    cursor_api_key: str | None = typer.Option(
        None,
        "--cursor-api-key",
        help=(
            "v0.9.5+: non-interactive Cursor API key intake. The literal "
            "value is forwarded to the OS keyring via "
            "popolaloom.credentials.store_cursor_api_key. Implies "
            "--configure-cursor-auth on every init path (auto-detect, "
            "verb subcommand, --target=cloud-only, --interactive). "
            "Mutually exclusive with --cursor-api-key-file. Empty / "
            "whitespace-only values are rejected."
        ),
    ),
    cursor_api_key_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--cursor-api-key-file",
        help=(
            "v0.9.5+: read the Cursor API key from the first non-empty "
            "line of PATH (utf-8) and persist via --cursor-api-key's path. "
            "Mutually exclusive with --cursor-api-key. Missing or empty "
            "files are rejected (No Silent Failures)."
        ),
    ),
) -> None:
    """Top-level callback: handle ``--target``, ``--list``, ``--interactive``, and auto-detect.

    v0.9.5 (closes ``.local/feedbacks/feedback_for_v0.9.4.md``): adds two
    new credential-intake options on the root callback —
    ``--cursor-api-key VAL`` and ``--cursor-api-key-file PATH`` — that
    forward the resolved value to
    :func:`popolaloom.credentials.store_cursor_api_key` so operators do
    not have to re-enter the key on subsequent dispatches. Either flag
    implies ``--configure-cursor-auth`` (no need to pass both); the
    flags compose with every init path (auto-detect, verb subcommand,
    ``--target=cloud-only``, ``--interactive``). ``--dry-run`` skips
    persistence with a clear one-line message (per the workspace
    **No Silent Failures** rule for secrets — never prompt or persist
    during a preview).
    """
    cwd = Path.cwd()

    resolved_cursor_api_key = _resolve_cursor_api_key_input(
        value=cursor_api_key,
        file=cursor_api_key_file,
    )
    effective_configure_cursor_auth = (
        configure_cursor_auth or resolved_cursor_api_key is not None
    )

    if target is InitTarget.CLOUD_ONLY:
        if ctx.invoked_subcommand is not None:
            raise typer.BadParameter(
                "--target=cloud-only cannot be combined with a verb subcommand"
            )
        if list_only:
            raise typer.BadParameter(
                "--target=cloud-only cannot be combined with --list"
            )
        if interactive:
            raise typer.BadParameter(
                "--target=cloud-only cannot be combined with --interactive"
            )
        _install_cloud_only(
            cwd,
            dry_run=dry_run,
            force=force,
            configure_cursor_auth=effective_configure_cursor_auth,
            cursor_api_key=resolved_cursor_api_key,
        )
        raise typer.Exit(code=0)

    if interactive:
        if ctx.invoked_subcommand is not None:
            raise typer.BadParameter(
                "--interactive cannot be combined with a verb subcommand"
            )
        # v0.9.5: when a non-interactive value was supplied, persist it
        # immediately so the credential reaches the keyring even if the
        # operator declines every install in the wizard's "Nothing
        # selected" early-return branch. The wizard's own credential
        # prompt is suppressed in this case (the value is already
        # collected) by passing ``configure_cursor_auth=False``.
        if resolved_cursor_api_key is not None:
            _handle_credential_intake_after_install(
                resolved_key=resolved_cursor_api_key,
                configure_cursor_auth=True,
                dry_run=dry_run,
            )
        _run_interactive_wizard(
            cwd,
            configure_cursor_auth=(
                configure_cursor_auth and resolved_cursor_api_key is None
            ),
            cursor_api_key=None,
            dry_run=dry_run,
        )
        raise typer.Exit(code=0)

    if list_only:
        if ctx.invoked_subcommand is not None:
            raise typer.BadParameter(
                "--list cannot be combined with a verb subcommand"
            )
        _print_list(cwd)
        raise typer.Exit(code=0)

    if ctx.invoked_subcommand is not None:
        # A per-verb subcommand (cursor / claude / copilot / codex /
        # local / all) will run after this callback returns. Defer the
        # credential helper to a click ``ctx.call_on_close`` hook so it
        # fires AFTER the verb body completes (per the v0.9.5 contract:
        # "after the verb installs, run the credential helper").
        if effective_configure_cursor_auth:

            def _after_subcommand_credential_helper() -> None:
                _handle_credential_intake_after_install(
                    resolved_key=resolved_cursor_api_key,
                    configure_cursor_auth=effective_configure_cursor_auth,
                    dry_run=dry_run,
                )

            ctx.call_on_close(_after_subcommand_credential_helper)
        return

    typer.echo("popola init — target: full (default)")

    detected = _auto_detect(cwd)
    if not detected:
        typer.echo("  No AI tools detected. Installing for Cursor (most common).")
        detected = ["cursor"]

    typer.echo(f"  popola init — auto-detected targets: {', '.join(detected)}")
    for verb in detected:
        if verb == "local":
            _install_local(cwd, no_compile=False, with_examples=False, dry_run=dry_run)
        elif verb in {"cursor", "claude", "copilot", "codex"}:
            _install_target(verb, scope="project", cwd=cwd, dry_run=dry_run)

    _handle_credential_intake_after_install(
        resolved_key=resolved_cursor_api_key,
        configure_cursor_auth=effective_configure_cursor_auth,
        dry_run=dry_run,
    )


# ── interactive wizard ──────────────────────────────────────────────────


_IDE_TARGETS_FOR_WIZARD: tuple[str, ...] = ("cursor", "claude", "copilot", "codex")
"""Order in which the wizard prompts about IDE targets.

The order matches the table printed by ``popola init --list`` so the
wizard's UX feels consistent with the discovery surface. ``local`` is
prompted last (after the IDE round) because scaffolding the
``.local/`` workspace is a project-shape decision rather than an IDE
integration.
"""


def _run_interactive_wizard(
    cwd: Path,
    *,
    configure_cursor_auth: bool = False,
    cursor_api_key: str | None = None,
    dry_run: bool = False,
) -> None:
    """Walk the operator through an interactive setup.

    Steps (per the v0.5.5 L5.B contract):

    1. Detect IDEs in ``cwd`` and ``$HOME``.
    2. For each IDE target (cursor / claude / copilot / codex):
       - Prompt: "Install for <IDE>? [Y/n]" — default Yes when the
         IDE was auto-detected, default No otherwise.
       - When Yes (and the IDE supports both scopes), prompt:
         "Global or project-local? [G/p]" — default Project when the
         project marker exists, default Global otherwise.  Copilot
         skips this prompt (project-only by design).
    3. Prompt: "Scaffold .local/ workspace? [Y/n]" — default Yes when
       the directory is missing.
    4. Show the install plan and prompt: "Proceed? [Y/n]".
    5. When confirmed, dispatch each chosen install verb.
    6. v0.9.2+: when ``configure_cursor_auth`` is True (operator passed
       ``--configure-cursor-auth``), invite the operator to securely
       store a Cursor API key in the OS keyring after every other step.
    7. v0.9.5+: when ``cursor_api_key`` is non-None (operator passed
       ``--cursor-api-key`` or ``--cursor-api-key-file`` on the init
       root), persist the value via the non-interactive path instead
       of prompting; the interactive credential prompt is skipped to
       avoid asking for a value the operator already supplied. ``dry_run``
       short-circuits credential persistence with a clear message
       (per the No Silent Failures dry-run rule for secrets).

    All I/O goes through :func:`typer.confirm` and :func:`typer.prompt`;
    tests inject stdin via ``CliRunner.invoke(..., input="...")`` per
    Typer's testing docs.

    The wizard never honours ``--dry-run`` directly for the install
    plan: it's a separate UX surface (operators driving the wizard
    explicitly want writes). The ``dry_run`` argument is forwarded
    only to the credential intake step so a previewer cannot
    accidentally persist a secret. Tests should patch ``Path.home`` +
    ``Path.cwd`` to a tmp dir to keep the developer's real config
    untouched.
    """
    detected = set(_auto_detect(cwd))

    typer.echo("PopolaLoom interactive setup wizard")
    typer.echo("-----------------------------------")
    if detected:
        typer.echo(f"Auto-detected: {', '.join(sorted(detected))}")
    else:
        typer.echo("Auto-detected: (none — defaults will favor cursor)")

    plan: list[tuple[str, str]] = []
    for ide in _IDE_TARGETS_FOR_WIZARD:
        default_yes = ide in detected or (ide == "cursor" and not detected)
        prompt_label = f"Install for {ide.capitalize()}?"
        if not typer.confirm(prompt_label, default=default_yes):
            continue
        if ide == "copilot":
            scope = "project"
        else:
            scope = _prompt_scope(ide=ide, default_project=ide in detected)
        plan.append((ide, scope))

    install_local_choice = typer.confirm(
        "Scaffold .local/ workspace?",
        default=not (cwd / ".local").is_dir(),
    )

    if not plan and not install_local_choice:
        typer.echo("\nNothing selected. Wizard exiting without changes.")
        return

    typer.echo("\nInstall plan:")
    for ide, scope in plan:
        path = _resolve_target_path_for_wizard(ide, scope=scope, cwd=cwd)
        typer.echo(f"  - {ide} ({scope}) → {path}")
    if install_local_choice:
        typer.echo(f"  - local (project) → {cwd / '.local'}")

    if not typer.confirm("\nProceed with this plan?", default=True):
        typer.echo("Aborted by operator. No changes written.")
        return

    typer.echo("")
    for ide, scope in plan:
        _install_target(ide, scope=scope, cwd=cwd, dry_run=False)
    if install_local_choice:
        _install_local(cwd, no_compile=False, with_examples=False, dry_run=False)

    _handle_credential_intake_after_install(
        resolved_key=cursor_api_key,
        configure_cursor_auth=configure_cursor_auth,
        dry_run=dry_run,
    )

    typer.echo("\nInteractive setup complete.")


def _prompt_scope(*, ide: str, default_project: bool) -> str:
    """Prompt the operator for ``global`` vs ``project`` scope.

    Returns the literal string ``"global"`` or ``"project"``.  Anything
    that doesn't parse as G/g/global is treated as project (mirrors the
    DevolaFlow wizard's "default-friendly" parser; we deliberately avoid
    raising on free-text since the wizard is meant for humans).
    """
    default_label = "P" if default_project else "G"
    raw = typer.prompt(
        f"  Scope for {ide} [G=global / P=project]",
        default=default_label,
    )
    token = (raw or "").strip().lower()
    if token in {"g", "global"}:
        return "global"
    return "project"


def _resolve_target_path_for_wizard(ide: str, *, scope: str, cwd: Path) -> Path:
    """Return the install path the wizard will write for the chosen ide+scope."""
    if ide == "cursor":
        return cursor_target_path(scope, cwd=cwd)
    if ide == "claude":
        return claude_target_path(scope, cwd=cwd)
    if ide == "copilot":
        return copilot_target_path(cwd=cwd)
    if ide == "codex":
        return codex_target_path()
    raise typer.BadParameter(f"unknown wizard target: {ide!r}")


def _resolve_scope(global_: bool, project: bool, *, default: str = "project") -> str:
    """Resolve ``--global`` / ``--project`` to a single ``scope`` string.

    Raises:
        typer.BadParameter: when both flags are passed simultaneously
            (S-5: explicit conflict error rather than a silent
            last-flag-wins fallback).
    """
    if global_ and project:
        raise typer.BadParameter("--global and --project are mutually exclusive")
    if global_:
        return "global"
    if project:
        return "project"
    return default


@app.command("cursor")
def cmd_cursor(
    global_: bool = typer.Option(False, "--global", help="Install to ~/.cursor/."),
    project: bool = typer.Option(False, "--project", help="Install to <cwd>/.cursor/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for Cursor (project-local default)."""
    scope = _resolve_scope(global_, project)
    _install_target("cursor", scope=scope, cwd=Path.cwd(), dry_run=dry_run)


@app.command("claude")
def cmd_claude(
    global_: bool = typer.Option(False, "--global", help="Install to ~/.claude/."),
    project: bool = typer.Option(False, "--project", help="Install to <cwd>/.claude/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for Claude Code."""
    scope = _resolve_scope(global_, project)
    _install_target("claude", scope=scope, cwd=Path.cwd(), dry_run=dry_run)


@app.command("copilot")
def cmd_copilot(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for GitHub Copilot (single-file, project-local)."""
    _install_target("copilot", scope="project", cwd=Path.cwd(), dry_run=dry_run)


@app.command("codex")
def cmd_codex(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install the PopolaLoom skill for Codex (``$CODEX_HOME`` or ~/.codex/)."""
    _install_target("codex", scope="global", cwd=Path.cwd(), dry_run=dry_run)


@app.command("local")
def cmd_local(
    no_compile: bool = typer.Option(
        False,
        "--no-compile",
        help="Skip the .rules/ compile chain (deferred to v0.6.0 — flag is a no-op for now).",
    ),
    with_examples_flag: bool | None = typer.Option(
        None,
        "--with-examples/--no-with-examples",
        help="Seed example tasks under .local/tasks/ (default: off; --mode=full overrides).",
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="Shorthand: core | standard | full (overridden by individual flags).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Scaffold the .local/ workspace surface for PopolaLoom."""
    no_compile_resolved, with_examples_resolved = _resolve_mode(
        mode,
        no_compile_flag=no_compile,
        with_examples_flag=with_examples_flag,
    )
    _install_local(
        Path.cwd(),
        no_compile=no_compile_resolved,
        with_examples=with_examples_resolved,
        dry_run=dry_run,
    )


@app.command("all")
def cmd_all(
    global_: bool = typer.Option(False, "--global", help="Use global scope where supported."),
    project: bool = typer.Option(False, "--project", help="Force project scope."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without writing."),
) -> None:
    """Install every IDE target (excluding ``local`` — opt-in via ``init local``)."""
    scope = _resolve_scope(global_, project)
    _install_all(scope=scope, cwd=Path.cwd(), dry_run=dry_run)


def _install_all(*, scope: str, cwd: Path, dry_run: bool) -> None:
    """Install every IDE target except ``local`` (mirrors DevolaFlow `all`)."""
    targets: Sequence[str] = ("cursor", "claude", "copilot", "codex")
    typer.echo(f"  popola init all — installing: {', '.join(targets)} (scope={scope})")
    for target in targets:
        _install_target(target, scope=scope, cwd=cwd, dry_run=dry_run)
