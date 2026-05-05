# PopolaLoom v0.5.5 — Loop 5 self-improvement patch (final patch before v0.6.0)

> Released: 2026-05-06
> Phase 2 prelude polish: v0.5.4 → v0.5.5 (Loop 5 of the v0.5.x
> self-improvement series; final patch before the v0.6.0 minor
> consolidation).
> Theme: polish what Loops 1–4 built + close the highest-priority
> known limitations carried forward across loops. README + DEMO get
> the v0.5.x evolution table; `popola init` learns an
> `--interactive` wizard for human-driven setup; the `[tool.mutmut]
> .paths_to_mutate` declarative surface grows from 4 to 5 modules
> (closes the v0.5.4 future-work bullet for `evaluation/runner.py`);
> a vendored ArkTower migration test suite lands; a final coverage
> push lifts default-lane 93.94 → 94.60 % and bumps the floor to 94.

## Summary

PopolaLoom v0.5.5 is the fifth and final patch in the v0.5.x →
v0.6.0 self-improvement series. It is the polish loop: every
deliverable closes a known limitation carried forward from a
previous loop or pulls a "user-facing rough edge" forward into
ready-for-v0.6.0 condition:

1. **README + DEMO refresh from loop learnings.** Updates the
   `README.md` Status table with rows for v0.5.{1,2,3,4,5} (per the
   actual coverage + test count), adds a "Loop-driven self-
   improvement" section explaining the v0.5.x pattern (5 loops →
   v0.6.0 consolidation), refreshes the verification commands to
   match the post-v0.5.5 floor (`fail_under = 94`), and adds the
   `popola init --interactive` example to the quickstart. The
   `docs/DEMO.md` gets a new "v0.5.x evolution walkthrough" section
   with the per-loop closure table (Loop / Version / Closure /
   Tests Δ / Coverage Δ) plus a worked demo of the new wizard.
   Existing v0.4.0 + v0.5.0 walkthroughs are preserved (additive
   only).
2. **`popola init --interactive` wizard.** Adds an `--interactive`
   flag to the root `popola init` callback in
   [`src/popolaloom/cli/init_cmd.py`](src/popolaloom/cli/init_cmd.py).
   When set, walks the operator through a wizard
   (auto-detect IDEs → confirm install per IDE → choose scope →
   confirm plan → execute) using `typer.confirm` + `typer.prompt`
   for I/O. The flag is mutually-exclusive with `--list` and verb
   subcommands (mixing them raises `BadParameter`); the
   non-interactive path remains the default for CI scripts. Shipped
   with 6 new default-lane tests in
   [`tests/cli/test_init_interactive.py`](tests/cli/test_init_interactive.py)
   (NEW) using `CliRunner.invoke(..., input="...")` per the Typer
   testing docs.
3. **`evaluation/runner.py` mutation surface declaration.** Closes
   the v0.5.4 future-work bullet (release-notes-v0.5.4.md §2): the
   [`pyproject.toml`](pyproject.toml) `[tool.mutmut].paths_to_mutate`
   list grows from 4 to 5 entries by adding
   `src/popolaloom/evaluation/runner.py`. The 8-dim PopolaLoom-nines
   scorer orchestrator is now declared mutation surface; a
   regression in `_score_<dim>()` boundary handling silently corrupts
   every reported nines score, which downstream
   `evolution/dual_gate.py` consumes as the outer-gate Δ ≥ 0.02
   input. 9 new boundary tests land in
   [`tests/test_evaluation_mutation_kills.py`](tests/test_evaluation_mutation_kills.py)
   (NEW) covering: zero-evidence placeholder for every scorer;
   partial-evidence interpolation; full-evidence ↦ composite =
   sum(weights); composite boundaries at 0.85 / 0.90 / 0.95
   (the canonical dual-gate cutoffs);
   `_load_weights` fallback paths (missing TOML, unparseable TOML,
   non-table `[eval] weights = "..."` shape); `_iso_utc` UTC
   normalisation of naive timestamps; `collect_evidence` zero-files
   when dir missing. Live mutmut runs remain blocked by the
   src-layout / editable-install friction documented in
   `evidence/mutmut-baseline.md` (carry-over from v0.3.4 + v0.5.4);
   v0.5.5 is a declarative + targeted-test bump.
4. **Vendored ArkTower migration test suite.** Closes the prior-
   plan carry-over with 4 new cases in
   [`tests/test_vendored_arktower_migrations.py`](tests/test_vendored_arktower_migrations.py)
   (NEW): (a) the vendored package + 4 subpackages all import
   cleanly; (b) the two PopolaLoom-owned migrations
   (`migrations/005_popolaloom_extensions.sql` +
   `migrations/006_popola_hitl.sql`) exist + create their respective
   tables (`popola_dispatch` + `popola_hitl`) when applied against
   an in-memory SQLite DB; (c) the vendored `MigrationRunner`
   applies the 4 ArkTower migrations end-to-end + populates
   `schema_version` rows for versions 1..4 + idempotent re-runs
   are a no-op; (d) the `POPOLA_ARKTOWER_MIGRATIONS_DIR` env-var
   override is honoured when the path is valid + falls back to the
   vendored sibling when bogus or unset.
5. **Final coverage push toward 94 %+.** 28 new tests land in
   [`tests/test_coverage_v055_push.py`](tests/test_coverage_v055_push.py)
   (NEW) targeting the LAST missing branches the v0.5.4 term-missing
   report flagged: `cli/_skill_source.py` placeholder-stub
   fallback + `canonical_source_path` not-a-file branch;
   `evaluation/dimensions/dispatch_isolation.py` `_safe_getpgid`
   None / TypeError edges + PID-only fallback; `single_threaded_writes.py`
   `OSError` on read + `ImportError` of popolaloom; `evolution/skill_inject.py`
   unknown-target / unsupported-scope KeyError + `$HOME` env override
   + `emit_skill_check_event` None-event-log + append-failure swallow;
   `evolution/skill_upgrade.py` `_read_existing_version` UnicodeDecodeError
   + missing-frontmatter + unclosed-frontmatter + no-version-field
   branches + quoted-version parsing; `cli/skill_cmd.py` status-renderer
   table-action-column branches (SKIP / `?` / UP-TO-DATE / DRIFT / OK
   / MISS). Coverage lifts 93.94 % → 94.60 % (+0.66 pp).
6. **`fail_under` floor bump 93 → 94.** Locks in the L5.E push by
   moving `[tool.coverage.report] fail_under` from 93 to 94 in
   `pyproject.toml`. The new floor is the post-v0.5.5 Phase-2-prelude-
   polish gate; the inline comment block grows by ~ 7 lines documenting
   the v0.5.5 push rationale + the 4 new test files that lifted the
   line count.
7. **Version bump + paper trail.** `pyproject.toml`,
   `src/popolaloom/__init__.py`,
   `src/popolaloom/skills/popolaloom/SKILL.md` (frontmatter +
   `last_updated`),
   `src/popolaloom/skills/popolaloom/.popolaloom-version`, and
   `tests/test_smoke.py` all move `0.5.4 → 0.5.5` in lockstep.
   `CHANGELOG.md` gets a `[0.5.5]` entry at the top; this document
   is the canonical write-up.

The patch stays inside the v0.5.0 envelope on the source side: 0
new src/ modules, 0 new dependencies, 0 ADRs. The init command grows
one new flag (a CLI surface only) + one wizard helper function (no
new public API outside `popolaloom.cli.init_cmd`).

## Closures (Loop 5 deliverables L5.A / L5.B / L5.C / L5.D / L5.E / L5.F)

| #    | Deliverable                                                                | Closure                                                                                                                                                                                                                       |
|------|----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| L5.A | README + DEMO refresh from loop learnings                                  | **Done** — `README.md` Status table grows by 5 rows (v0.5.{1,2,3,4,5}); a "Loop-driven self-improvement" section explains the 5-loop chain; verification commands updated for `fail_under = 94`. `docs/DEMO.md` gets a v0.5.x evolution walkthrough section + a worked `--interactive` wizard demo. v0.4.0 + v0.5.0 walkthroughs preserved. |
| L5.B | `popola init --interactive` wizard                                         | **Done** — `--interactive` flag added to the root callback in `src/popolaloom/cli/init_cmd.py`; wizard uses `typer.confirm` + `typer.prompt`; mutually-exclusive with `--list` + verb subcommands. 6 new tests in `tests/cli/test_init_interactive.py` (NEW) using `CliRunner` + stdin injection.                                            |
| L5.C | `evaluation/runner.py` mutation surface declaration                        | **Done** — `[tool.mutmut].paths_to_mutate` grows from 4 to 5 entries; the inline rationale comment grows by ~ 12 lines. 9 boundary tests in `tests/test_evaluation_mutation_kills.py` (NEW) covering zero / partial / full evidence + composite cutoffs + `_load_weights` fallbacks + `_iso_utc` normalisation.                                |
| L5.D | Vendored ArkTower migration test suite                                     | **Done** — 4 cases in `tests/test_vendored_arktower_migrations.py` (NEW) covering import surface + 005/006 SQL syntax + `MigrationRunner` end-to-end against in-memory SQLite + `POPOLA_ARKTOWER_MIGRATIONS_DIR` env-var override.                                                                                                            |
| L5.E | Final coverage push toward 94 %+                                           | **Done** — 28 new tests in `tests/test_coverage_v055_push.py` (NEW) target the last missing branches across 6 modules. Coverage lifts 93.94 % → 94.60 % (+0.66 pp); `[tool.coverage.report] fail_under` bumped 93 → 94.                                                                                                                       |
| L5.F | Version bump 0.5.4 → 0.5.5 + CHANGELOG + release notes                     | **Done** — version bumped in 5 files (`pyproject.toml`, `src/popolaloom/__init__.py`, SKILL.md frontmatter, `.popolaloom-version`, `tests/test_smoke.py`); `CHANGELOG.md [0.5.5]` entry at top; this file at repo root.                                                                                                                       |

## v0.5.x → v0.6.0 5-loop journey rollup

| Loop | Version | Closure focus                                                                                                                                                            | Default-lane Δ        | Coverage Δ          |
|------|---------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------|---------------------|
| 1    | v0.5.1  | CI runner-writability fix + 90 error-path tests (`tests/cli/test_main_error_paths.py`, `tests/daemon/test_rpc_error_paths.py`, `tests/cli/test_doctor_cmd.py` extensions) | 1104 → 1194 (+90)     | 91.15 % → 92.56 %   |
| 2    | v0.5.2  | NFR-2 / NFR-9 benchmarks + Lark supervisor graceful shutdown + auto-merge gate align (90 → 92)                                                                           | 1194 → 1258 (+64)     | 92.56 % → 93.37 %   |
| 3    | v0.5.3  | vendored arktower CI imports (5 sites) + ruff lint clean (10 vendored excludes + 1 owned fix) + SKILL.md `--cli-flag` Workflow 4 docs                                     | 1258 → 1258 (+0)      | 93.37 % → 93.37 %   |
| 4    | v0.5.4  | mutmut declarative surface 1 → 4 modules + 63 edge-case + mutation-kill tests (`cli/init_cmd.py`, `cli/doctor_cmd.py`, `cli/popolad.py`, `daemon/state.py` round-2)       | 1258 → 1321 (+63)     | 93.37 % → 93.94 %   |
| 5    | v0.5.5  | README/DEMO refresh + `popola init --interactive` + mutmut 4 → 5 + vendored migration tests + coverage push                                                              | 1321 → 1368 (+47)     | 93.94 % → 94.60 %   |
|      |         | **Cumulative: v0.5.0 → v0.5.5**                                                                                                                                          | **+264 tests**        | **+3.45 pp**        |

Total chain: 5 loops, 5 single-commit patches, 264 new default-lane
tests, +3.45 pp coverage, 0 regression weeks, 4 → 5 mutmut surface
modules, 1 new CLI flag (`--interactive`).

## v0.6.0 readiness

The v0.6.0 minor (Phase 2 — multi-agent dispatch + token-budget
gating + per-agent capability registry) does not depend on any new
exported surface from v0.5.5. The contract from
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) carries forward
unchanged; the test-quality bumps + the new `--interactive` flag +
the documentation refresh are pure quality / UX investment.

The v0.5.x → v0.6.0 hand-off contract:

1. **Public Python API:** `popolaloom.__version__ == "0.5.5"`; the
   exports from `popolaloom.cli.init_cmd` (root `app` + the per-IDE
   verb commands) + `popolaloom.cli.doctor_cmd.doctor_command` +
   `popolaloom.cli.skill_cmd.app` + the vendored `popolaloom._vendored
   .arktower.{store,core,cli.deps}` are all stable. v0.6.0 may
   reorganise the daemon-side modules but the CLI surface stays put.
2. **Coverage floor:** `[tool.coverage.report] fail_under = 94`;
   default-lane sits at 94.60 %, so v0.6.0 has 0.60 pp of headroom
   before any new code lowers the realised number to the floor.
3. **Mutmut declarative surface:** 5 modules
   (`daemon/{state,event_log}.py`, `cli/{init_cmd,doctor_cmd}.py`,
   `evaluation/runner.py`); v0.6.0 should add the new multi-agent
   dispatch primitives (`primitives/multi_dispatch.py`?) once those
   land + clear their per-module coverage gates.
4. **Test counts:** 1368 default-lane / 18 skipped (pinning
   external-dep gates) / 82 deselected (slow + chaos + e2e). The
   slow-lane suite is unchanged from v0.5.4.

## What changed (file-by-file)

### Build / lint config (1 file)

- `pyproject.toml`:
  - `[project] version = "0.5.4" → "0.5.5"`.
  - `[tool.mutmut] paths_to_mutate` grows from 4 entries to 5
    (adds `src/popolaloom/evaluation/runner.py`); the inline
    comment block grows by ~ 12 lines documenting the v0.5.5
    rationale + the carry-over live-mutmut blocker.
  - `[tool.coverage.report] fail_under = 93 → 94`; the inline
    comment block grows by ~ 7 lines documenting the v0.5.5
    coverage push rationale.

### Source (2 files)

- `src/popolaloom/__init__.py` — `__version__ = "0.5.5"`. No code
  changes; version-bump only on the source side.
- `src/popolaloom/cli/init_cmd.py` — adds `--interactive` flag to
  the root callback + `_run_interactive_wizard` helper +
  `_prompt_scope` + `_resolve_target_path_for_wizard` private
  helpers (~ 130 LOC). The non-interactive path is unchanged; the
  flag is opt-in.

### Skill artefacts (2 files)

- `src/popolaloom/skills/popolaloom/SKILL.md` — frontmatter
  `version: 0.5.4 → 0.5.5` + `last_updated: 2026-05-05 → 2026-05-06`.
  Body unchanged.
- `src/popolaloom/skills/popolaloom/.popolaloom-version` — `0.5.5`.

### Tests (4 NEW files + 1 file bumped)

- `tests/test_smoke.py` — version assertion bumped to `0.5.5`;
  module docstring prepended with a v0.5.5 paragraph.
- `tests/cli/test_init_interactive.py` (NEW, 6 default-lane cases):
  happy-path with all detected IDEs accepted; decline-all writes
  nothing; `--interactive` + verb subcommand → BadParameter;
  global-scope choice lands under `~/`; operator backs out at
  "Proceed?" cancels the plan; fresh-repo cursor-default fallback.
- `tests/test_evaluation_mutation_kills.py` (NEW, 9 default-lane
  cases): zero-evidence placeholder; partial-evidence interpolation;
  full-evidence composite = sum(weights); composite cutoffs at
  0.85 / 0.90 / 0.95; `_load_weights` 3 fallback paths;
  `_iso_utc` UTC tag for naive datetimes; `collect_evidence` files=0
  when dir missing.
- `tests/test_vendored_arktower_migrations.py` (NEW, 4 default-
  lane cases): vendored imports clean; PopolaLoom 005/006 migrations
  exist + create expected tables; `MigrationRunner` end-to-end
  against in-memory SQLite + idempotency; `POPOLA_ARKTOWER_MIGRATIONS_DIR`
  env-var override valid + bogus + unset paths.
- `tests/test_coverage_v055_push.py` (NEW, 28 default-lane cases):
  targets specific term-missing line ranges across 6 modules
  (see L5.E summary above for the full list).

### Docs (3 files)

- `README.md` — Status table grows by 5 rows; "Loop-driven self-
  improvement" section added; verification commands updated for
  `fail_under = 94`; quickstart adds `--interactive` example;
  install snippet expects `0.5.5`.
- `docs/DEMO.md` — title bumped to v0.3.5 → v0.5.5; new "v0.5.x
  evolution walkthrough" section with the 5-row closure table;
  new "v0.5.5 interactive wizard" section with a worked demo.
- `CHANGELOG.md` — `[0.5.5]` entry at top.
- `release-notes-v0.5.5.md` (NEW) — this file.

## Test counts + coverage

- **Default-lane**: **1368 pass / 0 fail / 18 skipped** (was 1321
  at v0.5.4, **+ 47 new tests** across 4 new test files; exceeds
  the ≥ 30 target by 17).
  Tests run in ~ 26 s on the developer VM.
- **Coverage**: **94.60 %** (was 93.94 % at v0.5.4, **+ 0.66 pp**).
  The `[tool.coverage.report] fail_under` directive moves `93 → 94`
  to lock in the new floor.
- **Slow-lane**: unchanged from v0.5.4.
- **No new lint or type errors** in any of the owned files.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.5.5'"

# 2. default lane + coverage gate (post-v0.5.5: fail_under = 94)
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=94

# 3. mutmut surface verified at 5 entries
git grep "paths_to_mutate" pyproject.toml

# 4. ruff lint clean
ruff check src/popolaloom tests/

# 5. spot-check the new test files
pytest tests/cli/test_init_interactive.py \
  tests/test_evaluation_mutation_kills.py \
  tests/test_vendored_arktower_migrations.py \
  tests/test_coverage_v055_push.py -v

# 6. interactive wizard surfaced in --help
popola init --help | grep -- "--interactive"
```

All six commands exit 0 on a clean v0.5.5 checkout.

## Behaviour deltas from v0.5.4

1. **`popola init --interactive` is a NEW UX surface.** The
   non-interactive path (verbs + flags) is unchanged. CI scripts
   authored against the v0.5.0 API keep working unchanged.
2. **No runtime behaviour change for any existing verb.** The
   `--interactive` callback only fires when explicitly requested;
   omitting the flag preserves the v0.5.4 dispatch logic byte-for-byte.
3. **Mutmut declarative path expansion.** Live mutmut runs are
   still blocked by the documented src-layout friction; the path
   list now declares 5 modules instead of 4.
4. **Coverage floor 93 → 94.** `pytest --cov-fail-under` defaults
   to 94 once `pyproject.toml` is reloaded; CI runs that pin the
   number explicitly (e.g. `automerge.yml --cov-fail-under=92`)
   should be updated in a follow-up.
5. **No public API change.** `popolaloom.__version__` reports the
   new string; the daemon, CLI, MCP server, adapter registry, and
   Lark integrations all behave identically to v0.5.4.

## Known limitations / deferred to v0.6.0

1. **Live `mutmut run` still blocked.** Carry-over from v0.3.4 +
   v0.5.4 + v0.5.5. The src-layout / editable-install friction
   (mutmut 3.5 chdirs into `mutants/` while pytest still resolves
   the editable install from `src/`) is unchanged across all 5
   loops; the path-list expansion is purely declarative across
   loops 4 + 5. Pinned for v0.6.0 alongside the proper layout fix
   (vendoring approach: drop editable + reinstall inside the mutant
   copy per run).
2. **CI workflow `--cov-fail-under` numbers.** `automerge.yml` was
   updated to 92 in v0.5.2; a follow-up should bump it to 94 to
   match the post-v0.5.5 floor. This is a 1-line documentation /
   CI tweak deferred to v0.6.0.
3. **Real Lark supervisor lifecycle test.** Carry-over from v0.5.{2,3,4}.
   The shutdown-correctness tests in v0.5.2 use a `_StubSupervisor`;
   a Tier-3 test that spawns a real `lark-cli event consume`
   subprocess + asserts SIGTERM cleanup still requires a Lark bot
   credential set on CI.
4. **Real `--cli-flag cmd_args="--trust"` adapter passthrough.**
   Carry-over from v0.5.{3,4}. The cursor adapter's `build_command`
   is closed-set; the proper fix (a `cmd_args: list[str]` extra
   key honoured by every adapter) is sized + tracked for v0.6.0.
5. **Wizard `--mode` + `--with-examples` extension.** v0.5.5's
   wizard intentionally focuses on per-IDE confirm + scope choice;
   it does NOT yet collect `--mode={core,standard,full}` or
   `--with-examples` selections (those are still typer.Option
   modifiers on the `init local` verb). v0.6.0 may add a
   "Customize local scaffold?" follow-up question to the wizard
   that exposes them.

## v0.6.0 hand-off contract

The v0.6.0 milestone (Phase 2 — multi-agent dispatch + token budget
gating) does not depend on any new exported surface from this
patch. The contract from
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) carries
forward unchanged; the v0.5.5 additions are:

- One new CLI flag (`popola init --interactive`) — pure UX surface.
- One new declared mutation path (`evaluation/runner.py`) — declarative
  config, no behaviour change.
- Four new test files — test quality investment.
- A coverage floor bump (93 → 94) — gating directive only.

The wheel build, runtime API, and adapter registry are byte-identical
to v0.5.4 except for the `--version` string + the new
`init_callback` parameter list.

---

**PopolaLoom v0.5.5 ships 2026-05-06.**
v0.6.0 (Phase 2 — multi-agent dispatch + token-budget gating + per-
agent capability registry) starts on a fresh `feature/v0.6.0-multi-
agent` branch off `feature/v0.5.0-skill-install` after the merge.
The v0.5.x → v0.6.0 5-loop self-improvement series concludes with
this release.
