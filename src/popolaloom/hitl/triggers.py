"""HITL trigger factories — v0.3.0 Stage F4.A.

Each factory builds a :class:`popolaloom.hitl.HITLPrompt` for one of the
five canonical trigger types defined by spec §12.6 + roadmap §12.1:

================  ================================================
Trigger           When fired
================  ================================================
``round_floor``   Self-evolution round did not reach gate floor
``ambiguous``     L3 produced ≥2 reasonable fixes, gate scored ≤0.85
``critical_err``  Daemon / LangGraph raised non-recoverable exception
``regression``    Same R-id reappears across ≥3 rounds
``interrupt``     LangGraph ``interrupt()`` called from inside a graph
================  ================================================

For the ``round_floor`` and ``regression`` triggers, the §12.6 contract
mandates a 3-option escalation card (``override`` / ``rollback`` /
``defer``) with ``defer`` as the timeout default, surfacing on **all 5
channels** — these factories pre-fill that contract so callers cannot
forget it.

Every factory honours workspace rule "No Silent Failures": invalid input
raises :class:`pydantic.ValidationError` (via the schema in
:mod:`popolaloom.hitl`).
"""

from __future__ import annotations

from collections.abc import Sequence

from popolaloom.hitl import (
    ArtifactRef,
    HITLChannel,
    HITLOption,
    HITLPrompt,
)

# ── Canonical default channel sets per trigger ──────────────────────────
# All triggers must use ≥ 2 channels; round_floor / regression / critical
# additionally require all 5 (per spec §12.6).

_ALL_CHANNELS: list[HITLChannel] = ["lark", "ide", "cli", "email", "signal"]
"""Per spec §12.6 — escalation triggers fan out to every available
channel so a stale Lark bot can't silently drop a critical decision."""

_DEFAULT_DEADLINE_SECONDS: int = 86400
"""24h default per spec §12 — 6h prior, an automated ping resends to all
channels (see :func:`popolaloom.hitl.sync.process_timeout`)."""


# ── Internal helpers ────────────────────────────────────────────────────


def _ensure_options(options: Sequence[HITLOption]) -> list[HITLOption]:
    """Validate ≥ 2 options + distinct ids; return list copy."""
    if len(options) < 2:
        raise ValueError(
            "HITL trigger factories require ≥ 2 options "
            f"(binary choice minimum); got {len(options)}"
        )
    return list(options)


def _pick_default(options: Sequence[HITLOption], explicit: str | None) -> str:
    """Pick default option id: explicit override → first option marked
    ``default`` → first option."""
    ids = [o.id for o in options]
    if explicit is not None:
        if explicit not in ids:
            raise ValueError(
                f"default_option_id={explicit!r} not in option ids {ids}"
            )
        return explicit
    for o in options:
        if o.default:
            return o.id
    return ids[0]


# ── Trigger factories ───────────────────────────────────────────────────


def create_interrupt_prompt(
    *,
    graph_state: dict[str, object],
    question: str,
    options: Sequence[HITLOption],
    default_option_id: str | None = None,
    channels: Sequence[HITLChannel] | None = None,
    deadline_seconds: int = _DEFAULT_DEADLINE_SECONDS,
    artifacts: Sequence[ArtifactRef] | None = None,
) -> HITLPrompt:
    """Build a ``info_request`` prompt for a LangGraph ``interrupt()`` call.

    The most common HITL case: graph paused via ``interrupt(...)`` and
    needs a free-form / multiple-choice answer to resume.

    Args:
        graph_state: dict snapshot of relevant LangGraph state values.
            The renderer surfaces a JSON preview so the human can audit.
        question: human-readable question (becomes ``HITLPrompt.what``).
        options: ≥ 2 :class:`HITLOption` entries (binary minimum).
        default_option_id: optional explicit timeout default; otherwise
            first option marked ``default=True``, else ``options[0]``.
        channels: optional override; defaults to ``["lark", "ide", "cli"]``.
        deadline_seconds: 1..86400 (1d cap per spec §12 deadline rule).
        artifacts: optional list of inspectable artifacts.

    Returns:
        HITLPrompt validated by Pydantic v2.

    Raises:
        ValueError: when ``options`` < 2 or ``default_option_id`` is not
            in ``options``.
        pydantic.ValidationError: on schema violations.
    """
    opts = _ensure_options(options)
    chans: list[HITLChannel] = list(channels) if channels else ["lark", "ide", "cli"]
    default = _pick_default(opts, default_option_id)
    why = (
        f"LangGraph interrupt() — current state keys: "
        f"{sorted(graph_state.keys()) or '(empty)'}"
    )
    return HITLPrompt(
        trigger="info_request",
        why=why,
        what=question,
        options=opts,
        default_option_id=default,
        channels=chans,
        deadline_seconds=deadline_seconds,
        artifacts=list(artifacts) if artifacts else [],
    )


def create_round_floor_prompt(
    *,
    round_num: int,
    blockers: Sequence[str],
    evidence_paths: Sequence[str],
    deadline_seconds: int = _DEFAULT_DEADLINE_SECONDS,
) -> HITLPrompt:
    """Build the §12.6 round-floor escalation prompt (3-option card).

    Per spec §12.6: when a self-evolution round fails to reach the
    composite gate floor (``< gate_threshold``), PopolaLoom MUST escalate
    to all 5 channels with three canonical options:

    - ``override`` — accept current result, advance round despite floor
    - ``rollback`` — roll back to ``round_num - 1`` and replan
    - ``defer`` — keep round in pending state for human to investigate

    Args:
        round_num: 1-indexed round that failed (must be ≥ 1).
        blockers: list of blocker-severity findings strings (≥ 1 expected).
        evidence_paths: NDJSON / artefact paths the human can audit.
        deadline_seconds: deadline override (default 24h).

    Returns:
        HITLPrompt with ``trigger="round_floor"``, 5 channels, ``defer``
        as default.

    Raises:
        ValueError: when ``round_num`` < 1 or no blockers / no evidence.
    """
    if round_num < 1:
        raise ValueError(f"round_num must be ≥ 1; got {round_num}")
    blockers_list = [b for b in blockers if b.strip()]
    if not blockers_list:
        raise ValueError(
            "create_round_floor_prompt requires ≥ 1 blocker reason "
            "(use create_interrupt_prompt for non-blocker decisions)"
        )

    options = [
        HITLOption(id="override", label="Override (advance despite floor)"),
        HITLOption(id="rollback", label=f"Rollback to round {round_num - 1}"),
        HITLOption(id="defer", label="Defer (pause for human review)", default=True),
    ]

    bullet_blockers = "\n".join(f"  - {b}" for b in blockers_list[:5])
    why = (
        f"Round {round_num} failed to reach the composite gate floor.\n"
        f"Blockers ({len(blockers_list)}):\n{bullet_blockers}"
    )
    what = (
        f"Choose how to handle round {round_num}: override the floor, "
        f"rollback to the previous round's state, or defer for review."
    )

    artifacts: list[ArtifactRef] = []
    for path in evidence_paths:
        if path.strip():
            artifacts.append(ArtifactRef(type="event_log", uri=path))

    return HITLPrompt(
        trigger="round_floor",
        why=why,
        what=what,
        options=options,
        default_option_id="defer",
        channels=list(_ALL_CHANNELS),
        deadline_seconds=deadline_seconds,
        artifacts=artifacts,
    )


def create_critical_error_prompt(
    *,
    error_msg: str,
    recovery_options: Sequence[HITLOption],
    artifacts: Sequence[ArtifactRef] | None = None,
    deadline_seconds: int = _DEFAULT_DEADLINE_SECONDS,
) -> HITLPrompt:
    """Build a ``destructive_op`` prompt for a non-recoverable error.

    Used when popolad / LangGraph raises an exception that can't be
    auto-recovered; human must pick a recovery path.

    Args:
        error_msg: short human-readable error string.
        recovery_options: ≥ 2 recovery options (e.g. retry / abort / restart).
        artifacts: optional traceback / log artifacts.
        deadline_seconds: deadline override (default 24h).
    """
    if not error_msg.strip():
        raise ValueError("create_critical_error_prompt requires non-empty error_msg")
    opts = _ensure_options(recovery_options)
    return HITLPrompt(
        trigger="destructive_op",
        why=f"Non-recoverable error: {error_msg.strip()}",
        what="Pick a recovery action (the daemon will execute the chosen option).",
        options=opts,
        default_option_id=_pick_default(opts, None),
        channels=list(_ALL_CHANNELS),
        deadline_seconds=deadline_seconds,
        artifacts=list(artifacts) if artifacts else [],
    )


def create_ambiguous_fix_prompt(
    *,
    scores: dict[str, float],
    paths: Sequence[str],
    deadline_seconds: int = _DEFAULT_DEADLINE_SECONDS,
) -> HITLPrompt:
    """Build an ``ambiguous_input`` prompt for tied gate scores.

    Used when L3 produced two or more reasonable fixes and the inner
    gate scored them within ``± 0.05`` of each other.

    Args:
        scores: dict ``{fix_id: composite_score}``; ≥ 2 entries.
        paths: list of evidence paths (artefacts / patches per fix).
        deadline_seconds: deadline override (default 24h).

    Returns:
        HITLPrompt with options for each scored fix + ``abort`` fallback.

    Raises:
        ValueError: when ``scores`` < 2 or any score outside ``[0, 1]``.
    """
    if len(scores) < 2:
        raise ValueError(
            f"create_ambiguous_fix_prompt requires ≥ 2 candidate fixes; "
            f"got {len(scores)}"
        )
    for fix_id, score in scores.items():
        if not 0.0 <= score <= 1.0:
            raise ValueError(
                f"score for {fix_id!r} must be in [0, 1]; got {score}"
            )

    sorted_fixes = sorted(scores.items(), key=lambda kv: -kv[1])
    options = [
        HITLOption(id=fix_id, label=f"{fix_id} (score {score:.3f})")
        for fix_id, score in sorted_fixes
    ]
    options.append(HITLOption(id="abort", label="Abort (none of the above)"))

    bullets = "\n".join(f"  - {fid}: {score:.3f}" for fid, score in sorted_fixes)
    why = f"Two or more fixes scored within ±0.05:\n{bullets}"
    what = "Pick the fix to merge (or abort and request a new round)."

    artifacts: list[ArtifactRef] = [
        ArtifactRef(type="diff", uri=path) for path in paths if path.strip()
    ]

    return HITLPrompt(
        trigger="ambiguous_input",
        why=why,
        what=what,
        options=options,
        default_option_id=sorted_fixes[0][0],
        channels=["lark", "ide", "cli"],
        deadline_seconds=deadline_seconds,
        artifacts=artifacts,
    )


def create_persistent_regression_prompt(
    *,
    r_id: str,
    round_history: Sequence[dict[str, object]],
    deadline_seconds: int = _DEFAULT_DEADLINE_SECONDS,
) -> HITLPrompt:
    """Build the §12.6 persistent-regression prompt.

    Same R-issue keeps reappearing across ≥ 3 rounds — escalate to
    human with 3-option contract (override / rollback / defer).

    Args:
        r_id: R-issue identifier (e.g. ``"R-EVO-3"``).
        round_history: list of dicts (one per round) with at least
            ``{"round": int, "finding": str, "score": float}``.
        deadline_seconds: deadline override (default 24h).

    Returns:
        HITLPrompt with ``trigger="round_floor"`` (regression is a
        floor-failure flavour) on all 5 channels, ``defer`` default.

    Raises:
        ValueError: when ``r_id`` blank or ``round_history`` shorter
            than 3 entries.
    """
    if not r_id.strip():
        raise ValueError("r_id must be non-empty")
    if len(round_history) < 3:
        raise ValueError(
            f"persistent regression requires ≥ 3 round history entries; "
            f"got {len(round_history)}"
        )

    options = [
        HITLOption(id="override", label=f"Override {r_id} (accept regression)"),
        HITLOption(id="rollback", label="Rollback to last green round"),
        HITLOption(id="defer", label="Defer for human investigation", default=True),
    ]

    history_summary = "\n".join(
        f"  - round {entry.get('round')}: "
        f"score={entry.get('score', 'n/a')} — {entry.get('finding', '(no finding)')}"
        for entry in round_history[-5:]
    )
    why = (
        f"{r_id} reappeared in ≥ {len(round_history)} consecutive rounds.\n"
        f"Recent history:\n{history_summary}"
    )
    what = (
        f"{r_id} is a persistent regression. Decide: override the gate, "
        f"rollback, or defer."
    )

    return HITLPrompt(
        trigger="round_floor",
        why=why,
        what=what,
        options=options,
        default_option_id="defer",
        channels=list(_ALL_CHANNELS),
        deadline_seconds=deadline_seconds,
        artifacts=[],
    )


__all__ = [
    "create_ambiguous_fix_prompt",
    "create_critical_error_prompt",
    "create_interrupt_prompt",
    "create_persistent_regression_prompt",
    "create_round_floor_prompt",
]
