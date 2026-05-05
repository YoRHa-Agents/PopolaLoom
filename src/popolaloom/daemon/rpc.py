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
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
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
            # at lifespan exit (release-notes-v0.5.1.md known limitation #2);
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

        events_log = popolad.events_dir / f"{task_id}.jsonl"
        return DispatchResponse(
            task_id=task_id,
            events_log=str(events_log),
            cli=req.cli,
        )

    @app.get("/status/{task_id}")
    async def status(task_id: str) -> dict[str, Any]:
        try:
            return popolad.get_status(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}") from exc

    @app.get("/list")
    async def list_tasks(include_terminal: bool = False) -> list[dict[str, Any]]:
        return popolad.list_all(include_terminal=include_terminal)

    @app.post("/cancel/{task_id}", response_model=CancelResponse)
    async def cancel(task_id: str) -> CancelResponse:
        try:
            result = await asyncio.to_thread(popolad.cancel_task, task_id)
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
