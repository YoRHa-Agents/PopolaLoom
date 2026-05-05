# mock CLI library — `tests/fixtures/mock_cli/`

> Per `.local/memory/specs/popolaloom/testing-matrix.md` §4 + §5.2 — the
> three-piece set (`mock_cursor` / `mock_claude` / `mock_codex`) replaces
> the real `cursor-agent` / `claude` / `codex` binaries in PopolaLoom
> Tier 2-5 tests so the suite runs **without touching real LLM calls**.

This library is consumed by:

- **Tier 4** (`tests/matrix/tier4/`) — real langgraph subgraph + HITL
  interrupt-resume + mock CLI dispatch contract verification.
- **Tier 5** (`tests/matrix/tier5/`) — full self-evolution dry-run
  (mock dispatch → attach → event log → arktower → nines).
- **`tests/self_bootstrap/{S2,S4,S5}_*_mock.py`** — the mock versions
  of S2 (reinforcement), S4 (8h offline), S5 (cross-CLI handoff).

Three real CLIs are simulated, plus an `install_mock_binaries(bin_dir)`
helper for tests that need an actual executable on `$PATH` (so a real
popolad subprocess can `shutil.which("cursor-agent")` it).

---

## 1. Public API

```python
from tests.fixtures.mock_cli import (
    run_mock_cursor,
    run_mock_claude,
    run_mock_codex,
    install_mock_binaries,
)
```

### 1.1 `run_mock_cursor(prompt, *, round_num=None, content=None, exit_code=0, output_format="text", composite_score=None) -> MockCursorOutputs`

Programmatic entry that returns a dataclass with `stdout`, `stderr`,
`returncode`, `argv`.  Useful in unit / Tier 4 tests that don't need a
real subprocess.

| Arg               | Type         | Default                                      | Notes                                                                              |
| ----------------- | ------------ | -------------------------------------------- | ---------------------------------------------------------------------------------- |
| `prompt`          | `str`        | (required)                                   | Prompt body; used to derive `round_num` from a Workflow Context block if present.  |
| `round_num`       | `int \| None` | parsed from prompt, env, or `1`             | Overrides round number in the first line `[devola-flow:round=N]`.                  |
| `content`         | `str \| None` | env `MOCK_CURSOR_CONTENT` or default         | Body content placed between the round marker and the trailing 3 sections.          |
| `exit_code`       | `int`         | `0` (env: `MOCK_CURSOR_EXIT_CODE`)          | Returned as `MockCursorOutputs.returncode`.                                        |
| `output_format`   | `"text"|"stream-json"` | `"text"`                            | Stream-json mode emits one JSON envelope per line.                                 |
| `composite_score` | `float \| None`| env `MOCK_CURSOR_COMPOSITE_SCORE` or `0.886` | Gate composite score; default is above the 0.85 inner-gate threshold (PASS).       |

### 1.2 `run_mock_claude(prompt, ...)`

Same shape as `run_mock_cursor` but defaults `output_format` to
`"stream-json"` (matches the real claude default for the adapter) and
emits claude-style envelopes (`{"type": "assistant", "message": {...}}`
per line; final `{"type": "result", "usage": {...}}`).

Env vars: `MOCK_CLAUDE_ROUND`, `MOCK_CLAUDE_CONTENT`,
`MOCK_CLAUDE_EXIT_CODE`, `MOCK_CLAUDE_COMPOSITE_SCORE`.

### 1.3 `run_mock_codex(prompt, *, sandbox=None, ...)`

Same shape as `run_mock_cursor` but accepts a `sandbox` arg matching
codex's three modes:

- `read-only`
- `workspace-write`
- `danger-full-access`

Any other value results in exit code 2 + a stderr complaint (matches
the real CLI's validation).

Env vars: `MOCK_CODEX_ROUND`, `MOCK_CODEX_CONTENT`,
`MOCK_CODEX_EXIT_CODE`, `MOCK_CODEX_COMPOSITE_SCORE`.

### 1.4 `install_mock_binaries(bin_dir) -> dict[str, Path]`

Materialises the 3 mocks as executable files in `bin_dir`:

- `bin_dir/cursor-agent` — invokes `python -m
  tests.fixtures.mock_cli.mock_cursor`.
- `bin_dir/claude` — invokes `python -m
  tests.fixtures.mock_cli.mock_claude`.
- `bin_dir/codex` — invokes `python -m
  tests.fixtures.mock_cli.mock_codex`.

Each shim prepends the workspace `src/` and project root onto
`PYTHONPATH` so the spawned interpreter can import both `popolaloom`
and `tests.fixtures.mock_cli.*`.

Tests typically:

```python
bin_dir = tmp_path / "bin"
install_mock_binaries(bin_dir)
env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
# ... spawn popolad with this env; CursorAdapter resolves to mock ...
```

---

## 2. Output contract — devola-flow 3-section block

Per testing-matrix.md §4.4 + roadmap §11.1 L3 output contract, every
mock invocation produces stdout shaped like:

```
[devola-flow:round=N]

(mock body content here — controllable via env or `content` arg)

## Acceptance Verification
- AC-1: ...
- AC-2: ...

## Gate Score Components
- test_quality: 0.92
- code_review: 0.88
- architecture: 0.85
- benchmark: 0.90
- composite: 0.886

## Findings
- info: mock_cursor emitted devola-flow contract
- ...
```

The first line `[devola-flow:round=N]` and the three trailing section
headers are **required by the inner-gate parser** (planned v0.3.0
F2.5 wiring); their presence is asserted by Tier 4 contract tests.

For `mock_claude` in stream-json mode the same content is emitted
inside `{"type": "assistant", "message": {"content": "..."}}`
envelopes, one line per text line.

---

## 3. When to use which mock

| Scenario                                                             | Recommended API                                            |
| -------------------------------------------------------------------- | ---------------------------------------------------------- |
| Unit test asserting the mock's own output shape (Tier 1 / Tier 4 schema). | `run_mock_*(prompt, ...)` — fastest, no subprocess overhead. |
| Adapter integration test: argv → CompletedProcess (Tier 2).         | `run_as_subprocess(prompt, ...)` — same shape as `subprocess.run`.|
| End-to-end test driving real popolad subprocess (Tier 4 / Tier 5).   | `install_mock_binaries(bin_dir)` + prepend on `$PATH`.      |
| Test asserting argv-shape parity with real CLI (Tier 4 contract).    | `run_mock_*` and inspect `MockCursorOutputs.argv`.          |

---

## 4. Real-CLI argv parity table

| Real CLI                          | Mock argv (default)                                              | Notes                                              |
| --------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------- |
| `cursor-agent agent --print PROMPT`            | `["cursor-agent", "agent", "--print", "--output-format", "text", PROMPT]`            | Matches CursorAdapter.build_command shape.        |
| `cursor-agent agent --print --output-format stream-json PROMPT` | `["cursor-agent", "agent", "--print", "--output-format", "stream-json", PROMPT]`     | Stream-json emits NDJSON envelopes.               |
| `claude -p PROMPT --output-format stream-json` | `["claude", "-p", PROMPT, "--output-format", "stream-json"]`                          | Default mode for ClaudeAdapter.                   |
| `codex exec PROMPT`                            | `["codex", "exec", PROMPT]`                                                         | No sandbox flag.                                  |
| `codex exec --sandbox workspace-write PROMPT`  | `["codex", "exec", "--sandbox", "workspace-write", PROMPT]`                          | Sandbox value validated against the 3-mode whitelist. |

---

## 5. Drift detection (per R-EVO-2)

Per testing-matrix.md §4.6, the v0.3.0+ `tests/matrix/real_cli/` smoke
suite is meant to compare these mocks against the real binaries weekly.
For v0.2.3 the comparison is **schema-only** (3-section block presence
+ argv-shape parity); usage-token equivalence is deferred to v0.3.0
when `usage_tokens` becomes part of the inner-gate scoring.

If you change a mock's output, also update:

- `tests/matrix/tier4/test_real_langgraph_subgraph.py` (snapshot).
- The relevant `tests/matrix/real_cli/test_real_*_smoke.py`
  invariants — they assert only the 3-section presence so the diff
  should be small.

---

## 6. Schema-only v0.2.3 placeholders

The following modules ship in `src/` purely as schema-only placeholders
that the mock library writes against — full wiring lands in v0.3.0:

- `src/popolaloom/hitl/__init__.py` — `HITLPrompt`, `HITLOption`,
  `ArtifactRef` Pydantic v2 schemas (see
  `tests/matrix/tier1/test_hitl_prompt_schema.py`).
- `src/popolaloom/evolution/__init__.py` — `WorkflowContext` Pydantic
  v2 schema (see
  `tests/matrix/tier1/test_devolaflow_context_schema.py`).

The mock CLI doesn't import either schema (it uses environment
variables + plain strings), but the trailing 3-section block aligns
with what `WorkflowContext` describes so the v0.3.0 wiring is a no-op
on the mock side.
