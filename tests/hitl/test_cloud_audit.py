"""v0.8.7 T2.2.1 — cloud HITL audit-event emission tests.

Covers AC (c)/(d) of T2.2.1 and SECURITY_CHECKLIST §6 (A1–A4) per
``.local/.agent/active/v0.8.7-cloud-hitl-prod/PLAN.md`` §4.2 +
``.../SECURITY_CHECKLIST.md`` §6 audit-log keys table:

- (f) **A1** ``cloud_hitl.requested`` row carries the 8 documented keys
  on a successful :meth:`CloudHITLBridge.submit_request` call (no
  extras — privacy: never the prompt body; no missing — every key
  must land per workspace rule "No Silent Failures").
- (g) **A1 deduped marker** — the second ``submit_request`` inside the
  1-hour window emits ``cloud_hitl.requested`` with ``deduped=True``
  (downstream audit consumers distinguish first-issue from replay).
- (h) **A2 (Lark channel)** ``cloud_hitl.answered`` row carries the 6
  documented keys when :meth:`CloudHITLBridge.submit_answer` wins
  the race via the Lark webhook path (``channel="lark"``).
- (i) **A2 (API channel)** same shape for the direct REST POST path
  (``channel="cloud"`` per the daemon RPC handler — the spec lists
  ``"api"`` informally but the production HITLChannel literal does
  not include it; ``"cloud"`` is the authoritative direct-REST tag).
- (j) **A3** ``cloud_hitl.failed`` row carries the 5 documented keys
  for *every* canonical ``error_kind`` (``timeout``, ``cancelled``,
  ``invalid_context``, ``lark_unreachable``, ``daemon_unreachable``,
  ``internal``) — parametrised over the full
  :data:`CLOUD_HITL_ERROR_KINDS` tuple so adding a 7th kind without
  the matching audit key set fails this test.
- (k) **A4** ``cloud_hitl.transition`` row emitted on the S1 (single
  approve, ``pending → answered``) and S3 (timeout watchdog,
  ``pending → timeout``) state transitions per ``lark-card-spec.md`` §3.
- (l) AC (d) **ordering** — the ``cloud_hitl.failed`` row MUST land
  *before* the bridge returns the error envelope to the caller (or
  the audit chain has a silent gap on early disconnect). We assert
  via a recording event log that flags the failed-append event;
  after :meth:`await_answer` returns, the flag is set.

The tests use a small in-memory :class:`RecordingEventLog` whose
``append`` mirrors the
:func:`popolaloom.hitl.cloud_bridge._safe_append` consumer surface
(just ``append(event_type, data)``) — keeps the suite independent of
the real NDJSON file writer + its background fsync worker thread.

Bridges are built with ``lark_notifier=None`` so the
``lark_unreachable`` audit path doesn't pollute the recording log on
the happy-path tests; the explicit ``record_failure`` call in (j)
exercises every error_kind without needing a real Lark fan-out.
"""

from __future__ import annotations

from importlib import resources
from importlib import resources
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from popolaloom.hitl.cloud_bridge import (
    CLOUD_HITL_ANSWERED_EVENT,
    CLOUD_HITL_ANSWERED_KEYS,
    CLOUD_HITL_ERROR_KINDS,
    CLOUD_HITL_FAILED_EVENT,
    CLOUD_HITL_FAILED_KEYS,
    CLOUD_HITL_REQUESTED_EVENT,
    CLOUD_HITL_REQUESTED_KEYS,
    CLOUD_HITL_TRANSITION_EVENT,
    CLOUD_HITL_TRANSITION_KEYS,
    CloudHITLBridge,
)
from popolaloom.hitl.sync import HITLStore

_MIGRATIONS = ("006_popola_hitl.sql", "007_popola_hitl_metadata.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply both 006 + 007 so ``popola_hitl.metadata`` is available.

    Mirrors :mod:`tests.hitl.test_cloud_bridge_replay` so the bridge can
    exercise the dedup lookup needed by AC (g).
    """
    repo_root = Path(__file__).resolve().parents[2]
    for name in _MIGRATIONS:
        sql = (Path(resources.files("popolaloom.migrations")) / name).read_text(encoding="utf-8")
        conn.executescript(sql)
    conn.commit()


class RecordingEventLog:
    """In-memory audit-event recorder.

    Mirrors the minimal :class:`popolaloom.daemon.event_log.EventLog`
    surface that :func:`popolaloom.hitl.cloud_bridge._safe_append`
    consumes — just an ``append(event_type, data)`` method. Keeps the
    test suite independent of the real NDJSON file writer + its
    background fsync worker thread (the latter would otherwise leak
    into pytest's worker-thread tally).

    The :attr:`failed_seen` flag is set on every
    ``cloud_hitl.failed`` append so the AC (l) ordering test can
    assert the audit row landed *before* the bridge returned to its
    caller (workspace rule "No Silent Failures" — the audit chain
    must be observable in real-time, not after the fact).
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.failed_seen: bool = False

    def append(self, event_type: str, data: dict[str, Any]) -> None:
        # Defensive copy: the bridge reuses the payload dict after
        # _safe_append returns; freezing a snapshot here avoids accidental
        # post-write mutation polluting the audit log.
        self.events.append((event_type, dict(data)))
        if event_type == CLOUD_HITL_FAILED_EVENT:
            self.failed_seen = True

    def filter(self, event_type: str) -> list[dict[str, Any]]:
        """Return all recorded payloads of the given ``event_type``."""
        return [d for et, d in self.events if et == event_type]


@pytest.fixture()
def hitl_store(tmp_path: Path) -> HITLStore:
    """v0.8.7-aware fixture: applies migrations 006 + 007 so the bridge
    can exercise dedup + audit paths without ``has_metadata_column``
    falling back to the v0.8.5 silent-skip warning."""
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return HITLStore(conn)


@pytest.fixture()
def bridge(hitl_store: HITLStore) -> CloudHITLBridge:
    """Bridge with ``lark_notifier=None`` so happy-path tests don't
    accidentally trigger a ``lark_unreachable`` audit event."""
    return CloudHITLBridge(hitl_store, lark_notifier=None)


@pytest.fixture()
def recording_log() -> RecordingEventLog:
    """Fresh recorder per test (no cross-test leakage)."""
    return RecordingEventLog()


_OPTIONS: list[dict[str, str]] = [
    {"id": "yes", "label": "Yes"},
    {"id": "no", "label": "No"},
]
"""Reused HITL option pair (matches :class:`HITLPrompt` ≥ 2-options rule)."""


def _backdate_deadline(
    store: HITLStore, hitl_id: str, *, seconds_in_past: int = 60
) -> None:
    """Set the row's ``deadline_at`` to the past so
    :meth:`CloudHITLBridge.await_answer`'s overdue branch fires
    immediately (avoids real-time waits in the timeout audit tests).
    """
    backdated = (
        (datetime.now(UTC) - timedelta(seconds=seconds_in_past))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    store.conn.execute(
        "UPDATE popola_hitl SET deadline_at = ? WHERE hitl_id = ?",
        (backdated, hitl_id),
    )
    store.conn.commit()


# ── AC (f) — A1 requested keys complete ────────────────────────────────


def test_audit_a1_requested_keys_complete(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """v0.8.7 AC (c) + SECURITY §6 A1: a happy-path
    :meth:`CloudHITLBridge.submit_request` emits exactly one
    ``cloud_hitl.requested`` event with the 8 documented keys.
    No extras (privacy: never the prompt body in audit), no missing
    (every key MUST land per workspace rule "No Silent Failures").
    """
    req = bridge.submit_request(
        task_id="t-a1",
        cursor_agent_id="agent-a1",
        cursor_run_id="run-a1",
        prompt_title="Title",
        prompt_body="Body — this MUST NOT appear in the audit row",
        options=_OPTIONS,
        idempotency_key="a1-key-aaaa",
        event_log=recording_log,
        requester_session="session-a1",
    )

    requested = recording_log.filter(CLOUD_HITL_REQUESTED_EVENT)
    assert len(requested) == 1, f"expected exactly 1 row, got {len(requested)}"
    row = requested[0]

    # AC: exactly the 8 keys, no extras, no missing.
    assert set(row.keys()) == set(CLOUD_HITL_REQUESTED_KEYS), (
        f"key set mismatch: got {sorted(row.keys())} "
        f"expected {sorted(CLOUD_HITL_REQUESTED_KEYS)}"
    )

    # Per-key value checks (the 8-tuple verbatim).
    assert row["hitl_id"] == req.hitl_id
    assert row["task_id"] == "t-a1"
    assert row["cursor_agent_id"] == "agent-a1"
    assert row["cursor_run_id"] == "run-a1"
    assert row["idempotency_key"] == "a1-key-aaaa"
    assert row["deduped"] is False
    assert isinstance(row["requested_at"], str) and row["requested_at"]
    assert row["requester_session"] == "session-a1"

    # Privacy invariant — the prompt body MUST NOT leak into the audit row
    # (per SECURITY §6 A1 — the prompt body lives in popola_hitl.prompt_json
    # only, not in the NDJSON event log line).
    audit_blob = repr(row)
    assert "MUST NOT appear" not in audit_blob, (
        "prompt body leaked into audit row — SECURITY §6 violation"
    )


def test_audit_a1_requester_session_defaults_to_unknown(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """When the caller omits ``requester_session``, the audit row carries
    the literal ``"unknown"`` (informational fallback for γ deployments
    that are loopback-only — β / SaaS gateways MUST forward an explicit
    ``X-Real-IP``)."""
    bridge.submit_request(
        task_id="t-a1-default",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=recording_log,
    )
    requested = recording_log.filter(CLOUD_HITL_REQUESTED_EVENT)
    assert len(requested) == 1
    assert requested[0]["requester_session"] == "unknown"


# ── AC (g) — A1 deduped marker on replay ───────────────────────────────


def test_audit_a1_deduped_replay_marker_present(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """v0.8.7 + SECURITY §5 R2: a replay inside the 1-hour idempotency
    window emits a *second* ``cloud_hitl.requested`` event with
    ``deduped=True``. Audit consumers can therefore distinguish a
    first-issue from a replay without re-running the dedup query.

    Both calls supply identical context tuples + the same explicit
    ``idempotency_key``; only the second one's ``deduped`` flag
    differs (the bridge short-circuits to the existing
    ``hitl_id`` per :meth:`lookup_by_idempotency_key`).
    """
    first = bridge.submit_request(
        task_id="t-dedup",
        cursor_agent_id="agent-dedup",
        cursor_run_id="run-dedup",
        prompt_title="Title",
        prompt_body="Same body?",
        options=_OPTIONS,
        idempotency_key="dedup-key-zzzz",
        event_log=recording_log,
    )
    second = bridge.submit_request(
        task_id="t-dedup",
        cursor_agent_id="agent-dedup",
        cursor_run_id="run-dedup",
        prompt_title="Title",
        prompt_body="Same body?",
        options=_OPTIONS,
        idempotency_key="dedup-key-zzzz",
        event_log=recording_log,
    )

    requested = recording_log.filter(CLOUD_HITL_REQUESTED_EVENT)
    assert len(requested) == 2, f"expected 2 rows, got {len(requested)}"

    assert requested[0]["deduped"] is False
    assert requested[1]["deduped"] is True
    # The dedup hit reuses the existing hitl_id (proves single-row contract).
    assert requested[0]["hitl_id"] == first.hitl_id
    assert requested[1]["hitl_id"] == second.hitl_id == first.hitl_id

    # Both rows still carry the full A1 key set — the deduped path MUST
    # NOT skip audit fields.
    for row in requested:
        assert set(row.keys()) == set(CLOUD_HITL_REQUESTED_KEYS)


# ── AC (h) — A2 answered keys complete (Lark channel) ──────────────────


def test_audit_a2_answered_keys_complete_lark_channel(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """v0.8.7 + SECURITY §6 A2: a successful :meth:`submit_answer` via
    the Lark webhook path emits exactly one ``cloud_hitl.answered``
    event with the 6 documented keys. ``custom_text_present`` is the
    boolean signal that the operator typed a custom answer (the audit
    row records ONLY the boolean — the prose lives in the SQLite
    ``answer_reason`` column to bound log size per SECURITY §6 A2).
    """
    req = bridge.submit_request(
        task_id="t-ans-lark",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=recording_log,
    )
    ok, descriptor = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="open_id_lark_user_42",
        channel="lark",
        reason="Looks good to me — proceeding",
        event_log=recording_log,
    )
    assert ok is True
    assert descriptor == "lark"

    answered = recording_log.filter(CLOUD_HITL_ANSWERED_EVENT)
    assert len(answered) == 1
    row = answered[0]

    assert set(row.keys()) == set(CLOUD_HITL_ANSWERED_KEYS), (
        f"key set mismatch: got {sorted(row.keys())} "
        f"expected {sorted(CLOUD_HITL_ANSWERED_KEYS)}"
    )

    assert row["hitl_id"] == req.hitl_id
    assert row["answered_by"] == "open_id_lark_user_42"
    assert row["channel"] == "lark"
    assert row["option_id"] == "yes"
    assert row["custom_text_present"] is True
    assert isinstance(row["answered_at"], str) and row["answered_at"]

    # Privacy invariant — the full reason text MUST NOT leak into the
    # audit row (per SECURITY §6 A2 — only the boolean signal).
    audit_blob = repr(row)
    assert "Looks good to me" not in audit_blob, (
        "reason prose leaked into audit row — SECURITY §6 violation"
    )


# ── AC (i) — A2 answered keys complete (direct REST API channel) ───────


def test_audit_a2_answered_keys_complete_api_channel(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """v0.8.7 + SECURITY §6 A2: same shape for the direct REST POST
    ``/hitl/cloud/answer/{hitl_id}`` path. The HITLChannel literal
    does not include ``"api"`` (the spec mention is informal); the
    daemon's ``hitl_cloud_answer`` route narrows the wire string into
    one of ``{lark, ide, cli, mcp, web, cloud, email, signal}`` —
    ``"cloud"`` is the canonical direct-REST tag (see
    ``daemon/rpc.py`` :func:`hitl_cloud_answer`).

    ``custom_text_present`` is ``False`` here because ``reason=None``
    (proves the boolean correctly negates).
    """
    req = bridge.submit_request(
        task_id="t-ans-api",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=recording_log,
    )
    ok, descriptor = bridge.submit_answer(
        req.hitl_id,
        "no",
        responder_id="api-client-7",
        channel="cloud",  # the canonical direct-REST channel
        reason=None,
        event_log=recording_log,
    )
    assert ok is True
    assert descriptor == "cloud"

    answered = recording_log.filter(CLOUD_HITL_ANSWERED_EVENT)
    assert len(answered) == 1
    row = answered[0]

    assert set(row.keys()) == set(CLOUD_HITL_ANSWERED_KEYS)
    assert row["hitl_id"] == req.hitl_id
    assert row["answered_by"] == "api-client-7"
    assert row["channel"] == "cloud"
    assert row["option_id"] == "no"
    assert row["custom_text_present"] is False
    assert isinstance(row["answered_at"], str) and row["answered_at"]


def test_audit_a2_custom_text_present_false_on_whitespace_reason(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """Edge case: a reason that's only whitespace MUST NOT count as
    ``custom_text_present=True`` — operators sometimes click "Custom…"
    by mistake and submit an empty string."""
    req = bridge.submit_request(
        task_id="t-ws",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=recording_log,
    )
    ok, _ = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="ws-clicker",
        channel="lark",
        reason="   \t  \n  ",  # whitespace-only
        event_log=recording_log,
    )
    assert ok is True
    answered = recording_log.filter(CLOUD_HITL_ANSWERED_EVENT)
    assert len(answered) == 1
    assert answered[0]["custom_text_present"] is False


# ── AC (j) — A3 failed keys complete for every error_kind ──────────────


@pytest.mark.parametrize("error_kind", list(CLOUD_HITL_ERROR_KINDS))
def test_audit_a3_failed_keys_complete_all_error_kinds(
    bridge: CloudHITLBridge,
    recording_log: RecordingEventLog,
    error_kind: str,
) -> None:
    """v0.8.7 AC (c) + SECURITY §6 A3: every error_kind in
    :data:`CLOUD_HITL_ERROR_KINDS` emits a ``cloud_hitl.failed`` row
    with the 5 documented keys. Parametrised over ALL canonical kinds
    so adding a 7th kind without the matching audit hook fails this
    test (workspace rule "No Silent Failures" — the audit chain must
    have zero gaps).

    Note: the "rejection-is-not-an-error" case
    (``option_id="reject"``) is NOT a failure — per
    ``mcp-tool-contract.md`` §7 row 5 a reject click is a SUCCESSFUL
    answer with ``option_id="reject"`` and lands as
    :data:`CLOUD_HITL_ANSWERED_EVENT`. The
    ``test_audit_a2_answered_keys_complete_*`` tests above cover that
    path; this parametrisation focuses strictly on the 6 canonical
    error kinds.
    """
    payload = bridge.record_failure(
        hitl_id="hitl-id-stub",
        error_kind=error_kind,
        attempt=1,
        event_log=recording_log,
    )

    # The returned payload mirrors what _safe_append wrote.
    assert set(payload.keys()) == set(CLOUD_HITL_FAILED_KEYS)
    assert payload["error_kind"] == error_kind

    failed_rows = recording_log.filter(CLOUD_HITL_FAILED_EVENT)
    assert len(failed_rows) == 1, (
        f"expected 1 row for error_kind={error_kind}, got {len(failed_rows)}"
    )
    row = failed_rows[0]
    assert set(row.keys()) == set(CLOUD_HITL_FAILED_KEYS)
    assert row["hitl_id"] == "hitl-id-stub"
    assert row["error_kind"] == error_kind
    assert row["attempt"] == 1
    assert row["hitl_id_if_known"] == "hitl-id-stub"
    assert isinstance(row["failed_at"], str) and row["failed_at"]


def test_audit_a3_unknown_error_kind_rejected(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """Defense-in-depth: unknown ``error_kind`` strings raise
    :class:`ValueError` (No Silent Failures — misspelled kinds MUST NOT
    land in the audit log as noise). Mirrors the bridge's explicit
    :data:`CLOUD_HITL_ERROR_KINDS` allow-list."""
    with pytest.raises(ValueError) as excinfo:
        bridge.record_failure(
            hitl_id="h",
            error_kind="not-a-real-kind",
            event_log=recording_log,
        )
    assert "not-a-real-kind" in str(excinfo.value)
    assert recording_log.filter(CLOUD_HITL_FAILED_EVENT) == []


def test_audit_a3_failed_with_no_hitl_id_records_empty_string(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """For very-early ``invalid_context`` failures fired before a row
    is created, the audit row records ``hitl_id=""`` plus
    ``hitl_id_if_known=None`` so consumers can distinguish "no row
    created" from "row exists but id is empty string"."""
    payload = bridge.record_failure(
        hitl_id=None,
        error_kind="invalid_context",
        event_log=recording_log,
    )
    assert payload["hitl_id"] == ""
    assert payload["hitl_id_if_known"] is None
    failed_rows = recording_log.filter(CLOUD_HITL_FAILED_EVENT)
    assert len(failed_rows) == 1
    assert failed_rows[0]["hitl_id"] == ""
    assert failed_rows[0]["hitl_id_if_known"] is None


# ── AC (k) — A4 transition rows for S1 + S3 paths ──────────────────────


def test_audit_a4_transition_emitted_for_s1_single_approve(
    bridge: CloudHITLBridge, recording_log: RecordingEventLog
) -> None:
    """v0.8.7 + SECURITY §6 A4: the S1 single-approver path emits one
    ``cloud_hitl.transition`` row with ``from_state="pending"`` and
    ``to_state="answered"``. ``actor`` is the responder's id (a human
    drove the transition) and the 5 documented keys are present.
    """
    req = bridge.submit_request(
        task_id="t-s1",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=recording_log,
    )
    ok, _ = bridge.submit_answer(
        req.hitl_id,
        "yes",
        responder_id="single-approver",
        channel="lark",
        event_log=recording_log,
    )
    assert ok is True

    transitions = recording_log.filter(CLOUD_HITL_TRANSITION_EVENT)
    assert len(transitions) == 1, (
        f"expected 1 transition row for S1, got {len(transitions)}"
    )
    row = transitions[0]
    assert set(row.keys()) == set(CLOUD_HITL_TRANSITION_KEYS)
    assert row["hitl_id"] == req.hitl_id
    assert row["from_state"] == "pending"
    assert row["to_state"] == "answered"
    assert row["actor"] == "single-approver"
    assert isinstance(row["transitioned_at"], str) and row["transitioned_at"]


def test_audit_a4_transition_emitted_for_s3_timeout(
    bridge: CloudHITLBridge,
    recording_log: RecordingEventLog,
    hitl_store: HITLStore,
) -> None:
    """v0.8.7 + SECURITY §6 A4: the S3 timeout-watchdog path emits one
    ``cloud_hitl.transition`` row with ``from_state="pending"`` and
    ``to_state="timeout"``. ``actor`` is ``None`` (the system itself
    drove the transition — there is no human responder for a
    deadline-expiry). A matching ``cloud_hitl.failed`` row with
    ``error_kind="timeout"`` lands alongside (per AC d — the audit
    pair is atomic).
    """
    req = bridge.submit_request(
        task_id="t-s3",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=recording_log,
    )
    _backdate_deadline(hitl_store, req.hitl_id, seconds_in_past=60)

    seen = bridge.await_answer(
        req.hitl_id,
        timeout_s=2.0,
        poll_interval_s=0.05,
        event_log=recording_log,
    )
    # Per await_answer's contract, a timeout returns None (the daemon's
    # /hitl/cloud/wait route translates this to status=timeout).
    assert seen is None

    transitions = recording_log.filter(CLOUD_HITL_TRANSITION_EVENT)
    assert len(transitions) == 1, (
        f"expected 1 transition row for S3, got {len(transitions)}"
    )
    transition = transitions[0]
    assert set(transition.keys()) == set(CLOUD_HITL_TRANSITION_KEYS)
    assert transition["hitl_id"] == req.hitl_id
    assert transition["from_state"] == "pending"
    assert transition["to_state"] == "timeout"
    assert transition["actor"] is None  # system-driven, no human responder

    # The matching A3 failed row also lands (paired with the A4 transition).
    failed = recording_log.filter(CLOUD_HITL_FAILED_EVENT)
    assert len(failed) == 1
    assert failed[0]["error_kind"] == "timeout"
    assert failed[0]["hitl_id"] == req.hitl_id


# ── AC (l) — failed audit lands BEFORE bridge returns the error envelope


def test_audit_failed_emitted_BEFORE_error_envelope_returned(  # noqa: N802
    bridge: CloudHITLBridge, hitl_store: HITLStore
) -> None:
    """v0.8.7 AC (d) + SECURITY §6 A3: the audit row MUST land BEFORE
    the bridge returns the error envelope to its caller — otherwise
    an early disconnect (e.g., the cloud agent dropping its long-poll
    half-way through) would lose the audit trail.

    We assert via a recording log whose ``append`` sets a flag on
    every ``cloud_hitl.failed`` event; after :meth:`await_answer`
    returns we assert the flag is set, proving the failure was
    logged in-line (not async / deferred).
    """
    flag_log = RecordingEventLog()
    req = bridge.submit_request(
        task_id="t-order",
        cursor_agent_id="ag",
        cursor_run_id="rn",
        prompt_title="t",
        prompt_body="b",
        options=_OPTIONS,
        event_log=flag_log,
    )
    _backdate_deadline(hitl_store, req.hitl_id, seconds_in_past=300)

    # Sanity: the flag is NOT set before the timeout-triggering call —
    # submit_request emits cloud_hitl.requested only, never cloud_hitl.failed
    # (the lark_notifier=None path skips the lark_unreachable branch).
    assert flag_log.failed_seen is False

    seen = bridge.await_answer(
        req.hitl_id,
        timeout_s=2.0,
        poll_interval_s=0.05,
        event_log=flag_log,
    )
    assert seen is None  # timeout path → None

    # AC (l) — flag MUST be set; the audit row was appended in
    # mark_timeout (called from inside await_answer) BEFORE the return
    # statement landed. If this assertion ever flakes, an audit
    # ordering regression has been introduced and the audit chain has
    # a silent gap.
    assert flag_log.failed_seen is True, (
        "cloud_hitl.failed was not emitted before await_answer returned; "
        "audit chain has a silent gap (workspace rule violation)"
    )

    # Defense-in-depth: the recorded row carries error_kind="timeout"
    # and the original hitl_id (proves we captured the right failure).
    failed_rows = flag_log.filter(CLOUD_HITL_FAILED_EVENT)
    assert len(failed_rows) == 1
    assert failed_rows[0]["error_kind"] == "timeout"
    assert failed_rows[0]["hitl_id"] == req.hitl_id

    # The transition row landed in the same window (paired with failure).
    transitions = flag_log.filter(CLOUD_HITL_TRANSITION_EVENT)
    assert len(transitions) == 1
    assert transitions[0]["from_state"] == "pending"
    assert transitions[0]["to_state"] == "timeout"
