"""popolad daemon entry — ``python -m popolaloom.daemon`` (v0.2.0 Stage A A1).

Boots an asyncio + uvicorn server bound to a Unix Domain Socket so the
``popola`` CLI can talk to it via ``httpx.AsyncHTTPTransport(uds=...)``.

Spec / plan references:

- ``v0.2.0-plan.md`` §4 Stage A A1 (this file).
- ``spec.md`` §10 canonical paths (UDS + PID + log + events_dir layout).

Path layout (controlled by ``$POPOLA_HOME`` env var, default ``~/.popola``):

- ``$POPOLA_HOME/popolad.sock`` — Unix Domain Socket (server bind point).
- ``$POPOLA_HOME/popolad.pid`` — PID file (written at startup; removed on
  graceful shutdown).
- ``$POPOLA_HOME/events/`` — NDJSON event log directory (one file per task).
- ``$POPOLA_HOME/log/popolad.log`` — daemon stderr log (only when started
  via ``popolad start`` subcommand; direct ``python -m`` invocations log
  to inherited stderr).

Signal handling:

- ``SIGTERM`` / ``SIGINT`` → graceful shutdown (uvicorn ``server.should_exit
  = True`` + lifespan tear-down cancels in-flight tasks via SIGTERM grace).

# TODO(v0.3.0): integrate ``systemd-run --user --scope`` for cgroup limits;
# add log rotation (NFR-12) + Prometheus /metrics (NFR-3 baseline).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

import uvicorn

from popolaloom.daemon.rpc import create_app

logger = logging.getLogger("popolaloom.daemon")


def get_popola_home() -> Path:
    """Return the popola home dir (``$POPOLA_HOME`` or ``~/.popola``).

    Always ensures the directory exists (mkdir parents=True).
    """
    home = os.environ.get("POPOLA_HOME")
    path = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_socket_path() -> Path:
    """Return the canonical UDS path: ``$POPOLA_HOME/popolad.sock``."""
    return get_popola_home() / "popolad.sock"


def get_pid_path() -> Path:
    """Return the canonical PID file path: ``$POPOLA_HOME/popolad.pid``."""
    return get_popola_home() / "popolad.pid"


def get_events_dir() -> Path:
    """Return the canonical events dir: ``$POPOLA_HOME/events``."""
    events = get_popola_home() / "events"
    events.mkdir(parents=True, exist_ok=True)
    return events


def write_pid_file(pid_path: Path | None = None) -> Path:
    """Write current process pid to ``pid_path`` and return that path."""
    pid_path = pid_path or get_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return pid_path


def remove_pid_file(pid_path: Path | None = None) -> None:
    """Best-effort PID file removal (logs but does not raise on failure)."""
    pid_path = pid_path or get_pid_path()
    try:
        if pid_path.exists():
            pid_path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove PID file %s: %s", pid_path, exc)


def remove_socket(socket_path: Path | None = None) -> None:
    """Best-effort UDS file cleanup (logs but does not raise on failure)."""
    socket_path = socket_path or get_socket_path()
    try:
        if socket_path.exists():
            socket_path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove socket %s: %s", socket_path, exc)


def _configure_logging(level: int = logging.INFO) -> None:
    """Configure structured stderr logging for the daemon process.

    Format: ``%(asctime)s %(levelname)s %(name)s %(message)s`` — verbose
    enough for journalctl / log file scraping but no third-party dep.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def _build_persistence_safely() -> Any:
    """Build :class:`TaskPersistence` for the daemon process; tolerate failures.

    Returns ``None`` and logs a warning when ArkTower migrations cannot be
    located (e.g. a wheel install missing the migrations data dir, see
    :func:`popolaloom.daemon.repository._arktower_migrations_dir`).  v0.2.0
    Stage E rehydrate (R-002 closure / S1 self-bootstrap) needs a real
    persistence to recover, but the daemon must still boot for
    ``--no-persistence`` debug runs.
    """
    try:
        from popolaloom.daemon.repository import make_persistence

        return make_persistence()
    except Exception:
        logger.exception(
            "Failed to build TaskPersistence; daemon will boot without "
            "ArkTower persistence (rehydrate disabled, dispatch falls back "
            "to in-memory ArkTask schema parity)"
        )
        return None


def _build_default_popolad(events_dir: Path) -> Any:
    """Construct the production-mode :class:`Popolad` for the daemon process.

    Wires in:

    - The unified 4-arg :func:`popolaloom.adapters.build_command` adapter.
    - A :class:`TaskPersistence` (ArkTower SQLite) when available so
      :meth:`Popolad.rehydrate_from_persistence` can recover in-flight
      tasks across daemon restarts (S1 self-bootstrap requirement).
    - A :class:`PopolaEventBusBridge` subscribed to ArkTower's
      :class:`EventBus` so ``TASK_TRANSITION`` propagates as
      ``task.transition`` NDJSON events.
    - v0.4.1 Stage L2.C: a :class:`LarkSupervisor` wrapping a
      :class:`LarkListener` when ``lark-cli`` is on PATH AND
      ``LARK_HITL_TARGET_OPEN_ID`` (or ``LARK_NOTIFY_TARGET_OPEN_ID``)
      is set. The supervisor is started as a background asyncio task
      on the currently-running loop (this function is called from
      :func:`main` which is itself async), so the daemon does not
      block on lark-cli during construction. When env vars or the
      binary are missing the wiring is skipped with a single INFO log
      (``lark.supervisor.skipped reason=...``) per workspace rule
      "No Silent Failures" — Lark is always optional.
    """
    from popolaloom.adapters import build_command
    from popolaloom.daemon.event_bus import PopolaEventBusBridge
    from popolaloom.daemon.server import Popolad

    persistence = _build_persistence_safely()
    bridge: PopolaEventBusBridge | None = None
    popolad = Popolad(
        events_dir=events_dir,
        adapter=build_command,
        persistence=persistence,
    )
    if persistence is not None:
        bridge = PopolaEventBusBridge(
            persistence.event_bus,
            popolad.event_log_for_arktower_id,
        )
        popolad._event_bus_bridge = bridge
        bridge.subscribe()

    _maybe_wire_lark_supervisor(popolad)
    return popolad


def _maybe_wire_lark_supervisor(popolad: Any) -> None:
    """Construct + schedule a :class:`LarkSupervisor` when env vars opt in.

    v0.4.1 Stage L2.C: the daemon supervises ``lark-cli event consume``
    automatically when both gating conditions are met:

    1. :func:`popolaloom.lark.is_lark_runtime_available` returns ``True``
       (i.e. ``lark-cli`` is on the daemon's PATH).
    2. :func:`popolaloom.lark.lark_target_open_id` resolves a non-empty
       Lark open_id (i.e. ``LARK_HITL_TARGET_OPEN_ID`` is set; the new
       ``LARK_NOTIFY_TARGET_OPEN_ID`` is consulted by
       :mod:`popolaloom.lark.notifier` for outbound notifications, but
       the listener target is the existing HITL env var because the
       inbound side reuses the same chat).

    Either condition false → log INFO ``lark.supervisor.skipped
    reason=...`` and return without touching ``popolad``.

    The supervisor's :meth:`LarkSupervisor.start` is async; we capture
    the running loop and schedule the start as a background task via
    :meth:`asyncio.AbstractEventLoop.create_task` so this function
    stays sync (the chaos tests in
    ``tests/matrix/chaos/test_chaos_uds_socket_*.py`` mock
    ``_build_default_popolad`` to return a MagicMock and expect a
    sync return shape — making this async would break them).

    Per workspace rule "No Silent Failures": LarkSupervisor.start
    failures are logged + bubbled into the supervisor's ``on_event``
    stream (which the supervisor itself surfaces); they do NOT abort
    daemon startup because Lark is optional.
    """
    from popolaloom.lark import is_lark_runtime_available, lark_target_open_id

    if not is_lark_runtime_available():
        logger.info("lark.supervisor.skipped reason=lark_cli_unavailable")
        return
    target = lark_target_open_id()
    if target is None:
        logger.info(
            "lark.supervisor.skipped reason=lark_target_open_id_unset"
        )
        return

    from popolaloom.lark import lark_allowed_responders
    from popolaloom.lark.listener import DEFAULT_EVENTS, LarkListener
    from popolaloom.lark.supervisor import LarkSupervisor

    callbacks = _build_lark_callbacks(popolad)
    listener = LarkListener(
        callbacks=callbacks,
        allowed_responders=lark_allowed_responders(),
        events=DEFAULT_EVENTS,
    )
    supervisor = LarkSupervisor(
        listener=listener,
        on_event=_make_supervisor_event_logger(),
    )
    popolad._lark_supervisor = supervisor

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "lark.supervisor.skipped reason=no_running_loop "
            "(LarkSupervisor.start could not be scheduled; tests must "
            "call it manually)"
        )
        return

    if hasattr(popolad, "attach_loop"):
        popolad.attach_loop(loop)
    loop.create_task(_safe_supervisor_start(supervisor))
    logger.info(
        "lark.supervisor.scheduled target=%s events=%s",
        target,
        ",".join(DEFAULT_EVENTS),
    )


async def _safe_supervisor_start(supervisor: Any) -> None:
    """Wrap :meth:`LarkSupervisor.start` with No-Silent-Failures logging.

    The supervisor itself catches listener-startup failures and
    surfaces them via its ``on_event`` callback, so this wrapper only
    needs to guard against unexpected exceptions in the start
    coroutine (e.g. ``lark-cli`` binary disappeared between PATH check
    and exec). Logged + swallowed: daemon must keep serving even when
    Lark is broken.
    """
    try:
        await supervisor.start()
    except Exception:
        logger.exception("lark.supervisor.start_failed; daemon continues without Lark")


def _build_lark_callbacks(popolad: Any) -> Any:
    """Build :class:`LarkEventCallbacks` that route into the HITL store.

    When :attr:`Popolad.hitl_store` is wired (v0.3.0 F4.C — set later
    by :func:`popolaloom.daemon.rpc.create_app` lifespan or by tests)
    incoming card-action / text-feedback events are folded via
    :meth:`HITLStore.fold_reply` so the HITL prompt is marked
    answered. When ``hitl_store`` is ``None`` (early in daemon boot
    or in tests that don't wire HITL) the callbacks log at DEBUG and
    drop — this matches the v0.3.0 contract where unwired channels
    are silent observers, not errors.

    The callbacks always log the receipt of an event so operators
    grepping daemon logs can confirm the listener is alive even before
    HITL is wired (per workspace rule "No Silent Failures": every drop
    has an explicit reason in the log).
    """
    from popolaloom.hitl import HITLReply
    from popolaloom.lark.listener import LarkEventCallbacks

    async def on_card_action(
        event: dict[str, Any], parsed: tuple[str, str]
    ) -> None:
        hitl_id, option_id = parsed
        store = getattr(popolad, "hitl_store", None)
        if store is None:
            logger.debug(
                "lark.listener.card_action: hitl_store unwired; dropping "
                "hitl_id=%s option=%s",
                hitl_id,
                option_id,
            )
            return
        sender = _extract_sender_open_id(event)
        reply = HITLReply(
            hitl_id=hitl_id,
            option_id=option_id,
            via="lark",
            responder=sender,
        )
        try:
            await asyncio.to_thread(store.fold_reply, reply)
        except Exception:
            logger.exception(
                "lark.listener.card_action: fold_reply raised hitl_id=%s",
                hitl_id,
            )

    async def on_text_feedback(
        event: dict[str, Any], parsed: dict[str, str]
    ) -> None:
        hitl_id = parsed.get("hitl_id", "")
        option_id = parsed.get("option_id", "")
        store = getattr(popolad, "hitl_store", None)
        if store is None:
            logger.debug(
                "lark.listener.text_feedback: hitl_store unwired; dropping "
                "hitl_id=%s option=%s",
                hitl_id,
                option_id,
            )
            return
        sender = _extract_sender_open_id(event)
        reply = HITLReply(
            hitl_id=hitl_id,
            option_id=option_id,
            via="lark",
            reason=parsed.get("reason"),
            responder=sender,
        )
        try:
            await asyncio.to_thread(store.fold_reply, reply)
        except Exception:
            logger.exception(
                "lark.listener.text_feedback: fold_reply raised hitl_id=%s",
                hitl_id,
            )

    async def on_unauthorized(event: dict[str, Any], sender: str) -> None:
        header = event.get("header")
        event_id = header.get("event_id") if isinstance(header, dict) else None
        logger.warning(
            "lark.listener.unauthorized sender=%s event_id=%s",
            sender,
            event_id,
        )

    return LarkEventCallbacks(
        on_card_action=on_card_action,
        on_text_feedback=on_text_feedback,
        on_unauthorized=on_unauthorized,
    )


def _extract_sender_open_id(event: dict[str, Any]) -> str | None:
    """Best-effort sender open_id extraction (mirrors listener's helper).

    Inlined here to avoid pulling in the listener module's private
    helper (``listener._extract_sender_open_id``); keeps the daemon
    main file self-contained for the wiring path.
    """
    inner = event.get("event") if isinstance(event.get("event"), dict) else event
    if not isinstance(inner, dict):
        return None
    sender = inner.get("sender")
    if isinstance(sender, dict):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, dict):
            oid = sender_id.get("open_id")
            if isinstance(oid, str) and oid:
                return oid
        oid = sender.get("open_id")
        if isinstance(oid, str) and oid:
            return oid
    operator = inner.get("operator")
    if isinstance(operator, dict):
        oid = operator.get("open_id")
        if isinstance(oid, str) and oid:
            return oid
    return None


def _make_supervisor_event_logger() -> Any:
    """Build a :class:`LarkSupervisor` ``on_event`` logger callback.

    The supervisor emits one of ``listener.started`` /
    ``listener.died`` / ``listener.restarted`` /
    ``listener.escalated`` per lifecycle event; we surface them at
    INFO so operators grep ``lark.supervisor.event`` to track listener
    health alongside the existing ``lark.send.*`` envelopes (v0.3.3
    round-3 lark_health real fixture pattern).
    """
    async def _on_event(event: dict[str, str]) -> None:
        logger.info(
            "lark.supervisor.event %s",
            " ".join(f"{k}={v}" for k, v in event.items()),
        )

    return _on_event


async def main(
    *,
    socket_path: Path | None = None,
    events_dir: Path | None = None,
    pid_path: Path | None = None,
    log_level: str = "info",
) -> None:
    """Run the popolad daemon until SIGTERM/SIGINT.

    Args:
        socket_path: UDS bind path (default ``$POPOLA_HOME/popolad.sock``).
        events_dir: NDJSON events directory (default ``$POPOLA_HOME/events``).
        pid_path: PID file path (default ``$POPOLA_HOME/popolad.pid``).
        log_level: uvicorn / root logger level string.

    Behavior:

    1. Configure stderr logging.
    2. Compute socket / pid / events paths (env-overridable).
    3. Cleanup any stale socket file (last daemon may have crashed).
    4. Write PID file.
    5. Construct production-wired :class:`Popolad` (ArkTower persistence +
       event-bus bridge); pass into :func:`create_app`.
    6. Build uvicorn server with ``uds=`` parameter.
    7. Install asyncio signal handlers (SIGTERM / SIGINT) → graceful shutdown.
    8. ``await server.serve()``.
    9. On exit (graceful or exception), remove PID + socket files.
    """
    _configure_logging(level=getattr(logging, log_level.upper(), logging.INFO))

    socket_path = socket_path or get_socket_path()
    events_dir = events_dir or get_events_dir()
    pid_path = pid_path or get_pid_path()

    if socket_path.exists():
        logger.info("Removing stale socket file: %s", socket_path)
        try:
            socket_path.unlink()
        except OSError as exc:
            logger.error("Could not remove stale socket %s: %s", socket_path, exc)
            raise

    write_pid_file(pid_path)
    logger.info(
        "popolad starting (pid=%d, sock=%s, events=%s)",
        os.getpid(),
        socket_path,
        events_dir,
    )

    popolad = _build_default_popolad(events_dir)
    app = create_app(popolad=popolad)

    config = uvicorn.Config(
        app=app,
        uds=str(socket_path),
        log_level=log_level,
        access_log=False,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(sig: int) -> None:
        logger.info(
            "Received signal %d (%s); initiating graceful shutdown",
            sig,
            signal.Signals(sig).name,
        )
        server.should_exit = True
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            logger.warning("add_signal_handler not supported for %s; relying on default", sig)

    try:
        await server.serve()
    finally:
        logger.info("popolad exiting; cleaning up PID + socket")
        remove_pid_file(pid_path)
        remove_socket(socket_path)


def run() -> None:
    """Synchronous entry — wraps :func:`main` in :func:`asyncio.run`.

    This is what ``python -m popolaloom.daemon`` invokes via ``__main__.py``.
    Splitting ``main`` (async) from ``run`` (sync) lets tests ``await main()``
    in their own loop without monkey-patching :func:`asyncio.run`.
    """
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("popolad interrupted by KeyboardInterrupt; cleanup attempted")
    except Exception:
        logger.exception("popolad failed with unhandled exception")
        raise


if __name__ == "__main__":  # pragma: no cover - module entry
    run()


def __getattr__(name: str) -> Any:  # pragma: no cover - debug aid
    """Module-level fallback: surface Popolad / create_app for ``python -m`` REPL.

    Used by debug-style imports like ``from popolaloom.daemon.main import
    Popolad``; primary public surface is in :mod:`popolaloom.daemon`.
    """
    if name == "Popolad":
        from popolaloom.daemon.server import Popolad  # noqa: PLC0415

        return Popolad
    if name == "create_app":
        return create_app
    raise AttributeError(name)
