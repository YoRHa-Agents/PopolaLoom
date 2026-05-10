---
layout: default
title: API Stability Boundary
description: PopolaLoom v0.9.x stable and experimental API surface.
---

# PopolaLoom API Stability Boundary — v0.9.x

<!-- updated: 2026-05-10 -->

> **Status**: v0.9.0 GA published the first explicit stable /
> experimental boundary; v0.9.6 carries forward the v0.9.3
> workspace-worker singleton dispatch + private-worker routing extras,
> the v0.9.4 Actions validation hotfix, and the v0.9.5 init-time
> Cursor API key intake flags under the same additive v0.9.x contract,
> plus v0.9.6 adds the install-time default flip (`./install.sh`
> default `--from=pypi` → `--from=git`) and the new
> `./install.sh --ref=<tag|branch|sha>` flag (see
> [§2.7](#27-installsh-bash-bootstrap-installer-v096)).
> **Lock decision**: **Q-D-7 (Q9-3)** — *"daemon RPC + CLI 列稳定；实验项以
> `extra` / `__experimental` 标记"* — see row 10 of the *已锁定的全部 11 道决策*
> table in the program plan and the matching `Q9-3` row in
> [`decision-matrices-zh.md`](../.local/research/v0.8.5-to-v0.9.0_roadmap/decision-matrices-zh.md).
> **Last updated**: 2026-05-10

This document is the canonical contract for what an operator, integrator,
or downstream Skill MAY rely on across the v0.9.x line, and what is
explicitly *not* part of that contract. The classification governs how
aggressively each surface may change inside v0.9.x:

- **Stable** surfaces follow strict SemVer: a v0.9.x **patch** ships no
  user-observable changes; a v0.9.x **minor** may add new fields, flags,
  or endpoints but never remove or rename existing ones; **breaking**
  changes are deferred to v0.10.0 and require a 1-minor
  `DeprecationWarning` cycle first.
- **Experimental** surfaces ship with an `extra` / `__experimental`
  marking convention (see [§3 Experimental Surfaces](#3-experimental-surfaces-no-semver-guarantee)
  and [§8 Marking convention](#8-marking-convention-__experimental-extra))
  and may change in v0.9.x **minor** releases with a CHANGELOG note —
  patch releases still leave them alone.

If a surface is not enumerated below, it is implementation detail even
when importable (for example
`popolaloom.daemon.cloud_poller.CloudPollLoop._poll_run_body` MUST NOT
be imported by integrators — see [§7 Out-of-Scope](#7-out-of-scope)).

**Companion documents**:
[`docs/MIGRATION_v07_to_v09.md`](MIGRATION_v07_to_v09.md) ·
[`docs/USER_GUIDE.md`](USER_GUIDE.md) ·
[`docs/known-issues.md`](known-issues.md) ·
[`RELEASE_NOTES.md`](../RELEASE_NOTES.md) ·
[`CHANGELOG.md`](../CHANGELOG.md).

---

## 1. SemVer Contract

PopolaLoom adheres to [Semantic Versioning 2.0](https://semver.org/) and
the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
For v0.9.x specifically, the contract is:

| Term | Meaning | Allowed in patch (`0.9.x → 0.9.x+1`) | Allowed in minor (`0.9.x → 0.9.(x+1)`) | Requires major (`0.9.x → 0.10.0`) |
| --- | --- | --- | --- | --- |
| **Stable** | Surface is locked under [§2](#2-stable-surfaces-v09x-guaranteed); SemVer applies. | bug fixes, doc-only edits, additive **opt-in** behaviour | new flags / fields / endpoints / event types (additive only) | renames, removals, semantic recycling of an exit code, default flips that change observable behaviour |
| **Experimental** | Surface is marked `__experimental` / `extra` per [§3](#3-experimental-surfaces-no-semver-guarantee). | bug fixes only | rename / remove / re-shape allowed (CHANGELOG must mention it) | n/a — experimental surfaces never escalate to "major-only change" |
| **Deprecated** | Surface is stable today but slated for removal; see [§4](#4-deprecation-policy). | emits `DeprecationWarning` (Python) or stderr `[deprecated]` (CLI) | warning continues; removal NOT allowed inside the same minor | removal allowed in the next minor (so v0.9.x → v0.10.0). |

**Concrete examples**:

- *Stable additive change (minor)* — adding a `--include-events` flag to
  `popola cloud runs` is a v0.9.1 minor: existing scripts that don't
  pass the flag see no behaviour change.
- *Stable rename (major)* — renaming `popola list`'s `runtime` column to
  `kind` is a v0.10.0 change requiring a `DeprecationWarning` shipped
  in v0.9.x first.
- *Experimental re-shape (minor)* — changing the JSON shape of
  `verbose.cost_estimate_usd` is permitted in a v0.9.x minor with a
  CHANGELOG entry (the field is `null` in v0.8.8 by Q-C-2 lock; see
  [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) Cost transparency section).
- *Deprecated removal (next minor)* — the legacy `RelayHandoffEnvelope`
  v0.3.0 wire format (deprecated in v0.7.3) is removed in v0.9.0 per
  [`docs/MIGRATION_v07_to_v09.md`](MIGRATION_v07_to_v09.md) §"Breaking changes"
  and Q-D-3 lock.

**Cross-references**:
[`RELEASE_NOTES.md`](../RELEASE_NOTES.md) ·
[`CHANGELOG.md`](../CHANGELOG.md) ·
program plan §"已锁定的全部 11 道决策" row 10 (Q-D-7).

---

## 2. Stable Surfaces (v0.9.x guaranteed)

The four surfaces below are covered by SemVer for the entire v0.9.x line.
**Verb / endpoint / class / key names**, **flag spellings**, **default
values**, **JSON output keys**, **exit codes**, and **rendered table
column ordering** are stable.

### 2.1 CLI commands and flags

Every Typer-registered verb is defined in
[`src/popolaloom/cli/main.py`](../src/popolaloom/cli/main.py) (root
commands) plus the sub-apps wired in `_register_subcommand_groups`. The
v0.9.x stable CLI surface is:

| # | Verb | Stable contract (flag names, defaults, exit codes) | Landed |
| --- | --- | --- | --- |
| 1 | `popola dispatch <prompt>` | `--cli`, `--cwd`, `--cli-flag KEY=VAL`, `--events-dir`, `--replay`, `--wait`, `--timeout`, `--json`. Returns `task_id` on stdout. For `--cli=cursor-cloud`, stable private-worker routing extras are listed in [§2.6](#26-cursor-cloud-private-worker-routing-extras-v093). Exit codes: `0` success / `1` daemon-down or unknown CLI / `2` invalid args. | v0.2.0 |
| 2 | `popola list` | `--state`, `--all/-a`, `--no-runtime`, `--json`. Default rendered columns (in order): `task_id, runtime, cli, state, pid, started_at`. `--json` always carries `runtime`. | v0.2.0 (column added v0.8.6 — see [§6](#6-cross-links-to-v08x-release_notes)) |
| 3 | `popola status <task>` | `--json`, `--verbose`. Without `--verbose` the JSON shape is the v0.8.5 baseline (no `verbose` key). Cost block (`--verbose`) is **experimental** — see [§3.2](#32-cost-surface-fields-in-popola-status-verbose-q-c-2). | v0.2.0 (`--verbose` v0.8.8) |
| 4 | `popola attach <task>` | `--from <int>`, `--follow/--no-follow` (default `--follow`), `--no-stream`. Streams NDJSON event lines `<time>  <type>  <data>`. | v0.2.0 (`--no-stream` v0.8.6) |
| 5 | `popola cancel <task>` | `--json`. SIGTERM → SIGKILL after 5 s grace; cloud tasks call `POST /v1/agents/{id}/cancel`. Idempotent. | v0.2.0 |
| 6 | `popola probe` | `--json`. Lightweight daemon health (`daemon_pid, started_at, uptime_seconds, active_tasks, version`). | v0.2.0 |
| 7 | `popola init` | Sub-app: `cursor` / `claude` / `codex` / `copilot` / `interactive`. Flags `--scope global\|project`, `--target full\|cloud-only`, `--dry-run`. | v0.5.0 (`--target=cloud-only` v0.9.0 per Q-D-4) |
| 8 | `popola skill` | Sub-app: `install` / `upgrade` / `uninstall` / `doctor`. Same `--scope` / `--target` flags as `init`. | v0.5.0 |
| 9 | `popola doctor` | `--json`. Aggregates `popolad / lark / sqlite / skills`. Exit `0` PASS / `2` any FAIL. | v0.5.0 |
| 10 | `popola handoff` | Sub-app: `inspect` / `archive` / `list`. On-disk envelope tooling. | v0.7.2 |
| 11 | `popola cloud` | Sub-app namespace. Verb `runs` is **experimental** in v0.9.0 — see [§3.1](#31-popola-cloud-runs-q-c-1). The sub-app shell itself is stable. | v0.8.8 |
| 12 | `popola relay <task_a>` | `--dry-run`, `--no-confirm`, `--target-repo`, `--confirm-allowlist`, `--message`, `--idempotency-key`, `--json`. Default `mode = "auto"` per Q-C-4 lock. Behaviour gated by `[cloud.relay]` (see [§3.3](#33-cloudrelay-config-schema-q-c-4)). Exit codes: `0`, `1` policy-denied, `2` invalid-args, `75` cloud-API, `77` cloud-auth, `78` feature-unavailable, `100` not-found, `102` conflict. | v0.8.8 |
| 13 | `popola cloud worker` | Sub-app: `debug` / `start` / `status` / `handoff` / `dispatch`. My Machines mode uses upstream `agent login`; `--pool` requires a Cursor service-account API key (resolved per [§2.5](#25-cursor-api-key-credential-resolver-v092)) and exits `77` when missing. `start` reuses the workspace worker by default unless `--allow-duplicate` is passed. `handoff.popola_task_id` is always `null`; `dispatch` creates a normal popola-tracked cursor-cloud task routed to the workspace worker. | v0.9.1 (`dispatch` + singleton reuse v0.9.3) |
| 14 | `popola auth cursor` | Sub-app: `set` / `status` / `clear`. `set` accepts `--api-key VAL` / `--from-env` / `--validate` / `--json` (mutually-exclusive `--api-key` ⊕ `--from-env`). `status --json` envelope keys are stable: `configured` / `source` / `backend_name` / `fingerprint` / `keyring_available`. `clear` accepts `--yes` / `--json` and is idempotent. The literal API key value is **never** echoed, logged, or returned in any envelope (No Silent Failures). Exit codes: `0` ok / `2` invalid args / `3` keyring backend unavailable / `77` `--validate` round-trip rejected by Cursor. | v0.9.2 |

**Stable-scope rules**:

1. *Verb name* (`popola dispatch`) and *flag spelling* (`--cli`,
   `--cli-flag`) are stable. Adding a new flag with a default value is
   non-breaking; renaming an existing flag is breaking.
2. *Exit codes* `0`, `1`, `2`, `4`, `75`, `77`, `78`, `100`, `102` are
   reserved across the CLI for the meanings documented above. Adding a
   new exit code in a minor is non-breaking; recycling an existing code
   for a different meaning is breaking.
3. *Rendered table column names* and *ordering* are stable; rendered
   widths, truncation glyphs, and color schemes are NOT.
4. *`--json` schemas* — consumers MUST tolerate unknown keys. Adding new
   keys in a minor is non-breaking; removing or renaming an existing
   key is breaking.

**Concrete example** (CLI flag stability):

```bash
# Stable since v0.8.6 — `--no-runtime` flag name + behaviour are locked.
popola list --no-runtime --json | jq '.[].task_id'

# Stable across v0.9.x — exit code 100 means "task / agent not found"
# for `dispatch --cli=cursor-cloud`; exit 4 means same thing for
# `popola cloud runs` (cross-verb difference is documented).
popola dispatch "fix bug" --cli=cursor-cloud
echo "exit=$?"  # 0 happy / 100 not-found / 77 auth-failed
```

### 2.2 Daemon RPC endpoints

Every endpoint registered on the popolad FastAPI app
([`src/popolaloom/daemon/rpc.py`](../src/popolaloom/daemon/rpc.py)) is
listed below. **Method + path + request body shape + response body
shape** are stable. The transport (Unix Domain Socket at
`$POPOLA_HOME/popolad.sock`, default `~/.popola/popolad.sock`) is itself
stable.

| Method | Path | Request | Response | Notes |
| --- | --- | --- | --- | --- |
| `POST` | `/dispatch` | `DispatchRequest` (`cli`, `prompt`, `cwd?`, `extra?`) | `DispatchResponse` (`task_id`, `events_log`, `cli`) | All keys stable. |
| `GET` | `/list` | `?include_terminal=<bool>` | List of `_task_summary` dicts | Each summary stable on `task_id`, `cli`, `runtime`, `state`, `pid`, `started_at`. |
| `GET` | `/status/{task_id}` | `?verbose=<bool>` | Status dict (+ optional `verbose` block) | Verbose block fields **experimental** — see [§3.2](#32-cost-surface-fields-in-popola-status-verbose-q-c-2). |
| `POST` | `/cancel/{task_id}` | — | `CancelResponse` (`task_id`, `requested_signal`, `escalated_to_sigkill`, `pid`, `result?`) | Stable. |
| `GET` | `/attach_stream/{task_id}` | `?since=<int>` | SSE stream of NDJSON CloudEvents envelopes | Envelope shape stable; specific `cloud.sse.*` event sub-types are **experimental** — see [§3.4](#34-sse-event-sub-types-cloudsse). |
| `POST` | `/hitl/answer` | `HitlAnswerRequest` | `HitlAnswerResponse` (`ok`, `hitl_id`, `already_status?`, `already_via?`) | Local first-responder-wins HITL; stable. |
| `GET` | `/hitl/pending` | `?task_id=<str>` | List of pending HITL rows | Stable. |
| `POST` | `/hitl/cloud/request` | `CloudHITLRequestBody` | `CloudHITLRequestResponse` (`hitl_id`, `status`, `deadline_at`, `cursor_agent_id?`, `cursor_run_id?`, `deduped`, `lark_dispatched`) | Cloud HITL bridge; stable since v0.8.5 (mis-route + dedup added v0.8.7). |
| `GET` | `/hitl/cloud/wait/{hitl_id}` | `?timeout_s=<float>` | `CloudHITLWaitResponse` (`hitl_id`, `status` ∈ `{pending,answered,timeout}`, `answer?`) | Long-poll cap 60 s; default 55 s; stable. |
| `POST` | `/hitl/cloud/answer/{hitl_id}` | `CloudHITLAnswerBody` | `CloudHITLAnswerResponse` | Mis-route defense (HTTP 400) is part of contract per `mcp-tool-contract.md` §6.3. |
| `POST` | `/relay/dispatch` | `RelayDispatchRequest` (`source_task_id`) | `RelayDispatchResponse` (envelope info for CLI) | Read-side helper for `popola relay`; stable from v0.8.8. |
| `POST` | `/supervise` | `SuperviseRequest` | `SuperviseResponse` | Subscribe parent → child terminal callback; stable. |
| `POST` | `/federate` | `FederateRequest` | `FederateRpcResponse` | Multi-CLI fan-out + voting; stable. |
| `GET` | `/probe` | — | `ProbeResponse` | Stable. |
| `GET` | `/health` | — | `HealthResponse` (`status: "ok"`) | Liveness probe; stable. |

> **Note** — the legacy `POST /relay` endpoint with the v0.3.0
> `RelayHandoffEnvelope` body is **removed in v0.9.0** per Q-D-3 (see
> [§4 Deprecation Policy](#4-deprecation-policy) and
> [`docs/MIGRATION_v07_to_v09.md`](MIGRATION_v07_to_v09.md) §"Breaking
> changes"). The new wire is `POST /relay/dispatch` listed above.

**Stable-scope rules**:

1. *Path additions* (e.g. a future `GET /cloud/runs/{agent_id}`
   daemon-side cache) are non-breaking; renaming a path is breaking.
2. *Response-body additive changes* are non-breaking; consumers MUST
   ignore unknown keys.
3. The NDJSON envelope shape `{specversion, id, source, type, time,
   data}` is stable; specific values of `type` are stable when listed
   in [`docs/USER_GUIDE.md`](USER_GUIDE.md) Cloud chapter, except for
   the `cloud.sse.*` namespace whose **sub-types** are experimental
   ([§3.4](#34-sse-event-sub-types-cloudsse)).

**Concrete example** (RPC stability):

```bash
# Stable since v0.5.0 — UDS path + path + verbose query are locked.
curl --unix-socket ~/.popola/popolad.sock \
  http://popolad/status/cursor-fix-bug-3a7f9c1d?verbose=true
```

### 2.3 Public Python API

The Python surface importable through `popolaloom.*` includes the items
listed below. Public means the **import path + class / attribute name +
public method signatures** are stable; private methods (leading `_`)
are not — see [§7 Out-of-Scope](#7-out-of-scope).

| Symbol | Import path | Stable scope |
| --- | --- | --- |
| `__version__` | [`popolaloom`](../src/popolaloom/__init__.py) | A SemVer-formatted string. Reading and comparing is stable; the value itself bumps per release. |
| `Popolad` | [`popolaloom.daemon.server`](../src/popolaloom/daemon/server.py) | Daemon orchestrator class. Public methods: `dispatch_task`, `cancel_task`, `get_status`, `list_active`, `list_all`, `event_log`, `rehydrate_from_persistence`, `shutdown_persistence_bridge`. |
| `HITLStore` | [`popolaloom.hitl.sync`](../src/popolaloom/hitl/sync.py) | Cross-channel HITL persistence. Public methods: `submit_request`, `mark_answered`, `get`, `list_pending`. |
| `EventLog` | [`popolaloom.daemon.event_log`](../src/popolaloom/daemon/event_log.py) | Append-only NDJSON writer. Public methods: `append`, `tail`, `path`. The 0o600 file-mode invariant is part of the contract. |
| `CloudHITLBridge` | [`popolaloom.hitl.cloud_bridge`](../src/popolaloom/hitl/cloud_bridge.py) | Cloud HITL request / answer / await flow. Public methods: `submit_request`, `submit_answer`, `await_answer`, plus the module-level `bridge_for_daemon` factory. |

**Stable-scope rules**:

1. The classes above can be imported and constructed by integrators.
   *Method signatures* of the listed public methods are stable; adding
   a new keyword-only argument with a default is non-breaking.
2. Attribute names exposed on instances (e.g. `Popolad.events_dir`,
   `Popolad.state_store`, `Popolad.hitl_store`) are stable.
3. Private helpers (anything beginning with `_`) and submodules under
   `popolaloom._vendored.*` are NOT public — see [§3.5](#35-internal-modules-_-prefixed-symbols).

**Concrete example** (Python API stability):

```python
import popolaloom
from popolaloom.daemon.event_log import EventLog

assert popolaloom.__version__.startswith("0.9.")

log = EventLog.open("/tmp/events/abc123.jsonl")
log.append("test.marker", {"note": "stable since v0.2.0"})
```

### 2.4 Skill front-matter contract

The PopolaLoom Skill ships at
[`src/popolaloom/skills/popola-loom/SKILL.md`](../src/popolaloom/skills/popola-loom/SKILL.md).
Its YAML front matter is consumed by Cursor / Claude / Codex / Copilot
discoverers and by `popola skill doctor`. Three keys are stable:

| Key | Type | Stable contract |
| --- | --- | --- |
| `name` | string | Always literal `popola-loom` (was renamed from `popolaloom` in v0.7.1). Renaming requires a new minor and CHANGELOG entry. |
| `version` | SemVer string | Tracks `popolaloom.__version__` (set per release). Consumers may parse with any standard SemVer parser. |
| `description` | string | Free-form summary block; the *opening sentence* up to the first period is treated as stable wording for skill-discovery surfaces (the body of the skill is **not** part of the contract — see [§7 Out-of-Scope](#7-out-of-scope) item 2). |

**Concrete example** (Skill front-matter contract):

```yaml
---
name: popola-loom
version: 0.9.3
description: "PopolaLoom — 跨 CLI 元编排器。…"
---
```

> Other keys (`metadata.surfaces`, `metadata.requires`, `tier`,
> `token_estimate`, `last_updated`) are **best-effort**; they may evolve
> as Cursor's Skill-discovery format evolves.

### 2.5 Cursor API key credential resolver (v0.9.2+)

Every cloud dispatch / runs / relay / cancel / attach call site routes
through [`popolaloom.credentials.resolve_cursor_api_key`](../src/popolaloom/credentials.py)
instead of reading `os.environ["CURSOR_API_KEY"]` directly. The
precedence chain is part of the v0.9.x stable surface:

| # | Slot | Source | Notes |
| - | ---- | ------ | ----- |
| 1 | Explicit override | `resolve_cursor_api_key(override=...)` | Test-only / library-injection hook (`CredentialResolver(override=...)`). Production CLI does NOT expose this — operators use slot 2 or 3. |
| 2 | Environment variable | `CURSOR_API_KEY` | Highest-precedence operator-facing slot. Whitespace-only values are ignored (treated as unset; No Silent Failures). |
| 3 | OS keyring | `popolaloom.cursor` / username `default` | Populated by `popola auth cursor set` (or the v0.9.2+ `init --target=cloud-only --configure-cursor-auth` prompt). Backend is the active OS keychain (macOS Keychain, Windows Credential Manager, libsecret on Linux, KWallet, etc.); requires `./install.sh install --with-credentials` on a fresh install or `./install.sh update --with-credentials` on an existing install. |
| 4 | Missing | n/a | Returns `None`; the CLI surfaces a remediation message naming all three slots. |

**Stable contract (v0.9.x)**:

- The resolver is the **only** supported path for reading the API key
  in PopolaLoom code; new call sites MUST go through it (CI lint
  pending in v0.9.x). Direct `os.environ.get("CURSOR_API_KEY")` reads
  in v0.9.x patches are deprecated for new code.
- The keyring service identifier `popolaloom.cursor` and username slot
  `default` are stable; changing either would orphan operator-stored
  secrets.
- `popolaloom.credentials.REDACTION_PLACEHOLDER` (`<REDACTED:CURSOR_API_KEY>`)
  is the canonical placeholder — third parties grep for it.
- `CredentialStatus.to_json_dict()` keys (`configured`, `source`,
  `backend_name`, `fingerprint`, `keyring_available`) are part of the
  `popola auth cursor status --json` stable schema.
- Fingerprint format: first **12 hex chars** of SHA-256 of the
  stripped secret. Stable across the v0.9.x line so operators can
  compare values across `popola auth cursor status` invocations.
- **v0.9.5 init-time intake flags** — `popola init --cursor-api-key VAL`
  and `popola init --cursor-api-key-file PATH` are stable additions to
  the resolver-set side of the surface: both forward their resolved
  value to `store_cursor_api_key` (slot #3 above) without prompting.
  Either flag implies `--configure-cursor-auth`; both flags are
  accepted on every init path (auto-detect, verb subcommand,
  `--target=cloud-only`, `--interactive`). Mutex of the two flags +
  empty/missing-file rejection are part of the stable surface
  (`tests/cli/test_init_credential_intake.py` pins the contract).
  `--dry-run` short-circuits credential persistence with a clear
  one-line skip message — secrets are never persisted during a
  preview (No Silent Failures).

**Out-of-scope (v0.9.x)**: alternative backends (e.g. HashiCorp Vault,
AWS Secrets Manager, GCP Secret Manager) are not exposed in v0.9.x and
remain implementation detail of the resolver if introduced. The
exception escape hatch is the `override=` kwarg, which is the
public-API-but-not-CLI-exposed test seam.

**Concrete example** (status JSON envelope shape — pinned by
[`tests/cli/test_auth_cmd.py`](../tests/cli/test_auth_cmd.py)):

```jsonc
{
  "configured": true,
  "source": "keyring",                  // env / keyring / override / none
  "backend_name": "macOS Keychain",     // best-effort label
  "fingerprint": "9c1f3a4b2e8d",        // 12 hex chars of sha256(value)
  "keyring_available": true
}
```

### 2.6.1 `install.sh` bash bootstrap installer (v0.9.6+)

> Note: this subsection is anchored as `2.7` in the navigation
> bullet at the top of this document because it lands after the
> v0.9.3 Cursor Cloud routing surface (§2.6). Renumber if §2 ever
> grows another subsection between §2.6 and the deprecation policy.

The repo-root `install.sh` bash bootstrap (script version
`POPOLA_INSTALL_SCRIPT_VERSION="0.9.6"`) is part of the v0.9.x stable
surface starting in v0.9.6. Its contract:

- The verb names `install` / `update` / `uninstall` / `version` /
  `help` are stable; renames are breaking.
- The flag spellings `--scope=<global|project>`,
  `--target=<cursor|claude|codex|copilot|all>`,
  `--from=<git|pypi|PATH>`, `--version=<X.Y.Z>`,
  `--ref=<tag|branch|sha>` (NEW v0.9.6),
  `--python=<bin>`, `--no-skills`, `--no-daemon`, `--purge`,
  `--yes` / `-y`, `--dry-run`, `--quiet` / `-q`, `--help` / `-h` are
  stable; renames or removals are breaking.
- The `--from` **default value is `git`** as of v0.9.6 (closes
  [`./.local/feedbacks/feedback_for_v0.9.4.md`](../.local/feedbacks/feedback_for_v0.9.4.md)
  lines 2-5; flipped from `pypi` because PyPI publish remains
  deferred for the v0.9.x line per Q-D-5 偏离默认 /
  `BL-v0.9.x-PyPI`). Flipping it back to `pypi` would re-introduce
  the 404 surface on Chinese pip mirrors and is therefore breaking.
- The `--ref=<value>` flag requires `--from=git`; it joins the
  v0.9.x stable surface in v0.9.6. Mirror of `--version=X.Y.Z`
  (which still requires `--from=pypi`); contradictory inputs fail
  loudly (No Silent Failures).
- The `--ref` flag is forbidden for the `uninstall` verb (mirrors
  the `--version` semantics for that verb).
- Exit codes: `0` for success, non-zero for any validation or
  command failure (the script aborts at the first non-best-effort
  step per "No Silent Failures"). Specific exit codes are
  implementation detail.
- `./install.sh version` prints `install.sh v<POPOLA_INSTALL_SCRIPT_VERSION>`
  on stdout; the `v<X.Y.Z>` substring is stable.
- `./install.sh --help` prints the full flag matrix on stdout. The
  `--ref` flag MUST appear in the rendered usage matrix
  (`tests/cli/test_install_script.py::test_install_script_help_returns_zero`
  pins this).

**Out-of-scope (v0.9.x)**: rendered widths, ANSI colors, and exact
prose wording of log lines are NOT part of the contract. Adding new
flags in a v0.9.x minor is non-breaking; renaming or removing any
listed flag in a v0.9.x patch or minor is breaking.

```bash
# Stable v0.9.6 surface — these three forms are the canonical install
# recipes for the v0.9.x line until BL-v0.9.x-PyPI lands.
./install.sh install                                              # default --from=git, tracks main
./install.sh install --ref=v0.9.6                                 # canonical tag-pinned
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6 # manual fallback
```

### 2.6 Cursor Cloud private-worker routing extras (v0.9.3+)

For `popola dispatch --cli=cursor-cloud`, the following `--cli-flag`
extras are stable across the v0.9.x line:

| Extra | Type | Stable contract |
| ----- | ---- | --------------- |
| `use_private_worker` | bool | Requests Cursor REST `usePrivateWorker=true`. |
| `labels` | `dict[str,str]` | Worker routing labels passed to Cursor Cloud Agents. |
| `worker_name` | string | Convenience key merged into `labels.worker`; automatically enables `use_private_worker`. |
| `machine_name` | string | Convenience key merged into `labels.machine`; automatically enables `use_private_worker`. |
| `pool_name` | string | Convenience key merged into `labels.pool`; automatically enables `use_private_worker`. |

Contradictory input (`use_private_worker=false` with labels or any
convenience key) fails loudly. `popola cloud worker dispatch` is the
workspace-targeted wrapper around this same stable cursor-cloud routing
surface; it posts through `popolad` and returns a normal popola task id.

---

## 3. Experimental Surfaces (no SemVer guarantee)

These surfaces are installed and usable but ship with the
`extra` / `__experimental` marking convention — they may change in a
v0.9.x minor with a CHANGELOG note. None of them are on the v0.9.x
SemVer-stable list above.

### 3.1 `popola cloud runs` (Q-C-1)

**Why experimental**: The verb shipped late in the v0.8.8 cycle as a
deviation from default — Q-C-1's locked default was *defer to v0.9*; the
user opted to ship in v0.8.8 as a deliberate偏离默认 (see
[CHANGELOG.md §0.8.8](../CHANGELOG.md) — `popola cloud runs` —
list cloud-agent run history (Q-C-1 偏离默认)). One additional minor
of usage is required before its column / pagination contracts are
locked. The wrapping sub-app `popola cloud` itself is stable
([§2.1](#21-cli-commands-and-flags) row 11); only the `runs` verb is
experimental.

**What may change** (in a v0.9.x minor with CHANGELOG note):

- The default 6-column rendered table layout (`run_id, run_index,
  state, created_at, wall_clock, model`) — column ordering may shift,
  e.g. if a `cost` column is added once Cursor publishes per-run cost.
- The `--include-events` slow-path JSON shape (it currently emits a
  per-row `events_summary` object built from `GET /runs/{run_id}`).
- The cross-verb exit-code split (`popola cloud runs` 404 → exit `4`
  vs `popola dispatch --cli=cursor-cloud` 404 → exit `100`); the spec
  may re-converge on a single code in v0.10.0.

**What is stable**: the existence of the `popola cloud` sub-app, the
verb name `runs`, and the high-level intent (read-only list of cloud
runs). Concrete example:

```bash
# Help text shows [experimental] tag.
popola cloud runs --help

# Rendered output / column layout MAY shift in v0.9.x minor.
popola cloud runs cursor-fix-bug-3a7f9c1d --limit 50 --json
```

### 3.2 Cost surface fields in `popola status --verbose` (Q-C-2)

**Why experimental**: Per Q-C-2 the v0.8.8 surface ships an *honest*
`cost: n/a` literal because the Cursor Cloud Agents v1 API does not
publish per-run cost on the public REST/SSE wire (see [v0.8.8
RELEASE_NOTES](../RELEASE_NOTES.md) — Cost transparency). The shape of
the verbose block (10 keys: `cost_estimate_usd`, `model_id`,
`model_mode`, `tokens_input`, `tokens_output`, `tokens_total`,
`wall_clock_s`, `agent_status`, `agent_url`, `doc_anchor`) will evolve
with the Cursor API: when authoritative cost / token fields land, the
schema will change.

**What may change**: any of the 10 keys' types or names; the one-line
text format (`cost: n/a  model: <id|->  [mode: max]  wall: NN.Ns
link: <url>`); the `doc_anchor` URL.

**What is stable**: the existence of `--verbose` on `popola status`,
the `--json --verbose` envelope key (`response["verbose"]` is a dict,
not `null`), and the `cost: n/a` literal **as long as Cursor's public
API has no per-run cost source** — see [`docs/known-issues.md`](known-issues.md)
for the honest-disclosure rationale.

```bash
popola status cursor-fix-bug-3a7f9c1d --verbose --json | jq .verbose
# → schema MAY add new keys in v0.9.x minor.
```

### 3.3 `[cloud.relay]` config schema (Q-C-4)

**Why experimental**: Per Q-C-4 the v0.8.8 default was flipped from the
roadmap default ("require human confirm") to `mode = "auto"` (see
[CHANGELOG.md §0.8.8](../CHANGELOG.md) — Behavior change callout).
The defaults — `repo_allowlist = []`, `prompt_size_cap_bytes = 16384`,
`idempotency_window_s = 3600` — may *tighten* in v0.9.x patches if real
operator data shows they're too permissive. The three locked-true
boolean flags (`require_confirm_allowlist_flag`, `secret_scan_enabled`,
`dry_run_emits_audit`) ARE stable (the loader rejects them being set to
`false`).

**What may change**: default values for any non-locked key; addition of
new keys (e.g. `audit_retention_days` once GC is implemented per
[`docs/known-issues.md`](known-issues.md) BL-v0.8.9-2).

**What is stable**: the section name `[cloud.relay]`, the existing key
names (renaming `repo_allowlist` to `allowed_repos` is breaking), the
three loader-locked booleans listed above, and the audit-row schema's
14 mandatory keys (per `relay-auto-safety.md` §M2).

```toml
# popolad.toml — section name + key spellings stable; defaults experimental.
[cloud.relay]
mode = "auto"          # default may flip in v0.9.x minor; rollback via "confirm"
repo_allowlist = []    # default-empty BLOCKS all relays — stable invariant
```

### 3.4 SSE event sub-types (`cloud.sse.*`)

**Why experimental**: The `cloud.sse.*` namespace was introduced in
v0.8.6 with a 1-cycle coexistence period alongside the legacy
`cloud.*` prefix (Q-A-3 lock; see [§6](#6-cross-links-to-v08x-release_notes)).
Specific sub-types (`cloud.sse.assistant_chunk`,
`cloud.sse.tool_call`, `cloud.sse.tool_result`,
`cloud.sse.parse_error`, `cloud.sse.dedup_drop`,
`cloud.sse.stream_expired`, `cloud.sse.fallback_to_poll`) follow Cursor's
upstream SSE schema and may add / rename fields as Cursor evolves.

**What may change**: payload shape inside `data` for any
`cloud.sse.*` event; the cardinality of sub-types (a new
`cloud.sse.*` event may be added in any v0.9.x minor).

**What is stable**: the namespace prefix `cloud.sse.` itself, the
NDJSON envelope shape (`{specversion, id, source, type, time, data}`),
the `cloud.run_started` / `cloud.run_finished` / `cloud.queue_*` /
`cloud.busy_*` brackets emitted by popolad code (NOT synthesised from
SSE — see [CHANGELOG.md §0.8.8](../CHANGELOG.md) Multi-run section),
and the dedup quintuple-now-sextuple identity
`(task_id, run_id, run_index, stream_session_id, sse_id, seq)`.

```jsonc
// Envelope shape stable; `data` payload of `cloud.sse.*` MAY change.
{"specversion":"1.0","type":"cloud.sse.tool_call","time":"…","data":{...}}
```

### 3.5 Internal modules (`_*`-prefixed symbols)

**Why experimental**: Anything whose name (module, class, function,
attribute, file) begins with `_` is implementation detail and not
covered by SemVer. This includes:

- Modules under `popolaloom._vendored.*` (vendored ArkTower per
  `VENDORING.md`).
- Private classes / functions (e.g. `_CloudSSEEventSink`,
  `_build_verbose_block`, `_parse_cli_flags` in `cli/main.py` and
  `daemon/rpc.py`).
- File-private constants (e.g. `_DAEMON_STATE`, `_ATTACH_QUEUE_MAXSIZE`,
  `_VALID_HITL_REPLY_CHANNELS`).
- Test-only helpers (anything under `tests/`).

**What may change**: any of the above can be renamed, moved, or
removed without notice. Integrators relying on them are liable to
break in a patch release.

**What is stable**: nothing — by definition `_*`-prefixed symbols are
private API.

```python
# DO NOT depend on this — it MAY disappear in any patch.
from popolaloom.cli.main import _parse_cli_flags  # 🔴 unstable
```

---

## 4. Deprecation Policy

PopolaLoom follows the **1-minor warning + remove in next minor** rule:

1. A surface scheduled for removal emits `DeprecationWarning`
   (Python) or a stderr `[deprecated]` notice (CLI / RPC) for **at
   least one minor cycle** (e.g. v0.9.0 marks → v0.10.0 removes).
2. Patch releases (e.g. v0.9.0 → v0.9.1) MUST NOT remove a deprecated
   surface — only minors do.
3. Per [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), every
   removal lands under `### Removed` in
   [`CHANGELOG.md`](../CHANGELOG.md) with a backlink to the minor that
   first emitted the warning.
4. Cross-link required: the same row MUST appear in
   [`docs/MIGRATION_v07_to_v09.md`](MIGRATION_v07_to_v09.md)
   §"Breaking changes" so operators upgrading any number of minors at
   once still see it.

### v0.8.x → v0.9.0 deprecations being removed (per W2.2)

The v0.9.0 GA release closes the `v0.8.x deprecation` shim sweep
landed in Stage 2 Wave 2.2 of the program plan
(`v085-v090-iteration-plan` §"Wave 2.2 — deprecation 清理" — research
artifact, kept under `.local/` per the project convention). The
following items are removed in v0.9.0:

| # | Removed surface | First deprecated | Reference |
| --- | --- | --- | --- |
| 1 | `popolaloom.daemon.primitives.RelayHandoffEnvelope` (Pydantic v0.3.0 wire format) | v0.7.3 | Q-D-3 lock; closes BL-v0.9.0-1 |
| 2 | `POST /relay` endpoint with the v0.3.0 envelope body | v0.7.3 | superseded by `POST /relay/dispatch` ([§2.2](#22-daemon-rpc-endpoints)) |
| 3 | `popolaloom.handoff.to_handoff_envelope` migration helper | v0.7.3 | superseded by `HandoffEnvelope` direct construction |
| 4 | Legacy `cloud.run_status` event sub-type (1-cycle coexistence with `cloud.sse.*`) | v0.8.6 (Q-A-3 lock) | promoted to single namespace `cloud.sse.*` |
| 5 | Static `_ERROR_CATALOG["rate_limit"]["backoff"]` data | v0.8.8 | superseded by `[cloud.backoff]` config (see [v0.8.8 CHANGELOG ###Changed](../CHANGELOG.md)) |
| 6 | Any other `# v0.8.x TEMP` / `# DeprecationWarning` shim caught by the sweep grep `grep -rn "DeprecationWarning\|deprecated\|v0\.8\.x TEMP"` (T2.2.1 AC) | v0.8.x patches | release-gate AC: **0 residuals** |

**Concrete example**:

```python
# Will raise ImportError on v0.9.0:
from popolaloom.daemon.primitives import RelayHandoffEnvelope  # 🔴 removed
# Replacement (stable since v0.7.3):
from popolaloom.handoff import HandoffEnvelope
```

```bash
# Will return 404 on v0.9.0:
curl --unix-socket ~/.popola/popolad.sock -X POST http://popolad/relay \
  -d '{"source_task_id": "...", "target_cli": "..."}'  # 🔴 removed
# Replacement:
curl --unix-socket ~/.popola/popolad.sock -X POST http://popolad/relay/dispatch \
  -d '{"source_task_id": "..."}'
```

For the full operator-side migration checklist see
[`docs/MIGRATION_v07_to_v09.md`](MIGRATION_v07_to_v09.md) §"Breaking
changes" and §"Action checklist".

---

## 5. Compatibility Promises by Surface

The matrix below aggregates §1–§4 into a single quick-reference table.
Each row pairs a surface kind with its SemVer guarantee, the change
types allowed within that guarantee, and a concrete example.

| Surface kind | SemVer guarantee | Allowed change types (within v0.9.x) | Concrete example |
| --- | --- | --- | --- |
| CLI verb / flag *name* | **stable** ([§2.1](#21-cli-commands-and-flags)) | additive only | adding `popola cloud agents list` is OK; renaming `--no-runtime` to `--hide-runtime` is breaking. |
| CLI flag *default value* | **stable** | flipping a default that changes observable behaviour requires a major (e.g. `[cloud.relay] mode = "auto"` → `"confirm"` is breaking); adding a new opt-in default mode is additive. | flipping `--follow` from default-on to default-off would require v0.10.0. |
| CLI exit code | **stable** | adding new exit codes is additive in a minor; recycling an existing code for a new meaning is breaking. | introducing exit `5` for "task throttled" is OK; changing `100` from "not found" to "rate limited" is breaking. |
| `--json` schema | **stable** (additive) | adding new keys is non-breaking; consumers MUST tolerate unknown keys. | adding `cost_estimate_usd` to a `--verbose` block in a future minor is OK once the field has an authoritative source. |
| Daemon RPC path / method | **stable** ([§2.2](#22-daemon-rpc-endpoints)) | new paths additive; renames breaking. | adding `GET /cloud/runs/{agent_id}` is OK; renaming `POST /dispatch` to `POST /tasks` is breaking. |
| Daemon RPC body keys | **stable** | additive only; consumers MUST tolerate unknown keys. | adding `RelayDispatchResponse.repo_url` was non-breaking in v0.8.8. |
| NDJSON envelope shape | **stable** | the 6 envelope keys (`specversion`, `id`, `source`, `type`, `time`, `data`) are stable; specific `type` values are stable except `cloud.sse.*` sub-types ([§3.4](#34-sse-event-sub-types-cloudsse)). | a new `cloud.run_paused` event in a minor is OK; renaming `cloud.run_started` to `cloud.run_began` is breaking. |
| Python class / public method | **stable** ([§2.3](#23-public-python-api)) | new keyword-only argument with a default is non-breaking. | adding `EventLog.append(..., *, source: str | None = None)` is OK. |
| Python `_*`-prefixed symbol | **experimental** ([§3.5](#35-internal-modules-_-prefixed-symbols)) | rename / remove allowed any time. | `_CloudSSEEventSink` may move modules in v0.9.x. |
| `popolad.toml` section name | **stable** | adding new sections is non-breaking. | introducing `[cloud.observability]` is OK; renaming `[cloud.backoff]` to `[backoff]` is breaking. |
| `popolad.toml` key inside an existing section | **stable** | additive only. | adding `[cloud.backoff] honor_x_ratelimit_remaining = true` is OK; renaming `max_retries` is breaking. |
| `popolad.toml` default value | **stable for behaviour-changing**; **experimental for relay** ([§3.3](#33-cloudrelay-config-schema-q-c-4)) | a default flip that changes behaviour requires major; the `[cloud.relay]` numeric defaults may tighten in patch with a CHANGELOG note. | tightening `prompt_size_cap_bytes = 16384 → 8192` is allowed in v0.9.x patch with a CHANGELOG note (per Q-C-4 mitigation). |
| Skill front-matter `name` / `version` / `description` | **stable** ([§2.4](#24-skill-front-matter-contract)) | additive keys (e.g. new `metadata.*`) non-breaking. | bumping `version` per release; renaming `name: popola-loom` → `name: popolaloom` would be breaking. |
| MCP `tools/list` verb name | **stable** | additive (new verb in minor); rename / remove breaking. | shipping `popola_cloud_runs` MCP verb in v0.9.x minor is OK. |
| `pyproject.toml` `[project.optional-dependencies]` extras | **stable** *name* + new extras experimental ([§3.5](#35-internal-modules-_-prefixed-symbols)) | adding a new `extra` is non-breaking; the dependency pin inside an extra may bump in a patch. | adding `popolaloom[cloud-only]` extra in a minor is OK. |

---

## 6. Cross-links to v0.8.x RELEASE_NOTES

Each stable / experimental surface above landed in a specific v0.8.x
release. The current single-file [`RELEASE_NOTES.md`](../RELEASE_NOTES.md)
holds only the latest release per the v0.7.0+ overwrite policy; the
full historical archive lives in [`CHANGELOG.md`](../CHANGELOG.md).

| Surface | Landed in | RELEASE_NOTES / CHANGELOG anchor |
| --- | --- | --- |
| `--cli=cursor-cloud` adapter, cloud HITL bridge (`POST /hitl/cloud/*`) | v0.8.5 | [`CHANGELOG.md` §[0.8.5]](../CHANGELOG.md) — *Cursor Cloud Agent integration shipped as sibling adapter `--cli=cursor-cloud`*. |
| SSE ingest, `runtime` column in `popola list`, `cloud.sse.*` event namespace, `--no-stream` escape hatch, 16-entry bilingual error catalog, manual cloud-smoke CI lane | v0.8.6 | [`CHANGELOG.md` §[0.8.6]](../CHANGELOG.md) — *Cloud observability + SSE ingest*. |
| `popolaloom_cloud_hitl_request` MCP tool, `cloud_hitl_request_card_v1` Lark card, `[hitl.cloud]` config, idempotency dedup, mis-route defense at the answer boundary | v0.8.7 | [`CHANGELOG.md` §[0.8.7]](../CHANGELOG.md) — *Cloud HITL production*. |
| Multi-run support (`POST /v1/agents/{id}/runs`), `cloud.run_started/finished` brackets, `popola status --verbose` cost surface, `[cloud.backoff]` + `[cloud.busy_strategy]` configs, `[cloud.relay]` + `popola relay`, `popola cloud runs` subcommand | v0.8.8 | [`CHANGELOG.md` §[0.8.8]](../CHANGELOG.md) — *Multi-run + Cost + Quota + Auto Relay*. |
| `popola cloud worker {debug,start,status,handoff}` self-hosted worker handoff | v0.9.1 | [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) (current release) and [`CHANGELOG.md` §[0.9.1]](../CHANGELOG.md) — *Self-hosted worker handoff*. |

---

## 7. Out-of-Scope

The following are explicitly NOT covered by this stability contract.
Operators relying on them do so at their own risk.

1. **Line-by-line internal Python helpers** — anything reachable only
   via private import (e.g.
   `popolaloom.daemon.cloud_poller.CloudPollLoop._poll_run_body`,
   `popolaloom.cli.main._build_status_busy_line`,
   `popolaloom.daemon.rpc._build_verbose_block`) is implementation
   detail. Renaming, splitting, or deleting them in a patch is
   permitted.
2. **The body of the `popola-loom` Skill** — only the three
   front-matter keys (`name`, `version`, `description`) are stable per
   [§2.4](#24-skill-front-matter-contract). Workflow numbers, the
   precise wording of walkthrough sections, the count or ordering of
   workflows, the token-budget caps in the canonical-test fixture
   (`tests/cli/test_skill_md_canonical.py`) — none of these are part
   of the API.
3. **Specific Cursor Cloud Agents API field names** — when PopolaLoom
   adapts an upstream payload, the payload's *content* travels through
   our envelope unchanged; if Cursor renames a field (e.g. `agent.id`
   → `agent.uuid`), our event `data` payload reflects that change in
   a *patch* release. Operators relying on Cursor field names MUST
   pin their integrations against Cursor's own SemVer
   ([`https://cursor.com/docs/cloud-agent/api/endpoints.md`](https://cursor.com/docs/cloud-agent/api/endpoints.md)),
   not PopolaLoom's.
4. **Sub-second timing characteristics** — SSE first-byte latency,
   `attach --follow` poll interval, daemon startup time, drainer
   cadence (`queue_poll_interval_s` default 5 s) — these are
   tunable / observable but not contractual; future minors may shift
   them as Cursor's upstream behaviour changes.
5. **Vendored ArkTower** (`popolaloom._vendored.arktower.*`) — the
   vendored subset is upstream code with its own SemVer; PopolaLoom
   refresh procedures are documented in `VENDORING.md` and refreshes
   may bump versions in patch releases.
6. **The `.local/`, `.local/.agent/`, `.local/research/`, and
   `.local/feedbacks/` directories** — gitignored research, plans,
   decisions, evidence, and tracker. Their existence, schema, and
   content evolve with each iteration and are NOT part of the public
   API.
7. **Test-only fixtures** — anything under `tests/` (including
   `tests/real_cursor_cloud/fixtures/`, `tests/cli/fixtures/`) is
   internal verification scaffolding. Their freezes (per Q-D-2 fixtures
   strategy) are CI quality gates, not user-facing schemas.
8. **Future cloud-only namespace verbs** — `popola cloud agents`,
   `popola cloud cancel`, and any other yet-unshipped sub-verbs under
   `popola cloud` are reserved namespace, not stable surface.

---

## 8. Marking convention (`__experimental` / `extra`)

When a surface is **experimental** in PopolaLoom source:

- **Python class / function**: docstring includes the literal token
  `**__experimental**` on its own paragraph, plus a
  `# v0.x.y __experimental` source-code comment at the entry point.
- **CLI verb / flag**: help text begins with `[experimental]` and the
  `--help` output prints `(experimental — may change in v0.9.x minor releases)`
  as the last line of the description.
- **`pyproject.toml` extras**: the dependency name does NOT inherit
  `__experimental`; instead the comment block above
  `[project.optional-dependencies]` records the marking and the minor
  version that promotes the extra to stable.
- **Skill metadata keys**: opt-in keys outside the three stable
  front-matter fields ([§2.4](#24-skill-front-matter-contract)) carry
  no marker, but this document's [§7 Out-of-Scope](#7-out-of-scope)
  item 2 is the canonical disclaimer.

A v0.9.x **patch** release MUST NOT silently remove an experimental
marking; promotion to stable is a CHANGELOG line item plus removal of
the marking in the **same** PR — landing in a v0.9.x **minor**.

<!-- updated: 2026-05-08 -->
