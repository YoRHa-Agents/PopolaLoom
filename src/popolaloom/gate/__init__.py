"""Auto-merge gate package — v0.3.0 Stage F3.

This subpackage implements the **5-condition AND gate** that gates
``automerge`` PRs into ``main``. Per spec §7.3 + roadmap §4.2 Stage F3
+ v0.3.0-plan §4 Stage F3.

The 5 conditions (must ALL pass):

1. DevolaFlow inner-gate composite ≥ ``gate_thresholds.devolaflow_composite``
   (default 0.85).
2. PopolaLoom-nines outer-gate current ≥ prior + ``gate_thresholds.nines_delta``
   (default +0.02; means quality must strictly improve).
3. ``blocker_max`` (default 0) — the L3 sub-task ``Findings`` section must
   carry zero blocker-severity items.
4. ``test_pass`` AND coverage ≥ ``gate_thresholds.coverage_min`` (default
   90.0). Coverage is reported by ``--cov-fail-under`` in CI.
5. PR diff ``paths`` whitelist (``required_paths.allowed``) ∩
   ¬ blacklist (``required_paths.blocked``).

The gate is invoked from ``.github/workflows/automerge.yml`` after the
test suite passes. The workflow then calls ``gh pr merge --auto --squash``
only when this gate returns ``verdict="pass"``.

Public API (re-exported from the package):

- :class:`AutomergeConfig` — Pydantic v2 schema for ``.workflow/automerge.yaml``.
- :class:`GateThresholds` — sub-model with the 5 numeric thresholds.
- :class:`PathPolicy` — sub-model with ``allowed`` / ``blocked`` glob lists.
- :class:`AutomergeResult` — verdict + per-condition status.
- :func:`evaluate_automerge` — core decision function.
- :func:`load_config` — read + validate YAML config from a path.

See also: ``.github/workflows/automerge.yml`` for CI wiring,
``tests/test_automerge_gate.py`` for the ≥6-case test suite.
"""

from __future__ import annotations

from popolaloom.gate.automerge import (
    AutomergeConfig,
    AutomergeResult,
    ConditionStatus,
    GateThresholds,
    PathPolicy,
    evaluate_automerge,
    load_config,
)

__all__ = [
    "AutomergeConfig",
    "AutomergeResult",
    "ConditionStatus",
    "GateThresholds",
    "PathPolicy",
    "evaluate_automerge",
    "load_config",
]
