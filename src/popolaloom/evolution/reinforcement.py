"""reinforcement — top-5 finding promotion + Markdown rendering (v0.3.0 F2.5).

Per [v0.3.0-plan.md D3.4](../../../.local/memory/specs/popolaloom/v0.3.0-plan.md)
+ [roadmap §11.3](/root/.cursor/plans/popolaloom_v0.2-v0.4_roadmap_e3d38a10.plan.md):

After each evolution round, the L3 finding list (parsed from the
``## Findings`` section of the L3 stdout) is filtered + ranked by
severity then top-5 are persisted to ``~/.popola/round-N-evidence.md``
for the next round's prepend.

Severity grades (per spec §3.4.4):

- ``blocker``  — must fix; round FAILS without it
- ``critical`` — must fix; reduces inner-gate score significantly
- ``major``    — should fix; reduces inner-gate score moderately
- ``minor``    — should fix; cosmetic / style only

Render format (D3.4 recommendation):

.. code-block:: markdown

    ## Reinforcement Rules (from round N-1) - MUST fix:
    - [blocker] (round N-1): finding text
    - [critical] (round N-1): finding text
    ... (top-5 only)

Workspace rule "No Silent Failures": invalid severity input raises
:class:`ValueError`; the renderer truncates over-long findings (> 200
chars) but logs a warning so operators see the truncation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Severity = Literal["minor", "major", "critical", "blocker"]
"""Severity grade for an L3 finding."""

SEVERITY_RANK: dict[str, int] = {
    "blocker": 4,
    "critical": 3,
    "major": 2,
    "minor": 1,
}
"""Numeric ranks for sorting; higher = more severe."""

MAX_FINDINGS: int = 5
"""Top-5 finding cap (per spec §11.2 + WorkflowContext.MAX_REINFORCEMENT_RULES)."""

MAX_FINDING_LENGTH: int = 200
"""Per finding char cap; longer findings are truncated with ellipsis."""


@dataclass(frozen=True)
class Finding:
    """One L3 finding with severity grade.

    Attributes:
        severity: one of ``"minor"`` / ``"major"`` / ``"critical"`` / ``"blocker"``.
        text: the actual finding description (truncated at
            :data:`MAX_FINDING_LENGTH` when rendered).
        round_num: the round in which this finding was emitted (used
            in render prefix ``"(round N-1)"``).
        category: optional category tag (e.g. ``"test_quality"`` /
            ``"architecture"``); not currently rendered but stored for
            future use.
    """

    severity: str
    text: str
    round_num: int = 0
    category: str = ""


def _is_severity_at_least(actual: str, minimum: str) -> bool:
    """Return ``True`` iff ``actual`` severity ≥ ``minimum`` severity."""
    actual_rank = SEVERITY_RANK.get(actual.lower())
    min_rank = SEVERITY_RANK.get(minimum.lower())
    if actual_rank is None or min_rank is None:
        raise ValueError(
            f"unknown severity {actual!r} or {minimum!r}; expected one of "
            f"{sorted(SEVERITY_RANK)}"
        )
    return actual_rank >= min_rank


def _truncate(text: str) -> str:
    """Truncate text at :data:`MAX_FINDING_LENGTH` chars with ellipsis suffix."""
    if len(text) <= MAX_FINDING_LENGTH:
        return text
    logger.warning(
        "reinforcement: truncating finding (len=%d > %d)",
        len(text),
        MAX_FINDING_LENGTH,
    )
    return text[: MAX_FINDING_LENGTH - 3] + "..."


def _normalise_finding(item: Any) -> Finding | None:
    """Coerce a heterogeneous finding entry into :class:`Finding` or ``None``.

    Accepts:

    - :class:`Finding` instance — returned as-is.
    - dict with ``severity`` + ``text`` keys (round_num / category optional).
    - str — assumed ``minor`` severity, no round info.

    Returns ``None`` for unrecognised shapes (logged at warning).
    """
    if isinstance(item, Finding):
        return item
    if isinstance(item, dict):
        severity = item.get("severity", "minor")
        text = item.get("text") or item.get("finding") or item.get("description")
        if not text:
            logger.warning("reinforcement: dict finding missing text/finding key: %r", item)
            return None
        return Finding(
            severity=str(severity).lower(),
            text=str(text),
            round_num=int(item.get("round_num") or 0),
            category=str(item.get("category") or ""),
        )
    if isinstance(item, str):
        return Finding(severity="minor", text=item)
    logger.warning("reinforcement: unrecognised finding type: %r", type(item))
    return None


class ReinforcementInjector:
    """Stateful collector + renderer for round-to-round reinforcement.

    Each :meth:`collect_findings_from_round` call merges new findings
    into the internal pool (deduplicated by text); :meth:`render_top_n`
    + :meth:`render_reinforcement_section` produce Markdown output.

    The injector is intentionally lightweight (in-memory only); the
    persistence layer (``~/.popola/round-N-evidence.md``) is the
    daemon's responsibility (F2.5.4 dispatch RPC reads files when
    they exist).
    """

    def __init__(self) -> None:
        self._findings: list[Finding] = []
        self._seen_texts: set[str] = set()

    def collect_findings_from_round(
        self,
        round_evidence: dict[str, Any] | list[Any],
        severity_min: str = "major",
    ) -> list[str]:
        """Pull findings ≥ ``severity_min`` from ``round_evidence``.

        Args:
            round_evidence: either:

                - dict containing ``"findings"`` key (list of
                  Finding/dict/str), OR
                - list of Finding/dict/str entries directly.
            severity_min: minimum severity to include.  Default
                ``"major"`` matches roadmap §11.3 ("≥ major" filter).

        Returns:
            list[str]: top-5 rendered finding strings (no ``"- "``
            prefix; suitable for direct list ingestion).  When fewer
            than 5 findings meet the threshold, returns all of them.

        Raises:
            ValueError: when ``severity_min`` is not a known severity.
        """
        if severity_min not in SEVERITY_RANK:
            raise ValueError(
                f"severity_min must be one of {sorted(SEVERITY_RANK)}; got {severity_min!r}"
            )

        raw_findings: list[Any] = []
        if isinstance(round_evidence, dict):
            raw_findings = list(round_evidence.get("findings", []))
        elif isinstance(round_evidence, list):
            raw_findings = list(round_evidence)
        else:
            raise ValueError(
                f"round_evidence must be dict or list; got {type(round_evidence)}"
            )

        coerced: list[Finding] = []
        for raw in raw_findings:
            f = _normalise_finding(raw)
            if f is None:
                continue
            try:
                if not _is_severity_at_least(f.severity, severity_min):
                    continue
            except ValueError:
                logger.warning(
                    "reinforcement: skipping finding with unknown severity %r",
                    f.severity,
                )
                continue
            if f.text in self._seen_texts:
                continue
            self._seen_texts.add(f.text)
            self._findings.append(f)
            coerced.append(f)

        ordered = sorted(
            coerced,
            key=lambda f: (-SEVERITY_RANK.get(f.severity, 0), -f.round_num, f.text),
        )
        return [_truncate(f.text) for f in ordered[:MAX_FINDINGS]]

    def render_reinforcement_section(
        self,
        findings: list[str] | list[Finding] | None = None,
        round_num: int = 0,
    ) -> str:
        """Render a Markdown reinforcement section for the next-round prompt.

        Args:
            findings: optional override list; defaults to internal
                pool (top-5 by severity + round_num).  Strings are
                rendered without prefix; :class:`Finding` instances
                get ``[severity] (round N)`` prefix per D3.4.
            round_num: current round number (used in section header
                ``"(from round N-1)"``).

        Returns:
            str: Markdown section (with trailing newline) suitable
            for direct prepend by :func:`skill_inject.prepend_workflow_context`.
            When findings is empty, returns empty string (caller
            should NOT prepend an empty section).
        """
        if findings is None:
            ranked = sorted(
                self._findings,
                key=lambda f: (-SEVERITY_RANK.get(f.severity, 0), -f.round_num, f.text),
            )
            findings_to_render: list[Finding | str] = list(ranked[:MAX_FINDINGS])
        else:
            findings_to_render = list(findings[:MAX_FINDINGS])

        if not findings_to_render:
            return ""

        prior = round_num - 1 if round_num >= 1 else round_num
        header = (
            f"## Reinforcement Rules (from round {prior}) - MUST fix:"
            if prior > 0
            else "## Reinforcement Rules - MUST fix:"
        )
        lines: list[str] = [header]
        for entry in findings_to_render:
            if isinstance(entry, Finding):
                line = (
                    f"- [{entry.severity}] (round {entry.round_num}): "
                    f"{_truncate(entry.text)}"
                )
            else:
                text_str = str(entry).strip()
                if text_str.startswith("- "):
                    text_str = text_str[2:]
                line = f"- {_truncate(text_str)}"
            lines.append(line)
        return "\n".join(lines) + "\n"


def render_reinforcement_section(
    findings: list[str] | list[Finding],
    round_num: int = 0,
) -> str:
    """Module-level convenience for one-shot Markdown rendering.

    Equivalent to ``ReinforcementInjector().render_reinforcement_section(...)``
    without persisting state across calls.  Used by the daemon
    dispatch RPC when round-N findings are read from disk.
    """
    injector = ReinforcementInjector()
    return injector.render_reinforcement_section(findings, round_num=round_num)


def collect_findings_from_round(
    round_evidence: dict[str, Any] | list[Any],
    severity_min: str = "major",
) -> list[str]:
    """Module-level convenience for one-shot top-5 finding extraction."""
    injector = ReinforcementInjector()
    return injector.collect_findings_from_round(round_evidence, severity_min=severity_min)


__all__ = [
    "MAX_FINDINGS",
    "MAX_FINDING_LENGTH",
    "SEVERITY_RANK",
    "Finding",
    "ReinforcementInjector",
    "Severity",
    "collect_findings_from_round",
    "render_reinforcement_section",
]
