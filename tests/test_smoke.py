"""Smoke test: verify package import + version string.

v0.7.0 minor (closes the 4 user-feedback items from v0.6.1 — see
``.local/feedbacks/feedback_for_v0.6.1.md``): (1) ``.local/`` is
gitignored (NOT deleted; on-disk files preserved); (2) ten per-version
``release-notes-v*.md`` files consolidated into a single floating
``RELEASE_NOTES.md`` (the historical archive stays in
``CHANGELOG.md``); (3) comprehensive docs refresh — ``README.md`` +
``docs/USER_GUIDE.md`` + ``docs/QUICKSTART.md`` + a Jekyll-ready
GitHub Pages site under ``docs/index.md`` + ``docs/_config.yml`` +
the ``docs/DEMO.md`` v0.7.0 era refresh; (4) NEW standalone
``install-popola`` Skill at ``src/popolaloom/skills/install-popola/``
(``SKILL.md`` + ``.popola-loom-version``; the dash in the directory
name means this is wheel data resolved via :func:`importlib.resources.files`,
never imported as a Python package) mirroring
the conventional ``/install-devola-flow`` workflow used to install
DevolaFlow globally. The smoke suite gains
``test_both_skills_resolve_via_importlib`` to assert BOTH the
canonical ``popola-loom/SKILL.md`` AND the new ``install-popola/
SKILL.md`` ship in the wheel and resolve via
``importlib.resources.files('popolaloom') / 'skills' / .../SKILL.md``
(the same lookup path the ``popola init`` installer uses to read the
wheel-bundled Skill before copying it into the per-IDE install
target). See ``RELEASE_NOTES.md`` for the closure ledger +
verification commands; the historical archive stays in
``CHANGELOG.md``.

v0.7.1+ rename (in lockstep with this docstring's references): the
user-facing Skill identifier was renamed from ``popolaloom`` to
``popola-loom`` (directory ``src/popolaloom/skills/popola-loom/``,
frontmatter ``name: popola-loom``, version-marker filename
``.popola-loom-version``). The Python package name ``popolaloom``
is unchanged.

v0.6.1 patch (CI hotfix — closes 3 distinct CI failures blocking the
v0.6.0 PR): (1) mypy strict raises ~12 errors inside the vendored
ArkTower subset under ``src/popolaloom/_vendored/arktower/`` (arg-type
mismatches + a ``list`` shadowing the builtin used as a type
annotation in upstream code); resolved by ``[tool.mypy] exclude =
["src/popolaloom/_vendored/.*"]`` mirroring the existing ruff +
coverage carve-outs for that tree. (2) ``.workflow/automerge.yaml``
was matched by the ``.workflow/`` pattern in ``.gitignore`` so the
auto-merge gate's repo-level config never reached the runner; the
``test_repo_workflow_automerge_yaml_loads_cleanly`` case asserted the
file exists. Resolved with a ``!.workflow/automerge.yaml`` whitelist
line plus the new tracked file documenting the 5 AND conditions.
(3) ``tests/test_repository.py`` (4 cases) failed with
``sqlite3.OperationalError: no such table: tasks`` because the test
fixture passes the legacy ``/home/agent/reference/ArkTower/migrations``
path explicitly to ``make_persistence(arktower_migrations_dir=...)``
and that dir does not exist on GitHub-hosted runners. The fix in
``daemon/repository.py:make_persistence`` falls through to the
vendored auto-detection when the explicit path's ``Path.is_dir()``
returns False — ``popolaloom._vendored.arktower.cli.deps.migrations_dir``
points at the in-package ``migrations/`` directory bundled with the
wheel. See ``release-notes-v0.6.1.md`` for the full 3-fix closure +
verification commands.

v0.5.5 patch (Loop 5 — final patch before v0.6.0 consolidation):
the polish loop. Closes the v0.5.4 carry-overs by (1) refreshing
``README.md`` + ``docs/DEMO.md`` to reflect v0.5.{1,2,3,4} closures
+ adding a "Loop-driven self-improvement" section explaining the
v0.5.x → v0.6.0 5-loop pattern; (2) adding ``--interactive`` to the
``popola init`` root callback (``typer.confirm`` + ``typer.prompt``
based wizard for human-driven setup); (3) closing the v0.5.4
deferred bullet by promoting ``evaluation/runner.py`` from
mutation candidate to declared surface (5 modules total now in
``[tool.mutmut].paths_to_mutate``); (4) adding the carry-over
vendored ArkTower migration test suite; (5) running the final
coverage push that lifts default-lane coverage 93.94 % → 94.60 %
+ bumps the ``[tool.coverage.report] fail_under`` floor 93 → 94.
See ``release-notes-v0.5.5.md`` for the full closure summary +
verification commands + the 5-loop rollup table.

v0.5.4 patch (Loop 4 of v0.5.x → v0.6.0 self-improvement series):
strengthens test quality beyond pure line coverage. The
``[tool.mutmut].paths_to_mutate`` declarative surface grows from 1
module (`daemon/state.py` round-4 baseline) to 4 modules — adds
``daemon/event_log.py`` (R-011 fd-held NDJSON appender; high blast
radius), ``cli/init_cmd.py`` (S2 multi-IDE installer; idempotency
contract), and ``cli/doctor_cmd.py`` (S4 aggregate health verb;
``--json`` schema is consumer-facing). Three new edge-case test
files target previously-undertested branches the live mutmut run
would prod first: ``tests/cli/test_init_cmd_edge_cases.py`` (20
cases — auto-detect / dry-run / scope conflict / idempotency
permutations), ``tests/cli/test_doctor_cmd_edge_cases.py`` (13
cases — ``_probe_daemon`` end-to-end success path + ``--json``
schema stability + ``_roll_up`` monotonicity + literal pinning),
``tests/cli/test_popolad_cmd.py`` (23 cases — start refuses live-
PID + recovers from corrupt-PID, stop SIGTERM/SIGKILL escalation,
status JSON envelope + non-200 health). Round-2 mutation kills for
``daemon/state.py`` land in
``tests/daemon/test_state_mutation_kills.py`` (7 cases — race
window, identity preservation, atomic transitions). Plus version
bump to 0.5.4 + ``evidence/mutmut-baseline.md`` v0.5.4 section.
See ``release-notes-v0.5.4.md`` for the full closure summary +
verification commands.

v0.5.3 patch (Loop 3 of v0.5.x → v0.6.0 self-improvement series):
closes the three CI red-build items surfaced by Loop 2's
``feat(v0.5.2)`` push: (1) the bare ``from arktower.X import Y``
imports in ``tests/test_event_bus.py`` + ``tests/test_repository.py``
that the GitHub-hosted runner cannot resolve since v0.5.0 vendored
ArkTower under ``popolaloom._vendored.arktower`` (the dev VM still
has a transient ``pip install -e /home/agent/reference/ArkTower``
which masks the gap locally), (2) eleven ruff lint errors that
``ruff check src/popolaloom tests/`` flags inside the read-only
``src/popolaloom/_vendored/arktower/`` upstream snapshot — fixed by
adding ``[tool.ruff] extend-exclude = ["src/popolaloom/_vendored"]``
to mirror the existing ``[tool.coverage.run] omit`` rule, plus the
single owned-code I001 import-sort fix in
``src/popolaloom/daemon/event_bus.py``, and (3) the
``--cli-flag KEY=VAL`` adapter-passthrough docs gap that the v0.5.0
functional test (``/tmp/popolaloom-skill-functional-test.md``)
flagged as the most-needed undocumented feature.  See
``release-notes-v0.5.3.md`` for the full closure summary +
verification commands.

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
S3 canonical SKILL.md at ``src/popolaloom/skills/popola-loom/SKILL.md``
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
    assert popolaloom.__version__ == "0.8.0"


def test_both_skills_resolve_via_importlib() -> None:
    """Both Skills (canonical popola-loom + opt-in install-popola) ship in the wheel.

    Regression guard: v0.7.0 added the install-popola Skill at
    src/popolaloom/skills/install-popola/SKILL.md alongside the canonical
    popola-loom Skill (renamed from popolaloom in v0.7.1+; the dash in
    the directory name means it is wheel data, never an importable
    Python package). Both must be discoverable via importlib.resources
    (which is how popola init reads them from the wheel-bundled package
    per [tool.hatch.build.targets.wheel] packages = ["src/popolaloom"]).
    """
    from importlib.resources import files

    canonical = files("popolaloom") / "skills" / "popola-loom" / "SKILL.md"
    installer = files("popolaloom") / "skills" / "install-popola" / "SKILL.md"

    assert canonical.is_file(), "canonical popola-loom SKILL.md missing from wheel data"
    assert installer.is_file(), "install-popola SKILL.md missing from wheel data"

    canon_text = canonical.read_text()
    inst_text = installer.read_text()

    assert "name: popola-loom" in canon_text, "canonical SKILL.md frontmatter wrong"
    assert "name: install-popola" in inst_text, "install-popola SKILL.md frontmatter wrong"

    assert "version: 0.8.0" in canon_text, "canonical SKILL.md not at 0.8.0"
    assert "version: 0.8.0" in inst_text, "install-popola SKILL.md not at 0.8.0"
