"""federate primitive — multi-CLI dispatch + voting (v0.3.0 F2.4).

Spawns the same prompt to ≥ 3 CLIs simultaneously then aggregates
their outputs via a voting strategy.  v0.3.0 ships ``majority`` /
``unanimous`` / ``first_to_finish`` strategies; v0.4.0+ may add
semantic similarity voting (LLM-as-judge) but for v0.3.0 mvp we
hash the output strings and pick the most-common bucket.

Design (per v0.3.0-plan.md §4 Stage F2 + spec §4.2 in
``.local/memory/specs/popolaloom/``):

- :class:`FederateConfig` Pydantic v2 model = the wire schema for
  configuring a federate dispatch.  Validated strictly with at least
  3 CLIs (``≥ 3 cli_list`` per task spec).
- :func:`federate` spawns N child tasks via :meth:`Popolad.dispatch_task`
  and returns immediately with the list of child task ids.  The
  caller is responsible for waiting on the children + invoking
  :func:`tally_votes` on their outputs (the daemon doesn't block on
  child completion in v0.3.0 mvp — that's an F4 supervise concern).
- :func:`tally_votes` is exposed publicly so RPC + tests + later
  v0.3.x voting strategy upgrades can reuse the simple hash-bucket
  algorithm.

Workspace rule "No Silent Failures": validation errors raise; the
RPC layer maps to ``HTTP 400``.  When the popolad facade rejects a
dispatch (e.g. unknown CLI adapter), :func:`federate` logs + records
the failure in the result so partial federations are observable.
"""

from __future__ import annotations

import collections
import hashlib
import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from popolaloom.daemon.server import Popolad

logger = logging.getLogger(__name__)

VotingStrategy = Literal["majority", "unanimous", "first_to_finish"]
"""Supported voting strategies for v0.3.0."""

DEFAULT_FEDERATE_CLIS: tuple[str, ...] = ("cursor", "claude", "codex")
"""Default 3-way fan-out across the v0.2.x supported CLIs."""

MIN_FEDERATE_CLIS: int = 3
"""≥ 3 CLIs required (per task spec); fewer would be a regular dispatch."""


class FederateConfig(BaseModel):
    """Federate-dispatch configuration (per v0.3.0-plan.md F2.4).

    Attributes:
        cli_list:        ≥ 3 CLI adapter names; duplicates are kept
                         (e.g. running cursor twice with different
                         seeds is a valid voting strategy).
        prompt:          The prompt sent verbatim to every CLI.
        voting_strategy: How outputs are aggregated.
        cwd:             Optional working directory shared by all
                         children; defaults to popolad CWD.
        extra:           Optional adapter extras shared by all children
                         (per-child extras are NOT supported in v0.3.0
                         mvp — same prompt + same flags = same task).

    Workspace rule "No Silent Failures": ``extra="forbid"`` so unknown
    keys raise :class:`pydantic.ValidationError`.
    """

    model_config = ConfigDict(extra="forbid")

    cli_list: list[str] = Field(default_factory=lambda: list(DEFAULT_FEDERATE_CLIS))
    prompt: str = Field(..., min_length=1)
    voting_strategy: VotingStrategy = "majority"
    cwd: str | None = None
    extra: dict[str, Any] | None = None

    @field_validator("cli_list")
    @classmethod
    def _at_least_three(cls, v: list[str]) -> list[str]:
        if len(v) < MIN_FEDERATE_CLIS:
            raise ValueError(
                f"FederateConfig.cli_list must have ≥ {MIN_FEDERATE_CLIS} entries "
                f"(got {len(v)}); use plain dispatch for fewer."
            )
        for i, name in enumerate(v):
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"FederateConfig.cli_list[{i}] must be a non-empty string; got {name!r}"
                )
        return v


class FederateResult(BaseModel):
    """Result returned by :func:`federate` (per task spec F2.3).

    Attributes:
        federate_id:    UUID4 hex id for the federate run (used as a
                        correlation id in the parent task's event log).
        child_task_ids: list of popola child task ids (one per CLI).
        cli_list:       echo of the cli_list (preserves order).
        voting_strategy: echo of the voting_strategy (informational).
        dispatch_errors: per-CLI dispatch errors (cli → message); when
                        non-empty, ``len(child_task_ids) < len(cli_list)``.
    """

    model_config = ConfigDict(extra="forbid")

    federate_id: str
    child_task_ids: list[str]
    cli_list: list[str]
    voting_strategy: VotingStrategy
    dispatch_errors: dict[str, str] = Field(default_factory=dict)


class VoteOutcome(BaseModel):
    """Result of :func:`tally_votes` over child outputs (per F2 spec).

    Attributes:
        winning_output: the output string that won the vote (or empty
                        when ``passed=False`` for ``unanimous`` strict
                        mode).
        passed: whether the voting strategy considers the outcome valid
                (e.g. ``unanimous`` requires 100% agreement).
        votes: count of each output bucket; sorted by count descending.
        total: total number of votes counted (excluding skipped).
        strategy: echo of the voting strategy used.
        skipped: number of outputs that were skipped (None / empty).
    """

    model_config = ConfigDict(extra="forbid")

    winning_output: str
    passed: bool
    votes: dict[str, int]
    total: int
    strategy: VotingStrategy
    skipped: int = 0


def _output_bucket(output: str) -> str:
    """Compute the SHA256-based bucket key for an output string.

    v0.3.0 mvp uses the literal SHA256 hex digest as the bucket key —
    semantic similarity (e.g. embedding-based clustering) is reserved
    for v0.4.0+.  Whitespace is stripped at edges to make ``"foo\\n"``
    and ``"foo"`` the same vote (common quirk of CLI stdout).
    """
    return hashlib.sha256(output.strip().encode("utf-8")).hexdigest()


def tally_votes(
    outputs: dict[str, str],
    voting_strategy: VotingStrategy,
) -> VoteOutcome:
    """Aggregate per-CLI outputs into a winner under ``voting_strategy``.

    Args:
        outputs: ``cli_or_task_id -> output_string`` map.  ``None``
            entries are skipped (they count toward ``skipped`` but
            NOT toward the vote).
        voting_strategy: which strategy to apply.

    Returns:
        VoteOutcome describing the winner + counts.

    Strategies:

    - ``majority``: the bucket with the most votes wins.  Tie-breaking
      is undefined (Python's ``Counter.most_common`` returns insertion
      order on ties).  ``passed=True`` when winner has > N/2 votes
      (strict majority), ``False`` for plurality only.
    - ``unanimous``: every output must hash to the same bucket.
      ``passed=False`` if ANY disagreement.
    - ``first_to_finish``: the first non-empty entry wins (caller is
      responsible for ordering ``outputs`` in time-of-arrival order).
      ``passed=True`` iff at least one output was non-empty.
    """
    cleaned: list[tuple[str, str, str]] = []
    skipped = 0
    for source, output in outputs.items():
        if output is None or not str(output).strip():
            skipped += 1
            continue
        bucket = _output_bucket(str(output))
        cleaned.append((source, str(output), bucket))

    if not cleaned:
        return VoteOutcome(
            winning_output="",
            passed=False,
            votes={},
            total=0,
            strategy=voting_strategy,
            skipped=skipped,
        )

    if voting_strategy == "first_to_finish":
        first_source, first_output, first_bucket = cleaned[0]
        votes_counter: collections.Counter[str] = collections.Counter()
        for _src, _out, bucket in cleaned:
            votes_counter[bucket] += 1
        return VoteOutcome(
            winning_output=first_output,
            passed=True,
            votes=dict(votes_counter),
            total=len(cleaned),
            strategy=voting_strategy,
            skipped=skipped,
        )

    bucket_to_output: dict[str, str] = {b: o for _s, o, b in cleaned}
    counter: collections.Counter[str] = collections.Counter(b for _s, _o, b in cleaned)
    sorted_counts = counter.most_common()
    winner_bucket, winner_count = sorted_counts[0]
    winner_output = bucket_to_output[winner_bucket]
    total = sum(counter.values())

    if voting_strategy == "unanimous":
        passed = winner_count == total and len(counter) == 1
    elif voting_strategy == "majority":
        passed = winner_count > total / 2
    else:
        raise ValueError(
            f"tally_votes: unsupported voting_strategy={voting_strategy!r}"
        )

    return VoteOutcome(
        winning_output=winner_output,
        passed=passed,
        votes=dict(counter),
        total=total,
        strategy=voting_strategy,
        skipped=skipped,
    )


def federate(
    popolad: Popolad,
    *,
    prompt: str,
    cli_list: list[str] | None = None,
    voting_strategy: VotingStrategy = "majority",
    cwd: str | None = None,
    extra: dict[str, Any] | None = None,
) -> FederateResult:
    """Dispatch the same prompt to every CLI in ``cli_list``.

    Each child task receives ``extra["federate_id"]`` so downstream
    consumers (F4 HITL renderer, F5 self-bootstrap S5) can correlate
    results.  v0.3.0 mvp returns immediately after dispatch — the
    caller waits on terminal events themselves (typically via SSE
    attach or :func:`tally_votes` once outputs are collected).

    Args:
        popolad: The :class:`Popolad` facade instance.
        prompt: Prompt sent verbatim to every CLI.
        cli_list: ≥ 3 CLI adapter names; default = ``("cursor", "claude", "codex")``.
        voting_strategy: which strategy to use when caller later calls
            :func:`tally_votes` on the outputs (echoed in the result).
        cwd: optional shared working directory.
        extra: optional adapter extras shared by all children.

    Returns:
        FederateResult: with the federate_id + dispatched child task ids.

    Raises:
        ValueError: when ``cli_list`` < 3 or ``prompt`` blank.
    """
    import uuid as _uuid

    config = FederateConfig(
        cli_list=list(cli_list) if cli_list is not None else list(DEFAULT_FEDERATE_CLIS),
        prompt=prompt,
        voting_strategy=voting_strategy,
        cwd=cwd,
        extra=extra,
    )

    federate_id = _uuid.uuid4().hex
    child_task_ids: list[str] = []
    dispatch_errors: dict[str, str] = {}

    for index, cli in enumerate(config.cli_list):
        child_extra: dict[str, Any] = {
            **(config.extra or {}),
            "federate_id": federate_id,
            "federate_index": index,
            "federate_voting_strategy": voting_strategy,
        }
        try:
            child_task_id = popolad.dispatch_task(
                cli=cli,
                prompt=config.prompt,
                cwd=config.cwd,
                env=None,
                adapter=None,
                extra=child_extra,
            )
        except Exception as exc:
            logger.exception(
                "federate: dispatch to cli=%s (index=%d) failed; recording error",
                cli,
                index,
            )
            dispatch_errors[f"{cli}#{index}"] = repr(exc)
            continue
        child_task_ids.append(child_task_id)

    logger.info(
        "federate: id=%s strategy=%s dispatched=%d/%d errors=%d",
        federate_id,
        voting_strategy,
        len(child_task_ids),
        len(config.cli_list),
        len(dispatch_errors),
    )

    return FederateResult(
        federate_id=federate_id,
        child_task_ids=child_task_ids,
        cli_list=list(config.cli_list),
        voting_strategy=voting_strategy,
        dispatch_errors=dispatch_errors,
    )


__all__ = [
    "DEFAULT_FEDERATE_CLIS",
    "FederateConfig",
    "FederateResult",
    "MIN_FEDERATE_CLIS",
    "VoteOutcome",
    "VotingStrategy",
    "federate",
    "tally_votes",
]
