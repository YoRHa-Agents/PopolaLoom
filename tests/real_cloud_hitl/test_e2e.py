"""Real cloud HITL E2E test — `popolaloom_cloud_hitl_request` round-trip.

Task: PopolaLoom v0.8.7 W2.3 T2.3.2 (manual / monthly cadence per Q-B-6).

This test invokes the **real** v0.8.7 MCP verb
:func:`popolaloom.mcp.cloud_hitl_tool.popolaloom_cloud_hitl_request` against
a **real** ``popolad`` instance, which fans out a Lark HITL card to the
configured human approver, blocks until they click an option, then asserts
the answer round-trips back through the MCP tool as a successful
``CallToolResult``. Because the wait-for-click step requires a human, this
file is gated behind the ``real_cloud_hitl`` pytest marker and the
collection-time env-var check in ``conftest.py`` — running ``pytest`` (no
marker) collects but skips it; running ``pytest -m real_cloud_hitl`` in an
environment without the env vars also skips (NOT fails).

================================================================
HOW TO RUN — cold-start in <2 min
================================================================

Required environment variables (all three must be set):

- ``CURSOR_API_KEY`` — Cursor Cloud Agents REST API key (this test does NOT
  consume Cursor API quota directly; the env var presence is a precondition
  marker so the same env that runs ``real_cursor_cloud`` tests can opt in).
- ``LARK_HITL_TARGET_OPEN_ID`` — Lark ``open_id`` of the human approver who
  will receive (and click) the HITL card. The popolad daemon must have been
  launched with the same env var so its Lark notifier knows the recipient.
- ``POPOLAD_BASE_URL`` — base URL of a running popolad instance, in either
  HTTP form (``http://127.0.0.1:8080``) or UDS form
  (``unix:///home/agent/.popola/popolad.sock``). The daemon must be running
  with the v0.8.7 cloud HITL bridge wired AND a Lark notifier configured.

Optional (defaults shown):

- ``POPOLA_HITL_TASK_ID``    — synthetic ``real-hitl-<8hex>`` if unset.
- ``POPOLA_HITL_AGENT_ID``   — synthetic ``bc-real-<8hex>`` if unset.
- ``POPOLA_HITL_RUN_ID``     — synthetic ``run-<8hex>`` if unset.
- ``POPOLA_HITL_TIMEOUT_S``  — total wall-clock budget in seconds for the
  human reply; defaults to 300 (5 min). Must be in ``[60, 86400]`` (the MCP
  tool clamps to that range).

Invocation:

.. code-block:: bash

    export CURSOR_API_KEY="<api-key>"
    export LARK_HITL_TARGET_OPEN_ID="<open-id>"
    export POPOLAD_BASE_URL="http://127.0.0.1:8080"  # or unix:///path/to/sock
    pytest tests/real_cloud_hitl/ -m real_cloud_hitl -q --no-header -s

The maintainer must click any option on the Lark card before
``POPOLA_HITL_TIMEOUT_S`` expires. The test asserts the resulting
``CallToolResult`` is non-error, includes a non-empty ``hitl_id`` /
``option_id`` / ``answer``, and reports ``channel == "lark"``.

================================================================
Expected sequence (for the maintainer running this manually)
================================================================

1. Test wakes ``popolad`` via ``POST /hitl/cloud/request`` with the
   synthetic ``(task_id, agent_id, run_id, question_text)`` tuple.
2. ``popolad`` writes a ``popola_hitl`` row, fans out a Lark card to
   ``LARK_HITL_TARGET_OPEN_ID``.
3. Test enters its ``GET /hitl/cloud/wait`` long-poll loop.
4. Operator clicks an option (Approve / Reject / Custom answer) on Lark.
5. Lark webhook → ``popolad`` ``POST /hitl/cloud/answer`` → row marked.
6. ``popolad`` answers the next ``/wait`` poll; the MCP tool returns
   :class:`mcp.types.CallToolResult` with ``isError=False`` and the JSON
   payload conforming to ``mcp-tool-contract.md`` §3.2.

Per workspace rule "secret 隔离" the env-var lookup is via
:func:`os.environ.get` only — no config file is read.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

import httpx
import pytest

# `popolaloom` is shipped as an editable install without a ``py.typed``
# marker (the project's CI runs ``mypy src/popolaloom`` so the marker is
# unnecessary inside the source tree). When mypy is invoked on this test
# file standalone (per AC h: ``mypy tests/real_cloud_hitl/test_e2e.py``),
# the import is treated as untyped — suppress the diagnostic so the AC
# command exits 0 without touching source files (out-of-scope per the
# task constraint "DO NOT touch any source file").
from popolaloom.mcp.cloud_hitl_tool import (  # type: ignore[import-untyped]
    popolaloom_cloud_hitl_request,
)

pytestmark = [
    pytest.mark.real_cloud_hitl,
    pytest.mark.usefixtures("ensure_cloud_hitl_env"),
]


def _build_async_client(base_url: str) -> httpx.AsyncClient:
    """Build an :class:`httpx.AsyncClient` pointing at the configured popolad.

    Supports both transport forms via ``POPOLAD_BASE_URL``:

    - ``unix:///path/to/popolad.sock`` → :class:`httpx.AsyncHTTPTransport`
      with a UDS, matching :func:`popolaloom.mcp.server.make_async_client`.
    - ``http://host:port`` (or ``https://...``) → standard HTTP client with
      ``base_url`` set directly so the MCP tool's relative paths
      (``/hitl/cloud/request`` etc.) resolve against the configured host.

    The read timeout is left unbounded because the inner ``GET /wait``
    long-poll wraps the daemon's 60-s slice cap with its own httpx-level
    timeout (see ``DAEMON_LONG_POLL_HTTP_TIMEOUT_S`` in the MCP tool); the
    connect timeout stays small (5 s) so a missing ``popolad`` fails fast.
    """
    timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0)
    if base_url.startswith("unix://"):
        sock_path = base_url[len("unix://") :]
        transport = httpx.AsyncHTTPTransport(uds=sock_path)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://popolad",
            timeout=timeout,
        )
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


def _read_env(name: str, default_factory: Any | None = None) -> str:
    """Return env var value (stripped); fall back to ``default_factory()``.

    Centralises the "secret 隔离" rule (env-only, never config file) at a
    single call site so the AC tests are easy to audit.
    """
    raw = os.environ.get(name, "").strip()
    if raw:
        return raw
    if default_factory is None:
        return ""
    value = default_factory()
    assert isinstance(value, str), (
        f"default_factory for {name!r} must return str, got {type(value)!r}"
    )
    return value


def _resolve_timeout_s() -> int:
    """Parse ``POPOLA_HITL_TIMEOUT_S`` with a 300 s (5 min) default.

    Fails (rather than silently coercing) if the env var is set but not an
    integer per the workspace rule "No Silent Failures".
    """
    raw = os.environ.get("POPOLA_HITL_TIMEOUT_S", "").strip()
    if not raw:
        return 300
    try:
        return int(raw)
    except ValueError:
        pytest.fail(
            f"POPOLA_HITL_TIMEOUT_S must be an integer; got {raw!r}"
        )


def _extract_text_payload(result: Any) -> str:
    """Concatenate every ``TextContent.text`` entry on a ``CallToolResult``.

    The MCP tool always emits a single ``TextContent`` block, but we
    iterate defensively so a future schema bump (e.g. adding a structured
    block alongside the text) doesn't silently drop diagnostic info.
    """
    blocks = list(getattr(result, "content", None) or [])
    text_parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
        else:
            text_parts.append(repr(block))
    return "\n".join(text_parts)


async def test_real_cloud_hitl_round_trip() -> None:
    """Happy path: send Lark card → wait for click → assert answer round-trips.

    The maintainer must click any option on the Lark card before the
    ``POPOLA_HITL_TIMEOUT_S`` budget expires; otherwise the daemon returns
    a terminal ``timeout`` envelope and the test fails with the rendered
    error JSON in the assertion message (so the failure mode is obvious
    without log archaeology).
    """
    base_url = _read_env("POPOLAD_BASE_URL")
    target_open_id = _read_env("LARK_HITL_TARGET_OPEN_ID")
    task_id = _read_env(
        "POPOLA_HITL_TASK_ID",
        lambda: f"real-hitl-{secrets.token_hex(4)}",
    )
    agent_id = _read_env(
        "POPOLA_HITL_AGENT_ID",
        lambda: f"bc-real-{secrets.token_hex(4)}",
    )
    run_id = _read_env(
        "POPOLA_HITL_RUN_ID",
        lambda: f"run-{secrets.token_hex(4)}",
    )
    timeout_s = _resolve_timeout_s()

    args: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "question_text": (
            "PopolaLoom T2.3.2 real cloud HITL E2E smoke "
            f"(target_open_id={target_open_id}). Please click any option "
            "on this card; the test will assert the answer round-trips."
        ),
        "context_summary": (
            "Triggered by `pytest tests/real_cloud_hitl/ -m real_cloud_hitl`. "
            f"task_id={task_id} agent_id={agent_id} run_id={run_id} "
            f"timeout_s={timeout_s}"
        ),
        "timeout_s": timeout_s,
    }

    async with _build_async_client(base_url) as client:
        result = await popolaloom_cloud_hitl_request(client, args)

    body = _extract_text_payload(result)
    assert getattr(result, "isError", None) is False, (
        "MCP tool returned isError=True; full payload:\n" + body
    )

    payload = json.loads(body)
    assert isinstance(payload, dict), (
        f"expected JSON object payload, got {type(payload).__name__}: {payload!r}"
    )

    hitl_id = payload.get("hitl_id")
    assert isinstance(hitl_id, str) and hitl_id, (
        f"missing/empty 'hitl_id' in payload: {payload!r}"
    )
    option_id = payload.get("option_id")
    assert isinstance(option_id, str) and option_id, (
        f"missing/empty 'option_id' in payload: {payload!r}"
    )
    answer = payload.get("answer")
    assert isinstance(answer, str) and answer, (
        f"missing/empty 'answer' in payload: {payload!r}"
    )
    channel = payload.get("channel")
    assert channel == "lark", (
        f"expected channel='lark', got {channel!r}; payload: {payload!r}"
    )
    answered_by = payload.get("answered_by")
    assert isinstance(answered_by, str) and answered_by, (
        f"missing/empty 'answered_by' in payload: {payload!r}"
    )
    answered_at = payload.get("answered_at")
    assert isinstance(answered_at, str) and answered_at, (
        f"missing/empty 'answered_at' in payload: {payload!r}"
    )
