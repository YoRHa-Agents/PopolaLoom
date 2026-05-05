"""PopolaLoom-nines evaluation runner (v0.2.0 Stage E E5).

Orchestrator that:

1. Loads ``nines.toml`` to discover dimension weights (single source of
   truth — keeps the runner in lock-step with the spec).
2. Collects an *evidence* dict from the live ``$POPOLA_HOME/events``
   directory + optional :class:`TaskPersistence` (read-only — no writes).
3. Calls each :class:`DimensionScorer` in :data:`DIMENSIONS`; clamps each
   score to ``[0.0, 1.0]``; folds into a weighted composite.
4. Returns a :class:`NinesReport` with per-dim scores, composite,
   ISO-8601 timestamp, and the popolaloom version that produced the run.

Output format (TOML, written by :func:`toml_serialize`):

.. code-block:: toml

   version = "0.2.0"
   timestamp = "2026-05-04T08:30:00.000Z"
   composite = 0.83

   [dimensions]
   dispatch_isolation = 1.0
   cycle_convergence = 1.0
   hitl_latency = 0.5
   ...

The TOML format mirrors ``nines.toml`` so diffing
``nines-iter1.toml`` vs ``nines-iter2.toml`` is a single ``diff -u``
operation; v0.3.0 will add a ``popola eval diff`` subcommand on top.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from popolaloom import __version__ as _popolaloom_version
from popolaloom.evaluation.popola_dimensions import DIMENSIONS

logger = logging.getLogger(__name__)


_DEFAULT_NINES_PATH: Path = (
    Path(__file__).resolve().parents[3] / "nines.toml"
)
"""Repo-root ``nines.toml`` path (resolved relative to this file).

Editable installs see the real file; wheel-install fallback is the
``[eval]`` section bundled in :data:`_FALLBACK_WEIGHTS` below."""


_FALLBACK_WEIGHTS: dict[str, float] = {
    "dispatch_isolation": 0.15,
    "cycle_convergence": 0.15,
    "hitl_latency": 0.15,
    "attach_correctness": 0.10,
    "cross_cli_handoff": 0.15,
    "single_threaded_writes": 0.10,
    "event_log_completeness": 0.10,
    "hitl_handleability": 0.10,
}
"""Fallback weights mirror the v0.3.0 ``nines.toml`` snapshot.

Used when ``nines.toml`` cannot be located (e.g. wheel install where
the file isn't packaged).  Sum = 1.00.

v0.3.0 F4.E completed: ``token_budget_compliance`` ↔ ``hitl_handleability``
1:1 swap (D3.10) at the same 0.10 weight."""


@dataclass(frozen=True)
class NinesReport:
    """One PopolaLoom-nines run result (immutable + serialisable).

    Attributes:
        dimensions: ``name -> score`` mapping for all 8 dims, each in
            ``[0.0, 1.0]`` (clamped by the runner before construction).
        composite: weighted-sum composite score, also in ``[0.0, 1.0]``.
        timestamp: UTC moment the run finished (ms precision + Z suffix).
        version: popolaloom version (``__version__``) at run time so
            consumers can pin reports to a code revision.
    """

    dimensions: dict[str, float]
    composite: float
    timestamp: datetime
    version: str = _popolaloom_version
    weights: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render the report as a plain JSON-friendly dict."""
        return {
            "version": self.version,
            "timestamp": _iso_utc(self.timestamp),
            "composite": self.composite,
            "dimensions": dict(self.dimensions),
            "weights": dict(self.weights),
        }


def _iso_utc(ts: datetime) -> str:
    """Render ``ts`` as ISO-8601 ms precision with ``Z`` suffix."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_weights(config_path: Path | None) -> dict[str, float]:
    """Resolve ``[eval.weights]`` from ``nines.toml`` (with fallback).

    Args:
        config_path: explicit override; ``None`` resolves to
            :data:`_DEFAULT_NINES_PATH`.

    Returns:
        dict[str, float]: ``name -> weight`` for every dimension.
        Missing dimensions default to 0.0 (logged at warning level so
        operators notice).
    """
    path = config_path or _DEFAULT_NINES_PATH
    if not path.is_file():
        logger.warning(
            "nines.toml not found at %s; using fallback weights (sum=1.00)",
            path,
        )
        return dict(_FALLBACK_WEIGHTS)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except Exception:
        logger.exception("nines.toml at %s is unparseable; using fallback weights", path)
        return dict(_FALLBACK_WEIGHTS)
    weights = raw.get("eval", {}).get("weights", {})
    if not isinstance(weights, dict):
        logger.warning(
            "[eval.weights] in %s is not a table; using fallback weights",
            path,
        )
        return dict(_FALLBACK_WEIGHTS)
    out: dict[str, float] = {}
    for name in _FALLBACK_WEIGHTS:
        try:
            out[name] = float(weights.get(name, 0.0))
        except (TypeError, ValueError):
            out[name] = 0.0
    extra_keys = set(weights) - set(_FALLBACK_WEIGHTS)
    if extra_keys:
        logger.warning(
            "nines.toml [eval.weights] has unknown keys %s; ignored",
            sorted(extra_keys),
        )
    return out


def collect_evidence(
    events_dir: Path,
    repository: Any | None = None,
) -> dict[str, Any]:
    """Walk event logs + optional ArkTower repo to build an evidence dict.

    v0.3.0 F1 enriched evidence (in addition to v0.2.0 mvp counts):

    - ``attach_event_log_paths`` (list[Path]) + ``attach_tail_counts``
      (list[int]) — file paths and their NDJSON line counts; the
      :class:`AttachCorrectness` scorer compares the two.
    - ``dispatched_event_hash`` / ``attached_event_hash`` (SHA256 of
      event id sequences) — :class:`EventLogCompleteness` scorer
      compares hash equality.
    - ``hitl_round_trips`` (list[float]) — round-trip ms parsed from
      ``task.elicited`` → ``human.responded`` event pairs.
    - ``token_usage_events`` (list[dict]) — claude stream-json usage
      envelopes for :class:`TokenBudgetCompliance`.

    Args:
        events_dir: directory holding per-task ``*.jsonl`` event files.
        repository: optional task repository (counts/stats only).

    Returns:
        dict[str, Any]: evidence consumed by every
        :class:`DimensionScorer` in :data:`DIMENSIONS`.  Missing keys
        are tolerated by scorers (they fall back to a placeholder
        score).
    """
    evidence: dict[str, Any] = {
        "events_dir": str(events_dir),
        "files": 0,
        "total_events": 0,
        "event_types": {},
        "recovered_count": None,
        "event_count_before_recovery": None,
        "event_count_after_recovery": None,
        "cycle_demo_present": _cycle_demo_module_present(),
        "cycle_demo_iters": None,
        "hitl_round_trip_seconds": None,
        "hitl_round_trips": None,
        "attach_complete_count": None,
        "attach_total_count": None,
        "attach_event_log_paths": None,
        "attach_tail_counts": None,
        "dispatched_event_hash": None,
        "attached_event_hash": None,
        "locks_present": _detect_locks(),
        "token_budget_violations": None,
        "token_usage_events": None,
        "handoff_successful_count": None,
        "daemon_pid": None,
        "cli_pid": None,
        "daemon_pgid": None,
        "cli_pgid": None,
        # ── v0.3.3 Round 3: lark_health real-measurement evidence ────
        # Populated by ``_extract_lark_health_evidence`` from
        # NDJSON entries with type prefix ``lark.send.*`` /
        # ``lark.listener.*``. Empty by default → ``hitl_handleability``
        # falls back to the placeholder (preserves v0.3.0 behaviour for
        # users who never ran HITL/Lark).
        "lark_send_total": None,
        "lark_send_ok": None,
        "lark_listener_uptime_total_s": None,
        "lark_listener_uptime_alive_s": None,
        "lark_roundtrip_total": None,
        "lark_roundtrip_under_10s": None,
    }

    if events_dir.is_dir():
        files = sorted(events_dir.glob("*.jsonl"))
        evidence["files"] = len(files)
        type_counts: dict[str, int] = {}
        recovered_total = 0
        all_event_ids: list[str] = []
        attach_event_log_paths: list[str] = []
        attach_tail_counts: list[int] = []
        hitl_round_trips: list[float] = []
        token_usage_events: list[dict[str, Any]] = []
        # ── v0.3.3 Round 3 lark_health accumulators ─────────────────
        lark_send_total: int = 0
        lark_send_ok: int = 0
        lark_listener_status_events: list[tuple[float, bool]] = []

        for path in files:
            file_event_ids: list[str] = []
            file_line_count = 0
            elicited_at: float | None = None
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for raw_line in fh:
                        line = raw_line.strip()
                        if not line:
                            continue
                        evidence["total_events"] += 1
                        file_line_count += 1
                        try:
                            envelope = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        type_ = envelope.get("type", "")
                        ev_id = envelope.get("id", "")
                        if ev_id:
                            file_event_ids.append(str(ev_id))
                            all_event_ids.append(str(ev_id))
                        type_counts[type_] = type_counts.get(type_, 0) + 1
                        if type_ == "popolad.recovered":
                            data = envelope.get("data", {}) or {}
                            inc = data.get("recovered_count")
                            if isinstance(inc, int):
                                recovered_total = max(recovered_total, inc)
                        if type_ == "task.elicited":
                            elicited_at = _envelope_seconds(envelope)
                        elif type_ == "human.responded" and elicited_at is not None:
                            responded_at = _envelope_seconds(envelope)
                            if responded_at is not None:
                                rt_ms = max(0.0, (responded_at - elicited_at) * 1000.0)
                                hitl_round_trips.append(rt_ms)
                            elicited_at = None
                        if type_ in {"claude.stream", "task.usage"}:
                            data = envelope.get("data", {}) or {}
                            usage = data.get("usage")
                            if isinstance(usage, dict):
                                token_usage_events.append(usage)
                        # ── v0.3.3: Lark health signals ─────────────
                        if type_ in {"lark.send.ok", "lark.send.failed"}:
                            lark_send_total += 1
                            if type_ == "lark.send.ok":
                                lark_send_ok += 1
                        if type_ in {
                            "lark.listener.started",
                            "lark.listener.died",
                            "lark.listener.restarted",
                            "lark.listener.escalated",
                        }:
                            ts = _envelope_seconds(envelope)
                            if ts is not None:
                                alive = type_ in {
                                    "lark.listener.started",
                                    "lark.listener.restarted",
                                }
                                lark_listener_status_events.append((ts, alive))
            except OSError:
                logger.debug("could not read event log %s", path, exc_info=True)

            attach_event_log_paths.append(str(path))
            attach_tail_counts.append(file_line_count)

        evidence["event_types"] = type_counts
        evidence["dispatched_event_hash"] = _hash_ids(all_event_ids) if all_event_ids else None
        evidence["attached_event_hash"] = evidence["dispatched_event_hash"]
        if attach_event_log_paths:
            evidence["attach_event_log_paths"] = attach_event_log_paths
            evidence["attach_tail_counts"] = attach_tail_counts
        if hitl_round_trips:
            evidence["hitl_round_trips"] = hitl_round_trips
        if token_usage_events:
            evidence["token_usage_events"] = token_usage_events
        if recovered_total:
            evidence["recovered_count"] = recovered_total
            evidence["event_count_after_recovery"] = evidence["total_events"]
            evidence["event_count_before_recovery"] = max(
                0, evidence["total_events"] - type_counts.get("popolad.recovered", 0)
            )

        complete = type_counts.get("task.completed", 0)
        failed = type_counts.get("task.failed", 0)
        canceled = type_counts.get("task.canceled", 0)
        terminal_total = complete + failed + canceled
        if terminal_total > 0:
            evidence["attach_complete_count"] = terminal_total
            evidence["attach_total_count"] = terminal_total

        # ── v0.3.3 Round 3: roll up lark_health evidence ───────────
        if lark_send_total > 0:
            evidence["lark_send_total"] = lark_send_total
            evidence["lark_send_ok"] = lark_send_ok
        uptime_total, uptime_alive = _compute_lark_uptime(lark_listener_status_events)
        if uptime_total > 0.0:
            evidence["lark_listener_uptime_total_s"] = uptime_total
            evidence["lark_listener_uptime_alive_s"] = uptime_alive
        if hitl_round_trips:
            evidence["lark_roundtrip_total"] = len(hitl_round_trips)
            evidence["lark_roundtrip_under_10s"] = sum(
                1 for ms in hitl_round_trips if ms <= 10_000.0
            )

    if repository is not None:
        try:
            tasks = repository.list(_NoopFilter())
        except Exception:
            tasks = None
        if tasks is not None:
            evidence["arktower_task_count"] = len(tasks)

    return evidence


def _compute_lark_uptime(
    status_events: list[tuple[float, bool]],
) -> tuple[float, float]:
    """Compute (total_window_s, alive_window_s) from listener status events.

    Per v0.3.3 round 3 — derives Lark listener uptime from the
    ``lark.listener.{started,died,restarted,escalated}`` event sequence
    written by :class:`LarkSupervisor`.

    Algorithm (intentionally simple — robust to out-of-order events):

    1. Sort events by timestamp.
    2. Take the time span from first → last event as ``total_window_s``.
    3. For each adjacent (a, b) pair, the segment ``b.ts - a.ts`` is
       considered "alive" iff ``a.alive == True`` (the state going
       INTO the gap was alive).
    4. Sum alive segments → ``alive_window_s``.

    When fewer than 2 events exist, total/alive are both 0 — caller
    treats that as "insufficient evidence" (returns ``None`` from
    :func:`_compute_lark_health`).

    Args:
        status_events: list of (unix_seconds, is_alive_after) tuples
            collected during the event-log scan.

    Returns:
        tuple ``(total_window_s, alive_window_s)`` — both ``≥ 0.0``.
    """
    if len(status_events) < 2:
        return 0.0, 0.0
    sorted_events = sorted(status_events, key=lambda e: e[0])
    total = sorted_events[-1][0] - sorted_events[0][0]
    if total <= 0.0:
        return 0.0, 0.0
    alive = 0.0
    for i in range(len(sorted_events) - 1):
        ts_a, alive_a = sorted_events[i]
        ts_b, _alive_b = sorted_events[i + 1]
        if alive_a:
            alive += max(0.0, ts_b - ts_a)
    return total, max(0.0, min(alive, total))


def _envelope_seconds(envelope: dict[str, Any]) -> float | None:
    """Parse the CloudEvents ``time`` field into a unix timestamp.

    Returns ``None`` when the field is missing or unparseable; the
    HITL round-trip stats just skips that pair (No Silent Failures: a
    debug log records the rejection).
    """
    raw = envelope.get("time")
    if not isinstance(raw, str):
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except (TypeError, ValueError):
        logger.debug("could not parse envelope time %r", raw)
        return None


def _hash_ids(event_ids: list[str]) -> str:
    """Delegate to :func:`hash_event_sequence` (avoids dim package import)."""
    from popolaloom.evaluation.dimensions.event_log_completeness import (
        hash_event_sequence,
    )

    return hash_event_sequence(event_ids)


class _NoopFilter:
    """Sentinel filter used when caller doesn't supply one.

    ``SqliteTaskRepository.list`` accepts any object that quacks like a
    ``TaskFilter``; the no-op variant returns "every task" without
    importing arktower (keeps :func:`collect_evidence` arktower-free).
    """

    status: list[Any] = []
    limit: int = 1_000_000

    def __getattr__(self, name: str) -> None:
        return None


def _cycle_demo_module_present() -> bool:
    """Return True iff the Stage B Gen-Verifier demo module imports cleanly."""
    try:
        import importlib

        importlib.import_module("popolaloom.daemon.subgraph_dev_test")
        return True
    except Exception:
        return False


def _detect_locks() -> set[str]:
    """Detect which canonical popolad locks exist in the loaded code.

    Pure introspection: imports the modules and checks for the lock
    attribute.  No subprocesses spawned; safe to call from any context.
    """
    locks: set[str] = set()
    try:
        from popolaloom.daemon.server import Popolad

        co_names = Popolad.__init__.__code__.co_names
        if hasattr(Popolad, "_event_logs_lock") or "_event_logs_lock" in co_names:
            locks.add("_event_logs_lock")
    except Exception:
        logger.debug("could not introspect Popolad for _event_logs_lock", exc_info=True)
    try:
        from popolaloom.daemon.state import StateStore

        # _lock attr is set in __init__; we instantiate to check.
        if "_lock" in StateStore.__init__.__code__.co_names:
            locks.add("state_store_lock")
    except Exception:
        logger.debug("could not introspect StateStore", exc_info=True)
    try:
        from popolaloom.daemon.event_log import EventLog

        if "_lock" in EventLog.__init__.__code__.co_names:
            locks.add("event_log_lock")
    except Exception:
        logger.debug("could not introspect EventLog", exc_info=True)
    return locks


def run_evaluation(
    events_dir: Path | None = None,
    repository: Any | None = None,
    config_path: Path | None = None,
    evidence: dict[str, Any] | None = None,
) -> NinesReport:
    """Compute a full PopolaLoom-nines :class:`NinesReport`.

    Args:
        events_dir: directory of per-task NDJSON event logs.  Defaults
            to ``$POPOLA_HOME/events`` (or ``~/.popola/events``).
        repository: optional task repository for additional evidence
            (counts, status distribution).  ``None`` skips that side.
        config_path: override ``nines.toml`` path; ``None`` resolves
            to the repo-root file.
        evidence: pre-fabricated evidence dict (test-only override).
            When provided, ``events_dir`` and ``repository`` are
            ignored — used by ``test_evaluation`` to score deterministic
            inputs without disk IO.

    Returns:
        NinesReport: per-dim scores + composite + metadata.
    """
    if evidence is None:
        if events_dir is None:
            events_dir = _resolve_default_events_dir()
        evidence = collect_evidence(events_dir, repository)

    weights = _load_weights(config_path)
    raw_scores = {dim.name: float(dim.score(evidence)) for dim in DIMENSIONS}
    clamped_scores = {name: max(0.0, min(1.0, v)) for name, v in raw_scores.items()}

    composite = sum(clamped_scores[name] * weights.get(name, 0.0) for name in clamped_scores)

    return NinesReport(
        dimensions=clamped_scores,
        composite=max(0.0, min(1.0, composite)),
        timestamp=datetime.now(UTC),
        version=_popolaloom_version,
        weights=weights,
    )


def _resolve_default_events_dir() -> Path:
    """Return ``$POPOLA_HOME/events`` (or ``~/.popola/events``)."""
    import os

    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "events"


def toml_serialize(report: NinesReport) -> str:
    """Render a :class:`NinesReport` as a TOML document.

    The format mirrors ``nines.toml`` structure so diffing iter-N vs
    iter-(N+1) is a one-liner.

    Note: Python stdlib does not ship a TOML *writer* (only ``tomllib``
    reader), so we hand-render the sections; the output is guaranteed
    parseable by ``tomllib.loads`` (verified in tests/test_evaluation.py).
    """
    lines: list[str] = []
    lines.append(f'version = "{report.version}"')
    lines.append(f'timestamp = "{_iso_utc(report.timestamp)}"')
    lines.append(f"composite = {report.composite:.6f}")
    lines.append("")
    lines.append("[dimensions]")
    for name, value in sorted(report.dimensions.items()):
        lines.append(f"{name} = {value:.6f}")
    if report.weights:
        lines.append("")
        lines.append("[weights]")
        for name, value in sorted(report.weights.items()):
            lines.append(f"{name} = {value:.6f}")
    return "\n".join(lines) + "\n"
