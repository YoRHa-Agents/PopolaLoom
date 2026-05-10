"""W2.2 release-gate lint — no v0.8.x deprecation shims remain in source.

Per ``.local/.agent/active/v0.9.0-ga/DECISIONS.md`` OQ-2 (the
``popola_inject_subtask`` carve-out) and Q-D-3 lock (`"删除全部 v0.8.x
deprecation shim"`), the v0.9.0 GA release gate asserts that the W2.2
sweep landed without residue.

Concretely, this lint walks ``src/popolaloom`` and rejects:

1. Any ``warnings.warn(..., DeprecationWarning)`` raise that targets a
   v0.8.x deprecation (preserves any future warnings that target later
   versions, e.g. v0.9.x → v0.10).
2. Any ``# v0.7.x TEMP`` / ``# v0.8.x TEMP`` source-code marker.
3. Any reference to the legacy ``RelayHandoffEnvelope`` Pydantic class
   or the ``to_handoff_envelope`` migration helper (BL-v0.9.0-1).

Allowlist (per OQ-2):

- ``popola_inject_subtask`` MCP verb in ``src/popolaloom/mcp/tools.py`` —
  v0.2.x legacy alias for ``popola_relay`` (predates v0.8.x; not a
  v0.8.x deprecation; explicitly retained per DECISIONS.md OQ-2).
- Comment / docstring text mentioning ``RelayHandoffEnvelope`` or
  ``to_handoff_envelope`` for migration-history explanation (the
  identifiers are gone from executable code, but historical context
  inside ``\"\"\"docstrings\"\"\"`` and ``# comments`` remains
  explanatory).

Failure here is a Stage 5 release-gate blocker for v0.9.0 (PLAN.md
§9 W2.2 box).
"""

from __future__ import annotations

import re
import tokenize
from io import StringIO
from pathlib import Path

import pytest

SRC_ROOT: Path = Path(__file__).resolve().parents[1] / "src" / "popolaloom"

_BANNED_IDENTIFIERS = (
    "RelayHandoffEnvelope",
    "to_handoff_envelope",
)

_TEMP_MARKER_RE = re.compile(r"#\s*v0\.[78]\.x\s+TEMP", re.IGNORECASE)
_DEPRECATION_WARN_RE = re.compile(
    r"warnings\.warn\s*\(.*DeprecationWarning",
    re.DOTALL,
)


def _walk_python_sources() -> list[Path]:
    """Return every ``.py`` file under ``src/popolaloom`` (excluding vendored)."""
    rels: list[Path] = []
    for path in SRC_ROOT.rglob("*.py"):
        if "_vendored" in path.parts:
            continue
        rels.append(path)
    return sorted(rels)


def _strip_comments_and_strings(source: str) -> str:
    """Return source with comments and string literals replaced by spaces.

    Uses Python's tokenize module so we accurately distinguish executable
    code from comments / docstrings / string literals.
    """
    out_chars: list[str] = []
    line_offsets: list[int] = [0]
    for ch in source:
        if ch == "\n":
            line_offsets.append(len(out_chars) + 1)
        out_chars.append(ch)
    if not source.endswith("\n"):
        line_offsets.append(len(out_chars))

    spaces = list(source)
    try:
        tokens = list(tokenize.generate_tokens(StringIO(source).readline))
    except (tokenize.TokenizeError, IndentationError):
        return source
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_line, start_col) = tok.start
        (end_line, end_col) = tok.end
        start_idx = line_offsets[start_line - 1] + start_col
        end_idx = line_offsets[end_line - 1] + end_col
        for i in range(start_idx, min(end_idx, len(spaces))):
            if spaces[i] != "\n":
                spaces[i] = " "
    return "".join(spaces)


def test_no_relay_handoff_envelope_references_in_code() -> None:
    """No executable code references ``RelayHandoffEnvelope`` or ``to_handoff_envelope``.

    Migration history docstrings / comments are allowed; only live code
    (imports, identifiers used at runtime) is rejected.
    """
    offenders: list[str] = []
    for path in _walk_python_sources():
        text = path.read_text(encoding="utf-8")
        code_only = _strip_comments_and_strings(text)
        for banned in _BANNED_IDENTIFIERS:
            if re.search(rf"\b{re.escape(banned)}\b", code_only):
                offenders.append(f"{path.relative_to(SRC_ROOT.parent.parent)}: {banned}")

    assert not offenders, (
        "v0.9.0 W2.2 release-gate: removed deprecation symbols still referenced "
        "in executable code:\n  " + "\n  ".join(offenders)
    )


def test_no_v08_temp_markers_in_source() -> None:
    """No ``# v0.7.x TEMP`` / ``# v0.8.x TEMP`` markers remain.

    These markers signal "code that's expected to be removed in the next
    minor"; v0.9.0 is the next minor after v0.8.x, so any survivor is a
    W2.2 sweep miss.
    """
    offenders: list[str] = []
    for path in _walk_python_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _TEMP_MARKER_RE.search(line):
                offenders.append(
                    f"{path.relative_to(SRC_ROOT.parent.parent)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "v0.9.0 W2.2 release-gate: # v0.7.x/v0.8.x TEMP markers remain:\n  "
        + "\n  ".join(offenders)
    )


_V010_DEPRECATION_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/popolaloom/adapters/cursor_cloud.py",
    }
)
"""Files allowed to call ``warnings.warn(..., DeprecationWarning)`` in v0.10.0+.

Per v0.10.0 DECISIONS Q-2 + Q-11 (`.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`),
``cursor_cloud.py`` translates legacy v0.9.x ``use_private_worker``/``labels``/
``worker_name``/``machine_name`` extras to the new ``env: AgentEnv`` shape
during a one-minor-release deprecation window. The warning targets the
v0.9.x → v0.10.0 transition (NOT a v0.8.x shim), so it is explicitly
allowlisted; v1.1+ removes the alias entirely.
"""


def test_no_v08x_deprecation_warning_raises() -> None:
    """No ``warnings.warn(..., DeprecationWarning)`` raise targets a v0.8.x surface.

    Future-targeted warnings (e.g. for v0.9.x → v0.10) are tolerated via the
    explicit ``_V010_DEPRECATION_ALLOWLIST``; any new file appearing in the
    offender list must be reviewed (either: actually target v0.8.x → remove,
    or: target v0.10.0+ legitimately → add to the allowlist).
    """
    offenders: list[str] = []
    for path in _walk_python_sources():
        text = path.read_text(encoding="utf-8")
        if _DEPRECATION_WARN_RE.search(text):
            rel = str(path.relative_to(SRC_ROOT.parent.parent))
            if rel not in _V010_DEPRECATION_ALLOWLIST:
                offenders.append(rel)

    assert not offenders, (
        "v0.9.0 W2.2 release-gate: warnings.warn(..., DeprecationWarning) "
        "raises remain (and are NOT on the v0.10.0 allowlist):\n  "
        + "\n  ".join(offenders)
    )


def test_popola_inject_subtask_alias_retained_per_oq2() -> None:
    """The single OQ-2 carve-out — ``popola_inject_subtask`` MCP verb — is retained.

    Asserts exactly ONE registration in ``mcp/tools.py``; the verb's
    body internally calls ``popola_relay``'s handler (it predates
    v0.8.x and is not a v0.8.x deprecation per DECISIONS.md OQ-2).
    """
    tools_path = SRC_ROOT / "mcp" / "tools.py"
    text = tools_path.read_text(encoding="utf-8")
    name_matches = re.findall(r'name="popola_inject_subtask"', text)
    assert len(name_matches) == 1, (
        f"DECISIONS.md OQ-2: expected exactly one popola_inject_subtask "
        f"verb registration in mcp/tools.py; got {len(name_matches)}"
    )


@pytest.mark.parametrize(
    "removed_symbol",
    [
        "RelayHandoffEnvelope",
        "to_handoff_envelope",
    ],
)
def test_removed_symbols_not_importable(removed_symbol: str) -> None:
    """The removed v0.7.3 → v0.8.x symbols raise ``ImportError`` at runtime.

    Documented in ``docs/MIGRATION_v07_to_v09.md`` §"v0.9.0 GA
    deprecation removals" and in ``docs/API_STABILITY.md`` §4.
    """
    import popolaloom.daemon.primitives as primitives_pkg

    assert not hasattr(primitives_pkg, removed_symbol), (
        f"{removed_symbol} should be removed from popolaloom.daemon.primitives "
        f"in v0.9.0 (BL-v0.9.0-1, Q-D-3 lock)"
    )
