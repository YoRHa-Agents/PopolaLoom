# Vendoring policy & refresh procedure

PopolaLoom v0.5.0 (Stage S1, decision **D5.7 LOCKED Path B**) vendors a
minimal subset of [ArkTower](https://github.com/YoRHa-Agents/ArkTower)
into `src/popolaloom/_vendored/arktower/`. This document explains
**why** the vendoring exists, **what** is included, and **how to
refresh** it from upstream.

> **Source:** `.local/memory/specs/popolaloom/v0.5.0-plan.md` §3 D5.7
> + `.local/memory/research/v0.5.0-skill-install-lark-research.md`
> §F.5 anomaly 2.

## Why vendor (and not depend on PyPI / lazy clone)

| Path | Why it failed for v0.5.0 |
|---|---|
| **A. PyPI publish** (`arktower>=0.1.0`) | `pip index versions arktower` returns *No matching distribution found* — ArkTower is not published on PyPI, and PopolaLoom has no authority to publish it. |
| **B. Vendor key modules** ← **LOCKED** | Zero external dependency, install is fully portable, ~1.4 KLOC of pure Python is small enough to maintain. |
| **C. Lazy clone** (`git clone` at first use) | Adds runtime requirements on `git` binary + network access, breaks `pip install popolaloom` in offline / air-gapped containers (DoD #1 conflict per v0.5.0 plan §3 D5.7). |

## What is vendored

We vendor only the symbols PopolaLoom imports at runtime, plus their
direct transitive dependencies. The full surface mirrors what
`Grep -r "from arktower" src/popolaloom/` returned at vendor time:

| File under `src/popolaloom/_vendored/arktower/` | Upstream source @ commit 467a087 | Imported by PopolaLoom |
|---|---|---|
| `core/event_bus.py` | `arktower/core/event_bus.py` | `daemon/event_bus.py`, `daemon/repository.py` |
| `core/models.py` | `arktower/core/models.py` | `daemon/event_bus.py` (TYPE_CHECKING), `daemon/repository.py` (transitive), `daemon/server.py` (lazy in `_maybe_create_arktower_task` + `rehydrate_from_persistence`) |
| `core/state_machine.py` | `arktower/core/state_machine.py` | transitive via `task_service.py` |
| `core/task_service.py` | `arktower/core/task_service.py` | `daemon/event_bus.py`, `daemon/repository.py` |
| `store/connection.py` | `arktower/store/connection.py` | `daemon/repository.py` |
| `store/migration.py` | `arktower/store/migration.py` | `daemon/repository.py` |
| `store/repository.py` | `arktower/store/repository.py` | transitive via `task_service.py` (TaskRepository protocol) |
| `store/sqlite_repository.py` | `arktower/store/sqlite_repository.py` | `daemon/repository.py` |
| `cli/deps.py` | `arktower/cli/deps.py` (only `migrations_dir()` is vendored) | `daemon/repository.py:_arktower_migrations_dir` |
| `migrations/00{1..4}_*.sql` | `migrations/00{1..4}_*.sql` | resolved at runtime via `cli/deps.py:migrations_dir()` |

We deliberately **do not vendor**:

- `arktower/core/normalizer.py` + `arktower/analysis/tag_extractor.py` — only re-exported via upstream `arktower/core/__init__.py`, never imported by PopolaLoom.
- `arktower/cli/{app,task_commands,server_commands,…}.py` — CLI surface is upstream-only.
- `arktower/api/`, `arktower/web/`, `arktower/mcp/`, `arktower/evaluation/`, `arktower/analysis/`, `arktower/archive/` — none of those are referenced from `src/popolaloom/`.
- `arktower/config.py` (`get_settings`) — only used by `cli/deps.py:ensure_cli_initialized()`, which PopolaLoom does NOT call. Our vendored `cli/deps.py` ships only the `migrations_dir()` helper, which has no `arktower.config` dependency.

## Pinned upstream commit

Vendored from commit **`467a087`** (branch `main`), captured on
**2026-05-05** during PopolaLoom v0.5.0 Stage S1.

The pin is recorded in `src/popolaloom/_vendored/arktower/__init__.py`
as the `__vendored_from__` and `__vendored_version__` module attributes.

## Refresh procedure (when upstream changes)

1. **Locate the upstream clone.** During v0.5.0 development the
   reference clone lives at `/home/agent/reference/ArkTower`. For other
   environments, clone fresh: `git clone https://github.com/YoRHa-Agents/ArkTower /tmp/ArkTower`.
2. **Pull / fetch the desired commit.** Note the new SHA.
3. **Re-grep PopolaLoom for the import surface.** Make sure the table
   above still matches the live imports:
   ```bash
   grep -RIn --include='*.py' -E '^\s*(from|import)\s+(popolaloom\._vendored\.)?arktower' src/popolaloom/
   ```
4. **Copy the matching files** verbatim into the corresponding location
   under `src/popolaloom/_vendored/arktower/`, then rewrite each
   internal `from arktower.X import Y` → `from popolaloom._vendored.arktower.X import Y`.
   The upstream `arktower/core/__init__.py` is **not** copied verbatim
   — instead, our `popolaloom/_vendored/arktower/core/__init__.py`
   re-exports the names PopolaLoom uses while skipping the
   `normalizer` re-export to avoid pulling in
   `arktower.analysis.tag_extractor`.
5. **Update the pin** in
   `src/popolaloom/_vendored/arktower/__init__.py`
   (`__vendored_from__` / `__vendored_version__`) and the date in this
   file.
6. **Verify**:
   ```bash
   pip check
   pytest tests/ -m "not slow and not nightly and not real_cli and not real_lark" --cov=src/popolaloom --cov-fail-under=91 -q
   python -c "from popolaloom._vendored.arktower.cli.deps import migrations_dir; assert migrations_dir().is_dir()"
   ```
7. **Bump the CHANGELOG** under `[Unreleased]` with a one-liner that
   says which upstream commit was synced and why.

## Coverage exclusion

`pyproject.toml` `[tool.coverage.run]` adds
`omit = ["src/popolaloom/_vendored/*"]` so the vendored code is
excluded from PopolaLoom's coverage gate. The rationale is recorded in
the inline pyproject comment: vendored code is upstream code with its
own test suite; PopolaLoom's coverage gate measures first-party code
only.

## When to stop vendoring

Vendoring is intended as the **interim** solution while ArkTower is
not yet published. Once ArkTower lands on PyPI (or a private index the
PopolaLoom user base can reach), a future patch may switch back to a
straight version-pin dep, delete `src/popolaloom/_vendored/arktower/`,
and revert the import-path rewrites. The
`[tool.hatch.metadata] allow-direct-references = true` setting is left
in place so a transitional `arktower @ git+https://...` pin can be
introduced without further pyproject edits.
