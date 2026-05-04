#!/usr/bin/env bash
# examples/quickstart.sh — 5-step smoke for the PopolaLoom v0.3.5 demo.
#
# Per `tests/matrix/tier5/test_quickstart_smoke.py` and v0.3.5
# round-5 evidence, this script must:
#
#   1. Start popolad daemon (UDS bind under $POPOLA_HOME).
#   2. Dispatch an "echo" task via popola CLI.
#   3. Confirm the task appears in `popola list`.
#   4. Run `popola eval run` and read 8 dimension scores from the
#      output TOML.
#   5. Stop popolad cleanly.
#
# All steps must succeed (exit 0) for the matrix tier-5 smoke test
# to pass.  The script honours $POPOLA_HOME, defaulting to a tmp dir
# under /tmp so it doesn't pollute a user's real ~/.popola.

set -euo pipefail

POPOLA_HOME="${POPOLA_HOME:-$(mktemp -d -t popolaloom-quickstart-XXXXXX)}"
export POPOLA_HOME

NINES_OUT="${NINES_OUT:-${POPOLA_HOME}/quickstart-nines.toml}"

# Set ArkTower migrations path with a sensible fallback so the daemon's
# ArkTower SQLite can be initialised on first start.  Honour the user's
# explicit POPOLA_ARKTOWER_MIGRATIONS_DIR if already set.
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

echo "[quickstart] Step 1/5: starting popolad in POPOLA_HOME=${POPOLA_HOME}"
popola popolad start

echo "[quickstart] Step 2/5: dispatching echo task via cursor adapter"
DISPATCH_OUTPUT="$(popola dispatch "echo hello popola" --cli cursor --json 2>&1)"
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

echo "[quickstart] Step 3/5: confirming task appears in popola list"
popola list --all --json | python -c "
import json, sys
data = json.load(sys.stdin)
ids = {t.get('task_id') for t in data}
target = '${TASK_ID}'
assert target in ids, f'expected {target!r} in list, got {sorted(ids)}'
print(f'[quickstart]  ✓ task_id {target!r} present in list')
"

echo "[quickstart] Step 4/5: running popola eval run → ${NINES_OUT}"
popola eval run --output "${NINES_OUT}"
python -c "
import sys, tomllib
with open('${NINES_OUT}', 'rb') as fh:
    data = tomllib.load(fh)
dims = data.get('dimensions', {})
expected = {
    'dispatch_isolation', 'cycle_convergence', 'hitl_latency',
    'attach_correctness', 'cross_cli_handoff', 'single_threaded_writes',
    'event_log_completeness', 'hitl_handleability',
}
missing = expected - dims.keys()
assert not missing, f'missing dimensions: {missing}'
composite = data.get('composite')
assert isinstance(composite, float), f'composite not a float: {composite!r}'
print(f'[quickstart]  ✓ 8/8 dimensions present, composite={composite:.3f}')
for name in sorted(dims):
    print(f'[quickstart]    {name:<24s} {dims[name]:.3f}')
"

echo "[quickstart] Step 5/5: stopping popolad"
popola popolad stop

echo "[quickstart] all 5 steps PASS — popolaloom v$(python -c 'import popolaloom; print(popolaloom.__version__)') ready"
