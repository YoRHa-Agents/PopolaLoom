"""B1 wiring regression — popolaloom_cloud_hitl_request listed in tools/list.

Per REVIEW.md finding B1 of `.local/.agent/active/v0.8.7-cloud-hitl-prod/REVIEW.md`:
the production stdio MCP server (``popolaloom-mcp``) registered only the
legacy dispatch verbs from :mod:`popolaloom.mcp.tools`; the v0.8.7 cloud
HITL verb was reachable from unit tests but not from a production
``tools/list`` response. This test asserts that
:func:`popolaloom.mcp.server.build_server` wires
:func:`popolaloom.mcp.cloud_hitl_tool.build_extended_tool_list` so the
new verb is part of the live registry.

The test does not exercise the JSON-RPC handshake (covered by the
process-level subprocess test in ``tests/test_mcp_server.py``); it just
inspects the registered :class:`mcp.types.Tool` definitions, which is
the cheapest possible regression guard for the wiring.
"""

from __future__ import annotations

import httpx
import pytest

from popolaloom.mcp.cloud_hitl_tool import (
    CLOUD_HITL_VERB_NAME,
    build_extended_tool_list,
)
from popolaloom.mcp.server import build_server


@pytest.mark.asyncio
async def test_build_server_registers_cloud_hitl_verb() -> None:
    """B1 — ``build_server`` exposes ``popolaloom_cloud_hitl_request``.

    Calls the registered ``list_tools`` handler directly via the lowlevel
    SDK's ``request_handlers`` dict, then asserts the cloud HITL verb is
    present alongside the legacy dispatch verbs.
    """
    from mcp.types import ListToolsRequest

    transport = httpx.AsyncHTTPTransport(uds="/nonexistent.sock")
    async with httpx.AsyncClient(
        transport=transport, base_url="http://popolad"
    ) as client:
        server = build_server(client)
        handler = server.request_handlers[ListToolsRequest]
        request = ListToolsRequest(method="tools/list")
        response = await handler(request)

    result = getattr(response, "root", response)
    tools = getattr(result, "tools", None)
    assert tools is not None, f"unexpected response shape: {response!r}"
    names = sorted(t.name for t in tools)
    assert CLOUD_HITL_VERB_NAME in names, (
        f"cloud HITL verb missing from tools/list; got {names}"
    )
    assert "popola_submit" in names, (
        "legacy dispatch verbs must still be registered alongside "
        f"the new cloud HITL verb; got {names}"
    )


def test_build_extended_tool_list_includes_cloud_hitl_verb() -> None:
    """B1 unit-level guard — :func:`build_extended_tool_list` is the contract.

    The wiring in :func:`build_server` delegates to this helper; if the
    helper itself ever stops emitting the cloud HITL verb, the production
    server would silently regress (per REVIEW.md B1). Assert the contract
    at the helper boundary so the failure is loud.
    """
    tools = build_extended_tool_list()
    names = sorted(t.name for t in tools)
    assert CLOUD_HITL_VERB_NAME in names, (
        f"build_extended_tool_list dropped the cloud HITL verb; got {names}"
    )
