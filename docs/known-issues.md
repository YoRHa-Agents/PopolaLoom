---
layout: default
title: Known limitations
description: Documented limitations of PopolaLoom — workarounds and tracking links.
lang: en
translation_url: /zh/known-issues.html
---

# Known limitations

This page collects **documented limitations** of PopolaLoom that have known
workarounds and are tracked for resolution in a future release. Each entry
states the symptom, the recommended workaround, and links to the design
note and backlog row that own the fix. Items here are *expected to be
addressed in future versions* — file an issue under
[`.local/feedbacks/`](../.local/feedbacks/) if you hit one and the
listed workaround does not unblock you.

## v1.6.0 — Cursor server downgrades `env=machine→pool` (upstream regression)

<!-- updated: 2026-05-18 -->

**Limitation.** Cursor's Connect-RPC `StartBackgroundComposerFromSnapshot`
server SILENTLY downgrades the request body's
`env={"type":"machine","name":X}` to `env={"type":"pool"}` server-side.
The request body shape is accepted (200 + `bc_id` + `initial_run_id`), but
`GET /v1/agents/<bcId>` afterwards returns `env={"type":"pool"}` — the
`name` field is dropped. Verified empirically 2026-05-17 against
`api2.cursor.sh` with `env={"type":"machine","name":"popolaloom-dev-worker-v15"}`.

**Implication.** PopolaLoom CANNOT fix server-side routing. v1.6.0's
constraint #1 is satisfied at the popola layer (the worker process is My
Machines only via `popola cloud worker start` — no `--pool` flag — AND
the daemon supervisor rejects `extra.env.type='pool'` for
`cloud_target=self-hosted` with `error_kind="pool_forbidden_self_hosted"`),
but operators on a multi-worker account may see a different worker claim
the task than the named one.

**Workaround.** Run **one worker per repo**: with a single My-Machines
worker registered per workspace, the Cursor server's pool fallback
trivially picks the only matching worker. Operators with multiple workers
sharing the same repo should either consolidate to one worker per repo OR
accept that Cursor's server-side scheduling may route to a sibling.

**Tracking.** `BL-v1.6.x-cursor-env-machine-to-pool` in `CHANGELOG.md`
§Unreleased. Deferred to a future v1.7.x (or later) iteration if Cursor
exposes a non-downgrading RPC; popola cannot fix this client-side.

## v1.1.0 — experimental Path-B RPC endpoint may return HTTP 404

<!-- updated: 2026-05-11 -->

**Limitation.** `--auth-mode=session-jwt` now routes through popolad instead
of hard-exiting, but Cursor's private
`StartBackgroundComposerFromSnapshot` Connect-RPC service path is
reverse-engineered and may drift. A known live probe returned HTTP 404 for
`https://api2.cursor.sh/aiserver.v1.BackgroundComposerService/StartBackgroundComposerFromSnapshot`.

**Workaround.** Use the stable REST lane for production dispatches:

```bash
popola dispatch "..." --cli=cursor-cloud --auth-mode=rest
```

Keep Path-B for experimental advanced controls (`--mode`, `--effort`,
`--long-running`, `--preset=grind`). The gated test
`pytest -m real_cursor_cloud_jwt tests/cloud/internal/test_rpc_mock.py`
records the live endpoint shape when a valid Cursor session JWT and
`POPOLA_REAL_CURSOR_REPO_URL` are available.

**Tracking.** v1.2.0 follow-up: re-capture Cursor's current private RPC
descriptor and update `SERVICE_PATH` / method naming if upstream moved it.

## v0.9.0 — `popola init --target=cloud-only` scaffold expectations (Q-D-4 偏离默认)

<!-- updated: 2026-05-10 -->

**Limitation.** `popola init --target=cloud-only` (Q-D-4 偏离默认; first
shipped in v0.9.0 GA) writes a deliberately minimal 3-file project
scaffold — `popolad.toml` + `.env.example` + `Makefile` at the project
root — and is intentionally **disjoint** from the IDE-skill install
paths walked by `popola init [<ide>]` / `popola init all`. It does NOT
write `~/.cursor/skills/popola-loom/SKILL.md`, `~/.claude/skills/popola-loom/SKILL.md`,
`<repo>/.local/`, or any other surface; the scaffold's only purpose is
the cloud-dispatch project skeleton. Operators expecting cloud-only
mode to also register Skills per-IDE will not find them — that path
remains the explicit `popola init <ide>` verbs (or `popola init all`).

**Symptoms.**

- After `popola init --target=cloud-only`, host agents (Cursor / Claude /
  Codex / Copilot) do NOT auto-load the canonical `popola-loom` Skill —
  Skill discovery requires a separate `popola init <verb>` invocation.
- A second invocation in the same directory prints `SKIP <path>
  (already exists)` for each of the 3 files; pass `--force` to
  overwrite operator edits (or run `popola init --target=cloud-only
  --dry-run` first to preview).

**Workaround (compose with IDE skill installs when needed).**

A project that started as `--target=cloud-only` can later add per-IDE
Skill installs by running `popola init <verb>` separately (the file
paths are disjoint, so the two scaffolds compose cleanly):

```bash
popola init --target=cloud-only       # 3-file project scaffold
popola init cursor --global           # Cursor Skill at ~/.cursor/skills/popola-loom/
popola init all --global              # every detected IDE
```

**Design references.**

- [`docs/USER_GUIDE.md` — `popola init --target=cloud-only`](USER_GUIDE.md#popola-init---targetcloud-only-v090) — the full walkthrough.
- [`cloud-quickstart.sh`](../cloud-quickstart.sh) — copy-paste-ready bash bootstrap that wraps `popola init --target=cloud-only` → daemon start → first cloud dispatch.
- [`src/popolaloom/skills/install-popola/SKILL.md`](../src/popolaloom/skills/install-popola/SKILL.md) §"Cloud-only project init (v0.9.0 GA)" — operator-facing install fragment.
- DECISIONS.md OQ-1 (in `.local/.agent/active/v0.9.0-ga/DECISIONS.md`; local-only research note) — the v0.9.0 reconciliation that the cloud-only scaffold is deliberately project-only and does NOT install per-IDE Skills.

**Tracking.** No backlog row — this is the spec-locked behaviour for
v0.9.0 GA per Q-D-4 偏离默认; future minors may revisit if operator
feedback warrants a hybrid `--target=cloud-only --skill=cursor` shape.

## v0.9.0 — install canonical-path during PyPI deferral (Q-D-5 偏离默认)

<!-- updated: 2026-05-10 -->

**Limitation.** v0.9.0 GA is GitHub-Release-only; PyPI publish is
deferred to a v0.9.x patch (`BL-v0.9.x-PyPI` in
`.local/feedbacks/TRACKER.md`). The `./install.sh install` script
currently defaults to `--from=pypi` (per its prior stable surface),
so `./install.sh install` and the bare package-name installer path
both resolve to the **previous v0.8.x stable line** until the v0.9.x
PyPI patch lands.

**Symptoms.**

- `./install.sh install` (default flags) succeeds but `popola version`
  prints `popolaloom 0.8.x` instead of `popolaloom 0.9.0`.
- The bare package-name installer path resolves to the latest PyPI
  release, which is currently v0.8.x.
- `./install.sh install --from=git --version=v0.9.0` is rejected with
  the validation error `--version=X.Y.Z requires --from=pypi`.

**Workaround (v0.9.0-specific install).**

Use one of the two v0.9.0-canonical install paths:

```bash
# Option A — canonical, tag-pinned (always works for v0.9.0):
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0

# Option B — repo-root unified installer with --from=git (auto-tracks
# main; equivalent to v0.9.0 immediately post-tag):
./install.sh install --from=git
```

The PyPI promotion patch (`BL-v0.9.x-PyPI`) will land a follow-on
RELEASE_NOTES top-of-file callout + CHANGELOG `### Added` entry; once
published, both the bare package-name installer path and `./install.sh install`
(default) will resolve to v0.9.x normally.

**Design references.**

- [`README.md` §Install](../README.md#install) — the v0.9.0 install canonical-path callout + alternate paths.
- [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) — top-of-file Q-D-5 偏离默认 callout (v0.9.0 GA).
- [`docs/MIGRATION_v07_to_v09.md` §TL;DR](MIGRATION_v07_to_v09.md#tldr) — operator-facing v0.7.x → v0.9.0 install path.

**Tracking.** Backlog row `BL-v0.9.x-PyPI` (`.local/feedbacks/TRACKER.md`) covers the PyPI promotion patch; an `install.sh` rewire to default to `--from=git` (or to lift the `--version` ⇒ `--from=pypi` guard) is tracked as a Stage-5 follow-up under `BL-v0.9.x-install-sh-rewire`.

## v0.8.6 — Cloud task hydration after daemon restart

<!-- updated: 2026-05-08 -->

**Limitation.** PopolaLoom cloud-runtime tasks
(`popola dispatch ... --cli=cursor-cloud`) carry two pieces of in-memory
state inside the `popolad` daemon process:

1. The `CloudPollLoop` thread (`src/popolaloom/daemon/cloud_poller.py`)
   that drives `TaskHandle.cloud_phase` updates by polling
   `GET /v1/agents/{id}/runs/{run_id}` every `interval_s` (default 2 s).
2. Any active SSE attach session — the `SSEReader` pump opened by
   `popola attach --follow` against
   `GET /v1/agents/{id}/runs/{run_id}/stream`, including its
   `Last-Event-ID` resume cursor.

Both are **lost when `popolad` restarts**. The persistent state in sqlite
(the `TaskHandle` row, the `popola_dispatch` row, and the appended
`event_log.jsonl` events) survives the restart, so the task itself is not
orphaned, but the poll loop and any SSE attach session must be
re-initialized manually before the daemon resumes emitting
`cloud.run_status` and `cloud.sse.*` events for that task.

**Symptoms.**

- After `popolad restart`, `popola list` still shows the cloud task with
  its prior `runtime=cloud` and last-known `cloud_phase`, but no new
  `cloud.run_status` events arrive until the next poll cycle (cold start).
- `popola attach <task_id>` immediately after a restart may show no new
  events until that next poll cycle completes; any pre-restart SSE session
  must be re-established because the server-side stream was dropped along
  with the daemon process.

**Workaround.**

- Re-issue `popola attach <task_id>` after the restart. The daemon's
  rehydration logic (introduced in v0.7.1+; see
  `Supervisor._rehydrate_from_persistence`) resumes the poll loop on the
  next dispatch event, and the new `attach` opens a fresh SSE session
  (subject to the Cursor server's stream-replay window).
- For batch or unattended workflows that cannot tolerate the cold-start
  gap, prefer keeping `popolad` up across the task lifetime, or wait for
  the persistent-cursor work tracked under `BL-v0.8.6-1` (see *Tracking*
  below).

**Design references.**

- [`state-source-of-truth.md` §5 failure mode #6](../.local/research/v0.8.6_sse/state-source-of-truth.md#5-failure-modes) —
  *"Stream open but poller dies (e.g., daemon-thread crash)"*: the same
  diagnosis applies to a full daemon restart, with the planned
  `cloud.poller_lost` heartbeat as the follow-up signal.
- [`state-source-of-truth.md` §8 out-of-scope](../.local/research/v0.8.6_sse/state-source-of-truth.md#8-out-of-scope) —
  a daemon-resident persistent SSE subscription is explicitly deferred to
  ≥v0.8.7 if user-driven need emerges; cross-session dedup persisted to
  disk likewise stays in-memory only for v0.8.6.

**Tracking.** Backlog row
[`BL-v0.8.6-1` in `.local/feedbacks/TRACKER.md`](../.local/feedbacks/TRACKER.md)
covers the cursor-persistence + SSE `last_event_id` snapshot work that
would close this limitation.

## v0.8.7 — Cloud HITL transport (anti-patterns)

<!-- updated: 2026-05-08 -->

**Scope.** This section is the **only** place in the in-tree documentation
set where the misleading transport wording (public IP, port-forward,
residential NAT, inbound port, VPN tunnel) may appear, and it appears as
an **explicit "do NOT do this" callout**. A session-scope CI grep guard
(`tests/conftest.py::test_misleading_wording_guard`) enforces that
contract: the regex
`(?i)(public\s+ip|port[- ]?forward|residential\s+NAT|inbound\s+port|VPN\s+tunnel)`
is asserted to match **only** this file (or files under
`.local/research/`, which is out-of-tree research material and is not
scanned by the guard). Any new in-tree hit fails CI at PR time.

**Background.** Cursor Cloud Agents access PopolaLoom HITL via one of
**two — and only two — supported transports** in v0.8.7
([`deployment-modes.md` §1](../.local/research/v0.8.7_hitl/deployment-modes.md)):

- **γ — Worker stdio MCP (first-class).** `popolaloom-mcp` runs as a
  `command/stdio` MCP server **on a Cursor Self-Hosted Worker** (or a
  personal "My Machines" worker). The worker reaches `popolad` over
  loopback or VPC; the Cursor cloud reaches the worker over a long-lived
  **outbound HTTPS** session.
- **β — HTTP MCP (backend-proxied).** The team registers an HTTPS MCP URL
  reachable by Cursor's backend; tool calls are proxied through the
  backend, so MCP credentials never enter the cloud agent VM.

The following five configurations are **NEVER required for either γ or
β** and **MUST NOT** be configured, documented, or recommended for v0.8.7
cloud HITL:

1. **Public IP** on the operator's `popolad` host. `popolad` binds to
   `127.0.0.1` (loopback) or RFC1918 (private VPC) only; under γ the
   worker reaches it over loopback / VPC, and under β a hardened HTTPS
   gateway in the same VPC fronts it. Exposing `popolad` on a public
   interface is a configuration error.
2. **Port-forwarding** through residential NAT (e.g., UPnP, ISP CGNAT
   workarounds, manual router rules). The γ worker is **outbound-only**
   from the operator's network perspective — it initiates and holds the
   long-lived HTTPS session to `api2.cursor.sh` and `api2direct.cursor.sh`
   ([`deployment-modes.md` §6 minimal connectivity](../.local/research/v0.8.7_hitl/deployment-modes.md)).
3. **Residential NAT** as a primary deployment mode. β explicitly
   requires "a stable, internet-reachable HTTPS URL"; ephemeral
   residential NAT URLs and `localhost` are not supported by Cursor's
   backend MCP proxy
   ([`deployment-modes.md` §3.1](../.local/research/v0.8.7_hitl/deployment-modes.md)).
4. **Inbound port** on the operator's host or LAN edge. Both γ and β
   route traffic through outbound-only sessions (γ on the worker side,
   β on the Cursor-backend side); no inbound listener on the operator's
   host is part of the supported topology
   ([`mcp-tool-contract.md` §1](../.local/research/v0.8.7_hitl/mcp-tool-contract.md)).
5. **VPN tunnel** between Cursor's cloud and the operator's network. The
   γ outbound HTTPS session and the β backend-proxied HTTPS request are
   the **only** sanctioned data planes; rolling a VPN tunnel for HITL
   purposes is unnecessary and out of scope for v0.8.7 support.

If you have **neither** a self-hosted worker option **nor** a public
HTTPS gateway, defer HITL to a future SaaS HITL gateway tracked under
Stage 3 / v0.9+
([`deployment-modes.md` §4 row D](../.local/research/v0.8.7_hitl/deployment-modes.md)).
The broad-audience `popola dispatch ... --cli=cursor-cloud` REST path
remains fully usable without HITL — only the "human approval over Lark"
sub-flow has the γ / β prerequisites.

**See also.**

- [`deployment-modes.md` §5 lateral-movement checklist](../.local/research/v0.8.7_hitl/deployment-modes.md) —
  the 10-item gate that audits both supported transports for residual
  attack surface.
- [`SECURITY_CHECKLIST.md` §8 M1](../.local/.agent/active/v0.8.7-cloud-hitl-prod/SECURITY_CHECKLIST.md) —
  the release-gate item this callout + the conftest guard implement
  jointly.

**CHANGELOG follow-up (T2.3.3).** A `## [0.8.7]` line — *"doc-only
correction: cloud HITL transport story aligned with `deployment-modes.md`"* —
should land in `CHANGELOG.md` as part of T2.3.3 (W2.3 of `PLAN.md`); it
is intentionally **not** added by this task (T2.2.2) to keep the
file-ownership matrix disjoint.

## v1.0.0-pre.1 — Cursor cloud auto-create-PR is occasionally flaky

<!-- updated: 2026-05-11 -->

**Limitation.** Even when `autoCreatePR=true` is set on the dispatch and
the agent successfully `git push`es the feature branch (the upstream
tracking ref is configured correctly), the Cursor Cloud Agent run may
return `"No branch name available for PR creation"` and the run finishes
without opening the PR. The commit is not lost — it is reachable from the
branch tip on GitHub — but the PR step itself silently no-ops on
Cursor's side.

**Symptoms.**

- The dispatched run reaches `completed` with the agent commit visible at
  `https://github.com/<owner>/<repo>/commits/<branch>`, yet
  `https://github.com/<owner>/<repo>/pulls` shows no new PR.
- The run's status JSON (`popola attach <task_id> --no-follow`) contains
  a `"No branch name available for PR creation"` line in the trailing
  events.
- This was observed on 2026-05-10 during the v1.0.0-pre.1 end-to-end
  smoke (PR #28 — see
  [`feedback_for_v1.0.0-pre.1.md` §2.1](../.local/feedbacks/feedback_for_v1.0.0-pre.1.md)),
  and was not deterministically reproducible on a re-run.

**Workaround.**

Manually open the PR with `gh pr create` once the agent's commit is on
the remote:

```bash
gh pr create \
  --base main \
  --head <feature-branch-the-agent-pushed-to> \
  --title "<your title>" \
  --body  "$(cat <<'EOF'
<your description>
EOF
)"
```

This works because the commit, branch tracking, and remote head are all
already correct — only the Cursor-side PR step failed. Use this whenever
the run reports `"No branch name available for PR creation"`.

**Tracking.** This is upstream-side flakiness on Cursor's
auto-create-PR pipeline; PopolaLoom has no code path to fix it. Tracked
in [`feedback_for_v1.0.0-pre.1.md` §2.1](../.local/feedbacks/feedback_for_v1.0.0-pre.1.md)
as a documentation-only mitigation for v1.0.0 GA.

## v1.0.0-pre.1 — Self-hosted worker pushes to the dispatch-time branch

<!-- updated: 2026-05-11 -->

**Limitation.** When you dispatch via `popola dispatch
--cloud-target=self-hosted --worker-name=<W>`, the Cursor Cloud Agent VM
runs against the worker's bound `git` checkout and uses **whichever
branch the worker is currently on at dispatch time** as the target for
its commit + push. The agent does NOT auto-create a new feature branch
the way `--cloud-target=cursor-managed` does — it commits straight onto
your current branch and pushes that branch to `origin`.

**Symptoms.**

- The agent's commit lands on the same branch from which you ran
  `popola dispatch ...`, **overwriting** any in-progress work on that
  branch (the local working tree is unaffected, but the remote branch
  head moves under you).
- During the v1.0.0-pre.1 end-to-end smoke (see
  [`feedback_for_v1.0.0-pre.1.md` §2.2](../.local/feedbacks/feedback_for_v1.0.0-pre.1.md)),
  the agent's commit was force-pushed to the operator's integration
  branch `feature/v0.9.10-web-demo-init-prefs`; the operator had to
  cherry-pick the agent commit onto an isolated branch
  (`docs/readme-banner-v1.0.0-pre.1`) and reset the integration branch
  back to its prior tip.

**Workaround (one of three).**

1. **Isolate the worker on a fresh branch BEFORE dispatching** — `cd`
   into the worker's checkout and `git checkout -b <agent-task-branch>`,
   then dispatch. The agent will commit + push to
   `<agent-task-branch>`, leaving your integration branch untouched.

   ```bash
   cd "$(popola cloud worker info --name $WORKER --print=worker-dir)"
   git checkout -b agent-task-$(date +%s)
   popola dispatch --cloud-target=self-hosted --worker-name=$WORKER \
     "<prompt>"
   ```

2. **Use `--cloud-target=cursor-managed` instead** — the Cursor cloud VM
   auto-creates a feature branch (`cursor/<slug>-<id>`) per dispatch, so
   your local branches are never touched. Trade-off: requires the Cursor
   GitHub App installation (org-level), see Q-9 in
   [`./.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md`](../.local/.agent/active/v0.10.0-cloud-dispatch-clarity/DECISIONS.md).

3. **After-the-fact rescue** — if the agent already pushed onto the
   wrong branch, recover with:

   ```bash
   git fetch origin
   git checkout -b agent-rescue-$(date +%s) origin/<wrong-branch>
   git checkout <wrong-branch> && git reset --hard <pre-agent-tip>
   git push --force-with-lease origin <wrong-branch>
   # The agent commit is preserved on the rescue branch.
   ```

**Tracking.** This is a self-hosted-worker behaviour by design (the
worker uses its bound checkout's current branch). PopolaLoom v1.0.0 GA
documents the workaround; an opt-in `--auto-isolate-branch` flag on
`popola dispatch` is a candidate for v1.1+ (`BL-v1.x-auto-isolate-branch`).
See [`feedback_for_v1.0.0-pre.1.md` §2.2](../.local/feedbacks/feedback_for_v1.0.0-pre.1.md).
