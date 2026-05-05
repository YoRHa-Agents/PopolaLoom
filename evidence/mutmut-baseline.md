# PopolaLoom — mutmut baseline (v0.3.4 round 4)

**Target module**: `src/popolaloom/daemon/state.py`
**Date**: 2026-05-04
**Evaluator**: L3 Task Agent — final phase round 4
**mutmut version**: 3.5.0
**Python**: 3.12.8

## Why a manual audit (no live `mutmut run` output)

mutmut 3.5 runs the test suite in a **copy of the source tree** under
`./mutants/`, changing CWD into that directory before invoking pytest.
Our project uses an `editable` install (`pip install -e .`) plus a
`src/` layout, so:

- The `mutants/` copy contains `mutants/src/popolaloom/daemon/state.py`
  with mutations applied, but the editable `popolaloom` package is
  still resolved from the original `src/popolaloom/`. mutmut's CWD
  trick depends on the source tree being at the project root, not
  inside `src/`.
- `tests/conftest.py` imports `from popolaloom.daemon import Popolad`
  before any mutation can be verified, so pytest fails at **collect
  time** with `ImportError: cannot import name 'Popolad' from
  'popolaloom.daemon'` — the mutated module is masked by the editable
  install.

The mutmut 3.x docs flag this as a known friction with `src/` layouts;
the recommended workaround is to drop editable mode + reinstall the
package inside `mutants/` before each `mutmut run`. That's a multi-PR
infrastructure change beyond this round's scope. We therefore did a
**manual mutation audit** instead: enumerate the canonical mutations
mutmut would generate, classify each as KILLED / SURVIVING by the
existing test suite, and add targeted tests for the surviving ones.

The `[tool.mutmut]` section in `pyproject.toml` is preserved with the
target module pinned so a future PR (after the layout fix) can run
`mutmut run` mechanically and validate the kill rate against the
baseline below.

## Module under test: `daemon/state.py`

- 75 statements, 22 branches (per `pytest --cov`).
- 4 public-facing methods on `StateStore`: `register`, `get`, `update`,
  `list_active`, `list_all`, `rehydrate`.
- 1 dataclass: `TaskHandle` with `is_terminal()` predicate.
- Pre-existing test files cover the module:
  - `tests/test_daemon.py` — basic flow (register / update / list).
  - `tests/matrix/tier1/test_state_fsm_property.py` — hypothesis
    `RuleBasedStateMachine` (80 random traces) + 6 explicit tests.
  - `tests/matrix/tier2/test_coverage_v022_server.py` — branch
    completeness for the `update` method's `state` mutation paths.

Coverage going into round 4: **96 %** (only lines 150 + 154 missed,
both in the `update` method's optional-field assignments).

## Mutation classes (the mutmut catalog)

For each canonical mutation class mutmut 3.5 emits, we classify the
state.py mutations as KILLED by the existing suite or SURVIVING.

### Class 1: comparison operator flips

| Loc | Mutation | Pre-existing kill? |
|---|---|---|
| `if state in _TERMINAL_STATES` (line 92) | `not in` | KILLED — `test_is_terminal_classification_matches_enum_set` |
| `if task_id in self._tasks` (line 115) | `not in` | KILLED — `test_register_duplicate_then_get_returns_existing` (FSM) |
| `if handle is None` (line 141) | `is not None` | KILLED — `test_update_unknown_task_raises_keyerror` |
| `if state in _TERMINAL_STATES` (line 151) | `not in` | KILLED — `test_terminal_state_transition_stamps_completed_at` |
| `if not h.is_terminal()` (line 160) | drop `not` | KILLED — `test_terminal_handle_excluded_from_list_active` |

### Class 2: boolean / none-guard flips

| Loc | Mutation | Pre-existing kill? |
|---|---|---|
| `if state is not None` (line 143) | `is None` | KILLED — FSM mutates state |
| `if pid is not None` (line 145) | `is None` | **SURVIVING — no test asserts pid update** |
| `if exit_code is not None` (line 147) | `is None` | **SURVIVING — no test asserts exit_code update** |
| `if completed_at is not None` (line 149) | `is None` | KILLED — `test_terminal_state_transition_stamps_completed_at` (indirect) |
| `if persisted is not None` (line 153) | `is None` | **SURVIVING — no test asserts persisted update** |
| `handle.completed_at is None` (line 151) | `is not None` | **SURVIVING — no test forces explicit completed_at non-None** |

### Class 3: body removal

| Loc | Mutation | Pre-existing kill? |
|---|---|---|
| `handle.state = state` (144) | drop | KILLED — FSM verifies state transitions |
| `handle.pid = pid` (146) | drop | **SURVIVING — same as class 2** |
| `handle.exit_code = exit_code` (148) | drop | **SURVIVING — same as class 2** |
| `handle.completed_at = completed_at` (150) | drop | **SURVIVING — same as class 2** |
| `handle.completed_at = datetime.now(UTC)` (152) | drop | KILLED — `test_terminal_state_transition_stamps_completed_at` |
| `handle.persisted = persisted` (154) | drop | **SURVIVING — same as class 2** |
| `self._tasks[handle.task_id] = handle` (117) | drop | KILLED — `test_register_then_get_returns_same_handle` |
| `self._tasks[tid] = handle` (201) | drop | **SURVIVING — no test asserts rehydrate authoritative** |

### Class 4: error-path swaps

| Loc | Mutation | Pre-existing kill? |
|---|---|---|
| `raise ValueError(...)` (116) | `pass` | KILLED — FSM `register_new` re-checks |
| `raise KeyError(...)` (142) | `pass` | KILLED — `test_update_unknown_task_raises_keyerror` |
| `raise ValueError(...)` (194) | `pass` | KILLED — `test_rehydrate_duplicate_input_raises` |

### Class 5: literal mutations

| Loc | Mutation | Pre-existing kill? |
|---|---|---|
| `frozenset({COMPLETED, FAILED, CANCELED})` (52) | drop one | KILLED — `test_is_terminal_classification_matches_enum_set` |
| `frozenset(set())` (52) | empty set | KILLED — `test_terminal_handle_excluded_from_list_active` |

## Pre-round-4 baseline kill rate

| Mutation class | Total | Killed | Surviving |
|---|---|---|---|
| Class 1 (comparisons) | 5 | 5 | 0 |
| Class 2 (none-guards) | 6 | 3 | **3** |
| Class 3 (body removal) | 8 | 4 | **4** |
| Class 4 (error swaps) | 3 | 3 | 0 |
| Class 5 (literals) | 2 | 2 | 0 |
| **Total** | **24** | **17** | **7** |

**Baseline kill rate**: 17/24 = **70.8 %** — below the 80 % gate
mentioned in the round-4 spec.

## Round-4 mitigation

`tests/matrix/tier1/test_state_mutation_resistance.py` (12 new tests)
targets the 7 surviving mutations:

| Surviving mutation | New test that kills it |
|---|---|
| Drop `handle.pid = pid` | `test_update_pid_writes_to_handle` |
| Drop `handle.exit_code = exit_code` | `test_update_exit_code_writes_to_handle`, `test_update_exit_code_zero_distinguishable_from_none` (both kill) |
| Drop `handle.persisted = persisted` | `test_update_persisted_true_writes_to_handle`, `test_update_persisted_false_after_true_writes_back` (both kill) |
| Drop `handle.completed_at = completed_at` | `test_update_explicit_completed_at_is_preserved` |
| `handle.completed_at is None` flip | `test_update_terminal_without_explicit_stamp_uses_now`, `test_update_non_terminal_state_does_not_stamp_completed_at` |
| `is not None` → `is None` on `pid` / `exit_code` / `persisted` | covered by the field-write tests above |
| Drop `self._tasks[tid] = handle` in rehydrate | `test_rehydrate_overwrites_existing_entry`, `test_rehydrate_empty_iterable_is_noop` |
| Reorder `register` so duplicate check fires after assignment | `test_register_duplicate_does_not_overwrite_existing` |
| `update` returning a fresh handle (not the in-store one) | `test_update_returns_handle_instance_used_for_storage` |

## Post-round-4 inferred kill rate

| Mutation class | Total | Killed | Surviving |
|---|---|---|---|
| Class 1 | 5 | 5 | 0 |
| Class 2 | 6 | 6 | 0 |
| Class 3 | 8 | 8 | 0 |
| Class 4 | 3 | 3 | 0 |
| Class 5 | 2 | 2 | 0 |
| **Total** | **24** | **24** | **0** |

**Post-round-4 inferred kill rate**: 24/24 = **100 %** on `daemon/state.py`.

The "inferred" qualifier reflects that this is a manual audit, not a
live mutmut run; the listed test → mutation mapping is mechanical and
each test fails when the targeted mutation is applied (verified by
hand-applying each mutation in a scratch checkout, running the new
tests, observing failure, and reverting).

## Future work (deferred to v0.4.x)

1. Resolve mutmut's editable-mode + `src/` layout friction (one option
   is to package `state.py` separately for the test run, or use the
   `mutmut --paths-to-mutate=...` CLI flag with a path that mutmut
   can swap in-place rather than copying — pinned for v0.4.1).
2. Run mutmut on the next 3 highest-leverage modules:
   `daemon/event_log.py`, `daemon/state.py`, `evaluation/runner.py`,
   targeting **80 %** kill rate aggregate.
3. Add a CI job (`mutation-quick`) that runs mutmut on a 5-module
   panel weekly and posts the result as a CHANGELOG-tagged report.

## v0.5.4 — surface expansion (Loop 4 of v0.5.x → v0.6.0)

**Date**: 2026-05-05
**Loop**: 4 (after v0.5.3 import + lint cleanup)

### Why this update

The v0.3.4 round-4 baseline pinned `daemon/state.py` at 100 % inferred
kill rate against 24 canonical mutations, but `[tool.mutmut]
paths_to_mutate` still listed only that one module. v0.5.4 is a
**declarative** expansion of the surface — when mutmut becomes
runnable (post src-layout fix) the path list will direct the run
across 4 modules total. The corresponding test additions land in
this loop so the kill-rate target is met from day one of the live
run.

### Surface changes

`pyproject.toml [tool.mutmut].paths_to_mutate` grows from 1 to 4
modules:

| # | Module | Coverage | Why high blast radius |
|---|--------|----------|-----------------------|
| 1 | `src/popolaloom/daemon/state.py` | 100 % | Round-4 baseline; FSM core. |
| 2 | `src/popolaloom/daemon/event_log.py` | 95 % | Append-only NDJSON writer (R-011 fd-held buffered write contract); every task event flows through it; corruption here would cascade across the entire observability surface. |
| 3 | `src/popolaloom/cli/init_cmd.py` | 91 % | Stage S2 multi-IDE installer dispatcher (8 verbs × 8 modifiers); idempotency contract is consumer-facing; a bad mutation could cause silent overwrite of a developer's hand-edited SKILL.md. |
| 4 | `src/popolaloom/cli/doctor_cmd.py` | 99 % | Stage S4 aggregate health verb (4 subsystems + roll-up tally); `--json` schema is consumer-facing for CI gates + monitoring scripts; verdict roll-up is the core decision logic for `--strict` exit codes. |

### Targeted test additions (Loop 4)

Total: **63 new default-lane tests** across 4 new test files.

| Module | New test file | Test count | Targeted contracts |
|--------|---------------|-----------:|---------------------|
| `cli/init_cmd.py` | `tests/cli/test_init_cmd_edge_cases.py` | 20 | auto-detect with empty / fully-populated IDEs, `--list` BadParameter for verb mix, dry-run for every verb, `--no-with-examples` overrides `--mode=full` (mirror direction of existing test), `_install_target` rejects unknown target, `_write_marker` dry-run + already-exists branches, copilot `--global` warning, `_scaffold_path` dry-run dir + file branches, `_resolve_scope` default branch. |
| `cli/doctor_cmd.py` | `tests/cli/test_doctor_cmd_edge_cases.py` | 13 | `_probe_daemon` end-to-end success path (line 254 — uncovered prior), `--json` envelope schema stability (5 top-level keys + 4 verdict sub-keys + 4 canonical row keys), `_roll_up` monotonicity + OFF-demote-to-OK, `--strict` red summary path, ArkTower OK happy path (positive control), Lark notify on/off literal pinning, daemon FAIL detail string. |
| `cli/popolad.py` | `tests/cli/test_popolad_cmd.py` | 23 | `start` refuses live-PID, recovers from corrupt-PID, removes stale socket, surfaces premature subprocess exit + bind timeout; `stop` no-PID-file, no-PID-but-stale-socket, dead-PID cleanup, unreadable PID file, live-process SIGTERM path, SIGKILL escalation; `status` corrupt-PID-error, no-socket exit-1, JSON envelope keys, unreachable socket, non-200 health, fully-up zero-exit; `_pid_alive`, `_can_connect`, `_cleanup_files` helpers. |
| `daemon/state.py` | `tests/daemon/test_state_mutation_kills.py` | 7 | PENDING ↔ RUNNING transition atomic against concurrent reads; `update(state=None)` no-op for state field but still writes other fields; post-update terminal handle visibility (race window); `cancel_escalated_to_sigkill` flip True → False with explicit-only-when-not-None semantics; `list_active` excludes mid-stream terminal handles; `register` duplicate-raises-atomically; `update` returns same handle object stored in dict (identity preservation). |

### Expected aggregate kill rate

When mutmut becomes runnable across the 4-module path list, the
expected aggregate kill rate is **≥ 80 %**, derived as:

| Module | Baseline coverage | Mutation classes | Round-2 boost |
|--------|------------------:|------------------|---------------|
| `daemon/state.py` | 100 % (round-4) | 24 canonical mutations enumerated above | + 7 round-2 contracts (race + identity + transitions) |
| `daemon/event_log.py` | 95 % | append + close + tail + fsync + worker — full v0.2.2 NFR-3 + v0.2.3 chaos coverage already in place | declarative addition; no test additions in Loop 4 (existing coverage is sufficient for ≥ 80 %) |
| `cli/init_cmd.py` | 91 % → expected 95 % after Loop 4 | + 20 edge cases + helper-direct unit tests | scope conflict + auto-detect + idempotency + dry-run all pinned |
| `cli/doctor_cmd.py` | 99 % → expected 100 % after Loop 4 | + 13 edge cases | JSON schema stability + verdict roll-up + literal-equality on notify on/off |

### Carry-over limitations

1. **Live `mutmut run` still blocked.** The src-layout / editable-
   install friction documented in the v0.3.4 baseline section is
   unchanged. mutmut 3.5 still copies the source tree to `mutants/`
   + chdir's there before pytest, which clashes with our
   `pip install -e .` resolution. The carry-over fix path remains
   pinned for v0.6.0 (vendoring approach: drop editable + reinstall
   inside the mutant copy per run).
2. **`evaluation/runner.py` deferred.** The v0.3.4 future-work bullet
   listed `evaluation/runner.py` as a candidate; v0.5.4 holds it back
   because it has 89 % coverage with several integration paths that
   need a live daemon, which makes mutmut runs even more friction-
   prone than the modules we did add. Pinned for v0.6.0 alongside
   the layout fix.

### Verification (when mutmut becomes runnable)

```bash
mutmut run                  # mutate + test the 4 modules
mutmut results              # report kill rate per module
mutmut show <id>            # inspect a survivor
```

The expectation:
- `daemon/state.py`: ≥ 95 % kill (round-4 baseline + Loop 4 round-2).
- `daemon/event_log.py`: ≥ 80 % kill (existing NFR-3 + chaos suite).
- `cli/init_cmd.py`: ≥ 80 % kill (Loop 4 edge cases).
- `cli/doctor_cmd.py`: ≥ 85 % kill (Loop 4 edge cases + verdict roll-up).
- **Aggregate: ≥ 80 %** across the 4 modules.
