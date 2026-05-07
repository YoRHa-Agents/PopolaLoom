#!/usr/bin/env bash
# install.sh — unified PopolaLoom + Skill installer / updater / uninstaller.
#
# Version: 0.8.4
# License: MIT (matches the PopolaLoom package license)
# Repo:    https://github.com/YoRHa-Agents/PopolaLoom
#
# This is the v0.8.4 single-shell-command installer that wraps the four
# manual steps from the install-popola Skill workflow:
#
#   1. pip install popolaloom (or git+, or local path)
#   2. popola skill install --target=<...> --<scope>
#   3. popola popolad start  (best-effort)
#   4. popola doctor          (best-effort)
#
# It also exposes the inverse path: ``install.sh uninstall`` removes the
# Skill from every IDE then ``pip uninstall popolaloom`` (and, when
# ``--purge`` is set, deletes ``$POPOLA_HOME``).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/YoRHa-Agents/PopolaLoom/main/install.sh | bash -s -- install --scope=global --target=all
#   ./install.sh install   --target=cursor --scope=project --from=git
#   ./install.sh update    --target=all
#   ./install.sh uninstall --target=all --yes --purge
#   ./install.sh version
#
# Run ``./install.sh --help`` for the full flag matrix.
#
# Bash compatibility: targets bash 3.2+ (macOS default) — no associative
# arrays, no readarray, no <<<. Linux distros ship bash 4+.
#
# Per workspace rule "No Silent Failures": every external command runs
# through the run_cmd() helper that prints the command (unless --quiet)
# and aborts on non-zero exit from critical steps. The single explicit
# best-effort step is the post-install daemon boot, which logs the skip
# reason when popolad fails to start (the install proper still succeeds
# so the operator can manually retry ``popola popolad start``).

set -euo pipefail
IFS=$'\n\t'

readonly POPOLA_INSTALL_SCRIPT_VERSION="0.8.4"
readonly POPOLA_PACKAGE_NAME="popolaloom"
readonly POPOLA_GIT_URL="git+https://github.com/YoRHa-Agents/PopolaLoom.git"

# ── defaults ────────────────────────────────────────────────────────────

VERB=""
SCOPE="global"
TARGET="all"
FROM="pypi"
PIN_VERSION=""
PYTHON_BIN=""
NO_SKILLS=0
NO_DAEMON=0
PURGE=0
ASSUME_YES=0
DRY_RUN=0
QUIET=0

# ── ANSI helpers (no escape sequences inside printf format strings —
#    keeps quiet-mode output egrep-able and avoids weird stdout in pipes).

INFO_PREFIX="[install.sh]"

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

die() {
    error "$@"
    exit 1
}

# ── usage / help ────────────────────────────────────────────────────────

usage() {
    cat <<EOF
PopolaLoom unified installer (install.sh v${POPOLA_INSTALL_SCRIPT_VERSION})

Usage: install.sh <verb> [options]

Verbs:
  install     (default) Install popolaloom + register Skills
  update      Upgrade popolaloom + refresh Skills
  uninstall   Remove Skills + uninstall popolaloom
  version     Print install.sh version + exit 0
  help, --help, -h
              Print usage + exit 0

Options:
  --scope=<global|project>     Skill install scope (default: global)
  --target=<cursor|claude|codex|copilot|all>
                                Which IDE Skill(s) to install (default: all)
  --from=<pypi|git|PATH>        Install source for popolaloom: pypi (default),
                                git (latest main from GitHub),
                                or a local path/wheel.
  --version=<X.Y.Z>             Pin a specific PyPI version (install/update only)
  --python=<bin>                Python interpreter to use (default: python3)
  --no-skills                   Skip Skill install/uninstall step
  --no-daemon                   Skip daemon start (install verb only)
  --purge                       (uninstall) also delete \$POPOLA_HOME (\${POPOLA_HOME:-\$HOME/.popola})
  --yes, -y                     Assume yes to interactive prompts
  --dry-run                     Print every command without executing
  --quiet, -q                   Suppress informational output
  --help, -h                    Print usage + exit 0

Examples:
  install.sh install --target=all --scope=global
  install.sh install --from=git
  install.sh install --from=./dist/popolaloom-0.8.4-py3-none-any.whl
  install.sh update --target=cursor
  install.sh uninstall --yes
  install.sh uninstall --yes --purge
EOF
}

# ── argument parsing ────────────────────────────────────────────────────

# Parse a single argument; bumps argv handling done by the caller.
# Returns 0 always; sets globals based on flags.
parse_flag() {
    local arg="$1"
    case "${arg}" in
        --scope=*)
            SCOPE="${arg#--scope=}"
            ;;
        --target=*)
            TARGET="${arg#--target=}"
            ;;
        --from=*)
            FROM="${arg#--from=}"
            ;;
        --version=*)
            PIN_VERSION="${arg#--version=}"
            ;;
        --python=*)
            PYTHON_BIN="${arg#--python=}"
            ;;
        --no-skills)
            NO_SKILLS=1
            ;;
        --no-daemon)
            NO_DAEMON=1
            ;;
        --purge)
            PURGE=1
            ;;
        --yes|-y)
            ASSUME_YES=1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --quiet|-q)
            QUIET=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            die "unknown option: ${arg} (run with --help for usage)"
            ;;
    esac
}

parse_args() {
    if [ "$#" -eq 0 ]; then
        VERB="install"
        return 0
    fi

    local first="$1"
    case "${first}" in
        install|update|uninstall|version)
            VERB="${first}"
            shift
            ;;
        help|--help|-h)
            usage
            exit 0
            ;;
        --*|-*)
            VERB="install"
            ;;
        *)
            die "unknown verb: ${first} (expected install|update|uninstall|version|help)"
            ;;
    esac

    while [ "$#" -gt 0 ]; do
        parse_flag "$1"
        shift
    done

    validate_args
}

validate_args() {
    case "${SCOPE}" in
        global|project) ;;
        *) die "invalid --scope=${SCOPE} (expected global|project)" ;;
    esac

    case "${TARGET}" in
        cursor|claude|codex|copilot|all) ;;
        *) die "invalid --target=${TARGET} (expected cursor|claude|codex|copilot|all)" ;;
    esac

    if [ -n "${PIN_VERSION}" ] && [ "${VERB}" = "uninstall" ]; then
        die "--version=X.Y.Z is not valid for the uninstall verb"
    fi

    if [ -n "${PIN_VERSION}" ] && [ "${FROM}" != "pypi" ]; then
        die "--version=X.Y.Z requires --from=pypi (got --from=${FROM})"
    fi
}

# ── command runner ──────────────────────────────────────────────────────

# Join all positional args with a single space, regardless of IFS.
# Bash 3.2+ portable; preserves order, drops zero-length args' separator.
join_args() {
    local out=""
    local arg
    for arg in "$@"; do
        if [ -z "${out}" ]; then
            out="${arg}"
        else
            out="${out} ${arg}"
        fi
    done
    printf '%s' "${out}"
}

# run_cmd runs the given command unless --dry-run is set, in which case
# it prints "DRY-RUN: ..." and returns 0. When critical=1 and the
# command fails, the script aborts (per "No Silent Failures").
run_cmd() {
    local critical="$1"
    shift
    local pretty
    pretty="$(join_args "$@")"
    if [ "${DRY_RUN}" -eq 1 ]; then
        if [ "${QUIET}" -eq 0 ]; then
            printf 'DRY-RUN: %s\n' "${pretty}"
        fi
        return 0
    fi
    if [ "${QUIET}" -eq 0 ]; then
        printf '+ %s\n' "${pretty}"
    fi
    if "$@"; then
        return 0
    fi
    local rc=$?
    if [ "${critical}" -eq 1 ]; then
        die "command failed (exit ${rc}): ${pretty}"
    fi
    warn "command failed (exit ${rc}); continuing in best-effort mode: ${pretty}"
    return "${rc}"
}

# ── python detection ────────────────────────────────────────────────────

detect_python() {
    if [ -n "${PYTHON_BIN}" ]; then
        if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
            die "--python=${PYTHON_BIN} is not on PATH; install Python 3.11+ or pass a different --python"
        fi
        echo "${PYTHON_BIN}"
        return 0
    fi
    local candidate
    for candidate in python3.12 python3.11 python3 python; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            local version
            version="$(${candidate} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")"
            local major minor
            major="$(printf '%s' "${version}" | cut -d. -f1)"
            minor="$(printf '%s' "${version}" | cut -d. -f2)"
            if [ "${major}" -ge 3 ] && [ "${minor}" -ge 11 ] 2>/dev/null; then
                echo "${candidate}"
                return 0
            fi
        fi
    done
    die "Python 3.11+ not found. Install Python 3.11 or 3.12, or pass --python=<path-to-bin>."
}

# ── pip source resolver ─────────────────────────────────────────────────

# Echoes the install spec to be passed to pip install / pip install --upgrade.
resolve_install_spec() {
    case "${FROM}" in
        pypi)
            if [ -n "${PIN_VERSION}" ]; then
                printf '%s==%s' "${POPOLA_PACKAGE_NAME}" "${PIN_VERSION}"
            else
                printf '%s' "${POPOLA_PACKAGE_NAME}"
            fi
            ;;
        git)
            printf '%s' "${POPOLA_GIT_URL}"
            ;;
        *)
            # Treat as a local filesystem path or .whl.
            printf '%s' "${FROM}"
            ;;
    esac
}

# ── confirmation prompt ─────────────────────────────────────────────────

confirm() {
    local prompt="$1"
    if [ "${ASSUME_YES}" -eq 1 ] || [ "${DRY_RUN}" -eq 1 ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        die "${prompt}: stdin is not a tty; pass --yes to skip the confirmation prompt"
    fi
    local reply
    printf '%s [y/N] ' "${prompt}"
    read -r reply
    case "${reply}" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# ── popolaloom install probe ────────────────────────────────────────────

# Returns 0 iff popolaloom is importable.
popola_installed() {
    local py="$1"
    "${py}" -c 'import popolaloom' >/dev/null 2>&1
}

# ── verbs ───────────────────────────────────────────────────────────────

verb_install() {
    log "PopolaLoom install — verb=install scope=${SCOPE} target=${TARGET} from=${FROM}"

    local py
    py="$(detect_python)"
    log "using python: ${py}"

    local spec
    spec="$(resolve_install_spec)"

    log "step 1/4: pip install ${spec}"
    run_cmd 1 "${py}" -m pip install "${spec}"

    if [ "${NO_SKILLS}" -eq 1 ]; then
        log "step 2/4: skipping skill install (--no-skills)"
    else
        log "step 2/4: popola skill install --target=${TARGET} --${SCOPE}"
        run_cmd 1 popola skill install --target="${TARGET}" "--${SCOPE}"
    fi

    if [ "${NO_DAEMON}" -eq 1 ]; then
        log "step 3/4: skipping daemon start (--no-daemon)"
    else
        log "step 3/4: popola popolad start (best-effort)"
        if ! run_cmd 0 popola popolad start; then
            warn "popolad failed to start; you can retry manually with 'popola popolad start'."
        fi
    fi

    log "step 4/4: popola doctor (best-effort)"
    run_cmd 0 popola doctor || warn "popola doctor reported issues — review the output above."

    log "install complete."
}

verb_update() {
    log "PopolaLoom update — scope=${SCOPE} target=${TARGET} from=${FROM}"

    local py
    py="$(detect_python)"

    local spec
    spec="$(resolve_install_spec)"

    log "step 1/3: pip install --upgrade ${spec}"
    run_cmd 1 "${py}" -m pip install --upgrade "${spec}"

    if [ "${NO_SKILLS}" -eq 1 ]; then
        log "step 2/3: skipping skill upgrade (--no-skills)"
    else
        log "step 2/3: popola skill upgrade --target=${TARGET} --${SCOPE}"
        run_cmd 1 popola skill upgrade --target="${TARGET}" "--${SCOPE}"
    fi

    log "step 3/3: popola doctor (best-effort)"
    run_cmd 0 popola doctor || warn "popola doctor reported issues — review the output above."

    log "update complete."
}

verb_uninstall() {
    log "PopolaLoom uninstall — scope=${SCOPE} target=${TARGET} purge=${PURGE}"

    local py
    py="$(detect_python)"

    if [ "${DRY_RUN}" -eq 0 ] && ! popola_installed "${py}"; then
        log "popolaloom not installed; nothing to do."
        return 0
    fi

    log "step 1/4: stopping popolad daemon (best-effort)"
    run_cmd 0 popola popolad stop || warn "popolad stop failed (already down?); continuing."

    if [ "${NO_SKILLS}" -eq 1 ]; then
        log "step 2/4: skipping skill uninstall (--no-skills)"
    else
        log "step 2/4: popola skill uninstall --target=${TARGET} --${SCOPE}"
        run_cmd 1 popola skill uninstall --target="${TARGET}" "--${SCOPE}"
    fi

    if ! confirm "Uninstall the popolaloom Python package via pip?"; then
        log "skipped pip uninstall (operator declined)."
        return 0
    fi

    log "step 3/4: pip uninstall ${POPOLA_PACKAGE_NAME}"
    run_cmd 1 "${py}" -m pip uninstall -y "${POPOLA_PACKAGE_NAME}"

    if [ "${PURGE}" -eq 1 ]; then
        local home_dir="${POPOLA_HOME:-$HOME/.popola}"
        if confirm "Purge ${home_dir} (this deletes daemon state, sqlite, events)?"; then
            log "step 4/4: rm -rf ${home_dir}"
            run_cmd 1 rm -rf "${home_dir}"
        else
            log "skipped purge (operator declined)."
        fi
    else
        log "step 4/4: skipping purge (--purge not set; ${POPOLA_HOME:-$HOME/.popola} retained)"
    fi

    log "uninstall complete."
}

verb_version() {
    printf 'install.sh v%s\n' "${POPOLA_INSTALL_SCRIPT_VERSION}"
}

# ── main ────────────────────────────────────────────────────────────────

main() {
    parse_args "$@"

    case "${VERB}" in
        install)   verb_install ;;
        update)    verb_update ;;
        uninstall) verb_uninstall ;;
        version)   verb_version ;;
        *)         die "internal error: unhandled verb=${VERB}" ;;
    esac
}

main "$@"
