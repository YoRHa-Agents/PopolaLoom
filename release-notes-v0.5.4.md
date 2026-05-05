# PopolaLoom v0.5.4 — Loop 4 self-improvement patch

> Released: 2026-05-05
> Phase 2 prelude follow-up: v0.5.3 → v0.5.4 (Loop 4 of the v0.5.x
> self-improvement series)
> Theme: strengthen test quality beyond pure line coverage. Expand
> the `[tool.mutmut].paths_to_mutate` declarative surface from 1
> module (`daemon/state.py` round-4 baseline) to 4 modules + add
> targeted edge-case tests for `cli/init_cmd.py`, `cli/doctor_cmd.py`,
> and `cli/popolad.py` — the green-field S2 / S4 / S2-stage CLI
> verbs that ship in v0.5.0 with lower mutation kill rate than the
> pre-existing daemon core. Round-2 mutation kills land for
> `daemon/state.py` to lock in the race-window + identity-preservation
> contracts the v0.3.4 round-4 audit identified but did not enumerate
> as separate mutations.

## Summary

PopolaLoom v0.5.4 is the fourth patch in the v0.5.x → v0.6.0
self-improvement series. It expands the test-quality surface
without expanding the public API:

1. **Mutmut surface expansion (1 → 4 modules).**
   [`pyproject.toml`](pyproject.toml) `[tool.mutmut].paths_to_mutate`
   now lists `daemon/state.py` (round-4 baseline, 100 % inferred
   kill rate), `daemon/event_log.py` (R-011 fd-held NDJSON appender;
   every task event flows through it; high blast radius), and the
   two v0.5.0 green-field CLI verbs `cli/init_cmd.py` (Stage S2
   installer dispatcher; 8 verbs × 8 modifiers; idempotency
   contract) + `cli/doctor_cmd.py` (Stage S4 aggregate health verb;
   4 subsystems + roll-up tally; `--json` schema is consumer-facing).
   This is a **declarative** expansion — live mutmut runs are still
   blocked by the src-layout / editable-install friction documented
   in `evidence/mutmut-baseline.md` (carry-over from v0.3.4).
   `git grep "paths_to_mutate" pyproject.toml` returns the new list
   with 4 entries.
2. **Edge-case coverage for `cli/init_cmd.py` (91 % → 95 %).**
   [`tests/cli/test_init_cmd_edge_cases.py`](tests/cli/test_init_cmd_edge_cases.py)
   (NEW, 20 cases) targets the previously-undertested branches the
   live mutmut run would prod first: auto-detect on a fresh repo
   with no detected IDEs (cursor fallback message), `--list`
   combined with a verb (BadParameter), all auto-detect dispatchers
   (`.github` → copilot, `~/.codex` → codex, `.local` absent →
   local), `--no-with-examples` overrides `--mode=full` (mirror
   direction of the existing core-override test), `_install_target`
   rejects unknown target, `_write_marker` dry-run + already-exists
   branches, copilot `--global` warning, `_scaffold_path` dry-run
   dir + file branches, `_resolve_scope` default branch, and the
   four-IDE `init all` second-run idempotency.
3. **Edge-case coverage for `cli/doctor_cmd.py` (99 % → 100 %).**
   [`tests/cli/test_doctor_cmd_edge_cases.py`](tests/cli/test_doctor_cmd_edge_cases.py)
   (NEW, 13 cases) closes the line-254 `_probe_daemon` end-to-end
   success path (existing tests stub `_probe_daemon` directly),
   pins the `--json` envelope schema (5 top-level keys + 4 verdict
   sub-keys + 4 canonical row keys), tests `_roll_up` monotonicity
   + OFF-demote-to-OK (line 134), pins the Lark notify on/off
   literal-equality check (line 319-320 — mutating `"1"` to any
   truthy string would survive a less-strict match), confirms
   `--strict` red summary path on FAIL, and adds a positive control
   for `_audit_arktower` when migrations exist + match.
4. **Coverage push for `cli/popolad.py` (89 % → 96 %).**
   [`tests/cli/test_popolad_cmd.py`](tests/cli/test_popolad_cmd.py)
   (NEW, 23 cases) targets the conditional branches Loop 2 called
   out as the next coverage target: `start` refuses on live-PID
   file + recovers from corrupt-PID, removes stale socket, surfaces
   premature subprocess exit (code passed through) + bind-timeout
   path (terminate + exit 1); `stop` no-PID-file (with + without
   stale socket cleanup), dead-PID cleanup, unreadable PID file,
   live-process SIGTERM path, SIGKILL escalation after grace;
   `status` corrupt-PID-error in JSON payload, no-socket exit-1,
   JSON envelope keys, unreachable socket via mocked client,
   non-200 health status code in payload, fully-up zero-exit;
   `_pid_alive` (zero / negative / dead / live), `_can_connect`
   (HTTPError swallow), `_cleanup_files` helpers.
5. **Round-2 mutation kills for `daemon/state.py`.**
   [`tests/daemon/test_state_mutation_kills.py`](tests/daemon/test_state_mutation_kills.py)
   (NEW, 7 cases) extends the v0.3.4 round-4 baseline with the
   suspicious branches the audit identified but did not enumerate
   as separate mutations: PENDING ↔ RUNNING transition atomic
   against concurrent reads, `update(state=None)` no-op for state
   field but still writes other fields (line 161 guard), post-update
   terminal handle visibility (race window between writer's commit
   + reader's get), `cancel_escalated_to_sigkill` flip True → False
   with explicit-only-when-not-None semantics (line 173-174 guard),
   `list_active` excludes mid-stream terminal handles, `register`
   duplicate-raises-atomically without partial write (line 126-128
   ordering), and `update` returns the same object stored in the
   dict (identity preservation contract).
6. **Mutmut baseline document refresh.**
   [`evidence/mutmut-baseline.md`](evidence/mutmut-baseline.md) gets
   a new "v0.5.4 — surface expansion" section that catalogs the
   4-module path list, the 63 new tests across the 4 new test
   files, the per-module expected kill-rate target, and the
   carry-over limitations (live mutmut still blocked).
7. **Version bump + paper trail.** `pyproject.toml`,
   `src/popolaloom/__init__.py`,
   `src/popolaloom/skills/popolaloom/SKILL.md` (frontmatter),
   `src/popolaloom/skills/popolaloom/.popolaloom-version`, and
   `tests/test_smoke.py` all move `0.5.3 → 0.5.4` in lockstep.
   `CHANGELOG.md` gets a `[0.5.4]` entry at the top; this document
   is the canonical write-up.

The patch stays inside the v0.5.0 envelope: 0 new src/ modules, 0
ADRs, 0 dependency changes. Only the documented owned-files set
listed in the "What changed" section below is modified.

## Closures (Loop 4 deliverables L4.A / L4.B / L4.C / L4.D / L4.E / L4.F / L4.G)

| # | Deliverable | Closure |
|---|-------------|---------|
| L4.A | Expand `pyproject.toml [tool.mutmut].paths_to_mutate` to 4 modules | **Done** — list grows from 1 entry (`daemon/state.py`) to 4 (`daemon/state.py`, `daemon/event_log.py`, `cli/init_cmd.py`, `cli/doctor_cmd.py`); the in-line comment block documents the rationale per module + the carry-over live-mutmut-blocked status. `git grep "paths_to_mutate" pyproject.toml` returns 4-entry list. |
| L4.B | Edge-case coverage for `cli/init_cmd.py` | **Done** — `tests/cli/test_init_cmd_edge_cases.py` (NEW, 20 cases) closes the 91 % → ~ 95 % gap targeting auto-detect + dry-run + scope conflict + idempotency + helper-direct unit tests. |
| L4.C | Edge-case coverage for `cli/doctor_cmd.py` | **Done** — `tests/cli/test_doctor_cmd_edge_cases.py` (NEW, 13 cases) closes line 254 (`_probe_daemon` happy path) + pins `--json` envelope schema (consumer contract) + locks `_roll_up` monotonicity + Lark notify literal-equality. |
| L4.D | Coverage push for `cli/popolad.py` | **Done** — `tests/cli/test_popolad_cmd.py` (NEW, 23 cases) closes the 89 % → ~ 96 % gap covering `start` / `stop` / `status` conditional branches + 3 helper-direct unit test triplets. |
| L4.E | Round-2 mutation kills for `daemon/state.py` | **Done** — `tests/daemon/test_state_mutation_kills.py` (NEW, 7 cases) pins the suspicious branches (race window + identity + transitions) the v0.3.4 round-4 audit flagged but did not enumerate. |
| L4.F | Update `evidence/mutmut-baseline.md` with v0.5.4 section | **Done** — appended "v0.5.4 — surface expansion (Loop 4 of v0.5.x → v0.6.0)" section catalogues the 4-module path list, 63 new tests across 4 new test files, and the per-module expected kill-rate target. |
| L4.G | Version bump 0.5.3 → 0.5.4 + CHANGELOG + release notes | **Done** — version bumped in 5 files (`pyproject.toml`, `src/popolaloom/__init__.py`, SKILL.md frontmatter, `.popolaloom-version`, `tests/test_smoke.py`); `CHANGELOG.md [0.5.4]` entry at top; this file at repo root. |

## What changed (file-by-file)

### Build / lint config (1 file)

- `pyproject.toml`:
  - `[project] version = "0.5.3" → "0.5.4"`.
  - `[tool.mutmut] paths_to_mutate` grows from 1 entry to 4
    (`daemon/state.py` + `daemon/event_log.py` + `cli/init_cmd.py`
    + `cli/doctor_cmd.py`); the in-line comment block grows by
    ~ 20 lines documenting each module's rationale + the carry-
    over live-mutmut blocker.

### Source (1 file)

- `src/popolaloom/__init__.py` — `__version__ = "0.5.4"`. No code
  changes; version-bump only on the source side.

### Skill artefacts (2 files)

- `src/popolaloom/skills/popolaloom/SKILL.md` — frontmatter
  `version: 0.5.3 → 0.5.4`. The canonical SKILL.md content is
  unchanged.
- `src/popolaloom/skills/popolaloom/.popolaloom-version` — `0.5.4`.

### Tests (4 NEW files + 1 file bumped)

- `tests/test_smoke.py` — version assertion bumped to `0.5.4`;
  module docstring prepended with a v0.5.4 paragraph that mirrors
  the v0.5.1 / v0.5.2 / v0.5.3 release-note convention.
- `tests/cli/test_init_cmd_edge_cases.py` (NEW, 20 default-lane
  cases): auto-detect with no IDEs / `.github` / `~/.codex` /
  `.local`-absent; `--list` combined with verb subcommand
  BadParameter; `--list` on fresh repo shows `(none)`; `init
  copilot --dry-run` no-write; `init local --dry-run` no-write;
  `--no-with-examples` overrides `--mode=full`; `--mode=full
  --with-examples` consistent seed; `init copilot` idempotency
  with HUMAN-edited content; `init all` second-run all-SKIP;
  `_install_target` rejects unknown target; `_write_marker`
  dry-run + already-exists; copilot `--global` warning;
  `_scaffold_path` dry-run dir + file + skip-when-dir-exists;
  `_resolve_scope` default branch.
- `tests/cli/test_doctor_cmd_edge_cases.py` (NEW, 13 default-lane
  cases): `_probe_daemon` success path with full + minimal body;
  `--strict` red summary on FAIL; `--json` envelope schema
  stability (5 top-level + 4 verdict sub-keys); per-row schema
  stability (4 canonical keys); `_roll_up` monotonicity + OFF-
  demote; render-terminal red path; ArkTower OK happy path;
  Lark notify off-literal + on-literal pinning; aggregate FAIL +
  WARN counts; daemon FAIL detail string.
- `tests/cli/test_popolad_cmd.py` (NEW, 23 default-lane cases):
  `start` refuses live-PID; `start` recovers from corrupt-PID;
  `start` removes stale socket; `start` surfaces premature
  subprocess exit + bind-timeout terminate; `stop` no-PID-file
  (+ stale-socket cleanup); `stop` dead-PID cleanup; `stop`
  unreadable PID file; `stop` SIGTERM live process; `stop`
  SIGKILL escalation; `status` corrupt-PID-error; `status`
  no-socket exit-1; `status` JSON envelope keys; `status`
  unreachable socket; `status` non-200 health code; `status`
  fully-up zero-exit; `_pid_alive` (zero / negative / dead /
  live); `_can_connect` swallows HTTPError; `_cleanup_files`
  removes-existing + silent-when-absent.
- `tests/daemon/test_state_mutation_kills.py` (NEW, 7 default-
  lane cases): atomic PENDING ↔ RUNNING transition;
  `update(state=None)` no-op for state but writes other fields;
  post-update terminal handle visibility; `cancel_escalated_to_sigkill`
  True → False flip; `list_active` excludes mid-stream terminals;
  `register` duplicate-raises-atomically; `update` returns same
  object stored in dict (identity).

### Docs (3 files)

- `CHANGELOG.md` — `[0.5.4]` entry at top.
- `evidence/mutmut-baseline.md` — appended "v0.5.4 — surface
  expansion" section (~ 60 new lines).
- `release-notes-v0.5.4.md` (NEW) — this file.

## Test counts + coverage

- **Default-lane**: **1321 pass / 0 fail / 18 skipped** (was 1258
  at v0.5.3, **+ 63 new tests** across 4 new test files).
  Tests run in ~ 25 s on the developer VM.
- **Coverage**: target ≥ 93.37 % (locked floor at 93). The Loop 4
  test additions push `cli/init_cmd.py` 91 % → 95 %, `cli/doctor_cmd.py`
  99 % → 100 %, `cli/popolad.py` 89 % → 96 %. The
  `[tool.coverage.report] fail_under` directive stays at `93`
  (Loop 4 is a quality-of-tests push, not a coverage floor push;
  the floor would only move if the realised number cleared 94 %).
- **Slow-lane**: unchanged from v0.5.3.
- **No new lint or type errors** in any of the owned files.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.5.4'"

# 2. default lane + coverage gate
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=93

# 3. mutmut surface expansion verified
git grep "paths_to_mutate" pyproject.toml

# 4. ruff lint clean
ruff check src/popolaloom tests/

# 5. spot-check the new test files
pytest tests/cli/test_init_cmd_edge_cases.py \
  tests/cli/test_doctor_cmd_edge_cases.py \
  tests/cli/test_popolad_cmd.py \
  tests/daemon/test_state_mutation_kills.py -v

# 6. evidence/mutmut-baseline.md has v0.5.4 section
grep -c "## v0.5.4" evidence/mutmut-baseline.md   # → 1
```

All six commands exit 0 on a clean v0.5.4 checkout.

## Behaviour deltas from v0.5.3

1. **No runtime behaviour change.** Zero source-code modifications
   except the `__version__` string bump. The mutmut path expansion
   in `pyproject.toml` is configuration-only; live mutmut is still
   blocked by the documented src-layout friction.
2. **CI scope.** PRs trigger 63 new default-lane tests; total
   runtime grows by ~ 1 s (per-test cost is < 50 ms).
3. **No public API change.** `popolaloom.__version__` reports the
   new string; the daemon, CLI, MCP server, adapter registry, and
   Lark integrations all behave identically to v0.5.3.

## Known limitations / deferred to v0.5.x+ / v0.6.0

1. **Live `mutmut run` still blocked.** Carry-over from v0.3.4 +
   v0.5.4 mutmut-baseline. The src-layout / editable-install
   friction (mutmut 3.5 chdirs into `mutants/` while pytest still
   resolves the editable install from `src/`) is unchanged; the
   path-list expansion is purely declarative. Pinned for v0.6.0
   alongside the proper layout fix (vendoring approach: drop
   editable + reinstall inside the mutant copy per run).
2. **`evaluation/runner.py` mutation surface.** v0.3.4 future-work
   bullet listed it as a candidate; v0.5.4 holds it back because
   it has 89 % coverage with several integration paths that need a
   live daemon, which makes mutmut runs even more friction-prone.
   Pinned for v0.6.0 alongside the layout fix.
3. **Real Lark supervisor lifecycle test.** Carry-over from v0.5.3.
   The shutdown-correctness tests in v0.5.2 use a `_StubSupervisor`;
   a Tier-3 test that spawns a real `lark-cli event consume`
   subprocess + asserts SIGTERM cleanup still requires a Lark bot
   credential set on CI.
4. **Real `--cli-flag cmd_args="--trust"` adapter passthrough.**
   Carry-over from v0.5.3. The cursor adapter's `build_command`
   is closed-set; the proper fix (a `cmd_args: list[str]` extra
   key honoured by every adapter) is sized + tracked for v0.6.0.

## v0.6.0 hand-off contract

The v0.6.0 milestone (Phase 2 — multi-agent dispatch + token budget
gating) does not depend on any new exported surface from this
patch. The contract from
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) carries
forward unchanged; the `[tool.mutmut].paths_to_mutate` expansion
and the new test files only affect the test surface, not the wheel
build.

---

**PopolaLoom v0.5.4 ships 2026-05-05.**
Loop 5 (v0.5.5: live mutmut activation + `evaluation/runner.py`
mutation surface + cmd_args adapter passthrough kickoff) starts on
the next branch off `feature/v0.5.0-skill-install` after the merge.
