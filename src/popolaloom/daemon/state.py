"""In-process task state store for popolad daemon (v0.2.0 Stage A).

只承担"当前活跃 task 的运行时句柄"职责; 持久化 (跨 daemon 重启 + 历史
查询) 走 ArkTower SQLite 任务池 (Stage C 接入)。

设计 invariants:

- ``StateStore`` 内部 dict 的写入用 :class:`threading.Lock` 保护, 因为
  supervisor 的后台线程会在子进程 exit 时 update 状态, 与主线程 dispatch
  并发。
- ``TaskState`` 是 :class:`enum.StrEnum`, 直接序列化即字符串, 与 spec
  §3.5.3 ``status`` enum 对齐 (但注意 spec 用的是 ArkTower 10-state FSM,
  本枚举只覆盖 popolad 进程级关注的 5 个; 完整 FSM 转换交给 ArkTower)。

v0.2.0 新增 (Stage A A3):

- :meth:`StateStore.rehydrate` — 启动时从 ArkTower SQLite 批量加载
  in-flight task handle 的 hook 点。Stage A 仅落 hook 形态, Stage C
  实际接入 :class:`arktower.core.task_service.TaskService`。
- :attr:`TaskHandle.persisted` — 标记该 handle 是否成功落到 ArkTower
  SQLite。修复 R-008 中"假装持久化"的违规 (No Silent Failures): 当
  ArkTower create_task 失败时, ``persisted=False`` 让上层显式知道。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class TaskState(StrEnum):
    """popolad 进程级别的 task 生命周期状态。

    注意: ArkTower ``TaskStatus`` 有 10 个 (submitted/queued/in_progress/
    review/input_required/blocked/completed/failed/canceled/timed_out)。
    本枚举只覆盖 popolad 关心的 5 个粗粒度子集; ``RUNNING`` 对应
    ArkTower ``IN_PROGRESS``, 后续 awaiting_input 等会在 Stage B
    HITL 接入时扩展。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}
)


@dataclass
class TaskHandle:
    """Runtime handle for a dispatched task (held in memory by popolad).

    Args:
        task_id: popola 内部 task id (例: ``cursor-abcdef012345``)。
        cli: CLI 名 (``cursor`` / ``claude`` / ``codex`` / ...)。
        pid: 子进程 pid (``None`` 在 dispatch 后 spawn 前的过渡窗口)。
        state: 当前状态 (默认 PENDING; dispatch_task 直接置为 RUNNING)。
        started_at: 派发时刻 (UTC ISO ms 精度)。
        event_log_path: NDJSON 事件文件路径。
        arktower_task_id: ArkTower SQLite ``Task.id``; ``None`` 表示
            未注入 task_repository 或 ArkTower 持久化失败。
        exit_code: 子进程退出码 (终态后才有值)。
        cmd: 实际 spawn 的 argv list, 便于调试 / 重放。
        completed_at: 进入终态的时刻; 非终态为 ``None``。
        persisted: 是否成功持久化到 ArkTower SQLite (R-008 修复)。
            ``True`` 当 task_service.create_task 成功; ``False`` 当
            未注入 repo / arktower 不可 import / create 抛异常。
        cancel_escalated_to_sigkill: v0.4.1 (Stage L1.A) — 标记
            :meth:`Popolad.cancel_task` 是否在 SIGTERM grace window
            内升级到了 SIGKILL。供 :class:`Supervisor` 的 wait 线程在
            emit ``task.canceled`` 终态事件时填充
            ``data.sigkill_escalated`` 字段, 闭合 [research §F.3](
            ../../../.local/memory/research/v0.5.0-skill-install-lark-research.md)
            消费侧 (`evaluation/runner.py:325-331`) 的 ``task.canceled``
            计数契约 + 让 v0.4.1 Stage L2 通知卡能区分"用户主动取消"
            vs "升级 SIGKILL 强杀"两类。默认 ``False``;
            :meth:`cancel_task` 仅在 SIGKILL 真发出时翻转。
    """

    task_id: str
    cli: str
    pid: int | None
    state: TaskState
    started_at: datetime
    event_log_path: Path
    arktower_task_id: str | None = None
    exit_code: int | None = None
    cmd: list[str] = field(default_factory=list)
    completed_at: datetime | None = None
    persisted: bool = False
    cancel_escalated_to_sigkill: bool = False

    def is_terminal(self) -> bool:
        """``True`` iff this task is in a terminal state (no further transitions)."""
        return self.state in _TERMINAL_STATES


class StateStore:
    """In-memory ``task_id -> TaskHandle`` registry, thread-safe.

    Methods are intentionally minimal; persistence (cross-restart recovery)
    will come in Stage C by mirroring writes into the ArkTower SQLite
    task pool (出处: spec §3.2 popolad 行 + ADR-0001).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskHandle] = {}
        self._lock = threading.Lock()

    def register(self, handle: TaskHandle) -> None:
        """Insert a new task handle.

        Raises:
            ValueError: 当 ``handle.task_id`` 已存在 (避免 silent overwrite,
                符合工作区 "No Silent Failures" 规则)。
        """
        with self._lock:
            if handle.task_id in self._tasks:
                raise ValueError(f"task_id already registered: {handle.task_id}")
            self._tasks[handle.task_id] = handle

    def get(self, task_id: str) -> TaskHandle | None:
        """Return the handle, or ``None`` if not found."""
        with self._lock:
            return self._tasks.get(task_id)

    def update(
        self,
        task_id: str,
        *,
        state: TaskState | None = None,
        pid: int | None = None,
        exit_code: int | None = None,
        completed_at: datetime | None = None,
        persisted: bool | None = None,
        cancel_escalated_to_sigkill: bool | None = None,
    ) -> TaskHandle:
        """Update mutable fields on a registered handle and return the new value.

        v0.4.1 Stage L1.A: ``cancel_escalated_to_sigkill`` exposed so
        :meth:`Popolad.cancel_task` can mark the SIGKILL escalation
        *before* sending the signal — the supervisor wait-thread
        consults this flag right before emitting the ``task.canceled``
        terminal event so its ``data.sigkill_escalated`` is accurate.

        Raises:
            KeyError: 当 ``task_id`` 未注册。
        """
        with self._lock:
            handle = self._tasks.get(task_id)
            if handle is None:
                raise KeyError(f"task_id not registered: {task_id}")
            if state is not None:
                handle.state = state
            if pid is not None:
                handle.pid = pid
            if exit_code is not None:
                handle.exit_code = exit_code
            if completed_at is not None:
                handle.completed_at = completed_at
            elif state in _TERMINAL_STATES and handle.completed_at is None:
                handle.completed_at = datetime.now(UTC)
            if persisted is not None:
                handle.persisted = persisted
            if cancel_escalated_to_sigkill is not None:
                handle.cancel_escalated_to_sigkill = cancel_escalated_to_sigkill
            return handle

    def list_active(self) -> list[TaskHandle]:
        """Return handles whose state is not terminal (snapshot copy)."""
        with self._lock:
            return [h for h in self._tasks.values() if not h.is_terminal()]

    def list_all(self) -> list[TaskHandle]:
        """Return all handles (snapshot copy), terminal + active."""
        with self._lock:
            return list(self._tasks.values())

    def rehydrate(self, handles: Iterable[TaskHandle]) -> None:
        """Bulk-load existing handles into the in-memory registry.

        Stage A hook (placeholder). Stage C 会在 popolad 启动时调用此方法,
        把 ArkTower SQLite ``tasks`` 表中状态非终态的 task 重新装回
        in-memory dict, 实现"popolad 重启可看到上次 in-flight task"
        (NFR-8 + S1 self-bootstrap).

        Semantics:

        - 已存在的 task_id 会被覆盖 (rehydrate 是 authoritative 源,
          适合 daemon 启动时一次性加载, 不适合运行期重复调)。
        - 调用方负责保证 handle 状态合理 (例: 不要 rehydrate 一个
          ``state=PENDING`` 但 ``completed_at`` 已设置的矛盾 handle)。

        Args:
            handles: 任意 iterable of :class:`TaskHandle`. 内部一次性
                抓锁批量写入, 中途异常会回滚已写入的部分 (用 dict.copy
                做事务性切换), 符合 No Silent Failures 规则。

        Raises:
            ValueError: 当 ``handles`` 中存在重复 task_id (输入数据自冲突)。
        """
        new_handles = list(handles)
        seen: dict[str, TaskHandle] = {}
        for h in new_handles:
            if h.task_id in seen:
                raise ValueError(
                    f"rehydrate input contains duplicate task_id: {h.task_id}"
                )
            seen[h.task_id] = h

        with self._lock:
            for tid, handle in seen.items():
                self._tasks[tid] = handle
