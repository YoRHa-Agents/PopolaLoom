"""C9b — asyncio loop blocked by sync call → backpressure exposed cleanly.

Per testing-matrix.md §10 #9.  When a sync helper blocks in the
event loop's main thread, the affected request must time out (or the
test must surface the block) rather than silently hanging.

PopolaLoom defends against this by routing all sync subprocess work
through ``asyncio.to_thread`` (see :mod:`popolaloom.daemon.rpc`); we
verify here that the rpc layer's ``dispatch`` route is *NOT* awaited
synchronously — i.e. that wrapping ``popolad.dispatch_task`` in
``asyncio.to_thread`` is the right pattern.

We construct an ``httpx.AsyncClient + ASGITransport`` test, then make
``dispatch_task`` call ``time.sleep(2)``; we measure the total wall
clock and assert it stayed roughly equal to the sleep (i.e. didn't
spawn extra retries that compound).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from popolaloom.daemon.rpc import create_app


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["echo", "asyncio chaos"]


@pytest.mark.asyncio
async def test_chaos_sync_dispatch_routed_through_to_thread_doesnt_block_loop(
    tmp_path: Path,
    mocker,
) -> None:
    """The dispatch endpoint runs the sync ``dispatch_task`` off the loop.

    If it ran inline, our concurrent ``probe`` request would be
    starved while ``dispatch_task`` slept; instead, the ``probe``
    must complete in ~immediate wall-clock while ``dispatch`` is
    still sleeping.
    """
    from popolaloom.daemon.server import Popolad

    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter, use_graph=False)

    sleeping = {"called": False}

    def _slow_dispatch(*args, **kwargs):
        sleeping["called"] = True
        time.sleep(0.6)
        return "cursor-blocked-task"

    mocker.patch.object(popolad, "dispatch_task", side_effect=_slow_dispatch)

    app = create_app(popolad=popolad)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://popolad",
    ) as client:
        t0 = time.monotonic()
        dispatch_task_coro = client.post(
            "/dispatch",
            json={"cli": "cursor", "prompt": "x", "cwd": None, "extra": None},
        )
        probe_coro = client.get("/probe")

        dispatch_resp, probe_resp = await asyncio.gather(
            dispatch_task_coro, probe_coro
        )
        elapsed = time.monotonic() - t0

    assert sleeping["called"], "dispatch_task wasn't even called"
    assert dispatch_resp.status_code == 200
    assert probe_resp.status_code == 200

    assert 0.55 <= elapsed < 1.5, (
        f"loop appears blocked: elapsed={elapsed:.3f}s; "
        "asyncio.to_thread should have let the probe finish concurrently"
    )
