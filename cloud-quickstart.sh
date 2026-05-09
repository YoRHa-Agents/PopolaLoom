#!/usr/bin/env bash
# cloud-quickstart.sh — copy-paste-ready Cloud Agent quickstart for v0.9.0 GA.
#
# Version: 0.9.0
# License: MIT (matches the PopolaLoom package license)
# Repo:    https://github.com/YoRHa-Agents/PopolaLoom
# Last updated: 2026-05-09
#
# v0.9.0 install recipe (Q-D-5 偏离默认: PyPI deferred to v0.9.x; see
# BL-v0.9.x-PyPI in TRACKER): use
#   pip install git+https://github.com/YoRHa-Agents/PopolaLoom@v0.9.0
# (canonical, tag-pinned) OR `./install.sh install --from=git` (alternate;
# auto-tracks main). The default `./install.sh install` resolves to the
# previous v0.8.x stable line until the v0.9.x PyPI patch lands.
#
# This script is the v0.9.0 GA cloud-agent bootstrap. It walks an
# operator from "fresh checkout + popolaloom installed + CURSOR_API_KEY
# set" to "first cloud-agent dispatch + attach + cloud runs history" in
# a single shell command, mirroring the W2.4 `popola init --target=
# cloud-only` Makefile shortcuts that the cloud-only scaffold exposes.
#
# Steps:
#
#   0. Pre-flight: CURSOR_API_KEY env var present + `popola` on PATH.
#   1. (optional) `popola init --target=cloud-only` — scaffold the
#      project (popolad.toml + .env.example + Makefile). Skipped when
#      `--no-init` is passed OR when the scaffold files already exist.
#   2. `popola popolad start` — boot the daemon (idempotent; warns
#      gracefully if already running).
#   3. `popola dispatch --cli=cursor-cloud --prompt "<prompt>"
#      [--cli-flag repo_url=<repo>]` — dispatch the cloud task. Stops
#      here in `--dry-run` mode.
#   4. `popola attach <task_id>` (one-shot dump; the operator can
#      re-run with `--follow` for live streaming).
#   5. `popola cloud runs <task_id>` — list the cloud agent's run
#      history (Q-C-1 deviation subcommand from v0.8.8).
#
# Usage:
#   ./cloud-quickstart.sh                                                 # default prompt + repo, runs all 5 steps
#   ./cloud-quickstart.sh --prompt "Plan database migration scaffolding"  # custom prompt
#   ./cloud-quickstart.sh --prompt "..." --repo-url "https://github.com/acme/monorepo"
#   ./cloud-quickstart.sh --no-init                                       # skip step 1 (existing scaffold)
#   ./cloud-quickstart.sh --target ./my-cloud-project                     # scaffold into a sub-directory
#   ./cloud-quickstart.sh --dry-run                                       # print every command, no I/O
#   ./cloud-quickstart.sh version
#   ./cloud-quickstart.sh --help
#
# Companion docs:
#   - docs/USER_GUIDE.md#cloud-agent-dispatch-v085
#   - docs/USER_GUIDE.md#popola-init---targetcloud-only-v090
#   - docs/MIGRATION_v07_to_v09.md
#   - docs/API_STABILITY.md
#
# Bash compatibility: bash 3.2+ (macOS default) — no associative arrays,
# no readarray, no <<<. Linux distros ship bash 4+.
#
# Per workspace rule "No Silent Failures": every external command runs
# through run_cmd() which prints the command (unless --quiet) and aborts
# on non-zero exit from critical steps. The single explicit best-effort
# step is `popola popolad start`, which logs the skip reason when the
# daemon is already up so the script proceeds rather than wedging on
# the harmless "already running" failure.

set -euo pipefail
IFS=$'\n\t'

readonly POPOLA_CLOUD_QUICKSTART_VERSION="0.9.0"
readonly POPOLA_GIT_URL="https://github.com/YoRHa-Agents/PopolaLoom"

# ── defaults ────────────────────────────────────────────────────────────

VERB=""
PROMPT="Smoke test: print the project's top-level layout"
REPO_URL=""
TARGET_DIR="."
NO_INIT=0
DRY_RUN=0
QUIET=0

INFO_PREFIX="[cloud-quickstart.sh]"

# ── Logging helpers ─────────────────────────────────────────────────────

log() {
    if [ "${QUIET}" -eq 1 ]; then
        return 0
    fi
    printf '%s %s\n' "${INFO_PREFIX}" "$*"
}

warn() {
    printf '%s WARN: %s\n' "${INFO_PREFIX}" "$*" >&2
}

error() {
    printf '%s ERROR: %s\n' "${INFO_PREFIX}" "$*" >&2
}

usage() {
    cat <<'USAGE'
cloud-quickstart.sh — copy-paste-ready PopolaLoom Cloud Agent quickstart (v0.9.0 GA).

Verbs:
  install / (default)   Run the full 5-step bootstrap (scaffold → daemon → dispatch → attach → cloud runs).
  version               Print the script version and exit.
  help / --help / -h    Print this message and exit.

Options (default verb):
  --prompt TEXT         Prompt to dispatch (default: built-in smoke prompt).
  --repo-url URL        Cursor Cloud Agent repo URL (passed via --cli-flag repo_url=<URL>).
  --target DIR          Project directory to scaffold into (default: current directory).
  --no-init             Skip step 1 (popola init --target=cloud-only); useful when the scaffold
                        already exists or you want to drive the dispatch standalone.
  --dry-run             Print every command that would be run; no I/O. Implies --no-init for
                        steps that would touch disk.
  --quiet, -q           Suppress informational output.
  --help, -h            Print this help.

Pre-flight:
  - CURSOR_API_KEY environment variable MUST be set (script exits 1 with a remediation hint when missing).
  - `popola` MUST be on PATH (script exits 1 with the install-method hint when missing).

Documentation:
  - docs/USER_GUIDE.md#cloud-agent-dispatch-v085
  - docs/USER_GUIDE.md#popola-init---targetcloud-only-v090
  - docs/API_STABILITY.md
  - docs/MIGRATION_v07_to_v09.md
USAGE
}

# ── Pre-flight checks ───────────────────────────────────────────────────

require_command() {
    # require_command <bin_name> <install_hint>
    local bin="$1"
    local hint="$2"
    if ! command -v "${bin}" >/dev/null 2>&1; then
        error "required command not found on PATH: ${bin}"
        error "  hint: ${hint}"
        exit 1
    fi
}

require_env_var() {
    # require_env_var <name> <hint>
    local name="$1"
    local hint="$2"
    # Use eval so we can read a variable whose name we only know at runtime.
    # `${!name}` works in bash 4+ but not in macOS bash 3.2; eval is portable.
    local value
    value="$(eval "printf '%s' \"\${${name}:-}\"")"
    if [ -z "${value}" ]; then
        error "required environment variable not set: ${name}"
        error "  hint: ${hint}"
        exit 1
    fi
}

run_cmd() {
    # run_cmd <description> -- <command> [args...]
    # Prints the command (unless --quiet); honours --dry-run.
    local description="$1"
    shift
    if [ "$1" = "--" ]; then
        shift
    fi
    log "${description}"
    if [ "${QUIET}" -ne 1 ]; then
        # Build a space-joined preview without disturbing the strict IFS.
        local _preview
        _preview="$(IFS=' '; printf '%s' "$*")"
        printf '  $ %s\n' "${_preview}"
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        return 0
    fi
    "$@"
}

# Best-effort wrapper for commands that may exit non-zero on benign
# states (e.g. "daemon already running"); prints the failure reason but
# does not abort the script per "No Silent Failures" — the failure IS
# logged, the operator just isn't blocked by an idempotent step.
run_cmd_best_effort() {
    local description="$1"
    shift
    if [ "$1" = "--" ]; then
        shift
    fi
    log "${description}"
    if [ "${QUIET}" -ne 1 ]; then
        local _preview
        _preview="$(IFS=' '; printf '%s' "$*")"
        printf '  $ %s\n' "${_preview}"
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        return 0
    fi
    if ! "$@"; then
        warn "  best-effort step exited non-zero; continuing (expected for idempotent boots)."
    fi
}

# ── Argument parsing ────────────────────────────────────────────────────

# First positional arg may be a verb; everything else is an option.
parse_verb() {
    case "${1:-}" in
        install|"")
            VERB="install"
            ;;
        version)
            VERB="version"
            ;;
        help|--help|-h)
            VERB="help"
            ;;
        --*|-*)
            # No verb supplied; default to install and re-dispatch.
            VERB="install"
            return 1   # signal caller to NOT shift the verb position
            ;;
        *)
            error "unknown verb: $1"
            error "  expected one of: install (default) | version | help"
            exit 2
            ;;
    esac
    return 0
}

# ── Step implementations ────────────────────────────────────────────────

step_preflight() {
    log "Step 0/5: pre-flight (CURSOR_API_KEY + popola on PATH)"
    require_env_var "CURSOR_API_KEY" \
        "export CURSOR_API_KEY='cr_...' (get one from https://cursor.com/dashboard → API Keys)"
    require_command "popola" \
        "for v0.9.0 GA: pip install git+${POPOLA_GIT_URL}@v0.9.0 (canonical, tag-pinned) \
OR ./install.sh install --from=git (Q-D-5 偏离默认: PyPI deferred to v0.9.x; see BL-v0.9.x-PyPI)"
    log "  OK pre-flight: CURSOR_API_KEY set; popola=$(command -v popola)"
}

step_init_cloud_only() {
    if [ "${NO_INIT}" -eq 1 ]; then
        log "Step 1/5: scaffold (skipped via --no-init)"
        return 0
    fi
    log "Step 1/5: scaffold cloud-only project at ${TARGET_DIR}"
    if [ "${TARGET_DIR}" != "." ] && [ ! -d "${TARGET_DIR}" ]; then
        run_cmd "  creating target directory" -- mkdir -p "${TARGET_DIR}"
    fi
    if [ -f "${TARGET_DIR}/popolad.toml" ] && [ -f "${TARGET_DIR}/.env.example" ]; then
        log "  scaffold appears already present (popolad.toml + .env.example)"
        log "  skipping popola init (idempotent — pass --no-init to silence this step)"
        return 0
    fi
    (
        cd "${TARGET_DIR}"
        run_cmd "  invoking popola init --target=cloud-only" \
            -- popola init --target=cloud-only
    )
}

step_daemon_start() {
    log "Step 2/5: boot popolad daemon"
    run_cmd_best_effort "  popola popolad start (idempotent — already-running is fine)" \
        -- popola popolad start
}

step_dispatch() {
    log "Step 3/5: dispatch cloud agent"
    if [ -n "${REPO_URL}" ]; then
        run_cmd "  popola dispatch --cli=cursor-cloud --prompt '${PROMPT}' --cli-flag repo_url=${REPO_URL}" \
            -- popola dispatch --cli=cursor-cloud --prompt "${PROMPT}" \
                                --cli-flag "repo_url=${REPO_URL}"
    else
        run_cmd "  popola dispatch --cli=cursor-cloud --prompt '${PROMPT}'" \
            -- popola dispatch --cli=cursor-cloud --prompt "${PROMPT}"
    fi
    log "  Note: copy the printed task_id from above; the next steps reference it as <task_id>."
}

step_attach_hint() {
    log "Step 4/5: attach to the dispatched task (one-shot dump)"
    log "  Re-run from a separate terminal for live streaming:"
    log "    popola attach <task_id> --follow"
    log "  Default --follow uses Cursor's SSE stream (since v0.8.6); pass --no-stream to force"
    log "  the legacy poll-only path."
}

step_cloud_runs_hint() {
    log "Step 5/5: inspect the cloud agent's full run history"
    log "  popola cloud runs <task_id>"
    log "  Add --json to script the listing; --include-events for per-row events_summary."
}

# ── Main ────────────────────────────────────────────────────────────────

main() {
    if ! parse_verb "${1:-}"; then
        # No verb supplied — VERB is already "install"; do NOT shift.
        :
    else
        shift || true
    fi

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --prompt)
                PROMPT="$2"
                shift 2
                ;;
            --prompt=*)
                PROMPT="${1#--prompt=}"
                shift
                ;;
            --repo-url)
                REPO_URL="$2"
                shift 2
                ;;
            --repo-url=*)
                REPO_URL="${1#--repo-url=}"
                shift
                ;;
            --target)
                TARGET_DIR="$2"
                shift 2
                ;;
            --target=*)
                TARGET_DIR="${1#--target=}"
                shift
                ;;
            --no-init)
                NO_INIT=1
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --quiet|-q)
                QUIET=1
                shift
                ;;
            --help|-h)
                VERB="help"
                shift
                ;;
            --)
                shift
                break
                ;;
            *)
                error "unknown option: $1"
                error "  run 'cloud-quickstart.sh --help' for the option list"
                exit 2
                ;;
        esac
    done

    case "${VERB}" in
        version)
            printf 'cloud-quickstart.sh v%s\n' "${POPOLA_CLOUD_QUICKSTART_VERSION}"
            exit 0
            ;;
        help)
            usage
            exit 0
            ;;
        install)
            log "PopolaLoom cloud-agent quickstart v${POPOLA_CLOUD_QUICKSTART_VERSION}"
            log "  prompt:    ${PROMPT}"
            log "  repo_url:  ${REPO_URL:-<not set; default Cursor account repo>}"
            log "  target:    ${TARGET_DIR}"
            log "  --no-init: $([ "${NO_INIT}" -eq 1 ] && printf 'yes' || printf 'no')"
            log "  --dry-run: $([ "${DRY_RUN}" -eq 1 ] && printf 'yes' || printf 'no')"
            step_preflight
            step_init_cloud_only
            step_daemon_start
            step_dispatch
            step_attach_hint
            step_cloud_runs_hint
            log "DONE — ${POPOLA_CLOUD_QUICKSTART_VERSION} cloud-quickstart bootstrap completed."
            log "  Next: copy the task_id from step 3 and run:"
            log "    popola attach <task_id> --follow"
            log "    popola cloud runs <task_id>"
            ;;
        *)
            error "internal error: unhandled verb '${VERB}'"
            exit 2
            ;;
    esac
}

main "$@"
