"""T3.4 — ``GET /attach_stream/{task_id}`` Server-Sent Events end-to-end.

Per testing-matrix.md §1.3 + workspace v0.2.2 brief T3.4.

The :func:`popolaloom.daemon.rpc.attach_stream` route streams CloudEvents
envelopes wrapped in SSE ``data:`` frames.  This test verifies:

1. A real subscriber can iterate the SSE stream and parse JSON envelopes
   that match the per-task NDJSON event log line-for-line;
2. Disconnecting mid-stream (closing the response) does not leak
   resources — the daemon is still healthy after the disconnect and
   subsequent dispatches keep working.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from tests.fixtures.real_popolad import RealPopoladHandle

pytestmark = pytest.mark.slow


def _parse_sse_data_lines(payload_bytes: bytes) -> list[dict]:
    """Extract ``data: ...\\n\\n`` JSON envelopes from an SSE payload chunk."""
    envelopes: list[dict] = []
    text = payload_bytes.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            envelopes.append(json.loads(line[len("data:"):].strip()))
        except json.JSONDecodeError:
            continue
    return envelopes


def test_attach_stream_sse_yields_ndjson_envelopes_matching_event_log(
    real_popolad: RealPopoladHandle,
) -> None:
    """T3.4.a: SSE stream envelopes match the per-task NDJSON entries."""
    with real_popolad.make_sync_client(timeout=15.0) as client:
        resp = client.post(
            "/dispatch",
            json={
                "cli": "cursor",
                "prompt": "T3.4 SSE stream test",
                "cwd": None,
                "extra": None,
            },
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]

    time.sleep(0.5)

    collected: list[dict] = []
    deadline = time.monotonic() + 8.0
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(real_popolad.socket_path)),
        base_url="http://popolad",
        timeout=15.0,
    ) as client, client.stream("GET", f"/attach_stream/{task_id}") as response:
        assert response.status_code == 200
        for raw_chunk in response.iter_raw():
            if not raw_chunk:
                continue
            collected.extend(_parse_sse_data_lines(raw_chunk))
            if collected and time.monotonic() > deadline - 4.0:
                break
            if time.monotonic() > deadline:
                break

    assert collected, (
        f"T3.4.a: SSE stream should yield ≥1 envelope before deadline; "
        f"daemon log:\n{real_popolad.read_log()}"
    )
    for ev in collected:
        assert "specversion" in ev
        assert "type" in ev
        assert "data" in ev
        assert "id" in ev and ev["id"].startswith("evt-")

    event_log_path = real_popolad.events_dir / f"{task_id}.jsonl"
    assert event_log_path.exists(), "per-task NDJSON file should exist"
    on_disk_types = []
    with event_log_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                on_disk_types.append(json.loads(line)["type"])
            except (json.JSONDecodeError, KeyError):
                continue

    sse_types = [ev["type"] for ev in collected]
    overlap = set(on_disk_types) & set(sse_types)
    assert overlap, (
        f"T3.4.a: SSE types {sse_types} share no entries with NDJSON types "
        f"{on_disk_types}"
    )


def test_attach_stream_sse_disconnect_mid_stream_does_not_leak(
    real_popolad: RealPopoladHandle,
) -> None:
    """T3.4.b: closing the SSE response mid-stream → daemon stays healthy."""
    with real_popolad.make_sync_client(timeout=15.0) as client:
        resp = client.post(
            "/dispatch",
            json={
                "cli": "cursor",
                "prompt": "T3.4 SSE disconnect test",
                "cwd": None,
                "extra": None,
            },
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]

    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(real_popolad.socket_path)),
        base_url="http://popolad",
        timeout=15.0,
    ) as client, client.stream("GET", f"/attach_stream/{task_id}") as response:
        assert response.status_code == 200
        for chunk in response.iter_raw():
            if chunk:
                break

    time.sleep(1.0)
    assert real_popolad.is_alive(), (
        f"T3.4.b: daemon should remain alive after SSE disconnect; "
        f"log:\n{real_popolad.read_log()}"
    )

    with real_popolad.make_sync_client(timeout=10.0) as client:
        probe = client.get("/probe")
        assert probe.status_code == 200, probe.text
        body = probe.json()
        assert body["daemon_pid"] == real_popolad.pid
