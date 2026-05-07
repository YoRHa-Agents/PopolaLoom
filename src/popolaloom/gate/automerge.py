"""Auto-merge gate core — 5 AND conditions (v0.3.0 Stage F3).

Per spec §7.3 + v0.3.0-plan §4 Stage F3 + roadmap §4.2 Stage F3.

The 5 AND conditions (every condition must be ``"pass"`` for the
overall verdict to be ``"pass"``):

1. ``devolaflow_composite`` — inner-gate composite_score ≥ threshold
   (default ``0.85``). The L3 sub-task self-rates 4 sub-scores per
   roadmap §11.4 (test_quality + code_review + architecture +
   benchmark, weights 0.30/0.30/0.20/0.20); the inner-gate composite
   is reported in the L3 ``## Gate Score Components`` section.
2. ``nines_delta`` — outer-gate PopolaLoom-nines composite delta
   (current − prior) ≥ threshold (default ``+0.02``). Strict
   improvement: quality must measurably go up between rounds.
3. ``blocker_max`` — count of blocker-severity findings ≤ threshold
   (default ``0``). A single blocker is enough to fail the gate.
4. ``test_pass + coverage`` — PR's pytest run was green AND coverage
   ≥ threshold (default ``90.0``). CI is responsible for running
   pytest with ``--cov-fail-under`` before invoking this gate.
5. ``paths`` — every changed file matches at least one
   ``required_paths.allowed`` glob AND no changed file matches any
   ``required_paths.blocked`` glob. Blocked wins over allowed (a
   file caught by both is rejected — defense in depth).

The gate honours workspace rule "No Silent Failures": when evidence
is missing (e.g. ``nines_delta`` cannot be computed because no prior
score exists) the corresponding condition is marked ``"fail"`` with
an explicit reason, never silently skipped.

CLI / module entry:

    python -m popolaloom.gate.automerge \\
        --config .workflow/automerge.yaml \\
        --pr-paths "src/popolaloom/foo.py,tests/test_foo.py" \\
        --evidence gate_evidence.json \\
        --output gate_result.json

The workflow uses the ``$?`` exit code (0 on pass, non-zero on fail)
to decide whether to invoke ``gh pr merge``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


# ── thresholds ───────────────────────────────────────────────────────────


class GateThresholds(BaseModel):
    """Numeric thresholds for the 5 AND conditions.

    All defaults match v0.3.0-plan §4 Stage F3 schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    devolaflow_composite: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Inner-gate composite_score floor (default 0.85).",
    )
    nines_delta: float = Field(
        default=0.02,
        ge=-1.0,
        le=1.0,
        description=(
            "Required improvement in PopolaLoom-nines composite vs prior round "
            "(default +0.02; strict improvement)."
        ),
    )
    blocker_max: int = Field(
        default=0,
        ge=0,
        description="Maximum allowed blocker-severity findings (default 0).",
    )
    test_pass: bool = Field(
        default=True,
        description=(
            "When True, evidence MUST report ``test_pass=true`` "
            "(CI green); set False for dry-run / debug only."
        ),
    )
    coverage_min: float = Field(
        default=90.0,
        ge=0.0,
        le=100.0,
        description="Minimum line coverage % (default 90.0; matches fail_under).",
    )


class PathPolicy(BaseModel):
    """Glob-based path whitelist + blacklist.

    Patterns use ``fnmatch`` semantics (``*`` matches one segment, ``**``
    matches zero or more segments). Empty ``allowed`` means "no path is
    allowed" (deliberately strict — operator must opt in to which trees
    can auto-merge).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: list[str] = Field(
        default_factory=list,
        description="Glob patterns of paths PRs may modify.",
    )
    blocked: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns of paths PRs must NOT modify (overrides allowed; "
            "any match here is an immediate fail)."
        ),
    )

    @field_validator("allowed", "blocked")
    @classmethod
    def _no_blank_globs(cls, v: list[str]) -> list[str]:
        for i, pattern in enumerate(v):
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(
                    f"path glob entry {i} must be a non-empty string"
                )
        return v


class AutomergeConfig(BaseModel):
    """Top-level config schema for ``.workflow/automerge.yaml``.

    Loaded by :func:`load_config`; consumed by :func:`evaluate_automerge`.
    """

    model_config = ConfigDict(extra="forbid")

    gate_thresholds: GateThresholds = Field(default_factory=GateThresholds)
    required_paths: PathPolicy = Field(default_factory=PathPolicy)


# ── result ───────────────────────────────────────────────────────────────


Verdict = Literal["pass", "fail"]
"""Two-valued gate verdict.

The CI workflow translates ``"pass"`` → exit code 0 → ``gh pr merge``,
``"fail"`` → exit code 2 (per :func:`main`)."""


class ConditionStatus(BaseModel):
    """Per-condition result row in :class:`AutomergeResult`.

    Attributes:
        name: short identifier (e.g. ``"devolaflow_composite"``).
        verdict: ``"pass"`` or ``"fail"``.
        observed: stringified observed value ("0.87", "false", etc).
        threshold: stringified threshold/expected value for context.
        reason: short human-readable reason (especially useful on fail).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    verdict: Verdict
    observed: str
    threshold: str
    reason: str


class AutomergeResult(BaseModel):
    """Output of :func:`evaluate_automerge`.

    Attributes:
        verdict: overall ``"pass"`` (every condition passed) or ``"fail"``.
        reason: 1-line summary (e.g. ``"all 5 conditions pass"`` or
                ``"3/5 conditions failed: nines_delta, blocker_max, paths"``).
        conditions: ordered list of all 5 :class:`ConditionStatus` rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Verdict
    reason: str
    conditions: list[ConditionStatus]

    def failures(self) -> list[ConditionStatus]:
        """Return only the conditions that failed (helper for logging)."""
        return [c for c in self.conditions if c.verdict == "fail"]


# ── evaluation logic ─────────────────────────────────────────────────────


_CONDITION_ORDER: tuple[str, ...] = (
    "devolaflow_composite",
    "nines_delta",
    "blocker_max",
    "test_pass",
    "paths",
)
"""Stable display order of the 5 AND conditions (matches spec §7.3)."""


def _check_devolaflow_composite(
    config: AutomergeConfig, evidence: dict[str, Any]
) -> ConditionStatus:
    """Condition 1: inner-gate composite ≥ threshold."""
    threshold = config.gate_thresholds.devolaflow_composite
    raw = evidence.get("devolaflow_composite")
    if raw is None:
        return ConditionStatus(
            name="devolaflow_composite",
            verdict="fail",
            observed="missing",
            threshold=f"{threshold:.4f}",
            reason="evidence.devolaflow_composite is required",
        )
    try:
        observed = float(raw)
    except (TypeError, ValueError):
        return ConditionStatus(
            name="devolaflow_composite",
            verdict="fail",
            observed=str(raw),
            threshold=f"{threshold:.4f}",
            reason="evidence.devolaflow_composite must be numeric",
        )
    verdict: Verdict = "pass" if observed >= threshold else "fail"
    return ConditionStatus(
        name="devolaflow_composite",
        verdict=verdict,
        observed=f"{observed:.4f}",
        threshold=f"{threshold:.4f}",
        reason=(
            "inner gate composite passes threshold"
            if verdict == "pass"
            else f"composite {observed:.4f} below threshold {threshold:.4f}"
        ),
    )


def _check_nines_delta(
    config: AutomergeConfig, evidence: dict[str, Any]
) -> ConditionStatus:
    """Condition 2: PopolaLoom-nines current ≥ prior + delta."""
    delta_required = config.gate_thresholds.nines_delta
    current = evidence.get("nines_current")
    prior = evidence.get("nines_prior")
    if current is None or prior is None:
        return ConditionStatus(
            name="nines_delta",
            verdict="fail",
            observed=f"current={current!r} prior={prior!r}",
            threshold=f"prior + {delta_required:+.4f}",
            reason="evidence.nines_current AND evidence.nines_prior required",
        )
    try:
        current_f = float(current)
        prior_f = float(prior)
    except (TypeError, ValueError):
        return ConditionStatus(
            name="nines_delta",
            verdict="fail",
            observed=f"current={current!r} prior={prior!r}",
            threshold=f"prior + {delta_required:+.4f}",
            reason="nines_current / nines_prior must be numeric",
        )
    delta_observed = current_f - prior_f
    verdict: Verdict = "pass" if delta_observed >= delta_required else "fail"
    return ConditionStatus(
        name="nines_delta",
        verdict=verdict,
        observed=f"{delta_observed:+.4f} (current={current_f:.4f}, prior={prior_f:.4f})",
        threshold=f"≥ {delta_required:+.4f}",
        reason=(
            "outer gate nines improved sufficiently"
            if verdict == "pass"
            else f"nines delta {delta_observed:+.4f} below required {delta_required:+.4f}"
        ),
    )


def _check_blocker_max(
    config: AutomergeConfig, evidence: dict[str, Any]
) -> ConditionStatus:
    """Condition 3: blocker count ≤ threshold."""
    threshold = config.gate_thresholds.blocker_max
    raw = evidence.get("blocker_count", 0)
    try:
        observed = int(raw)
    except (TypeError, ValueError):
        return ConditionStatus(
            name="blocker_max",
            verdict="fail",
            observed=str(raw),
            threshold=str(threshold),
            reason="evidence.blocker_count must be integer",
        )
    verdict: Verdict = "pass" if observed <= threshold else "fail"
    return ConditionStatus(
        name="blocker_max",
        verdict=verdict,
        observed=str(observed),
        threshold=str(threshold),
        reason=(
            "blocker findings within budget"
            if verdict == "pass"
            else f"{observed} blocker(s) found, max allowed {threshold}"
        ),
    )


def _check_test_pass_and_coverage(
    config: AutomergeConfig, evidence: dict[str, Any]
) -> ConditionStatus:
    """Condition 4: tests green AND coverage above floor."""
    cov_threshold = config.gate_thresholds.coverage_min
    require_pass = config.gate_thresholds.test_pass
    test_pass_raw = evidence.get("test_pass")
    coverage_raw = evidence.get("coverage")

    if test_pass_raw is None:
        return ConditionStatus(
            name="test_pass",
            verdict="fail",
            observed="test_pass=missing",
            threshold=f"true & coverage ≥ {cov_threshold}",
            reason="evidence.test_pass required",
        )
    if coverage_raw is None:
        return ConditionStatus(
            name="test_pass",
            verdict="fail",
            observed=f"test_pass={test_pass_raw!r} coverage=missing",
            threshold=f"true & coverage ≥ {cov_threshold}",
            reason="evidence.coverage required",
        )

    try:
        coverage = float(coverage_raw)
    except (TypeError, ValueError):
        return ConditionStatus(
            name="test_pass",
            verdict="fail",
            observed=f"coverage={coverage_raw!r}",
            threshold=f"true & coverage ≥ {cov_threshold}",
            reason="evidence.coverage must be numeric (% value)",
        )

    test_passed = bool(test_pass_raw)
    coverage_ok = coverage >= cov_threshold
    overall = (not require_pass or test_passed) and coverage_ok
    verdict: Verdict = "pass" if overall else "fail"

    if verdict == "pass":
        reason = "tests green and coverage above floor"
    else:
        parts: list[str] = []
        if require_pass and not test_passed:
            parts.append("tests not green")
        if not coverage_ok:
            parts.append(f"coverage {coverage:.2f} < {cov_threshold}")
        reason = "; ".join(parts) or "test_pass condition failed"

    return ConditionStatus(
        name="test_pass",
        verdict=verdict,
        observed=f"test_pass={test_passed} coverage={coverage:.2f}",
        threshold=(
            f"test_pass={require_pass} coverage ≥ {cov_threshold}"
        ),
        reason=reason,
    )


def _glob_to_regex(pattern: str) -> str:
    """Translate a git-style pathspec glob into a regex.

    Subset of git pathspecs:

    - ``**/`` matches zero or more path segments (incl. separator)
    - ``**``  matches any sequence of characters (incl. ``/``)
    - ``*``   matches any sequence except ``/``
    - ``?``   matches a single non-``/`` character
    - all other chars matched literally (regex-escaped)
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return "^" + "".join(parts) + "$"


def _path_matches_globs(path: str, globs: list[str]) -> bool:
    """Return True iff ``path`` matches any of the ``globs``.

    Supports git-pathspec ``**`` semantics (recursive directory match).
    Path separators are honoured (``a/b`` does not match ``a*b``).
    """
    return any(re.match(_glob_to_regex(pattern), path) for pattern in globs)


def _check_paths(
    config: AutomergeConfig, evidence: dict[str, Any]
) -> ConditionStatus:
    """Condition 5: every changed path is allowed and none is blocked."""
    raw_paths = evidence.get("pr_paths", [])
    if isinstance(raw_paths, str):
        paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
    else:
        paths = [str(p) for p in raw_paths if str(p).strip()]

    if not paths:
        return ConditionStatus(
            name="paths",
            verdict="fail",
            observed="(no paths)",
            threshold="≥1 changed path required",
            reason="evidence.pr_paths must list at least one changed file",
        )

    allowed = config.required_paths.allowed
    blocked = config.required_paths.blocked

    blocked_hits = [p for p in paths if _path_matches_globs(p, blocked)]
    if blocked_hits:
        return ConditionStatus(
            name="paths",
            verdict="fail",
            observed=f"blocked={blocked_hits}",
            threshold=f"none in {blocked}",
            reason=(
                f"{len(blocked_hits)} path(s) hit blocked globs: "
                f"{blocked_hits[:3]}{'...' if len(blocked_hits) > 3 else ''}"
            ),
        )

    if not allowed:
        return ConditionStatus(
            name="paths",
            verdict="fail",
            observed=f"{len(paths)} path(s) but no allowed globs configured",
            threshold="any path matches allowed globs",
            reason="required_paths.allowed is empty (no path can pass)",
        )

    not_allowed = [p for p in paths if not _path_matches_globs(p, allowed)]
    if not_allowed:
        return ConditionStatus(
            name="paths",
            verdict="fail",
            observed=f"unmatched={not_allowed[:3]}{'...' if len(not_allowed) > 3 else ''}",
            threshold=f"all paths match {allowed}",
            reason=(
                f"{len(not_allowed)} path(s) not in allowed globs"
            ),
        )

    return ConditionStatus(
        name="paths",
        verdict="pass",
        observed=f"{len(paths)} path(s) all allowed, 0 blocked",
        threshold="all in allowed, none in blocked",
        reason="path policy satisfied",
    )


_CHECKER_BY_NAME: dict[str, Any] = {
    "devolaflow_composite": _check_devolaflow_composite,
    "nines_delta": _check_nines_delta,
    "blocker_max": _check_blocker_max,
    "test_pass": _check_test_pass_and_coverage,
    "paths": _check_paths,
}


def evaluate_automerge(
    config: AutomergeConfig, evidence: dict[str, Any]
) -> AutomergeResult:
    """Apply all 5 AND conditions; return :class:`AutomergeResult`.

    Args:
        config: parsed :class:`AutomergeConfig`.
        evidence: dict supplying observed values:

            - ``devolaflow_composite`` (float, 0..1)
            - ``nines_current`` + ``nines_prior`` (float each)
            - ``blocker_count`` (int, default 0)
            - ``test_pass`` (bool) + ``coverage`` (float, %)
            - ``pr_paths`` (list[str] or comma-separated string)

    Returns:
        AutomergeResult: overall verdict + per-condition rows.
        ``verdict="pass"`` ONLY when every condition passes.
    """
    rows: list[ConditionStatus] = []
    for name in _CONDITION_ORDER:
        check = _CHECKER_BY_NAME[name]
        try:
            row = check(config, evidence)
        except Exception as exc:
            logger.exception("automerge: %s checker raised", name)
            row = ConditionStatus(
                name=name,
                verdict="fail",
                observed="exception",
                threshold="(checker raised)",
                reason=f"{type(exc).__name__}: {exc}",
            )
        rows.append(row)

    failed = [r for r in rows if r.verdict == "fail"]
    if not failed:
        verdict: Verdict = "pass"
        reason = "all 5 conditions pass"
    else:
        verdict = "fail"
        reason = (
            f"{len(failed)}/{len(rows)} conditions failed: "
            + ", ".join(r.name for r in failed)
        )

    return AutomergeResult(verdict=verdict, reason=reason, conditions=rows)


# ── config loader ────────────────────────────────────────────────────────


def load_config(path: Path) -> AutomergeConfig:
    """Read + validate a YAML config file.

    Uses ``yaml.safe_load`` so embedded code/objects are rejected.

    Args:
        path: path to ``.workflow/automerge.yaml``.

    Returns:
        AutomergeConfig: validated config.

    Raises:
        FileNotFoundError: when ``path`` does not exist.
        ValueError: when the YAML is malformed or fails Pydantic validation.
    """
    if not path.is_file():
        raise FileNotFoundError(f"automerge config not found: {path}")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load .workflow/automerge.yaml; "
            "ensure it is installed (it ships with uvicorn / fastapi extras)."
        ) from exc
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"automerge config root must be a mapping; got {type(raw).__name__}"
        )
    try:
        return AutomergeConfig.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"automerge config validation failed: {exc}") from exc


def parse_evidence(args: argparse.Namespace) -> dict[str, Any]:
    """Build the evidence dict from CLI args + optional JSON evidence file.

    CLI flags take precedence over the evidence JSON so a CI workflow can
    inject up-to-the-minute paths via ``--pr-paths`` while leaving the
    pytest / nines metrics in the JSON file produced by earlier steps.
    """
    evidence: dict[str, Any] = {}
    if args.evidence:
        evidence_path = Path(args.evidence)
        if not evidence_path.is_file():
            raise FileNotFoundError(f"evidence JSON not found: {evidence_path}")
        loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{evidence_path} must contain a JSON object")
        evidence.update(loaded)
    if args.pr_paths is not None:
        evidence["pr_paths"] = args.pr_paths
    if args.devolaflow_composite is not None:
        evidence["devolaflow_composite"] = args.devolaflow_composite
    if args.nines_current is not None:
        evidence["nines_current"] = args.nines_current
    if args.nines_prior is not None:
        evidence["nines_prior"] = args.nines_prior
    if args.blocker_count is not None:
        evidence["blocker_count"] = args.blocker_count
    if args.test_pass is not None:
        evidence["test_pass"] = args.test_pass
    if args.coverage is not None:
        evidence["coverage"] = args.coverage
    return evidence


# ── CLI entry ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """``python -m popolaloom.gate.automerge`` entry.

    Args:
        argv: optional override for testing (None → ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` on PASS, ``2`` on FAIL, ``1`` on error.
    """
    parser = argparse.ArgumentParser(
        prog="popolaloom-gate-automerge",
        description="PopolaLoom auto-merge gate — 5 AND conditions (v0.3.0 F3).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to .workflow/automerge.yaml.",
    )
    parser.add_argument(
        "--pr-paths",
        type=str,
        default=None,
        help="Comma-separated list of changed paths in the PR.",
    )
    parser.add_argument(
        "--evidence",
        type=str,
        default=None,
        help="Path to JSON file pre-supplying evidence (composite, nines, etc).",
    )
    parser.add_argument(
        "--devolaflow-composite", type=float, default=None,
        help="Inner-gate composite_score override.",
    )
    parser.add_argument(
        "--nines-current", type=float, default=None,
        help="Outer-gate PopolaLoom-nines current composite.",
    )
    parser.add_argument(
        "--nines-prior", type=float, default=None,
        help="Outer-gate PopolaLoom-nines prior composite.",
    )
    parser.add_argument(
        "--blocker-count", type=int, default=None,
        help="Number of blocker-severity findings.",
    )
    parser.add_argument(
        "--test-pass",
        type=lambda v: v.lower() in {"true", "1", "yes", "y"},
        default=None,
        help="Whether the test suite passed (true/false).",
    )
    parser.add_argument(
        "--coverage", type=float, default=None,
        help="Coverage percentage (0-100).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the JSON-serialised result.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        evidence = parse_evidence(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"automerge gate: {exc}", file=sys.stderr)
        return 1

    result = evaluate_automerge(config, evidence)

    serialised = result.model_dump(mode="json")
    text = json.dumps(serialised, indent=2)
    print(text)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")

    return 0 if result.verdict == "pass" else 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
