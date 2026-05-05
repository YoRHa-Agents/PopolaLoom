"""popolaloom-mcp — stdio MCP server exposing 7 dispatch verbs (v0.2.0 Stage D).

Architecture::

    server.py     — :class:`mcp.server.Server` factory + ``stdio_server`` glue
    tools.py      — 7 verb handlers + :class:`Tool` descriptors
    elicitation.py — form-mode ``elicitation/create`` builder (v0.3.0 F4 occupied)
    __main__.py   — entry point for ``python -m popolaloom.mcp``

Run as either::

    python -m popolaloom.mcp
    python -m popolaloom.mcp.server

Configure in ``~/.cursor/mcp.json`` or ``~/.claude/settings.json`` per
the templates in ``templates/`` at the project root.

See spec.md §3.2 row "popolaloom-mcp" + v0.2.0-plan §4 Stage D for full
context. Closes the spec's "0% current state" line for popolaloom-mcp.

Implementation note — lazy re-exports
-------------------------------------
Top-level attributes resolve via :func:`__getattr__` (PEP 562). This
matters because eagerly importing :mod:`popolaloom.mcp.server` here would
make ``python -m popolaloom.mcp.server`` emit
``RuntimeWarning: 'popolaloom.mcp.server' found in sys.modules ...`` (the
package import primes ``server`` into ``sys.modules`` *before* runpy gets
to it). Lazy re-exports keep both invocation forms warning-free while
still exposing the same surface to ordinary callers like
``from popolaloom.mcp import build_server, build_tool_list``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — only fires for type checkers
    from popolaloom.mcp.elicitation import (
        ELICITATION_PAYLOAD_SCHEMA,
        build_elicitation_request,
        validate_elicitation_request,
    )
    from popolaloom.mcp.server import (
        Server,
        build_server,
        main,
        make_async_client,
        socket_path,
    )
    from popolaloom.mcp.tools import (
        TOOL_DEFINITIONS,
        build_tool_list,
        call_verb,
        popola_attach_stream,
        popola_cancel,
        popola_inject_subtask,
        popola_list,
        popola_status,
        popola_submit,
        popola_supply_feedback,
    )

__all__ = [
    "ELICITATION_PAYLOAD_SCHEMA",
    "Server",
    "TOOL_DEFINITIONS",
    "build_elicitation_request",
    "build_server",
    "build_tool_list",
    "call_verb",
    "main",
    "make_async_client",
    "popola_attach_stream",
    "popola_cancel",
    "popola_inject_subtask",
    "popola_list",
    "popola_status",
    "popola_submit",
    "popola_supply_feedback",
    "socket_path",
    "validate_elicitation_request",
]


_LAZY_LOOKUP: dict[str, str] = {
    "ELICITATION_PAYLOAD_SCHEMA": "popolaloom.mcp.elicitation",
    "build_elicitation_request": "popolaloom.mcp.elicitation",
    "validate_elicitation_request": "popolaloom.mcp.elicitation",
    "Server": "popolaloom.mcp.server",
    "build_server": "popolaloom.mcp.server",
    "main": "popolaloom.mcp.server",
    "make_async_client": "popolaloom.mcp.server",
    "socket_path": "popolaloom.mcp.server",
    "TOOL_DEFINITIONS": "popolaloom.mcp.tools",
    "build_tool_list": "popolaloom.mcp.tools",
    "call_verb": "popolaloom.mcp.tools",
    "popola_attach_stream": "popolaloom.mcp.tools",
    "popola_cancel": "popolaloom.mcp.tools",
    "popola_inject_subtask": "popolaloom.mcp.tools",
    "popola_list": "popolaloom.mcp.tools",
    "popola_status": "popolaloom.mcp.tools",
    "popola_submit": "popolaloom.mcp.tools",
    "popola_supply_feedback": "popolaloom.mcp.tools",
}


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolver — see module docstring rationale."""
    module_name = _LAZY_LOOKUP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name)
    return getattr(module, name)
