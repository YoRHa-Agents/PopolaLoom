"""Subprocess supervisor — spawns CLI children with cross-terminal survival.

Day-1 路径选择 (出处: spec §3.1 Mermaid + 06 §0.0 Q7 答案):

- 直接 ``subprocess.Popen(cmd, start_new_session=True)`` (相当于
  ``setsid``); 当父 (popolad / 测试) 退出, 子进程不会收到 ``SIGHUP``,
  满足 NFR-5 (≥99% 跨终端存活)。
- stdout / stderr 各起一个 daemon 后台线程 ``readline()`` 循环, 每行
  封装成 CloudEvents ``process.stdout`` / ``process.stderr`` 事件追加到
  :class:`popolaloom.daemon.event_log.EventLog`。
- 第三个 wait 线程 ``proc.wait()`` 后写入终态事件 ``task.completed`` /
  ``task.failed`` (含 ``exit_code``), 然后调可选的 ``on_exit`` 回调,
  让 :class:`Popolad` 同步更新 :class:`StateStore`。

v0.2.0 修复 (Stage A A6):

- R-007: ``stdout/stderr.join(timeout=30.0)`` (原 5s 太短, 大输出场景
  可能丢最后几行); join 超时则 emit ``stream.truncated`` event 含
  ``actual_lines`` / ``reason`` 字段, 不再静默吞噬。

# TODO(v0.3.0): detect ``systemctl --user`` 可用 → wrap cmd 为
# ``systemd-run --user --scope --unit=popola-<task_id> ...``; tmux fallback
# 与 nohup 兜底也在该阶段补 (R-010, 推迟到 v0.3.0)。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from popolaloom.daemon.event_log import EventLog

logger = logging.getLogger(__name__)


_DRAIN_JOIN_TIMEOUT_S: float = 30.0
"""Maximum wait for stdout/stderr drain threads after subprocess exit (R-007).

Was 5.0s in v0.0.1; bumped to 30.0s for large-output scenarios (e.g. ``cursor
agent --print`` 18000-char outputs). On timeout, the supervisor emits a
``stream.truncated`` event so callers see explicit evidence of truncation
(No Silent Failures — see R-007 in 09-iter1-self-eval.md §5)."""


class Supervisor:
    """Spawns and monitors per-task subprocesses, streaming output to event log.

    Day-1 实现是纯 ``subprocess.Popen`` + ``threading``; 不引入 asyncio
    以避免与上层 popolad 的事件循环耦合 (上层 Stage Impl-3 的 LangGraph
    + uvicorn 是 asyncio, 我们只需保证 ``spawn()`` 立即返回, 不阻塞)。
    """

    def __init__(self) -> None:
        self._workers: dict[str, list[threading.Thread]] = {}
        self._lock = threading.Lock()
        self._line_counts: dict[str, dict[str, int]] = {}
        """Per-task stream line counters; updated atomically by drain threads.

        Shape: ``{task_id: {"stdout": int, "stderr": int}}``. Reads after
        ``proc.wait`` returns are safe-ish (drain thread is still running but
        the count only grows), and we use it for ``stream.truncated`` event
        ``actual_lines`` reporting on join timeout (R-007 fix).
        """

    def spawn(
        self,
        task_id: str,
        cmd: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        event_log: EventLog,
        on_exit: Callable[[str, int], None] | None = None,
    ) -> int:
        """Start ``cmd`` as a detached subprocess and stream output to ``event_log``.

        Args:
            task_id: 用于标识该 task 的字符串 (出现在 ``data.task_id`` 字段)。
            cmd: 命令 + 参数列表 (例如 ``["python", "-c", "print('hi')"]``)。
            cwd: 子进程工作目录 (``None`` 沿用父进程 CWD)。
            env: 子进程环境变量 (``None`` 继承父进程; 显式传入会**完全替换**,
                调用方需自行合并 ``os.environ``)。
            event_log: 行级 NDJSON 写入器, 每条 stdout / stderr 行追加一个
                ``process.stdout`` / ``process.stderr`` 事件。
            on_exit: 可选回调 ``(task_id, exit_code) -> None``, 在终态事件
                写入后被调用 (在 wait 线程内同步执行, 注意线程安全)。

        Returns:
            int: 子进程 PID (popolad 立即可见, 不等子进程完成)。
        """
        cwd_str = str(cwd) if cwd is not None else None

        proc = subprocess.Popen(  # noqa: S603 - 调用方负责传入安全的 cmd
            cmd,
            cwd=cwd_str,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲, 配合 readline()
            start_new_session=True,  # = setsid(2); 父退出不传 SIGHUP
        )
        pid = proc.pid

        # 事件: 子进程已启动 (不等于 task.dispatched, dispatched 由上层 Popolad 写)
        event_log.append(
            "process.started",
            {
                "task_id": task_id,
                "pid": pid,
                "cmd": cmd,
                "cwd": cwd_str,
                "session_id": _get_session_id(pid),
            },
        )

        stdout_thread = threading.Thread(
            target=self._drain_stream,
            args=(task_id, proc.stdout, "stdout", event_log),
            name=f"popolad-stdout-{task_id}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._drain_stream,
            args=(task_id, proc.stderr, "stderr", event_log),
            name=f"popolad-stderr-{task_id}",
            daemon=True,
        )
        wait_thread = threading.Thread(
            target=self._wait_and_finalize,
            args=(task_id, proc, event_log, stdout_thread, stderr_thread, on_exit),
            name=f"popolad-wait-{task_id}",
            daemon=True,
        )

        with self._lock:
            self._workers[task_id] = [stdout_thread, stderr_thread, wait_thread]
            self._line_counts[task_id] = {"stdout": 0, "stderr": 0}

        stdout_thread.start()
        stderr_thread.start()
        wait_thread.start()

        return pid

    def _drain_stream(
        self,
        task_id: str,
        stream: Any,
        stream_name: str,
        event_log: EventLog,
    ) -> None:
        """读子进程的 PIPE 直到 EOF, 每行发一个 process.<stream_name> 事件.

        v0.2.0: per-stream line counter 维护在 ``self._line_counts[task_id]``
        以便 wait thread 在 join 超时时知道实际写了多少行 (R-007).
        """
        try:
            for raw_line in iter(stream.readline, ""):
                line = raw_line.rstrip("\r\n")
                if line == "" and raw_line == "":
                    break
                event_log.append(
                    f"process.{stream_name}",
                    {"task_id": task_id, "stream": stream_name, "line": line},
                )
                counts = self._line_counts.get(task_id)
                if counts is not None:
                    counts[stream_name] = counts.get(stream_name, 0) + 1
        except Exception as exc:
            logger.exception("Stream drain failed for task %s (%s)", task_id, stream_name)
            event_log.append(
                "process.stream_error",
                {"task_id": task_id, "stream": stream_name, "error": repr(exc)},
            )
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - close 异常无意义, 但记一笔
                logger.debug("Stream close failed for %s/%s", task_id, stream_name)

    def _wait_and_finalize(
        self,
        task_id: str,
        proc: subprocess.Popen[str],
        event_log: EventLog,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
        on_exit: Callable[[str, int], None] | None,
    ) -> None:
        """阻塞等子进程退出, 然后写终态事件 + 调 on_exit 回调.

        v0.2.0 R-007 fix: drain join timeout = ``_DRAIN_JOIN_TIMEOUT_S`` (30s),
        超时则在终态事件之前 emit ``stream.truncated`` event 含
        ``{stream, actual_lines, reason: 'join_timeout_30s'}``.
        """
        try:
            exit_code = proc.wait()
        except Exception as exc:  # noqa: BLE001
            logger.exception("proc.wait failed for task %s", task_id)
            event_log.append(
                "task.failed",
                {"task_id": task_id, "exit_code": -1, "error": repr(exc)},
            )
            if on_exit is not None:
                self._safe_on_exit(on_exit, task_id, -1)
            return

        stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)
        if stdout_thread.is_alive():
            self._emit_stream_truncated(task_id, "stdout", event_log)
        stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)
        if stderr_thread.is_alive():
            self._emit_stream_truncated(task_id, "stderr", event_log)

        event_type = "task.completed" if exit_code == 0 else "task.failed"
        event_log.append(
            event_type,
            {"task_id": task_id, "exit_code": exit_code, "pid": proc.pid},
        )

        if on_exit is not None:
            self._safe_on_exit(on_exit, task_id, exit_code)

    def _emit_stream_truncated(
        self,
        task_id: str,
        stream_name: str,
        event_log: EventLog,
    ) -> None:
        """Emit ``stream.truncated`` event when a drain thread exceeded the join timeout.

        per R-007: 终态事件契约要求 ``task.completed`` / ``task.failed`` 是
        文件末行; 但若 drain 线程仍在写, 这个契约会被破坏。我们的折衷:
        显式 emit 一个 ``stream.truncated`` 事件 (含 ``actual_lines``), 然后
        立即写终态事件; 后续可能仍有 drain 线程 stragglers 落下来, 但调用方
        看到 ``stream.truncated`` 后就知道终态事件后的内容是 best-effort 的。
        """
        counts = self._line_counts.get(task_id, {})
        actual_lines = counts.get(stream_name, 0)
        event_log.append(
            "stream.truncated",
            {
                "task_id": task_id,
                "stream": stream_name,
                "actual_lines": actual_lines,
                "expected_lines": None,
                "reason": "join_timeout_30s",
            },
        )
        logger.warning(
            "stream.truncated for task=%s stream=%s actual_lines=%d "
            "(drain thread still alive after %.1fs)",
            task_id,
            stream_name,
            actual_lines,
            _DRAIN_JOIN_TIMEOUT_S,
        )

    @staticmethod
    def _safe_on_exit(
        callback: Callable[[str, int], None],
        task_id: str,
        exit_code: int,
    ) -> None:
        """on_exit 回调里若抛异常, 不能让 wait 线程崩 (但要 log)."""
        try:
            callback(task_id, exit_code)
        except Exception:  # noqa: BLE001 - 回调失败不影响事件已写入
            logger.exception("on_exit callback failed for task %s", task_id)

    def join(self, task_id: str, timeout: float | None = None) -> bool:
        """Block until all worker threads of ``task_id`` finish.

        主要给测试用; 生产环境的 ``Popolad`` 不需要 join。

        Returns:
            bool: ``True`` if all threads finished within ``timeout``.
        """
        with self._lock:
            threads = list(self._workers.get(task_id, []))
        if not threads:
            return True
        for t in threads:
            t.join(timeout=timeout)
        return all(not t.is_alive() for t in threads)


def _get_session_id(pid: int) -> int | None:
    """Return ``setsid``-assigned session id of ``pid`` (Linux/macOS), else ``None``."""
    try:
        return os.getsid(pid)
    except (OSError, AttributeError):
        return None
