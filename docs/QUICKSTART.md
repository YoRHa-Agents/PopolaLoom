---
layout: default
title: Quickstart
description: 5-minute onboarding for PopolaLoom — install → first task → health check.
lang: en
translation_url: /zh/QUICKSTART.html
---

# PopolaLoom — 5-minute Quickstart

<!-- updated: 2026-05-09 -->

> Get from install to "task dispatched and visible in `popola list`" in five minutes. For the full reference, see [`USER_GUIDE.md`](USER_GUIDE.md).

## Prerequisites

- Python 3.11 or 3.12 (`python --version`)
- `pip` (`pip --version`)
- One of: Cursor, Claude Code, Codex CLI, or GitHub Copilot CLI installed (optional — popola works headless too)
- Optional: `lark-cli` on PATH for Lark HITL/notification flow

## Step 1 — Install popolaloom

```bash
# Current v0.9.6 release.
# v0.9.6 closes feedback_for_v0.9.4 lines 2-5: ./install.sh install no longer defaults
# to PyPI (which 404'd on Chinese pip mirrors that don't carry popolaloom yet).
# PyPI promotion is still deferred for the v0.9.x line, so use the GitHub paths:
./install.sh install                                              # canonical (default --from=git, tracks main)
./install.sh install --ref=v0.9.6                                 # canonical tag-pinned (recommended for v0.9.6)
pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.6 # manual fallback (always-works, tag-pinned)

# OR from a clone (dev):
git clone https://github.com/YoRHa-Agents/PopolaLoom.git
cd PopolaLoom
pip install -e ".[dev]"

# verify
python -c "import popolaloom; print(popolaloom.__version__)"   # → 0.9.6
which popola                         # → /usr/local/bin/popola (or similar)
popola version                       # → "popolaloom 0.9.6"
```

If you intentionally want the latest PyPI-published stable line, `pip install popolaloom` still works, but it currently resolves to the previous v0.8.x line until the `BL-v0.9.x-PyPI` promotion patch lands. After that, `./install.sh install --from=pypi --version=0.9.x` becomes the opt-in PyPI path; the `./install.sh install` default stays on the GitHub URL because v0.9.6 flipped it there.

If `popola: command not found` after install, your shell's PATH may not include `~/.local/bin`. Fix:

```bash
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

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
# [quickstart] all 6 steps PASS — popolaloom v0.9.6 ready
```

The script honours `$POPOLA_HOME` (default: a fresh `mktemp -d`) so it never pollutes your real `~/.popola`.

## Where to next

- **Full CLI + MCP reference**: [`USER_GUIDE.md`](USER_GUIDE.md)
- **Walkthroughs + example outputs**: [`DEMO.md`](DEMO.md)
- **Latest release notes**: [`RELEASE_NOTES.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/RELEASE_NOTES.md)
- **Historical archive (every version)**: [`CHANGELOG.md`](https://github.com/YoRHa-Agents/PopolaLoom/blob/main/CHANGELOG.md)
- **Cloud-only bootstrap**: run `./cloud-quickstart.sh` after configuring a Cursor API key (env var OR keyring — see next bullet).
- **Secure Cursor API key storage (v0.9.2+)**: `pip install 'popolaloom[credentials]'` then `popola auth cursor set` to persist the key in the OS keyring instead of `export`-ing it in every shell. `popola auth cursor status` shows resolver state without revealing the value. See [`USER_GUIDE.md#credentials--secure-storage-v092`](USER_GUIDE.md#credentials--secure-storage-v092).
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
| `Permission denied` installing globally | Use `pip install --user popolaloom` or run inside a virtualenv |
| `ArkTower migrations dir not found` | Set `POPOLA_ARKTOWER_MIGRATIONS_DIR` or rely on the vendored default |

For the full troubleshooting guide, see [`USER_GUIDE.md#troubleshooting`](USER_GUIDE.md#troubleshooting).
