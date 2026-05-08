"""Cloud-agent HITL bridge (v0.8.5 spike-poc Wave 3 / Stage 3 + v0.8.7 T2.1.3).

Cursor Cloud Agents invoke PopolaLoom daemon RPC endpoints to persist a HITL
row in ``popola_hitl`` (:class:`~popolaloom.hitl.sync.HITLStore`), optionally
fan out via Lark (:func:`~popolaloom.hitl.renderers.lark.send_lark_card`), and
later collect the reply through the existing cross-channel first-responder wins
semantics in :meth:`HITLStore.mark_answered`.

v0.8.7 T2.1.3 extensions
------------------------

Per ``.local/.agent/active/v0.8.7-cloud-hitl-prod/PLAN.md`` §4.1 T2.1.3 +
``.local/research/v0.8.7_hitl/mcp-tool-contract.md`` §5 idempotency design:

- :meth:`CloudHITLBridge.submit_request` accepts an ``idempotency_key`` keyword
  (default ``None`` → ``sha256(f"{task_id}|{cursor_agent_id}|{cursor_run_id}|"
  f"{prompt_body}").hexdigest()[:32]``) and persists it (alongside the
  ``cursor_*`` tuple and caller-supplied metadata) into the new
  ``popola_hitl.metadata`` JSON column added by migration ``007``.
- Replays inside the 1-hour window short-circuit via SQL-only dedup lookup
  (``json_extract(metadata, '$.idempotency_key')``) → returns the existing
  :class:`CloudHITLRequest` with ``deduped=True``; no new card sent. Per
  SECURITY R3 the lookup MUST be SQL (no in-memory cache that would not
  survive a daemon restart) — the SQLite JSON1 extension is required and
  smoke-checked at module import time per R2 mitigation.
- :meth:`CloudHITLBridge.submit_answer` gains optional ``expected_cursor_*``
  kwargs for mis-route defense: when provided AND mismatched against the
  row's stored cursor tuple, the call rejects with ``ok=False`` plus a
  ``"mis-route"`` descriptor (the daemon answer handler / Lark listener
  translates that to HTTP 400 — Lark webhooks MUST NOT be able to answer
  rows owned by a different ``cursor_run_id``). ``mark_answered`` itself is
  untouched (sole-writer invariant I-4).

Backward compatibility
----------------------

When the connection's ``popola_hitl`` schema lacks the ``metadata`` column
(e.g., a v0.8.5 test fixture that only applies migration 006), the bridge
detects this at construction time, logs a one-time warning, and silently
disables metadata writes / dedup lookups. Existing v0.8.5 tests
(``tests/hitl/test_cloud_bridge.py``, ``tests/hitl/test_cloud_bridge_coverage.py``)
continue to pass without modification — the documented degradation is the
absence of dedup, not a hard error (per workspace rule "No Silent Failures",
the warning surfaces the missing migration explicitly rather than coercing).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

from popolaloom.hitl import (
    HITLChannel,
    HITLOption,
    HITLPrompt,
    HITLReply,
    HITLStore,
)

logger = logging.getLogger(__name__)


# ── v0.8.7 T2.2.1: audit event types + key sets ──────────────────────────
#
# Per ``SECURITY_CHECKLIST.md`` §6 (A1–A4) — the bridge MUST emit one
# NDJSON envelope per HITL state change so the audit chain has zero
# silent gaps (workspace rule "No Silent Failures"). Key sets are listed
# here as constants so reviewers can grep the contract → call sites in
# one hop.

CLOUD_HITL_REQUESTED_EVENT: Final[str] = "cloud_hitl.requested"
"""NDJSON event type for A1 (every HITL request creation)."""

CLOUD_HITL_ANSWERED_EVENT: Final[str] = "cloud_hitl.answered"
"""NDJSON event type for A2 (every successful answer write)."""

CLOUD_HITL_FAILED_EVENT: Final[str] = "cloud_hitl.failed"
"""NDJSON event type for A3 (every error envelope path)."""

CLOUD_HITL_TRANSITION_EVENT: Final[str] = "cloud_hitl.transition"
"""NDJSON event type for A4 (every row state-machine transition)."""

CLOUD_HITL_REQUESTED_KEYS: Final[tuple[str, ...]] = (
    "hitl_id",
    "task_id",
    "cursor_agent_id",
    "cursor_run_id",
    "idempotency_key",
    "deduped",
    "requested_at",
    "requester_session",
)
"""8 keys for A1 — every key MUST be present in the emitted dict.

``requester_session`` defaults to ``"unknown"`` when the caller does not
supply a peer-addr / session id (γ deployments are loopback-only so this
is informational; β / SaaS gateways MUST forward an explicit value)."""

CLOUD_HITL_ANSWERED_KEYS: Final[tuple[str, ...]] = (
    "hitl_id",
    "answered_by",
    "answered_at",
    "channel",
    "option_id",
    "custom_text_present",
)
"""6 keys for A2. ``custom_text_present`` is a boolean that signals whether
the operator typed a custom answer (no full reason text in the audit row
to keep log size bounded; the full text remains on the SQLite row)."""

CLOUD_HITL_FAILED_KEYS: Final[tuple[str, ...]] = (
    "hitl_id",
    "error_kind",
    "failed_at",
    "attempt",
    "hitl_id_if_known",
)
"""5 keys for A3. ``hitl_id`` carries the row id (or ``""`` when the row
was never created — e.g., a very-early ``invalid_context`` rejection);
``hitl_id_if_known`` mirrors that as ``str | None`` so consumers can
distinguish ``"no row"`` from ``"row exists but empty id"``."""

CLOUD_HITL_TRANSITION_KEYS: Final[tuple[str, ...]] = (
    "hitl_id",
    "from_state",
    "to_state",
    "transitioned_at",
    "actor",
)
"""5 keys for A4. ``actor`` is the open_id / responder id (``None`` when
the system itself drove the transition — e.g., the timeout watchdog)."""

CLOUD_HITL_ERROR_KINDS: Final[tuple[str, ...]] = (
    "timeout",
    "cancelled",
    "invalid_context",
    "lark_unreachable",
    "daemon_unreachable",
    "internal",
)
"""6 canonical error kinds per :doc:`mcp-tool-contract` §3.3 enum.

Test fixtures parameterise over this tuple to enforce A3 row keys are
emitted for *every* error path (no silent gaps in the audit chain)."""


EventLogResolver = Callable[[str], Any]
"""Resolver shape: ``(task_id) -> EventLog | None``.

Wired by :mod:`popolaloom.daemon.main` at startup so the bridge can fall
back to a per-task event log when the immediate caller (rpc.py) does not
pass one explicitly. Production resolver is :meth:`Popolad.event_log`."""


_CLOUD_HITL_DEFAULTS: dict[str, Any] = {
    "default_timeout_s": 600.0,
    "idempotency_window_s": 3600,
    "event_log_resolver": None,
}
"""Module-level config wired by :func:`configure_cloud_hitl_defaults`.

Initialised to the v0.8.5 defaults so importers without explicit config
keep the historical behavior. ``daemon/main.py`` overwrites these at
startup using the values parsed from ``popolad.toml`` ``[hitl.cloud]``
(see :data:`CLOUD_HITL_IDEMPOTENCY_WINDOW_S` below for the canonical
public re-export of the window default).
"""


def configure_cloud_hitl_defaults(
    *,
    default_timeout_s: float | None = None,
    idempotency_window_s: int | None = None,
    event_log_resolver: EventLogResolver | None = None,
) -> None:
    """Wire ``[hitl.cloud]`` defaults onto the bridge module (T2.2.1 AC b).

    Intended single caller: :func:`popolaloom.daemon.main._apply_cloud_hitl_config`.
    Tests may also call this to inject a stub resolver. Each kwarg defaults
    to ``None``; passing ``None`` leaves the corresponding entry unchanged
    so partial overrides compose (e.g., a unit test that only wants to
    swap the resolver does not have to re-state the timeout default).
    """
    if default_timeout_s is not None:
        _CLOUD_HITL_DEFAULTS["default_timeout_s"] = float(default_timeout_s)
    if idempotency_window_s is not None:
        _CLOUD_HITL_DEFAULTS["idempotency_window_s"] = int(idempotency_window_s)
    if event_log_resolver is not None:
        _CLOUD_HITL_DEFAULTS["event_log_resolver"] = event_log_resolver


def _resolve_event_log_for_task(task_id: str | None) -> Any:
    """Best-effort event-log lookup via the daemon-injected resolver.

    Returns ``None`` when no resolver is wired or the resolver raises;
    callers MUST tolerate ``None`` (audit emission becomes a no-op).
    """
    resolver = _CLOUD_HITL_DEFAULTS.get("event_log_resolver")
    if resolver is None or not task_id:
        return None
    try:
        return resolver(task_id)
    except Exception:
        logger.exception(
            "cloud_hitl event_log_resolver failed for task_id=%s", task_id
        )
        return None


def _utc_iso_now() -> str:
    """ISO 8601 UTC timestamp with millisecond precision (matches EventLog)."""
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe_append(event_log: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort ``EventLog.append`` that swallows + logs exceptions.

    Per workspace rule "No Silent Failures": the audit chain MUST be
    observable, so when the underlying log raises (e.g., closed fd,
    disk full) we surface the failure via :data:`logger` rather than
    crashing the request path. Callers above the bridge see the same
    HITL outcome as before.
    """
    if event_log is None:
        return
    try:
        event_log.append(event_type, payload)
    except Exception:
        logger.exception(
            "cloud_hitl audit append failed type=%s hitl_id=%s",
            event_type,
            payload.get("hitl_id"),
        )


# ── Module-import smoke check (R2 mitigation) ────────────────────────────


def _verify_sqlite_json1() -> None:
    """Fail loudly at import time when SQLite is built without JSON1.

    Per ``SECURITY_CHECKLIST.md`` §11 R2 mitigation: a Python build without
    the JSON1 extension would silently fail the dedup lookup (``OperationalError:
    no such function: json_extract``). We surface that at module import so the
    operator sees the broken install before the first cloud HITL request.

    This is also the smoke contract referenced in ``PLAN.md`` §4.1 T2.1.3
    impl notes ("verify it's compiled in via
    ``sqlite3.connect(":memory:").execute("SELECT json('1')")`` smoke at
    module import"). The probe runs once at import; failure is escalated as
    :class:`RuntimeError` (No Silent Failures rule).
    """
    try:
        with sqlite3.connect(":memory:") as probe:
            probe.execute("SELECT json('1')")
            probe.execute("SELECT json_extract('{\"k\":\"v\"}', '$.k')")
    except sqlite3.OperationalError as exc:  # pragma: no cover - hard to repro
        raise RuntimeError(
            "popolaloom.hitl.cloud_bridge requires SQLite with the JSON1 "
            "extension compiled in (used for the cloud HITL idempotency "
            "dedup lookup per v0.8.7 T2.1.3). Detected SQLite version: "
            f"{sqlite3.sqlite_version}. See SECURITY_CHECKLIST §11 R2 / "
            "docs/known-issues.md for the supported Python builds."
        ) from exc


_verify_sqlite_json1()


# ── Public dedup constants ───────────────────────────────────────────────


CLOUD_HITL_IDEMPOTENCY_WINDOW_S: int = 3600
"""Default rolling dedup window for the v0.8.7 cloud HITL replay defense.

Sourced from :doc:`mcp-tool-contract` §5 idempotency design table
("1 hour rolling; configurable via ``cloud_hitl.idempotency_window_s``").
Configurable per call by passing ``idempotency_window_s=`` to
:meth:`CloudHITLBridge.submit_request` or
:meth:`CloudHITLBridge.lookup_by_idempotency_key`."""


CLOUD_HITL_IDEMPOTENCY_KEY_MAX_LEN: int = 128
"""Hard cap on caller-supplied ``idempotency_key`` length.

Mirrors :doc:`mcp-tool-contract` §3.1 (``"idempotency_key": { "maxLength": 128 }``)
+ SECURITY R1 (key opacity bound). Auto-derived keys are 32 hex chars so
they comfortably fit; this cap rejects keys that would otherwise bloat the
metadata JSON or hint at non-opaque payloads (Q-B-4 dedup cap)."""


@dataclass(frozen=True)
class CloudHITLRequest:
    """Outbound view of a cloud-agent HITL submission.

    Rows are keyed by ``hitl_id``. The authoritative prompt lives in SQLite
    (``prompt_json``) as a validated :class:`HITLPrompt`. v0.8.7 adds the
    ``deduped`` flag — ``True`` when the daemon short-circuited a replay
    inside :data:`CLOUD_HITL_IDEMPOTENCY_WINDOW_S`.
    """

    hitl_id: str
    task_id: str
    cursor_agent_id: str | None
    cursor_run_id: str | None
    prompt: HITLPrompt
    options: tuple[HITLOption, ...]
    created_at: datetime
    deadline_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict[str, Any])
    deduped: bool = False
    """``True`` iff this request was returned by a 1-hour-window dedup hit
    (see :meth:`CloudHITLBridge.submit_request`). v0.8.7 T2.1.3."""

    lark_dispatched: bool = True
    """``True`` iff the Lark fan-out for this request did NOT raise
    ``lark_unreachable`` during :meth:`CloudHITLBridge.submit_request`.

    v0.8.7 M3 (REVIEW.md). Plumbed through :class:`CloudHITLRequestResponse`
    so the MCP tool can flip ``error.code`` from ``timeout`` to
    ``lark_unreachable`` per contract §7 row 4 when the row was created
    but the card never reached the human. Defaults to ``True`` so the
    bridge's pre-v0.8.7 callers (no notifier wired) read the historical
    "delivery succeeded" semantics."""


class CloudHITLLarkNotifier(Protocol):
    """Injectable Lark fan-out facade (production calls ``send_lark_card``)."""

    def send_hitl_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        event_log: Any | None = None,
        task_id: str | None = None,
    ) -> Any:
        """Best-effort HITL card send; may raise."""


class _DefaultCloudLarkNotifier:
    """v0.8.7 production cloud-HITL Lark notifier (B2 wiring).

    Per REVIEW.md finding **B2**, the production notifier MUST render
    the v1 versioned card (``cloud_hitl_request_card_v1`` per
    ``lark-card-spec.md`` §2.3) instead of falling through to the
    legacy generic HITL card. This implementation:

    1. Looks up the row's ``metadata`` JSON column on the bridge's
       store (so we can populate ``cursor_agent_id`` / ``cursor_run_id``
       / ``idempotency_key`` / ``context_summary`` per spec §2.4).
    2. Builds a typed :class:`CloudHITLCardInput` (allowlist pattern
       per spec §6.1 — no ad-hoc ``dict[str, Any]``).
    3. Renders the v1 envelope via :func:`build_cloud_hitl_card`.
    4. Passes the rendered dict as ``card_payload=`` to
       :func:`send_lark_card` so the v1 card actually goes out the
       wire (rather than the legacy ``build_card_payload`` fallback).

    Any failure on the render path is logged + falls back to the legacy
    notifier so a Lark delivery still happens; the bridge's
    ``record_failure(lark_unreachable)`` machinery handles the
    user-facing failure surface upstream of this notifier.
    """

    def __init__(self, store: HITLStore | None = None) -> None:
        self._store = store

    def send_hitl_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        event_log: Any | None = None,
        task_id: str | None = None,
    ) -> Any:
        from popolaloom.hitl.renderers.lark import send_lark_card
        from popolaloom.lark.cloud_hitl_card import (
            CloudHITLCardInput,
            build_cloud_hitl_card,
        )

        card_payload: dict[str, Any] | None = None
        if self._store is not None:
            try:
                card_payload = self._render_v1_card(
                    prompt,
                    hitl_id=hitl_id,
                    task_id=task_id,
                    builder=build_cloud_hitl_card,
                    input_cls=CloudHITLCardInput,
                )
            except Exception:
                logger.exception(
                    "_DefaultCloudLarkNotifier: v1 card render failed for "
                    "hitl_id=%s; falling back to legacy generic card",
                    hitl_id,
                )
                card_payload = None

        return send_lark_card(
            prompt,
            event_log=event_log,
            card_payload=card_payload,
        )

    def _render_v1_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        task_id: str | None,
        builder: Callable[..., dict[str, Any]],
        input_cls: type,
    ) -> dict[str, Any]:
        """Build the v1 cloud HITL card from the persisted row (B2 helper).

        Reads the ``popola_hitl.metadata`` JSON column to populate the
        ``card_metadata`` block per spec §2.4 (12 keys including
        ``template_version``, ``idempotency_key``, ``cursor_*``).
        Falls back to deriving an idempotency key from the prompt body
        when metadata is absent (legacy rows pre-migration 007).
        """
        if self._store is None:
            raise RuntimeError(
                "_DefaultCloudLarkNotifier._render_v1_card called without a store"
            )
        row = self._store.get(hitl_id)
        if row is None:  # pragma: no cover - defensive
            raise LookupError(
                f"_DefaultCloudLarkNotifier: row {hitl_id!r} missing at render time"
            )
        metadata_raw = row.get("metadata")
        metadata: dict[str, Any]
        if isinstance(metadata_raw, str) and metadata_raw:
            try:
                parsed = json.loads(metadata_raw)
                metadata = dict(parsed) if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                metadata = {}
        elif isinstance(metadata_raw, dict):
            metadata = dict(metadata_raw)
        else:
            metadata = {}

        deadline_str = str(row.get("deadline_at") or "")
        deadline_at = (
            _parse_isoformat(deadline_str)
            if deadline_str
            else datetime.now(UTC)  # pragma: no cover - defensive
        )

        idempotency_key_raw = metadata.get("idempotency_key")
        if not isinstance(idempotency_key_raw, str) or not idempotency_key_raw:
            # Legacy row without metadata 007 column: derive an opaque
            # display key from the prompt body so the v1 card still
            # carries a normalised metadata.idempotency_key.
            idempotency_key_raw = hashlib.sha256(
                (prompt.what or "").encode("utf-8")
            ).hexdigest()[:32]

        question_text = prompt.what or ""
        # Spec §2.3 B1 forbids questions ≥ 2000 chars; truncate at the
        # builder boundary if a legacy row carries an oversize question
        # (the bridge's submit_request validates pre-write but a
        # migration-rewritten row could violate this — fail-safe to a
        # truncated question + explicit warning).
        if len(question_text) >= 2000:
            logger.warning(
                "_DefaultCloudLarkNotifier: question_text %d chars >= 2000 "
                "for hitl_id=%s; truncating at v1 card boundary",
                len(question_text),
                hitl_id,
            )
            question_text = question_text[:1990] + "…"

        context_summary_raw = metadata.get("context_summary")
        prompt_body = (
            str(context_summary_raw)
            if isinstance(context_summary_raw, str) and context_summary_raw
            else question_text
        )

        cursor_agent_id = metadata.get("cursor_agent_id")
        cursor_run_id = metadata.get("cursor_run_id")
        cursor_agent_id_str = (
            str(cursor_agent_id) if isinstance(cursor_agent_id, str) else None
        )
        cursor_run_id_str = (
            str(cursor_run_id) if isinstance(cursor_run_id, str) else None
        )

        timeout_seconds_raw = prompt.deadline_seconds or 1800
        try:
            timeout_seconds = int(timeout_seconds_raw)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            timeout_seconds = 1800

        card_input = input_cls(
            hitl_id=hitl_id,
            task_id=task_id or str(row.get("task_id") or ""),
            question_text=question_text,
            prompt_body=prompt_body,
            cursor_agent_id=cursor_agent_id_str,
            cursor_run_id=cursor_run_id_str,
            idempotency_key=idempotency_key_raw,
            expiration_at=deadline_at,
            timeout_seconds=timeout_seconds,
        )
        return builder(card_input)


class _NoopCloudLarkNotifier:
    """Null sender for ``build_default_bridge`` when no notifier is provided."""

    def send_hitl_card(
        self,
        prompt: HITLPrompt,
        *,
        hitl_id: str,
        event_log: Any | None = None,
        task_id: str | None = None,
    ) -> Any:
        _ = prompt, hitl_id, event_log, task_id
        return None


def _ceil_deadline_seconds(timeout_s: float | None, default: float) -> int:
    """Clamp prompt ``deadline_seconds`` to (1, 86400] per :class:`HITLPrompt`."""
    raw = float(default if timeout_s is None else timeout_s)
    return max(1, min(86400, int(math.ceil(raw))))


def _build_cloud_prompt(
    *,
    prompt_title: str,
    prompt_body: str,
    options: list[dict[str, str]],
    task_id: str,
    cursor_agent_id: str | None,
    cursor_run_id: str | None,
    metadata: dict[str, Any] | None,
    deadline_seconds: int,
) -> HITLPrompt:
    """Build a store-safe :class:`HITLPrompt` for cloud-agent approvals.

    The DB ``trigger`` column holds ``approval`` (schema-valid). Cloud origin
    and caller metadata are embedded in ``why`` so prompts round-trip in SQLite
    without ``extra=`` fields.
    """
    hitl_options = [HITLOption(id=o["id"], label=o["label"]) for o in options]
    default_id = hitl_options[0].id
    ctx_bits = [
        f"task_id={task_id}",
        f"cursor_agent_id={cursor_agent_id!s}",
        f"cursor_run_id={cursor_run_id!s}",
    ]
    ctx = "\n".join(ctx_bits)
    meta_line = ""
    if metadata:
        meta_line = "\nMetadata: " + json.dumps(metadata, sort_keys=True)
    why = f"{prompt_title}\n\nCloud HITL context:\n{ctx}{meta_line}"
    return HITLPrompt(
        trigger="approval",
        why=why,
        what=prompt_body,
        options=hitl_options,
        default_option_id=default_id,
        channels=["lark", "mcp", "cloud"],
        deadline_seconds=deadline_seconds,
    )


def compute_idempotency_key(
    *,
    task_id: str,
    cursor_agent_id: str | None,
    cursor_run_id: str | None,
    prompt_body: str,
) -> str:
    """Auto-derive the dedup key per :doc:`mcp-tool-contract` §5.

    Returns ``sha256(f"{task_id}|{cursor_agent_id}|{cursor_run_id}|"
    f"{prompt_body}").hexdigest()[:32]`` — a 32-hex-char opaque token. The
    inputs are not recoverable from the digest (one-way hash, per SECURITY
    R1) and the same tuple always produces the same key (idempotency).
    """
    components = "|".join([
        task_id or "",
        cursor_agent_id or "",
        cursor_run_id or "",
        prompt_body or "",
    ])
    return hashlib.sha256(components.encode("utf-8")).hexdigest()[:32]


class CloudHITLBridge:
    """Orchestrates cloud-sourced HITL rows + optional Lark delivery."""

    def __init__(
        self,
        store: HITLStore,
        lark_notifier: CloudHITLLarkNotifier | None,
        *,
        default_timeout_s: float = 600.0,
    ) -> None:
        self._store = store
        self._lark_notifier = lark_notifier
        self._default_timeout_s = default_timeout_s
        self._has_metadata_column: bool = _detect_metadata_column(store.conn)
        if not self._has_metadata_column:
            logger.warning(
                "CloudHITLBridge: popola_hitl.metadata column not present; "
                "v0.8.7 idempotency dedup is disabled for this connection. "
                "Apply migration 007_popola_hitl_metadata.sql to enable it."
            )

    @property
    def store(self) -> HITLStore:
        return self._store

    @property
    def has_metadata_column(self) -> bool:
        """``True`` iff migration 007 has been applied on the bridge's DB."""
        return self._has_metadata_column

    def submit_request(
        self,
        *,
        task_id: str,
        cursor_agent_id: str | None,
        cursor_run_id: str | None,
        prompt_title: str,
        prompt_body: str,
        options: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
        idempotency_window_s: int | None = None,
        event_log: Any | None = None,
        requester_session: str | None = None,
    ) -> CloudHITLRequest:
        """Create a HITL request originating from a cloud agent.

        v0.8.7 T2.1.3 behavior:

        - Auto-derives ``idempotency_key`` via :func:`compute_idempotency_key`
          when the caller does not supply one.
        - Performs an SQL-only dedup lookup over the
          :data:`CLOUD_HITL_IDEMPOTENCY_WINDOW_S` window before inserting; on
          hit, returns the existing :class:`CloudHITLRequest` with
          ``deduped=True`` and does NOT call the Lark notifier (one card per
          ``(task_id, agent_id, run_id, question_text)``).
        - On a miss, persists the key + cursor tuple + caller metadata into
          ``popola_hitl.metadata`` (added by migration 007) so a later restart
          plus replay still short-circuits (SECURITY R3 — SQL is the single
          source of truth).
        - Best-effort sends a Lark card when :attr:`_lark_notifier` is set.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key:
            resolved_key = compute_idempotency_key(
                task_id=task_id,
                cursor_agent_id=cursor_agent_id,
                cursor_run_id=cursor_run_id,
                prompt_body=prompt_body,
            )
        else:
            if len(idempotency_key) > CLOUD_HITL_IDEMPOTENCY_KEY_MAX_LEN:
                raise ValueError(
                    f"idempotency_key exceeds maximum length "
                    f"{CLOUD_HITL_IDEMPOTENCY_KEY_MAX_LEN} chars "
                    f"(got {len(idempotency_key)}); see "
                    f"mcp-tool-contract.md §3.1."
                )
            resolved_key = idempotency_key

        window = (
            CLOUD_HITL_IDEMPOTENCY_WINDOW_S
            if idempotency_window_s is None
            else int(idempotency_window_s)
        )

        existing = self.lookup_by_idempotency_key(
            resolved_key, window_seconds=window
        )
        if existing is not None:
            logger.info(
                "CloudHITLBridge.submit_request dedup hit "
                "task_id=%s hitl_id=%s key=%s",
                task_id,
                existing.hitl_id,
                resolved_key,
            )
            self._emit_requested_audit(
                event_log=event_log,
                hitl_id=existing.hitl_id,
                task_id=task_id,
                cursor_agent_id=cursor_agent_id,
                cursor_run_id=cursor_run_id,
                idempotency_key=resolved_key,
                deduped=True,
                requested_at=_utc_iso_now(),
                requester_session=requester_session or "unknown",
            )
            return existing

        deadline_seconds = _ceil_deadline_seconds(timeout_s, self._default_timeout_s)
        caller_meta = dict(metadata or {})
        prompt = _build_cloud_prompt(
            prompt_title=prompt_title,
            prompt_body=prompt_body,
            options=options,
            task_id=task_id,
            cursor_agent_id=cursor_agent_id,
            cursor_run_id=cursor_run_id,
            metadata=caller_meta,
            deadline_seconds=deadline_seconds,
        )
        hitl_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        deadline_at = now + timedelta(seconds=deadline_seconds)
        self._store.create(
            prompt,
            hitl_id=hitl_id,
            task_id=task_id,
            deadline_at=deadline_at,
        )

        persisted_meta: dict[str, Any] = {
            **caller_meta,
            "idempotency_key": resolved_key,
            "task_id": task_id,
            "cursor_agent_id": cursor_agent_id,
            "cursor_run_id": cursor_run_id,
        }
        self._persist_metadata(hitl_id, persisted_meta)

        self._emit_requested_audit(
            event_log=event_log,
            hitl_id=hitl_id,
            task_id=task_id,
            cursor_agent_id=cursor_agent_id,
            cursor_run_id=cursor_run_id,
            idempotency_key=resolved_key,
            deduped=False,
            requested_at=now.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            requester_session=requester_session or "unknown",
        )
        lark_dispatched = True
        if self._lark_notifier is not None:
            try:
                self._lark_notifier.send_hitl_card(
                    prompt,
                    hitl_id=hitl_id,
                    event_log=event_log,
                    task_id=task_id,
                )
            except Exception as exc:
                logger.warning(
                    "Cloud HITL Lark delivery failed for hitl_id=%s — request still "
                    "recorded; user can answer via MCP/web/CLI fallback. Error: %r",
                    hitl_id,
                    exc,
                )
                self.record_failure(
                    hitl_id=hitl_id,
                    error_kind="lark_unreachable",
                    event_log=event_log,
                )
                lark_dispatched = False
        return CloudHITLRequest(
            hitl_id=hitl_id,
            task_id=task_id,
            cursor_agent_id=cursor_agent_id,
            cursor_run_id=cursor_run_id,
            prompt=prompt,
            options=tuple(prompt.options),
            created_at=now,
            deadline_at=deadline_at,
            metadata=persisted_meta,
            deduped=False,
            lark_dispatched=lark_dispatched,
        )

    def lookup_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        window_seconds: int | None = None,
    ) -> CloudHITLRequest | None:
        """SQL-only dedup lookup over the rolling
        :data:`CLOUD_HITL_IDEMPOTENCY_WINDOW_S` window.

        Per SECURITY R3 this MUST hit SQLite directly (no in-memory cache that
        would not survive ``popolad`` restarts). The query parameterizes both
        the key and the cutoff timestamp; no string interpolation. Returns
        ``None`` when:

        - the metadata column is absent (migration 007 not applied),
        - no row matches the key inside the window, or
        - the matching row is in a terminal non-replayable state
          (``cancelled`` / ``timeout``) — per :doc:`mcp-tool-contract` §5
          terminal-state row, those are invalidated so a retry rebuilds.

        Pending and answered rows ARE returned (replays inside the window of
        an answered row re-emit the recorded answer; the MCP tool's wait loop
        observes ``status=answered`` immediately).
        """
        if not idempotency_key:
            return None
        if not self._has_metadata_column:
            return None
        if not isinstance(idempotency_key, str):
            raise TypeError("idempotency_key must be a string")
        window = (
            CLOUD_HITL_IDEMPOTENCY_WINDOW_S
            if window_seconds is None
            else int(window_seconds)
        )
        cutoff = (datetime.now(UTC) - timedelta(seconds=window)).isoformat()
        cur = self._store.conn.execute(
            """
            SELECT * FROM popola_hitl
             WHERE json_extract(metadata, '$.idempotency_key') = ?
               AND created_at > ?
               AND status IN ('pending', 'answered')
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (idempotency_key, cutoff),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_cloud_request(row, deduped=True)

    def get_request(self, hitl_id: str) -> CloudHITLRequest | None:
        """Reconstruct a :class:`CloudHITLRequest` from the stored row.

        Used by the daemon RPC layer (and tests) to surface the persisted
        ``cursor_*`` tuple + idempotency_key without reaching into the raw
        SQLite row factory. Returns ``None`` when no row matches ``hitl_id``.
        """
        cur = self._store.conn.execute(
            "SELECT * FROM popola_hitl WHERE hitl_id = ?",
            (hitl_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_cloud_request(row, deduped=False)

    def _persist_metadata(self, hitl_id: str, meta: dict[str, Any]) -> None:
        """Write ``meta`` into ``popola_hitl.metadata`` (no-op when absent).

        The column is added by migration 007. When the schema lacks it (e.g.,
        v0.8.5 fixtures), we silently skip the UPDATE — the bridge already
        warned at construction time, so each missed write is not an extra
        surprise. We use the existing :class:`HITLStore` connection because
        SQLite serializes writes per connection (and the store's own
        ``mark_answered`` is the sole writer of answer columns — I-4).
        """
        if not self._has_metadata_column:
            return
        try:
            payload = json.dumps(meta, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.exception(
                "CloudHITLBridge._persist_metadata: payload not JSON-serialisable "
                "for hitl_id=%s; skipping metadata write",
                hitl_id,
            )
            return
        try:
            self._store.conn.execute(
                "UPDATE popola_hitl SET metadata = ? WHERE hitl_id = ?",
                (payload, hitl_id),
            )
            self._store.conn.commit()
        except sqlite3.OperationalError:
            logger.exception(
                "CloudHITLBridge._persist_metadata: UPDATE failed for hitl_id=%s",
                hitl_id,
            )

    def await_answer(
        self,
        hitl_id: str,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 1.0,
        event_log: Any | None = None,
    ) -> HITLReply | None:
        """Block until the row is answered, or time out / reach terminal state.

        v0.8.7 T2.2.1: when the row's ``deadline_at`` has passed and the row
        is still ``pending``, this call atomically transitions it to
        ``timeout`` (via :meth:`mark_timeout`) and emits the SECURITY §6
        A3+A4 audit pair *before* returning ``None`` so the daemon's
        ``_cloud_wait_sync`` reads the new ``timeout`` status on its next
        ``store.get`` call. The audit therefore lands on the daemon side
        before the MCP tool ever sees the timeout response (AC d).

        ``event_log`` is best-effort: the daemon-injected resolver
        (:func:`_resolve_event_log_for_task`) is consulted when the caller
        does not pass one explicitly, so production rpc.py paths get audit
        emission without changing their call site.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        interval = max(0.05, poll_interval_s)
        while True:
            row = self._store.get(hitl_id)
            if row is None:
                return None
            status = str(row.get("status", ""))
            if status == "answered":
                return self._reply_from_row(hitl_id, row)
            if status in {"timeout", "cancelled"}:
                return None
            if status == "pending" and self._row_is_overdue(row):
                self.mark_timeout(hitl_id, event_log=event_log)
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def _row_is_overdue(self, row: Mapping[str, Any]) -> bool:
        """Return ``True`` iff the row's ``deadline_at`` lies in the past.

        Tolerates missing / unparseable ``deadline_at`` (returns ``False``)
        because the row creation path always populates the column with an
        ISO 8601 timestamp; a missing value indicates a non-cloud HITL row
        and should not trigger the cloud timeout watchdog.
        """
        raw = row.get("deadline_at")
        if not raw:
            return False
        deadline_at = _parse_isoformat(raw)
        return datetime.now(UTC) >= deadline_at

    def _reply_from_row(self, hitl_id: str, row: dict[str, Any]) -> HITLReply:
        via_raw = row.get("answered_via")
        via = cast(
            HITLChannel,
            via_raw
            if via_raw
            in (
                "lark",
                "ide",
                "cli",
                "email",
                "signal",
                "mcp",
                "web",
                "cloud",
            )
            else "cloud",
        )
        return HITLReply(
            hitl_id=hitl_id,
            option_id=str(row.get("answer_option_id") or ""),
            via=via,
            reason=_str_or_none(row.get("answer_reason")),
            responder_id=_str_or_none(row.get("answer_responder_id")),
        )

    def submit_answer(
        self,
        hitl_id: str,
        answer_option_id: str,
        *,
        responder_id: str,
        reason: str | None = None,
        channel: HITLChannel = "cloud",
        expected_cursor_agent_id: str | None = None,
        expected_cursor_run_id: str | None = None,
        event_log: Any | None = None,
    ) -> tuple[bool, str | None]:
        """Record an answer via :meth:`HITLStore.mark_answered`.

        v0.8.7 T2.1.3 mis-route defense: when both ``expected_cursor_*``
        kwargs are provided AND mismatch the row's stored cursor tuple, the
        call returns ``(False, "mis-route:...")`` *without* calling
        :meth:`HITLStore.mark_answered` (the row's ``status`` stays
        ``pending``). The Lark webhook handler / daemon answer route is
        expected to translate the rejection to ``HTTP 400``.

        v0.8.7 T2.2.1: when this call wins the race, emits the SECURITY §6
        audit pair (``cloud_hitl.transition`` pending→answered followed by
        ``cloud_hitl.answered``). ``event_log`` defaults to a daemon-injected
        per-task log via :func:`_resolve_event_log_for_task` so production
        callers (rpc.py) get audit emission without changing their call site.

        Returns:
            ``(True, channel)`` when this call won the race, else
            ``(False, already_descriptor)`` where ``already_descriptor`` is
            best-effort ``"<via>:<responder>"`` from the existing row,
            ``"mis-route:..."`` for the mis-route defense above, or ``None``
            when the row no longer exists.
        """
        if (
            expected_cursor_agent_id is not None
            or expected_cursor_run_id is not None
        ):
            mis_route = self._check_mis_route(
                hitl_id,
                expected_cursor_agent_id=expected_cursor_agent_id,
                expected_cursor_run_id=expected_cursor_run_id,
            )
            if mis_route is not None:
                return False, mis_route

        result = self._store.mark_answered(
            hitl_id,
            option_id=answer_option_id,
            via=channel,
            reason=reason,
            responder_id=responder_id,
        )
        if result.ok:
            log = self._resolve_event_log(event_log, hitl_id)
            answered_at = _utc_iso_now()
            self.record_transition(
                hitl_id=hitl_id,
                from_state="pending",
                to_state="answered",
                actor=responder_id,
                transitioned_at=answered_at,
                event_log=log,
            )
            self._emit_answered_audit(
                event_log=log,
                hitl_id=hitl_id,
                answered_by=responder_id,
                answered_at=answered_at,
                channel=str(channel),
                option_id=answer_option_id,
                custom_text_present=bool(reason and reason.strip()),
            )
            return True, channel
        existing = self._store.get(hitl_id)
        if existing is None:
            return False, None
        via = _str_or_none(existing.get("answered_via")) or "unknown"
        rid = _str_or_none(existing.get("answer_responder_id")) or ""
        already = f"{via}:{rid}" if rid else via
        return False, already

    def mark_timeout(
        self,
        hitl_id: str,
        *,
        event_log: Any | None = None,
        attempt: int = 1,
    ) -> bool:
        """Atomically transition ``pending → timeout`` + emit the A3/A4 audit pair.

        Per AC (d): the audit row MUST be emitted *before* the MCP tool
        returns the error envelope. Since the bridge runs inside the daemon
        process and the MCP tool only observes the daemon's wait response,
        this method does both writes (SQLite + NDJSON) before any wait
        endpoint flushes ``status: "timeout"`` back to the cloud agent.

        Returns ``True`` iff this call won the row's pending→terminal race
        (i.e., it is the first observer of the deadline expiry). Subsequent
        calls return ``False`` (idempotent — the row is already terminal),
        matching :meth:`HITLStore.mark_status` semantics.
        """
        won = self._store.mark_status(hitl_id, "timeout")
        log = self._resolve_event_log(event_log, hitl_id)
        if not won:
            return False
        failed_at = _utc_iso_now()
        self.record_transition(
            hitl_id=hitl_id,
            from_state="pending",
            to_state="timeout",
            actor=None,
            transitioned_at=failed_at,
            event_log=log,
        )
        self.record_failure(
            hitl_id=hitl_id,
            error_kind="timeout",
            attempt=attempt,
            failed_at=failed_at,
            event_log=log,
        )
        return True

    def record_failure(
        self,
        *,
        hitl_id: str | None,
        error_kind: str,
        attempt: int = 1,
        failed_at: str | None = None,
        event_log: Any | None = None,
    ) -> dict[str, Any]:
        """Emit a single :data:`CLOUD_HITL_FAILED_EVENT` row (A3 keys).

        Used directly by callers that detect failures the bridge does not
        own (e.g., the MCP tool rejecting a daemon-unreachable response).
        For invalid_context failures fired *before* a row exists, pass
        ``hitl_id=None`` — the audit row records ``hitl_id=""`` plus
        ``hitl_id_if_known=None`` so consumers can distinguish "not yet
        created" from "row id is empty string".

        Validates ``error_kind`` against :data:`CLOUD_HITL_ERROR_KINDS`
        (per workspace rule "No Silent Failures": misspelled kinds raise
        :class:`ValueError` rather than landing as audit-noise).
        """
        if error_kind not in CLOUD_HITL_ERROR_KINDS:
            raise ValueError(
                f"unknown cloud_hitl error_kind {error_kind!r}; "
                f"expected one of {list(CLOUD_HITL_ERROR_KINDS)}"
            )
        log = self._resolve_event_log(event_log, hitl_id)
        payload: dict[str, Any] = {
            "hitl_id": hitl_id or "",
            "error_kind": error_kind,
            "failed_at": failed_at or _utc_iso_now(),
            "attempt": int(attempt),
            "hitl_id_if_known": hitl_id if hitl_id else None,
        }
        _safe_append(log, CLOUD_HITL_FAILED_EVENT, payload)
        return payload

    def record_transition(
        self,
        *,
        hitl_id: str,
        from_state: str,
        to_state: str,
        actor: str | None = None,
        transitioned_at: str | None = None,
        event_log: Any | None = None,
    ) -> dict[str, Any]:
        """Emit a single :data:`CLOUD_HITL_TRANSITION_EVENT` row (A4 keys).

        Public so the W2.1 T2.1.2 Lark card mutators can stamp the S2
        ``pending → pending_second_approval`` intermediate transition (no
        atomic SQLite move; the row stays ``pending`` while we wait for the
        second approver). The from/to_state strings are not validated here
        — the audit consumer is the source of truth for the FSM rules.
        """
        log = self._resolve_event_log(event_log, hitl_id)
        payload: dict[str, Any] = {
            "hitl_id": hitl_id,
            "from_state": from_state,
            "to_state": to_state,
            "transitioned_at": transitioned_at or _utc_iso_now(),
            "actor": actor,
        }
        _safe_append(log, CLOUD_HITL_TRANSITION_EVENT, payload)
        return payload

    def _resolve_event_log(self, explicit: Any | None, hitl_id: str | None) -> Any:
        """Return ``explicit`` when truthy; else fall back to module resolver.

        The fallback uses the row's ``task_id`` (looked up via the store)
        because the daemon's per-task NDJSON log is the authoritative audit
        sink. When the row is missing or has no ``task_id``, returns
        ``None`` so :func:`_safe_append` no-ops cleanly.
        """
        if explicit is not None:
            return explicit
        if not hitl_id:
            return None
        row = self._store.get(hitl_id)
        if row is None:
            return None
        task_id = _str_or_none(row.get("task_id"))
        if task_id is None:
            return None
        return _resolve_event_log_for_task(task_id)

    def _emit_requested_audit(
        self,
        *,
        event_log: Any | None,
        hitl_id: str,
        task_id: str,
        cursor_agent_id: str | None,
        cursor_run_id: str | None,
        idempotency_key: str,
        deduped: bool,
        requested_at: str,
        requester_session: str,
    ) -> dict[str, Any]:
        """Emit one :data:`CLOUD_HITL_REQUESTED_EVENT` row (A1, 8 keys)."""
        log = self._resolve_event_log(event_log, hitl_id)
        payload: dict[str, Any] = {
            "hitl_id": hitl_id,
            "task_id": task_id,
            "cursor_agent_id": cursor_agent_id,
            "cursor_run_id": cursor_run_id,
            "idempotency_key": idempotency_key,
            "deduped": deduped,
            "requested_at": requested_at,
            "requester_session": requester_session,
        }
        _safe_append(log, CLOUD_HITL_REQUESTED_EVENT, payload)
        return payload

    def _emit_answered_audit(
        self,
        *,
        event_log: Any | None,
        hitl_id: str,
        answered_by: str,
        answered_at: str,
        channel: str,
        option_id: str,
        custom_text_present: bool,
    ) -> dict[str, Any]:
        """Emit one :data:`CLOUD_HITL_ANSWERED_EVENT` row (A2, 6 keys)."""
        payload: dict[str, Any] = {
            "hitl_id": hitl_id,
            "answered_by": answered_by,
            "answered_at": answered_at,
            "channel": channel,
            "option_id": option_id,
            "custom_text_present": bool(custom_text_present),
        }
        _safe_append(event_log, CLOUD_HITL_ANSWERED_EVENT, payload)
        return payload

    def _check_mis_route(
        self,
        hitl_id: str,
        *,
        expected_cursor_agent_id: str | None,
        expected_cursor_run_id: str | None,
    ) -> str | None:
        """Return a ``"mis-route:..."`` descriptor when the inbound tuple
        does not match the row's stored cursor tuple, else ``None``.

        The comparison reads the structured ``metadata`` JSON column written
        by :meth:`submit_request`. Rows that pre-date migration 007 (no
        metadata) cannot enforce mis-route defense — we return ``None`` and
        let the answer proceed; the construction-time warning surfaces the
        gap explicitly.
        """
        existing = self.get_request(hitl_id)
        if existing is None:
            return None
        stored_agent = existing.metadata.get("cursor_agent_id")
        stored_run = existing.metadata.get("cursor_run_id")
        if stored_agent is None and stored_run is None:
            return None
        agent_mismatch = (
            expected_cursor_agent_id is not None
            and stored_agent is not None
            and expected_cursor_agent_id != stored_agent
        )
        run_mismatch = (
            expected_cursor_run_id is not None
            and stored_run is not None
            and expected_cursor_run_id != stored_run
        )
        if not (agent_mismatch or run_mismatch):
            return None
        descriptor = (
            f"mis-route:expected_agent={expected_cursor_agent_id!s}"
            f",expected_run={expected_cursor_run_id!s}"
            f",stored_agent={stored_agent!s},stored_run={stored_run!s}"
        )
        logger.warning(
            "CloudHITLBridge.submit_answer rejected mis-route hitl_id=%s %s",
            hitl_id,
            descriptor,
        )
        return descriptor


def _row_to_cloud_request(
    row: sqlite3.Row | dict[str, Any],
    *,
    deduped: bool,
) -> CloudHITLRequest:
    """Reconstruct a :class:`CloudHITLRequest` from a raw ``popola_hitl`` row.

    Used by :meth:`CloudHITLBridge.lookup_by_idempotency_key` and
    :meth:`CloudHITLBridge.get_request`. Parses the ``prompt_json`` blob and
    the structured ``metadata`` JSON column to surface the cursor tuple and
    idempotency_key for downstream callers.
    """
    if hasattr(row, "keys"):
        keys = list(row.keys())  # noqa: SIM118 — Row.__iter__ returns values
        data: dict[str, Any] = {k: row[k] for k in keys}
    else:
        data = dict(row)
    prompt = HITLPrompt.model_validate_json(str(data.get("prompt_json") or "{}"))
    raw_meta = data.get("metadata")
    metadata: dict[str, Any]
    if isinstance(raw_meta, str) and raw_meta:
        try:
            parsed = json.loads(raw_meta)
            metadata = dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            logger.warning(
                "_row_to_cloud_request: bad JSON in metadata for hitl_id=%s; "
                "treating as empty",
                data.get("hitl_id"),
            )
            metadata = {}
    else:
        metadata = {}
    created_at = _parse_isoformat(data.get("created_at"))
    deadline_at = _parse_isoformat(data.get("deadline_at")) or created_at
    return CloudHITLRequest(
        hitl_id=str(data.get("hitl_id") or ""),
        task_id=str(data.get("task_id") or ""),
        cursor_agent_id=_str_or_none(metadata.get("cursor_agent_id")),
        cursor_run_id=_str_or_none(metadata.get("cursor_run_id")),
        prompt=prompt,
        options=tuple(prompt.options),
        created_at=created_at,
        deadline_at=deadline_at,
        metadata=metadata,
        deduped=deduped,
        lark_dispatched=True,
    )


def _parse_isoformat(value: Any) -> datetime:
    """Best-effort ISO 8601 parser; falls back to ``datetime.now(UTC)``.

    SQLite stores timestamps as either ``2026-05-08T12:34:56.789012+00:00``
    (popolaloom code path) or ``2026-05-08T12:34:56.789Z`` (raw default).
    Both are RFC3339-compatible; ``datetime.fromisoformat`` accepts the
    former on 3.11+ and the latter when we strip the ``Z``.
    """
    if value is None:
        return datetime.now(UTC)
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:  # pragma: no cover - defensive
        return datetime.now(UTC)


def _detect_metadata_column(conn: sqlite3.Connection) -> bool:
    """Return ``True`` iff ``popola_hitl.metadata`` exists on ``conn``.

    Probes via ``PRAGMA table_info(popola_hitl)`` rather than catching an
    UPDATE failure so the bridge can decide its behavior up front (no-op
    metadata writes + dedup disabled vs. full v0.8.7 behavior). Robust to
    a missing table (returns ``False``) — the caller layer already handles
    "store not wired up" elsewhere.
    """
    try:
        cur = conn.execute("PRAGMA table_info(popola_hitl)")
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return False
    for row in rows:
        if hasattr(row, "keys"):
            name = row["name"] if "name" in row.keys() else None  # noqa: SIM118
        else:
            try:
                name = row[1]
            except (IndexError, TypeError):
                name = None
        if name == "metadata":
            return True
    return False


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def build_default_bridge(
    connection: sqlite3.Connection,
    *,
    lark_notifier: CloudHITLLarkNotifier | None = None,
    default_timeout_s: float | None = None,
) -> CloudHITLBridge:
    """Construct a bridge with :class:`HITLStore` on ``connection``.

    v0.8.7 T2.2.1: when ``default_timeout_s`` is ``None``, the bridge falls
    back to the ``[hitl.cloud].timeout_seconds`` value that
    :func:`configure_cloud_hitl_defaults` has injected (or the v0.8.5
    fallback of 600 s when no config has been wired). Explicit overrides
    win so existing tests that pass a literal value keep working.
    """
    resolved: CloudHITLLarkNotifier | None
    resolved = _NoopCloudLarkNotifier() if lark_notifier is None else lark_notifier
    store = HITLStore(connection)
    timeout = (
        float(default_timeout_s)
        if default_timeout_s is not None
        else float(_CLOUD_HITL_DEFAULTS["default_timeout_s"])
    )
    return CloudHITLBridge(store, resolved, default_timeout_s=timeout)


def bridge_for_daemon(
    store: HITLStore | None,
    *,
    send_lark: bool = True,
    default_timeout_s: float | None = None,
) -> CloudHITLBridge | None:
    """Minimal factory used by :mod:`popolaloom.daemon.rpc` handlers.

    v0.8.7 T2.2.1: ``default_timeout_s`` defaults to ``None`` so the bridge
    inherits the ``[hitl.cloud].timeout_seconds`` value pushed by
    :func:`configure_cloud_hitl_defaults` at daemon startup. Explicit
    overrides (e.g., tests passing a literal float) still win.

    v0.8.7 B2 wiring: when ``send_lark=True`` the production
    :class:`_DefaultCloudLarkNotifier` is constructed with the bridge's
    store so it can render the v1 versioned card via
    :func:`popolaloom.lark.cloud_hitl_card.build_cloud_hitl_card` (per
    REVIEW.md B2 finding — the legacy notifier built v0.5 generic HITL
    cards, dropping the v0.8.7 ``card_metadata`` v1 contract).
    """
    if store is None:
        return None
    notifier: CloudHITLLarkNotifier = (
        _DefaultCloudLarkNotifier(store=store)
        if send_lark
        else _NoopCloudLarkNotifier()
    )
    timeout = (
        float(default_timeout_s)
        if default_timeout_s is not None
        else float(_CLOUD_HITL_DEFAULTS["default_timeout_s"])
    )
    return CloudHITLBridge(store, notifier, default_timeout_s=timeout)


__all__ = [
    "CLOUD_HITL_ANSWERED_EVENT",
    "CLOUD_HITL_ANSWERED_KEYS",
    "CLOUD_HITL_ERROR_KINDS",
    "CLOUD_HITL_FAILED_EVENT",
    "CLOUD_HITL_FAILED_KEYS",
    "CLOUD_HITL_IDEMPOTENCY_KEY_MAX_LEN",
    "CLOUD_HITL_IDEMPOTENCY_WINDOW_S",
    "CLOUD_HITL_REQUESTED_EVENT",
    "CLOUD_HITL_REQUESTED_KEYS",
    "CLOUD_HITL_TRANSITION_EVENT",
    "CLOUD_HITL_TRANSITION_KEYS",
    "CloudHITLBridge",
    "CloudHITLLarkNotifier",
    "CloudHITLRequest",
    "EventLogResolver",
    "bridge_for_daemon",
    "build_default_bridge",
    "compute_idempotency_key",
    "configure_cloud_hitl_defaults",
]
