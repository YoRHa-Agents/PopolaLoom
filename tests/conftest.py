"""Shared pytest fixtures for the popolaloom test suite (Stage Impl-2).

Exposes :func:`popolad_factory` — a builder callable that constructs
:class:`popolaloom.daemon.Popolad` instances backed by a tmp_path events
directory and a configurable fake adapter, so tests across daemon / adapter /
cli teams don't need to retype the boilerplate.

v0.7.2+ also auto-redirects ``$POPOLA_HANDOFF_DIR`` to a per-session tmp
directory via :func:`_handoff_dir_session` (autouse, session-scoped) so
``Popolad.dispatch_with_envelope`` (and thus every ``dispatch_task`` call
through the new E3 internal-unification path) writes envelope files to a
disposable location instead of polluting the real workspace
``.local/.agent/handoff/``.

v0.8.6 (T2.2.2) appends :func:`test_invariant_i1_sole_writer_of_cloud_phase`
— a CI static-grep guard that asserts ``daemon/cloud_poller.py`` is the **only**
module passing ``cloud_phase=`` to ``StateStore.update`` (invariant **I-1** in
``state-source-of-truth.md`` §6). Any new call site outside the allow-list
fails this test at PR time.

Lives in ``tests/`` (not ``src/``) so production code never imports it.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tokenize
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import Popolad


@pytest.fixture(autouse=True, scope="session")
def _handoff_dir_session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Auto-redirect ``$POPOLA_HANDOFF_DIR`` to a session tmp dir.

    v0.7.2+ rationale: ``Popolad.dispatch_with_envelope`` (the canonical
    E3-unified dispatch path) writes a Markdown envelope file per dispatch.
    Default location is ``.local/.agent/handoff/<id>.md`` relative to CWD;
    in tests we want isolation so env files don't pollute the project
    workspace's gitignored handoff dir (which is for real dispatches).

    The fixture is autouse + session-scoped so every test in the suite
    benefits without explicit opt-in. The env var is only set if not
    already set by the user (so an explicit override in a sub-process /
    nested pytest run survives).
    """
    handoff_dir = tmp_path_factory.mktemp("popola_handoff_session")
    if os.environ.get("POPOLA_HANDOFF_DIR") is None:
        os.environ["POPOLA_HANDOFF_DIR"] = str(handoff_dir)
    return handoff_dir

# Stage E: AdapterCallback is now a strict 4-arg signature (cli, prompt, cwd, extra)
# per R-009 closure; conftest stays 4-arg too to satisfy mypy strict.
_AdapterFn = Callable[[str, str, "Path | None", "dict[str, Any] | None"], list[str]]


def _default_noop_adapter(
    cli: str,
    prompt: str,
    cwd: Path | None,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Return a fast python subprocess argv that prints + exits 0 (test default).

    使用 ``sys.executable`` 而非裸 ``python``: 保证测试始终用 pytest 当前
    解释器, 不依赖 ``$PATH`` 上是否有 ``python`` 别名。
    """
    return [
        sys.executable,
        "-c",
        "print('test stdout'); import sys; sys.exit(0)",
    ]


@pytest.fixture
def popolad_factory() -> Callable[..., Popolad]:
    """Yield a builder ``(events_dir, adapter=None) -> Popolad``.

    The default adapter spawns a tiny python subprocess that prints
    ``test stdout`` and exits 0 — fast enough for assertions that just need a
    completed task without dragging in cursor-agent / claude / codex binaries.

    Returns:
        Callable[[Path, _AdapterFn | None], Popolad]: factory that the test
        invokes once per Popolad instance it needs.
    """

    def _build(events_dir: Path, adapter: _AdapterFn | None = None) -> Popolad:
        return Popolad(events_dir=events_dir, adapter=adapter or _default_noop_adapter)

    return _build


# ---------------------------------------------------------------------------
# v0.8.6 T2.2.2 — I-1 sole-writer guard (CI static-grep fixture)
# ---------------------------------------------------------------------------
#
# Cross-reference: ``state-source-of-truth.md`` §1.2 rule 1 + §6 I-1.
# Cross-reference: ``daemon/cloud_poller.py`` carries the matching inline
# ``# I-1 sole-writer: ...`` comment at the canonical ``_emit_run_status``
# site so reviewers can trace the contract from either direction.
#
# Cross-reference (I-2): a non-poller invocation of
# ``state_store.update(cloud_phase=...)`` from any future SSE worker — for
# example a refactor that hands a :class:`StateStore` reference to
# :class:`SSEReader` — would be caught by THIS guard at CI time and would
# fail the build. I-2 (append-only SSE) is enforced at runtime by
# :class:`SSEReader.__init__` rejecting any ``StateStore``-typed
# collaborator (see ``adapters/cursor_cloud.py`` Q-A-8 docstring).

# Single-source allow-list. Paths are POSIX-style relative to
# ``src/popolaloom/``. To extend (e.g., when refactoring), edit only here.
_I1_MUST_BE_ONLY_FILE: frozenset[str] = frozenset({"daemon/cloud_poller.py"})

# Regex (per T2.2.2 spec §Hints): tolerant of formatting — the ``[^)]*``
# greedy class spans newlines so a multi-line ``state_store.update(...
# cloud_phase=...)`` call still matches. ``state[_\s]*store`` lets us catch
# both ``state_store`` and the rarer ``state store`` typo. ``re.IGNORECASE``
# guards against a future ``StateStore.update(...)`` rename.
_I1_PATTERN: re.Pattern[str] = re.compile(
    r"state[_\s]*store\.update\([^)]*cloud_phase\s*=",
    re.IGNORECASE,
)


def _i1_repo_root() -> Path:
    """Return the repository root, derived from this conftest's location."""
    return Path(__file__).resolve().parents[1]


def _i1_strip_strings_and_comments(content: str) -> str:
    """Replace string literals + comments with spaces, preserving line numbers.

    Without this step the I-1 regex would false-positive on docstrings that
    *describe* the rule (e.g., the ``cursor_cloud.py`` Q-A-8 docstring quotes
    ``StateStore.update(... cloud_phase=...)`` as part of explaining the
    invariant). Replacing token characters with spaces keeps every newline
    intact so the line number we report still points at the original source.
    On a tokenize failure (corrupt source) we fall back to the raw content
    rather than silently skipping the file (No-Silent-Failures rule).
    """
    line_chars = [list(line) for line in content.splitlines(keepends=True)]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return content
    for tok in tokens:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (start_row, start_col), (end_row, end_col) = tok.start, tok.end
        for row_idx in range(start_row - 1, end_row):
            if row_idx >= len(line_chars):
                continue
            row = line_chars[row_idx]
            col_lo = start_col if row_idx == start_row - 1 else 0
            col_hi = end_col if row_idx == end_row - 1 else len(row)
            for col in range(col_lo, min(col_hi, len(row))):
                if row[col] not in ("\n", "\r"):
                    row[col] = " "
    return "".join("".join(row) for row in line_chars)


def _i1_collect_offenders() -> dict[str, list[tuple[int, str]]]:
    """Walk ``src/popolaloom/**/*.py``; return offenders → list of (lineno, line).

    A file is an "offender" when the I-1 regex matches its **executable**
    content (after stripping strings + comments) AND the file's POSIX-relative
    path under ``src/popolaloom/`` is NOT in :data:`_I1_MUST_BE_ONLY_FILE`.
    The ``_vendored/`` tree is skipped because it is upstream code we are not
    allowed to modify (see ``VENDORING.md``); this mirrors the existing carve-
    outs in ``pyproject.toml`` for ruff / mypy / coverage.
    """
    src_dir = _i1_repo_root() / "src" / "popolaloom"
    offenders: dict[str, list[tuple[int, str]]] = {}
    for py_path in sorted(src_dir.rglob("*.py")):
        rel_posix = py_path.relative_to(src_dir).as_posix()
        if rel_posix.startswith("_vendored/"):
            continue
        try:
            raw = py_path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned = _i1_strip_strings_and_comments(raw)
        match = _I1_PATTERN.search(scanned)
        if match is None:
            continue
        if rel_posix in _I1_MUST_BE_ONLY_FILE:
            continue
        # Compute 1-indexed line of the match start; show the ORIGINAL line
        # (with literals + comments intact) so the diagnostic is readable.
        line_no = scanned[: match.start()].count("\n") + 1
        raw_lines = raw.splitlines()
        offending_line = (
            raw_lines[line_no - 1] if 0 < line_no <= len(raw_lines) else "<?>"
        )
        offenders.setdefault(rel_posix, []).append((line_no, offending_line))
    return offenders


def test_invariant_i1_sole_writer_of_cloud_phase() -> None:
    """I-1: only ``daemon/cloud_poller.py`` may write ``cloud_phase`` via StateStore.

    This static-grep guard fails CI if any module under ``src/popolaloom/``
    (other than the allow-listed canonical writer) passes ``cloud_phase=`` to
    ``StateStore.update``. It is the strongest preventer of regression for
    invariant **I-1** in ``state-source-of-truth.md`` §6 because it fires at
    PR time, before review. Diagnostic output lists every offending file +
    line so a reviewer can immediately see what to fix.

    Cross-reference: see the ``# I-1 sole-writer`` comment block at the top
    of :meth:`CloudPollLoop._emit_run_status` in ``daemon/cloud_poller.py``.
    """
    offenders = _i1_collect_offenders()
    if not offenders:
        return
    msg_lines: list[str] = [
        "I-1 sole-writer rule violated.",
        "Only files in MUST_BE_ONLY_FILE may pass `cloud_phase=` to "
        "`StateStore.update`.",
        f"  allow-list = {sorted(_I1_MUST_BE_ONLY_FILE)}",
        "Offending files (relative to src/popolaloom/):",
    ]
    for rel in sorted(offenders):
        msg_lines.append(f"  - {rel}:")
        for line_no, offending in offenders[rel]:
            msg_lines.append(f"      L{line_no}: {offending.strip()}")
    msg_lines.append(
        "See state-source-of-truth.md §1.2 rule 1 + §6 I-1 for the contract."
    )
    raise AssertionError("\n".join(msg_lines))


# ---------------------------------------------------------------------------
# v0.8.7 T2.2.2 — Q-B-5 / M1 misleading-wording guard (CI grep fixture)
# ---------------------------------------------------------------------------
#
# Cross-reference: ``deployment-modes.md`` §1 + §4 row D + §5 lateral-
# movement list; ``mcp-tool-contract.md`` §1 ("the cloud reaches the worker
# over a long-lived outbound HTTPS — no inbound ports / public IP / VPN
# required"); ``SECURITY_CHECKLIST.md`` §8 M1; ``PLAN.md`` §4.2 T2.2.2.
#
# Cursor Cloud Agents access PopolaLoom HITL via outbound-only worker
# sessions (γ — Worker stdio MCP) or backend-proxied HTTPS MCP (β — HTTP
# MCP). The five misleading prerequisites — public IP, port-forward,
# residential NAT, inbound port, VPN tunnel — are NEVER required and the
# in-tree documentation set MUST NOT recommend them.
#
# Allowed exception: ``docs/known-issues.md`` carries an explicit
# "do NOT do this" callout (the only place reviewers will see the wording
# in-tree). The companion CHANGELOG entry — *"doc-only correction: cloud
# HITL transport story aligned with deployment-modes.md"* — lands later
# under T2.3.3 (W2.3 of PLAN.md), not in this task; do NOT add the
# CHANGELOG line here.

# Single ripgrep regex per T2.2.2 spec §Hints. Case-insensitive; matches
# the five forbidden prerequisites the Q-B-5 cleanup targets.
_MISLEADING_WORDING_PATTERN: str = (
    r"(public\s+ip|port[- ]?forward|residential\s+NAT"
    r"|inbound\s+port|VPN\s+tunnel)"
)

# Allowlist: paths (POSIX-style, repo-root-relative) where the misleading
# wording is allowed to appear because it lives as the explicit callout.
# To extend (e.g., when refactoring), edit only here.
_MISLEADING_WORDING_ALLOWLIST: frozenset[str] = frozenset(
    {"docs/known-issues.md"}
)

# Grep scope: the in-tree paths the guard checks. ``.local/research/`` is
# intentionally excluded — it is the out-of-tree research-note tree where
# the upstream Q-B-5 source material lives (and is gitignored).
_MISLEADING_WORDING_SCOPE: tuple[str, ...] = (
    "src/popolaloom/",
    "docs/",
    "README.md",
    "RELEASE_NOTES.md",
    "CHANGELOG.md",
)


def _misleading_wording_repo_root() -> Path:
    """Return the repository root, derived from this conftest's location."""
    return Path(__file__).resolve().parents[1]


def _misleading_wording_parse_rg(stdout: str) -> dict[str, list[tuple[int, str]]]:
    """Parse ``rg -n --no-heading`` output into ``{rel_path: [(lineno, line)]}``.

    Output format is one match per line: ``rel/path/file:lineno:content``.
    The first two ``:`` are the path / lineno separators; everything after
    the second ``:`` is the matched content (which may itself contain
    colons, hence ``split(":", 2)``). Lines whose path is in the
    allow-list are dropped here so the test body stays declarative.
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        rel_posix, lineno_s, content = parts[0], parts[1], parts[2]
        try:
            lineno = int(lineno_s)
        except ValueError:
            continue
        if rel_posix in _MISLEADING_WORDING_ALLOWLIST:
            continue
        offenders.setdefault(rel_posix, []).append((lineno, content))
    return offenders


@pytest.fixture(scope="session")
def cloud_hitl_misleading_wording_guard() -> dict[str, list[tuple[int, str]]]:
    """Session-scoped: greps once per session, returns offenders.

    Wraps a **single** ``rg`` invocation per the T2.2.2 transparency
    constraint. ``rg`` exit codes: 0 = matches, 1 = no matches, 2 = error;
    rc 0 / 1 are both acceptable (only rc ≥ 2 raises so a corrupt CLI
    install fails loudly per the No-Silent-Failures rule).

    If ``rg`` is not on ``PATH`` we ``pytest.skip`` with an actionable
    message — the production guard runs in CI where ripgrep is part of
    the toolchain; local environments may not have it.

    Returns:
        Mapping of repo-root-relative POSIX path → list of
        ``(lineno, matched_line)`` tuples for every offender (i.e., every
        match outside :data:`_MISLEADING_WORDING_ALLOWLIST`).
    """
    if shutil.which("rg") is None:
        pytest.skip(
            "ripgrep (rg) is not installed; the Q-B-5 / M1 misleading-"
            "wording guard requires it (see PLAN.md §4.2 T2.2.2). "
            "Install via `cargo install ripgrep` or `pip install ripgrep`."
        )

    repo_root = _misleading_wording_repo_root()
    cmd = [
        "rg",
        "-i",
        "-n",
        "--no-heading",
        "--color",
        "never",
        _MISLEADING_WORDING_PATTERN,
        *_MISLEADING_WORDING_SCOPE,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            "Q-B-5 / M1 guard: `rg` invocation failed "
            f"(rc={result.returncode}). stderr={result.stderr.strip()!r} "
            f"stdout={result.stdout.strip()!r}"
        )
    return _misleading_wording_parse_rg(result.stdout)


def test_misleading_wording_guard(
    cloud_hitl_misleading_wording_guard: dict[str, list[tuple[int, str]]],
) -> None:
    """Q-B-5 / M1: misleading transport wording must not leak in-tree.

    Asserts that the misleading prerequisites — public IP, port-forward,
    residential NAT, inbound port, VPN tunnel — appear **only** in the
    explicit "do NOT do this" callout under
    :data:`_MISLEADING_WORDING_ALLOWLIST`. Any other in-tree hit fails CI
    at PR time with a diagnostic listing the offending file + line.

    Cross-reference: ``deployment-modes.md`` §1 + §4 row D;
    ``SECURITY_CHECKLIST.md`` §8 M1; ``PLAN.md`` §4.2 T2.2.2.
    """
    offenders = cloud_hitl_misleading_wording_guard
    if not offenders:
        return
    msg_lines: list[str] = [
        "Q-B-5 / M1 misleading-wording guard violated.",
        "Cloud HITL transport story is γ (Worker stdio MCP) + β (HTTP MCP "
        "backend-proxied). Public IP / port-forward / residential NAT / "
        "inbound port / VPN tunnel are NEVER required and MUST NOT be "
        "documented as prerequisites.",
        f"  allowlist = {sorted(_MISLEADING_WORDING_ALLOWLIST)}",
        f"  scope     = {list(_MISLEADING_WORDING_SCOPE)}",
        "Offending paths (relative to repo root):",
    ]
    for rel in sorted(offenders):
        msg_lines.append(f"  - {rel}:")
        for lineno, content in offenders[rel]:
            msg_lines.append(f"      L{lineno}: {content.strip()}")
    msg_lines.append(
        "See deployment-modes.md §1 + §4 row D for the canonical wording; "
        "the only allowed in-tree mention is the explicit callout in "
        "docs/known-issues.md."
    )
    raise AssertionError("\n".join(msg_lines))
