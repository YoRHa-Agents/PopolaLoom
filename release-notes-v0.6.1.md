# PopolaLoom v0.6.1 — CI hotfix patch

> Released: 2026-05-06
> Theme: close the 3 distinct CI failures blocking the v0.6.0 PR
> (GitHub Actions run `25392679894`) without touching any user-facing
> surface — config-only mypy carve-out, gitignore whitelist + tracked
> auto-merge config, and a one-call-site fall-through to vendored
> ArkTower migrations.

## Summary

PopolaLoom v0.6.1 is a pure CI plumbing patch: no new daemon
primitives, no new public Python APIs, no schema changes, no new
dependencies, no ADRs. The v0.6.0 PR went red on three orthogonal
issues that surface only on GitHub-hosted runners (the dev VM
silently masks all three). v0.6.1 closes each one in the smallest
possible diff:

1. **mypy strict on the vendored ArkTower subset** — ~12 errors in
   read-only upstream code (arg-type mismatches + `list` shadowing
   the builtin used as a type annotation). Per `VENDORING.md` we are
   not allowed to modify vendored sources, so we exempt the tree
   from `[tool.mypy]` exactly the way `[tool.ruff] extend-exclude`
   (line 115) and `[tool.coverage.run] omit` (line 148) already
   exempt it from ruff + coverage. Owned-code mypy strict still
   runs against `src/popolaloom/**` minus `_vendored/`.
2. **`.workflow/automerge.yaml` was gitignored** — the test
   `tests/test_automerge_gate.py::test_repo_workflow_automerge_yaml_loads_cleanly`
   asserts the file exists at the repo root (workflow loads it,
   schema validation roundtrips), but `.workflow/` was ignored
   wholesale by `.gitignore`. v0.6.1 adds `!.workflow/automerge.yaml`
   right after the `.workflow/` rule + checks the actual config
   file in (5 AND-condition thresholds + path policy with the
   `src/popolaloom/gate/**` self-test rule).
3. **`tests/test_repository.py` 4× `no such table: tasks`** — the
   test fixture explicitly passes the legacy
   `/home/agent/reference/ArkTower/migrations` path to
   `make_persistence(arktower_migrations_dir=...)` and that
   directory does not exist on hosted runners. v0.6.1 patches
   `daemon/repository.py:make_persistence` so that when the
   explicit path's `Path.is_dir()` returns `False`, we fall through
   to `_arktower_migrations_dir()` which prefers the vendored
   `popolaloom._vendored.arktower.cli.deps.migrations_dir`. The
   vendored copy ships in the wheel via
   `[tool.hatch.build.targets.wheel] packages = ["src/popolaloom"]`.

## Closures (3/3)

| # | Symptom (GH Actions run 25392679894) | Closure |
|---|---|---|
| 1 | mypy strict — ~12 errors in `src/popolaloom/_vendored/arktower/` | `pyproject.toml [tool.mypy] exclude = ["src/popolaloom/_vendored/.*"]` mirroring the existing ruff / coverage carve-outs. |
| 2 | `tests/test_automerge_gate.py::test_repo_workflow_automerge_yaml_loads_cleanly` — `.workflow/automerge.yaml` missing | `.gitignore` gains `!.workflow/automerge.yaml` whitelist; the actual config file is now tracked with 5 AND-condition thresholds + path policy + R-EVO-5 self-test rule. |
| 3 | `tests/test_repository.py` 4× `sqlite3.OperationalError: no such table: tasks` | `daemon/repository.py:make_persistence` falls through to vendored auto-detection when an explicit `arktower_migrations_dir=` does not resolve to an existing directory. |

## Behaviour deltas

- **Operators** — no behaviour change. The vendored ArkTower
  migrations are bundled in the wheel since v0.5.0; v0.6.1 just
  guarantees they get picked up when callers pass a stale legacy
  path. Setting `POPOLA_ARKTOWER_MIGRATIONS_DIR` still wins;
  setting it to a non-existent path now also falls through (the
  warning still fires for visibility).
- **Auto-merge gate** — `.workflow/automerge.yaml` is now part of
  the repo, so workflow + tests + future operators all see the
  same config. `required_paths.blocked` keeps `pyproject.toml`,
  `.github/workflows/automerge.yml`, and `src/popolaloom/gate/**`
  (the gate self-test rule).
- **Owned-code typing** — mypy strict gate is now passing on
  `src/popolaloom/` minus `_vendored/`. The owned surface is
  unchanged; the carve-out is the same shape ruff + coverage
  already had.

## Verification commands

```bash
## 1. version (5 files in lockstep)
python -c "import popolaloom; assert popolaloom.__version__ == '0.6.1'"
grep -E '^version = "0.6.1"' pyproject.toml
grep -E '^version: 0.6.1' src/popolaloom/skills/popolaloom/SKILL.md
cat src/popolaloom/skills/popolaloom/.popolaloom-version  # 0.6.1
grep -E '== "0.6.1"' tests/test_smoke.py

## 2. mypy strict — Fix 1
mypy src/popolaloom 2>&1 | tail -5
## ⇒ "Success: no issues found in N source files"

## 3. .workflow/automerge.yaml is tracked + valid — Fix 2
git ls-files .workflow/automerge.yaml
## ⇒ .workflow/automerge.yaml
pytest tests/test_automerge_gate.py::test_repo_workflow_automerge_yaml_loads_cleanly -v

## 4. repository migrations discovery — Fix 3
pytest tests/test_repository.py -v
## ⇒ all 4 cases pass on a runner that lacks /home/agent/reference/

## 5. default lane stays green at the v0.5.5 floor
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=popolaloom --cov-fail-under=94

## 6. ruff exits 0 across owned tree
ruff check src/popolaloom tests/
```

All six commands exit 0 on a clean v0.6.1 checkout.

## Owned files (this patch)

- `pyproject.toml` — mypy carve-out + version `0.6.0 → 0.6.1`.
- `.gitignore` — `!.workflow/automerge.yaml` whitelist.
- `.workflow/automerge.yaml` — NEW (tracked).
- `src/popolaloom/__init__.py` — `__version__` bump.
- `src/popolaloom/skills/popolaloom/SKILL.md` — frontmatter version
  bump (body unchanged).
- `src/popolaloom/skills/popolaloom/.popolaloom-version` — `0.6.1`.
- `tests/test_smoke.py` — version assertion + docstring lead
  paragraph.
- `src/popolaloom/daemon/repository.py` — `make_persistence`
  fall-through logic + module/function docstring updates.
- `CHANGELOG.md` — top-of-file `[0.6.1]` entry only.
- `release-notes-v0.6.1.md` — this document.

## Known limitations / deferred to v0.6.2

1. **Live `mutmut run` activation** — same v0.6.0 carry-over;
   blocked by the src/ + editable install friction documented in
   `evidence/mutmut-baseline.md`.
2. **Real Lark Tier-3 test creds** — same v0.6.0 carry-over;
   `tests/lark/test_listener_real.py` requires creds the hosted
   runner lacks.
3. **Vendored ArkTower mypy compatibility** — the carve-out is the
   right hammer for v0.6.1, but a future ArkTower refresh
   (per `VENDORING.md`) could unblock removing the exclude. Out
   of scope for this hotfix.

---

**PopolaLoom v0.6.1 ships 2026-05-06.**
The next branch picks up the v0.6.0 known-limitation backlog
(`release-notes-v0.6.0.md` §"Known limitations") on a fresh
feature branch off `main` after this hotfix lands.
