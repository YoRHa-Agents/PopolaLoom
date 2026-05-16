"""v1.3.0 P3 — :func:`_parse_worker_start_cmdline` handles Node-wrapped argv.

Pins the matcher fix described in ``feedback_for_v1.2.0.md`` §7
("``popola cloud worker stop`` 当前定位 bug"): the modern cursor-agent
install ships ``agent`` as a small shell shim that ``exec``s
``node /path/to/agent.js worker start ...``, so the running process has
``argv[0] == "node"`` and the pre-v1.3.0 basename-only check failed to
identify it as a worker. The fix makes :func:`_parse_worker_start_cmdline`
scan argv for the ``["worker", "start"]`` verb pair plus a "worker binary
indicator" token (basename ``agent`` / ``cursor-agent`` OR token ending in
``/agent.js`` / ``/cursor-agent.js``) anywhere in argv, so both the direct
and Node-wrapped invocations are accepted.

The tests below pin the three behaviours called out in the
``.local/research/v1.3.0_patches/PLAN.md`` Patch P3 acceptance list:

1. Direct ``["agent", "worker", "start", "--worker-dir", "/x"]`` argv is
   parsed (the v1.1.1 behaviour stays intact).
2. Node-wrapped ``["node", "/usr/lib/cursor-agent/agent.js", "worker",
   "start", "--worker-dir", "/x", "--name", "wname"]`` argv is parsed
   (the new behaviour — covers the actual production cmdline shape).
3. Unrelated ``["agent", "some-other-verb", "--worker-dir", "/x"]`` argv
   returns ``None`` (negative case; protects against accidental
   matches when only the binary indicator is present).
"""
from __future__ import annotations

from pathlib import Path

from popolaloom.cli.cloud_worker_cmd import _parse_worker_start_cmdline


def test_direct_agent_argv_matched() -> None:
    """The legacy direct ``agent worker start ...`` argv parses cleanly.

    Regression guard for the v1.1.1 behaviour: even though P3 broadens
    the matcher, the original direct-invocation form must continue to
    work — operators on older cursor-agent installs (where ``agent``
    is a real binary, not a Node shim) still need ``popola cloud
    worker stop --worker-dir`` to locate them.
    """
    argv = ["agent", "worker", "start", "--worker-dir", "/tmp/x"]
    parsed = _parse_worker_start_cmdline(12345, argv)
    assert parsed is not None
    assert parsed.pid == 12345
    assert parsed.worker_dir == Path("/tmp/x").resolve()


def test_node_wrapped_argv_matched() -> None:
    """Node-wrapped ``node /path/agent.js worker start ...`` argv parses.

    This is the actual production cmdline shape on cursor-agent
    installs from 2026-04+ where ``/usr/local/bin/agent`` is a shell
    shim that ``exec``s ``node /usr/lib/cursor-agent/agent.js ...``.
    The pre-v1.3.0 matcher rejected this because ``argv[0]`` was
    ``"node"`` (not in :data:`_WORKER_CMD_BASENAMES`), which is why
    ``popola cloud worker stop`` failed to locate live workers in
    feedback §7.
    """
    argv = [
        "node",
        "/usr/lib/cursor-agent/agent.js",
        "worker",
        "start",
        "--worker-dir",
        "/tmp/x",
        "--name",
        "wname",
    ]
    parsed = _parse_worker_start_cmdline(99999, argv)
    assert parsed is not None
    assert parsed.pid == 99999
    assert parsed.name == "wname"
    assert parsed.worker_dir == Path("/tmp/x").resolve()


def test_unrelated_argv_rejected() -> None:
    """``agent <other-verb> --worker-dir /x`` returns ``None``.

    Negative case — ensures the binary-indicator check is necessary
    but not sufficient: a process whose argv contains the agent
    binary but does NOT include the ``["worker", "start"]`` verb
    pair must still be rejected so ``popola cloud worker stop`` only
    targets actual worker processes.
    """
    argv = ["agent", "some-other-verb", "--worker-dir", "/tmp/x"]
    parsed = _parse_worker_start_cmdline(11111, argv)
    assert parsed is None
