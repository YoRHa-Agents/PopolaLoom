"""Typed wrappers for v0.8.8 multi-run cloud lifecycle events.

T2.1.1 — owns the canonical schema for ``cloud.run_started`` and
``cloud.run_finished`` envelopes per ``event-merge-spec.md`` §2.3 and §5.

Both events are **terminal-cycle markers** that bracket the inner stream of
``cloud.run_status`` and ``cloud.sse.*`` events for one run; because they are
emitted by popolad code (not synthesized from SSE) they are dedup-immune and
safely re-orderable on replay. Centralising the schema here gives ArkTower /
replay consumers a single typed surface to import and lets us evolve the
keyset without mutating every call site.

Wire shape (per spec §2.3 + §5):

- ``cloud.run_started`` (producer: popolad / supervisor; cadence: once per
  run, at creation): ``task_id, agent_id, run_id, run_index, started_at``
  + optional ``parent_run_id`` (``null`` for the initial run; the prior
  ``run_id`` for follow-ups) + optional ``prompt_digest`` (SHA-256 hex of
  the follow-up ``prompt.text`` for at-a-glance diffing without leaking
  secrets).
- ``cloud.run_finished`` (producer: :class:`CloudPollLoop`; cadence: once
  per run, at terminal phase): ``task_id, agent_id, run_id, run_index,
  terminal_phase, ended_at, exit_code``. ``terminal_phase`` ∈
  ``{FINISHED, ERROR, CANCELLED, EXPIRED}``.

Invariants (enforced by ``tests/cloud/test_multi_run.py``):

- I-10 — exactly one ``cloud.run_started`` per ``run_id``; its ``time`` is
  ``<=`` every other envelope's ``time`` for that ``run_id``. Symmetric for
  ``cloud.run_finished``.
- I-11 — within a single ``task_id``, no two ``run_id`` values share the
  same ``run_index``.

Both helpers are thin facades over :meth:`EventLog.append`; they NEVER mutate
``TaskState`` or ``TaskHandle`` (preserves the v0.8.6 sole-writer rule).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from popolaloom.daemon.event_log import EventLog


def record_run_started(
    event_log: EventLog,
    *,
    task_id: str,
    agent_id: str,
    run_id: str,
    run_index: int,
    started_at: str,
    parent_run_id: str | None = None,
    prompt_digest: str | None = None,
) -> dict[str, Any]:
    """Emit ``cloud.run_started`` and return the rendered envelope.

    Called by popolad / supervisor immediately after ``POST /v1/agents``
    (initial run) or ``POST /v1/agents/{id}/runs`` (follow-up) returns a
    non-error response. ``run_index`` is the 0-based ordinal of the run
    within its agent (``0`` for the initial run; ``n`` for the nth
    follow-up). ``parent_run_id`` is ``None`` for the initial run and the
    prior ``run_id`` for follow-ups (renderers use it to display
    "follow-up of run-N" in the §3.2 divider).

    Args:
        event_log: Append target; never mutated otherwise.
        task_id: PopolaLoom-internal task id (stable across restarts).
        agent_id: Cursor durable ``bc-...`` agent id.
        run_id: Cursor per-run id.
        run_index: 0-based ordinal within this agent.
        started_at: ISO-8601 UTC timestamp (ms precision; ``Z`` suffix);
            should match the producer's *event-causation-time*.
        parent_run_id: Prior ``run_id`` for follow-ups; ``None`` for the
            initial run.
        prompt_digest: Optional SHA-256 hex of the follow-up prompt; lets
            renderers diff at a glance without leaking secrets.

    Returns:
        The full CloudEvents envelope dict written to ``event_log``.
    """
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "run_index": run_index,
        "started_at": started_at,
    }
    if parent_run_id is not None:
        data["parent_run_id"] = parent_run_id
    if prompt_digest is not None:
        data["prompt_digest"] = prompt_digest
    return event_log.append("cloud.run_started", data)


def record_run_finished(
    event_log: EventLog,
    *,
    task_id: str,
    agent_id: str,
    run_id: str,
    run_index: int,
    terminal_phase: str,
    ended_at: str,
    exit_code: int,
) -> dict[str, Any]:
    """Emit ``cloud.run_finished`` and return the rendered envelope.

    Called by :class:`CloudPollLoop` on its terminal branch (alongside the
    existing ``task.{completed,failed,canceled}`` event), so a single
    terminal observation produces both the run-bracket marker (this
    function) and the task-lifecycle marker. Renderers and replayers use
    the former to detect run boundaries without scanning every
    intermediate ``cloud.run_status`` / ``cloud.sse.*`` envelope.

    Args:
        event_log: Append target.
        task_id: PopolaLoom-internal task id.
        agent_id: Cursor agent id.
        run_id: Cursor run id.
        run_index: 0-based ordinal within this agent.
        terminal_phase: ``FINISHED`` / ``ERROR`` / ``CANCELLED`` /
            ``EXPIRED`` (the upstream Cursor phase string, uppercased).
        ended_at: ISO-8601 UTC timestamp (ms precision; ``Z`` suffix).
        exit_code: PopolaLoom-mapped exit code (``0`` for FINISHED;
            ``-2`` for CANCELLED; ``1`` for ERROR / EXPIRED).

    Returns:
        The full CloudEvents envelope dict written to ``event_log``.
    """
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "run_index": run_index,
        "terminal_phase": terminal_phase,
        "ended_at": ended_at,
        "exit_code": exit_code,
    }
    return event_log.append("cloud.run_finished", data)
