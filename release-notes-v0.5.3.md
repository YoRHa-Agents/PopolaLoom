# PopolaLoom v0.5.3 — Loop 3 self-improvement patch

> Released: 2026-05-05
> Phase 2 prelude follow-up: v0.5.2 → v0.5.3 (Loop 3 of the v0.5.x
> self-improvement series)
> Theme: close the three CI red-build items the Loop 2
> (`feat(v0.5.2)`) push lit up on the GitHub-hosted runner —
> (1) the bare `from arktower.X import Y` imports in
> `tests/test_event_bus.py` + `tests/test_repository.py` that the
> dev VM resolved transparently (it has
> `pip install -e /home/agent/reference/ArkTower`) but the hosted
> runner cannot since v0.5.0 vendored ArkTower under
> `popolaloom._vendored.arktower`; (2) eleven `ruff check` errors,
> ten of them inside the read-only
> `src/popolaloom/_vendored/arktower/` upstream snapshot; and
> (3) the `--cli-flag KEY=VAL` adapter-passthrough docs gap that
> the v0.5.0 functional test
> (`/tmp/popolaloom-skill-functional-test.md`) flagged as the
> highest-value undocumented user surface.

## Summary

PopolaLoom v0.5.3 is the third patch in the v0.5.x → v0.6.0
self-improvement series. It closes the three GA-deferred items
surfaced by v0.5.2 + the CI red build on the
`feature/v0.5.0-skill-install` branch without expanding the
public surface:

1. **`arktower` bare imports rewritten to the vendored path.**
   v0.5.0 (D5.7 LOCKED Path B) replaced
   `arktower @ file:///home/agent/reference/ArkTower` (a direct
   reference) with a vendored snapshot at
   `src/popolaloom/_vendored/arktower/` (pinned to upstream
   commit `467a087`, refresh procedure in
   [`VENDORING.md`](VENDORING.md)). Two test files —
   [`tests/test_event_bus.py`](tests/test_event_bus.py) and
   [`tests/test_repository.py`](tests/test_repository.py) — still
   imported via the bare `arktower.X.Y` upstream layout, which the
   dev VM resolves locally because the developer also kept the
   transient `pip install -e /home/agent/reference/ArkTower` from
   the v0.4.x baseline. The GitHub-hosted runner has neither the
   transient install nor the upstream package on PyPI, so test
   collection crashed with
   `ModuleNotFoundError: No module named 'arktower'` (CI run
   25373295453). v0.5.3 rewrites all 5 import sites to
   `from popolaloom._vendored.arktower.X import Y` so the runner
   stops at zero import errors. `git grep "^from arktower"
   tests/ src/popolaloom/` (excluding `_vendored/`) returns ZERO
   hits after the fix.
2. **Ruff lint clean (eleven → zero).**
   `ruff check src/popolaloom tests/` had been flagging 11
   violations: 1 SIM105 + 4 UP042 + 1 N818 + 4 UP017 inside
   `src/popolaloom/_vendored/arktower/` (upstream code we are
   contractually not allowed to modify per
   [`VENDORING.md`](VENDORING.md)) plus 1 I001 inside our own
   `src/popolaloom/daemon/event_bus.py:55` `if TYPE_CHECKING:`
   block (a stray blank line between two first-party imports).
   v0.5.3 (a) adds `[tool.ruff] extend-exclude =
   ["src/popolaloom/_vendored"]` to
   [`pyproject.toml`](pyproject.toml) — symmetric with the existing
   `[tool.coverage.run] omit = ["src/popolaloom/_vendored/*"]`
   rule that already exempts the vendored copy from our coverage
   gate (the rationale is identical: upstream code with its own
   lint config + test suite, not ours to re-style); (b) fixes the
   lone owned-code I001 in `daemon/event_bus.py` by removing the
   blank line inside `if TYPE_CHECKING:`. After the fix,
   `ruff check src/popolaloom tests/` exits 0.
3. **`--cli-flag KEY=VAL` adapter passthrough documented in
   SKILL.md.** The v0.5.0 functional test
   (`/tmp/popolaloom-skill-functional-test.md`) flagged that the
   `--cli-flag KEY=VAL` repeatable option (R-012 landing in
   v0.2.0; user-spec shorthand "`--extra`") was the most-needed
   undocumented user surface in
   `src/popolaloom/skills/popolaloom/SKILL.md`. v0.5.3 adds
   (a) a Quick Reference table row pointing to Workflow 4 with a
   `popola dispatch ... --cli=cursor --cli-flag
   output_format=stream-json` example; (b) a new **Workflow 4 —
   Adapter-specific arg passthrough (`--cli-flag`)** section
   documenting the JSON-then-string value parser, the supported
   KEYs per adapter (cursor: `output_format` / `cwd_flag` /
   `session_id`; claude: `session_id` / `max_turns`; codex:
   `sandbox`), and 3 worked examples (cursor stream-json + claude
   session_id pre-allocation + codex sandbox lockdown); (c)
   renames the previous "Workflow 4 — Self-eval (PopolaLoom-
   nines)" to "Workflow 5 — Self-eval" with content unchanged.
   Body length grows 10 037 → 12 460 chars — well inside the
   documented `[8 000, 16 000]` budget gate
   ([`tests/cli/test_skill_md_canonical.py::test_skill_md_body_length_in_token_budget`](tests/cli/test_skill_md_canonical.py)).
4. **Version bump + paper trail.** `pyproject.toml`,
   `src/popolaloom/__init__.py`,
   `src/popolaloom/skills/popolaloom/SKILL.md` (frontmatter +
   `token_estimate`),
   `src/popolaloom/skills/popolaloom/.popolaloom-version`, and
   `tests/test_smoke.py` all move `0.5.2 → 0.5.3` in lockstep.
   `CHANGELOG.md` gets a `[0.5.3]` entry at the top; this
   document is the canonical write-up.

The patch stays inside the v0.5.0 envelope: 0 new src/ modules
(only 1 line of source code was touched —
`daemon/event_bus.py:55`), 0 ADRs, 0 dependency changes. Only the
9 owned files listed in the "What changed" section below are
modified.

## Closures (Loop 3 deliverables L3.A / L3.B / L3.C / L3.D / L3.E)

| # | Deliverable | Closure |
|---|-------------|---------|
| L3.A | Fix `from arktower.X import Y` bare imports → vendored path | **Done** — `tests/test_event_bus.py` (3 imports) + `tests/test_repository.py` (3 imports — top-level + 2 inside test functions) rewritten to `from popolaloom._vendored.arktower.X import Y`. Verified via `git grep "^from arktower" tests/ src/popolaloom/` excluding `_vendored/` → ZERO hits, and `git grep "^import arktower" ...` → ZERO hits. |
| L3.B | Ruff lint errors `11 → 0` | **Done** — 10 vendored-code errors deferred via `[tool.ruff] extend-exclude = ["src/popolaloom/_vendored"]` (mirrors the existing coverage exemption); 1 I001 fixed in `src/popolaloom/daemon/event_bus.py` by removing the stray blank line inside the `if TYPE_CHECKING:` block. `ruff check src/popolaloom tests/` exits 0. |
| L3.C | Verify quickstart smoke contract is v0.5.0 6-step (no v0.3.5 leftovers) | **Done** — `tests/matrix/tier5/test_quickstart_smoke.py::test_demo_md_exists_with_v050_sections` already asserts the 6 v0.5.0 markers (`Quickstart walkthrough` / `popola dispatch` / `popola eval run` / `8 dimensions` / `popola init` / `popola doctor`); `docs/DEMO.md` still contains all 6 (the `popola eval run` mention is now an ad-hoc command, not a quickstart step). `examples/quickstart.sh` does NOT contain `popola eval run` (was rewritten to the v0.5.0 6-step contract in v0.5.0 Stage S5). No leftover v0.3.5 contract assertions remain — confirmed by direct grep + read of all `tests/matrix/tier5/` files. |
| L3.D | SKILL.md `--cli-flag` adapter-passthrough docs (table row + Workflow 4) | **Done** — Quick Reference table grows 1 row pointing to Workflow 4; new **Workflow 4 — Adapter-specific arg passthrough (`--cli-flag`)** section documents (a) the JSON-first value parser per `cli/main.py:_parse_cli_flags`, (b) the supported KEYs per adapter (cursor 3 / claude 2 / codex 1), (c) 3 concrete worked examples. Existing Workflow 4 (Self-eval) renumbered to Workflow 5. Body length 10 037 → 12 460 chars (well inside `[8 000, 16 000]`). |
| L3.E | Version bump 0.5.2 → 0.5.3 + CHANGELOG + release notes | **Done** — version bumped in 5 files (`pyproject.toml`, `src/popolaloom/__init__.py`, SKILL.md frontmatter, `.popolaloom-version`, `tests/test_smoke.py`); `CHANGELOG.md [0.5.3]` entry at top; this file at repo root. |

## What changed (file-by-file)

### Build / lint config (1 file)

- `pyproject.toml`:
  - `[project] version = "0.5.2" → "0.5.3"`.
  - `[tool.ruff]` gets a new `extend-exclude =
    ["src/popolaloom/_vendored"]` directive (4 lines including
    the docstring comment explaining the rationale + cross-
    reference to the existing `[tool.coverage.run] omit` rule).
    No other ruff-config changes (selected rules + line-length +
    target-version unchanged).

### Source (2 files)

- `src/popolaloom/__init__.py` — `__version__ = "0.5.3"`. No
  code changes; version-bump only on the source side.
- `src/popolaloom/daemon/event_bus.py` — single blank-line
  removal inside the `if TYPE_CHECKING:` block (line 55, between
  `from popolaloom._vendored.arktower.core.models import TaskEvent`
  and `from popolaloom.daemon.event_log import EventLog`). Both
  imports are first-party from isort's perspective, so a stray
  blank line was the only thing distinguishing them as separate
  groups. The fix is a 1-character delta (the newline). No
  runtime behaviour change.

### Skill artefacts (2 files)

- `src/popolaloom/skills/popolaloom/SKILL.md`:
  - Frontmatter `version: 0.5.2 → 0.5.3`,
    `token_estimate: 2800 → 2950` (the new Workflow 4 added
    ~ 2 400 body chars / ~ 600 tokens).
  - Quick Reference table grows by 1 row:
    `popola dispatch ... --cli-flag KEY=VAL` with a
    `popola dispatch ... --cli=cursor --cli-flag
    output_format=stream-json` example, pointing to Workflow 4
    for the full adapter-key matrix.
  - **NEW Workflow 4 — Adapter-specific arg passthrough
    (`--cli-flag`)** with 6-row adapter-key table + 3 worked
    examples (Cursor `output_format=stream-json`, Claude
    `session_id` pre-allocation + `max_turns` cap, Codex
    `sandbox=read-only` lockdown).
  - Previous "Workflow 4 — Self-eval (PopolaLoom-nines)" →
    "Workflow 5 — Self-eval (PopolaLoom-nines)" with content
    unchanged.
- `src/popolaloom/skills/popolaloom/.popolaloom-version` —
  `0.5.3`.

### Tests (3 files)

- `tests/test_event_bus.py` — 3 imports rewritten:
  `from arktower.core.event_bus import EventBus` →
  `from popolaloom._vendored.arktower.core.event_bus import
  EventBus`; same shape for `core.models` (3 names:
  `TaskEvent`, `TaskStatus`, `Trigger`) and `core.task_service`
  (`TASK_TRANSITION_EVENT`).
- `tests/test_repository.py` — 3 imports rewritten:
  `from arktower.core.models import TaskCreate, TaskFilter,
  TaskStatus` → `from popolaloom._vendored.arktower.core.models
  import (TaskCreate, TaskFilter, TaskStatus)`, plus 2 in-
  function imports of `Trigger` rewritten to the vendored path.
- `tests/test_smoke.py` — version assertion bumped to `0.5.3`;
  module docstring prepended with a v0.5.3 paragraph
  enumerating the 3 closures (mirrors the v0.5.1 / v0.5.2
  release-note convention).

### Docs (2 files)

- `CHANGELOG.md` — `[0.5.3]` entry at top.
- `release-notes-v0.5.3.md` (NEW) — this file.

## Test counts + coverage

- **Default-lane**: **1258 pass / 0 fail / 18 skipped**
  (unchanged from v0.5.2 — Loop 3 added 0 new tests; the changes
  are import rewrites + 1 blank-line removal + docs).
  Tests run in ~ 25 s.
- **Coverage**: **93.37 %** (unchanged from v0.5.2 — only 1 line
  of source code was touched and it was the removal of a blank
  line inside an existing `if TYPE_CHECKING:` block).
  `[tool.coverage.report] fail_under` stays at `93` (locked in
  by v0.5.2).
- **Slow-lane**: unchanged.
- **No new lint or type errors** in any of the 9 owned files.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.5.3'"

# 2. default lane + coverage gate (carried forward from v0.5.2)
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=93

# 3. ruff lint clean
ruff check src/popolaloom tests/

# 4. arktower bare-import audit (must be ZERO outside _vendored/)
git grep "^from arktower" tests/ src/popolaloom/ | \
  grep -v _vendored/ | wc -l    # → 0
git grep "^import arktower" tests/ src/popolaloom/ | \
  grep -v _vendored/ | wc -l    # → 0

# 5. SKILL.md canonical contract
pytest tests/cli/test_skill_md_canonical.py -v

# 6. quickstart smoke (slow lane — confirms no v0.3.5 leftovers)
pytest tests/matrix/tier5/test_quickstart_smoke.py -m slow -v
```

All six commands exit 0 on a clean v0.5.3 checkout.

## Behaviour deltas from v0.5.2

1. **No runtime behaviour change.** The 1 line of source code
   touched (`daemon/event_bus.py:55`) is a blank-line removal
   inside an `if TYPE_CHECKING:` block — invisible to the
   runtime interpreter. The arktower import rewrites only affect
   test collection, not anything that ships in the wheel. The
   ruff `extend-exclude` directive only affects the lint scope,
   not the wheel build (the vendored copy stays packaged because
   `[tool.hatch.build.targets.wheel] packages =
   ["src/popolaloom"]` is unchanged).
2. **CI scope.** PRs that previously failed test collection on
   the GitHub-hosted runner because `arktower` is not installed
   there will now pass collection. The lint job stops flagging
   the 10 vendored violations because they are now out of scope
   for our `ruff check`. The default lane behaviour matches the
   v0.5.2 numbers (1 258 pass / 18 skip / coverage 93.37 %).
3. **No public API change.** `popolaloom.__version__` reports
   the new string; the daemon, CLI, MCP server, adapter
   registry, and Lark integrations all behave identically to
   v0.5.2.

## Known limitations / deferred to v0.5.x+

1. **Real Lark supervisor lifecycle test (Tier 3 / `real_lark`)**
   — carried forward from v0.5.2. The shutdown-correctness
   tests added in v0.5.2 use a `_StubSupervisor`; a Tier-3 test
   that spawns a real `lark-cli event consume` subprocess and
   asserts SIGTERM cleanup still requires a Lark bot credential
   set on CI. Tracked for v0.5.4 / Loop 4.
2. **Coverage > 95 % aspirational target** — carried forward
   from v0.5.2. The next coverage push targets the remaining
   yellow modules (`cli/popolad.py` 89 %,
   `lark/renderers/lark.py` 88 %,
   `evolution/skill_inject.py` 88 %). Tracked for v0.5.4 / Loop
   4.
3. **`--cli-flag` true arbitrary passthrough (`cmd_args="--foo
   --bar"`)** — the v0.5.0 functional test specifically asked
   about `cli_args="--trust"` for cursor-agent's non-interactive
   mode. v0.5.3 ships honest documentation: the cursor adapter's
   `build_command` is closed-set (`output_format` / `cwd_flag` /
   `session_id`) and does not currently transparently pass
   arbitrary `cmd_args` through. The Workflow 4 "Tip" callout
   names this gap explicitly and points to the
   `popolaloom._vendored` two-modify path as the workaround.
   The proper fix (a `cmd_args: list[str]` extra key honoured
   by every adapter) is sized + tracked for v0.6.0 alongside the
   `popola relay` CLI verb gap.

## v0.6.0 hand-off contract

The v0.6.0 milestone (Phase 2 — multi-agent dispatch + token
budget gating) does not depend on any new exported surface from
this patch. The contract from
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) carries
forward unchanged; the `--cli-flag` documentation in SKILL.md is
purely additive and uses only the existing public CLI option.

---

**PopolaLoom v0.5.3 ships 2026-05-05.**
Loop 4 (v0.5.4: real Lark supervisor lifecycle test + coverage > 95 % aspirational + `cmd_args` passthrough scoping) starts on the next branch off `feature/v0.5.0-skill-install` after the merge.
