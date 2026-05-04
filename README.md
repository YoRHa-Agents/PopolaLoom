# PopolaLoom

> v0.4.0 GA — meta-orchestrator over local agent CLIs

PopolaLoom 是 DevolaFlow 之上的本机常驻"织机式"元编排器: 通过 `popolad` daemon
+ [ArkTower](https://github.com/YoRHa-Agents/ArkTower) 任务池 + LangGraph 子图,
在 Cursor / Claude / Codex 等多 CLI 之上提供依赖图、HITL、attach/resume 与跨终端
存活的一等公民支持。把"跨 CLI 派发 + 持久化进程总线 + Lark+IDE 三通道 HITL"
做成开发者桌面的 sidecar 服务。

## Status

**v0.4.0 GA** — Phase 1 closed (closes the v0.0.1 → v0.4.0 journey across
M1-M5 + 5 self-evolution rounds). See
[`release-notes-v0.4.0.md`](release-notes-v0.4.0.md) for the full
roadmap progression and known limitations.

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
| 980 default-lane tests / **91.36 %** coverage | ✅ live |
| 5 self-evolution rounds (v0.3.1..v0.3.5) — synthetic nines 0.90→1.00 | ✅ shipped |

## 5-minute Quickstart

```bash
git clone https://github.com/YoRHa-Agents/ArkTower /home/agent/reference/ArkTower
pip install -e "/home/agent/reference/ArkTower[dev]"
git clone <this repo> popolaloom && cd popolaloom
pip install -e ".[dev]"

# 1. start the daemon
popola popolad start

# 2. dispatch a task
popola dispatch "echo hello popola" --cli cursor

# 3. inspect the queue
popola list

# 4. evaluate self-bootstrap with 8-dim nines
popola eval run --output /tmp/nines.toml
cat /tmp/nines.toml

# 5. shut the daemon down
popola popolad stop
```

Or run the automated 5-step smoke:

```bash
bash examples/quickstart.sh
```

## Install

```bash
# 1. ArkTower (sibling project, same org YoRHa-Agents)
git clone https://github.com/YoRHa-Agents/ArkTower /home/agent/reference/ArkTower
pip install --index-url https://pypi.org/simple/ -e "/home/agent/reference/ArkTower[dev]"

# 2. PopolaLoom (this repo, editable + dev extras)
pip install -e ".[dev]"

# 3. Smoke check
python -c "import popolaloom; print(popolaloom.__version__)"
pytest tests/ -m "not slow and not nightly and not real_cli and not real_lark"

# 4. (optional) full slow lane (NFR + chaos + S1..S5)
pytest tests/ -m "slow"
```

## Architecture (TL;DR)

```text
Cursor / Claude / Codex IDE  ─┐
                              ├─→ popolaloom-mcp (stdio)  ─┐
$ popola CLI  ────────────────┘                              ├─→ popolad daemon (UDS)
                                                            │      ├─ ArkTower task pool (SQLite)
$ lark-cli (out: +send --card)  ←─── HITL renderer ◄───┐     │      ├─ LangGraph subgraph + interrupt()
$ lark-cli event consume (in)    ───→ LarkSupervisor ─┤     │      ├─ NDJSON event log (CloudEvents)
                                                       │     │      └─ 8-dim self-eval runner
                                                       └────►┘
```

See [`docs/DEMO.md`](docs/DEMO.md) for screenshots and full session walkthroughs.

## Design docs

设计、ADR 与 research dossier 全部位于 [`.local/memory/specs/popolaloom/`](.local/memory/specs/popolaloom/):

- [`spec.md`](.local/memory/specs/popolaloom/spec.md) — 项目规格 v1.0
- [`implementation-plan.md`](.local/memory/specs/popolaloom/implementation-plan.md) — 9-day 排期
- [`v0.2.0-plan.md`](.local/memory/specs/popolaloom/v0.2.0-plan.md), [`v0.3.0-plan.md`](.local/memory/specs/popolaloom/v0.3.0-plan.md) — phase plans
- `adrs/0001-arktower-as-task-pool-dependency.md` — 依赖 ArkTower 决策
- `adrs/0002-langgraph-as-graph-engine.md` — 选 LangGraph 决策
- `research/` — 上游 research dossier (06 决策路线 / 07 review / 08 ArkTower deep-dive)

`evidence/round-{1..5}-evidence.md` documents the v0.3.x self-evolution
rounds; `release-notes-v0.4.0.md` (after the GA bump) summarises the
full v0.0.1 → v0.4.0 journey.

## Sibling project

PopolaLoom 与 [ArkTower](https://github.com/YoRHa-Agents/ArkTower) 在同 org `YoRHa-Agents`
下,本项目通过 **本地 editable install** 复用 ArkTower 的任务池、FSM、EventBus、12 个
MCP tools 与 NiceGUI 仪表盘框架 (出处: `spec.md` §5.1)。Phase 1 不修改 ArkTower 源代码,
通过 `005_popolaloom_extensions.sql` migration 注入 PopolaLoom 自有表。

## License

MIT
