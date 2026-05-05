"""Lark supervisor graceful shutdown tests (v0.5.2 Loop 2 §L2.B).

The ``daemon/main.py:_build_default_popolad`` factory wires an optional
:class:`popolaloom.lark.supervisor.LarkSupervisor` onto ``popolad._lark_supervisor``
when both gating conditions hold (``lark-cli`` on PATH AND
``LARK_HITL_TARGET_OPEN_ID`` set).  Until v0.5.2 the supervisor was
**leaked** at daemon shutdown — the ``daemon/rpc.py:lifespan`` exit
hook only torn down the active task list and the persistence bridge,
so the ``lark-cli event consume`` subprocess plus the watchdog
asyncio task would survive across daemon restarts (this was tracked
as known-limitation #2 in
[`release-notes-v0.5.1.md`](../../release-notes-v0.5.1.md)).

This module asserts the v0.5.2 fix:

1. ``test_lifespan_calls_supervisor_stop_when_wired`` — when a
   supervisor is attached to ``popolad._lark_supervisor`` (mocked,
   so we don't spawn a real ``lark-cli`` subprocess), exiting the
   FastAPI ``lifespan`` MUST call ``supervisor.stop()`` exactly once.
2. ``test_lifespan_no_op_when_lark_disabled`` — when
   ``_lark_supervisor`` is absent / ``None`` (the default state when
   the daemon boots without Lark env vars), the lifespan must shut
   down cleanly without raising and without invoking any stop hook.
3. ``test_lifespan_swallows_supervisor_stop_exception`` —
   per the workspace "No Silent Failures" rule the daemon must keep
   shutting down even if ``supervisor.stop()`` itself raises (a
   shutdown trap inside the watchdog must not block ``shutdown_persistence_bridge``
   nor the ``_DAEMON_STATE`` reset).

The tests drive the FastAPI lifespan **directly** via
``app.router.lifespan_context(app)`` rather than going through
:class:`httpx.ASGITransport` (which silently skips lifespan startup
+ shutdown notifications, see httpx 0.27 ASGITransport docs).  That
keeps the suite in the fast default lane while still exercising the
real ``__aexit__`` path in :func:`popolaloom.daemon.rpc.create_app`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from popolaloom.daemon import Popolad
from popolaloom.daemon.rpc import create_app


class _StubSupervisor:
    """A minimal :class:`LarkSupervisor` stand-in for lifespan tests.

    Records each ``await stop()`` invocation so we can assert exactly-once
    semantics. ``raise_on_stop`` lets the test simulate the No-Silent-
    Failures swallow path.
    """

    def __init__(self, *, raise_on_stop: bool = False) -> None:
        self.stop_calls = 0
        self._raise_on_stop = raise_on_stop

    async def stop(self) -> None:
        self.stop_calls += 1
        if self._raise_on_stop:
            raise RuntimeError("synthetic supervisor stop boom")


@pytest.mark.asyncio
async def test_lifespan_calls_supervisor_stop_when_wired(
    tmp_path: Path,
) -> None:
    """When ``popolad._lark_supervisor`` is set, lifespan exit must call ``stop()``.

    This is the canonical L2.B happy path: the supervisor was wired
    by ``_build_default_popolad`` (production path) or — here — set
    explicitly by the test, and we observe one ``stop()`` call once
    we exit the lifespan ``async with`` block.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    stub = _StubSupervisor()
    popolad._lark_supervisor = stub  # type: ignore[attr-defined]

    app = create_app(popolad=popolad)
    async with app.router.lifespan_context(app):
        # Inside lifespan: stop must NOT have been called yet.
        assert stub.stop_calls == 0, (
            "supervisor.stop() must not be called during lifespan startup"
        )

    assert stub.stop_calls == 1, (
        "L2.B: lifespan exit must invoke supervisor.stop() exactly once "
        f"(saw {stub.stop_calls})"
    )


@pytest.mark.asyncio
async def test_lifespan_no_op_when_lark_disabled(
    tmp_path: Path,
) -> None:
    """No supervisor attribute → lifespan exit is a no-op (no exception).

    Mirrors the production state when the operator has not set
    ``LARK_HITL_TARGET_OPEN_ID`` / ``lark-cli`` is missing from PATH:
    ``_build_default_popolad`` returns a Popolad without
    ``_lark_supervisor``.  Tearing down the lifespan in that mode must
    NOT raise ``AttributeError``, and any ``getattr`` fallback returning
    ``None`` is treated as the documented opt-out path.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    assert getattr(popolad, "_lark_supervisor", None) is None, (
        "fresh Popolad must not have _lark_supervisor set by default"
    )

    app = create_app(popolad=popolad)
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_lifespan_swallows_supervisor_stop_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``supervisor.stop()`` raising must NOT trap the daemon shutdown.

    Per the workspace rule "No Silent Failures", the lifespan finally
    block MUST log the exception via ``logger.exception`` and keep
    going so ``shutdown_persistence_bridge`` plus ``_DAEMON_STATE``
    cleanup still execute.  This is the symmetric guarantee with the
    existing ``shutdown_persistence_bridge`` and ``cancel_task``
    swallow paths added in v0.5.1 §L1.B.
    """
    popolad = Popolad(events_dir=tmp_path / "events")
    stub = _StubSupervisor(raise_on_stop=True)
    popolad._lark_supervisor = stub  # type: ignore[attr-defined]

    app = create_app(popolad=popolad)
    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.rpc"):
        async with app.router.lifespan_context(app):
            pass

    assert stub.stop_calls == 1, (
        "stop() must still be attempted exactly once even when it raises"
    )
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("lark.supervisor.stop_failed" in m for m in msgs), (
        f"expected lark.supervisor.stop_failed log line; got: {msgs}"
    )


@pytest.mark.asyncio
async def test_lifespan_calls_supervisor_stop_before_shutdown_persistence_bridge(
    tmp_path: Path,
) -> None:
    """Ordering: ``supervisor.stop()`` runs before ``shutdown_persistence_bridge``.

    The supervisor watches ``lark-cli event consume`` which itself
    pushes events back through ``hitl_store`` (which is owned by the
    persistence layer). If we close the persistence first, an in-flight
    ``fold_reply`` driven by a final event from the listener would hit
    a closed connection.  Asserting the ordering here protects the
    cooperative-shutdown contract documented in spec §3.4.
    """
    order: list[str] = []

    class _OrderedSupervisor:
        async def stop(self) -> None:
            order.append("supervisor.stop")

    popolad = Popolad(events_dir=tmp_path / "events")
    popolad._lark_supervisor = _OrderedSupervisor()  # type: ignore[attr-defined]

    original_bridge_close = popolad.shutdown_persistence_bridge

    def _tracked_bridge_close() -> None:
        order.append("shutdown_persistence_bridge")
        original_bridge_close()

    popolad.shutdown_persistence_bridge = _tracked_bridge_close  # type: ignore[method-assign]

    app = create_app(popolad=popolad)
    async with app.router.lifespan_context(app):
        pass

    assert order == ["supervisor.stop", "shutdown_persistence_bridge"], (
        f"expected supervisor.stop before bridge close; saw: {order}"
    )
