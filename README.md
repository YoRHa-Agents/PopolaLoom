# PopolaLoom

> v0.5.5 — Phase 2 prelude polish: meta-orchestrator with Skill, multi-IDE installer, interactive setup wizard, and 5-loop self-improvement series

PopolaLoom 是 DevolaFlow 之上的本机常驻"织机式 (loom) / 编织者 (weaver)"
元编排器: 通过 `popolad` daemon + vendored
[ArkTower](https://github.com/YoRHa-Agents/ArkTower) 任务池 + LangGraph 子图,
在 Cursor / Claude Code / Codex / Kimi / GitHub Copilot 等多 CLI 之上提供
依赖图、HITL、attach/resume 与跨终端存活的一等公民支持。把"跨 CLI 派发 +
持久化进程总线 + Lark + IDE 三通道 HITL"做成开发者桌面的 sidecar 服务。

v0.5.0 把上述能力"装得动 + 看得见": 一份 Anthropic-baseline 的
[`SKILL.md`](src/popolaloom/skills/popolaloom/SKILL.md) 让 host agent
(Cursor / Claude / Codex / Copilot) 自动发现 PopolaLoom; `popola init`
一键 file-copy 到所有检测到的 IDE; `popola doctor` 一次性审计 skill /
daemon / lark-cli / ArkTower 四个子系统的健康。

v0.5.{1,2,3,4,5} 是收口 5 个 self-improvement loop, 把覆盖率从 91 %
推到 94 %+, mutmut 声明面从 1 个模块扩到 5 个, 修 CI red-build,
补 NFR 量化基线, 加 Lark graceful shutdown, 加 `popola init
--interactive` 向导, 把 README + DEMO 与 5 个 loop 的实证齐平 — 为
v0.6.0 phase 2 multi-agent dispatch 做整合前的最后清单。

## Status

**v0.5.5 — Phase 2 prelude polish** — closes Loop 5 of the
v0.5.x → v0.6.0 self-improvement series. See
[`release-notes-v0.5.5.md`](release-notes-v0.5.5.md) for the full
write-up + the 5-loop journey table; the v0.5.0 baseline is still in
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) for the
v0.0.1 → v0.5.0 rollup.

| Capability | Status |
|---|---|
| popolad daemon (UDS RPC, 7 dispatch verbs) | ✅ live |
| 7 dispatch primitives (dispatch / attach / probe / relay / supervise / federate / cancel) | ✅ live |
| MCP stdio server (Cursor / Claude IDE) | ✅ live |
| LangGraph dev↔test subgraph + HITL `interrupt()` | ✅ live |
| ArkTower task pool persistence (cross-restart rehydrate) | ✅ live |
| HITL handle-ability (5 channels: lark / ide / cli / mcp / web) | ✅ live |
| Lark 双向 (out: `+send --card`, in: `event consume` listener) | ✅ live |
| 8-dim PopolaLoom-nines self-eval | ✅ live |
| devola-flow dual gate (inner ≥ 0.85 + outer Δ ≥ 0.02) | ✅ live |
| Auto-merge gate (5 AND conditions) | ✅ live |
| 5/5 self-bootstrap scenarios (S1..S5 real, 3× consecutive PASS) | ✅ live |
| **v0.4.1**: proactive Lark terminal-state notifications + auto-start LarkSupervisor | ✅ live |
| **v0.5.0**: vendored ArkTower (`pip install popolaloom` works on any host) | ✅ live |
| **v0.5.0**: `popola init` 8 verbs + 8 modifiers (Cursor / Claude / Codex / Copilot / local / all) | ✅ live |
| **v0.5.0**: canonical SKILL.md (~ 10 KB / ~ 2 655 tokens, 7 sections) | ✅ live |
| **v0.5.0**: `popola skill {install, doctor, upgrade}` + `popola doctor` aggregate verb | ✅ live |
| **v0.5.1**: CI runner-writability fix + 90 new error-path tests; coverage 91.15 % → 92.56 % | ✅ live |
| **v0.5.2**: NFR-2 / NFR-9 benchmarks + Lark supervisor graceful shutdown + auto-merge gate align; coverage 92.56 % → 93.37 % | ✅ live |
| **v0.5.3**: vendored arktower CI imports + ruff lint clean + SKILL.md `--cli-flag` docs | ✅ live |
| **v0.5.4**: mutmut declarative surface 1 → 4 modules + 63 edge-case + mutation-kill tests; coverage 93.37 % → 93.94 % | ✅ live |
| **v0.5.5**: `popola init --interactive` wizard + mutmut surface 4 → 5 + vendored migration tests + coverage 93.94 % → 94.60 % | ✅ live |
| **v0.6.0**: v0.5.x consolidation + auto-merge gate align (92 → 94) + cursor `cli_args` / `cmd_args` passthrough (`--trust`, `--no-color`, …) | ✅ live |
| 1380+ default-lane tests / **≥ 94 %** coverage (post-v0.6.0; cumulative +279 tests / +3.47 pp across the v0.5.x → v0.6.0 chain) | ✅ live |

## Loop-driven self-improvement (v0.5.x → v0.6.0)

The v0.5.x patch series is a deliberate **5-loop self-improvement
chain** between the v0.5.0 release (Phase 2 prelude) and the
forthcoming v0.6.0 minor (Phase 2 — multi-agent dispatch). Each loop
is a single-commit patch on `feature/v0.5.0-skill-install` that picks
one dimension — CI greenness, NFR baseline, lint cleanliness,
mutation kill rate, UX polish — and pushes it forward measurably,
with the hard-gate constraint "never regress the previous loop's
metric". The chain is intentionally modeled on the 5-round v0.3.x
self-evolution loop that took PopolaLoom from `v0.3.0 → v0.3.5`
(documented under `evidence/round-{1..5}-evidence.md`); the
difference is that v0.5.x has a fixed *terminal target* (v0.6.0
consolidation) rather than an open-ended evolution objective.

Each loop is sized for a single ~ 50 minute L3-implement-team
session: read the previous loop's "Known limitations / deferred"
section in its release notes, pick the highest-leverage 3-5
deliverables, ship them as a single commit with a release-notes
file + `CHANGELOG.md` entry + version bump, then hand off the
"deferred" set to the next loop. The pattern produces a paper-trail
the next loop's first action is to consume — see the loop ledger in
[`release-notes-v0.5.5.md`](release-notes-v0.5.5.md) for the full
table (Loop / Version / Closure / Tests Δ / Coverage Δ).

## 5-minute Quickstart

```bash
pip install popolaloom    # or: pip install -e . from a clone for development

popola init               # auto-detect Cursor / Claude / Codex / Copilot + register SKILL.md
popola init --interactive # alternative: wizard mode (v0.5.5+; prompts per IDE + scope)
popola popolad start      # boot the daemon (UDS bind under $POPOLA_HOME)
popola dispatch "echo hello popola" --cli=cursor   # or any other registered CLI
popola list               # see active tasks
popola attach <task_id> --follow                   # tail the SSE event stream
popola doctor             # health-check skill + daemon + lark-cli + ArkTower
```

Or run the automated 6-step smoke (now includes `popola init --dry-run`):

```bash
bash examples/quickstart.sh
```

### Verification commands (default-lane gates)

The CI matches the developer-VM gates documented in
[`release-notes-v0.5.0.md`](release-notes-v0.5.0.md) §Verification
plus the post-v0.5.5 floor bump:

```bash
# 1. version
python -c "import popolaloom; assert popolaloom.__version__ == '0.5.5'"

# 2. default lane + coverage gate (post-v0.5.5: fail_under = 94)
pytest -m "not slow and not nightly and not real_cli and not real_lark" \
  --cov=src/popolaloom --cov-fail-under=94

# 3. ruff lint clean (vendored ArkTower excluded)
ruff check src/popolaloom tests/

# 4. mutmut declarative surface (5 modules at v0.5.5)
git grep "paths_to_mutate" pyproject.toml
```

## Skill (v0.5.0)

PopolaLoom ships a canonical [`SKILL.md`](src/popolaloom/skills/popolaloom/SKILL.md)
at `src/popolaloom/skills/popolaloom/SKILL.md` (~ 10 KB / ~ 2 655 tokens,
7 sections, frontmatter `name: popolaloom` per Q5-1 lock). Host agents
(Cursor / Claude Code / Codex CLI / GitHub Copilot) auto-discover
PopolaLoom via the file-system convention; once installed, asking
"派发任务给 cursor 跑 X" or "list my running agents" automatically
routes to the right `popola` verb.

### Per-IDE install paths

| IDE | Scope | Install path |
|---|---|---|
| Cursor | global | `~/.cursor/skills/popolaloom/SKILL.md` |
| Cursor | project | `<repo>/.cursor/skills/popolaloom/SKILL.md` |
| Claude Code | global | `~/.claude/skills/popolaloom/SKILL.md` |
| Claude Code | project | `<repo>/.claude/skills/popolaloom/SKILL.md` |
| Codex | global | `$CODEX_HOME/skills/popolaloom/SKILL.md` (default `~/.codex/`) |
| Copilot | project-only | `<repo>/.github/copilot-instructions.md` (single-file flatten) |

`popola init` mirrors DevolaFlow's `devola-init` 14-row dispatcher (per
Q5-2 lock): 8 verbs (`cursor` / `claude` / `codex` / `copilot` / `local`
/ `all` / `--list` / no-args auto-detect) × 8 modifiers (`--global` /
`--project` / `--mode={core,standard,full}` / `--no-compile` /
`--with-examples` / `--no-with-examples` / `--dry-run` /
`--popolaloom-version`).

Every install verb is **idempotent**: a second invocation prints
`SKIP <path> (already installed)` instead of overwriting operator edits.
A `.popolaloom-version` marker is written beside the SKILL.md so
`popola doctor` can detect drift (`v0.4.1 (expected v0.5.0)` etc.) when
you upgrade the wheel without re-running install.

### Upgrade workflow

```bash
pip install --upgrade popolaloom
popola skill upgrade --target=cursor   # backup .popolaloom-bak.<ts> + overwrite from wheel
popola skill upgrade --target=all      # cycle every detected install
popola init                             # idempotent fallback (no overwrite)
```

`popola skill upgrade` differs from `popola init` / `popola skill
install` in that it **always overwrites** the on-disk SKILL.md with the
wheel-bundled canonical version (after writing a timestamped backup
sibling). Use it after `pip install --upgrade` to bring already-installed
targets back in lockstep with the new wheel version.

### `popola doctor` audit

```bash
popola doctor          # human-readable table; exit 0 by default
popola doctor --strict # exit 1 if any subsystem reports FAIL
popola doctor --json   # machine-readable 4-section envelope
```

The aggregator runs four subsystem audits in one go:

1. **Skill audit** — every `(target, scope)` slot from `SKILL_TARGETS`;
   reports `OK` / `MISS` / `DRIFT` (drift = installed version ≠ wheel
   version).
2. **Daemon audit** — `GET /probe` over the popolad UDS socket;
   `OK` (with pid + uptime) when the daemon is up, `FAIL` otherwise.
3. **Lark audit** — `lark-cli` on PATH + `LARK_HITL_TARGET_OPEN_ID`
   env var; `OK` when both are present, `WARN` when binary exists but
   env is unset, `OFF` (informational, not a fail) when binary is missing.
4. **ArkTower audit** — vendored module imports cleanly + the two
   PopolaLoom migrations (`005_popolaloom_extensions.sql` /
   `006_popola_hitl.sql`) are on disk; `WARN` when migrations are
   missing (the daemon falls back to a no-op runner).

## Install

PopolaLoom v0.5.0 ships with ArkTower **vendored** under
`src/popolaloom/_vendored/arktower/` (per
[`VENDORING.md`](VENDORING.md), Stage S1 / D5.7 LOCKED Path B); a
fresh `pip install popolaloom` no longer requires a sibling ArkTower
clone, and the previous
`arktower @ file:///home/agent/reference/ArkTower` direct reference is
gone from `pyproject.toml`.

```bash
pip install popolaloom              # from PyPI (pending publish — see VENDORING.md)
# — OR —
pip install -e ".[dev]"             # from a clone, with dev extras

python -c "import popolaloom; print(popolaloom.__version__)"   # → 0.5.5
pytest tests/ -m "not slow and not nightly and not real_cli and not real_lark"

pytest tests/ -m "slow"             # optional: full slow lane (NFR + chaos + S1..S5)
```

> **PyPI publish status**: `popolaloom` is not yet on PyPI. Once
> ArkTower lands on PyPI (or a private index), the
> `popolaloom._vendored.arktower` directory can be deleted and
> `pyproject.toml` reverted to a normal version pin (`arktower>=0.1`).
> The `[tool.hatch.metadata] allow-direct-references = true` setting
> is preserved for that transition. See
> [`VENDORING.md`](VENDORING.md) "When to stop vendoring".

## Lark notifications (v0.4.1+)

PopolaLoom ships proactive Lark interactive cards on every task
terminal state. Set the env vars below (no daemon restart needed)
and `popola popolad start`:

| Env var | Purpose | Default |
|---|---|---|
| `LARK_HITL_TARGET_OPEN_ID` | recipient open_id (HITL prompts + terminal cards) | (unset → Lark silent) |
| `LARK_NOTIFY_TARGET_OPEN_ID` | dedicated terminal-state recipient | falls back to `LARK_HITL_TARGET_OPEN_ID` |
| `LARK_NOTIFY_ON_COMPLETED` | `task.completed` → green card | `1` (ON) |
| `LARK_NOTIFY_ON_FAILED` | `task.failed` → red card | `1` (ON) |
| `LARK_NOTIFY_ON_CANCELED` | `task.canceled` → yellow card | `1` (ON) |
| `LARK_NOTIFY_ON_CANCEL_ESCALATED` | `cancel → SIGKILL` → orange card | `0` (OFF) |
| `LARK_NOTIFY_PROMPT_TRUNCATE` | prompt summary char cap (50–2000) | `200` |

Per Q5-3 lock, v0.5.0 inherits the v0.4.1 default (3 ON / 2 OFF)
unchanged; `popola init` does not export env vars (operator manages
`~/.bashrc` / `~/.zshrc` directly), but `popola doctor` displays the
current values so you can audit what the daemon will do at next boot.
When `lark-cli` is missing or the target open_id is unset, the daemon
silently degrades to NDJSON-only event logging (per the
"degrade gracefully" + "No Silent Failures" double constraint — every
skip emits a single `lark.supervisor.skipped reason=...` INFO line).

## Architecture (TL;DR)

```text
Cursor / Claude / Codex / Copilot IDE  ─┐
   ↑ SKILL.md auto-discovery            ├─→ popolaloom-mcp (stdio)  ─┐
   ↑ (popola init writes here)          ┘                              ├─→ popolad daemon (UDS)
                                                                       │      ├─ ArkTower task pool (vendored, SQLite)
$ popola CLI  ────────────────────────────────────────────────────────┘      ├─ LangGraph subgraph + interrupt()
$ lark-cli (out: +send --card) ←─── HITL renderer + Lark notifier ◄───┐      ├─ NDJSON event log (CloudEvents)
$ lark-cli event consume (in)  ───→ LarkSupervisor ──────────────────┤      └─ 8-dim self-eval runner
                                                                       └────────► popola doctor (skill / daemon / lark / arktower)
```

See [`docs/DEMO.md`](docs/DEMO.md) for screenshots, full session
walkthroughs, and the new "v0.5.0 Skill installation walkthrough"
section.

## Design docs

设计、ADR 与 research dossier 全部位于
[`.local/memory/specs/popolaloom/`](.local/memory/specs/popolaloom/):

- [`spec.md`](.local/memory/specs/popolaloom/spec.md) — 项目规格 v1.0
- [`implementation-plan.md`](.local/memory/specs/popolaloom/implementation-plan.md) — 9-day 排期
- [`v0.2.0-plan.md`](.local/memory/specs/popolaloom/v0.2.0-plan.md), [`v0.3.0-plan.md`](.local/memory/specs/popolaloom/v0.3.0-plan.md), [`v0.5.0-plan.md`](.local/memory/specs/popolaloom/v0.5.0-plan.md) — phase plans
- `adrs/0001-arktower-as-task-pool-dependency.md` — 依赖 ArkTower 决策
- `adrs/0002-langgraph-as-graph-engine.md` — 选 LangGraph 决策
- `research/v0.5.0-skill-install-lark-research.md` — DevolaFlow `devola-init` 实证 + Anthropic Skill baseline + multi-IDE 路径表

`evidence/round-{1..5}-evidence.md` documents the v0.3.x self-evolution
rounds; `release-notes-v0.4.0.md` and `release-notes-v0.4.1.md` cover
the v0.4.x phase 1; [`release-notes-v0.5.0.md`](release-notes-v0.5.0.md)
summarises the v0.5.0 Phase 2 prelude (Skill + multi-IDE installer +
`popola doctor`).

## Sibling project

PopolaLoom 与 [ArkTower](https://github.com/YoRHa-Agents/ArkTower) 在同 org `YoRHa-Agents` 下,
PopolaLoom v0.5.0 起 **vendor** ArkTower 的关键子集 (TaskService /
EventBus / SqliteTaskRepository / 4 个 schema migrations) 进
`popolaloom._vendored.arktower`, 让 `pip install popolaloom` 在任意机器
上能 0 错误装上。Refresh 流程见 [`VENDORING.md`](VENDORING.md)。

## License

MIT
