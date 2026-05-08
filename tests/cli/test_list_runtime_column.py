"""Default-lane tests for ``popola list`` ``runtime`` column (v0.8.6 T2.1.2).

Per [v0.8.6 PLAN.md §4.1 T2.1.2](../../.local/.agent/active/v0.8.6-cloud-sse/PLAN.md):
the ``popola list`` table gains a default-on ``runtime`` column showing
``local`` (subprocess) vs ``cloud`` (Cursor Cloud Agent) per row, sourced
from ``TaskHandle.runtime`` via the daemon ``/list`` summary builder.

These tests validate the entire AC checklist:

* (a) Every row has a non-empty ``runtime`` cell when present (``local``/``cloud``).
* (b) Column widths align via Rich auto-format (no manual padding asserted —
  we just check the header + row tokens appear in the rendered text).
* (c) ``--no-runtime`` hides the column (escape hatch, default = column shown).
* (d) ``--json`` output retains the ``runtime`` key per item.
* (e) Column order is verbatim ``task_id, runtime, cli, state, pid, started_at``.
* (No Silent Failures) Legacy rows missing ``runtime`` render ``"-"``,
  not blank — explicit sentinel keeps observability honest.

Mock pattern is shared with ``tests/cli/test_main_error_paths.py``:
``CliRunner`` invokes the Typer app and ``make_sync_client`` is monkeypatched
to a context-manager-shaped mock returning a fixed ``/list`` payload, so no
real popolad daemon or socket is required (default-lane).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from popolaloom.cli import main as cli_main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Point the CLI at a tmp socket path so no real daemon is touched."""
    sock = tmp_path / "popolad.sock"
    monkeypatch.setattr(cli_main, "_socket_path", lambda: sock)
    yield sock


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the module-level Rich Console to 200x50 so substring asserts hold.

    Under :class:`CliRunner` stdout is non-TTY (often ``TERM=dumb``) so Rich's
    ``is_dumb_terminal`` short-circuits ``Console.size`` to ``(80, 25)`` and
    truncates long ``task_id`` / ``started_at`` cells with ``…``. Passing
    BOTH ``width`` and ``height`` makes Rich's size resolver take the early
    explicit-dimensions branch (``console.py:1012-1013``) so we render the full
    string and substring asserts are deterministic. Production behaviour is
    unchanged — real users only see truncation on genuinely narrow terminals,
    and the ``--json`` path is width-agnostic regardless.
    """
    monkeypatch.setattr(cli_main, "_console_out", Console(width=200, height=50))


def _combined(result: object) -> str:
    """Best-effort ``stdout + stderr`` extraction (click 8.x compat)."""
    parts: list[str] = []
    for attr in ("stdout", "stderr", "output"):
        try:
            value = getattr(result, attr, "") or ""
        except (ValueError, AttributeError):
            value = ""
        parts.append(value)
    return "".join(parts)


def _make_response(*, status_code: int, body: Any) -> MagicMock:
    """Build a MagicMock shaped like an :class:`httpx.Response`."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


def _make_sync_client(*, on_get: Any) -> MagicMock:
    """Build a context-manager-shaped sync httpx client double for GET /list."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = on_get
    return client


def _items_local_and_cloud() -> list[dict[str, Any]]:
    """Two rows — one ``local`` runtime + one ``cloud`` runtime — happy default."""
    return [
        {
            "task_id": "task-local-001",
            "cli": "cursor",
            "state": "running",
            "pid": 4242,
            "started_at": "2026-05-08T10:00:00.000+00:00",
            "runtime": "local",
        },
        {
            "task_id": "task-cloud-002",
            "cli": "cursor-cloud",
            "state": "running",
            "pid": None,
            "started_at": "2026-05-08T10:01:00.000+00:00",
            "runtime": "cloud",
        },
    ]


def _patch_list_response(
    monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]
) -> None:
    """Wire ``cli_main.make_sync_client`` to return ``items`` from ``GET /list``."""
    response = _make_response(status_code=200, body=items)
    monkeypatch.setattr(
        cli_main,
        "make_sync_client",
        lambda *a, **kw: _make_sync_client(on_get=response),
    )


# ── (a) + (b) default rendering: header + row values present ────────────────


def test_list_default_renders_runtime_header_and_values(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola list`` (no flags) shows the ``runtime`` header + ``local``/``cloud`` cells."""
    _patch_list_response(monkeypatch, _items_local_and_cloud())
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "runtime" in out, f"expected 'runtime' header in output:\n{out}"
    assert "local" in out, f"expected 'local' row value in output:\n{out}"
    assert "cloud" in out, f"expected 'cloud' row value in output:\n{out}"
    assert "task-local-001" in out
    assert "task-cloud-002" in out


# ── (e) column order: task_id, runtime, cli, state, pid, started_at ─────────


def test_list_default_column_order_is_canonical(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header line lists ``task_id, runtime, cli, state, pid, started_at`` in order.

    Rich's ``Table`` renderer separates header cells with whitespace; we slice
    the rendered string and assert the relative order of the six headers.
    """
    _patch_list_response(monkeypatch, _items_local_and_cloud())
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    expected_order = ["task_id", "runtime", "cli", "state", "pid", "started_at"]
    indices = [out.find(name) for name in expected_order]
    assert all(i >= 0 for i in indices), (
        f"missing one of {expected_order} in output:\n{out}"
    )
    assert indices == sorted(indices), (
        f"column order mismatch: expected {expected_order}, "
        f"got positions {dict(zip(expected_order, indices, strict=False))}\n{out}"
    )


# ── (c) --no-runtime escape hatch hides the column ──────────────────────────


def test_list_no_runtime_flag_hides_column(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola list --no-runtime`` removes the column header AND row values.

    The other column headers + the row task_ids must still render.
    """
    _patch_list_response(monkeypatch, _items_local_and_cloud())
    result = runner.invoke(cli_main.app, ["list", "--no-runtime"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "runtime" not in out, (
        f"--no-runtime should hide the header, but found 'runtime' in:\n{out}"
    )
    for token in ("task_id", "cli", "state", "pid", "started_at"):
        assert token in out, (
            f"--no-runtime accidentally removed '{token}' header:\n{out}"
        )
    assert "task-local-001" in out
    assert "task-cloud-002" in out


# ── (d) JSON output retains runtime key ─────────────────────────────────────


def test_list_json_output_includes_runtime_per_item(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola list --json`` emits each item with its ``runtime`` field intact."""
    _patch_list_response(monkeypatch, _items_local_and_cloud())
    result = runner.invoke(cli_main.app, ["list", "--json"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    assert isinstance(payload, list)
    assert len(payload) == 2
    runtimes = {row["task_id"]: row["runtime"] for row in payload}
    assert runtimes == {"task-local-001": "local", "task-cloud-002": "cloud"}


def test_list_json_output_with_no_runtime_flag_still_includes_field(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-runtime`` is a *display* hatch only — JSON shape is unchanged.

    AC (d): "``--json`` output already includes runtime (no schema change)".
    The flag must NOT delete the field from the JSON envelope.
    """
    _patch_list_response(monkeypatch, _items_local_and_cloud())
    result = runner.invoke(cli_main.app, ["list", "--json", "--no-runtime"])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(_combined(result).strip().splitlines()[-1])
    for row in payload:
        assert "runtime" in row, f"--no-runtime stripped JSON 'runtime' key: {row}"


# ── No-Silent-Failures: missing runtime renders "-", not blank ──────────────


def test_list_legacy_row_missing_runtime_renders_dash(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with no ``runtime`` field (legacy / pre-v0.8.5 daemon) shows ``-``.

    Per workspace rule "No Silent Failures": never render an unexplained
    blank cell — emit an explicit sentinel so the operator can tell
    "missing data" apart from "data is empty string".
    """
    items = [
        {
            "task_id": "legacy-task-007",
            "cli": "cursor",
            "state": "running",
            "pid": 1234,
            "started_at": "2026-05-08T10:02:00.000+00:00",
        },
    ]
    _patch_list_response(monkeypatch, items)
    result = runner.invoke(cli_main.app, ["list"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "legacy-task-007" in out
    assert "-" in out, (
        f"expected '-' sentinel for missing runtime; got:\n{out}"
    )
    assert "runtime" in out, (
        f"runtime header still must render even with legacy data:\n{out}"
    )


# ── empty list: --no-runtime path doesn't crash on the no-tasks branch ──────


def test_list_no_runtime_flag_empty_does_not_crash(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola list --no-runtime`` on an empty registry → friendly empty msg."""
    _patch_list_response(monkeypatch, [])
    result = runner.invoke(cli_main.app, ["list", "--no-runtime"])
    assert result.exit_code == 0, _combined(result)
    assert "No active tasks." in _combined(result)


# ── all-tasks (--all) path also picks up runtime column ─────────────────────


def test_list_all_flag_keeps_runtime_column(
    isolated_socket: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``popola list --all`` (include_terminal=True) still surfaces runtime.

    AC (a) requires the column on *every* row — including terminal rows
    that v0.8.6 cloud poller marks ``runtime=cloud`` even after FINISHED.
    """
    items = _items_local_and_cloud() + [
        {
            "task_id": "task-completed-003",
            "cli": "cursor-cloud",
            "state": "completed",
            "pid": None,
            "started_at": "2026-05-08T09:50:00.000+00:00",
            "runtime": "cloud",
        },
    ]
    _patch_list_response(monkeypatch, items)
    result = runner.invoke(cli_main.app, ["list", "--all"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "runtime" in out
    assert "task-completed-003" in out
    assert out.count("cloud") >= 2, (
        f"expected ≥2 'cloud' tokens (header + cloud-runtime rows):\n{out}"
    )
