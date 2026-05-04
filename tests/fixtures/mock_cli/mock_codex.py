"""Mock ``codex`` CLI for PopolaLoom Tier 2-5 tests.

Simulates ``codex exec [--sandbox <mode>] <prompt>`` — the argv shape
consumed by :class:`popolaloom.adapters.codex.CodexAdapter`.

Behavior matches :mod:`mock_cursor` / :mod:`mock_claude` with
codex-specific argv parsing:

- ``codex exec`` is the canonical sub-command; the mock tolerates its
  absence (``exec`` is treated as a no-op token).
- ``--sandbox`` accepts ``read-only|workspace-write|danger-full-access``
  per spec §3.2; an unrecognised value results in exit code 2 and a
  stderr complaint (mirrors the real CLI's validation step).
- Output is plain text (no stream-json by default; codex's real CLI
  also defaults to text).  Same 3-section trailing block.
- Env knobs:
  - ``MOCK_CODEX_ROUND`` — round number.
  - ``MOCK_CODEX_CONTENT`` — body content.
  - ``MOCK_CODEX_EXIT_CODE`` — exit code (overrides invalid-sandbox 2).
  - ``MOCK_CODEX_COMPOSITE_SCORE`` — composite_score.

Test imports::

    from tests.fixtures.mock_cli.mock_codex import run_mock_codex
    cp = run_mock_codex("write tests", sandbox="workspace-write")
    assert cp.returncode == 0
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from tests.fixtures.mock_cli.mock_cursor import (
    DEFAULT_COMPOSITE_SCORE,
    _detect_round_num_from_prompt,
    render_three_section,
)

VALID_SANDBOX_MODES: frozenset[str] = frozenset(
    {"read-only", "workspace-write", "danger-full-access"}
)
"""Per :class:`popolaloom.adapters.codex.CodexAdapter` allowed values."""

DEFAULT_BODY_CONTENT: str = (
    "Mock codex: produced patch for the requested prompt.\n"
    "(diff would be shown here in a real run)"
)


@dataclass
class MockCodexOutputs:
    """Captured outputs of a single mock_codex invocation."""

    stdout: str
    stderr: str
    returncode: int
    argv: list[str]


def _parse_prompt_from_argv(argv: list[str]) -> tuple[str, str | None, str | None]:
    """Extract (prompt, sandbox, error) from a codex-style argv.

    Returns ``(prompt, sandbox, error)`` where ``error`` is non-None
    iff the sandbox value is unrecognised — used by main() to translate
    into a non-zero exit code + stderr.
    """
    if not argv:
        return "", None, None
    sandbox: str | None = None
    error: str | None = None
    skip_next = False
    flag_with_value = {"--sandbox", "--config", "--profile"}
    candidates: list[str] = []
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg == "--sandbox" and i + 1 < len(argv):
            sandbox = argv[i + 1]
            if sandbox not in VALID_SANDBOX_MODES:
                error = (
                    f"mock_codex: invalid --sandbox value: {sandbox!r}; "
                    f"expected one of {sorted(VALID_SANDBOX_MODES)}"
                )
            skip_next = True
            continue
        if arg in flag_with_value:
            skip_next = True
            continue
        if arg.startswith("--") or arg.startswith("-"):
            continue
        if arg == "exec":
            continue
        candidates.append(arg)
    prompt = candidates[-1] if candidates else ""
    return prompt, sandbox, error


def run_mock_codex(
    prompt: str,
    *,
    round_num: int | None = None,
    content: str | None = None,
    exit_code: int = 0,
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None,
    composite_score: float | None = None,
) -> MockCodexOutputs:
    """Programmatic entry — invoke the mock and return captured outputs.

    Args:
        prompt: Prompt body the mock should "see".
        round_num: Override round_num that appears in the first line.
        content: Override the body content.
        exit_code: Process exit code; default 0.
        sandbox: Sandbox mode — only validated for membership in
            :data:`VALID_SANDBOX_MODES`; an invalid value results in
            exit code 2 (mirrors the real CLI's behavior; bypassable
            via the ``exit_code`` arg if a test wants different
            semantics).
        composite_score: Composite gate score; default 0.886.
    """
    body = content if content is not None else os.environ.get(
        "MOCK_CODEX_CONTENT", DEFAULT_BODY_CONTENT
    )
    if round_num is None:
        env_round = os.environ.get("MOCK_CODEX_ROUND")
        round_num = (
            int(env_round)
            if env_round and env_round.isdigit()
            else _detect_round_num_from_prompt(prompt)
        )
    if composite_score is None:
        env_score = os.environ.get("MOCK_CODEX_COMPOSITE_SCORE")
        composite_score = float(env_score) if env_score else DEFAULT_COMPOSITE_SCORE
    stderr = ""
    rc = exit_code
    if sandbox is not None and sandbox not in VALID_SANDBOX_MODES:
        stderr = (
            f"mock_codex: invalid --sandbox value: {sandbox!r}; "
            f"expected one of {sorted(VALID_SANDBOX_MODES)}\n"
        )
        if exit_code == 0:
            rc = 2
    stdout = render_three_section(
        body, round_num=round_num, composite_score=composite_score
    )
    argv: list[str] = ["codex", "exec"]
    if sandbox is not None:
        argv.extend(["--sandbox", sandbox])
    argv.append(prompt)
    return MockCodexOutputs(
        stdout=stdout,
        stderr=stderr,
        returncode=rc,
        argv=argv,
    )


def run_as_subprocess(
    prompt: str,
    *,
    round_num: int | None = None,
    content: str | None = None,
    exit_code: int = 0,
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Convenience wrapper returning a :class:`subprocess.CompletedProcess`."""
    out = run_mock_codex(
        prompt,
        round_num=round_num,
        content=content,
        exit_code=exit_code,
        sandbox=sandbox,
    )
    return subprocess.CompletedProcess(
        args=out.argv,
        returncode=out.returncode,
        stdout=out.stdout,
        stderr=out.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the install_mock_binaries shim."""
    if argv is None:
        argv = sys.argv[1:]
    prompt, sandbox, error = _parse_prompt_from_argv(argv)
    if error:
        sys.stderr.write(error + "\n")
        sys.stderr.flush()
        env_exit = os.environ.get("MOCK_CODEX_EXIT_CODE")
        if env_exit and env_exit.lstrip("-").isdigit():
            return int(env_exit)
        return 2
    env_round = os.environ.get("MOCK_CODEX_ROUND")
    round_num = (
        int(env_round)
        if env_round and env_round.isdigit()
        else _detect_round_num_from_prompt(prompt)
    )
    body = os.environ.get("MOCK_CODEX_CONTENT", DEFAULT_BODY_CONTENT)
    env_score = os.environ.get("MOCK_CODEX_COMPOSITE_SCORE")
    composite_score = float(env_score) if env_score else DEFAULT_COMPOSITE_SCORE
    sys.stdout.write(
        render_three_section(body, round_num=round_num, composite_score=composite_score)
    )
    sys.stdout.flush()
    exit_env = os.environ.get("MOCK_CODEX_EXIT_CODE")
    return int(exit_env) if exit_env and exit_env.lstrip("-").isdigit() else 0


if __name__ == "__main__":
    sys.exit(main())
