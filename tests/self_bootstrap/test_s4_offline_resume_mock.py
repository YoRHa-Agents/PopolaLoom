"""S4 self-bootstrap (mock): 8h offline + reopen IDE → task survives.

Per spec.md §3.4.1 S4 + roadmap §3.4 v0.2.3 — exercises the
"developer closes IDE for 8h, daemon survives, reopens to find the
task still attachable" scenario without actually waiting 8 hours.

Strategy:

1. Spawn real popolad subprocess.
2. Dispatch a long-sleeping mock cursor task.
3. Use ``freezegun`` to advance the test process clock by 8 hours
   (so any deadline-related logic sees the time skip).  The daemon
   is in a separate process so the *daemon's* monotonic clock keeps
   ticking; the freeze only affects what the test process sees.
4. Verify the task is still in the daemon's listing + still
   attachable by re-checking the status — proves the daemon's
   in-memory state wasn't lost (no re-spawn happened on the daemon
   side; the daemon is a long-lived OS process).
5. Cancel the long-running task to clean up.

NOTE: the v0.3.0 real S4 will additionally exercise the
``rehydrate_from_persistence`` path after a daemon restart simulating
a real 8h offline+reopen scenario.  Here we cover the simpler
"daemon stays up; clock skipped 8h on caller side" subset.
"""

from __future__ import annotations

import contextlib
import json
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from tests.fixtures.real_popolad import (
    RealPopoladHandle,
    spawn_real_popolad,
)

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    args: list[str], env: dict[str, str], timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "popolaloom.cli.main", *args]
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO_ROOT),
    )


def _make_long_running_shim(bin_dir: Path, sleep_seconds: float = 30.0) -> Path:
    """Create a fake cursor-agent shim that sleeps long enough to outlive the test."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "cursor-agent"
    shim.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "print('[devola-flow:round=1]', flush=True)\n"
            "print('long-running mock; sleeping for offline simulation', flush=True)\n"
            f"time.sleep({sleep_seconds})\n"
            "print('## Acceptance Verification', flush=True)\n"
            "print('## Gate Score Components', flush=True)\n"
            "print('- composite: 0.886', flush=True)\n"
            "print('## Findings', flush=True)\n"
        ),
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@contextlib.contextmanager
def _spawn_with_long_running_shim(tmp_path: Path) -> Iterator[RealPopoladHandle]:
    bin_dir = tmp_path / "bin"
    _make_long_running_shim(bin_dir, sleep_seconds=30.0)
    with spawn_real_popolad(tmp_path, extra_path=bin_dir) as handle:
        yield handle


def test_s4_eight_hour_offline_then_reattach(tmp_path: Path) -> None:
    """S4: 8h freezegun travel; long-running task still discoverable."""
    with _spawn_with_long_running_shim(tmp_path) as handle:
        env = handle.env.copy()

        d = _run_cli(
            ["dispatch", "long-running offline test", "--cli", "cursor", "--json"],
            env=env,
            timeout=15.0,
        )
        assert d.returncode == 0, f"dispatch failed: {d.stderr}"
        task_id = json.loads(d.stdout.strip().splitlines()[-1])["task_id"]
        assert task_id

        time.sleep(0.5)
        list_result = _run_cli(["list", "--json"], env=env, timeout=10.0)
        assert list_result.returncode == 0
        listed = json.loads(list_result.stdout.strip().splitlines()[-1])
        ids_pre = {item["task_id"] for item in listed}
        assert task_id in ids_pre, (
            f"task missing from pre-skip list: {ids_pre}"
        )

        skip_target = datetime.now(UTC) + timedelta(hours=8)
        with freeze_time(skip_target):
            list_after = _run_cli(["list", "--all", "--json"], env=env, timeout=10.0)
            assert list_after.returncode == 0
            listed_after = json.loads(list_after.stdout.strip().splitlines()[-1])
            ids_after = {item["task_id"] for item in listed_after}
            assert task_id in ids_after, (
                f"task missing after 8h freezegun skip: {ids_after}"
            )

            status_after = _run_cli(["status", task_id, "--json"], env=env, timeout=10.0)
            assert status_after.returncode == 0
            payload = json.loads(status_after.stdout.strip().splitlines()[-1])
            assert payload["task_id"] == task_id

        cancel = _run_cli(["cancel", task_id, "--json"], env=env, timeout=10.0)
        assert cancel.returncode == 0
