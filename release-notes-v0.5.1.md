# PopolaLoom v0.5.1 — Loop 1 self-improvement patch

> Released: 2026-05-05
> Phase 2 prelude follow-up: v0.5.0 GA + first L3-loop patch
> Theme: turn the v0.5.0 GA into something every PR can actually
> verify in CI by (1) unbreaking the GitHub-hosted runner install
> step, (2) closing the 0.85 pp coverage gap that has been carried
> forward since v0.4.0, and (3) raising the `fail_under` floor so
> we can never silently regress past it.

## Summary

PopolaLoom v0.5.1 is the first patch in the v0.5.x → v0.6.0
self-improvement series.  It closes the three GA-blockers surfaced
during v0.5.0 functional testing without expanding the public
surface:

1. **CI is green again.** Both
   [`.github/workflows/ci.yml`](.github/workflows/ci.yml) (default
   + slow + lint jobs — three install sites) and
   [`.github/workflows/automerge.yml`](.github/workflows/automerge.yml)
   used to run an unconditional `mkdir -p /home/agent/reference`
   that fails with `Permission denied` on GitHub-hosted runners.
   Under `set -e`, the `mkdir` failure killed the whole step
   *before* `pip install -e ".[dev]"` ran, so PRs #1 / #2 / #3
   ended up with `red builds whose root cause was the workflow
   itself, not the code under review`.  v0.5.1 wraps the
   ArkTower clone in `if [ -w /home ] && [ ! -d /home/agent/reference/ArkTower ]`
   and softens both the `mkdir` and the `git clone` with
   `2>/dev/null || true` so the install step always reaches the
   `pip install` line.  ArkTower itself has been vendored under
   `src/popolaloom/_vendored/arktower/` since v0.5.0 (Stage S1
   per [`release-notes-v0.5.0.md`](release-notes-v0.5.0.md)),
   so no separate clone is needed at all on the runner —
   the guarded clone path remains as a no-op for the v0.4.x
   baseline.
2. **Coverage is at 92 %.** The v0.4.0 GA released at `91.36 %`
   with the 0.64 pp gap to the aspirational 92 % target tracked
   as known-limitation #1; v0.4.1 nudged it to `91.38 %`; v0.5.0
   landed at `91.15 %` (a small dip caused by the new `popola
   init` + `popola skill` + `popola doctor` subcommands).  v0.5.1
   adds 90 new default-lane tests across two new files plus a
   doctor-cmd extension and lifts the metric to **`92.56 %`**.
   The `[tool.coverage.report] fail_under` gate is raised from
   **91 → 92** in the same commit so the new floor is locked in.
3. **Version bump + paper trail.** `pyproject.toml`,
   `src/popolaloom/__init__.py`,
   `src/popolaloom/skills/popolaloom/SKILL.md` (frontmatter),
   `src/popolaloom/skills/popolaloom/.popolaloom-version`, and
   `tests/test_smoke.py` all move `0.5.0 → 0.5.1` in lockstep.
   `CHANGELOG.md` gets a `[0.5.1]` entry at the top; this
   document is the canonical write-up.

The patch stays inside the v0.5.0 envelope: 0 new src/ modules,
0 ADRs, 0 dependency changes.  Only the 12 owned files listed in
the "What changed" section below are modified.

## Closures (Loop 1 deliverables L1.A / L1.B / L1.C)

| # | Deliverable | Closure |
|---|-------------|---------|
| L1.A | `.github/workflows/{ci,automerge}.yml` runner-writable | **Done** — mkdir guarded by `[ -w /home ]`; soft-failed with `2>/dev/null \|\| true`; identical wording at all 4 sites (default + slow + lint + automerge). `git grep "\\[ -w /home \\]" .github/ \| wc -l` = `4`. |
| L1.B | Coverage 91.15 % → ≥ 92 % | **Done** — `92.56 %` (`+ 1.41 pp`). 90 new default-lane tests across `tests/cli/test_main_error_paths.py` (NEW, 42 cases), `tests/daemon/test_rpc_error_paths.py` (NEW, 36 cases), and `tests/cli/test_doctor_cmd.py` (extended, 12 new cases). |
| L1.C | Version bump 0.5.0 → 0.5.1 + `fail_under` 91 → 92 + CHANGELOG + release notes | **Done** — version bumped in 5 files; `pyproject.toml [tool.coverage.report] fail_under = 92`; `CHANGELOG.md [0.5.1]` entry at top; this file at repo root. |

## What changed (file-by-file)

### CI workflows (2 files)

- `.github/workflows/ci.yml` — `default` + `slow` + `lint` jobs (3
  install sites) now guard the ArkTower clone with `if [ -w /home ]
  && [ ! -d /home/agent/reference/ArkTower ]`; `mkdir -p` and
  `git clone` both pipe stderr to `/dev/null` and `|| true`.
  Identical wording per site for grep-ability.
- `.github/workflows/automerge.yml` — same fix in the auto-merge
  gate's install step.

### Source (1 file)

- `src/popolaloom/__init__.py` — `__version__ = "0.5.1"`. No code
  changes; the v0.5.1 patch is documentation + tests + CI YAML
  only on the source side.

### Skill artefacts (2 files)

- `src/popolaloom/skills/popolaloom/SKILL.md` — frontmatter
  `version: 0.5.0 → 0.5.1`. The `last_updated: "2026-05-05"`
  field is unchanged because the canonical SKILL.md content is
  unchanged in this patch (Loop 3 will revisit `--extra
  cli_args="--trust"` adapter passthrough docs per the v0.5.0
  functional-test follow-up).
- `src/popolaloom/skills/popolaloom/.popolaloom-version` — `0.5.1`.

### Build / coverage config (1 file)

- `pyproject.toml`:
  - `[project] version = "0.5.0" → "0.5.1"`.
  - `[tool.coverage.report] fail_under = 91 → 92`. The comment
    block above the directive is extended with a v0.5.1 note
    citing this release.

### Tests (4 files)

- `tests/test_smoke.py` — version assertion bumped to `0.5.1`;
  module docstring prepended with a v0.5.1 paragraph that
  mirrors the v0.4.1 / v0.5.0 release-note convention.
- `tests/cli/test_main_error_paths.py` (NEW) — 42 default-lane
  cases that cover every documented error ramp of the v0.2.0+
  `popola` CLI:
  - `dispatch`: 404 (unknown adapter), 400 (validation),
    500 (unexpected status), `httpx.ConnectError`.
  - `status`: 404 (unknown task), 500 (unexpected),
    `httpx.ConnectError`.
  - `list`: 500 (unexpected), `httpx.ConnectError`, empty-body
    "No active tasks." render path, `--state` filter.
  - `cancel`: 404, 409 (already-terminal), 500,
    `httpx.ConnectError`, success text-format render.
  - `probe`: 500, `httpx.ConnectError`, `--json` short-circuit.
  - `attach` (both `--follow` and `--no-follow` paths): 404
    status, 500 status, `attach_stream` non-200, ConnectError,
    KeyboardInterrupt, happy-path streaming + comment / garbage
    line skipping.
  - `_consume_sse`, `_wait_for_terminal` (ConnectError, non-200
    warning, deadline expiry), `_format_event`, `_summarize_data`
    (every known event-type branch + truncation + non-dict
    payload).
  - `list-cli`: missing-adapter row + empty-registry exit-1.
  - `main()` entry point + `make_sync_client` / `make_async_client`
    default-path constructors.
- `tests/daemon/test_rpc_error_paths.py` (NEW) — 36 default-lane
  cases driving the FastAPI app via `httpx.ASGITransport`:
  - `POST /dispatch`: 404 (`KeyError` ramp), 400
    (`evolution_round` `ValueError`, generic `RuntimeError`,
    generic `ValueError` ramps).
  - `GET /status/{id}`: 404 ramp.
  - `POST /cancel/{id}`: 404 + 409 (already-terminal RuntimeError
    ramp) — the 409 case dispatches a real echo-adapter task,
    waits for it to finish, then asserts cancel returns 409.
  - `POST /relay`: 400 (unknown source `ValueError`),
    400 (RuntimeError from the underlying primitive), happy-path
    envelope echo.
  - `POST /supervise`: happy path, blank-parent → 422 from
    Pydantic, callback-fire ramp that exercises both
    `_rpc_complete_callback` and `_rpc_fail_callback` inner
    closures (lines 467 + 477 of `daemon/rpc.py`).
  - `POST /federate`: 400 (invalid voting strategy), 422
    (cli_list too short), 400 ValueError, 400 RuntimeError, 400
    generic-Exception ramp ("federate dispatch failed: ..."),
    happy path with 3 echo CLIs.
  - `POST /hitl/answer` + `GET /hitl/pending`: 503-when-store-missing
    ramps.
  - `GET /attach_stream/{id}`: 404 ramp + happy-path streaming
    that exercises the final-events drain on terminal.
  - `_read_tail` helper: empty-when-missing-event-log,
    `FileNotFoundError`-swallow.
  - `_format_sse` shape, `_apply_evolution_round_prepend` round-1
    (no-prior-evidence) + round-2 (with evidence file) ramps.
  - `_build_default_popolad` factory + `create_app` lazy-import
    of `build_command`.
  - lifespan error swallowers: `rehydrate_from_persistence` raise,
    `shutdown_persistence_bridge` raise, shutdown `cancel_task`
    raise — all logged + continued per the No Silent Failures
    rule (the lifespan finally swallows so a bad cancellation
    doesn't trap the daemon at shutdown).
  - `GET /probe` falls back to `datetime.now` when `_DAEMON_STATE`
    `started_at` is unset.
- `tests/cli/test_doctor_cmd.py` (extended) — 12 new cases at
  the end of the file (existing 12 cases unchanged):
  - `_probe_daemon` `ConnectError` / `HTTPError` / `OSError` /
    non-200 status / non-JSON response ramps.
  - Daemon FAIL-detail rendering (line 196 of `doctor_cmd.py`).
  - Skill-DRIFT branch (frontmatter version `0.0.0-stale`).
  - ArkTower module ImportError ramp (`__import__` interception
    of `from popolaloom._vendored import arktower`).
  - ArkTower migration WARN ramp (missing 005/006 SQL files).
  - WARN-only summary-yellow branch in `_render_terminal`.
  - `--strict` exits 0 when only WARN rows are present.
  - `collect_doctor_aggregate()` direct unit invocation
    returning a populated `DoctorAggregate` dataclass.

### Docs (2 files)

- `CHANGELOG.md` — `[0.5.1]` entry at top.
- `release-notes-v0.5.1.md` (NEW) — this file.

## Test counts + coverage

- **Default-lane**: **1194 pass / 0 fail / 18 skipped** (was 1104
  at v0.5.0, **+ 90 new tests**). Tests run in ~24 s.
- **Coverage**: **92.56 %** (was 91.15 % at v0.5.0,
  **+ 1.41 pp**). The improvement is concentrated in
  `cli/main.py` (88 % → ≥ 95 %), `daemon/rpc.py` (82 % → ≥ 92 %),
  and `cli/doctor_cmd.py` (86 % → ≥ 95 %).
- **No new lint or type errors** in any of the 12 owned files.

## Verification commands

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.5.1'"

# 2. default lane + new coverage gate
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=92

# 3. CI YAML grep — must return 4 hits
git grep -E "\[ -w /home \]" .github/

# 4. spot-check the 2 new test files + the doctor-cmd extension
pytest tests/cli/test_main_error_paths.py \
  tests/daemon/test_rpc_error_paths.py \
  tests/cli/test_doctor_cmd.py -v

# 5. ruff + mypy on the touched files
ruff check src/popolaloom/__init__.py \
  tests/cli/test_main_error_paths.py \
  tests/daemon/test_rpc_error_paths.py \
  tests/cli/test_doctor_cmd.py
mypy src/popolaloom/__init__.py

# 6. yaml smoke — both workflows still parse
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
python -c "import yaml; yaml.safe_load(open('.github/workflows/automerge.yml'))"
```

All six commands exit 0 on a clean v0.5.1 checkout.

## Behaviour deltas from v0.5.0

1. **CI install step** — on GitHub-hosted runners (where `/home`
   is owned by a different system user than the runner), the
   `mkdir -p /home/agent/reference` call no longer terminates
   the step.  The runner now jumps straight to `pip install -e
   ".[dev]"`, which works because the v0.5.0 wheel ships the
   vendored ArkTower.  On the developer VM (`/home/agent`
   writable), behaviour is unchanged: the mkdir + clone proceed
   exactly as before.
2. **Coverage gate** — `pytest --cov-fail-under=91` is no longer
   sufficient; the project's pyproject directive now requires
   92.  CI invocations that pass `--cov-fail-under=90` (e.g.
   `automerge.yml`'s gate) continue to work because that flag
   overrides the directive at the CLI layer; v0.5.x will
   eventually align them once the auto-merge gate's coverage
   threshold is reviewed.
3. **No runtime changes** — `popolaloom.__version__` reports the
   new string; the daemon, CLI, MCP server, and Lark integrations
   all behave identically to v0.5.0.

## Known limitations / deferred to v0.5.x

1. **`--extra cli_args="--trust"` adapter passthrough** — the
   v0.5.0 functional test (`/tmp/popolaloom-skill-functional-test.md`)
   flagged this as undocumented in `SKILL.md`. Not addressed in
   v0.5.1 by design (Loop 3 takes the docs polish pass per the
   v0.5.x roadmap).
2. **Lark supervisor graceful shutdown** — same as v0.4.1 + v0.5.0:
   the supervisor is started as a background task on the daemon
   loop; explicit `await popolad.lark_supervisor.stop()` is still
   not wired into `daemon/rpc.py`'s lifespan exit handler.
3. **Coverage 92.56 % vs 95 % aspirational target** — the next
   coverage push targets `daemon/server.py` (87 %), `daemon/
   supervisor.py` (87 %), and `lark/listener.py` (81 %), which
   between them carry roughly half of the remaining uncovered
   lines.  Tracked for v0.5.2 / Loop 2.
4. **Auto-merge `--cov-fail-under=90`** —
   `.github/workflows/automerge.yml` still uses `--cov-fail-under=90`
   while the project pyproject directive is now 92.  Aligning the
   two requires updating the auto-merge gate's evidence schema
   and is deferred to v0.5.2 + (Loop 2).

## v0.6.0 hand-off contract

The v0.6.0 milestone (Phase 2 — multi-agent dispatch + token
budget gating) does not depend on any new exported surface from
this patch.  The contract from
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) carries
forward unchanged.

---

**PopolaLoom v0.5.1 ships 2026-05-05.**
Loop 2 (v0.5.2: deeper coverage push for `daemon/server.py` +
`daemon/supervisor.py` + `lark/listener.py`) starts on the next
branch off `feature/v0.5.0-skill-install` after the merge.
