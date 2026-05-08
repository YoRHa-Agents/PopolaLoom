---
name: install-popola
version: 0.8.7
description: "Install PopolaLoom (popola CLI + popolad daemon + the `popola-loom` Skill) globally for Cursor / Claude Code / Codex / GitHub Copilot. Trigger when the user says install popola / install popola-loom / install popolaloom / set up popola-loom / 装 popola-loom / 装 popolaloom / 安装 popola / /install-popola. Walks pip install + per-IDE registration + daemon boot + post-install verification (popola doctor)."
metadata:
  surfaces: ["cli", "ide"]
  requires:
    bins: ["pip", "popola"]
    pythonVersion: ">=3.11"
  cliHelp: "popola init --help"
tier: 1
token_estimate: 1900
last_updated: "2026-05-08"
triggers:
  - "install popola"
  - "install popola-loom"
  - "install popolaloom"
  - "set up popola"
  - "set up popola-loom"
  - "set up popolaloom"
  - "装 popola-loom"
  - "装 popolaloom"
  - "安装 popola-loom"
  - "安装 popolaloom"
  - "/install-popola"
  - "register popola skill"
  - "register popola-loom skill"
  - "update popola"
  - "update popolaloom"
  - "uninstall popola"
  - "uninstall popolaloom"
  - "更新 popolaloom"
  - "更新 popola-loom"
  - "卸载 popolaloom"
  - "卸载 popola-loom"
  - "/update-popola"
  - "/uninstall-popola"
---

# install-popola Skill

## What this Skill does

A standalone, installer-only Skill that walks the host agent (Cursor / Claude Code / Codex / GitHub Copilot) through registering PopolaLoom on a fresh machine: `pip install popolaloom` → `popola init <ide> --global` → `popola popolad start` → `popola doctor` smoke. Mirrors the conventional `/install-devola-flow` workflow that installs DevolaFlow globally to `~/.cursor/skills/devola-flow/` and `~/.claude/skills/devola-flow/`. Idempotent and safe to re-run for upgrades — every `popola init` step prints `SKIP <path> (already installed)` instead of overwriting.

## When to use

Trigger this Skill (NOT the canonical `popola-loom` Skill) on any of:

- "install popola" / "install popola-loom" / "install popolaloom" / "set up popola" / "set up popola-loom" / "set up popolaloom"
- "装 popola-loom" / "装 popolaloom" / "安装 popola-loom" / "安装 popolaloom" / "把 popola 装到我的电脑" / "全局安装 popola"
- "/install-popola" slash command
- "register popola skill" / "register popola-loom skill" / "把 popola 加到 cursor / claude"

> **Note**: The user-facing Skill identifier was renamed from `popolaloom` to `popola-loom` in v0.7.1+; the legacy phrasings (`install popolaloom`, `装 popolaloom`, etc.) remain as triggers above so existing muscle memory keeps working. The Python package name `popolaloom` is unchanged.

The canonical `popola-loom/SKILL.md` (loaded after install) assumes `popola` is already on PATH and the daemon can be started. If that assumption fails on the host machine, run THIS Skill first.

### Cloud Agent prerequisite (v0.8.5+)

If you will drive **Cursor Background / Cloud Agents** through PopolaLoom (`--cli=cursor-cloud`), provision a **`CURSOR_API_KEY`** alongside your shell profile **before** invoking `popola dispatch`. This is unrelated to ordinary local `cursor-agent` binaries — omit the key entirely if you only use `--cli=cursor|claude|codex|kimi|copilot` subprocess adapters.

## Pre-flight checks (run first, in order)

```bash
python --version              # must be 3.11+
pip --version                 # needed for install
which popola                  # if present → jump to "Upgrade path"
test -d ~/.cursor && echo "cursor present" || echo "cursor absent"
test -d ~/.claude && echo "claude present" || echo "claude absent"
```

If `which popola` returns a path, popolaloom is already installed — skip Steps 1 + 2 below and go straight to **Upgrade path** (or just to **Verification checklist** for a sanity smoke).

## Install (full path — fresh machine)

### Step 0 — one-line install (preferred, v0.8.4+)

```bash
curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash
```

The unified `install.sh` (v0.8.4+) wraps Steps 1–4 below into a single shell command: `pip install popolaloom` → `popola skill install --target=all --global` → `popola popolad start` → `popola doctor`. It is **idempotent** — safe to re-run on an already-installed machine. Useful options:

```bash
# install only for Cursor at project scope
curl -fsSL .../install.sh | bash -s -- install --scope=project --target=cursor

# install latest main from GitHub (when PyPI is gated by a corporate proxy)
curl -fsSL .../install.sh | bash -s -- install --from=git

# pin a specific version
curl -fsSL .../install.sh | bash -s -- install --version=0.8.4

# preview every command without touching disk
curl -fsSL .../install.sh | bash -s -- install --dry-run
```

The script auto-detects Python 3.11+ (searching `python3.12` → `python3.11` → `python3` → `python`); pass `--python=/path/to/bin` to override. Errors are surfaced explicitly per the workspace "No Silent Failures" rule.

If the one-line bootstrap fails (e.g. corporate firewall blocks `raw.githubusercontent.com`), fall back to the **manual Steps 1–4** below.

### Step 1 — pip install (one of these)

```bash
pip install popolaloom                                          # PyPI (when published)
pip install -e .                                                # from a clone (dev workflow)
pip install git+https://github.com/YoRHa-Agents/PopolaLoom.git  # latest main
```

If the user is on a corporate network that blocks PyPI, the `pip install git+...` form usually still works (HTTPS to github.com is whitelisted in most environments).

### Step 2 — register the Skill into every IDE you use

```bash
popola init                   # auto-detect (preferred for first-time setup)

# OR explicit per-IDE (idempotent; second run prints SKIP):
popola init cursor --global   # → ~/.cursor/skills/popola-loom/SKILL.md
popola init claude --global   # → ~/.claude/skills/popola-loom/SKILL.md
popola init codex             # → $CODEX_HOME/skills/popola-loom/SKILL.md
popola init copilot           # → <repo>/.github/copilot-instructions.md (project-only)
popola init local             # → scaffold .local/ workspace surface

# OR every IDE at once (preferred when multiple IDEs are present):
popola init all --global      # every IDE except local, with global scope
```

`popola init` is the v0.5.0 multi-IDE installer (8 verbs × 8 modifiers; mirrors DevolaFlow per Q5-2 lock). The default scope is project-local; pass `--global` to install for the whole user.

### Step 3 — boot the daemon

```bash
popola popolad start
# → "popolad started, PID=12345" + socket at ~/.popola/popolad.sock
```

The daemon is the persistent process that holds task state, the event bus, and the socket every `popola dispatch / list / status / attach / cancel` call talks to. Start it once per machine; re-run `popola popolad start` after a reboot (or wire it into systemd / launchd if you want it pinned).

### Step 4 — verify install (5-second smoke)

```bash
popola doctor                 # 4-subsystem health table (skill / daemon / lark / arktower)
popola version                # → "popolaloom 0.7.0"
popola list-cli               # registered adapters (cursor / claude / codex / copilot etc.)
```

`popola doctor` exits non-zero on any FAIL row, so it's CI-friendly. The expected first-run output is **0 FAIL** rows; any FAIL row prints a remediation hint.

## Upgrade path (popola already installed)

```bash
# Preferred (v0.8.4+) — single-command upgrade
./install.sh update

# Or manually (matches the v0.5.0–v0.8.3 workflow):
pip install --upgrade popolaloom
popola skill upgrade --target=all   # overwrite installed SKILL.md with the wheel-shipped baseline
popola doctor                       # confirm no DRIFT
```

`popola skill upgrade` (Stage S4 of v0.5.0+) compares SHA-256 between the wheel-shipped SKILL.md and the on-disk installed copy, takes a `.popola-loom-bak.<ts>` backup, then writes the new content. Running `popola init` instead is also safe — it's idempotent — but it WON'T overwrite an existing SKILL.md (it only writes when the file is missing).

## Uninstall path (v0.8.4+)

```bash
# Preferred (v0.8.4+) — single-command teardown (interactive prompt before pip uninstall)
./install.sh uninstall

# Scripted (CI / non-tty) — skip the prompt and also delete daemon state under ~/.popola/
./install.sh uninstall --yes --purge

# Or surgically remove the Skill from one IDE without touching the package:
popola skill uninstall --target=cursor --global
```

`popola skill uninstall` (NEW in v0.8.4) is the inverse of `popola skill install`: it removes the SKILL.md (and the sibling `.popola-loom-version` marker for cursor/claude/codex; copilot has no marker since it ships as a single `copilot-instructions.md` file) and prunes the now-empty `popola-loom/` leaf directory. It is **idempotent** — re-running on a clean home prints `ABSENT` rather than failing. The unified `install.sh uninstall` verb composes this with `popola popolad stop` + `pip uninstall popolaloom` (+ optional `rm -rf $POPOLA_HOME` when `--purge` is set) for full teardown in one command.

## Interactive wizard (alternative, v0.5.5+)

```bash
popola init --interactive
```

Walks the operator through per-IDE confirmations: detect IDEs → confirm install per IDE → choose scope → confirm plan → execute. Equivalent to Step 2 above but human-driven; safer for unfamiliar setups or when the operator wants to review every install path before writes.

## Verification checklist

| Check | Command | Expected |
|---|---|---|
| popola CLI on PATH | `which popola` | `/usr/local/bin/popola` (or similar) |
| Python module imports | `python -c "import popolaloom; print(popolaloom.__version__)"` | `0.7.0` |
| Cursor Skill installed | `cat ~/.cursor/skills/popola-loom/SKILL.md \| head -1` | `---` (frontmatter) |
| Claude Skill installed | `cat ~/.claude/skills/popola-loom/SKILL.md \| head -1` | `---` |
| daemon running | `popola probe` | `pid=...  uptime=...` |
| 4-subsystem audit | `popola doctor` | `0 FAIL` rows |

If every row matches the expected column, the install is complete. Move on to the canonical `popola-loom` Skill — the host agent will auto-load it the next time the user mentions "dispatch a task" / "派发任务".

## Common installation errors and fixes

- **`popola: command not found`** — `pip install` succeeded but `~/.local/bin` is not on PATH. Add it: `export PATH="$HOME/.local/bin:$PATH"` and append the same line to `~/.bashrc` / `~/.zshrc` so it survives new shells.
- **Permission denied installing globally** — use `pip install --user popolaloom` instead of system-wide; or run inside a virtualenv (`python -m venv .venv && source .venv/bin/activate && pip install popolaloom`).
- **`popolad failed to bind socket`** — a stale socket from a previous daemon is at `~/.popola/popolad.sock`. Delete it (`rm ~/.popola/popolad.sock`) then retry `popola popolad start`. If a previous `popolad` is still running, `popola popolad stop` first.
- **`popola doctor` reports DRIFT for the Skill** — the installed SKILL.md version differs from the wheel version (usually because the user upgraded the wheel without re-running install). Run `popola skill upgrade --target=all` to refresh.
- **Cursor / Claude doesn't auto-load the Skill after install** — restart the IDE (or open a new chat); Skill discovery happens at startup. Confirm the file exists with `ls ~/.cursor/skills/popola-loom/SKILL.md`.

## After install — what next?

Open Cursor or Claude Code in any project and say:

- "派发任务给 cursor 跑 X" / "dispatch a task to cursor: X"
- "list my running agents" / "popola list"
- "check my popola-loom health" / "popola doctor"

The host agent will auto-load the canonical `popola-loom` Skill (now installed) and route the request to the right `popola` verb. From here on, this `install-popola` Skill is dormant unless the user later asks to re-install / upgrade.

## Reference

- **Repo**: [github.com/YoRHa-Agents/PopolaLoom](https://github.com/YoRHa-Agents/PopolaLoom)
- **Canonical Skill (loaded after install)**: `~/.cursor/skills/popola-loom/SKILL.md` (or `~/.claude/skills/popola-loom/SKILL.md`)
- **5-minute Quickstart**: `docs/QUICKSTART.md`
- **DEMO walkthrough**: `docs/DEMO.md`
- **User Guide**: `docs/USER_GUIDE.md`
- **Sibling installer (reference)**: `/install-devola-flow` slash command — installs DevolaFlow globally with the same shape (curl → bash + per-IDE registration + verify).

## Version + drift detection

- This Skill's frontmatter `version` field reflects the wheel-shipped baseline at install time (currently `0.7.0`; bumped in lockstep with each minor release).
- After upgrading the wheel, run `popola skill upgrade --target=all` to refresh the on-disk SKILL.md (otherwise `popola doctor` flags `DRIFT v0.6.1 (expected v0.7.0)` once the wheel moves ahead).
- The companion `.popola-loom-version` marker beside this `SKILL.md` is the byte-stable input the doctor uses for the drift check; do not hand-edit it (the installer + upgrader own the file).
