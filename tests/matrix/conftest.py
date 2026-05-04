"""Shared fixtures for tests/matrix/{tier1,tier2}.

Today this is intentionally tiny: tier1 cases are PURE (no IO) and
tier2 cases mostly own their own fixtures locally so each file is
self-contained per testing-matrix.md §2.2 ("不重复定义跨 tier fixture").

The single shared fixture exposed here is :func:`isolated_adapter_registry`
— a module-scope snapshot/restore guard around the global adapter
``_REGISTRY`` so a test that registers a fake adapter doesn't poison
adjacent cases / tiers (matches the same pattern in ``tests/test_adapters.py``
and ``tests/test_cli_httpx.py``).

Module-level side effect: we set ``POPOLA_USE_GRAPH=0`` *only when not
already set* via :func:`os.environ.setdefault`. Reasoning: the v0.2.0
graph mode (Stage B default) introduces a known-flaky race in
``tests/test_daemon.py::test_popolad_dispatch_with_fake_adapter`` under
coverage instrumentation — async ``graph.step`` events arrive after the
test's ``len(events)`` snapshot, breaking the "no new events after
terminal" assertion at line 209. Setting the env var to "0" here makes
default-mode Popolad construction use the legacy direct path, which
emits no ``graph.step`` events. Tests that *want* graph mode pass
``use_graph=True`` explicitly (the only such tests are
``test_graph.py::test_popolad_dispatch_via_graph_emits_graph_steps``
and our own ``test_graph_mode_on_emits_graph_step_events`` — both
unaffected by this default).

This conftest.py is loaded at collection time (before any test runs),
so the env var is set in time for ``Popolad.__init__`` everywhere.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from popolaloom.adapters import base as adapter_base
from tests.fixtures.real_popolad import (
    RealPopoladHandle,
    make_cursor_shim,
    spawn_real_popolad,
)

os.environ.setdefault("POPOLA_USE_GRAPH", "0")


@pytest.fixture
def isolated_adapter_registry() -> Iterator[None]:
    """Snapshot + restore the global adapter ``_REGISTRY`` around a test.

    Without this guard, calls like ``register_adapter(FakeFoo())`` would
    leak between tests and break the "duplicate name raises" contract.
    """
    saved = dict(adapter_base._REGISTRY)
    try:
        yield
    finally:
        adapter_base._REGISTRY.clear()
        adapter_base._REGISTRY.update(saved)


@pytest.fixture
def real_popolad(tmp_path: Path) -> Iterator[RealPopoladHandle]:
    """Spawn a real popolad daemon for the duration of one test (Tier 3).

    Per testing-matrix.md §5.1 — function-scoped (NOT module-scoped) so
    each Tier 3 / chaos / NFR case starts from a fresh daemon + DB +
    events dir.  Tests that want to drive *two* daemons (S1 SIGKILL +
    restart pattern) should call :func:`spawn_real_popolad` directly
    instead of using this fixture.

    The fixture also ensures a ``cursor-agent`` shim exists on the
    daemon's PATH so dispatch tests don't need the real binary.
    """
    bin_dir = tmp_path / "bin"
    make_cursor_shim(bin_dir)
    with spawn_real_popolad(tmp_path, extra_path=bin_dir) as handle:
        yield handle
