# PopolaLoom v0.5.0 — Skill + multi-IDE install + popola doctor

> Released: 2026-05-05
> Theme: User-facing Skill interaction surface + DevolaFlow-style installer
> Phase 2 prelude (per spec §2.3 / `.local/feedbacks/feedback_for_v0.4.0.md`)

## Summary

PopolaLoom v0.5.0 is the **Phase 2 prelude** — the first release that
makes PopolaLoom *installable + discoverable* from the user-facing
side. v0.4.x shipped the daemon, dispatch primitives, HITL stack, and
proactive Lark notifications, but a fresh `pip install popolaloom` on
a clean machine would fail because of an `arktower @ file://` direct
reference, and host agents (Cursor / Claude / Codex / Copilot) had no
canonical SKILL.md to auto-discover. v0.5.0 closes both gaps in one
milestone.

The headline shifts: `pip install popolaloom` now works on any host
(ArkTower vendored under `popolaloom._vendored.arktower`, Q5-4 Path B
fallback after PyPI publish was unreachable); `popola init` mirrors
DevolaFlow's `devola-init` 14-row dispatcher and file-copies the
canonical SKILL.md into Cursor / Claude / Codex / Copilot in one
command; `popola skill {install, doctor, upgrade}` lets PopolaLoom
self-manage its multi-IDE install state; and a new `popola doctor`
verb aggregates four subsystem audits (skill / daemon / lark-cli /
ArkTower) into a single human-readable + machine-readable report.

The milestone shipped in 5 stages (S1–S5) on the
`feature/v0.5.0-skill-install` branch over ~ 6 working days, against
the locked plan at
`.local/memory/specs/popolaloom/v0.5.0-plan.md`. All 5 user
questions Q5-1..Q5-5 were answered by the operator's
"skip-default" GATE response on 2026-05-05, locking every best-guess
recommendation in §0.5 of the plan.

## The journey: v0.0.1 → v0.5.0

| Version | Date | Theme | Tests | Coverage | nines |
|---|---|---|---|---|---|
| v0.0.1 | 2026-04-29 | Day-0 scaffold | 18 | n/a (75 % target) | 0.32 (estimated) |
| v0.2.0 | 2026-05-01 | M1-M5: real daemon + LangGraph + ArkTower + MCP + S1/S3 | ~50 | ~75 % | 0.85 |
| v0.2.1 | 2026-05-01 | Tier matrix v1 + property tests | 250 | 80 % | 0.86 |
| v0.2.2 | 2026-05-02 | Tier 4 (real langgraph) + Tier 5 (e2e) + S1-S5 mock | 500 | 85 % | 0.87 |
| v0.2.3 | 2026-05-03 | Tier 4 / 5 + S1-S5 mock complete + HITL+devola schema | 624+ | 90 % | 0.88 |
| v0.3.0 | 2026-05-04 | F1-F5: 8 real nines + 7 primitives + dual gate + auto-merge + HITL/Lark + S2/S4/S5 real | 887 | 89.23 % | 0.90 |
| v0.3.1 | 2026-05-04 | Round 1: coverage restoration → 90.79 % | 929 (+42) | 90.79 % | 0.921 |
| v0.3.2 | 2026-05-04 | Round 2: NFR-2 (status RTT) + NFR-9 (dispatch p95) | 929 (+5 slow) | 90.79 % | 0.941 |
| v0.3.3 | 2026-05-04 | Round 3: lark_health real fixture + 4-restart escalation | 946 (+17) | 90.86 % | 0.961 |
| v0.3.4 | 2026-05-04 | Round 4: mutation testing baseline 70.8 → 100 % on state.py | 958 (+12) | 91.0 % | 0.981 |
| v0.3.5 | 2026-05-04 | Round 5: README + quickstart.sh + DEMO.md + smoke test | 958 (+6 slow) | 91.0 % | 1.000 (clamped) |
| v0.4.0 | 2026-05-04 | GA + supplementary cli/popolad coverage push | 980 (+22) | 91.36 % | 1.000 |
| **v0.4.1** | **2026-05-05** | **Phase 1 close-out: proactive Lark notifications (5 cards + auto-start LarkSupervisor + `task.canceled` repair)** | **1023 (+43)** | **91.38 %** | **1.000** |
| **v0.5.0** | **2026-05-05** | **Phase 2 prelude: vendored ArkTower + `popola init` 8 verbs + canonical SKILL.md + `popola skill` group + `popola doctor` aggregate** | **1104+ (+81)** | **91.15 %** | **1.000** |

## v0.5.0 closures (5 stages S1-S5)

- **S1** · ArkTower `file://` dep removed; vendored at
  `src/popolaloom/_vendored/arktower/` (Path B per Q5-4 fallback;
  PyPI publish path A was unreachable — `pip index versions arktower`
  returned no distribution). `[tool.coverage.run] omit =
  ["src/popolaloom/_vendored/*"]` keeps the coverage gate focused on
  first-party code; refresh procedure documented in
  [`VENDORING.md`](VENDORING.md). Pinned to upstream commit
  `467a087`.
- **S2** · `popola init` Typer subcommand group with **8 verbs**
  (`cursor` / `claude` / `codex` / `copilot` / `local` / `all` /
  `--list` / no-args auto-detect) + **8 modifiers** (`--global` /
  `--project` / `--mode={core,standard,full}` / `--no-compile` /
  `--with-examples` / `--no-with-examples` / `--dry-run` /
  `--popolaloom-version`); mirrors DevolaFlow `devola-init` per Q5-2
  lock. 4 IDE targets × 2 scopes (except Copilot, project-only) × 3
  modes = 33 install matrix cases all green. Idempotency contract:
  second invocation prints `SKIP <path> (already installed)`.
- **S3** · canonical SKILL.md authored at
  `src/popolaloom/skills/popolaloom/SKILL.md` (10 623 chars / ~ 2 655
  tokens, **7 sections**, frontmatter `name: popolaloom` /
  `version: 0.5.0` per Q5-1 + Q5-3 locks). Ships in the wheel via
  `[tool.hatch.build.targets.wheel] packages = ["src/popolaloom"]`;
  `popola init` resolves it via
  `popolaloom.cli._skill_source.canonical_source_path` so editable
  + installed checkouts both work.
- **S4** · `popola skill install` / `popola skill doctor` /
  `popola skill upgrade` subcommand group + `popola doctor` aggregate
  health verb (4 new verbs total). The skill commands sit on top of
  three new `popolaloom.evolution` siblings
  (`skill_install.py` / `skill_doctor.py` / `skill_upgrade.py`)
  that share the `SKILL_TARGETS` registry with the existing
  `skill_inject.py`. `popola doctor` audits 4 subsystems
  (skill / daemon / lark / ArkTower) into a single report; default
  exit 0, `--strict` escalates FAIL to exit 1, `--json` emits a
  4-section envelope.
- **S5** · docs / DEMO / quickstart refresh + release notes + e2e +
  version bump (this file + `README.md` rewrite + `docs/DEMO.md`
  additive `v0.5.0 Skill installation walkthrough` section +
  `examples/quickstart.sh` 6-step rewrite + `CHANGELOG.md` `[0.5.0]`
  entry + `tests/integration/test_quickstart_v050.py` slow-marked
  e2e + `pyproject.toml` / `__init__.py` / `tests/test_smoke.py` /
  `SKILL.md` frontmatter version bump 0.4.1 → 0.5.0).

## Test count + coverage

- **Total**: **1104+ default-lane PASS** / 18 skipped / 0 failed
  (was 1023 at v0.4.1; +81 from S1–S4: ~ +20 vendoring tests, ~ +33
  init matrix cases, ~ +15 skill subcommand cases, ~ +13 doctor
  aggregate cases). S5 adds **0** default-lane tests by design (all
  S1-S4 stages already shipped their own coverage; S5 is docs +
  release).
- **Coverage**: **91.15 %** default lane against the ratcheted
  `fail_under = 91` gate (was 91.38 % at v0.4.1; the modest dip
  reflects the new evolution sibling modules' init-time error
  branches, all under the 91 floor with a 0.15 pp cushion).
- **e2e**: `tests/integration/test_quickstart_v050.py` (slow-marked,
  one case) exercises the new 6-step `examples/quickstart.sh`
  end-to-end against an isolated `tmp_path` `$POPOLA_HOME`.
- **Lifted from v0.4.1 baseline**: +81 tests across S1–S4 (no new
  src tests in S5 by design — see "Owned files" in the L3 task brief).

## 5 user questions Q5-1..Q5-5 — answers

All five questions were locked at the 2026-05-05 GATE via the
operator's "skip-default" response per the §0.5 protocol; the
implementation followed every best-guess recommendation:

- **Q5-1**: locked **A `popolaloom`** (frontmatter `name`); install
  paths use `<scope>/.cursor/skills/popolaloom/SKILL.md` etc.
- **Q5-2**: locked **A mirror DevolaFlow full 14-verb matrix**;
  `popola init` ships 8 verbs + 8 modifiers in one Typer subgroup.
- **Q5-3**: locked **A inherit v0.4.1 Lark defaults (3 ON / 2 OFF)**;
  `popola init` does not export env vars (operator manages
  `~/.bashrc` directly), but `popola doctor` displays the current
  values.
- **Q5-4**: locked **A PyPI publish ArkTower** → **fell back to
  Path B vendor** when `pip index versions arktower` returned no
  distribution. Risk **RV5-1** in
  `.local/memory/specs/popolaloom/v0.5.0-plan.md` §6 was updated to
  reflect the realised path; vendor maintenance procedure documented
  in `VENDORING.md`.
- **Q5-5**: locked **A defer curl one-liner to v0.5.1**; the
  `pip install + popola init` two-step is sufficient for v0.5.0 GA.
  The hosted `scripts/install.sh` lands in v0.5.1 alongside the
  README screenshot placeholders.

## Known limitations / deferred to v0.5.1+

1. **Coverage 91.15 % vs 92 % aspirational target** — same shape as
   v0.4.0 / v0.4.1 (~ 0.85 pp gap, mostly CLI / RPC integration
   error paths and the new `evolution/skill_*` init branches that
   need a real daemon to hit). Tracked for v0.5.1 alongside the
   curl-installer + screenshot work.
2. **PyPI publish of `popolaloom`** — gated on ArkTower's PyPI
   publication (Q5-4 Path A retry). Until then,
   `pip install popolaloom` works from a local clone or a private
   index; the vendored ArkTower subset means there is no external
   hard dep. See `VENDORING.md` "When to stop vendoring".
3. **DevolaFlow folder-shape SKILL** (with `references/` /
   `examples/` siblings) — deferred to v0.6.0 per Q5-1's "single-file
   first" recommendation (research §E.1.1).
4. **`popola init --interactive`** (mirroring `devola-init`'s
   `init_interview.py`) — deferred to v0.5.x.
5. **`popolaloom plugins {list,status,refresh}`** subcommand group —
   deferred to v0.5.2 per research §A.7.
6. **NFR-4 (LangGraph super-step latency ≤ 100 ms)** + **NFR-12
   (multi-CLI vote convergence)** — still un-benchmarked, carried
   forward from v0.4.1.
7. **Live `mutmut run`** — still blocked by mutmut 3.5 / src-layout
   friction; manual audit ledger at `evidence/mutmut-baseline.md`
   remains authoritative.
8. **Lark real e2e** — still gated on `@pytest.mark.real_lark`
   (requires real Lark bot credentials); CI continues to run with
   mocked `lark-cli`. The v0.4.1 + v0.5.0 wiring is exercised
   manually against a real bot before each release.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.5.0'"

# 2. default lane + coverage gate
pytest tests/ -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=91

# 3. SKILL.md frontmatter / canonical contract
pytest tests/cli/test_skill_md_canonical.py -v

# 4. ruff + mypy on the touched stages (S1-S4 source files)
ruff check src/popolaloom tests/
mypy src/popolaloom

# 5. quickstart 6-step e2e (slow-marked; honours $POPOLA_HOME)
pytest tests/integration/test_quickstart_v050.py -m slow -x

# 6. install matrix sanity (Stage S2)
pytest tests/cli/test_init_matrix.py -v

# 7. skill subcommand + doctor aggregate (Stage S4)
pytest tests/cli/test_skill_subcommand.py tests/cli/test_doctor_cmd.py -v
```

All seven commands exit 0 on a clean v0.5.0 checkout.

---

**PopolaLoom v0.5.0 ships 2026-05-05.**
Phase 2 proper (popolaloom-tui / popolaloom-web / Cloud Agent
adapter / multi-tenant remote daemon) starts on the next branch off
`main`. The deferred items in §"Known limitations" above are
tracked for v0.5.1 (curl installer + screenshots) and v0.6.0
(folder-shape SKILL + Textual TUI + NiceGUI Web).
