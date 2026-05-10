---
layout: default
title: Quickstart
description: 5-minute onboarding for PopolaLoom — install → first task → health check.
lang: en
translation_url: /zh/QUICKSTART.html
---

# PopolaLoom — 5-minute Quickstart

<!-- updated: 2026-05-10 -->

> Get from install to "task dispatched and visible in `popola list`" in five minutes. For the full reference, see [`USER_GUIDE.md`](USER_GUIDE.md).

## Prerequisites

- Python 3.11 or 3.12 (`python --version`)
- `pip` (`pip --version`)
- One of: Cursor, Claude Code, Codex CLI, or GitHub Copilot CLI installed (optional — popola works headless too)
- Optional: `lark-cli` on PATH for Lark HITL/notification flow

## Step 1 — Install popolaloom

```bash
# Current v0.9.9 release. The default installer path uses GitHub while PyPI promotion is deferred.
./install.sh install                                              # canonical (default --from=git, tracks main)

# Optional reproducible tag pin:
./install.sh install --ref=v0.9.9

# Optional secure-credential extra (v0.9.7+):
./install.sh install --with-credentials

# OR from a clone (dev):
git clone https://github.com/YoRHa-Agents/PopolaLoom.git
cd PopolaLoom
pip install -e ".[dev]"

# verify
python -c "import popolaloom; print(popolaloom.__version__)"   # → 0.9.9
which popola                         # → /usr/local/bin/popola (or similar)
popola version                       # → "popolaloom 0.9.9"
```

If you specifically need a tag-pinned manual fallback outside the installer, use `pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.9`. Avoid the bare package-name form until the `BL-v0.9.x-PyPI` promotion patch lands.

If `popola: command not found` after install, your shell's PATH may not include `~/.local/bin`. Fix:

```bash
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

## Step 1.5 — (optional) configure your Cursor API key

Use this when you plan to dispatch Cursor Cloud Agents (`--cli=cursor-cloud`), use `popola cloud runs`, run cross-PR relay, or start an Enterprise self-hosted worker pool.

```bash
# Recommended: store the key in the OS keyring and validate it once.
popola auth cursor set --validate

# Headless-container fallback (option A): rely on the environment slot.
export CURSOR_API_KEY="cr_..."

# Headless-container fallback (option B; v0.9.9+): let `popola init`
# write a 0o600 fallback file at ~/.popola/cursor_api_key.env
# (auto-sourced by `popola popolad start` from v0.9.9 onward).
popola init --cursor-api-key "cr_..."
source ~/.popola/cursor_api_key.env   # only needed for the SAME shell;
                                       # popolad auto-sources at startup.
```

`popola auth cursor set --validate` requires the optional keyring extra. The easiest path is `./install.sh install --with-credentials` on a fresh machine or `./install.sh update --with-credentials` on an existing install. In Linux containers without SecretService, the installer can add the Python keyring package but no OS backend exists; v0.9.9 closes that gap by writing the secret to the 0o600 fallback file at `~/.popola/cursor_api_key.env` so `popola dispatch` from a fresh shell after `popola init --cursor-api-key VAL` works once you `source` the file (or rely on the daemon auto-source on next `popola popolad start`).

## Step 2 — Register the Skill into your IDEs

```bash
popola init                          # auto-detect (preferred for first time)

# OR per-IDE explicit:
popola init cursor --global          # → ~/.cursor/skills/popola-loom/SKILL.md
popola init claude --global          # → ~/.claude/skills/popola-loom/SKILL.md
popola init codex                    # → $CODEX_HOME/skills/popola-loom/SKILL.md
popola init copilot                  # → <repo>/.github/copilot-instructions.md (project-only)
popola init local                    # → scaffold .local/ workspace surface

# OR every IDE at once (preferred when multiple IDEs are present):
popola init all --global             # every detected IDE except local, with global scope

# Inspect / dry-run:
popola init --list                   # print every detected target + install path
popola init --interactive            # human-driven wizard (v0.5.5+)
popola init cursor --project --dry-run   # preview writes without touching disk
```

(Re-running `popola init` is idempotent: it prints `SKIP <path> (already installed)` instead of overwriting operator edits. To force-refresh after a wheel upgrade, use `popola skill upgrade --target=<ide>`.)

## Step 3 — Boot the daemon

```bash
popola popolad start
# popolad started, PID=12345
# socket: ~/.popola/popolad.sock
# log:    ~/.popola/log/popolad.log

popola probe                         # quick health (pid + uptime + active task count)
popola popolad status                # full daemon state (socket / pid / probe roll-up)
```

The daemon survives terminal close + SSH disconnect (`start_new_session=True`); restart your machine and you'll need `popola popolad start` again. To stop it cleanly: `popola popolad stop` (SIGTERM → 5s grace → SIGKILL escalation).

## Step 4 — Dispatch your first task

```bash
popola dispatch "echo hello from popola" --cli=cursor
# → cursor-23e74ec18917

popola list                          # see active tasks (default = non-terminal only)
popola list --all                    # include completed/failed/canceled

popola status cursor-23e74ec18917    # full state envelope (JSON with --json)
```

The `task_id` is returned synchronously; the actual subprocess runs in the background managed by the daemon. You can close the terminal at this point — the task survives.

## Step 5 — Attach for live output

```bash
popola attach cursor-23e74ec18917 --follow
# → tails the SSE / NDJSON event stream:
#   process.stdout / process.stderr / state.* / task.completed
# → Ctrl-C exits attach but the task keeps running
```

`popola attach <id> --no-follow` does a one-shot dump of all events seen so far without blocking. If you want only the final state envelope, use `popola status <id> --json` instead.

## Step 6 — One-shot health check

```bash
popola doctor                        # 4-subsystem audit (skill / daemon / lark / arktower)
popola doctor --strict               # exit 1 on any FAIL (CI-friendly)
popola doctor --json                 # machine-readable envelope
```

The 4 subsystems audited:

1. **Skill** — every `(target, scope)` slot the installer knows about; reports `OK` / `MISS` / `DRIFT`.
2. **Daemon** — `GET /probe` over the popolad UDS socket; `OK` (with pid + uptime) when up.
3. **Lark** — `lark-cli` on PATH + `LARK_HITL_TARGET_OPEN_ID` env var; `OK` / `WARN` / `OFF`.
4. **ArkTower** — vendored module imports cleanly + the two PopolaLoom migrations are on disk; `WARN` when migrations are missing.

## Or just run the automated 6-step smoke

```bash
bash examples/quickstart.sh
# [quickstart] Step 0/6: Skill installer dry-run
# [quickstart] Step 1/6: starting popolad in POPOLA_HOME=...
# [quickstart] Step 2/6: dispatching echo task via cursor adapter
# [quickstart] Step 3/6: confirming task appears in popola list
# [quickstart] Step 4/6: querying popola status ...
# [quickstart] Step 5/6: running popola doctor
# [quickstart] Step 6/6: stopping popolad
# [quickstart] all 6 steps PASS — popolaloom v0.9.9 ready
```

The script honours `$POPOLA_HOME` (default: a fresh `mktemp -d`) so it never pollutes your real `~/.popola`.

## Where to next

- **Full CLI + MCP reference**: [`USER_GUIDE.md`](USER_GUIDE.md)
- **Walkthroughs + example outputs**: [`DEMO.md`](DEMO.md)
- **Interactive visual demo**: [`demo-page.md`](demo-page.md)
- **Core design philosophy**: [`design-ideas.md`](design-ideas.md)
- **Latest release notes**: [`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md)
- **Historical archive (every version)**: [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)
- **Cloud-only bootstrap**: run `./cloud-quickstart.sh` after configuring a Cursor API key (env var OR keyring — see Step 1.5).
- **Secure Cursor API key storage (v0.9.2+)**: easiest path is `./install.sh install --with-credentials` (v0.9.7+) which bundles the optional `keyring>=25` extra into the same install; then `popola auth cursor set --validate` persists and verifies the key in the OS keyring. `popola auth cursor status` shows resolver state without revealing the value. See [`USER_GUIDE.md#credentials--secure-storage-v092`](USER_GUIDE.md#credentials--secure-storage-v092).
- **Self-hosted worker handoff**: use [`USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091`](USER_GUIDE.md#self-hosted-worker-handoff-popola-cloud-worker-v091) when you want this machine registered in Cursor Cloud Agents UI; use `--cli=cursor-cloud` instead when you need a popola-tracked task id.
- **Want an LLM to install for you?** Open Cursor or Claude Code and say `install popola` — the `install-popola` Skill (v0.7.0+) handles it.
- **Hands-off envelope（v0.8.0+ NEW）**: Every dispatch persists a Markdown front-matter envelope under `.local/.agent/handoff/<id>.md`; replay any prior dispatch via `popola dispatch --replay <handoff_id>`. See [`USER_GUIDE.md#hands-off-envelope`](USER_GUIDE.html#hands-off-envelope).

## Common errors + fixes

| Symptom | Fix |
|---|---|
| `popola: command not found` | `export PATH="$HOME/.local/bin:$PATH"` (and append to `~/.bashrc`) |
| `popolad failed to bind socket` | A stale socket from a previous daemon: `rm ~/.popola/popolad.sock` then retry |
| `popola doctor` reports `DRIFT` for the Skill | `popola skill upgrade --target=all` to refresh from the wheel |
| Cursor / Claude doesn't auto-load the Skill | Restart the IDE; Skill discovery happens at startup |
| `Permission denied` installing globally | Use `./install.sh install --scope=project` inside a writable clone, or run inside a virtualenv |
| `ArkTower migrations dir not found` | Set `POPOLA_ARKTOWER_MIGRATIONS_DIR` or rely on the vendored default |

For the full troubleshooting guide, see [`USER_GUIDE.md#troubleshooting`](USER_GUIDE.md#troubleshooting).
