"""Module entry-point: ``python -m popolaloom.mcp`` → start the stdio server.

Mirrors :mod:`popolaloom.mcp.server` 's ``__main__`` block so users have
two equivalent invocations::

    python -m popolaloom.mcp
    python -m popolaloom.mcp.server

The first form is what ``templates/mcp.json`` and ``templates/claude_settings.json``
recommend (shorter; idiomatic Python).
"""

from __future__ import annotations

from popolaloom.mcp.server import _sync_main

if __name__ == "__main__":
    _sync_main()
