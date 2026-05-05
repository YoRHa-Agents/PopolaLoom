"""C1 — TaskService.create_task raises → handle.persisted=False, no silent crash.

Per testing-matrix.md §10 #1.  We mock ``TaskService.create_task`` at
the deepest sensible point and verify Popolad surfaces the failure
via ``(arktower_task_id=None, persisted=False)`` — exactly what
:meth:`Popolad._maybe_create_arktower_task` advertises in its
docstring (``except Exception: log + return (None, False)``).

Workspace rule "No Silent Failures": the failure MUST be reflected in
the returned tuple AND the exception MUST be logged (otherwise
operators have no way to discover the broken arktower install).
"""

from __future__ import annotations

from pathlib import Path

from popolaloom.daemon.server import Popolad


def _stub_adapter(cli, prompt, cwd, extra=None):
    return ["python", "-c", "print('chaos C1')"]


def test_chaos_arktower_create_task_raises_returns_none_persisted_false(
    tmp_path: Path,
    mocker,
    caplog,
) -> None:
    """``TaskService.create_task`` raises → tuple ``(None, False)`` + WARNING log."""
    popolad = Popolad(events_dir=tmp_path / "events", adapter=_stub_adapter)

    fake_persistence = mocker.MagicMock()
    fake_persistence.task_service = mocker.MagicMock()

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated arktower SQLite IntegrityError")

    fake_persistence.task_service.create_task = boom
    popolad._persistence = fake_persistence

    with caplog.at_level("ERROR"):
        ark_id, persisted = popolad._maybe_create_arktower_task(
            task_id="cursor-chaos-c1",
            cli="cursor",
            prompt="trigger chaos",
            cmd=["echo", "x"],
        )

    assert ark_id is None
    assert persisted is False
    error_records = [r for r in caplog.records if "create_task failed" in r.message]
    assert error_records, (
        "No Silent Failures: TaskService.create_task failure must be logged"
    )


def test_chaos_arktower_create_task_dispatch_records_persisted_false(
    tmp_path: Path,
    mocker,
) -> None:
    """End-to-end: a dispatch where create_task raises → handle.persisted=False."""
    popolad = Popolad(
        events_dir=tmp_path / "events",
        adapter=_stub_adapter,
        use_graph=False,
    )

    fake_persistence = mocker.MagicMock()
    fake_persistence.task_service = mocker.MagicMock()

    async def boom(*args, **kwargs):
        raise RuntimeError("arktower fake failure")

    fake_persistence.task_service.create_task = boom
    popolad._persistence = fake_persistence

    task_id = popolad.dispatch_task("cursor", "test C1", cwd=None)

    handle = popolad.state_store.get(task_id)
    assert handle is not None
    assert handle.persisted is False, (
        "task handle should mark persisted=False when arktower create_task raised"
    )
    assert handle.arktower_task_id is None, (
        "arktower_task_id must be None when persistence layer raised"
    )
