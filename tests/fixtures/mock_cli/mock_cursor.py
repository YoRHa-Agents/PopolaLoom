"""Mock ``cursor-agent`` for PopolaLoom Tier 2-5 tests.

Simulates ``cursor-agent agent --print [--output-format text|stream-json]
[--session-id <id>] [--cwd <dir>] <prompt>`` — the argv shape consumed
by :class:`popolaloom.adapters.cursor.CursorAdapter`.

Behavior summary (per testing-matrix.md §4 + §4.4):

- Reads the prompt from the first positional arg after ``--print`` (or
  the last positional arg if ``--print`` is absent — matching the real
  cursor-agent's tolerant argv parsing).
- First stdout line: ``[devola-flow:round=N]`` where ``N`` defaults to
  ``1`` and can be overridden via env ``MOCK_CURSOR_ROUND`` or via the
  programmatic :func:`run_mock_cursor` API.
- Body: configurable via env ``MOCK_CURSOR_CONTENT`` or the
  programmatic ``content`` arg.  Default body is a brief mock patch
  description so dispatch tests can verify "the agent did something".
- Trailing 3-section block: always emitted, with composite_score 0.886
  by default (configurable via ``MOCK_CURSOR_COMPOSITE_SCORE`` env or
  ``composite_score`` arg) so the inner-gate logic in
  ``src/popolaloom/evolution/`` (v0.3.0) sees a passing score.
- Exit code: ``MOCK_CURSOR_EXIT_CODE`` env or ``exit_code`` arg
  (default 0).
- ``--output-format stream-json`` emits one JSON envelope per line on
  stdout (``{"event": "text", "content": "..."}``) instead of plain
  text — matches real cursor-agent's stream-json mode close enough for
  adapter tests.

Test imports::

    from tests.fixtures.mock_cli.mock_cursor import run_mock_cursor

    cp = run_mock_cursor("implement foo", round_num=2, exit_code=0)
    assert cp.returncode == 0
    assert cp.stdout.startswith("[devola-flow:round=2]")
    assert "## Acceptance Verification" in cp.stdout

CLI entry (used by ``install_mock_binaries`` shim)::

    python -m tests.fixtures.mock_cli.mock_cursor agent --print "foo"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

DEFAULT_COMPOSITE_SCORE: float = 0.886
"""Default composite_score when not overridden — above the 0.85 inner
gate threshold so dispatch tests get an inner-PASS by default."""

DEFAULT_BODY_CONTENT: str = (
    "Mock cursor-agent: implemented the requested change.\n"
    "(diff would be shown here in a real run)"
)


@dataclass
class MockCursorOutputs:
    """Captured outputs of a single mock invocation (for assertions)."""

    stdout: str
    stderr: str
    returncode: int
    argv: list[str]


def _parse_prompt_from_argv(argv: list[str]) -> str:
    """Extract the prompt string from a cursor-agent-style argv.

    Real cursor-agent: ``cursor-agent agent --print [--output-format X]
    [--session-id ID] [--cwd D] <prompt>``.  We accept either the
    last positional arg, or — if ``--print`` is followed by a
    non-flag — the value right after ``--print``.

    Returns an empty string if no prompt-like arg can be located
    (caller may treat that as a no-op test setup).
    """
    if not argv:
        return ""
    skip_next = False
    flag_with_value = {"--output-format", "--session-id", "--cwd", "-w"}
    candidates: list[str] = []
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in flag_with_value:
            skip_next = True
            continue
        if arg.startswith("--") or arg.startswith("-"):
            continue
        if arg in {"agent", "exec", "--print"}:
            continue
        candidates.append(arg)
    if not candidates:
        return ""
    return candidates[-1]


def _detect_output_format(argv: list[str]) -> Literal["text", "stream-json"]:
    """Return ``stream-json`` if ``--output-format stream-json`` present, else ``text``."""
    for i, a in enumerate(argv):
        if (
            a == "--output-format"
            and i + 1 < len(argv)
            and argv[i + 1] == "stream-json"
        ):
            return "stream-json"
    return "text"


def _detect_round_num_from_prompt(prompt: str) -> int:
    """Parse ``round_num`` out of the prompt body if a Workflow Context block is present.

    Per roadmap §11.2, prompt may contain a ``## Workflow Context
    (devola-flow)`` section with a ``round_num: N`` line.  We extract
    that so the mock's first line ``[devola-flow:round=N]`` reflects
    it (per §4.4).  Falls back to the env override or 1.
    """
    for line in prompt.splitlines():
        ls = line.strip().lower()
        if ls.startswith("round_num:"):
            try:
                return int(ls.split(":", 1)[1].strip())
            except ValueError:
                continue
        if ls.startswith("- round_num:"):
            try:
                return int(ls.split(":", 1)[1].strip())
            except ValueError:
                continue
    env_val = os.environ.get("MOCK_CURSOR_ROUND")
    if env_val and env_val.isdigit():
        return int(env_val)
    return 1


def render_three_section(
    body: str,
    *,
    round_num: int,
    composite_score: float,
    extra_findings: list[str] | None = None,
) -> str:
    """Render the canonical 3-section devola-flow output (per testing-matrix.md §4.4).

    Args:
        body: Free-form mock body content (placed between the round
            marker and the trailing 3 sections).
        round_num: Reflected in the first line ``[devola-flow:round=N]``.
        composite_score: Used as the ``composite`` line in the
            ``## Gate Score Components`` block.
        extra_findings: Optional additional finding lines (severity-
            prefixed, e.g. ``"blocker: foo"``).

    Returns:
        Multi-line string ready to be written to stdout — guaranteed to
        contain all 3 trailing section headers + a composite_score
        line so the inner-gate parser succeeds.
    """
    lines: list[str] = []
    lines.append(f"[devola-flow:round={round_num}]")
    lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    lines.append("## Acceptance Verification")
    lines.append("- AC-1: mock implementation present in stdout body")
    lines.append("- AC-2: 3-section format honoured (per testing-matrix.md §4.4)")
    lines.append("")
    lines.append("## Gate Score Components")
    lines.append("- test_quality: 0.92")
    lines.append("- code_review: 0.88")
    lines.append("- architecture: 0.85")
    lines.append("- benchmark: 0.90")
    lines.append(f"- composite: {composite_score}")
    lines.append("")
    lines.append("## Findings")
    lines.append("- info: mock_cursor emitted devola-flow contract")
    if extra_findings:
        for f in extra_findings:
            lines.append(f"- {f}")
    return "\n".join(lines) + "\n"


def render_stream_json(
    body: str,
    *,
    round_num: int,
    composite_score: float,
) -> str:
    """Render the same 3-section content as NDJSON envelopes (cursor-agent stream-json mode).

    Matches the real cursor-agent's stream-json shape closely enough
    that adapter tests asserting ``output_format=stream-json`` round-
    trips behave correctly.  Each line is one JSON envelope; the final
    envelope has ``"event": "complete"``.
    """
    full_text = render_three_section(
        body, round_num=round_num, composite_score=composite_score
    )
    out_lines: list[str] = []
    for line in full_text.splitlines():
        out_lines.append(
            json.dumps({"event": "text", "content": line}, ensure_ascii=False)
        )
    out_lines.append(
        json.dumps({"event": "complete", "exit_code": 0}, ensure_ascii=False)
    )
    return "\n".join(out_lines) + "\n"


def run_mock_cursor(
    prompt: str,
    *,
    round_num: int | None = None,
    content: str | None = None,
    exit_code: int = 0,
    output_format: Literal["text", "stream-json"] = "text",
    composite_score: float | None = None,
) -> MockCursorOutputs:
    """Programmatic entry — invoke the mock and return captured outputs.

    Used in tests where you want to assert behaviour without going
    through ``subprocess.run``.  For tests that need to actually drive
    the popolad adapter chain, use :func:`install_mock_binaries` to
    materialise an executable shim instead.

    Args:
        prompt: Prompt text the mock should "see"; used to derive the
            round number if it contains a Workflow Context section.
        round_num: Override the round number that appears in the first
            output line.  Default is parsed from ``prompt`` or env or
            1.
        content: Override the body content; default is a brief mock
            patch description.
        exit_code: Process exit code (also overridable via env
            ``MOCK_CURSOR_EXIT_CODE``).
        output_format: ``"text"`` (default) or ``"stream-json"``.
        composite_score: Score in the gate-component block; default
            0.886 (above the 0.85 inner-gate threshold).

    Returns:
        :class:`MockCursorOutputs` with the rendered ``stdout``, an
        empty ``stderr`` (mock never writes to stderr), the
        ``returncode``, and the synthetic ``argv``.
    """
    body = content if content is not None else os.environ.get(
        "MOCK_CURSOR_CONTENT", DEFAULT_BODY_CONTENT
    )
    if round_num is None:
        round_num = _detect_round_num_from_prompt(prompt)
    if composite_score is None:
        env_score = os.environ.get("MOCK_CURSOR_COMPOSITE_SCORE")
        composite_score = float(env_score) if env_score else DEFAULT_COMPOSITE_SCORE
    if output_format == "stream-json":
        stdout = render_stream_json(
            body, round_num=round_num, composite_score=composite_score
        )
    else:
        stdout = render_three_section(
            body, round_num=round_num, composite_score=composite_score
        )
    argv = ["cursor-agent", "agent", "--print", "--output-format", output_format, prompt]
    return MockCursorOutputs(
        stdout=stdout,
        stderr="",
        returncode=exit_code,
        argv=argv,
    )


def run_as_subprocess(
    prompt: str,
    *,
    round_num: int | None = None,
    content: str | None = None,
    exit_code: int = 0,
    output_format: Literal["text", "stream-json"] = "text",
) -> subprocess.CompletedProcess[str]:
    """Convenience wrapper that returns a :class:`subprocess.CompletedProcess`.

    Useful when tests want the same shape as ``subprocess.run`` but
    without paying the cost of forking another Python interpreter for
    the mock.
    """
    out = run_mock_cursor(
        prompt,
        round_num=round_num,
        content=content,
        exit_code=exit_code,
        output_format=output_format,
    )
    return subprocess.CompletedProcess(
        args=out.argv,
        returncode=out.returncode,
        stdout=out.stdout,
        stderr=out.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry: parse argv → render → print → exit.

    Used by the ``install_mock_binaries`` shim so that
    ``shutil.which("cursor-agent")`` resolves to a real executable
    that emits the 3-section output and exits with the configured
    code.
    """
    if argv is None:
        argv = sys.argv[1:]
    prompt = _parse_prompt_from_argv(argv)
    output_format = _detect_output_format(argv)
    round_num = _detect_round_num_from_prompt(prompt)
    body = os.environ.get("MOCK_CURSOR_CONTENT", DEFAULT_BODY_CONTENT)
    env_score = os.environ.get("MOCK_CURSOR_COMPOSITE_SCORE")
    composite_score = float(env_score) if env_score else DEFAULT_COMPOSITE_SCORE
    if output_format == "stream-json":
        sys.stdout.write(
            render_stream_json(body, round_num=round_num, composite_score=composite_score)
        )
    else:
        sys.stdout.write(
            render_three_section(body, round_num=round_num, composite_score=composite_score)
        )
    sys.stdout.flush()
    exit_env = os.environ.get("MOCK_CURSOR_EXIT_CODE")
    return int(exit_env) if exit_env and exit_env.lstrip("-").isdigit() else 0


if __name__ == "__main__":
    sys.exit(main())
