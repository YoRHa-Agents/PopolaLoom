"""Resolve the canonical SKILL.md source path bundled with the wheel.

Stage S2 of v0.5.0 introduces the ``popola init`` skill installer; each
install verb (``cursor`` / ``claude`` / ``copilot`` / ``codex``) needs to
copy a SKILL.md from the package wheel to a per-IDE target path. The real
SKILL.md content lands in Stage S3; this module provides the resolver
contract so S2 can ship a working installer without blocking on S3.

Resolution strategy (per the v0.5.0 plan §4 Stage S2.6 + the locked S2
spec — `_skill_source.py` resolver):

1. ``<package_root>/skills/popola-loom/SKILL.md`` — the canonical
   wheel-bundled path written by Stage S3.  When present this file is
   the source of truth for every install verb.  (The directory name was
   renamed from ``popolaloom`` → ``popola-loom`` in v0.7.1+; the Python
   package name ``popolaloom`` is unchanged.)
2. Fallback to a generated stub with the v0.5.0 frontmatter shape and a
   "TODO: stage S3 will replace this" body.  The stub is byte-stable
   across runs (no timestamps, no hashes) so idempotency tests can hash
   the install target without flapping.

Both the lookup path and the stub body are version-pinned to
``popolaloom.__version__`` so a doctor verb (Stage S4) can detect drift
between the installed SKILL.md and the running wheel.

S-5 (No Silent Failures): when the canonical path exists but is
unreadable (e.g. permission denied on a corrupt wheel), the resolver
re-raises :class:`OSError` instead of silently returning the stub — the
operator must see the failure rather than getting an out-of-date stub
masquerading as the real skill.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from popolaloom import __version__

__all__ = [
    "STUB_BODY",
    "canonical_source_path",
    "is_real_skill",
    "render_stub",
    "resolve_skill_source",
]


def canonical_source_path() -> Path | None:
    """Return the on-disk path of the wheel-bundled SKILL.md, or ``None``.

    Uses :func:`importlib.resources.files` so the path resolves correctly
    inside a real wheel install (where the package may live under
    ``site-packages/popolaloom/``) and inside the editable / source
    checkout (``src/popolaloom/``).

    Returns:
        Path: absolute path to ``<package>/skills/popola-loom/SKILL.md``
        when it exists on disk; ``None`` otherwise (Stage S2 ships
        without the real skill so the fallback stub fires).
    """
    package_root = resources.files("popolaloom")
    candidate = package_root.joinpath("skills", "popola-loom", "SKILL.md")
    try:
        path = Path(str(candidate))
    except (TypeError, ValueError):
        return None
    if path.is_file():
        return path
    return None


STUB_FRONTMATTER: str = f"""\
---
name: popola-loom
version: {__version__}
description: >
  Use when delegating long-running coding tasks to a local agent CLI
  (Cursor / Claude / Codex / Copilot) that should survive terminal exit
  and emit progress events / HITL prompts.  Provides task ID assignment,
  status tracing, and attach-to-running-task semantics.
metadata:
  requires:
    bins: ["popola"]
    pythonVersion: ">=3.11"
  cliHelp: "popola --help"
  stage: "S2-placeholder"
---
"""


STUB_BODY: str = (
    STUB_FRONTMATTER
    + """
# PopolaLoom

> Stage S2 placeholder — the canonical SKILL.md content lands in Stage
> S3 of v0.5.0.  Until then the installer ships this stub so that the
> install path, frontmatter shape, and idempotency contract can be
> exercised end-to-end.

PopolaLoom is a meta-orchestrator for local coding agent CLIs (Cursor,
Claude Code, Codex CLI, GitHub Copilot CLI).  See `popola --help` for
the runtime surface.

## TODO: Stage S3 will replace this

Stage S3 fills in:

- Overview + when-to-use prose
- Quick reference (≥ 5 commands)
- Detailed workflows (supervised long-running task)
- MCP verb summary (9 verbs)
- Troubleshooting + token-budget evidence

Refresh by re-running `popola init` after upgrading to v0.5.0+ once
Stage S3 lands.
"""
)


def render_stub() -> str:
    """Return the frontmatter + body stub as a single UTF-8 string.

    Stable across runs: no timestamps, no random tokens.  Tests can hash
    this output to verify that re-running ``popola init <verb>`` is a
    no-op when the target SKILL.md is byte-identical.
    """
    return STUB_BODY


def is_real_skill(text: str) -> bool:
    """Return ``True`` iff ``text`` is the post-S3 canonical SKILL.md.

    The check is a substring lookup for the stub's "Stage S2
    placeholder" sentinel: any SKILL.md without that sentinel is
    considered the real skill (Stage S3 must remove the sentinel when
    it ships the canonical content).
    """
    return "Stage S2 placeholder" not in text


def resolve_skill_source() -> tuple[str, bool]:
    """Resolve the SKILL.md content + a "is real skill" flag.

    Returns:
        tuple[str, bool]: ``(content, is_real)`` — ``content`` is the
        UTF-8 string ready to be written into the install target;
        ``is_real`` is ``True`` when the wheel-bundled canonical
        SKILL.md was found (post-S3), ``False`` when the placeholder
        stub fired (Stage S2 default).

    Raises:
        OSError: re-raises read errors from the canonical path so the
        operator sees corrupt-wheel failures (S-5 No Silent Failures).
    """
    src = canonical_source_path()
    if src is not None:
        content = src.read_text(encoding="utf-8")
        return content, is_real_skill(content)
    return render_stub(), False
