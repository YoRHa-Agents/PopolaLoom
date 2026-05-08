"""popolad RPC — FastAPI app + 7 endpoints over UDS (v0.2.0 Stage A A2).

Endpoints (all use Pydantic v2 models for request/response validation):

1. ``POST /dispatch`` — body ``{cli, prompt, cwd?, extra?}`` →
   ``{task_id, events_log}``
2. ``GET  /status/{task_id}`` → full status dict
3. ``GET  /list?include_terminal={bool}`` → list of task summaries
4. ``GET  /attach_stream/{task_id}`` — Server-Sent Events stream of NDJSON
   events; respects ``request.is_disconnected()``; uses ``asyncio.Queue``
   with ``maxsize=1000`` for backpressure (RV2-1 mitigation).
5. ``POST /cancel/{task_id}`` — SIGTERM, then SIGKILL after 5s grace.
6. ``GET  /probe`` — ``{daemon_pid, started_at, uptime_seconds, active_tasks,
   version}`` (lightweight health).
7. ``GET  /health`` — ``{status: "ok"}`` for liveness probe.

Module structure:

- :func:`create_app` — FastAPI factory, accepts optional ``events_dir`` /
  ``adapter`` / ``popolad`` (for tests). Tests use ``httpx.ASGITransport``
  to drive the app in-process; production runs ``python -m popolaloom.daemon``
  which binds the same app to a real UDS via uvicorn.
- ``_DAEMON_STATE`` — module-level dict holding the daemon-process Popolad
  singleton (this is the **only** place a singleton is allowed; daemon/server.py
  no longer has ``_default_popolad`` — R-013 fix).

# TODO(Stage B): graph.ainvoke 替换 dispatch_task 内部调用
# TODO(Stage C): TaskService + EventBus 注入到 Popolad.__init__
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from popolaloom import __version__
from popolaloom.daemon.primitives import (
    RelayHandoffEnvelope,
)
from popolaloom.daemon.primitives import (
    federate as federate_primitive,
)
from popolaloom.daemon.primitives import (
    relay as relay_primitive,
)
from popolaloom.daemon.primitives.supervise import (
    SubscriptionHandle,
    get_default_registry,
)
from popolaloom.daemon.server import AdapterCallback, Popolad
from popolaloom.hitl import HITLChannel, HITLReply
from popolaloom.hitl.cloud_bridge import CloudHITLBridge, bridge_for_daemon

logger = logging.getLogger(__name__)


# ── pydantic schemas ─────────────────────────────────────────────────────


class DispatchRequest(BaseModel):
    """Body of ``POST /dispatch``."""

    cli: str = Field(..., min_length=1, description="Adapter name (cursor/claude/codex/...)")
    prompt: str = Field(..., description="Prompt forwarded verbatim to the chosen CLI.")
    cwd: str | None = Field(None, description="Working directory; None = popolad's CWD.")
    extra: dict[str, Any] | None = Field(
        None,
        description="Adapter-specific extras (e.g. {'output_format': 'stream-json'}). "
        "Mapped from CLI's --cli-flag KEY=VAL repeatable option (R-012).",
    )


class DispatchResponse(BaseModel):
    """Response body of ``POST /dispatch``."""

    task_id: str
    events_log: str
    cli: str


class CancelResponse(BaseModel):
    """Response body of ``POST /cancel/{task_id}``."""

    task_id: str
    requested_signal: str
    escalated_to_sigkill: bool
    pid: int | None
    result: str | None = None


class ProbeResponse(BaseModel):
    """Response body of ``GET /probe``."""

    daemon_pid: int
    started_at: str
    uptime_seconds: float
    active_tasks: int
    version: str


class HealthResponse(BaseModel):
    """Response body of ``GET /health``."""

    status: str


class HitlAnswerRequest(BaseModel):
    """Body of ``POST /hitl/answer`` (v0.3.0 F4.F)."""

    hitl_id: str = Field(..., min_length=1, description="Prompt id from popola_hitl.")
    option_id: str = Field(..., min_length=1, description="Chosen option id.")
    via: str = Field(
        ...,
        description="Channel that recorded the reply (lark/ide/cli/mcp/web/...).",
    )
    reason: str | None = Field(default=None, description="Optional rationale.")
    responder_id: str | None = Field(
        default=None, description="Optional opaque responder id (open_id / $USER / ...)."
    )


class HitlAnswerResponse(BaseModel):
    """Response body of ``POST /hitl/answer`` (v0.3.0 F4.F)."""

    ok: bool
    hitl_id: str
    already_status: str | None = None
    already_via: str | None = None


class CloudHITLRequestBody(BaseModel):
    """Body of ``POST /hitl/cloud/request`` (v0.8.5 cloud-agent HITL bridge)."""

    task_id: str = Field(..., min_length=1)
    cursor_agent_id: str | None = None
    cursor_run_id: str | None = None
    prompt_title: str = Field(..., min_length=1)
    prompt_body: str = Field(..., min_length=1)
    options: list[dict[str, str]] = Field(
        ...,
        min_length=2,
        description="Each entry must include id + label (≥ 2 options).",
    )
    metadata: dict[str, Any] | None = None
    timeout_s: float | None = Field(
        default=None,
        gt=0,
        le=86400,
        description="Optional per-request deadline (seconds); capped by HITL schema at 86400.",
    )


class CloudHITLRequestResponse(BaseModel):
    """Immediate acknowledgement for ``POST /hitl/cloud/request``.

    v0.8.7 T2.1.3 additions:

    - ``deduped`` is ``True`` when the daemon short-circuited a replay inside
      the rolling 1-hour ``cloud_hitl.idempotency_window_s`` window
      (``mcp-tool-contract.md`` §5). The MCP tool surfaces this back to the
      cloud agent so retries are observably idempotent.
    - ``status`` widens to also include ``"answered"`` because dedup hits on
      already-answered rows return the existing row directly; the MCP tool
      then resolves the answer via its long-poll loop without ever reaching
      ``GET /hitl/cloud/wait``.

    v0.8.7 M3 (REVIEW.md): ``lark_dispatched`` is ``True`` when the
    bridge's Lark fan-out succeeded (or no notifier was wired); ``False``
    when the bridge recorded a ``lark_unreachable`` failure during
    :meth:`CloudHITLBridge.submit_request`. The MCP tool's
    ``_make_timeout_envelope`` reads this flag to flip the user-facing
    error code from ``timeout`` → ``lark_unreachable`` per contract §7
    row 4 (Lark-unreachable scenario where the row was created but the
    card never reached the human surfaces as a poll-then-error).
    Defaults to ``True`` to preserve v0.8.5 wire compatibility.
    """

    hitl_id: str
    status: Literal["pending", "answered"] = "pending"
    deadline_at: str
    cursor_agent_id: str | None = None
    cursor_run_id: str | None = None
    deduped: bool = False
    lark_dispatched: bool = True


class CloudHITLWaitAnswerPayload(BaseModel):
    """Answer payload returned by ``GET /hitl/cloud/wait/{hitl_id}``."""

    option_id: str
    reason: str | None = None
    responder_id: str | None = None
    channel: str


class CloudHITLWaitResponse(BaseModel):
    """Long-poll result for ``GET /hitl/cloud/wait/{hitl_id}``."""

    hitl_id: str
    status: Literal["pending", "answered", "timeout"]
    answer: CloudHITLWaitAnswerPayload | None = None


class CloudHITLAnswerBody(BaseModel):
    """Body of ``POST /hitl/cloud/answer/{hitl_id}``.

    v0.8.7 C1 wiring: the optional ``cursor_agent_id`` / ``cursor_run_id``
    fields let HTTP / MCP callers thread the mis-route defense kwargs
    into :meth:`CloudHITLBridge.submit_answer`. When supplied AND
    mismatched against the row's stored cursor tuple, the daemon
    rejects the answer with HTTP 400 (per ``mcp-tool-contract.md``
    §6.3 mis-route table). When omitted (legacy v0.8.5 callers) the
    bridge keeps the un-validated path so existing integrations keep
    working — this is a strict superset of the v0.8.5 schema.
    """

    option_id: str = Field(..., min_length=1)
    reason: str | None = None
    responder_id: str = Field(..., min_length=1)
    channel: str = "cloud"
    cursor_agent_id: str | None = Field(
        default=None,
        description=(
            "Optional cursor agent id; when set, the bridge rejects the "
            "answer with HTTP 400 if the row's stored cursor_agent_id "
            "does not match (mis-route defense per SECURITY R5)."
        ),
    )
    cursor_run_id: str | None = Field(
        default=None,
        description=(
            "Optional cursor run id; same mis-route defense as "
            "cursor_agent_id above."
        ),
    )


class CloudHITLAnswerResponse(BaseModel):
    """JSON body for successful cloud HITL answer recording."""

    ok: bool
    channel: str | None = None
    already_answered_by: str | None = None


class RelayRequest(BaseModel):
    """Body of ``POST /relay`` (v0.3.0 F2.5)."""

    source_task_id: str = Field(..., min_length=1)
    target_cli: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(..., min_length=1)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_cli: str | None = None
    prompt: str | None = None


class RelayResponse(BaseModel):
    """Response body of ``POST /relay``."""

    child_task_id: str
    handoff_envelope: RelayHandoffEnvelope


class RelayDispatchRequest(BaseModel):
    """Body of ``POST /relay/dispatch`` (v0.8.8 T2.2.1).

    Read-only RPC that returns the source task's envelope info
    (``cursor_agent_id`` / ``cursor_run_id`` / ``repo_url`` /
    ``summary`` / ``model``) so the CLI can build the relay payload.
    Per ``relay-primitive.md`` §4 the CLI does the secret scan +
    allowlist gate + audit row write + cloud POST locally; this RPC
    only verifies the source task is terminal and surfaces what the
    CLI needs to construct the envelope.
    """

    source_task_id: str = Field(..., min_length=1)


class RelayDispatchResponse(BaseModel):
    """Response body of ``POST /relay/dispatch`` (v0.8.8 T2.2.1).

    Returns enough source-task metadata for the CLI to construct a
    relay envelope. ``state`` is one of the popola coarse states
    (terminal: ``completed`` / ``failed`` / ``canceled``); the daemon
    rejects non-terminal tasks at this endpoint with HTTP 400 so the
    CLI can map to its own exit-2 ``InvalidArgs`` per ``§2.4`` step 2.
    """

    source_task_id: str
    cursor_agent_id: str | None = None
    cursor_run_id: str | None = None
    repo_url: str | None = None
    pr_url: str | None = None
    summary: str = ""
    model: str = ""
    state: str
    cloud_phase: str | None = None
    runtime: str = "local"


class SuperviseRequest(BaseModel):
    """Body of ``POST /supervise`` (v0.3.0 F2.5)."""

    parent_task_id: str = Field(..., min_length=1)
    child_task_id: str = Field(..., min_length=1)
    callback_url: str | None = Field(
        None,
        description=(
            "Optional HTTP webhook URL — v0.3.0 stores this for forensic "
            "auditing only; in-process callbacks fire via SuperviseRegistry. "
            "Future: F4 cross-channel HITL will dispatch to this URL when child "
            "reaches a terminal state."
        ),
    )


class SuperviseResponse(BaseModel):
    """Response body of ``POST /supervise``."""

    subscription_id: str
    parent_task_id: str
    child_task_id: str


class FederateRequest(BaseModel):
    """Body of ``POST /federate`` (v0.3.0 F2.5)."""

    cli_list: list[str] = Field(..., min_length=3)
    prompt: str = Field(..., min_length=1)
    voting_strategy: str = "majority"
    cwd: str | None = None
    extra: dict[str, Any] | None = None


class FederateRpcResponse(BaseModel):
    """Response body of ``POST /federate``."""

    federate_id: str
    child_task_ids: list[str]
    cli_list: list[str]
    voting_strategy: str
    dispatch_errors: dict[str, str] = Field(default_factory=dict)


# ── module-level daemon state (only allowed singleton, R-013 fix) ────────


_DAEMON_STATE: dict[str, Any] = {
    "popolad": None,
    "started_at": None,
}
"""The daemon-process-level Popolad singleton + start-time marker.

Stored in module-level dict (not a typed singleton attribute) so reload /
recreate semantics are explicit; tests construct fresh instances via
:func:`create_app`."""


_VALID_HITL_REPLY_CHANNELS: frozenset[str] = frozenset(
    {"lark", "ide", "cli", "email", "signal", "mcp", "web", "cloud"}
)


def _narrow_hitl_channel(raw: str) -> HITLChannel:
    """Map a wire string to :data:`~popolaloom.hitl.HITLChannel`."""
    lowered = raw.strip().lower()
    if lowered not in _VALID_HITL_REPLY_CHANNELS:
        allowed = sorted(_VALID_HITL_REPLY_CHANNELS)
        raise HTTPException(
            status_code=422,
            detail=(
                f"invalid HITL channel {raw!r}; "
                f"expected one of {allowed}"
            ),
        )
    return lowered  # type: ignore[return-value]


def _cloud_wait_sync(
    bridge: CloudHITLBridge,
    hitl_id: str,
    timeout_cap: float,
) -> tuple[str, HITLReply | None]:
    """Return ``(status_key, reply?)`` for the HTTP long-poll surface.

    ``status_key`` is one of ``answered`` / ``pending`` / ``timeout`` / ``missing``.
    """
    reply = bridge.await_answer(hitl_id, timeout_s=timeout_cap, poll_interval_s=1.0)
    row = bridge.store.get(hitl_id)
    if row is None:
        return "missing", None
    if reply is not None:
        return "answered", reply
    st = str(row.get("status", ""))
    if st == "pending":
        return "pending", None
    return "timeout", None


def _reply_to_wait_payload(reply: HITLReply) -> CloudHITLWaitAnswerPayload:
    return CloudHITLWaitAnswerPayload(
        option_id=reply.option_id,
        reason=reply.reason,
        responder_id=reply.responder_id,
        channel=str(reply.via),
    )


def _build_default_popolad() -> Popolad:
    """Default Popolad factory — wires :func:`build_command` adapter facade.

    Imported lazily so unit tests that pass their own ``adapter`` to
    :func:`create_app` don't pull adapters/__init__ side effects.
    """
    from popolaloom.adapters import build_command

    return Popolad(adapter=build_command)


# ── lifespan + factory ───────────────────────────────────────────────────


def create_app(
    *,
    events_dir: Path | None = None,
    adapter: AdapterCallback | None = None,
    popolad: Popolad | None = None,
) -> FastAPI:
    """Construct a FastAPI app exposing the 7 popolad RPC endpoints.

    Args:
        events_dir: Override events directory; defaults to ``~/.popola/events``.
        adapter: Override adapter callback; defaults to
            :func:`popolaloom.adapters.build_command`.
        popolad: Pre-built Popolad instance (test-only override; if provided,
            ``events_dir`` and ``adapter`` are ignored).

    Returns:
        FastAPI: app with ``lifespan`` wired to:

        - startup: install Popolad into ``_DAEMON_STATE``;
        - shutdown: cancel any active tasks (best-effort SIGTERM).
    """
    if popolad is None:
        if adapter is None:
            from popolaloom.adapters import build_command

            adapter = build_command
        popolad = Popolad(events_dir=events_dir, adapter=adapter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _DAEMON_STATE["popolad"] = popolad
        _DAEMON_STATE["started_at"] = datetime.now(UTC)
        logger.info(
            "popolad rpc app starting (pid=%d, events_dir=%s)",
            os.getpid(),
            popolad.events_dir,
        )

        # v0.2.0 Stage E E1 (R-002 closure): rehydrate any in-flight ArkTower
        # tasks from the previous daemon process so cross-restart visibility
        # works (S1 self-bootstrap scenario). The popolad.recovered event is
        # emitted per-task by Popolad._emit_recovered_events.
        try:
            recovered = popolad.rehydrate_from_persistence()
            if recovered > 0:
                logger.info(
                    "popolad lifespan startup: rehydrated %d in-flight task(s)",
                    recovered,
                )
        except Exception:
            logger.exception(
                "popolad lifespan startup: rehydrate_from_persistence() failed; "
                "continuing without recovery"
            )

        try:
            yield
        finally:
            logger.info("popolad rpc app shutting down; cancelling active tasks")
            active = popolad.list_active()
            for task in active:
                tid = task.get("task_id")
                if not tid:
                    continue
                try:
                    popolad.cancel_task(tid, sigterm_grace_s=2.0)
                except Exception:
                    logger.exception("shutdown cancel failed for task=%s", tid)
            # v0.5.2 Loop 2 (L2.B): tear down the optional LarkSupervisor
            # that ``_build_default_popolad`` may have wired onto the
            # popolad instance.  Prior to v0.5.2 the supervisor was leaked
            # at lifespan exit (CHANGELOG.md [0.5.1] entry, "Known limitations" subsection);
            # we now call the public ``await supervisor.stop()`` so the
            # ``lark-cli event consume`` subprocess + watchdog asyncio task
            # are stopped cooperatively.  When env vars never opted Lark in
            # ``_lark_supervisor`` is ``None`` and this branch is a no-op
            # (per workspace rule "No Silent Failures": missing supervisor
            # is the documented opt-out path, not an error).
            supervisor = getattr(popolad, "_lark_supervisor", None)
            if supervisor is not None:
                try:
                    await supervisor.stop()
                except Exception:
                    logger.exception("lark.supervisor.stop_failed; daemon shutdown continues")
            try:
                popolad.shutdown_persistence_bridge()
            except Exception:
                logger.exception("shutdown_persistence_bridge() failed")
            _DAEMON_STATE["popolad"] = None
            _DAEMON_STATE["started_at"] = None

    app = FastAPI(
        title="popolad",
        version=__version__,
        description="PopolaLoom daemon RPC over Unix Domain Socket (v0.2.0 Stage A)",
        lifespan=lifespan,
    )

    _register_routes(app, popolad)
    return app


# ── routes ───────────────────────────────────────────────────────────────


_ATTACH_QUEUE_MAXSIZE: int = 1000
"""Backpressure cap for SSE attach streams (RV2-1 mitigation).

Drain thread enqueues at most 1000 buffered events; further events block
the drain thread (which is fine because subprocess output is naturally
slow relative to network). When the SSE client disconnects, the queue
is freed via ``request.is_disconnected()`` polling."""


_ATTACH_POLL_INTERVAL_S: float = 0.1
"""How often the SSE producer polls the underlying NDJSON file for new lines.

100ms keeps latency low for IDE consumers (popola_attach_stream MCP verb)
without burning CPU on idle tasks."""


def _register_routes(app: FastAPI, popolad: Popolad) -> None:
    """Register all 7 routes on ``app``, closing over ``popolad``."""

    @app.post("/dispatch", response_model=DispatchResponse)
    async def dispatch(
        req: DispatchRequest,
        evolution_round: int | None = None,
        max_rounds: int = 5,
        prior_nines: float = 0.0,
        gate_threshold: float = 0.85,
    ) -> DispatchResponse:
        prompt = req.prompt
        if evolution_round is not None and evolution_round >= 1:
            try:
                prompt = _apply_evolution_round_prepend(
                    prompt,
                    round_num=evolution_round,
                    max_rounds=max_rounds,
                    prior_nines=prior_nines,
                    gate_threshold=gate_threshold,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"evolution_round invalid: {exc}"
                ) from exc

        try:
            task_id = await asyncio.to_thread(
                popolad.dispatch_task,
                req.cli,
                prompt,
                req.cwd,
                None,
                None,
                req.extra,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"adapter not registered: {exc}") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # v0.8.8 T2.1.2 (cost-fields.md §4 caveat 3 + DECISIONS.md EOQ-B2):
        # When the user did NOT pass ``model`` for a cloud-cursor dispatch,
        # popolad substitutes a hard default (currently ``composer-2``).
        # Emit ``cloud.model_default_used`` so the verbose status surface
        # can render ``model: -`` (truthful) instead of the substituted
        # value, and so SREs can audit drift if Cursor changes its system
        # default. Best-effort: a missing event_log (e.g. test fixtures
        # that bypass popolad's normal lifecycle) does not abort dispatch
        # — the user-facing behavior is correct without the audit row,
        # we just lose telemetry on this run.
        if req.cli == "cursor-cloud":
            user_extra: dict[str, Any] = req.extra or {}
            if "model" not in user_extra:
                event_log = popolad.event_log(task_id)
                if event_log is not None:
                    try:
                        event_log.append(
                            "cloud.model_default_used",
                            {
                                "task_id": task_id,
                                "default_model": "composer-2",
                            },
                        )
                    except Exception:
                        logger.warning(
                            "cloud.model_default_used emit failed for task=%s; "
                            "verbose status will fall back to recorded model",
                            task_id,
                            exc_info=True,
                        )

        events_log = popolad.events_dir / f"{task_id}.jsonl"
        return DispatchResponse(
            task_id=task_id,
            events_log=str(events_log),
            cli=req.cli,
        )

    @app.get("/status/{task_id}")
    async def status(task_id: str, verbose: bool = False) -> dict[str, Any]:
        """Return runtime status; ``?verbose=true`` adds a ``verbose`` block.

        v0.8.8 T2.1.2 (`cost-fields.md` §3.2 schema, Q-C-2 locked):

        - Default response (``verbose=false``) preserves the v0.8.5 shape
          verbatim — no ``verbose`` key at all (NOT ``null``) so legacy
          consumers calling ``response["verbose"]`` get a
          :class:`KeyError` rather than a silent ``None``.
        - When ``verbose=true``, the response gains a ``verbose`` block
          per spec §3.2 with ten keys: ``cost_estimate_usd`` (always
          ``null`` in v0.8.8 — no authoritative source per Q-C-2),
          ``model_id`` / ``model_mode`` / ``tokens_input`` /
          ``tokens_output`` / ``tokens_total`` / ``wall_clock_s`` /
          ``agent_status`` / ``agent_url`` / ``doc_anchor``.
        """
        try:
            base = popolad.get_status(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from exc

        if not verbose:
            return base

        handle = popolad.state_store.get(task_id)
        verbose_block = _build_verbose_block(handle, base, popolad)
        base["verbose"] = verbose_block
        return base

    @app.get("/list")
    async def list_tasks(include_terminal: bool = False) -> list[dict[str, Any]]:
        return popolad.list_all(include_terminal=include_terminal)

    @app.post("/cancel/{task_id}", response_model=CancelResponse)
    async def cancel(task_id: str) -> CancelResponse:
        daemon_started_at = _DAEMON_STATE.get("started_at")
        try:
            result = await asyncio.to_thread(
                popolad.cancel_task,
                task_id,
                daemon_started_at=daemon_started_at,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return CancelResponse(
            task_id=result["task_id"],
            requested_signal=result["requested_signal"],
            escalated_to_sigkill=result["escalated_to_sigkill"],
            pid=result.get("pid"),
            result=result.get("result"),
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/probe", response_model=ProbeResponse)
    async def probe() -> ProbeResponse:
        started_at = _DAEMON_STATE.get("started_at") or datetime.now(UTC)
        uptime = (datetime.now(UTC) - started_at).total_seconds()
        active = len(popolad.list_active())
        return ProbeResponse(
            daemon_pid=os.getpid(),
            started_at=started_at.isoformat(timespec="milliseconds"),
            uptime_seconds=uptime,
            active_tasks=active,
            version=__version__,
        )

    @app.post("/relay/dispatch", response_model=RelayDispatchResponse)
    async def relay_dispatch_endpoint(
        req: RelayDispatchRequest,
    ) -> RelayDispatchResponse:
        """v0.8.8 T2.2.1: read-side RPC for ``popola relay <task_a>``.

        Returns the envelope info (``cursor_agent_id`` / ``repo_url`` /
        ``summary`` / ``model``) the CLI needs to build the relay
        payload. The CLI runs the secret scan + allowlist gate + audit
        row write + cloud POST locally; this RPC's job is purely to
        validate the source task is terminal and expose the few
        ``TaskHandle`` fields the CLI cannot read locally.

        Raises:
            HTTPException(404): source ``task_id`` not registered.
            HTTPException(400): source task not in a terminal state
                (``state ∈ {pending, queued, starting, running}``).
        """
        handle = popolad.state_store.get(req.source_task_id)
        if handle is None:
            raise HTTPException(
                status_code=404,
                detail=f"task not found: {req.source_task_id}",
            )
        if not handle.is_terminal():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"task_a is not in a terminal state "
                    f"(state={handle.state.value})"
                ),
            )

        repo_url: str | None = None
        pr_url: str | None = None
        model: str = ""
        summary: str = ""
        cloud_runs = handle.cloud_runs or {}
        if handle.cursor_run_id and handle.cursor_run_id in cloud_runs:
            run_meta = cloud_runs[handle.cursor_run_id]
            if isinstance(run_meta, dict):
                repo_obj = run_meta.get("repo_url")
                pr_obj = run_meta.get("pr_url")
                model_obj = run_meta.get("model")
                summary_obj = run_meta.get("summary")
                if isinstance(repo_obj, str):
                    repo_url = repo_obj
                if isinstance(pr_obj, str):
                    pr_url = pr_obj
                if isinstance(model_obj, str):
                    model = model_obj
                if isinstance(summary_obj, str):
                    summary = summary_obj

        return RelayDispatchResponse(
            source_task_id=req.source_task_id,
            cursor_agent_id=handle.cursor_agent_id,
            cursor_run_id=handle.cursor_run_id,
            repo_url=repo_url,
            pr_url=pr_url,
            summary=summary,
            model=model,
            state=handle.state.value,
            cloud_phase=handle.cloud_phase,
            runtime=handle.runtime,
        )

    @app.post("/relay", response_model=RelayResponse)
    async def relay_endpoint(req: RelayRequest) -> RelayResponse:
        try:
            child_task_id = await asyncio.to_thread(
                relay_primitive,
                popolad,
                source_task_id=req.source_task_id,
                target_cli=req.target_cli,
                payload=req.payload,
                reason=req.reason,
                constraints=req.constraints,
                source_cli=req.source_cli,
                prompt=req.prompt,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        parent_handle = popolad.state_store.get(req.source_task_id)
        source_cli = req.source_cli or (parent_handle.cli if parent_handle else "unknown")
        envelope = RelayHandoffEnvelope(
            source_cli=source_cli,
            target_cli=req.target_cli,
            source_task_id=req.source_task_id,
            payload=req.payload,
            reason=req.reason,
            constraints=req.constraints,
        )
        return RelayResponse(child_task_id=child_task_id, handoff_envelope=envelope)

    @app.post("/supervise", response_model=SuperviseResponse)
    async def supervise_endpoint(req: SuperviseRequest) -> SuperviseResponse:
        registry = get_default_registry()

        def _rpc_complete_callback(
            parent: str, child: str, payload: dict[str, Any]
        ) -> None:
            logger.info(
                "supervise.complete: parent=%s child=%s payload_keys=%s",
                parent,
                child,
                sorted(payload.keys()),
            )

        def _rpc_fail_callback(
            parent: str, child: str, payload: dict[str, Any]
        ) -> None:
            logger.info(
                "supervise.fail: parent=%s child=%s payload_keys=%s",
                parent,
                child,
                sorted(payload.keys()),
            )

        try:
            handle: SubscriptionHandle = registry.subscribe(
                req.parent_task_id,
                req.child_task_id,
                on_complete=_rpc_complete_callback,
                on_fail=_rpc_fail_callback,
                metadata={"callback_url": req.callback_url} if req.callback_url else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SuperviseResponse(
            subscription_id=handle.subscription_id,
            parent_task_id=handle.parent_task_id,
            child_task_id=handle.child_task_id,
        )

    @app.post("/federate", response_model=FederateRpcResponse)
    async def federate_endpoint(req: FederateRequest) -> FederateRpcResponse:
        if req.voting_strategy not in ("majority", "unanimous", "first_to_finish"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"federate: voting_strategy must be one of "
                    f"majority/unanimous/first_to_finish; got {req.voting_strategy!r}"
                ),
            )

        try:
            result = await asyncio.to_thread(
                federate_primitive,
                popolad,
                prompt=req.prompt,
                cli_list=req.cli_list,
                voting_strategy=req.voting_strategy,  # type: ignore[arg-type]
                cwd=req.cwd,
                extra=req.extra,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"federate dispatch failed: {exc}"
            ) from exc

        return FederateRpcResponse(
            federate_id=result.federate_id,
            child_task_ids=result.child_task_ids,
            cli_list=result.cli_list,
            voting_strategy=result.voting_strategy,
            dispatch_errors=result.dispatch_errors,
        )

    @app.post("/hitl/answer", response_model=HitlAnswerResponse)
    async def hitl_answer(req: HitlAnswerRequest) -> HitlAnswerResponse:
        """v0.3.0 F4.F: record a HITL reply.

        Atomic ``UPDATE popola_hitl SET status='answered' WHERE
        hitl_id=? AND status='pending'`` via :meth:`HITLStore.mark_answered`.
        Returns ``ok=False`` plus ``already_status`` / ``already_via``
        when this caller lost the cross-channel race (per workspace rule
        "No Silent Failures": never silently drop a duplicate reply).
        """
        store = popolad.hitl_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="HITL store not wired up; popolad started without F4 wiring",
            )
        result = await asyncio.to_thread(
            store.mark_answered,
            req.hitl_id,
            option_id=req.option_id,
            via=req.via,
            reason=req.reason,
            responder_id=req.responder_id,
        )
        return HitlAnswerResponse(
            ok=result.ok,
            hitl_id=req.hitl_id,
            already_status=result.already_status,
            already_via=result.already_via,
        )

    @app.get("/hitl/pending")
    async def hitl_pending(task_id: str | None = None) -> list[dict[str, Any]]:
        """v0.3.0 F4.F: list pending HITL prompts (optional task filter)."""
        store = popolad.hitl_store
        if store is None:
            raise HTTPException(status_code=503, detail="HITL store not wired up")
        return await asyncio.to_thread(store.list_pending, task_id=task_id)

    @app.post("/hitl/cloud/request", response_model=CloudHITLRequestResponse)
    async def hitl_cloud_request(req: CloudHITLRequestBody) -> CloudHITLRequestResponse:
        """Persist a cloud-agent HITL prompt and fan out to Lark (best-effort).

        v0.8.7 T2.1.3:

        - Validates ``cursor_agent_id`` / ``cursor_run_id`` non-empty
          (``invalid_context`` error per ``mcp-tool-contract.md`` §3.3 row 6).
        - Auto-derives the ``idempotency_key`` (or honors a caller-supplied
          ``metadata.idempotency_key``) and lets
          :meth:`CloudHITLBridge.submit_request` perform the SQL-only 1-hour
          dedup lookup; replays inside the window short-circuit to the
          existing ``hitl_id`` with ``deduped=True``.
        """
        bridge = bridge_for_daemon(popolad.hitl_store, send_lark=True)
        if bridge is None:
            raise HTTPException(
                status_code=503,
                detail="HITL store not wired up; popolad started without F4 wiring",
            )
        if not (req.cursor_agent_id and req.cursor_agent_id.strip()):
            raise HTTPException(
                status_code=400,
                detail="invalid_context: cursor_agent_id is required (non-empty)",
            )
        if not (req.cursor_run_id and req.cursor_run_id.strip()):
            raise HTTPException(
                status_code=400,
                detail="invalid_context: cursor_run_id is required (non-empty)",
            )
        logger.info(
            "hitl.cloud.request entry task_id=%s cursor_agent_id=%s cursor_run_id=%s",
            req.task_id,
            req.cursor_agent_id,
            req.cursor_run_id,
        )
        for opt in req.options:
            if "id" not in opt or "label" not in opt:
                raise HTTPException(
                    status_code=422,
                    detail="each option must include 'id' and 'label' keys",
                )
            if not str(opt["id"]).strip() or not str(opt["label"]).strip():
                raise HTTPException(
                    status_code=422,
                    detail="option id and label must be non-empty strings",
                )

        event_log = popolad.event_log(req.task_id)
        caller_meta = dict(req.metadata or {})
        idem_key = caller_meta.pop("idempotency_key", None)
        idem_key_str = idem_key if isinstance(idem_key, str) and idem_key else None

        def _submit() -> Any:
            return bridge.submit_request(
                task_id=req.task_id,
                cursor_agent_id=req.cursor_agent_id,
                cursor_run_id=req.cursor_run_id,
                prompt_title=req.prompt_title,
                prompt_body=req.prompt_body,
                options=req.options,
                metadata=caller_meta,
                timeout_s=req.timeout_s,
                idempotency_key=idem_key_str,
                event_log=event_log,
            )

        try:
            cloud_req = await asyncio.to_thread(_submit)
        except Exception as exc:
            logger.exception("hitl.cloud.request failed for task_id=%s", req.task_id)
            raise HTTPException(
                status_code=400,
                detail=f"failed to create cloud HITL request: {exc}",
            ) from exc

        deadline_iso = cloud_req.deadline_at.isoformat()
        if event_log is not None and not cloud_req.deduped:
            try:
                event_log.append(
                    "hitl.cloud_requested",
                    {
                        "task_id": req.task_id,
                        "hitl_id": cloud_req.hitl_id,
                        "cursor_agent_id": req.cursor_agent_id,
                        "cursor_run_id": req.cursor_run_id,
                        "prompt_title": req.prompt_title,
                        "options_count": len(req.options),
                        "deadline_at": deadline_iso,
                    },
                )
            except Exception:
                logger.exception(
                    "hitl.cloud.request event_log append failed task_id=%s hitl_id=%s",
                    req.task_id,
                    cloud_req.hitl_id,
                )

        existing_status = "pending"
        if cloud_req.deduped:
            row = bridge.store.get(cloud_req.hitl_id)
            if row is not None and str(row.get("status", "")) == "answered":
                existing_status = "answered"
        logger.info(
            "hitl.cloud.request exit hitl_id=%s task_id=%s deadline_at=%s deduped=%s",
            cloud_req.hitl_id,
            req.task_id,
            deadline_iso,
            cloud_req.deduped,
        )
        return CloudHITLRequestResponse(
            hitl_id=cloud_req.hitl_id,
            status=existing_status,  # type: ignore[arg-type]
            deadline_at=deadline_iso,
            cursor_agent_id=req.cursor_agent_id,
            cursor_run_id=req.cursor_run_id,
            deduped=cloud_req.deduped,
            lark_dispatched=cloud_req.lark_dispatched,
        )

    @app.get("/hitl/cloud/wait/{hitl_id}", response_model=None)
    async def hitl_cloud_wait(
        hitl_id: str,
        timeout_s: float = 55.0,
    ) -> JSONResponse:
        """Long-poll Human-in-the-loop state for a cloud-sourced prompt."""
        bridge = bridge_for_daemon(popolad.hitl_store, send_lark=False)
        if bridge is None:
            raise HTTPException(status_code=503, detail="HITL store not wired up")
        logger.info("hitl.cloud.wait entry hitl_id=%s timeout_s=%s", hitl_id, timeout_s)

        capped = max(0.1, min(60.0, timeout_s))

        def _wait() -> tuple[str, HITLReply | None]:
            return _cloud_wait_sync(bridge, hitl_id, capped)

        status_key, reply = await asyncio.to_thread(_wait)
        logger.info(
            "hitl.cloud.wait poll finished hitl_id=%s status_key=%s",
            hitl_id,
            status_key,
        )

        if status_key == "missing":
            raise HTTPException(status_code=404, detail=f"HITL id not found: {hitl_id}")
        if status_key == "answered" and reply is not None:
            body = CloudHITLWaitResponse(
                hitl_id=hitl_id,
                status="answered",
                answer=_reply_to_wait_payload(reply),
            )
            logger.info("hitl.cloud.wait exit hitl_id=%s status=answered", hitl_id)
            return JSONResponse(status_code=200, content=body.model_dump())
        if status_key == "timeout":
            out = CloudHITLWaitResponse(hitl_id=hitl_id, status="timeout", answer=None)
            logger.info("hitl.cloud.wait exit hitl_id=%s status=timeout", hitl_id)
            return JSONResponse(status_code=200, content=out.model_dump())
        payload = CloudHITLWaitResponse(
            hitl_id=hitl_id, status="pending", answer=None
        ).model_dump()
        logger.info("hitl.cloud.wait exit hitl_id=%s status=pending http=202", hitl_id)
        return JSONResponse(status_code=202, content=payload)

    @app.post(
        "/hitl/cloud/answer/{hitl_id}",
        responses={
            200: {"model": CloudHITLAnswerResponse},
            400: {"description": "mis-route — cursor tuple mismatch"},
            409: {"model": CloudHITLAnswerResponse},
        },
    )
    async def hitl_cloud_answer(hitl_id: str, req: CloudHITLAnswerBody) -> JSONResponse:
        """Record a HITL answer from MCP/CLI/cloud surfaces (non-Lark callers).

        v0.8.7 C1 wiring: when the caller supplies ``cursor_agent_id`` /
        ``cursor_run_id``, those are threaded into
        :meth:`CloudHITLBridge.submit_answer` as the ``expected_cursor_*``
        mis-route defense. A mismatch between the inbound tuple and the
        row's stored tuple → ``HTTP 400`` with the bridge's
        ``"mis-route:..."`` descriptor (per SECURITY R5 + invariant I-4).
        """
        bridge = bridge_for_daemon(popolad.hitl_store, send_lark=False)
        if bridge is None:
            raise HTTPException(status_code=503, detail="HITL store not wired up")
        channel = _narrow_hitl_channel(req.channel)

        logger.info(
            "hitl.cloud.answer entry hitl_id=%s option_id=%s channel=%s",
            hitl_id,
            req.option_id,
            channel,
        )

        row_before = bridge.store.get(hitl_id)
        if row_before is None:
            raise HTTPException(status_code=404, detail=f"HITL id not found: {hitl_id}")
        task_id = str(row_before.get("task_id") or "")

        expected_agent = req.cursor_agent_id
        expected_run = req.cursor_run_id

        def _answer() -> tuple[bool, str | None]:
            return bridge.submit_answer(
                hitl_id,
                req.option_id,
                responder_id=req.responder_id,
                reason=req.reason,
                channel=channel,
                expected_cursor_agent_id=expected_agent,
                expected_cursor_run_id=expected_run,
            )

        ok, descriptor = await asyncio.to_thread(_answer)

        # C1: a mis-route descriptor is shaped ``mis-route:expected_agent=...``
        # and indicates the bridge's _check_mis_route triggered. Translate
        # to HTTP 400 per acceptance criterion (c). Ordinary "already
        # answered" rejections still surface as HTTP 409 (the legacy
        # path) since they reflect a successful first-responder race
        # rather than an authentication-style failure.
        if not ok and descriptor and descriptor.startswith("mis-route:"):
            logger.warning(
                "hitl.cloud.answer rejected mis-route hitl_id=%s descriptor=%s",
                hitl_id,
                descriptor,
            )
            raise HTTPException(
                status_code=400,
                detail=descriptor,
            )

        payload = CloudHITLAnswerResponse(
            ok=ok,
            channel=channel if ok else None,
            already_answered_by=None if ok else (descriptor or "unknown"),
        )

        event_log = popolad.event_log(task_id) if task_id else None
        if ok and task_id and event_log is not None:
            try:
                event_log.append(
                    "hitl.cloud_answered",
                    {
                        "task_id": task_id,
                        "hitl_id": hitl_id,
                        "option_id": req.option_id,
                        "channel": channel,
                        "responder_id": req.responder_id,
                    },
                )
            except Exception:
                logger.exception(
                    "hitl.cloud.answer event_log append failed task_id=%s hitl_id=%s",
                    task_id,
                    hitl_id,
                )

        status_code = 200 if ok else 409
        logger.info(
            "hitl.cloud.answer exit hitl_id=%s ok=%s http=%s",
            hitl_id,
            ok,
            status_code,
        )
        return JSONResponse(status_code=status_code, content=payload.model_dump())

    @app.get("/attach_stream/{task_id}")
    async def attach_stream(task_id: str, request: Request, since: int = 0) -> StreamingResponse:
        if popolad.state_store.get(task_id) is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

        async def producer() -> AsyncIterator[bytes]:
            cursor = max(0, since)
            while True:
                if await request.is_disconnected():
                    logger.debug(
                        "SSE client disconnected for task=%s at cursor=%d",
                        task_id,
                        cursor,
                    )
                    return

                events = await asyncio.to_thread(
                    _read_tail, popolad, task_id, cursor
                )
                for ev in events:
                    yield _format_sse(ev)
                    cursor += 1

                handle = popolad.state_store.get(task_id)
                if handle is None or handle.is_terminal():
                    final_events = await asyncio.to_thread(
                        _read_tail, popolad, task_id, cursor
                    )
                    for ev in final_events:
                        yield _format_sse(ev)
                        cursor += 1
                    return

                await asyncio.sleep(_ATTACH_POLL_INTERVAL_S)

        return StreamingResponse(producer(), media_type="text/event-stream")


_COST_DOC_ANCHOR: str = (
    "https://cursor.com/docs/cloud-agent/api/endpoints.md#get-a-run"
)
"""Public ``cost-fields.md`` §3.2 doc anchor.

Surfaced verbatim in the ``--json --verbose`` ``verbose.doc_anchor`` field
so machine readers can verify field provenance independent of the
PopolaLoom version (see EOQ-B1 default-include rationale)."""


def _build_verbose_block(
    handle: Any,
    base: dict[str, Any],
    popolad: Popolad,
) -> dict[str, Any]:
    """Return the ``verbose`` JSON block per ``cost-fields.md`` §3.2.

    Schema (10 keys):

    - ``cost_estimate_usd`` — always ``None`` in v0.8.8 (Q-C-2 locked:
      no authoritative source for per-run cost).
    - ``model_id`` — recorded model id, or ``None`` when popolad's
      hard-coded default was substituted (detected via
      ``cloud.model_default_used`` event).
    - ``model_mode`` — ``"std"`` by default; ``"max"`` /
      ``"thinking-high"`` when ``extra.model_params`` carried a
      non-default reasoning value.
    - ``tokens_input`` / ``tokens_output`` / ``tokens_total`` — always
      ``None`` in v0.8.8 (F7/F8/F11 not safely available on the public
      REST/SSE wire — see §2.2).
    - ``wall_clock_s`` — derived from ``handle.started_at`` and
      ``handle.completed_at`` (or ``now()`` for live tasks); ``None``
      when ``started_at`` is missing.
    - ``agent_status`` — derived from ``handle.cloud_phase`` (or the
      coarse ``handle.state`` for local tasks); ``None`` when neither
      is populated.
    - ``agent_url`` — ``https://cursor.com/agents?id=<agent_id>`` when
      ``handle.cursor_agent_id`` is set; ``None`` otherwise.
    - ``doc_anchor`` — :data:`_COST_DOC_ANCHOR` (locked literal).

    Args:
        handle: :class:`TaskHandle` snapshot (or ``None`` for tasks the
            state store no longer holds — defensive-only path).
        base: Base status dict from :meth:`Popolad.get_status` — used
            for ``state``, ``started_at`` etc. when ``handle`` is
            ``None``.
        popolad: Daemon singleton — needed to read the per-task
            event log for ``cloud.model_default_used`` detection.
    """
    cost_estimate_usd: float | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_total: int | None = None

    cmd_extra = _parse_cloud_cmd_extra(handle)
    model_default_used = _has_model_default_used_event(popolad, base.get("task_id"))

    model_id: str | None = None
    if cmd_extra is not None:
        raw_model = cmd_extra.get("model")
        if isinstance(raw_model, str) and raw_model:
            model_id = raw_model
    if model_default_used:
        model_id = None

    model_mode: str = _resolve_model_mode(cmd_extra)

    wall_clock_s = _resolve_wall_clock_s(handle, base)

    agent_status = _resolve_agent_status(handle, base)
    agent_url = _resolve_agent_url(handle, base)

    return {
        "cost_estimate_usd": cost_estimate_usd,
        "model_id": model_id,
        "model_mode": model_mode,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_total": tokens_total,
        "wall_clock_s": wall_clock_s,
        "agent_status": agent_status,
        "agent_url": agent_url,
        "doc_anchor": _COST_DOC_ANCHOR,
    }


def _parse_cloud_cmd_extra(handle: Any) -> dict[str, Any] | None:
    """Return the normalized ``extra`` dict from a cloud task's cmd marker.

    Returns ``None`` for non-cloud tasks, missing handles, or when the
    marker JSON cannot be parsed (No-Silent-Failures: a WARN-level log
    fires but the verbose surface degrades to ``model: -``).
    """
    if handle is None:
        return None
    cmd = getattr(handle, "cmd", None)
    if not isinstance(cmd, list) or len(cmd) < 3:
        return None
    if cmd[:2] != ["__cloud__", "cursor-cloud"]:
        return None
    raw = cmd[2]
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "verbose status: failed to parse cloud cmd marker JSON: %s", exc
        )
        return None
    if not isinstance(payload, dict):
        return None
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        return None
    return extra


def _has_model_default_used_event(
    popolad: Popolad, task_id: str | None
) -> bool:
    """Return ``True`` when the task's event log contains a default-used row.

    Scans only the first ~64 events to keep the verbose status path cheap
    on long-running tasks; ``cloud.model_default_used`` is emitted by
    rpc's dispatch handler within microseconds of the task being created
    so it always lives at the head of the file.
    """
    if not task_id:
        return False
    event_log = popolad.event_log(task_id)
    if event_log is None:
        return False
    try:
        events = event_log.tail(since_index=0)
    except (FileNotFoundError, OSError):
        return False
    for ev in events[:64]:
        if isinstance(ev, dict) and ev.get("type") == "cloud.model_default_used":
            return True
    return False


def _resolve_model_mode(extra: dict[str, Any] | None) -> str:
    """Map ``extra.model_params`` to a short mode label per spec §3.1.

    The marker's ``extra`` may include an ``model_params`` array of
    ``{id, value}`` pairs (Cursor's per-model parameter syntax — F6 in
    the catalog). When present and non-default, surface a short label
    (``"max"`` / ``"thinking-high"`` etc.) so the user sees the
    higher-cost dimension at a glance.
    """
    if extra is None:
        return "std"
    params = extra.get("model_params")
    if not isinstance(params, list):
        return "std"
    for entry in params:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        pval = entry.get("value")
        if pid == "max_mode" and pval is True:
            return "max"
        if pid == "thinking" and isinstance(pval, str) and pval not in ("", "off"):
            return f"thinking-{pval}"
    return "std"


def _resolve_wall_clock_s(handle: Any, base: dict[str, Any]) -> float | None:
    """Compute wall-clock duration in seconds (1-decimal precision).

    Terminal tasks: ``completed_at - started_at``.
    Live tasks: ``now() - started_at`` (approximation; spec §4 caveat 2).
    Missing ``started_at``: ``None`` (No-Silent-Failures).
    """
    started_at = getattr(handle, "started_at", None) if handle is not None else None
    completed_at = (
        getattr(handle, "completed_at", None) if handle is not None else None
    )
    if started_at is None:
        return None
    end = completed_at if completed_at is not None else datetime.now(UTC)
    try:
        delta = (end - started_at).total_seconds()
    except (TypeError, ValueError):
        return None
    if delta < 0:
        delta = 0.0
    return float(round(delta, 1))


def _resolve_agent_status(handle: Any, base: dict[str, Any]) -> str | None:
    """Return a short ``agent_status`` string for the verbose surface.

    Cloud runtime: prefer ``cloud_phase`` (e.g. ``CREATING`` / ``RUNNING``
    / ``FINISHED``). Local runtime: fall back to the coarse ``state``.
    Returns ``None`` when neither is populated (legacy snapshot).
    """
    if handle is not None:
        cloud_phase = getattr(handle, "cloud_phase", None)
        if isinstance(cloud_phase, str) and cloud_phase:
            return cloud_phase
        state = getattr(handle, "state", None)
        if state is not None:
            return str(state)
    fallback = base.get("cloud_phase") or base.get("state")
    if isinstance(fallback, str) and fallback:
        return fallback
    return None


def _resolve_agent_url(handle: Any, base: dict[str, Any]) -> str | None:
    """Build ``https://cursor.com/agents?id=<agent_id>`` when available.

    Returns ``None`` for local-runtime tasks (no Cursor dashboard link).
    """
    agent_id: str | None = None
    if handle is not None:
        candidate = getattr(handle, "cursor_agent_id", None)
        if isinstance(candidate, str) and candidate:
            agent_id = candidate
    if agent_id is None:
        candidate2 = base.get("cursor_agent_id")
        if isinstance(candidate2, str) and candidate2:
            agent_id = candidate2
    if agent_id is None:
        return None
    return f"https://cursor.com/agents?id={agent_id}"


def _read_tail(popolad: Popolad, task_id: str, since_index: int) -> list[dict[str, Any]]:
    """Sync helper run in a thread to read NDJSON tail from disk."""
    event_log = popolad.event_log(task_id)
    if event_log is None:
        return []
    try:
        return event_log.tail(since_index=since_index)
    except FileNotFoundError:
        return []


def _format_sse(envelope: dict[str, Any]) -> bytes:
    """Format a CloudEvents envelope as a single SSE ``data:`` frame."""
    payload = json.dumps(envelope, ensure_ascii=False, default=str)
    return f"data: {payload}\n\n".encode()


def _apply_evolution_round_prepend(
    prompt: str,
    *,
    round_num: int,
    max_rounds: int = 5,
    prior_nines: float = 0.0,
    gate_threshold: float = 0.85,
) -> str:
    """Prepend WorkflowContext + reinforcement section to ``prompt``.

    v0.3.0 F2.5.4 contract: when ``POST /dispatch?evolution_round=N`` is
    invoked, the daemon checks for ``~/.popola/round-<N-1>-evidence.md``
    and (if present) renders its top-5 findings as a reinforcement
    block before the user prompt.

    Args:
        prompt: original user prompt.
        round_num: ≥ 1; current round number.
        max_rounds: total round budget; default 5.
        prior_nines: composite from previous round (0..1).
        gate_threshold: inner gate floor; default 0.85.

    Returns:
        str: prompt with WorkflowContext + (optional) reinforcement
        section prepended.

    Raises:
        ValueError: when round_num exceeds max_rounds (per
            :class:`WorkflowContext` invariants).
    """
    from popolaloom.evolution.reinforcement import render_reinforcement_section
    from popolaloom.evolution.skill_inject import (
        check_skill_present,
        prepend_workflow_context,
    )

    check_skill_present()

    reinforcement_text = ""
    if round_num >= 2:
        prior_round = round_num - 1
        evidence_path = (
            Path.home() / ".popola" / f"round-{prior_round}-evidence.md"
        )
        if evidence_path.is_file():
            try:
                content = evidence_path.read_text(encoding="utf-8")
                lines = [
                    line.strip()
                    for line in content.splitlines()
                    if line.strip().startswith("- ")
                ]
                findings = [line[2:].strip() for line in lines][:5]
                if findings:
                    reinforcement_text = render_reinforcement_section(
                        findings, round_num=round_num
                    )
            except OSError:
                logger.exception(
                    "evolution_round: could not read %s; continuing without reinforcement",
                    evidence_path,
                )

    return prepend_workflow_context(
        prompt,
        round_num=round_num,
        max_rounds=max_rounds,
        prior_nines=prior_nines,
        reinforcement=reinforcement_text,
        gate_threshold=gate_threshold,
    )
