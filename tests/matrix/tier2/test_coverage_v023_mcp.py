"""Tier 2 coverage gap-fillers for ``popolaloom.mcp.tools`` (v0.2.3).

Targets the HTTP-error / non-200 / JSONDecodeError / ConnectError /
HTTPError branches in the 7 MCP verbs that ``test_coverage_helpers.py``
didn't cover (those tests focused on input-validation errors; this one
focuses on transport / response-shape errors).
"""

from __future__ import annotations

import httpx
import pytest

from popolaloom.mcp.tools import (
    popola_attach_stream,
    popola_cancel,
    popola_list,
    popola_status,
    popola_submit,
)


def _client_with_handler(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    """Build an httpx.AsyncClient backed by a MockTransport calling ``handler``.

    The handler signature is ``(request: httpx.Request) -> httpx.Response``.
    """
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://popolad")


def _raising_handler(exc: Exception):
    """Build a MockTransport handler that raises ``exc`` synchronously."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


@pytest.mark.asyncio
async def test_popola_submit_connect_error_returns_friendly_daemon_down() -> None:
    client = _client_with_handler(_raising_handler(httpx.ConnectError("UDS missing")))
    result = await popola_submit(client, {"cli": "cursor", "prompt": "hi"})
    assert result.isError is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "popolad not running" in text


@pytest.mark.asyncio
async def test_popola_submit_http_error_surfaces_transport_message() -> None:
    client = _client_with_handler(_raising_handler(httpx.ReadTimeout("bad timeout")))
    result = await popola_submit(client, {"cli": "cursor", "prompt": "hi"})
    assert result.isError is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "transport error" in text


@pytest.mark.asyncio
async def test_popola_submit_non_200_returns_http_error_payload() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal explosion")

    client = _client_with_handler(_handler)
    result = await popola_submit(client, {"cli": "cursor", "prompt": "hi"})
    assert result.isError is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "HTTP 500" in text
    assert "internal explosion" in text


@pytest.mark.asyncio
async def test_popola_list_connect_error_returns_friendly_daemon_down() -> None:
    client = _client_with_handler(_raising_handler(httpx.ConnectError("UDS missing")))
    result = await popola_list(client, {})
    assert result.isError is True


@pytest.mark.asyncio
async def test_popola_list_http_error_surfaces_transport_message() -> None:
    client = _client_with_handler(_raising_handler(httpx.ReadTimeout("read")))
    result = await popola_list(client, {})
    text = result.content[0].text  # type: ignore[union-attr]
    assert "transport error" in text


@pytest.mark.asyncio
async def test_popola_list_non_200_returns_http_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    client = _client_with_handler(_handler)
    result = await popola_list(client, {})
    assert result.isError is True
    assert "HTTP 503" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_status_http_error_surfaces() -> None:
    client = _client_with_handler(_raising_handler(httpx.ReadTimeout("status read")))
    result = await popola_status(client, {"task_id": "x-1"})
    text = result.content[0].text  # type: ignore[union-attr]
    assert "transport error" in text


@pytest.mark.asyncio
async def test_popola_status_non_200_other_than_404() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="bang")

    client = _client_with_handler(_handler)
    result = await popola_status(client, {"task_id": "x-1"})
    assert result.isError is True
    assert "HTTP 500" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_attach_stream_connect_error_path() -> None:
    client = _client_with_handler(_raising_handler(httpx.ConnectError("uds")))
    result = await popola_attach_stream(client, {"task_id": "x-1"})
    assert result.isError is True
    assert "popolad not running" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_attach_stream_status_http_error() -> None:
    client = _client_with_handler(_raising_handler(httpx.ReadTimeout("status read")))
    result = await popola_attach_stream(client, {"task_id": "x-1"})
    text = result.content[0].text  # type: ignore[union-attr]
    assert "transport error" in text


@pytest.mark.asyncio
async def test_popola_attach_stream_status_non_200() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="explode")

    client = _client_with_handler(_handler)
    result = await popola_attach_stream(client, {"task_id": "x-1"})
    assert result.isError is True
    assert "HTTP 500" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_attach_stream_sse_non_200() -> None:
    """When /attach_stream returns non-200, error path catches it."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url.path).startswith("/status"):
            return httpx.Response(
                200, json={"task_id": "x-1", "latest_event_index": 0}
            )
        return httpx.Response(500, text="bad sse")

    client = _client_with_handler(_handler)
    result = await popola_attach_stream(client, {"task_id": "x-1"})
    assert result.isError is True


@pytest.mark.asyncio
async def test_popola_attach_stream_sse_skips_non_data_frames_and_decode_errors() -> None:
    """SSE stream containing non-data lines + bad-JSON lines doesn't crash."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url.path).startswith("/status"):
            return httpx.Response(
                200, json={"task_id": "x-1", "latest_event_index": 5}
            )
        body = (
            ":heartbeat\n"
            'data: {"type": "task.dispatched", "id": "evt-1"}\n'
            "data: not-json-content\n"
            'data: {"type": "task.completed", "id": "evt-2"}\n'
        )
        return httpx.Response(
            200, content=body.encode("utf-8"), headers={"content-type": "text/event-stream"}
        )

    client = _client_with_handler(_handler)
    result = await popola_attach_stream(client, {"task_id": "x-1", "last_n": 5})
    assert result.isError is False
    text = result.content[0].text  # type: ignore[union-attr]
    assert "task.dispatched" in text
    assert "task.completed" in text


@pytest.mark.asyncio
async def test_popola_cancel_connect_error_path() -> None:
    client = _client_with_handler(_raising_handler(httpx.ConnectError("uds")))
    result = await popola_cancel(client, {"task_id": "x-1"})
    assert result.isError is True


@pytest.mark.asyncio
async def test_popola_cancel_http_error_path() -> None:
    client = _client_with_handler(_raising_handler(httpx.ReadTimeout("read")))
    result = await popola_cancel(client, {"task_id": "x-1"})
    text = result.content[0].text  # type: ignore[union-attr]
    assert "transport error" in text


@pytest.mark.asyncio
async def test_popola_cancel_404_returns_clean_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    client = _client_with_handler(_handler)
    result = await popola_cancel(client, {"task_id": "missing-task"})
    assert result.isError is True
    assert "not found" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_cancel_409_already_terminal_idempotent() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"detail": "task already in terminal state"}
        )

    client = _client_with_handler(_handler)
    result = await popola_cancel(client, {"task_id": "term-1"})
    assert result.isError is False
    assert "already_terminal" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_popola_cancel_409_with_non_json_body_uses_text() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="not-json-body")

    client = _client_with_handler(_handler)
    result = await popola_cancel(client, {"task_id": "term-2"})
    assert result.isError is False


@pytest.mark.asyncio
async def test_popola_cancel_non_200_other_than_404_409() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client_with_handler(_handler)
    result = await popola_cancel(client, {"task_id": "x-1"})
    assert result.isError is True
    assert "HTTP 500" in result.content[0].text  # type: ignore[union-attr]
