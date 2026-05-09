> **Policy (v0.7.0+)**: This file is overwritten with each release; for the full historical archive of every version see [`CHANGELOG.md`](CHANGELOG.md). Per-version `release-notes-v*.md` files were consolidated into this single file in v0.7.0 (per user feedback v0.6.1#2).

# PopolaLoom v0.9.2 — Secure Cursor API key storage

<!-- updated: 2026-05-10 -->

> Released: 2026-05-10

> **How to install v0.9.2** (Q-D-5 偏离默认 carries forward; PyPI promotion remains tracked as `BL-v0.9.x-PyPI`):
>
> ```bash
> # Option A — canonical, tag-pinned (always works for v0.9.2):
> pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.2
>
> # Option A+ — opt into the new secure-credentials extra (recommended for cloud users):
> pip install 'popolaloom[credentials] @ git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.2'
>
> # Option B — repo-root unified installer, from-git (auto-tracks main; post-tag = v0.9.2):
> ./install.sh install --from=git
> ```

## Theme

v0.9.2 is the **second strictly-additive patch on the v0.9.x line**. It closes the v0.9.1 user-feedback request "could there be a sufficiently safe storage / protection setting for the API key, added to popola, and made part of `popola init`?" by shipping an OS-keyring-backed credential resolver, a new `popola auth cursor` Typer subapp, and an opt-in `popola init --target=cloud-only --configure-cursor-auth` flag — all without breaking a single line of v0.8.x / v0.9.0 / v0.9.1 documentation, CI workflow, or `.env.example` file. Every previous `export CURSOR_API_KEY=...` snippet keeps working because the env var stays the highest-precedence operator-facing slot in the new resolver; the keyring is precedence #3, queried only when the env var is unset.

This release is **strictly additive** under the v0.9.x SemVer contract published in [`docs/API_STABILITY.md`](docs/API_STABILITY.md): the new `popola auth cursor {set,status,clear}` verbs join the stable CLI surface alongside the v0.9.0 / v0.9.1 verbs without altering them; the new `CredentialStatus.to_json_dict()` envelope (`configured` / `source` / `backend_name` / `fingerprint` / `keyring_available`) and the `popolaloom.credentials` resolver precedence chain are pinned in [§2.5](docs/API_STABILITY.md#25-cursor-api-key-credential-resolver-v092) as part of v0.9.x. Six cloud call sites (`--cli=cursor-cloud` dispatch, cloud cancel, `popola cloud runs`, `popola relay`, cloud SSE attach, `popola cloud worker --pool`) now route through the resolver instead of reading `os.environ` directly; this is the single seam that lets the keyring slot answer when the env var is unset, and it is the single seam that makes the change invisible when the env var is set.

The defense-in-depth contribution is the **cloud marker payload redaction**: when an operator passes `--cli-flag api_key=...` to a `popola dispatch --cli=cursor-cloud` invocation, the inline value used to land verbatim in `TaskHandle.cmd`, the NDJSON `task.dispatched` event, and the ArkTower SQLite `cmd` column — readable via `popola list` / `popola status --json` / NDJSON tail. v0.9.2 strips it to `<REDACTED:CURSOR_API_KEY>` at the persistence boundaries while still passing the unredacted cmd to `Supervisor.spawn` so the override path keeps working. `tests/test_credentials_redaction.py` pins this contract.

## Highlights

### `popolaloom.credentials` — typed credential resolver (NEW; v0.9.2)

Single source of truth for resolving the Cursor Cloud Agents REST API key. Lives at [`src/popolaloom/credentials.py`](src/popolaloom/credentials.py); pinned in [`docs/API_STABILITY.md` §2.5](docs/API_STABILITY.md#25-cursor-api-key-credential-resolver-v092).

Precedence chain (highest first):

1. **Explicit override** — `resolve_cursor_api_key(override=...)` / `CredentialResolver(override=...)`. Test-only / library-injection hook; CLI does NOT expose this slot.
2. **Environment variable** — `CURSOR_API_KEY`. Highest-precedence operator-facing slot. Whitespace-only values are ignored (treated as unset).
3. **OS keyring** — `popolaloom.cursor` / username `default`. Populated by `popola auth cursor set` (or the `init --target=cloud-only --configure-cursor-auth` prompt). Backend is the active OS keychain (macOS Keychain, Windows Credential Manager, libsecret on Linux, KWallet, etc.); requires `pip install 'popolaloom[credentials]'`.
4. **Missing** — returns `None`; cloud call sites print a remediation message naming all three slots.

The resolver is the **only** v0.9.x-supported path for reading the API key in PopolaLoom code. Every cloud call site that previously read `os.environ.get("CURSOR_API_KEY")` directly (six in total) now goes through the resolver — backward-compatible, env var still wins, but the keyring slot answers when the env var is unset.

### `popola auth cursor` — three-verb subcommand group (NEW; v0.9.2)

| Verb | Purpose | Stable flags (v0.9.x) |
|---|---|---|
| `popola auth cursor set` | Persist a key in the OS keyring | `--api-key VAL`, `--from-env`, `--validate`, `--json` (`--api-key` ⊕ `--from-env`) |
| `popola auth cursor status` | Show resolver state without revealing the secret | `--json` |
| `popola auth cursor clear` | Remove the keyring entry (env var untouched) | `--yes` / `-y`, `--json` |

The literal API key value never appears in stdout / stderr / log output for any of the three verbs. `set` reads from `--api-key` (already on the operator's argv), `--from-env` (copies the env var into the keyring), or a hidden-input prompt (`typer.prompt(hide_input=True)`). `--validate` round-trips `GET /v1/me` to confirm Cursor accepts the key BEFORE persisting; failures emit a redacted error and exit `77` without writing the keyring. `status` only surfaces `configured` / `source` / `backend_name` / `fingerprint` (12 hex chars of `sha256(value)`) / `keyring_available` — six security invariants pinned by the test suite (see [Security invariants](#security-invariants)).

### `popola init --target=cloud-only --configure-cursor-auth` (NEW; v0.9.2)

Opt-in flag on the existing cloud-only init flow. After the three scaffold files (`popolad.toml` / `.env.example` / `Makefile`) are written, walks the operator through a one-shot `popola auth cursor set` interaction — hidden-input prompt, fingerprint-only confirmation banner. The `--interactive` wizard accepts the same flag and runs the helper after the IDE / `.local/` install plan completes.

The flag is **opt-in** because:

- It prompts; non-interactive callers (CI) should rely on the env-var slot and not be blocked by an interactive question.
- `--dry-run` short-circuits the prompt entirely (No Silent Failures: never prompt for a secret during a preview).
- When the keyring extra is unavailable, the helper prints an actionable hint (`pip install 'popolaloom[credentials]'`) plus the env-var fallback rather than failing the scaffold.

### Setup walkthrough (one-time per machine)

```bash
# 1. Install the optional extra (one-time per machine).
pip install 'popolaloom[credentials]'

# 2. Store the key (interactive hidden-input prompt; pipe-friendly variants below).
popola auth cursor set
# Cursor API key (will be stored in the OS keyring; input hidden):

# 2b. Pipe-friendly variants:
popola auth cursor set --api-key cr_...                       # explicit
popola auth cursor set --from-env                             # migrate from `export`
popola auth cursor set --api-key cr_... --validate            # round-trip GET /v1/me first

# 3. Confirm it's reachable (NEVER reveals the value).
popola auth cursor status
# Cursor API key: configured
#   source:           keyring
#   backend:          macOS Keychain   (or "libsecret" / "Secret Service" / "Windows Credential Manager" / ...)
#   fingerprint:      9c1f3a4b2e8d
#   keyring available: True

# 4. Remove (idempotent; env var untouched).
popola auth cursor clear --yes
```

After step 2, every cloud call site (`popola dispatch --cli=cursor-cloud`, `popola attach <cloud-task>`, `popola cloud runs`, `popola relay`, cloud cancel) resolves the key from the keyring without further configuration. You can `unset CURSOR_API_KEY` from your shell — the keyring slot answers from then on.

### Cloud call sites that route through the resolver

| Call site | What changed |
|---|---|
| [`adapters/cursor_cloud.py`](src/popolaloom/adapters/cursor_cloud.py)::`CursorCloudAdapter.is_available()` | Returns True iff `resolve_cursor_api_key()` is non-None (was: env-var-only) |
| [`daemon/supervisor.py`](src/popolaloom/daemon/supervisor.py)::`_spawn_cloud()` | Resolves via the resolver with `extra.api_key` as the explicit override |
| [`daemon/server.py`](src/popolaloom/daemon/server.py)::`_resolve_cloud_cursor_client()` | Cloud cancel resolver (replaces direct env-var read) |
| [`cli/cloud_cmd.py`](src/popolaloom/cli/cloud_cmd.py)::`runs` | `popola cloud runs` resolver |
| [`cli/relay_cmd.py`](src/popolaloom/cli/relay_cmd.py) | `popola relay` cloud dispatch resolver |
| [`cli/main.py`](src/popolaloom/cli/main.py)::`_maybe_spawn_cloud_sse_thread()` | Cloud SSE attach resolver |
| [`cli/cloud_worker_cmd.py`](src/popolaloom/cli/cloud_worker_cmd.py)::`_spawn_worker_subprocess()` | Pool worker injects keyring-resolved key into the subprocess env |

Backward-compatibility: every previous v0.8.x / v0.9.0 / v0.9.1 doc, CI workflow, and `.env.example` keeps working byte-for-byte because the env var remains the highest-precedence operator-facing slot. The resolver is invisible when `CURSOR_API_KEY` is set; it only matters when it isn't.

### Cloud marker payload redaction (defense in depth)

When `--cli-flag api_key=...` is passed to `popola dispatch --cli=cursor-cloud`, the inline value used to land verbatim in `TaskHandle.cmd`, the NDJSON `task.dispatched` event payload, and the ArkTower SQLite `cmd` column. v0.9.2 adds [`redact_cloud_marker_cmd`](src/popolaloom/adapters/cursor_cloud.py) and [`_redact_cmd_for_persistence`](src/popolaloom/daemon/server.py); both replace `extra.api_key` with `<REDACTED:CURSOR_API_KEY>` before the cmd reaches any persistence boundary, while passing the unredacted cmd through to `Supervisor.spawn` so the override path keeps working. `tests/test_credentials_redaction.py` pins this contract; non-cloud cmds (vanilla `cursor-agent` / `claude` / `codex` argv) pass through unchanged.

## Security invariants

The following invariants are part of the v0.9.x stable surface; tests in [`tests/test_credentials.py`](tests/test_credentials.py), [`tests/cli/test_auth_cmd.py`](tests/cli/test_auth_cmd.py), and [`tests/test_credentials_redaction.py`](tests/test_credentials_redaction.py) pin them at PR time:

1. The literal API key value never appears in stdout, stderr, log output, NDJSON event payloads, audit rows, or handoff envelopes. Status surfaces show only `configured` / `source` / `backend_name` / `fingerprint` / `keyring_available`.
2. The fingerprint is the first **12 hex chars** of `sha256(value)` — enough to disambiguate "is this the same key I just set?" without leaking entropy.
3. The non-secret metadata file at `$POPOLA_HOME/credentials.toml` is created with mode `0600` (owner read/write only) and contains only `backend` / `fingerprint` / `last_set_at` — never the value itself.
4. The keyring service identifier `popolaloom.cursor` and username slot `default` are stable; changing either would orphan operator-stored secrets.
5. When the keyring extra is missing, `popola auth cursor set` exits **3** with a remediation hint rather than silently falling back to a plaintext file.
6. The cursor-cloud marker payload (visible via `popola list` / `popola status` after the v0.8.5 dispatch path) redacts `extra.api_key` to `<REDACTED:CURSOR_API_KEY>` before persisting, so the override slot leaks zero entropy into the SQLite + NDJSON surfaces.

## Test surface

- **Default lane**: `pytest -m "not slow and not nightly and not real_cli and not real_lark" -q` → **2761 passed, 25 skipped, 82 deselected** (was 2729 at the v0.9.1 GA tip; +32 new tests).
- **Coverage**: **94.21%** (was 94.00% at v0.9.1; coverage gate `[tool.coverage.report] fail_under = 94` unchanged).
- **Lint**: `ruff check src/popolaloom tests/` clean; `mypy src/popolaloom` clean (98 source files).
- **New test files**: [`tests/test_credentials.py`](tests/test_credentials.py) (49 cases — precedence, fingerprint, redaction, backend-name labels, storage round-trip, metadata file mode, escaped quotes), [`tests/test_credentials_redaction.py`](tests/test_credentials_redaction.py) (7 cases — cloud-marker redaction happy path, pass-through for non-cloud / malformed / missing-key, fresh-list-not-mutation, server-side wrapper), [`tests/cli/test_auth_cmd.py`](tests/cli/test_auth_cmd.py) (21 cases — full verb matrix, `--validate` flow, hidden-input prompt, JSON envelope shape, exit-3 on missing extra, raw-secret-never-echoed), [`tests/cli/test_init_configure_cursor_auth.py`](tests/cli/test_init_configure_cursor_auth.py) (6 cases — happy path, declined, empty input, no-keyring hint, dry-run short-circuit, flag-misuse rejection).

## Out of scope (deferred to v0.10.x or later)

- **Multi-profile support** — v0.9.2 stores at most one Cursor API key (service `popolaloom.cursor`, username `default`). Operators with separate personal vs service-account keys must rely on the env-var override to switch contexts. Multi-profile slots (`personal`, `service-account`) are tracked for v0.10.x.
- **Alternative backends** — HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager are not exposed in v0.9.x; only the OS keyring and env-var paths are SemVer-stable. The `override=` kwarg on `CredentialResolver` is the public-API-but-not-CLI-exposed test seam.
- **Threat model bounded by the OS login session** — the keyring backend is at most as secure as the operator's login session. v0.9.2 does NOT defend against root-level attackers reading `/proc/<pid>/environ` (env path), malicious processes running as the same user (the keyring is unlocked for the session — by design), or operators who paste the key into chat tools / commit it to git manually.

## Companion docs

- [`docs/API_STABILITY.md`](docs/API_STABILITY.md) §2.5 — credential resolver SemVer-stable contract; row 14 in §2.1 for `popola auth cursor`.
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md#credentials--secure-storage-v092) — full credentials & secure-storage walkthrough with verb reference, init `--configure-cursor-auth` flow, security invariants, and threat model.
- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) + [`docs/zh/QUICKSTART.md`](docs/zh/QUICKSTART.md) — new "Where to next" bullet pointing at `popola auth cursor set` (EN + ZH).
- [`README.md`](README.md) — verb-table row + Cloud Agent dispatch callout.

## Cross-links

- Issue this closes: v0.9.1 user-feedback request for "a sufficiently safe storage path for the API key as part of `popola init`".
- Companion CHANGELOG entry: [`CHANGELOG.md` §[0.9.2]](CHANGELOG.md).
- Skill marker bumps: `src/popolaloom/skills/popola-loom/SKILL.md` `version: 0.9.2`; `src/popolaloom/skills/install-popola/SKILL.md` `version: 0.9.2`; `.popola-loom-version` markers synchronised with `popolaloom.__version__`.
