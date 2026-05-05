"""Tier 2 coverage gap-fillers (extra) for v0.2.3 — eval / rpc / server.

Targets ``cli/eval.py``, ``daemon/server.py`` graph fallback path, and
``daemon/event_log.py`` blank-line skip path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from popolaloom.cli.eval import (
    _default_events_dir,
    _default_output_path,
    _format_report_summary,
)
from popolaloom.cli.eval import (
    app as eval_app,
)
from popolaloom.daemon import EventLog, Popolad, TaskState, make_checkpointer
from popolaloom.evaluation import run_evaluation

# ── 1. cli/eval.py — cover defaults + show + write-failure path ────────


def test_default_output_path_is_in_cwd() -> None:
    path = _default_output_path()
    assert path.name == "nines-iter2.toml"
    assert path.parent.is_absolute()


def test_default_events_dir_uses_popola_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POPOLA_HOME", "/tmp/popola-test-eval-cov")
    p = _default_events_dir()
    assert p.parent.name == "popola-test-eval-cov"
    assert p.name == "events"


def test_default_events_dir_falls_back_to_home_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POPOLA_HOME", raising=False)
    p = _default_events_dir()
    assert p.name == "events"
    assert ".popola" in str(p)


def test_eval_run_writes_default_report_path(tmp_path: Path) -> None:
    """popola eval run --output X --events-dir Y writes a valid TOML report."""
    runner = CliRunner()
    output = tmp_path / "report.toml"
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    result = runner.invoke(
        eval_app, ["run", "--output", str(output), "--events-dir", str(events_dir)]
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "composite" in content


def test_eval_run_oserror_path_returns_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing the report to a path that can't be created → exit 1, error stderr."""
    runner = CliRunner()
    bad_path = tmp_path / "deep" / "report.toml"

    def _raise(*a: Any, **kw: Any) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr("pathlib.Path.write_text", _raise)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    result = runner.invoke(
        eval_app,
        ["run", "--output", str(bad_path), "--events-dir", str(events_dir)],
    )
    assert result.exit_code == 1


def test_eval_show_json_branch_emits_dimension_weights() -> None:
    runner = CliRunner()
    result = runner.invoke(eval_app, ["show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    # v0.3.0 F4.E: token_budget_compliance → hitl_handleability swap (D3.10).
    expected_dims = {
        "dispatch_isolation",
        "cycle_convergence",
        "hitl_latency",
        "attach_correctness",
        "cross_cli_handoff",
        "single_threaded_writes",
        "event_log_completeness",
        "hitl_handleability",
    }
    assert set(payload.keys()) == expected_dims


def test_format_report_summary_contains_composite_and_dim_count(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    report = run_evaluation(events_dir=events_dir)
    summary = _format_report_summary(report)
    assert "composite=" in summary
    assert "dims=" in summary


# ── 2. daemon/event_log.py — blank line skip + source property ────────


def test_event_log_source_property_returns_value(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "x.jsonl", source="popola/source-test-1")
    try:
        assert log.source == "popola/source-test-1"
        assert log.path.name == "x.jsonl"
    finally:
        log.close()


def test_event_log_tail_skips_blank_lines(tmp_path: Path) -> None:
    """A NDJSON file with blank lines mixed in should yield only the non-blank events."""
    p = tmp_path / "blanks.jsonl"
    p.write_text(
        '\n{"specversion":"1.0","id":"evt-a","type":"foo","time":"2026-01-01T00:00:00Z","source":"x","data":{}}\n\n{"specversion":"1.0","id":"evt-b","type":"bar","time":"2026-01-01T00:00:00Z","source":"x","data":{}}\n\n',
        encoding="utf-8",
    )
    log = EventLog(p, source="x")
    try:
        events = log.tail()
        assert len(events) == 2
        assert events[0]["type"] == "foo"
        assert events[1]["type"] == "bar"
    finally:
        log.close()


# ── 3. daemon/server.py — _run_graph_for_task exception fallback ──────


def test_graph_invoke_exception_runs_fallback_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the graph thread raises, _run_graph_for_task logs FAILED fallback (lines 1001-1008)."""
    events_dir = tmp_path / "events"
    saver = make_checkpointer(db_path=tmp_path / "state.sqlite")

    def adapter(cli: str, prompt: str, cwd: Path | None, extra: Any = None) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    popolad = Popolad(
        events_dir=events_dir,
        adapter=adapter,
        use_graph=True,
        checkpointer=saver,
    )

    class _ExplodingGraph:
        """Wraps a real compiled graph; invoke() raises a controlled error."""

        def invoke(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("simulated graph.invoke failure")

    from popolaloom.daemon import server as server_module

    real_build = server_module.build_main_graph

    def _patched_build(*a: Any, **kw: Any) -> Any:
        _ = real_build(*a, **kw)
        return _ExplodingGraph()

    import logging

    with caplog.at_level(logging.ERROR), patch.object(
        server_module, "build_main_graph", _patched_build
    ):
        task_id = popolad.dispatch_task(cli="cursor", prompt="will explode")

    import time

    deadline = time.monotonic() + 5.0
    final = ""
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        final = status["state"]
        if final in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            break
        time.sleep(0.05)
    assert final in {str(TaskState.COMPLETED), str(TaskState.FAILED)}, (
        f"task should reach a terminal state; got {final}"
    )
    assert any(
        "graph.invoke raised" in record.message for record in caplog.records
    ), (
        f"expected ERROR log with 'graph.invoke raised' for the fallback "
        f"path; got: {[r.message for r in caplog.records]}"
    )


# ── 4. mock_cli — install_mock_binaries materialises 3 executable shims ──


def test_install_mock_binaries_creates_three_executables(tmp_path: Path) -> None:
    from tests.fixtures.mock_cli import install_mock_binaries

    bin_dir = tmp_path / "bin"
    shims = install_mock_binaries(bin_dir)
    assert set(shims.keys()) == {"cursor-agent", "claude", "codex"}
    for name, path in shims.items():
        assert path.exists()
        st = path.stat()
        assert st.st_mode & 0o111, f"{name} should be executable"


def test_install_mock_binaries_invokes_cursor_agent_emits_three_section(
    tmp_path: Path,
) -> None:
    """End-to-end: shim cursor-agent runs and emits the 3-section block."""
    import subprocess

    from tests.fixtures.mock_cli import install_mock_binaries

    bin_dir = tmp_path / "bin"
    install_mock_binaries(bin_dir)
    cmd = [str(bin_dir / "cursor-agent"), "agent", "--print", "test"]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert cp.returncode == 0
    assert "[devola-flow:round=1]" in cp.stdout
    assert "## Acceptance Verification" in cp.stdout
    assert "## Findings" in cp.stdout
