"""popolaloom-mcp stdio server (v0.2.0 Stage D D1).

This module wires the official Anthropic ``mcp`` Python SDK
(``mcp.server.Server`` + ``mcp.server.stdio.stdio_server``) into popolad,
exposing 7 dispatch verbs (defined in :mod:`popolaloom.mcp.tools`) to
IDE Agents (Cursor / Claude Code / generic mcp clients) over stdin/stdout.

Architecture::

    Cursor IDE / Claude Code
        ↓  (spawn `python -m popolaloom.mcp.server` per ~/.cursor/mcp.json)
    popolaloom-mcp (this module)
        ↓  httpx.AsyncClient(transport=AsyncHTTPTransport(uds=...))
    popolad daemon (Stage A)
        ↓  spawns subprocess (cursor / claude / codex / ...)
    local agent CLI

The server reuses Stage A's UDS pattern: socket path is resolved via
:func:`socket_path` (``$POPOLA_HOME/popolad.sock`` or
``~/.popola/popolad.sock``), and an :class:`httpx.AsyncClient` is opened
once at server startup and reused across all verb invocations to avoid
per-call connection overhead.

If the daemon is **not running**, verb calls don't fail loudly at import
time — they only surface as MCP ``CallToolResult`` errors with
``isError=True`` and a friendly "popolad not running" message (per the
No Silent Failures workspace rule). The MCP server itself stays up and
healthy so the IDE Agent can offer a "start daemon" hint.

Run as::

    python -m popolaloom.mcp.server     # blocks on stdio

Tested via ``pytest tests/test_mcp_tools.py`` (mocked client, no real
daemon needed); the AC subprocess test additionally validates the
JSON-RPC ``initialize`` handshake on stdin/stdout.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from popolaloom import __version__
from popolaloom.mcp.tools import build_tool_list, call_verb

__all__ = [
    "Server",
    "build_server",
    "main",
    "make_async_client",
    "socket_path",
]

logger = logging.getLogger("popolaloom.mcp")


# ── socket / transport helpers (mirror cli/main.py for consistency) ──────


def socket_path() -> Path:
    """Resolve the popolad UDS path used by all verbs.

    Honours ``$POPOLA_HOME`` (tests / nonstandard installs use this);
    falls back to the canonical ``~/.popola/popolad.sock`` matching
    Stage A's :func:`popolaloom.cli.main._socket_path`.
    """
    home = os.environ.get("POPOLA_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".popola"
    return base / "popolad.sock"


def make_async_client(uds: Path | None = None) -> httpx.AsyncClient:
    """Construct an :class:`httpx.AsyncClient` bound to popolad's UDS.

    Args:
        uds: optional override for the socket path; defaults to
            :func:`socket_path`. Tests pass a non-existent socket to
            exercise the daemon-down error path.

    Returns:
        :class:`httpx.AsyncClient` — caller is responsible for ``aclose``.
        Connection errors don't raise here; they raise on first request.
    """
    sock = uds or socket_path()
    transport = httpx.AsyncHTTPTransport(uds=str(sock))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://popolad",
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
    )


# ── server factory ───────────────────────────────────────────────────────


def build_server(client: httpx.AsyncClient) -> Server[Any, Any]:
    """Build a configured :class:`mcp.server.Server` with the 7 verbs wired.

    Args:
        client: an :class:`httpx.AsyncClient` to share across all verb
            invocations (one per server instance).

    Returns:
        :class:`mcp.server.Server` ready for ``await server.run(...)``.

    Implementation notes:
        - We use the lowlevel :class:`Server` (not :class:`FastMCP`)
          because we need direct control over annotations + structured
          inputSchemas, and FastMCP's auto-derivation from Python type
          hints can't easily express ``destructiveHint`` / ``readOnlyHint``.
        - The two registered handlers (``list_tools`` + ``call_tool``)
          form a pure functional façade over :mod:`popolaloom.mcp.tools` —
          all routing logic lives there for testability.
    """
    server: Server[Any, Any] = Server(
        name="popolaloom-mcp",
        version=__version__,
        instructions=(
            "popolaloom-mcp exposes 7 dispatch verbs to drive popolad "
            "(meta-orchestrator over local agent CLIs). Use popola_submit "
            "to dispatch a task; popola_list / popola_status to inspect; "
            "popola_attach_stream for an event snapshot; popola_cancel "
            "to terminate. popola_supply_feedback and popola_inject_subtask "
            "are reserved for v0.3.0 — calling them returns a clear "
            "'not implemented' error."
        ),
    )

    # ``Server.list_tools`` and ``Server.call_tool`` are decorator factories
    # in the mcp SDK; they are not annotated with strict typing, so mypy in
    # ``strict = true`` mode flags them as ``no-untyped-call`` /
    # ``untyped-decorator``. Suppress narrowly — alternatives (subclassing
    # Server, overriding ``request_handlers`` directly) lose the SDK's
    # automatic capability registration and parameter-validation glue.

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools_handler() -> list[Tool]:
        """Return all 7 verbs (per :data:`TOOL_DEFINITIONS`)."""
        return build_tool_list()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool_handler(
        name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        """Dispatch a verb by name; never raises (No Silent Failures).

        Returns :class:`CallToolResult` directly so the verb's
        ``isError=True`` flag is propagated to the IDE Agent verbatim
        without being clobbered by ``mcp.server.lowlevel.server`` 's
        default success-wrapping.
        """
        logger.info("popolaloom-mcp call_tool: name=%s", name)
        try:
            return await call_verb(name, arguments, client)
        except Exception:
            logger.exception("popolaloom-mcp call_tool unhandled error: name=%s", name)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"popolaloom-mcp internal error invoking {name!r}: "
                            "see daemon logs for traceback."
                        ),
                    )
                ],
                isError=True,
            )

    return server


# ── lifecycle ────────────────────────────────────────────────────────────


@asynccontextmanager
async def _server_lifecycle(
    uds: Path | None = None,
) -> AsyncIterator[tuple[Server[Any, Any], httpx.AsyncClient]]:
    """Construct + tear down the server + httpx client together.

    Yields a (server, client) tuple; the caller owns running the server
    on stdio. Used by :func:`main` and by future TUI / TCP integrations
    that may swap stdio for another transport.
    """
    client = make_async_client(uds)
    try:
        server = build_server(client)
        yield server, client
    finally:
        await client.aclose()


async def main(uds: Path | None = None) -> None:
    """Run the popolaloom-mcp stdio server (blocks on stdin/stdout).

    Args:
        uds: optional override for the popolad socket path. Tests do NOT
            normally invoke this entry point directly (they exercise
            :mod:`popolaloom.mcp.tools` with a mocked client); the
            subprocess-init AC test invokes ``python -m popolaloom.mcp.server``
            instead.

    Implementation:
        Wraps the official ``mcp.server.stdio.stdio_server`` async context
        manager and feeds the read/write streams to ``server.run`` with
        the default :class:`InitializationOptions` derived from registered
        capabilities.
    """
    logging.basicConfig(
        level=os.environ.get("POPOLALOOM_MCP_LOG", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=__import__("sys").stderr,
    )
    logger.info(
        "popolaloom-mcp v%s starting (uds=%s)",
        __version__,
        uds or socket_path(),
    )

    async with (
        _server_lifecycle(uds) as (server, _client),
        stdio_server() as (read_stream, write_stream),
    ):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _sync_main() -> None:
    """Synchronous wrapper that ``asyncio.run``s :func:`main`.

    Allows ``python -m popolaloom.mcp.server`` (and the eventual
    ``popolaloom-mcp`` console-script entry point) to start the server
    without the caller having to manage an event loop.
    """
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("popolaloom-mcp interrupted, exiting")


if __name__ == "__main__":
    _sync_main()
