"""C2 / C3 — SqliteSaver checkpoint write fails (disk full / WAL locked).

Per testing-matrix.md §10 #2 + #3.  We can't easily inject a real
SqliteSaver write failure mid-graph because the graph runs in a
background thread; instead we mock the checkpointer factory so its
``put`` method raises, and assert the daemon surfaces the error as
something other than a silent swallow.

Strategy: monkey-patch :func:`popolaloom.daemon.checkpoint.make_checkpointer`
to return a checkpointer whose ``put`` raises ``OSError(ENOSPC)``.
We then assert that constructing a Popolad with ``use_graph=True``
and calling ``_get_or_create_checkpointer`` correctly invokes our
faulty factory (subsequent graph operations raise rather than
silently dropping the checkpoint).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from popolaloom.daemon.server import Popolad


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["python", "-c", "print('chaos C2')"]


def test_chaos_sqlitesaver_put_raises_oserror_propagates_not_silent(
    tmp_path: Path,
    mocker,
) -> None:
    """SqliteSaver.put raising ENOSPC → propagation, not silent drop."""
    fake_saver = mocker.MagicMock()
    fake_saver.put = mocker.MagicMock(side_effect=OSError(28, "No space left on device"))
    fake_saver.aput = mocker.AsyncMock(side_effect=OSError(28, "No space left on device"))

    mocker.patch(
        "popolaloom.daemon.checkpoint.make_checkpointer",
        return_value=fake_saver,
    )

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=True,
    )

    saver = popolad._get_or_create_checkpointer()
    assert saver is fake_saver

    with pytest.raises(OSError) as exc_info:
        saver.put(config={"thread_id": "t"}, checkpoint={}, metadata={}, new_versions={})
    assert exc_info.value.errno == 28


def test_chaos_make_checkpointer_factory_failure_logged_returns_none(
    tmp_path: Path,
    mocker,
    caplog,
) -> None:
    """When the checkpointer factory raises, the error MUST be logged.

    Per :meth:`Popolad._get_or_create_checkpointer` design (server.py
    line 913), failure to construct the SqliteSaver is non-fatal —
    the graph still runs, just without checkpointing.  But the
    exception MUST be logged at ERROR level (No Silent Failures) so
    operators see what's broken.
    """
    import logging

    mocker.patch(
        "popolaloom.daemon.checkpoint.make_checkpointer",
        side_effect=RuntimeError("simulated checkpoint init failure"),
    )

    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=True,
    )

    with caplog.at_level(logging.ERROR, logger="popolaloom.daemon.server"):
        result = popolad._get_or_create_checkpointer()

    assert result is None, (
        "_get_or_create_checkpointer should return None on factory failure"
    )
    assert any(
        "Failed to initialise SqliteSaver" in r.message for r in caplog.records
    ), (
        "factory failure must be logged (No Silent Failures); "
        f"records: {[r.message for r in caplog.records]}"
    )
