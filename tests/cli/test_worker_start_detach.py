"""v1.3.0 P1 — ``popola cloud worker start --detach`` background lifecycle.

Pins the new ``--detach`` Typer flag described in
``feedback_for_v1.2.0.md`` §7 and
``.local/research/v1.3.0_patches/PLAN.md`` Patch P1. The flag flips
``worker_start_cmd`` from the v1.1.1 foreground default to a double-fork
+ ``setsid`` detached spawn so the worker's PPID becomes 1 (init /
systemd) and closing the launching IDE terminal cannot cascade SIGHUP to
the worker.

These tests do NOT exercise the real fork path — they monkey-patch
:func:`popolaloom.cli.cloud_worker_cmd._spawn_detached_worker` so the
fork pattern stays hermetic. The actual double-fork code is exercised
indirectly via the acceptance criterion in PLAN.md ("killing the
spawning shell does not kill the worker"). What we pin here is the
CLI plumbing:

1. ``--detach`` invokes :func:`_spawn_detached_worker` (NOT
   :func:`_spawn_worker_subprocess`) and prints the helper's return
   value as one JSON line on stdout.
2. Without ``--detach``, :func:`_spawn_worker_subprocess` is invoked
   instead — the v1.1.1 foreground default is preserved verbatim.
3. ``--detach --dry-run`` prints the argv (and a ``(dry run)`` marker)
   without invoking either spawn helper; useful for ``popola init``
   wizards / config check scripts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from popolaloom.cli import cloud_worker_cmd

runner = CliRunner()


def test_detach_invokes_spawn_detached_worker(monkeypatch, tmp_path: Path) -> None:
    """``--detach`` routes through :func:`_spawn_detached_worker`.

    The recorder asserts both that the helper was invoked with the
    expected keyword arguments AND that the foreground spawn was NOT
    invoked (the failed-assertion lambda surfaces any accidental
    regression to the foreground path).
    """
    calls: list[dict[str, Any]] = []

    def fake_detached(
        argv: list[str],
        *,
        name: str,
        log_dir: Path,
        pid_dir: Path,
    ) -> dict[str, Any]:
        calls.append(
            {
                "argv": list(argv),
                "name": name,
                "log_dir": str(log_dir),
                "pid_dir": str(pid_dir),
            }
        )
        return {
            "pid": 12345,
            "name": name,
            "worker_dir": "/tmp",
            "log_file": str(log_dir / f"worker-{name}.log"),
            "pid_file": str(pid_dir / f"worker-{name}.pid"),
            "management_addr": None,
            "detached": True,
        }

    def fail_fg(argv: list[str], *, pool: bool) -> int:
        raise AssertionError(
            "foreground spawn should NOT be invoked under --detach"
        )

    monkeypatch.setattr(cloud_worker_cmd, "_spawn_detached_worker", fake_detached)
    monkeypatch.setattr(cloud_worker_cmd, "_spawn_worker_subprocess", fail_fg)
    monkeypatch.setattr(
        cloud_worker_cmd, "_resolve_agent_binary", lambda: "/usr/bin/agent"
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda *a, **k: [],
    )

    result = runner.invoke(
        cloud_worker_cmd.app,
        ["start", "--detach", "--worker-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    payload = json.loads(result.output.strip())
    assert payload["detached"] is True
    assert payload["pid"] == 12345
    assert payload["name"].startswith(f"popolaloom-{tmp_path.name}-")


def test_no_detach_invokes_foreground_subprocess(
    monkeypatch, tmp_path: Path
) -> None:
    """Without ``--detach``, the v1.1.1 foreground spawn path runs.

    Mirrors :test:`test_detach_invokes_spawn_detached_worker` but
    inverted: the foreground recorder captures argv while the
    detached spawn explodes if accidentally called. Pins the
    no-regression contract for operators who do not opt into the
    new background mode.
    """
    foreground_calls: list[list[str]] = []

    def fake_fg(argv: list[str]) -> int:
        foreground_calls.append(list(argv))
        return 0

    def fail_detach(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "detached spawn should NOT be invoked without --detach"
        )

    monkeypatch.setattr(cloud_worker_cmd, "_spawn_worker_subprocess", fake_fg)
    monkeypatch.setattr(cloud_worker_cmd, "_spawn_detached_worker", fail_detach)
    monkeypatch.setattr(
        cloud_worker_cmd, "_resolve_agent_binary", lambda: "/usr/bin/agent"
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda *a, **k: [],
    )

    result = runner.invoke(
        cloud_worker_cmd.app,
        ["start", "--worker-dir", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert len(foreground_calls) == 1


def test_dry_run_with_detach(monkeypatch, tmp_path: Path) -> None:
    """``--detach --dry-run`` prints argv preview without spawning.

    Both spawn helpers are intentionally left un-stubbed: if the
    dry-run short-circuit ever regresses and the code falls through
    to a real spawn, the test will trip on the real ``os.fork`` /
    subprocess call (which we don't want in CI).
    """
    monkeypatch.setattr(
        cloud_worker_cmd, "_resolve_agent_binary", lambda: "/usr/bin/agent"
    )
    monkeypatch.setattr(
        cloud_worker_cmd,
        "_detect_running_workers_for_dir",
        lambda *a, **k: [],
    )

    result = runner.invoke(
        cloud_worker_cmd.app,
        ["start", "--detach", "--dry-run", "--worker-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "worker" in result.output
    assert "start" in result.output
