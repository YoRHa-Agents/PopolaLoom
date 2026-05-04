"""Smoke test: verify package import + version string.

v0.4.0 GA release: closes the v0.0.1 → v0.4.0 phase 1 journey.  All
14 R-series issues (R-001..R-014) closed across v0.2.0 + v0.3.0 + the
v0.3.x self-evolution rounds.  See ``release-notes-v0.4.0.md`` for the
full roadmap progression, 5/5 self-bootstrap real evidence, 8/8 nines
dimensions, auto-merge gate workflow, and known limitations.

v0.3.5 release: round-5 self-evolution patch — quickstart + DEMO docs.
Refreshed README.md to v0.3.5 era + added ``examples/quickstart.sh``
(automating 5 steps: start daemon / dispatch echo / list / eval run /
stop) + ``docs/DEMO.md`` walkthrough + ``tests/matrix/tier5/test_quickstart_smoke.py``
(6 cases verifying the script, README pointer, and DEMO.md presence).

v0.3.4 release: round-4 self-evolution patch — mutation-testing
baseline.  Manual audit of `daemon/state.py` identified 7 surviving
mutations against the v0.3.3 test suite (kill rate 70.8 %); 12 new
mutation-resistance tests under
``tests/matrix/tier1/test_state_mutation_resistance.py`` lift the
inferred kill rate to 100 % on that module.  Live `mutmut run`
remains pinned in `pyproject.toml [tool.mutmut]` for v0.4.x once the
mutmut 3.5 / `src/` layout friction is resolved (see
``evidence/mutmut-baseline.md``).

v0.3.3 release: round-3 self-evolution patch — wired
``hitl_handleability``'s ``lark_health`` sub-score to **real evidence**.
``collect_evidence`` now scans NDJSON logs for ``lark.send.*`` (success
rate) + ``lark.listener.*`` (uptime), and the Tier 4 chaos test
``test_lark_supervisor_escalates_after_3_restarts`` verifies the
supervisor escalates to HITL after 4 consecutive listener deaths.

v0.3.2 release: round-2 self-evolution patch — added quantitative
NFR-2 (``GET /status`` mean RTT < 200 ms over 50 samples) + NFR-9
(``POST /dispatch`` p95 < 1 s over 20 samples) benchmarks under
``tests/matrix/nfr/`` (5 new test cases).  Closes the v0.3.0-plan §6
risk-register entry that flagged NFR-2 + NFR-9 as un-benchmarked
before v0.4.0 GA.

v0.3.1 release: round-1 self-evolution patch (42 coverage gap fillers
across mcp/tools, mcp/elicitation, cycle_convergence, lark/listener,
hitl/renderers/cli) lifted default-lane coverage from 89.23 → 90.79%
and restored ``fail_under = 90`` per testing-matrix.md §6.1.

v0.3.0 release: bumped from 0.2.3 → 0.3.0 after F1 (8 nines real
measurement) + F2 (relay/supervise/federate primitives) + F2.5
(devola-flow skill injection + dual gate) + F3 (auto-merge 5-AND gate)
+ F4 (HITL handle-ability full stack with 5 renderers + Lark 双向 +
006 popola_hitl table + cross-channel sync + nines hitl_handleability
swap) + F5 (real S2/S4/S5 self-bootstrap replacing v0.2.3 mocks).
"""

import popolaloom


def test_import_and_version() -> None:
    """popolaloom 顶层包可被 import 且 __version__ 与 pyproject.toml 一致."""
    assert popolaloom is not None
    assert popolaloom.__version__ == "0.4.0"
