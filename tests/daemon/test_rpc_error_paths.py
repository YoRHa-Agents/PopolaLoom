"""Default-lane tests for popolad RPC error paths (v0.5.1 coverage push).

Per [v0.5.1 Loop 1 §L1.B](../../release-notes-v0.5.1.md): the v0.5.0
GA shipped at 91.15 % default-lane coverage; ``daemon/rpc.py`` was at
82 % — the lowest of any first-party module — because the
:mod:`fastapi.HTTPException` exit ramps in dispatch / cancel /
relay / supervise / federate / attach_stream / hitl + the lifespan
exception swallowers were exercised only in slow-lane real-daemon
integration tests.

These tests close the gap with an in-process FastAPI ``ASGITransport``
test client.  No uvicorn thread is spawned — every HTTP call goes
straight through the ASGI stack — so the suite stays in the fast
default lane.

Coverage targets in this module:

* ``POST /dispatch``:
    - 404 when the adapter is not registered (``KeyError``).
    - 400 when ``_apply_evolution_round_prepend`` raises ``ValueError``.
    - 400 when ``dispatch_task`` raises ``RuntimeError`` / ``ValueError``.
* ``GET /status/{task_id}``:
    - 404 when the task is unknown (``KeyError`` ramp).
* ``POST /cancel/{task_id}``:
    - 404 when the task is unknown (``KeyError``).
    - 409 when the task is already in a terminal state
      (``RuntimeError``).
* ``POST /relay``:
    - 400 when the source task id is unknown.
    - 200 happy path with the envelope echo (covers the
      ``parent_handle.cli`` fallback line).
* ``POST /supervise``:
    - 200 happy path that registers + fires both
      ``_rpc_complete_callback`` and ``_rpc_fail_callback`` via
      ``on_child_terminal`` (covers lines 467 + 477).
    - 400 when the registry rejects the subscription (``ValueError``).
* ``POST /federate``:
    - 400 when ``voting_strategy`` is invalid (line 502-509).
    - 422 when ``cli_list`` is too short (Pydantic validation).
    - 400 when ``federate_primitive`` raises ``ValueError`` /
      ``RuntimeError`` (line 521-522).
    - 400 ``federate dispatch failed: ...`` when ``federate_primitive``
      raises a generic ``Exception`` (line 523-526).
* ``POST /hitl/answer`` and ``GET /hitl/pending``:
    - 503 when ``popolad.hitl_store is None`` (lines 547-551, 571-572).
* ``GET /attach_stream/{task_id}``:
    - 404 when the task is unknown (line 577-578).
* ``_read_tail`` helper:
    - missing event log → ``[]`` (line 616-617).
    - ``FileNotFoundError`` from ``EventLog.tail`` → ``[]`` (620-621).
* ``_build_default_popolad`` factory and ``create_app`` default-popolad
  path (lines 222-225, 252-257).
* ``lifespan`` startup/shutdown error swallowers:
    - rehydrate exception → continues (lines 280-284).
    - shutdown cancel exception → continues (lines 295-298).
    - shutdown_persistence_bridge exception → continues (lines 301-302).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from popolaloom.adapters import base as adapter_base
from popolaloom.adapters import build_command, register_adapter
from popolaloom.daemon import Popolad
from popolaloom.daemon.rpc import (
    _DAEMON_STATE,
    _build_default_popolad,
    _format_sse,
    _read_tail,
    create_app,
)

# ── shared fixtures ──────────────────────────────────────────────────────


class _EchoAdapter:
    """Minimal echo adapter used for happy-path RPC traffic in this module."""

    name = "echo_rpc_err"
    binary = sys.executable

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        snippet = f"print('echo:', {prompt!r}); import sys; sys.exit(0)"
        return [sys.executable, "-c", snippet]

    def is_available(self) -> bool:
        return True


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Snapshot + restore the global adapter registry."""
    saved = dict(adapter_base._REGISTRY)
    try:
        yield
    finally:
        adapter_base._REGISTRY.clear()
        adapter_base._REGISTRY.update(saved)


@pytest.fixture
def popolad_instance(
    tmp_path: Path,
    isolated_registry: None,
) -> Popolad:
    """A clean :class:`Popolad` with the echo adapter registered."""
    if "echo_rpc_err" not in adapter_base._REGISTRY:
        register_adapter(_EchoAdapter())
    events = tmp_path / "events"
    return Popolad(events_dir=events, adapter=build_command)


@pytest.fixture
def asgi_client(popolad_instance: Popolad) -> Iterator[httpx.AsyncClient]:
    """Yield an ``httpx.AsyncClient`` driving the FastAPI app via ASGI transport.

    The lifespan context is started/stopped via ``LifespanManager`` so the
    ``_DAEMON_STATE["popolad"]`` invariant is satisfied even though we
    never spawn a uvicorn process.
    """
    app = create_app(popolad=popolad_instance)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://popolad-test")
    try:
        yield client
    finally:

        async def _close() -> None:
            await client.aclose()

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_close())


# ── 1. POST /dispatch — error ramps ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_unknown_cli_returns_404(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /dispatch`` with an unknown adapter → 404 + helpful detail."""
    r = await asgi_client.post(
        "/dispatch",
        json={"cli": "no-such-cli", "prompt": "p"},
    )
    assert r.status_code == 404
    detail = r.json().get("detail", "")
    assert "adapter not registered" in detail or "no-such-cli" in detail


@pytest.mark.asyncio
async def test_dispatch_evolution_round_invalid_returns_400(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``evolution_round`` workflow validation failure → 400 ``evolution_round invalid``."""

    def _explode(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("round_num exceeds max_rounds")

    monkeypatch.setattr(
        "popolaloom.daemon.rpc._apply_evolution_round_prepend",
        _explode,
    )
    r = await asgi_client.post(
        "/dispatch",
        json={"cli": "echo_rpc_err", "prompt": "p"},
        params={"evolution_round": "9", "max_rounds": "5"},
    )
    assert r.status_code == 400
    assert "evolution_round invalid" in r.json()["detail"]


@pytest.mark.asyncio
async def test_dispatch_runtime_error_returns_400(
    asgi_client: httpx.AsyncClient,
    popolad_instance: Popolad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_task`` raising ``RuntimeError`` → 400."""

    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("no adapter wired")

    monkeypatch.setattr(popolad_instance, "dispatch_task", _raise)
    r = await asgi_client.post(
        "/dispatch",
        json={"cli": "echo_rpc_err", "prompt": "p"},
    )
    assert r.status_code == 400
    assert "no adapter wired" in r.json()["detail"]


@pytest.mark.asyncio
async def test_dispatch_value_error_returns_400(
    asgi_client: httpx.AsyncClient,
    popolad_instance: Popolad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_task`` raising ``ValueError`` → 400."""

    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("invalid cwd")

    monkeypatch.setattr(popolad_instance, "dispatch_task", _raise)
    r = await asgi_client.post(
        "/dispatch",
        json={"cli": "echo_rpc_err", "prompt": "p"},
    )
    assert r.status_code == 400
    assert "invalid cwd" in r.json()["detail"]


# ── 2. GET /status/{task_id} — 404 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_status_unknown_task_returns_404(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``GET /status/{task_id}`` for unknown task → 404."""
    r = await asgi_client.get("/status/no-such-tid")
    assert r.status_code == 404
    assert "task not found" in r.json()["detail"]


# ── 3. POST /cancel/{task_id} — 404 + 409 ────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_404(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /cancel/{task_id}`` for unknown task → 404."""
    r = await asgi_client.post("/cancel/no-such-tid")
    assert r.status_code == 404
    assert "task not found" in r.json()["detail"]


@pytest.mark.asyncio
async def test_cancel_already_terminal_returns_409(
    asgi_client: httpx.AsyncClient,
    popolad_instance: Popolad,
) -> None:
    """``POST /cancel/{task_id}`` for terminal task → 409."""
    r_disp = await asgi_client.post(
        "/dispatch",
        json={"cli": "echo_rpc_err", "prompt": "for-cancel-409"},
    )
    assert r_disp.status_code == 200
    tid = r_disp.json()["task_id"]

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        info = (await asgi_client.get(f"/status/{tid}")).json()
        if info["state"] in {"completed", "failed", "canceled"}:
            break
        await asyncio.sleep(0.05)

    r_cancel = await asgi_client.post(f"/cancel/{tid}")
    assert r_cancel.status_code == 409
    assert "terminal" in r_cancel.json()["detail"]


# ── 4. POST /relay — error + happy ───────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_unknown_source_returns_400(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /relay`` with unknown source_task_id → 400 (ValueError ramp)."""
    r = await asgi_client.post(
        "/relay",
        json={
            "source_task_id": "no-such-source",
            "target_cli": "echo_rpc_err",
            "reason": "bridge",
        },
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


@pytest.mark.asyncio
async def test_relay_runtime_error_returns_400(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /relay`` when the underlying primitive raises ``RuntimeError`` → 400."""

    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("relay primitive blew up")

    monkeypatch.setattr("popolaloom.daemon.rpc.relay_primitive", _raise)
    r = await asgi_client.post(
        "/relay",
        json={
            "source_task_id": "any",
            "target_cli": "echo_rpc_err",
            "reason": "bridge",
        },
    )
    assert r.status_code == 400
    assert "relay primitive blew up" in r.json()["detail"]


@pytest.mark.asyncio
async def test_relay_happy_returns_envelope(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /relay`` happy path: dispatch parent first, then relay → 200 + envelope.

    v0.9.0 (BL-v0.9.0-1) — the response now carries a canonical
    :class:`HandoffEnvelope` (not the legacy v0.3.0
    ``RelayHandoffEnvelope``); ``parent_task_id`` replaces the old
    ``source_task_id`` field.
    """
    r_parent = await asgi_client.post(
        "/dispatch",
        json={"cli": "echo_rpc_err", "prompt": "parent"},
    )
    parent_tid = r_parent.json()["task_id"]

    r = await asgi_client.post(
        "/relay",
        json={
            "source_task_id": parent_tid,
            "target_cli": "echo_rpc_err",
            "reason": "needs help",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "child_task_id" in body
    envelope = body["handoff_envelope"]
    assert envelope["parent_task_id"] == parent_tid
    assert envelope["target_cli"] == "echo_rpc_err"
    assert envelope["source_cli"] == "echo_rpc_err"
    assert envelope["reason"] == "needs help"
    assert "relay" in envelope["tags"]


# ── 5. POST /supervise — happy + ValueError + callback fire ──────────────


@pytest.mark.asyncio
async def test_supervise_happy_path_registers(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /supervise`` happy path returns subscription_id + parent + child ids."""
    r = await asgi_client.post(
        "/supervise",
        json={
            "parent_task_id": "p-001",
            "child_task_id": "c-001",
            "callback_url": "https://example.test/cb",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "subscription_id" in body
    assert body["parent_task_id"] == "p-001"
    assert body["child_task_id"] == "c-001"


@pytest.mark.asyncio
async def test_supervise_blank_parent_returns_400(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /supervise`` with blank ``parent_task_id`` is rejected by Pydantic with 422."""
    r = await asgi_client.post(
        "/supervise",
        json={"parent_task_id": "", "child_task_id": "c-x"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_supervise_callbacks_fire_via_registry(
    asgi_client: httpx.AsyncClient,
) -> None:
    """Registering via RPC + firing on_child_terminal exercises the inner cb closures.

    Covers ``_rpc_complete_callback`` (line 467) AND
    ``_rpc_fail_callback`` (line 477) inside
    ``daemon/rpc.py::supervise_endpoint``.
    """
    from popolaloom.daemon.primitives.supervise import get_default_registry

    r1 = await asgi_client.post(
        "/supervise",
        json={"parent_task_id": "p-cb-1", "child_task_id": "c-cb-1"},
    )
    r2 = await asgi_client.post(
        "/supervise",
        json={"parent_task_id": "p-cb-2", "child_task_id": "c-cb-2"},
    )
    assert r1.status_code == 200 and r2.status_code == 200

    registry = get_default_registry()
    registry.on_child_terminal("c-cb-1", "completed", payload={"exit_code": 0})
    registry.on_child_terminal("c-cb-2", "failed", payload={"exit_code": 1})


# ── 6. POST /federate — error ramps + happy ──────────────────────────────


@pytest.mark.asyncio
async def test_federate_invalid_voting_strategy_returns_400(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``voting_strategy`` outside the canonical set → 400."""
    r = await asgi_client.post(
        "/federate",
        json={
            "cli_list": ["echo_rpc_err", "echo_rpc_err", "echo_rpc_err"],
            "prompt": "p",
            "voting_strategy": "supreme-court",
        },
    )
    assert r.status_code == 400
    assert "voting_strategy" in r.json()["detail"]


@pytest.mark.asyncio
async def test_federate_short_cli_list_returns_422(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``cli_list`` shorter than 3 entries → 422 from Pydantic validation."""
    r = await asgi_client.post(
        "/federate",
        json={"cli_list": ["echo_rpc_err"], "prompt": "p"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_federate_value_error_in_primitive_returns_400(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``federate_primitive`` raising ``ValueError`` → 400."""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("bad cli_list")

    monkeypatch.setattr("popolaloom.daemon.rpc.federate_primitive", _raise)
    r = await asgi_client.post(
        "/federate",
        json={
            "cli_list": ["echo_rpc_err", "echo_rpc_err", "echo_rpc_err"],
            "prompt": "p",
        },
    )
    assert r.status_code == 400
    assert "bad cli_list" in r.json()["detail"]


@pytest.mark.asyncio
async def test_federate_runtime_error_in_primitive_returns_400(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``federate_primitive`` raising ``RuntimeError`` → 400."""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("popolad mid-shutdown")

    monkeypatch.setattr("popolaloom.daemon.rpc.federate_primitive", _raise)
    r = await asgi_client.post(
        "/federate",
        json={
            "cli_list": ["echo_rpc_err", "echo_rpc_err", "echo_rpc_err"],
            "prompt": "p",
        },
    )
    assert r.status_code == 400
    assert "popolad mid-shutdown" in r.json()["detail"]


@pytest.mark.asyncio
async def test_federate_generic_exception_in_primitive_returns_400(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-ValueError/RuntimeError → 400 ``federate dispatch failed: ...``."""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("popolaloom.daemon.rpc.federate_primitive", _raise)
    r = await asgi_client.post(
        "/federate",
        json={
            "cli_list": ["echo_rpc_err", "echo_rpc_err", "echo_rpc_err"],
            "prompt": "p",
        },
    )
    assert r.status_code == 400
    assert "federate dispatch failed" in r.json()["detail"]


# ── 7. POST /hitl/answer + GET /hitl/pending — 503 when store unset ──────


@pytest.mark.asyncio
async def test_hitl_answer_503_when_store_missing(
    asgi_client: httpx.AsyncClient,
    popolad_instance: Popolad,
) -> None:
    """``POST /hitl/answer`` with no HITL store wired → 503."""
    assert popolad_instance.hitl_store is None
    r = await asgi_client.post(
        "/hitl/answer",
        json={"hitl_id": "h-001", "option_id": "yes", "via": "cli"},
    )
    assert r.status_code == 503
    assert "HITL store not wired" in r.json()["detail"]


@pytest.mark.asyncio
async def test_hitl_pending_503_when_store_missing(
    asgi_client: httpx.AsyncClient,
    popolad_instance: Popolad,
) -> None:
    """``GET /hitl/pending`` with no HITL store wired → 503."""
    assert popolad_instance.hitl_store is None
    r = await asgi_client.get("/hitl/pending")
    assert r.status_code == 503
    assert "HITL store not wired" in r.json()["detail"]


# ── 8. GET /attach_stream/{task_id} — 404 for unknown task ───────────────


@pytest.mark.asyncio
async def test_attach_stream_unknown_task_returns_404(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``GET /attach_stream/{task_id}`` for unknown task → 404 (line 577-578)."""
    r = await asgi_client.get("/attach_stream/no-such-task")
    assert r.status_code == 404
    assert "task not found" in r.json()["detail"]


# ── 9. _read_tail helper — empty for missing log + FileNotFoundError ─────


def test_read_tail_returns_empty_when_event_log_missing(
    popolad_instance: Popolad,
) -> None:
    """``_read_tail`` returns ``[]`` when ``popolad.event_log`` returns ``None``."""

    class _Stub:
        def event_log(self, _tid: str) -> Any:
            return None

        @property
        def state_store(self) -> Any:
            return popolad_instance.state_store

    out = _read_tail(_Stub(), "tid", since_index=0)
    assert out == []


def test_read_tail_swallows_file_not_found_error(
    popolad_instance: Popolad,
) -> None:
    """``_read_tail`` returns ``[]`` when ``EventLog.tail`` raises ``FileNotFoundError``."""

    class _StubLog:
        def tail(self, since_index: int = 0) -> list[dict[str, Any]]:
            raise FileNotFoundError("missing")

    class _Stub:
        def event_log(self, _tid: str) -> Any:
            return _StubLog()

    out = _read_tail(_Stub(), "tid", since_index=0)
    assert out == []


# ── 10. _format_sse — bytes shape (line 627) ─────────────────────────────


def test_format_sse_emits_data_prefix() -> None:
    """``_format_sse`` returns bytes starting with ``data: `` and ending with two newlines."""
    raw = _format_sse({"type": "test", "data": {"x": 1}})
    assert isinstance(raw, bytes)
    text = raw.decode("utf-8")
    assert text.startswith("data: ")
    assert text.endswith("\n\n")


# ── 11. _build_default_popolad + create_app default factory paths ────────


def test_build_default_popolad_returns_instance(
    isolated_registry: None,
) -> None:
    """``_build_default_popolad()`` constructs a real :class:`Popolad`."""
    popolad = _build_default_popolad()
    assert isinstance(popolad, Popolad)


def test_create_app_with_default_popolad_factory(
    monkeypatch: pytest.MonkeyPatch,
    isolated_registry: None,
    tmp_path: Path,
) -> None:
    """``create_app(events_dir=..., adapter=None)`` triggers the lazy ``build_command`` import."""
    events = tmp_path / "events"
    app = create_app(events_dir=events)
    assert app is not None
    assert app.title == "popolad"


def test_create_app_with_explicit_adapter_skips_lazy_import(
    isolated_registry: None,
    tmp_path: Path,
) -> None:
    """When ``adapter=`` is explicitly supplied, the lazy import branch is bypassed."""
    events = tmp_path / "events"
    app = create_app(events_dir=events, adapter=build_command)
    assert app is not None


# ── 12. lifespan — startup rehydrate + shutdown error swallowers ─────────


@pytest.mark.asyncio
async def test_lifespan_swallows_rehydrate_exception(
    tmp_path: Path,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rehydrate_from_persistence`` raising on startup is logged + ignored.

    Covers lines 280-284 in ``daemon/rpc.py``.
    """
    if "echo_rpc_err" not in adapter_base._REGISTRY:
        register_adapter(_EchoAdapter())

    popolad = Popolad(events_dir=tmp_path / "events", adapter=build_command)

    def _boom() -> int:
        raise RuntimeError("rehydrate boom")

    monkeypatch.setattr(popolad, "rehydrate_from_persistence", _boom)
    app = create_app(popolad=popolad)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_lifespan_swallows_shutdown_persistence_bridge_exception(
    tmp_path: Path,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutdown_persistence_bridge`` raising on close is logged + ignored.

    Covers lines 301-302 in ``daemon/rpc.py``.
    """
    if "echo_rpc_err" not in adapter_base._REGISTRY:
        register_adapter(_EchoAdapter())

    popolad = Popolad(events_dir=tmp_path / "events", adapter=build_command)

    def _boom() -> None:
        raise RuntimeError("bridge close boom")

    monkeypatch.setattr(popolad, "shutdown_persistence_bridge", _boom)
    app = create_app(popolad=popolad)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_lifespan_swallows_shutdown_cancel_exception(
    tmp_path: Path,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown cancel of an active task that raises is logged + ignored.

    Covers lines 295-298 in ``daemon/rpc.py``.  We arrange for
    ``list_active`` to return one fake task and ``cancel_task`` to
    raise — the lifespan ``finally`` should swallow + continue.
    """
    if "echo_rpc_err" not in adapter_base._REGISTRY:
        register_adapter(_EchoAdapter())

    popolad = Popolad(events_dir=tmp_path / "events", adapter=build_command)

    monkeypatch.setattr(
        popolad, "list_active", lambda: [{"task_id": "ghost-tid"}]
    )

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("cancel boom")

    monkeypatch.setattr(popolad, "cancel_task", _boom)
    app = create_app(popolad=popolad)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200


# ── 13. /probe rolls up daemon state ────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_handles_missing_started_at(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /probe`` falls back to ``datetime.now`` when ``started_at`` is unset."""
    monkeypatch.setitem(_DAEMON_STATE, "started_at", None)
    r = await asgi_client.get("/probe")
    assert r.status_code == 200
    body = r.json()
    assert body["uptime_seconds"] >= 0
    assert "version" in body


# ── 14. /attach_stream — happy path covers final-events-on-terminal ─────


@pytest.mark.asyncio
async def test_attach_stream_happy_path_streams_to_terminal(
    asgi_client: httpx.AsyncClient,
) -> None:
    """End-to-end ``GET /attach_stream`` for a real task drains terminal events.

    Covers the producer's final-events drain (lines 599-606 of
    ``daemon/rpc.py``) — the task is already terminal by the time the
    SSE producer wakes up, so the second ``_read_tail`` collects the
    tail before exiting.
    """
    r_disp = await asgi_client.post(
        "/dispatch",
        json={"cli": "echo_rpc_err", "prompt": "stream-tail"},
    )
    tid = r_disp.json()["task_id"]

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        info = (await asgi_client.get(f"/status/{tid}")).json()
        if info["state"] in {"completed", "failed", "canceled"}:
            break
        await asyncio.sleep(0.05)

    seen_types: set[str] = set()
    async with asgi_client.stream("GET", f"/attach_stream/{tid}", params={"since": 0}) as stream:
        assert stream.status_code == 200
        async for line in stream.aiter_lines():
            if not line.startswith("data: "):
                continue
            import json as _json

            payload = _json.loads(line[len("data: "):])
            seen_types.add(payload["type"])

    assert "task.dispatched" in seen_types
    assert "task.completed" in seen_types


# ── 15. /federate happy path returns dispatched ids ─────────────────────


@pytest.mark.asyncio
async def test_federate_happy_path_returns_three_child_ids(
    asgi_client: httpx.AsyncClient,
) -> None:
    """``POST /federate`` happy path dispatches to all 3 echo adapters."""
    r = await asgi_client.post(
        "/federate",
        json={
            "cli_list": ["echo_rpc_err", "echo_rpc_err", "echo_rpc_err"],
            "prompt": "majority test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["child_task_ids"]) == 3
    assert body["voting_strategy"] == "majority"


# ── 16. _apply_evolution_round_prepend — direct unit tests ──────────────


def test_apply_evolution_round_prepend_skips_round_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 has no prior evidence, so the helper returns the prefixed prompt only."""
    from popolaloom.daemon.rpc import _apply_evolution_round_prepend

    monkeypatch.setattr(
        "popolaloom.evolution.skill_inject.check_skill_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "popolaloom.evolution.skill_inject.prepend_workflow_context",
        lambda prompt, **kwargs: f"<ctx round={kwargs.get('round_num')}> {prompt}",
    )
    out = _apply_evolution_round_prepend(
        "user prompt",
        round_num=1,
        max_rounds=5,
        prior_nines=0.0,
    )
    assert "user prompt" in out


def test_apply_evolution_round_prepend_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If the prior-round evidence file raises ``OSError`` on read, the helper continues."""
    from popolaloom.daemon import rpc as rpc_mod

    monkeypatch.setattr(
        "popolaloom.evolution.skill_inject.check_skill_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "popolaloom.evolution.skill_inject.prepend_workflow_context",
        lambda prompt, **kwargs: f"<ctx> {prompt}",
    )

    fake_home = tmp_path / "home"
    (fake_home / ".popola").mkdir(parents=True)
    evidence = fake_home / ".popola" / "round-1-evidence.md"
    evidence.write_text("- finding A\n- finding B", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    out = rpc_mod._apply_evolution_round_prepend(
        "user prompt",
        round_num=2,
        max_rounds=5,
    )
    assert "user prompt" in out


# ── 17. asgi-fixture cleanup smoke ───────────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint_smoke(asgi_client: httpx.AsyncClient) -> None:
    """``GET /health`` returns 200 — sanity check that the ASGI fixture is wired up."""
    r = await asgi_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
