---
layout: default
title: Known limitations
description: Documented limitations of PopolaLoom — workarounds and tracking links.
---

# Known limitations

This page collects **documented limitations** of PopolaLoom that have known
workarounds and are tracked for resolution in a future release. Each entry
states the symptom, the recommended workaround, and links to the design
note and backlog row that own the fix. Items here are *expected to be
addressed in future versions* — file an issue under
[`.local/feedbacks/`](../.local/feedbacks/) if you hit one and the
listed workaround does not unblock you.

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
