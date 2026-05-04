"""Stage Impl-2 tests for popolad daemon (event_log / state / supervisor / server).

每个测试用 ``tmp_path`` fixture 隔离, 不污染真实 ``~/.popola/``。
全部 4 个测试要求 ≤ 5s 完成 (NFR-1 启动 ≤ 2s 推断的合理上限)。
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from popolaloom.daemon import (
    EventLog,
    Popolad,
    StateStore,
    Supervisor,
    TaskHandle,
    TaskState,
)

# ── EventLog ─────────────────────────────────────────────────────────────


def test_event_log_append_and_tail(tmp_path: Path) -> None:
    """append() 三次后 tail() 返回 3 条 dict, 每条满足 CloudEvents 1.0 必填字段."""
    log_path = tmp_path / "events" / "T-abc.jsonl"
    log = EventLog(log_path)

    e1 = log.append("task.dispatched", {"task_id": "T-abc", "cli": "claude"})
    e2 = log.append("process.stdout", {"task_id": "T-abc", "line": "hello"})
    e3 = log.append("task.completed", {"task_id": "T-abc", "exit_code": 0})

    # append 自身返回的 envelope 一致
    assert e1["type"] == "task.dispatched"
    assert e2["type"] == "process.stdout"
    assert e3["type"] == "task.completed"

    events = log.tail()
    assert len(events) == 3
    for ev in events:
        # CloudEvents 1.0 必填字段
        assert ev["specversion"] == "1.0"
        assert ev["id"].startswith("evt-")
        assert ev["source"] == "popola/T-abc"
        assert "type" in ev
        assert "time" in ev
        assert ev["time"].endswith("Z")  # ISO + Z 后缀
        assert "data" in ev

    assert events[0]["data"]["cli"] == "claude"
    assert events[1]["data"]["line"] == "hello"
    assert events[2]["data"]["exit_code"] == 0

    # since_index 增量
    assert log.tail(since_index=2) == [events[2]]
    assert log.tail(since_index=10) == []
    assert len(log) == 3


# ── StateStore ───────────────────────────────────────────────────────────


def test_state_store_register_and_update(tmp_path: Path) -> None:
    """register → update(state=COMPLETED) → list_active 不再返回该 task."""
    store = StateStore()

    handle_a = TaskHandle(
        task_id="T-a",
        cli="claude",
        pid=None,
        state=TaskState.PENDING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "T-a.jsonl",
    )
    handle_b = TaskHandle(
        task_id="T-b",
        cli="codex",
        pid=None,
        state=TaskState.PENDING,
        started_at=datetime.now(UTC),
        event_log_path=tmp_path / "T-b.jsonl",
    )
    store.register(handle_a)
    store.register(handle_b)

    # 重复注册必须报错 (No Silent Failures)
    with pytest.raises(ValueError):
        store.register(handle_a)

    assert {h.task_id for h in store.list_active()} == {"T-a", "T-b"}

    updated = store.update("T-a", state=TaskState.RUNNING, pid=12345)
    assert updated.state == TaskState.RUNNING
    assert updated.pid == 12345
    assert store.get("T-a").pid == 12345

    store.update("T-b", state=TaskState.COMPLETED, exit_code=0)
    active_ids = {h.task_id for h in store.list_active()}
    assert active_ids == {"T-a"}, "终态 task 不应出现在 list_active"

    all_ids = {h.task_id for h in store.list_all()}
    assert all_ids == {"T-a", "T-b"}

    # 终态 handle 应自动有 completed_at
    assert store.get("T-b").completed_at is not None
    assert store.get("T-b").is_terminal() is True

    with pytest.raises(KeyError):
        store.update("T-missing", state=TaskState.FAILED)


# ── Supervisor ───────────────────────────────────────────────────────────


def test_supervisor_spawn_echo(tmp_path: Path) -> None:
    """spawn 一个简短 python 子进程, 等其退出, 验证 NDJSON 含 stdout 行 + completed."""
    log_path = tmp_path / "events" / "T-echo.jsonl"
    event_log = EventLog(log_path)
    supervisor = Supervisor()

    pid = supervisor.spawn(
        task_id="T-echo",
        cmd=[sys.executable, "-c", "print('hello'); import sys; sys.exit(0)"],
        cwd=tmp_path,
        env=None,
        event_log=event_log,
    )
    assert isinstance(pid, int) and pid > 0

    # 用 join (有上限) 替代固定 sleep, 更稳; fallback 1.5s 足够 cold-start python
    finished = supervisor.join("T-echo", timeout=3.0)
    assert finished, "子进程 + 工作线程未在 3s 内退出"

    events = event_log.tail()
    types = [ev["type"] for ev in events]

    # 必含: 启动 + stdout 至少一行 + 完成
    assert "process.started" in types
    assert "task.completed" in types
    stdout_events = [ev for ev in events if ev["type"] == "process.stdout"]
    assert any(ev["data"].get("line") == "hello" for ev in stdout_events), (
        f"未找到 'hello' stdout 行, 实际 events: {events}"
    )

    completed = next(ev for ev in events if ev["type"] == "task.completed")
    assert completed["data"]["exit_code"] == 0
    assert completed["data"]["task_id"] == "T-echo"


# ── Popolad facade ──────────────────────────────────────────────────────


def test_popolad_dispatch_with_fake_adapter(tmp_path: Path) -> None:
    """Full dispatch 闭环: fake adapter → spawn → 状态 RUNNING→COMPLETED + 完整事件流."""
    events_dir = tmp_path / "events"

    def fake_adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, object] | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", f"print({prompt!r})"]

    popolad = Popolad(events_dir=events_dir, adapter=fake_adapter)

    task_id = popolad.dispatch_task(cli="testcli", prompt="noop")
    assert task_id.startswith("testcli-")

    # dispatch 后立即 (子进程可能还未结束) 状态应为 RUNNING (或已是 COMPLETED)
    status = popolad.get_status(task_id)
    assert status["state"] in {str(TaskState.RUNNING), str(TaskState.COMPLETED)}
    assert status["cli"] == "testcli"
    assert status["pid"] is not None and status["pid"] > 0
    assert status["started_at"]
    assert status["arktower_task_id"] is not None  # ArkTower Task model 已构造

    # 等子进程跑完 (≤ 3s 上限)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        status = popolad.get_status(task_id)
        if status["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"task did not reach terminal state in 3s: {status}")

    assert status["state"] == str(TaskState.COMPLETED)
    assert status["exit_code"] == 0
    assert status["completed_at"] is not None
    assert status["latest_event_index"] >= 3

    # tail_events 必含 dispatched + stdout('noop') + completed
    events = popolad.tail_events(task_id)
    types = [ev["type"] for ev in events]
    assert types[0] == "task.dispatched", f"first event 不是 dispatched: {types}"
    assert "process.stdout" in types
    assert "task.completed" in types
    stdout_events = [ev for ev in events if ev["type"] == "process.stdout"]
    assert any(ev["data"]["line"] == "noop" for ev in stdout_events), (
        f"未找到 'noop' stdout 行: {events}"
    )

    # since_index 增量 polling
    assert popolad.tail_events(task_id, since_index=len(events)) == []

    # 终态后 list_active 不返回该 task
    assert all(item["task_id"] != task_id for item in popolad.list_active())

    # event log 文件落到指定 events_dir
    expected_path = events_dir / f"{task_id}.jsonl"
    assert expected_path.exists()

    # 未注册的 task_id 必须 raise (No Silent Failures)
    with pytest.raises(KeyError):
        popolad.get_status("not-a-real-task-id")
    with pytest.raises(KeyError):
        popolad.tail_events("not-a-real-task-id")


# ── R-014 closure: __events_dir advisory hint honored per-task ───────────


def test_dispatch_honors_events_dir_advisory_hint(tmp_path: Path) -> None:
    """``extra['__events_dir']`` redirects the per-task NDJSON file (R-014 closure).

    Default ``self._events_dir`` is the daemon-wide root; Stage E adds
    a per-task override so multi-tenant tooling can silo events into
    a custom dir without restarting the daemon (e.g. self-bootstrap S3
    recursive dispatch isolates child tasks). The override:

    1. Routes the new task's NDJSON file into the override dir.
    2. Leaves OTHER tasks (no override) on the daemon-wide default.
    3. Does NOT mutate ``self._events_dir`` (subsequent unrelated tasks
       must still land on the default).
    4. Auto-creates the override directory tree.
    5. Passes the ``__events_dir`` key through to the adapter unchanged
       (back-compat with Stage A's
       ``test_cli_events_dir_advisory_passthrough`` contract).
    """
    default_dir = tmp_path / "default_events"
    override_dir = tmp_path / "child" / "isolated_events"

    seen_extras: list[dict[str, object]] = []

    def adapter(
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, object] | None = None,
    ) -> list[str]:
        seen_extras.append(dict(extra) if extra else {})
        return [sys.executable, "-c", f"print({prompt!r}); import sys; sys.exit(0)"]

    popolad = Popolad(events_dir=default_dir, adapter=adapter, use_graph=False)

    plain_task_id = popolad.dispatch_task(cli="plain", prompt="no override")
    plain_path = default_dir / f"{plain_task_id}.jsonl"

    redirected_task_id = popolad.dispatch_task(
        cli="redirected",
        prompt="with override",
        extra={"__events_dir": str(override_dir)},
    )
    redirected_path = override_dir / f"{redirected_task_id}.jsonl"

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        plain_status = popolad.get_status(plain_task_id)
        red_status = popolad.get_status(redirected_task_id)
        if (
            plain_status["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}
            and red_status["state"] in {str(TaskState.COMPLETED), str(TaskState.FAILED)}
        ):
            break
        time.sleep(0.05)
    else:
        pytest.fail("tasks did not terminate in 3s")

    assert plain_path.exists(), f"plain task NDJSON missing at {plain_path}"
    assert redirected_path.exists(), (
        f"redirected task NDJSON missing at {redirected_path}; "
        f"override_dir contents: {list(override_dir.glob('*'))}"
    )

    rogue_path = default_dir / f"{redirected_task_id}.jsonl"
    assert not rogue_path.exists(), (
        f"redirected task leaked into default events_dir at {rogue_path}"
    )

    assert popolad.events_dir == default_dir, (
        "popolad.events_dir was mutated by per-task override"
    )

    assert any(extra.get("__events_dir") == str(override_dir) for extra in seen_extras), (
        f"adapter did not receive __events_dir hint; seen_extras={seen_extras}"
    )
