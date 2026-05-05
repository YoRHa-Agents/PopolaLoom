#!/usr/bin/env bash
# examples/quickstart.sh — 6-step smoke for the PopolaLoom v0.5.0 demo.
#
# Per `tests/integration/test_quickstart_v050.py` and v0.5.0 Stage S5,
# this script must:
#
#   0. Show the new `popola init` Skill installer (dry-run, no writes).
#   1. Start popolad daemon (UDS bind under $POPOLA_HOME).
#   2. Dispatch an "echo" task via popola CLI.
#   3. Confirm the task appears in `popola list`.
#   4. Run `popola status` against the dispatched task.
#   5. Run the new `popola doctor` aggregate health check.
#   6. Stop popolad cleanly.
#
# All steps must succeed (exit 0) for the integration smoke test to
# pass. The script honours $POPOLA_HOME, defaulting to a tmp dir
# under /tmp so it doesn't pollute a user's real ~/.popola.
#
# v0.5.0 deviation from the L3 task brief: the brief example used
# `--cli=echo`, but `echo` is not a registered adapter (the v0.5.0
# adapter registry contains only cursor / claude / codex). The script
# below uses `--cli=cursor` (the same shape as the v0.3.5 quickstart);
# the dispatched subprocess may fail at spawn time when cursor-agent
# is not on PATH, but the dispatch HTTP call still succeeds + returns
# a task_id (the failure is reflected in the task's terminal state,
# which `popola status` happily reports).

set -euo pipefail

POPOLA_HOME="${POPOLA_HOME:-$(mktemp -d -t popolaloom-quickstart-XXXXXX)}"
export POPOLA_HOME

# Set ArkTower migrations path with a sensible fallback so the daemon's
# ArkTower SQLite can be initialised on first start. v0.5.0 vendors
# the migrations under src/popolaloom/_vendored/arktower/migrations/
# so this fallback only matters on very old checkouts.
if [[ -z "${POPOLA_ARKTOWER_MIGRATIONS_DIR:-}" ]]; then
    if [[ -d "/home/agent/reference/ArkTower/migrations" ]]; then
        export POPOLA_ARKTOWER_MIGRATIONS_DIR="/home/agent/reference/ArkTower/migrations"
    fi
fi

cleanup() {
    # Best-effort daemon shutdown — workspace rule "No Silent Failures"
    # applies to production code, not to teardown convenience scripts;
    # any failure here is logged and ignored.
    popola popolad stop >/dev/null 2>&1 || echo "[quickstart] daemon stop failed (already down?)" >&2
    rm -rf "$POPOLA_HOME" || true
}
trap cleanup EXIT

echo "[quickstart] Step 0/6: Skill installer dry-run (NEW in v0.5.0)"
popola init cursor --project --dry-run
popola init claude --project --dry-run

echo "[quickstart] Step 1/6: starting popolad in POPOLA_HOME=${POPOLA_HOME}"
popola popolad start

echo "[quickstart] Step 2/6: dispatching echo task via cursor adapter"
DISPATCH_OUTPUT="$(popola dispatch "echo hello popola v0.5.0" --cli cursor --json 2>&1)"
echo "${DISPATCH_OUTPUT}"
TASK_ID="$(printf '%s\n' "${DISPATCH_OUTPUT}" | python -c "
import json, sys
for line in sys.stdin:
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and 'task_id' in obj:
            print(obj['task_id'])
            sys.exit(0)
    except json.JSONDecodeError:
        continue
sys.exit('task_id not found in dispatch output')
")"
echo "[quickstart] dispatched task_id=${TASK_ID}"

echo "[quickstart] Step 3/6: confirming task appears in popola list"
popola list --all --json | python -c "
import json, sys
data = json.load(sys.stdin)
ids = {t.get('task_id') for t in data}
target = '${TASK_ID}'
assert target in ids, f'expected {target!r} in list, got {sorted(ids)}'
print(f'[quickstart]  ✓ task_id {target!r} present in list')
"

echo "[quickstart] Step 4/6: querying popola status ${TASK_ID}"
popola status "${TASK_ID}" --json | python -c "
import json, sys
info = json.load(sys.stdin)
state = info.get('state', '<missing>')
print(f'[quickstart]  ✓ task state={state!r}, exit_code={info.get(\"exit_code\")}')
"

echo "[quickstart] Step 5/6: running popola doctor (aggregate health, NEW in v0.5.0)"
popola doctor --json | python -c "
import json, sys
report = json.load(sys.stdin)
summary = report.get('summary', {})
fail = summary.get('fail', 0)
warn = summary.get('warn', 0)
drift = summary.get('drift', 0)
verdicts = summary.get('verdicts', {})
print(f'[quickstart]  ✓ doctor: fail={fail} warn={warn} drift={drift}')
for sub, verdict in sorted(verdicts.items()):
    print(f'[quickstart]    {sub:<10s} {verdict}')
"

echo "[quickstart] Step 6/6: stopping popolad"
popola popolad stop

echo "[quickstart] all 6 steps PASS — popolaloom v$(python -c 'import popolaloom; print(popolaloom.__version__)') ready"
