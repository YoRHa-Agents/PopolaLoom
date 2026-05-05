# PopolaLoom v0.6.0 — v0.5.x → v0.6.0 self-evolution consolidation

> Released: 2026-05-06
> Theme: 5-loop self-improvement consolidation
> Phase 2 step 1 (per spec §2.3 / `.local/feedbacks/feedback_for_v0.4.0.md`)

## Summary

PopolaLoom v0.6.0 is a **consolidation minor** that closes the v0.5.x →
v0.6.0 self-improvement series and ships the two carry-over deliverables
that Loop 5 (v0.5.5) explicitly deferred. It is *not* a feature minor —
no new daemon primitives, no new public Python APIs, no schema changes.
The 5 v0.5.x patch rounds (v0.5.1 through v0.5.5) drove cumulative
**+264 default-lane tests, +3.45 pp coverage (91.15 % → 94.60 %), +5
mutmut declarative-surface modules, and +1 CLI flag** (`popola init
--interactive`). This minor bundles those round-by-round closures into a
single GA tag, closes the last 2 deferred items
(`automerge.yml --cov-fail-under` 92 → 94 alignment + cursor adapter
`extra["cli_args"]` passthrough), and ships the comprehensive release
notes + CHANGELOG entry that turn the loop chain into a citable artefact.

The minor is a **single commit on `feature/v0.5.0-skill-install`** with
the message `chore(release): v0.6.0 — v0.5.x consolidation + automerge
align + cursor --trust passthrough`. There are no breaking changes; every
caller that worked on v0.5.5 keeps working on v0.6.0 byte-identically.
The new cursor `cli_args` passthrough is purely additive (opt-in via
`--cli-flag cli_args=...` or the `cmd_args` alias), and the
`automerge.yml` gate-floor bump only tightens the merge bar — it cannot
reject a PR that v0.5.5's pyproject already accepted (since the project's
`fail_under` was already at 94 by Loop 5).

## The journey: v0.5.0 → v0.6.0 (5 patch rounds + consolidation)

| Loop | Version | Commit     | Tests           | Coverage        | Closure |
|------|---------|------------|-----------------|-----------------|---------|
| 0    | v0.5.0 GA | `e5c6784` | 1104            | 91.15 %         | Skill + install + popola doctor + canonical SKILL.md |
| 1    | v0.5.1  | `fa9af92`  | 1194 (+90)      | 92.56 % (+1.41) | CI mkdir fix + coverage push + version bump |
| 2    | v0.5.2  | `ab0b9ea`  | 1258 (+64)      | 93.37 % (+0.81) | NFR-2/9 benchmarks + Lark graceful shutdown + automerge alignment |
| 3    | v0.5.3  | `c80aabb`  | (no count regression) | 93.37 %+   | arktower CI imports + ruff lint + SKILL.md `--extra` docs |
| 4    | v0.5.4  | `740d011`  | 1321 (+63)      | 93.94 % (+0.57) | mutmut 1→4 modules + cli edge cases + popolad coverage |
| 5    | v0.5.5  | `3189604`  | 1368 (+47)      | 94.60 % (+0.66) | README/DEMO refresh + popola init --interactive + mutmut 4→5 + final coverage |
| GA   | **v0.6.0** | (this commit) | 1383 (+15)  | 94.62 % (≈)     | automerge align + cursor `cli_args` passthrough + comprehensive release notes |

Cumulative across 5 patch rounds + this consolidation: **+279 tests
(1104 → 1383), +3.47 pp coverage (91.15 → 94.62), +5 mutmut modules
(1 → 5), +1 CLI flag (`popola init --interactive`), all CI green on
hosted runners** (was 100 % red on `/home/agent/` permission error in
the v0.5.0 baseline). The v0.6.0 line in the table reflects only the
incremental L6.B `cli_args` passthrough test addition (15 tests in
`tests/adapters/test_cursor_extra_passthrough.py`); the v0.5.5 → v0.6.0
delta is therefore +15 tests / +0.02 pp coverage / 0 new src modules.

## v0.6.0 closures (3 carryovers from v0.5.x backlog)

- **L6.A: `automerge.yml --cov-fail-under` aligned 92 → 94.** Closes
  the v0.5.5 known-limitation #2: `pyproject.toml [tool.coverage.report]
  fail_under = 94` was already enforced locally by `pytest --cov-fail-
  under` (set during the Loop 5 final coverage push, 93.94 % → 94.60 %),
  but the hosted auto-merge gate at `.github/workflows/automerge.yml`
  was still pinned to 92. Without this fix, the auto-merge job would
  green-light a PR that sat at 92.x % even though the project pyproject
  already required 94. The single-line bump
  (`--cov-fail-under=92` → `--cov-fail-under=94`) plus a 7-line inline
  comment explaining the v0.6.0 rationale closes the gap; future PR
  authors no longer need to reason about two divergent floors.
- **L6.B: cursor adapter now propagates `extra["cli_args"]` (or alias
  `cmd_args`) to cursor-agent argv via `shlex.split`.** Closes the
  v0.5.{3,4,5} carry-over chain that SKILL.md v0.5.3 Workflow 4 had
  documented as "需要 cursor-agent 自定义 flag 时走 popolaloom._vendored
  二开或等 v0.6+ 的 --passthrough 项". The new
  `_normalize_cli_args(value)` helper (16 LOC + 47-line docstring) in
  `src/popolaloom/adapters/cursor.py` accepts either `list[str]`
  (preferred — explicit token list, used by JSON payloads) or `str`
  (split via `shlex.split` so quoted compound tokens survive intact).
  Each token lands AFTER the `--print --output-format <fmt>` core flags
  but BEFORE the `<prompt>` positional, so cursor-agent recognises them
  as flags rather than prompt content. The legacy `cmd_args` key (the
  one SKILL.md v0.5.3 Workflow 4 used in its shell-quoting tip) is
  accepted as an alias for back-compat; when both are set, the
  canonical `cli_args` wins (pinned by
  `test_cursor_cli_args_takes_precedence_over_cmd_args_alias`). Type
  errors raise `ValueError` with a key-pinned message (No Silent
  Failures workspace rule). 15 default-lane tests in
  `tests/adapters/test_cursor_extra_passthrough.py` (NEW) pin every
  branch: 5 happy-paths (string / list / alias / shlex split / quoted
  compound token) + 3 argv-positioning contracts (before prompt /
  after `--output-format` / composes with `session_id` + `cwd_flag`)
  + 3 No-Silent-Failures branches (int / list-with-non-string / dict)
  + 4 empty / no-op / legacy-shape contracts. Unblocks
  `popola dispatch ... --cli=cursor --cli-flag cli_args=--trust` —
  the SKILL.md v0.5.3 Workflow 4 example that prompted the carry-over
  in the first place.
- **L6.C: comprehensive `release-notes-v0.6.0.md` + CHANGELOG `[0.6.0]`
  entry + README v0.6.0 status row.** This document is the canonical
  write-up of the v0.5.x → v0.6.0 self-improvement series + the L6.A /
  L6.B closures + the hand-off contract for the v0.6.x patch line.
  Mirrors the structural anchor `release-notes-v0.4.0.md` (Phase 1 GA)
  for inter-release readability.

## Cumulative metrics (v0.5.0 → v0.6.0)

- **Tests**: 1104 → 1383 (+279, ~ 25 % growth across 5 loops + this
  consolidation).
- **Coverage**: 91.15 % → 94.62 % (+3.47 pp; pyproject `fail_under` lifted
  91 → 92 → 93 → 94 across loops 1 / 2 / 4 / 5).
- **Mutmut surface**: 1 → 5 modules (`daemon/state.py` baseline +
  `daemon/event_log.py` + `cli/init_cmd.py` + `cli/doctor_cmd.py` +
  `evaluation/runner.py` declarative-only; live `mutmut run` remains
  blocked by src-layout / editable-install friction — see Known
  limitations §1).
- **CLI verbs**: +1 (`popola init --interactive` wizard, v0.5.5).
- **CI workflows**: green on hosted runners (was 100 % red on
  `/home/agent/` permission error pre-v0.5.1; auto-merge gate now
  aligns with project `fail_under = 94` post-v0.6.0).
- **Lark proactive notifications** (v0.4.1 baseline): 5 trigger types
  + 5 env vars + LarkSupervisor wired (no v0.5.x → v0.6.0 regression).
- **Adapter passthrough**: cursor adapter now accepts `extra["cli_args"]`
  / `extra["cmd_args"]` (v0.6.0 L6.B); claude / codex / kimi / copilot
  adapters unchanged.

## Known limitations (forward-looking to v0.6.x / v0.7.0)

1. **Live `mutmut run` activation still blocked** by src-layout /
   editable-install friction (carry-over from v0.3.4 baseline +
   v0.5.4 / v0.5.5 declarative-only bumps). Tracked for v0.6.x with
   the proper venv-isolated runner approach. The 5 declared modules
   in `[tool.mutmut].paths_to_mutate` plus the targeted boundary
   tests in `test_evaluation_mutation_kills.py` (9 cases),
   `tests/cli/test_init_cmd.py` (mutation-kill subset), and
   `tests/test_state_mutation_kills.py` provide a *manual-audit*
   safety net in the meantime.
2. **Real Lark supervisor Tier-3 test still needs Lark bot creds on
   CI** (gated by `@pytest.mark.real_lark`). The mock-driven
   coverage in `tests/lark/test_lark_supervisor_wiring.py` +
   `test_lark_supervisor_shutdown.py` exercises every state
   transition; the real-Lark e2e is exercised manually post-merge
   before each release.
3. **`popola init --interactive` wizard does not yet support
   `--mode` / `--with-examples` modifiers** (prompts always default
   to `standard` mode without examples). Tracked for v0.6.1; the
   non-interactive path supports both modifiers, so any operator
   needing them can fall back to flag-driven invocation.
4. **Coverage 94.62 % vs aspirational 95 % target** (carry-over from
   v0.4.0 release notes). The remaining missed lines are concentrated
   in: `lark/listener.py` (88 %, mostly chat-message-router branches
   that need a real Lark bot), `lark/renderers/lark.py` (88 %, card-
   builder edge cases for non-default themes), and `mcp/__main__.py`
   (0 %, the entrypoint stub that exists only for `python -m
   popolaloom.mcp`). Both are mechanical follow-ups; no design
   changes needed.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.6.0'"

# 2. default lane
pytest tests/ -m "not slow and not nightly and not real_cli and not real_lark"

# 3. coverage (≥ 94 % for the new fail_under)
pytest tests/ -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=94

# 4. self-bootstrap 3× consecutive
for i in 1 2 3; do
  pytest tests/self_bootstrap -m slow || exit 1
done

# 5. NFR slow lane
pytest tests/matrix/nfr -m slow

# 6. lint + types
ruff check src/popolaloom tests/

# 7. quickstart smoke
bash examples/quickstart.sh
```

All seven commands exit 0 on a clean v0.6.0 checkout.

## Migration from v0.5.x

- **No breaking changes.** Every caller that worked on v0.5.5 keeps
  working on v0.6.0 byte-identically.
- **Lark notification env vars unchanged** (`LARK_NOTIFY_ON_*` family
  from v0.4.1 — `LARK_NOTIFY_ON_DISPATCH`, `LARK_NOTIFY_ON_HITL`,
  `LARK_NOTIFY_ON_TERMINAL`, etc.).
- **`popola init` / `popola skill` / `popola doctor` surfaces
  unchanged** — the v0.5.0 14-row dispatcher matrix + the v0.5.5
  `--interactive` wizard remain stable; v0.6.0 adds zero verbs.
- **New: cursor adapter accepts `--cli-flag cli_args="--trust"`** (or
  the `--cli-flag cmd_args="..."` alias for back-compat with the
  v0.5.3 SKILL.md Workflow 4 example) — purely additive. Callers that
  do not set the new key see byte-identical argv to what v0.5.5
  produced (pinned by
  `test_cursor_no_cli_args_key_preserves_legacy_argv_shape`).
- **Auto-merge gate floor lifted 92 → 94** in
  `.github/workflows/automerge.yml`; existing PRs that already
  cleared the project's `[tool.coverage.report] fail_under = 94`
  (set in v0.5.5) continue to clear the gate without change.
- **Wheel `popolaloom-0.6.0-py3-none-any.whl`** is a drop-in
  replacement for `popolaloom-0.5.5`; `pip install --upgrade
  popolaloom` followed by `popola skill upgrade --target=all` is the
  recommended upgrade path.

## Commit-by-commit (v0.5.x rounds + this minor)

The 5-loop self-improvement series + this consolidation produces a
single auditable paper trail on `feature/v0.5.0-skill-install`:

- `e5c6784` v0.5.0 GA — Skill + install + popola doctor + canonical SKILL.md.
- `fa9af92` v0.5.1 Loop 1 — CI runner-writability fix + 90 error-path tests.
- `ab0b9ea` v0.5.2 Loop 2 — NFR-2/9 + Lark graceful shutdown + automerge align (92).
- `c80aabb` v0.5.3 Loop 3 — arktower CI imports + ruff lint + SKILL.md docs.
- `740d011` v0.5.4 Loop 4 — mutmut 1→4 modules + cli edge cases + popolad coverage.
- `3189604` v0.5.5 Loop 5 — README/DEMO refresh + `--interactive` + mutmut 4→5 + coverage.
- (this commit) v0.6.0 GA — automerge align + cursor `cli_args` + comprehensive release notes.

Each round shipped: a single code change closing ≥ 1 deliverable, ≥ 5
new tests, a version bump in `pyproject.toml` + `__init__.py` + SKILL.md
frontmatter + `.popolaloom-version` + `tests/test_smoke.py`, a
`CHANGELOG.md` entry at the top, and a release-notes file at the repo
root with closures + verification + journey rollup. The pattern is
identical to the v0.3.x 5-round self-evolution loop that took
PopolaLoom from v0.3.0 → v0.4.0 GA (documented under
`evidence/round-{1..5}-evidence.md`) — the difference is that the
v0.5.x chain has a fixed *terminal target* (this v0.6.0 consolidation)
rather than an open-ended evolution objective.

---

**PopolaLoom v0.6.0 ships on 2026-05-06.** Phase 2 (multi-agent
dispatch + token-budget gating + per-agent capability registry) picks
up the v0.6.x patch line from here; the deferred items in §"Known
limitations" above seed the v0.6.1 backlog.
