---
layout: default
title: Core Design Ideas
description: The 7 architectural choices that make PopolaLoom what it is — loom metaphor, sidecar daemon, file-backed handoff, 5-channel HITL, vendoring, Skill auto-discovery, and the GA stability boundary.
lang: en
translation_url: /zh/design-ideas.html
---

# Core Design Ideas

<!-- updated: 2026-05-10 -->

## The Loom Metaphor (织机)

PopolaLoom is called a loom because the project is not trying to be the smartest router in front of agent CLIs. A router chooses one destination and disappears. A loom keeps many strands under tension, records how they cross, and turns repeated passes into one cloth. Cursor, Claude, Codex, Kimi, Copilot, Lark, MCP, cloud workers, and human approvals are the strands; the task pool, event log, and handoff envelopes are the warp that keeps them aligned.

That metaphor explains the two verbs that matter most. The user-facing verb is `dispatch` because an operator wants to yank one strand into motion: run this prompt on this CLI, now. The project-facing verb is `weave` because useful work rarely ends at the first subprocess. A cloud run may ask for approval, relay its output to another repo, or leave a handoff file that a different CLI can inspect later. The loom keeps those moves visible instead of flattening them into chat history.

It also sets a boundary. PopolaLoom does not replace the agents, their native prompts, or their product-specific affordances. It gives them a shared task bus and enough structure for humans to reason about the fabric.

> See: `src/popolaloom/daemon/server.py::Popolad.dispatch_task` + [`README.md#what-is-popolaloom`](../README.md#what-is-popolaloom)

## Daemon-as-Sidecar (`popolad`)

The daemon is a sidecar because task lifetime must not belong to the terminal that happened to launch it. A library would die with the importing process. A plain CLI wrapper would lose supervision once the command returned. A hosted SaaS would move the local agent credentials, filesystem context, and operator trust boundary out of the machine. `popolad` stays local, binds a Unix Domain Socket at `$POPOLA_HOME/popolad.sock`, and owns only the coordination layer.

That choice gives PopolaLoom its operational invariants. Each task is still a subprocess of the native agent CLI, but it is spawned with `start_new_session=True` and watched by a wait-thread. The persistent SQLite task pool, vendored from ArkTower, records state across daemon restarts. The NDJSON event log under `$POPOLA_HOME/events/` gives `attach`, `status`, and `list` a single source of truth. A closed terminal or SSH disconnect is therefore an attach problem, not a task-loss problem.

The sidecar also keeps cloud and local paths symmetrical. Local tasks produce stdout/stderr events from a subprocess; cloud tasks produce Cursor REST / SSE state. Both are normalized into the same daemon status envelope.

> See: `src/popolaloom/daemon/supervisor.py` + [`docs/USER_GUIDE.md#architecture-deep-dive`](USER_GUIDE.md#architecture-deep-dive)

## File-Backed Handoff

PopolaLoom treats argv strings as ephemeral and files as auditable. Long prompts can hit shell quoting bugs, kernel argument limits, and adapter-specific parsing surprises. More importantly, an argv string is hard to review after the fact. A Markdown file with YAML front-matter can be opened, searched, archived, diffed, and attached to a PR as evidence.

Every dispatch therefore creates a handoff envelope before the adapter argv is built. The envelope captures schema version, target CLI, optional parent task, cwd, adapter extras, constraints, reason, tags, and the prompt body. The id is content-derived: target CLI plus prompt slug plus an eight-hex hash over the normalized payload. Same content maps to the same id, so `popola dispatch --replay <handoff_id>` is a real replay-by-id contract rather than a best-effort reconstruction.

The subprocess receives `POPOLA_HANDOFF_FILE` and `POPOLA_HANDOFF_ID` in its environment. The agent can inspect the original dispatch without PopolaLoom forcing a new prompt format on Cursor, Claude, or Codex. That is the key compatibility move: the loom stores the contract in a file while each strand remains native.

> See: `popolaloom.handoff.HandoffEnvelope` + [`docs/USER_GUIDE.md#hands-off-envelope`](USER_GUIDE.md#hands-off-envelope)

## 5-Channel HITL Fanout

Human-in-the-loop is intentionally redundant. A human may be in the IDE, in a terminal, in Lark, behind an MCP form, or on a future web surface. Treating any one channel as canonical would make the task brittle: the agent pauses, the operator misses the one surface, and the run stalls. PopolaLoom fans the same HITL request to Lark, IDE, CLI, MCP, and Web so the human can answer wherever attention already is.

Redundancy is controlled by one invariant: first responder wins. The HITL store records the pending request, and `mark_answered` is the cross-channel atomic gate. Once one channel submits an answer, the LangGraph state writeback emits `state.resumed`; late replies see the already-answered status rather than resuming the task twice. This makes fanout safe instead of noisy.

Lark is deliberately out-of-band. It reaches the operator even when the IDE is busy or the cloud run is remote, but it is not a hard dependency. If `lark-cli` is missing or `LARK_HITL_TARGET_OPEN_ID` is unset, PopolaLoom degrades to local NDJSON / CLI / MCP surfaces and logs a skipped reason. Missing Lark is not a task failure.

> See: `src/popolaloom/hitl/sync.py::mark_answered` + [`docs/USER_GUIDE.md#hitl-workflow`](USER_GUIDE.md#hitl-workflow)

## Vendoring Philosophy

PopolaLoom vendors ArkTower under `popolaloom._vendored.arktower` because the task pool is runtime infrastructure, not an optional integration. The daemon needs the task model, EventBus, SQLite repository, migration helper, and four SQL migrations before it can reliably persist and rehydrate work. At the time this surface was adopted, ArkTower was not published as a normal package that a fresh install could resolve everywhere.

The promise is practical: a fresh PopolaLoom install should work without sibling clones, private checkouts, or a runtime `git clone`. Vendoring the minimal ArkTower subset keeps `popola dispatch`, `popola list`, and `popola doctor` available in air-gapped or mirror-constrained environments. The vendored tree is namespaced so imports are explicit and do not collide with a user's own ArkTower checkout.

The trade-off is a release boundary. When ArkTower changes a surface that PopolaLoom vendors, PopolaLoom cuts its own release that refreshes the copy, records the upstream commit, and reruns the daemon persistence tests. Vendoring is not an excuse to drift silently; it is a portability contract with an audit trail.

> See: `src/popolaloom/_vendored/arktower/` + [`VENDORING.md`](../VENDORING.md)

## Skill = Auto-Discovery Contract

The Skill is not just prettier help text. It is the contract that lets host agents discover PopolaLoom as a capability at startup. `popola init` writes the canonical `SKILL.md` into Cursor, Claude, Codex, and Copilot locations; those hosts then load the verbs and operating rules without the user pasting a manual command reference into every conversation.

That matters because PopolaLoom is often invoked through natural language. A user says "dispatch this to cursor" or "attach to my running agents" and expects the host to know when to call the CLI, when to ask for a task id, and when to run `popola doctor`. The Skill gives the host a stable vocabulary, not just a dump of Typer help output. It also carries the install/update lifecycle: the `install-popola` Skill handles fresh setup, while the canonical `popola-loom` Skill handles day-to-day orchestration.

Versioning stays lockstep. The release process bumps package version and Skill front-matter together, and `popola doctor` reports drift when an on-disk Skill does not match the installed package. That makes auto-discovery auditable instead of magical.

> See: `src/popolaloom/skills/popola-loom/SKILL.md` + [`docs/USER_GUIDE.md#ide-integration`](USER_GUIDE.md#ide-integration)

## GA Stability Boundary (v0.9.0+)

v0.9.0 draws the line between dependable automation and experimental exploration. The stable set includes CLI verb names, flag spellings, daemon RPC paths, key `--json` schemas, public Python symbols, and `popolad.toml` section names. Existing automation can build on `popola list --json`, `popola status --json`, `POST /dispatch`, and the documented credential resolver without fearing patch-level churn.

That does not freeze the project in amber. Additive fields, new flags with safe defaults, and new endpoints are allowed. Experimental surfaces are marked as such: `popola cloud runs`, selected verbose cost fields, and SSE event sub-types can evolve inside v0.9.x with clear CHANGELOG notes. The rule is that the operator should never have to guess which parts are contracts and which parts are still research.

This boundary is why the docs distinguish local dispatch, Cursor Cloud REST, self-hosted worker handoff, Cloud HITL, and relay instead of hiding them behind one vague cloud feature. Stable names let teams script the boring parts; explicit experimental labels let maintainers continue tightening the loom.

> See: `docs/API_STABILITY.md#2-stable-surfaces-v09x-guaranteed` + [`docs/MIGRATION_v07_to_v09.md#v090--ga-deprecation-removals-pr-pending`](MIGRATION_v07_to_v09.md#v090--ga-deprecation-removals-pr-pending)
