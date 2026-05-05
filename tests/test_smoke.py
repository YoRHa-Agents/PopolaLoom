"""Smoke test: verify package import + version string.

v0.5.2 patch (Loop 2 of v0.5.x → v0.6.0 self-improvement series):
closes the three v0.5.1 deferred items by (1) aligning
``.github/workflows/automerge.yml --cov-fail-under`` from 90 to 92
so the auto-merge gate matches the project pyproject directive,
(2) wiring ``LarkSupervisor.stop()`` into the
``daemon/rpc.py:lifespan`` exit hook so the optional ``lark-cli
event consume`` subprocess is torn down cooperatively at daemon
shutdown (closes
[`release-notes-v0.5.1.md`](release-notes-v0.5.1.md)
known-limitation #2), and (3) lifting default-lane coverage with
new tests against ``daemon/server.py`` + ``daemon/supervisor.py``
+ ``lark/listener.py`` (the 87 % / 87 % / 81 % modules called out
as the next coverage targets in v0.5.1's known-limitation #3).
Slow-lane gets two new NFR benchmark files
(``tests/matrix/nfr/test_nfr_2_status_rtt.py`` + extensions to
``tests/matrix/nfr/test_nfr_9_dispatch_p95.py``) that publish
``mean / p95 / p99`` percentiles for trend tracking plus mocked-
daemon serialization-overhead floors via ``httpx.MockTransport``.
See ``release-notes-v0.5.2.md`` for the full closure summary +
verification commands.

v0.5.1 patch (Loop 1 of v0.5.x → v0.6.0 self-improvement series):
unblocks GitHub-hosted CI by replacing the hardcoded
``mkdir -p /home/agent/reference`` (which fails with Permission
denied on the runner user) with a ``[ -w /home ]`` writability
guard in ``ci.yml`` + ``automerge.yml``; lifts default-lane
coverage 91.15 % → 92.56 % via 90 new error-path tests across
``tests/cli/test_main_error_paths.py`` (NEW), ``tests/daemon/
test_rpc_error_paths.py`` (NEW), and the doctor-cmd test
extensions; raises the ``[tool.coverage.report] fail_under`` gate
from 91 to 92 to lock in the new floor.  See
``release-notes-v0.5.1.md`` for the full closure summary +
verification commands.

v0.5.0 release (Phase 2 prelude): user-facing Skill interaction
surface + DevolaFlow-style multi-IDE installer.  Closes the v0.4.0
"Known limitations" §4 (Skill install / `popola init` / multi-IDE)
in 5 stages: S1 vendored ArkTower at ``popolaloom._vendored.arktower``
(removing the ``arktower @ file://`` direct reference per Q5-4 fallback
to Path B vendor), S2 ``popola init`` Typer subcommand group with 8
verbs + 8 modifiers (mirrors DevolaFlow ``devola-init`` per Q5-2 lock),
S3 canonical SKILL.md at ``src/popolaloom/skills/popolaloom/SKILL.md``
(~ 10 623 chars / ~ 2 655 tokens, 7 sections, ships in wheel), S4
``popola skill {install,doctor,upgrade}`` subcommand group + ``popola
doctor`` aggregate health verb (4 new verbs total), S5 docs / DEMO /
quickstart refresh + release notes + e2e + version bump.  See
``release-notes-v0.5.0.md`` for the full v0.0.1 → v0.5.0 journey
table, 5/5 stage closures, the Q5-1..Q5-5 answer ledger, and the
final default-lane test count + coverage gate.

v0.4.1 minor (Phase 1 close-out): the proactive Lark notification
patch — the supervisor wait-thread now emits ``task.canceled``
(closing the latent contract bug from v0.4.0 research §F.3), 5 new
card builders cover the terminal-state taxonomy, ``lark/notifier.py``
sends the cards on every COMPLETED/FAILED/CANCELED transition, and
``_build_default_popolad`` auto-starts ``LarkSupervisor`` when env
vars opt in.  See ``release-notes-v0.4.1.md`` for the full set of
closures, the 23 new default-lane tests (15 L1 + 8 L2), coverage
delta (91.36 % → 91.38 %), and v0.5.0 hand-off contract.

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
    assert popolaloom.__version__ == "0.5.2"
