"""Mock ``claude`` CLI for PopolaLoom Tier 2-5 tests.

Simulates ``claude -p <prompt> --output-format stream-json [--verbose]
[--session-id <UUID>] [--bare] [--max-turns N]`` — the argv shape
consumed by :class:`popolaloom.adapters.claude.ClaudeAdapter`.

Behavior matches :mod:`mock_cursor` but reflects claude-style argv:

- Prompt is the value following ``-p`` (or the last positional if no
  ``-p`` was given — tolerant fallback).
- Default ``--output-format stream-json`` matches real claude defaults
  for our adapter; we emit one JSON envelope per line, with a final
  ``{"type": "result", "usage": {...}}`` echo modelled after claude's
  termination event.
- Same trailing 3-section devola-flow contract embedded inside the
  stream-json text envelopes (so inner-gate parsing concatenates them
  correctly).
- Env knobs:
  - ``MOCK_CLAUDE_ROUND`` — override round number.
  - ``MOCK_CLAUDE_CONTENT`` — override body.
  - ``MOCK_CLAUDE_EXIT_CODE`` — override exit code.
  - ``MOCK_CLAUDE_COMPOSITE_SCORE`` — override gate composite.

Test imports::

    from tests.fixtures.mock_cli.mock_claude import run_mock_claude
    cp = run_mock_claude("implement foo", round_num=3)
    assert "[devola-flow:round=3]" in cp.stdout
"""

from __future__ import annotations

import json
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

DEFAULT_BODY_CONTENT: str = (
    "Mock claude: drafted reply for the requested prompt.\n"
    "(structured response would be shown here in a real run)"
)


@dataclass
class MockClaudeOutputs:
    """Captured outputs of a single mock_claude invocation."""

    stdout: str
    stderr: str
    returncode: int
    argv: list[str]


def _parse_prompt_from_argv(argv: list[str]) -> str:
    """Extract the prompt from a claude-style argv (``-p <prompt>``)."""
    if not argv:
        return ""
    for i, arg in enumerate(argv):
        if arg == "-p" and i + 1 < len(argv):
            return argv[i + 1]
    skip_next = False
    flag_with_value = {
        "-p",
        "--output-format",
        "--session-id",
        "--max-turns",
    }
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
        candidates.append(arg)
    return candidates[-1] if candidates else ""


def _detect_output_format(argv: list[str]) -> Literal["text", "stream-json"]:
    """Return ``stream-json`` if ``--output-format stream-json`` present, else ``text``."""
    for i, a in enumerate(argv):
        if a == "--output-format" and i + 1 < len(argv):
            if argv[i + 1] == "stream-json":
                return "stream-json"
            if argv[i + 1] == "text":
                return "text"
    return "text"


def render_claude_stream_json(
    body: str,
    *,
    round_num: int,
    composite_score: float,
    session_id: str = "00000000-0000-4000-8000-000000000001",
    input_tokens: int = 12,
    output_tokens: int = 64,
) -> str:
    """Render the 3-section content as claude-style stream-json envelopes.

    Each line is one JSON object; the final line is a ``{"type": "result"}``
    envelope carrying a fake ``usage`` block (matches claude's drift
    contract, so v0.3.0+ usage-tracking tests have something to read).
    """
    full_text = render_three_section(
        body, round_num=round_num, composite_score=composite_score
    )
    out_lines: list[str] = []
    out_lines.append(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "model": "claude-mock",
            },
            ensure_ascii=False,
        )
    )
    for line in full_text.splitlines():
        out_lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "message": {"content": line},
                },
                ensure_ascii=False,
            )
        )
    out_lines.append(
        json.dumps(
            {
                "type": "result",
                "session_id": session_id,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
                "subtype": "success",
            },
            ensure_ascii=False,
        )
    )
    return "\n".join(out_lines) + "\n"


def run_mock_claude(
    prompt: str,
    *,
    round_num: int | None = None,
    content: str | None = None,
    exit_code: int = 0,
    output_format: Literal["text", "stream-json"] = "stream-json",
    composite_score: float | None = None,
) -> MockClaudeOutputs:
    """Programmatic entry — invoke the mock and return captured outputs.

    See module docstring + :func:`run_mock_cursor` for the broader
    contract.  ``output_format`` defaults to ``stream-json`` because
    that's the real claude default for our adapter.
    """
    body = content if content is not None else os.environ.get(
        "MOCK_CLAUDE_CONTENT", DEFAULT_BODY_CONTENT
    )
    if round_num is None:
        env_round = os.environ.get("MOCK_CLAUDE_ROUND")
        round_num = (
            int(env_round)
            if env_round and env_round.isdigit()
            else _detect_round_num_from_prompt(prompt)
        )
    if composite_score is None:
        env_score = os.environ.get("MOCK_CLAUDE_COMPOSITE_SCORE")
        composite_score = float(env_score) if env_score else DEFAULT_COMPOSITE_SCORE
    if output_format == "stream-json":
        stdout = render_claude_stream_json(
            body, round_num=round_num, composite_score=composite_score
        )
    else:
        stdout = render_three_section(
            body, round_num=round_num, composite_score=composite_score
        )
    argv = ["claude", "-p", prompt, "--output-format", output_format]
    return MockClaudeOutputs(
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
    output_format: Literal["text", "stream-json"] = "stream-json",
) -> subprocess.CompletedProcess[str]:
    """Convenience wrapper returning a :class:`subprocess.CompletedProcess`."""
    out = run_mock_claude(
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
    """CLI entry for the install_mock_binaries shim."""
    if argv is None:
        argv = sys.argv[1:]
    prompt = _parse_prompt_from_argv(argv)
    output_format = _detect_output_format(argv)
    env_round = os.environ.get("MOCK_CLAUDE_ROUND")
    round_num = (
        int(env_round)
        if env_round and env_round.isdigit()
        else _detect_round_num_from_prompt(prompt)
    )
    body = os.environ.get("MOCK_CLAUDE_CONTENT", DEFAULT_BODY_CONTENT)
    env_score = os.environ.get("MOCK_CLAUDE_COMPOSITE_SCORE")
    composite_score = float(env_score) if env_score else DEFAULT_COMPOSITE_SCORE
    if output_format == "stream-json":
        sys.stdout.write(
            render_claude_stream_json(
                body, round_num=round_num, composite_score=composite_score
            )
        )
    else:
        sys.stdout.write(
            render_three_section(
                body, round_num=round_num, composite_score=composite_score
            )
        )
    sys.stdout.flush()
    exit_env = os.environ.get("MOCK_CLAUDE_EXIT_CODE")
    return int(exit_env) if exit_env and exit_env.lstrip("-").isdigit() else 0


if __name__ == "__main__":
    sys.exit(main())
