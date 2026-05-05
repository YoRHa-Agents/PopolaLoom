"""Shared pytest fixtures for the popolaloom test suite (Stage Impl-2).

Exposes :func:`popolad_factory` — a builder callable that constructs
:class:`popolaloom.daemon.Popolad` instances backed by a tmp_path events
directory and a configurable fake adapter, so tests across daemon / adapter /
cli teams don't need to retype the boilerplate.

Lives in ``tests/`` (not ``src/``) so production code never imports it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from popolaloom.daemon import Popolad

# Stage E: AdapterCallback is now a strict 4-arg signature (cli, prompt, cwd, extra)
# per R-009 closure; conftest stays 4-arg too to satisfy mypy strict.
_AdapterFn = Callable[[str, str, "Path | None", "dict[str, Any] | None"], list[str]]


def _default_noop_adapter(
    cli: str,
    prompt: str,
    cwd: Path | None,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Return a fast python subprocess argv that prints + exits 0 (test default).

    使用 ``sys.executable`` 而非裸 ``python``: 保证测试始终用 pytest 当前
    解释器, 不依赖 ``$PATH`` 上是否有 ``python`` 别名。
    """
    return [
        sys.executable,
        "-c",
        "print('test stdout'); import sys; sys.exit(0)",
    ]


@pytest.fixture
def popolad_factory() -> Callable[..., Popolad]:
    """Yield a builder ``(events_dir, adapter=None) -> Popolad``.

    The default adapter spawns a tiny python subprocess that prints
    ``test stdout`` and exits 0 — fast enough for assertions that just need a
    completed task without dragging in cursor-agent / claude / codex binaries.

    Returns:
        Callable[[Path, _AdapterFn | None], Popolad]: factory that the test
        invokes once per Popolad instance it needs.
    """

    def _build(events_dir: Path, adapter: _AdapterFn | None = None) -> Popolad:
        return Popolad(events_dir=events_dir, adapter=adapter or _default_noop_adapter)

    return _build
