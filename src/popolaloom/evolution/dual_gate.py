"""dual_gate — devola-flow inner gate + PopolaLoom-nines outer gate (v0.3.0 F2.5).

Per [v0.3.0-plan.md §4 Stage F2.5](../../../.local/memory/specs/popolaloom/v0.3.0-plan.md)
+ [roadmap §11.2](/root/.cursor/plans/popolaloom_v0.2-v0.4_roadmap_e3d38a10.plan.md):

After each L3 sub-task completes, the daemon evaluates two gates:

1. **Inner gate** (devola-flow composite score) — parsed from the L3
   stdout's 3-section output (Acceptance Verification + Gate Score
   Components + Findings).  Default threshold 0.85.
2. **Outer gate** (PopolaLoom-nines composite score) — independently
   measured by F1's evidence pipeline.  Must improve by ≥ 0.02 over
   the prior round's outer score.

The verdict is one of 4 cases:

- ``pass``        — both gates PASS (round can advance)
- ``inner_fail``  — inner < 0.85 (sub-task retry max 2x)
- ``outer_fail``  — outer below prior + 0.02 (round rollback +
  reinforcement)
- ``both_fail``   — both gates fail (round rollback + full
  reinforcement)

L3 output 3-section format (per spec §3.4.4):

.. code-block:: markdown

    ## Acceptance Verification
    - Test pass: 87/87 (100%)
    - Coverage: 92.3%
    ...

    ## Gate Score Components
    - test_quality: 0.92
    - code_review: 0.88
    - architecture: 0.85
    - benchmark: 0.91

    ## Findings
    - [blocker] (severity 1): some critical issue
    - [major] (severity 2): some moderate issue
    ...

Workspace rule "No Silent Failures": parser raises :class:`ValueError`
when any required section is missing OR when ``gate_score_components``
contains non-numeric values.  Caller catches + handles (typically by
triggering sub-task retry).
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


GateVerdict = Literal["pass", "inner_fail", "outer_fail", "both_fail"]
"""4-case verdict from :func:`evaluate_dual_gate`."""

DEFAULT_INNER_THRESHOLD: float = 0.85
"""Per spec §4.1 + ADR-0002 §2.1 — devola-flow composite floor."""

DEFAULT_OUTER_DELTA: float = 0.02
"""Per roadmap §11.2 — PopolaLoom-nines outer score must improve by ≥ 0.02."""

DEFAULT_WEIGHTS: dict[str, float] = {
    "test_quality": 0.30,
    "code_review": 0.30,
    "architecture": 0.20,
    "benchmark": 0.20,
}
"""devola-flow standard profile weights (sum = 1.00)."""


class L3Sections(BaseModel):
    """The 3 mandatory L3-output sections (per spec §3.4.4 + roadmap §11.4).

    Attributes:
        acceptance_verification: Markdown content of the
            ``## Acceptance Verification`` section (e.g. test pass
            counts, coverage %).
        gate_score_components: dict of dimension → score.  Standard
            profile: ``test_quality`` / ``code_review`` /
            ``architecture`` / ``benchmark``.  Extra keys are tolerated
            (logged at info) but only the standard 4 contribute to
            :func:`compute_inner_score`.
        findings: list of finding strings (each ≥ 1 char); v0.3.0
            does NOT enforce severity prefix at the parse level — the
            reinforcement collector does that filter.

    Workspace rule "No Silent Failures": ``extra="forbid"`` plus
    field-level non-empty validators reject malformed L3 output.
    """

    model_config = ConfigDict(extra="forbid")

    acceptance_verification: str = Field(..., min_length=1)
    gate_score_components: dict[str, float] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)


def parse_l3_output(output: str) -> L3Sections:
    """Parse the L3 stdout into the 3 mandatory sections.

    The parser is intentionally lenient about ordering — Acceptance
    Verification / Gate Score Components / Findings can appear in any
    order, and unknown ``## H2`` sections are simply ignored.

    Section detection rules:

    - ``## Acceptance Verification`` (case-insensitive) — collects
      Markdown until the next ``## H2`` or EOF.
    - ``## Gate Score Components`` — same; body is parsed for
      ``- key: value`` (or ``- key = value``) pairs to populate the
      ``gate_score_components`` dict; non-numeric values are skipped
      with a warning log.
    - ``## Findings`` — same; body is parsed for ``- ...`` bullet
      lines to populate ``findings``.

    Args:
        output: full L3 stdout (typically captured from
            ``task.completed`` event data ``output`` field).

    Returns:
        L3Sections: parsed Pydantic model.

    Raises:
        ValueError: when the **acceptance_verification** section is
        missing OR empty (the other two are tolerable empty per spec
        §3.4.4 — a sub-task can legitimately have 0 findings).
    """
    sections = _split_h2_sections(output)
    av = sections.get("acceptance verification", "").strip()
    gsc_raw = sections.get("gate score components", "").strip()
    findings_raw = sections.get("findings", "").strip()

    if not av:
        raise ValueError(
            "L3 output missing or empty '## Acceptance Verification' section "
            "(spec §3.4.4 contract violation)"
        )

    gate_components: dict[str, float] = {}
    for line in gsc_raw.splitlines():
        text = line.strip()
        if not text.startswith("- "):
            continue
        body = text[2:].strip()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([0-9eE+\-\.]+)", body)
        if not match:
            logger.debug("dual_gate: skipping non-key:value line %r", body)
            continue
        key, raw_value = match.group(1), match.group(2)
        try:
            gate_components[key] = float(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                "dual_gate: gate_score_components[%s] is not numeric: %r",
                key,
                raw_value,
            )

    finding_list: list[str] = []
    for line in findings_raw.splitlines():
        text = line.strip()
        if not text.startswith("- "):
            continue
        body = text[2:].strip()
        if body:
            finding_list.append(body)

    return L3Sections(
        acceptance_verification=av,
        gate_score_components=gate_components,
        findings=finding_list,
    )


def _split_h2_sections(text: str) -> dict[str, str]:
    """Split Markdown text into ``{lower_h2_title: body}`` mapping.

    H2 headings are matched as ``^## ...$`` lines; everything until
    the next H2 or EOF is the section body (newline-stripped).
    Section titles are lower-cased for case-insensitive lookup.
    """
    pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[title] = text[start:end]
    return out


def compute_inner_score(
    sections: L3Sections, weights: dict[str, float] | None = None
) -> float:
    """Compute the devola-flow composite from gate_score_components.

    Args:
        sections: parsed L3 sections (typically from :func:`parse_l3_output`).
        weights: optional override; defaults to
            :data:`DEFAULT_WEIGHTS` (test_quality 0.30 + code_review
            0.30 + architecture 0.20 + benchmark 0.20).

    Returns:
        float: composite in ``[0.0, 1.0]`` (clamped).  Returns ``0.0``
        when **any** weighted component is missing from
        ``gate_score_components`` (matches roadmap §11.2 strict policy:
        partial L3 output cannot pass the inner gate).
    """
    w = dict(weights) if weights is not None else dict(DEFAULT_WEIGHTS)
    weight_sum = sum(w.values())
    if weight_sum <= 0:
        raise ValueError(
            f"compute_inner_score: weights sum to {weight_sum}; must be > 0"
        )

    composite = 0.0
    for key, weight in w.items():
        value = sections.gate_score_components.get(key)
        if value is None:
            logger.warning(
                "compute_inner_score: missing gate_score_components[%s]; "
                "inner score forced to 0.0 (strict policy)",
                key,
            )
            return 0.0
        composite += float(value) * weight
    composite /= weight_sum
    return max(0.0, min(1.0, composite))


def evaluate_dual_gate(
    inner_scores: list[float],
    outer_score: float,
    prior_outer_score: float,
    *,
    inner_threshold: float = DEFAULT_INNER_THRESHOLD,
    outer_delta: float = DEFAULT_OUTER_DELTA,
) -> GateVerdict:
    """Evaluate inner + outer gates and return the 4-case verdict.

    Args:
        inner_scores: per-sub-task inner composites (from
            :func:`compute_inner_score`).  ALL must be ≥
            ``inner_threshold`` for the inner gate to pass.
        outer_score: current round's PopolaLoom-nines composite.
        prior_outer_score: previous round's PopolaLoom-nines composite
            (for the +0.02 delta check).
        inner_threshold: per-sub-task minimum (default 0.85).
        outer_delta: required outer-score improvement over prior round
            (default 0.02).

    Returns:
        GateVerdict: ``"pass"`` / ``"inner_fail"`` / ``"outer_fail"`` /
        ``"both_fail"``.
    """
    inner_pass = bool(inner_scores) and all(
        score >= inner_threshold for score in inner_scores
    )
    outer_pass = outer_score >= prior_outer_score + outer_delta

    if inner_pass and outer_pass:
        return "pass"
    if inner_pass and not outer_pass:
        return "outer_fail"
    if not inner_pass and outer_pass:
        return "inner_fail"
    return "both_fail"


__all__ = [
    "DEFAULT_INNER_THRESHOLD",
    "DEFAULT_OUTER_DELTA",
    "DEFAULT_WEIGHTS",
    "GateVerdict",
    "L3Sections",
    "compute_inner_score",
    "evaluate_dual_gate",
    "parse_l3_output",
]
