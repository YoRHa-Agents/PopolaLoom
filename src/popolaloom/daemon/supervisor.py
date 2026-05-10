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

v0.8.5 (Cloud Agent Stage 2): ``cursor-cloud`` 适配器通过 argv 前缀
:data:`~popolaloom.adapters.cursor_cloud.CLOUD_BUILD_COMMAND_MARKER` 标记
云运行时。:meth:`spawn` 在检测到该前缀时走 :meth:`_spawn_cloud` —
无子进程、返回 ``0`` 作为「无 PID / runtime=cloud」哨兵, 由
:class:`~popolaloom.daemon.cloud_poller.CloudPollLoop` 后台轮询驱动
终态与 ``on_exit``。

v0.2.0 修复 (Stage A A6):

- R-007: ``stdout/stderr.join(timeout=30.0)`` (原 5s 太短, 大输出场景
  可能丢最后几行); join 超时则 emit ``stream.truncated`` event 含
  ``actual_lines`` / ``reason`` 字段, 不再静默吞噬。

# TODO(v0.3.0): detect ``systemctl --user`` 可用 → wrap cmd 为
# ``systemd-run --user --scope --unit=popola-<task_id> ...``; tmux fallback
# 与 nohup 兜底也在该阶段补 (R-010, 推迟到 v0.3.0)。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from popolaloom.adapters.cursor_cloud import CLOUD_BUILD_COMMAND_MARKER
from popolaloom.daemon.event_log import EventLog

if TYPE_CHECKING:
    from popolaloom.daemon.state import StateStore

logger = logging.getLogger(__name__)


_DRAIN_JOIN_TIMEOUT_S: float = 30.0
"""Maximum wait for stdout/stderr drain threads after subprocess exit (R-007).

Was 5.0s in v0.0.1; bumped to 30.0s for large-output scenarios (e.g. ``cursor
agent --print`` 18000-char outputs). On timeout, the supervisor emits a
``stream.truncated`` event so callers see explicit evidence of truncation
(No Silent Failures — see R-007 in 09-iter1-self-eval.md §5)."""


_SILENCE_TIMEOUT_SECS: float = 30.0
"""Stdout-silence threshold before the supervisor emits a ``process.note``.

v0.9.9 F1 (Q-V099-5 + Q-V099-14): when a child subprocess produces neither
a stdout nor a stderr line within this window after :meth:`Supervisor.spawn`
fans out the worker threads (t0 = ``process.started``, see Q-V099-5), the
supervisor emits a single ``process.note`` event with ``kind=stdout_silence``
plus a branched operator-facing hint:

- ``cursor`` + ``output_format=text`` (or unknown) → verbatim feedback wording
  from ``feedback_for_v0.9.7.md:33-34`` so ``popola attach --follow`` users
  immediately know the long silence is the cursor-agent stdout buffer, not
  a stuck task.
- ``cursor`` + ``output_format=stream-json`` → "first frame not yet emitted"
  hint per Q-V099-14, since stream-json is supposed to flush eagerly but
  large prompts can still defer the first frame for 60s+.
- Any other CLI (claude / codex / ...) → a generic stdout-silence note.

The fire-once timer is cancelled by:

- :meth:`_drain_stream` on the FIRST non-empty stdout / stderr line, AND
- :meth:`_wait_and_finalize` after :meth:`subprocess.Popen.wait` returns
  (so a fast-exiting task does not leak a delayed note after termination).

Tests monkeypatch this constant to a small value (≈ 0.05s) so the silence
path runs in milliseconds without sleeping a real 30s window."""


class Supervisor:
    """Spawns and monitors per-task subprocesses, streaming output to event log.

    Day-1 实现是纯 ``subprocess.Popen`` + ``threading``; 不引入 asyncio
    以避免与上层 popolad 的事件循环耦合 (上层 Stage Impl-3 的 LangGraph
    + uvicorn 是 asyncio, 我们只需保证 ``spawn()`` 立即返回, 不阻塞)。

    v0.4.1 Stage L1.A: optionally accepts a :class:`StateStore`
    reference at construction time so the wait-thread can emit
    ``task.canceled`` (instead of ``task.failed``) when
    :meth:`Popolad.cancel_task` has marked the handle as
    :attr:`TaskState.CANCELED` before sending SIGTERM. The reference
    is **stored as an instance attribute** rather than threaded
    through :meth:`spawn` so v0.3.x callers (and their test mocks
    of ``spawn``) keep their signature unchanged — workspace rule
    "No Silent Failures" honoured by an explicit log when the store
    lookup itself fails.
    """

    def __init__(self, state_store: StateStore | None = None) -> None:
        self._workers: dict[str, list[threading.Thread]] = {}
        self._lock = threading.Lock()
        self._line_counts: dict[str, dict[str, int]] = {}
        """Per-task stream line counters; updated atomically by drain threads.

        Shape: ``{task_id: {"stdout": int, "stderr": int}}``. Reads after
        ``proc.wait`` returns are safe-ish (drain thread is still running but
        the count only grows), and we use it for ``stream.truncated`` event
        ``actual_lines`` reporting on join timeout (R-007 fix).
        """
        self._silence_state: dict[
            str, tuple[threading.Event, threading.Timer]
        ] = {}
        """Per-task stdout-silence timer + cancel-event tuples (v0.9.9 F1).

        Shape: ``{task_id: (silence_event, silence_timer)}``. The
        :class:`threading.Event` is set by the cancel paths
        (:meth:`_drain_stream` first-line, :meth:`_wait_and_finalize`
        exit-before-fire) so the timer's emit callback can short-circuit
        on a race; the :class:`threading.Timer` is kept so the cancel
        paths can also call :meth:`threading.Timer.cancel` and stop the
        underlying timer thread cleanly. Entries are popped exactly once
        by :meth:`_cancel_silence_timer` (No Silent Failures — duplicate
        cancel calls are idempotent and never re-emit)."""
        self._state_store: StateStore | None = state_store

    @property
    def state_store(self) -> StateStore | None:
        """v0.4.1 Stage L1.A: optional :class:`StateStore` injected at __init__.

        Read by the wait-thread terminal-event emitter; ``None`` falls
        back to the v0.4.0 two-way ``task.completed``/``task.failed``
        path (preserving backward-compat for any caller that constructs
        :class:`Supervisor` without injecting the store).
        """
        return self._state_store

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
            v0.8.5: 云运行时 (``cmd`` 以 ``CLOUD_BUILD_COMMAND_MARKER`` 开头)
            返回 ``0`` 作为哨兵 — 表示未创建本地子进程、无 PID;
            存活由 ``runtime=cloud`` 与云轮询线程体现。
        """
        # v0.8.5: cloud-marker fast-path (zero subprocess for cloud-runtime tasks)
        if len(cmd) >= 3 and cmd[:2] == CLOUD_BUILD_COMMAND_MARKER:
            return self._spawn_cloud(
                task_id,
                cmd,
                cwd,
                env,
                event_log,
                on_exit,
            )

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
            args=(
                task_id,
                proc,
                event_log,
                stdout_thread,
                stderr_thread,
                on_exit,
                self._state_store,
            ),
            name=f"popolad-wait-{task_id}",
            daemon=True,
        )

        with self._lock:
            self._workers[task_id] = [stdout_thread, stderr_thread, wait_thread]
            self._line_counts[task_id] = {"stdout": 0, "stderr": 0}

        stdout_thread.start()
        stderr_thread.start()
        wait_thread.start()

        # v0.9.9 F1 (Q-V099-5 + Q-V099-14): register the stdout-silence
        # timer AFTER the worker threads start so a first-line cancel
        # race is structurally impossible (the timer can only fire once
        # the drain threads are already running and may have set the
        # cancel event). The timer is cancelled by `_drain_stream` on
        # the first non-empty stdout/stderr line and by
        # `_wait_and_finalize` after `proc.wait()` returns.
        self._register_silence_timer(
            task_id=task_id,
            cmd=cmd,
            event_log=event_log,
        )

        return pid

    def _spawn_cloud(
        self,
        task_id: str,
        cmd: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        event_log: EventLog,
        on_exit: Callable[[str, int], None] | None,
    ) -> int:
        """Cloud-runtime spawn: POST /v1/agents → start poller thread, return 0.

        No subprocess is created. Returns 0 as a sentinel meaning "no PID";
        callers that compare ``pid > 0`` for liveness see it as "not a live process",
        which is correct — the actual liveness is encoded in the cloud
        agent state machine, observed by the poller thread.

        Side effects:
        - BEFORE any other side effect: tags TaskHandle.runtime="cloud" so the
          task is observable as a cloud-runtime task even if subsequent steps
          (marker decode, missing api_key, create_agent failure) fail early.
        - Updates StateStore with cursor_agent_id / cursor_run_id / runtime=cloud /
          state=STARTING via the injected state_store.
        - Emits cloud.queued event.
        - Starts a daemon background thread running the cloud_poller loop.

        Failure modes (No Silent Failures):
        - Malformed marker JSON: emit task.failed with error_kind=marker_decode_error,
          call on_exit(task_id, 1), return 0.
        - CursorCloudError on create_agent: emit task.failed with full error info,
          call on_exit(task_id, 1), return 0.
        - Missing CURSOR_API_KEY: emit task.failed with error_kind=missing_api_key,
          call on_exit(task_id, 1), return 0.
        """
        from popolaloom.adapters.cursor_cloud import CloudCursorClient, CursorCloudError
        from popolaloom.daemon.cloud_poller import run_poll_loop
        from popolaloom.daemon.state import TaskState

        _ = env
        _ = cwd

        def _fail(
            *,
            error_kind: str,
            error_detail: str | None = None,
            err: CursorCloudError | None = None,
        ) -> int:
            payload: dict[str, Any] = {
                "task_id": task_id,
                "exit_code": 1,
                "runtime": "cloud",
                "agent_id": None,
                "run_id": None,
                "terminal_phase": None,
                "error_kind": error_kind,
            }
            if error_detail is not None:
                payload["error_detail"] = error_detail
            if err is not None:
                payload["error"] = {
                    "error_type": type(err).__name__,
                    "is_retryable": err.is_retryable,
                    "message": str(err),
                }
            logger.error(
                "cloud spawn failed task=%s kind=%s detail=%s",
                task_id,
                error_kind,
                error_detail or err,
            )
            event_log.append("task.failed", payload)
            if on_exit is not None:
                self._safe_on_exit(on_exit, task_id, 1)
            return 0

        if self._state_store is None:
            return _fail(
                error_kind="cloud_create_failed",
                error_detail="Supervisor requires state_store for cloud runtime",
            )

        self._state_store.update(task_id, runtime="cloud")

        try:
            payload = json.loads(cmd[2])
        except json.JSONDecodeError as exc:
            return _fail(
                error_kind="marker_decode_error",
                error_detail=f"invalid marker JSON: {exc}",
            )

        if not isinstance(payload, dict):
            return _fail(
                error_kind="marker_decode_error",
                error_detail="marker payload must be a JSON object",
            )

        prompt = payload.get("prompt")
        extra_raw = payload.get("extra")
        if extra_raw is not None and not isinstance(extra_raw, dict):
            return _fail(
                error_kind="marker_decode_error",
                error_detail="marker payload 'extra' must be object or null",
            )
        extra = extra_raw if isinstance(extra_raw, dict) else {}
        if not isinstance(prompt, str):
            return _fail(
                error_kind="marker_decode_error",
                error_detail="marker payload requires string 'prompt'",
            )

        # v0.9.2: route through the credential resolver so dispatch
        # honours OS keyring storage in addition to the historical
        # CURSOR_API_KEY env var. The resolver enforces precedence:
        # explicit override (the marker payload's `api_key` extra) >
        # CURSOR_API_KEY env > OS keyring. Returns None when nothing
        # is configured, which we surface via the existing
        # error_kind="missing_api_key" failure (No Silent Failures —
        # the operator hint in the resulting failure event lists all
        # three precedence slots).
        from popolaloom.credentials import resolve_cursor_api_key

        raw_override = extra.get("api_key")
        override: str | None = None
        if raw_override is not None and str(raw_override).strip():
            override = str(raw_override).strip()
        api_key = resolve_cursor_api_key(override=override) or ""
        if not api_key:
            return _fail(error_kind="missing_api_key")

        model = str(extra.get("model", "composer-2"))
        repo_url = extra.get("repo_url")
        if repo_url is not None and not isinstance(repo_url, str):
            return _fail(
                error_kind="marker_decode_error",
                error_detail="extra.repo_url must be str when present",
            )
        pr_url = extra.get("pr_url")
        if pr_url is not None and not isinstance(pr_url, str):
            return _fail(
                error_kind="marker_decode_error",
                error_detail="extra.pr_url must be str when present",
            )

        env_vars_param: dict[str, str] | None = None
        if "env_vars" in extra:
            ev = extra.get("env_vars")
            if ev is None:
                env_vars_param = None
            elif not isinstance(ev, dict):
                return _fail(
                    error_kind="marker_decode_error",
                    error_detail="extra.env_vars must be object or null",
                )
            elif not all(
                isinstance(k, str) and isinstance(v, str) for k, v in ev.items()
            ):
                return _fail(
                    error_kind="marker_decode_error",
                    error_detail="extra.env_vars must be dict[str, str]",
                )
            else:
                env_vars_param = dict(ev)

        use_private_worker_param = extra.get("use_private_worker", False)
        if not isinstance(use_private_worker_param, bool):
            return _fail(
                error_kind="marker_decode_error",
                error_detail="extra.use_private_worker must be bool",
            )

        labels_param: dict[str, str] | None = None
        if "labels" in extra:
            labels_raw = extra.get("labels")
            if labels_raw is None:
                labels_param = None
            elif not isinstance(labels_raw, dict):
                return _fail(
                    error_kind="marker_decode_error",
                    error_detail="extra.labels must be object or null",
                )
            elif not all(
                isinstance(k, str) and isinstance(v, str) for k, v in labels_raw.items()
            ):
                return _fail(
                    error_kind="marker_decode_error",
                    error_detail="extra.labels must be dict[str, str]",
                )
            else:
                labels_param = dict(labels_raw)

        timeout_s_param: float | None = None
        if extra.get("timeout_s") is not None:
            try:
                timeout_s_param = float(extra["timeout_s"])
            except (TypeError, ValueError):
                return _fail(
                    error_kind="marker_decode_error",
                    error_detail="extra.timeout_s must be int or float",
                )

        client: CloudCursorClient | None = None
        try:
            client = CloudCursorClient(api_key)
            resp = client.create_agent(
                prompt,
                model,
                repo_url,
                starting_ref=str(extra.get("starting_ref", "main")),
                auto_create_pr=bool(extra.get("auto_create_pr", False)),
                work_on_current_branch=bool(extra.get("work_on_current_branch", False)),
                skip_reviewer_request=bool(extra.get("skip_reviewer_request", False)),
                pr_url=pr_url,
                env_vars=env_vars_param,
                use_private_worker=use_private_worker_param,
                labels=labels_param,
                timeout_s=timeout_s_param,
            )
        except ValueError as exc:
            if client is not None:
                client.close()
            return _fail(
                error_kind="cloud_create_failed",
                error_detail=str(exc),
            )
        except CursorCloudError as exc:
            if client is not None:
                client.close()
            payload_out: dict[str, Any] = {
                "task_id": task_id,
                "exit_code": 1,
                "runtime": "cloud",
                "agent_id": None,
                "run_id": None,
                "terminal_phase": None,
                "error_kind": "cloud_create_failed",
                "error": {
                    "error_type": type(exc).__name__,
                    "is_retryable": exc.is_retryable,
                    "message": str(exc),
                },
            }
            logger.error("cursor-cloud create_agent failed task=%s: %s", task_id, exc)
            event_log.append("task.failed", payload_out)
            if on_exit is not None:
                self._safe_on_exit(on_exit, task_id, 1)
            return 0

        agent_id = (resp.get("agent") or {}).get("id")
        run_id = (resp.get("run") or {}).get("id")
        if not agent_id or not run_id:
            if client is not None:
                client.close()
            return _fail(
                error_kind="cloud_create_failed",
                error_detail="create_agent response missing agent.id or run.id",
            )

        # I-1 sole-writer (state-source-of-truth.md §1.2 rule 1): only
        # cloud_poller.py may pass cloud_phase=/state= to state_store.update.
        # Seed the bootstrap STARTING/CREATING snapshot via the TaskHandle
        # constructor (dataclasses.replace re-invokes TaskHandle.__init__ with
        # the merged fields) and re-register through StateStore.rehydrate —
        # the documented authoritative-overwrite path. Bootstrap semantics are
        # preserved end-to-end: by the time `cloud.queued` is emitted the
        # handle observed by `popola status` already shows
        # state=STARTING / cloud_phase=CREATING, identical to the v0.8.5
        # update-based seed it replaces.
        existing_handle = self._state_store.get(task_id)
        if existing_handle is None:
            if client is not None:
                client.close()
            return _fail(
                error_kind="cloud_create_failed",
                error_detail=(
                    f"task_id {task_id} missing from state_store before cloud "
                    "seed (Popolad pre-register contract violated)"
                ),
            )
        seeded_handle = dataclasses.replace(
            existing_handle,
            state=TaskState.STARTING,
            runtime="cloud",
            cursor_agent_id=agent_id,
            cursor_run_id=run_id,
            cloud_phase="CREATING",
        )
        self._state_store.rehydrate([seeded_handle])
        event_log.append(
            "cloud.queued",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "runtime": "cloud",
                "initial_phase": "CREATING",
            },
        )
        logger.info(
            "cloud task queued task=%s agent=%s run=%s",
            task_id,
            agent_id,
            run_id,
        )

        poll_thread = run_poll_loop(
            task_id,
            agent_id,
            run_id,
            client=client,
            state_store=self._state_store,
            event_log=event_log,
            on_exit=on_exit,
        )
        with self._lock:
            self._workers[task_id] = [poll_thread]
        return 0

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

        v0.9.9 F1: the FIRST non-empty line on either stream cancels the
        stdout-silence timer registered by :meth:`spawn`; this is why
        the cancellation hook lives in the drain loop rather than at
        ``proc.wait`` time (a chatty subprocess that prints once at t=0
        must not trip the silence note even if it then runs for hours).
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
                if line:
                    self._cancel_silence_timer(task_id)
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
        state_store: StateStore | None = None,
    ) -> None:
        """阻塞等子进程退出, 然后写终态事件 + 调 on_exit 回调.

        v0.2.0 R-007 fix: drain join timeout = ``_DRAIN_JOIN_TIMEOUT_S`` (30s),
        超时则在终态事件之前 emit ``stream.truncated`` event 含
        ``{stream, actual_lines, reason: 'join_timeout_30s'}``.

        v0.4.1 Stage L1.A: 终态事件三元化 — 若 ``state_store`` 注入且对应
        :class:`TaskHandle` 已被 :meth:`Popolad.cancel_task` 标记为
        :attr:`TaskState.CANCELED`, 改 emit ``task.canceled`` 含
        ``{task_id, exit_code, pid, sigkill_escalated: bool}`` 字段; 否则
        保持 v0.4.0 的 ``task.completed`` (exit_code == 0) /
        ``task.failed`` (else) 二元路径。
        """
        try:
            exit_code = proc.wait()
        except Exception as exc:  # noqa: BLE001
            logger.exception("proc.wait failed for task %s", task_id)
            # v0.9.9 F1: even on a surprise wait failure, cancel the
            # silence timer first so we don't fire a misleading note
            # after the supervisor has already given up on the child.
            self._cancel_silence_timer(task_id)
            terminal_type, terminal_data = self._resolve_terminal_event(
                task_id=task_id,
                pid=proc.pid,
                exit_code=-1,
                state_store=state_store,
            )
            terminal_data["error"] = repr(exc)
            event_log.append(terminal_type, terminal_data)
            if on_exit is not None:
                self._safe_on_exit(on_exit, task_id, -1)
            return

        # v0.9.9 F1: clean exit-before-fire path. proc.wait returned, so
        # any silence timer still pending would (a) be racy with the
        # imminent terminal event emit below and (b) fire a misleading
        # "still working" hint after the task has already finished.
        # Cancellation is idempotent with the per-task state dict, so
        # if `_drain_stream` already cancelled (chatty subprocess) this
        # is a no-op.
        self._cancel_silence_timer(task_id)

        stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)
        if stdout_thread.is_alive():
            self._emit_stream_truncated(task_id, "stdout", event_log)
        stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)
        if stderr_thread.is_alive():
            self._emit_stream_truncated(task_id, "stderr", event_log)

        terminal_type, terminal_data = self._resolve_terminal_event(
            task_id=task_id,
            pid=proc.pid,
            exit_code=exit_code,
            state_store=state_store,
        )
        event_log.append(terminal_type, terminal_data)

        if on_exit is not None:
            self._safe_on_exit(on_exit, task_id, exit_code)

    @staticmethod
    def _resolve_terminal_event(
        *,
        task_id: str,
        pid: int,
        exit_code: int,
        state_store: StateStore | None,
    ) -> tuple[str, dict[str, Any]]:
        """Decide the terminal event type + payload for a single subprocess exit.

        v0.4.1 Stage L1.A: returns ``("task.canceled", {sigkill_escalated})``
        when the :class:`StateStore` says the handle is already
        :attr:`TaskState.CANCELED` (set by :meth:`Popolad.cancel_task`
        *before* it sent SIGTERM/SIGKILL); otherwise falls back to v0.4.0
        ``task.completed`` / ``task.failed`` two-way split.

        StateStore lookup failures are logged at WARNING and downgraded
        to the legacy two-way path (No Silent Failures workspace rule —
        we record the failure rather than inventing a verdict).
        """
        canceled_payload = Supervisor._maybe_canceled_terminal(
            task_id=task_id,
            pid=pid,
            exit_code=exit_code,
            state_store=state_store,
        )
        if canceled_payload is not None:
            return canceled_payload

        event_type = "task.completed" if exit_code == 0 else "task.failed"
        return (
            event_type,
            {"task_id": task_id, "exit_code": exit_code, "pid": pid},
        )

    @staticmethod
    def _maybe_canceled_terminal(
        *,
        task_id: str,
        pid: int,
        exit_code: int,
        state_store: StateStore | None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Return ``("task.canceled", payload)`` iff state_store says CANCELED.

        Helper extracted from :meth:`_resolve_terminal_event` so the
        deferred ``TaskState`` import lives in one place; returns
        ``None`` when no override applies (caller falls back to the
        legacy two-way emit).
        """
        if state_store is None:
            return None
        try:
            from popolaloom.daemon.state import TaskState

            handle = state_store.get(task_id)
        except Exception:
            logger.exception(
                "state_store.get failed for task=%s; falling back to legacy "
                "task.completed/task.failed terminal event",
                task_id,
            )
            return None
        if handle is None or handle.state != TaskState.CANCELED:
            return None
        return (
            "task.canceled",
            {
                "task_id": task_id,
                "exit_code": exit_code,
                "pid": pid,
                "sigkill_escalated": bool(
                    handle.cancel_escalated_to_sigkill
                ),
            },
        )

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

    def _register_silence_timer(
        self,
        *,
        task_id: str,
        cmd: list[str],
        event_log: EventLog,
    ) -> None:
        """Arm the v0.9.9 F1 stdout-silence timer for ``task_id``.

        Reads :data:`_SILENCE_TIMEOUT_SECS` at registration time so
        tests can monkeypatch the constant before calling
        :meth:`spawn` and run the silence path in milliseconds. The
        timer thread is daemonised so it never blocks interpreter exit.
        """
        silence_event = threading.Event()
        timer = threading.Timer(
            _SILENCE_TIMEOUT_SECS,
            self._emit_silence_note,
            kwargs={
                "task_id": task_id,
                "cmd": list(cmd),
                "silence_event": silence_event,
                "event_log": event_log,
                "elapsed_seconds": _SILENCE_TIMEOUT_SECS,
            },
        )
        timer.daemon = True
        with self._lock:
            self._silence_state[task_id] = (silence_event, timer)
        timer.start()

    def _cancel_silence_timer(self, task_id: str) -> None:
        """Cancel + clear the F1 silence timer for ``task_id`` (idempotent).

        Safe to call from any thread (drain, wait, or the test harness).
        Pops the per-task entry so subsequent calls are pure no-ops; the
        underlying :class:`threading.Timer` is also cancelled to release
        its scheduling thread immediately rather than waiting for the
        natural timeout.
        """
        with self._lock:
            state = self._silence_state.pop(task_id, None)
        if state is None:
            return
        silence_event, timer = state
        silence_event.set()
        timer.cancel()

    def _emit_silence_note(
        self,
        *,
        task_id: str,
        cmd: list[str],
        silence_event: threading.Event,
        event_log: EventLog,
        elapsed_seconds: float,
    ) -> None:
        """Timer callback — emit ``process.note`` unless cancelled in flight.

        Re-checks the cancel event under the per-supervisor lock to win
        the race against a drain-thread first-line cancel that arrived
        microseconds before the timer fired (Q-V099-14: at most one
        ``process.note`` per task lifecycle).
        """
        if silence_event.is_set():
            return
        with self._lock:
            state = self._silence_state.pop(task_id, None)
        if state is None:
            return
        silence_event.set()

        cli_name = _detect_cli_name_from_cmd(cmd)
        output_format = (
            _detect_cursor_output_format_from_cmd(cmd)
            if cli_name == "cursor"
            else None
        )
        hint = _silence_hint_for(cli_name, output_format)
        event_log.append(
            "process.note",
            {
                "task_id": task_id,
                "kind": "stdout_silence",
                "elapsed_seconds": elapsed_seconds,
                "hint": hint,
            },
        )

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


_CURSOR_TEXT_HINT: str = (
    "cursor-agent 'text' output is buffered until exit; "
    "pass --cli-flag output_format=stream-json for live progress"
)
"""v0.9.9 F1 / Q-V099-14 text-mode hint.

Verbatim from ``feedback_for_v0.9.7.md:33-34`` so the operator-facing
``process.note`` payload matches exactly the wording the user proposed.
Module-level constant so tests can assert by reference rather than by
copy-paste-prone string literal."""


_CURSOR_STREAM_JSON_HINT: str = (
    "cursor-agent is working; first stream-json frame not yet emitted "
    "(this can take 60s+ for large prompts)"
)
"""v0.9.9 F1 / Q-V099-14 stream-json hint.

Stream-json is supposed to flush eagerly, but large prompts can defer
the first frame past the 30s silence threshold; the alternative wording
makes it clear that the silence does NOT mean the buffered-text bug —
it just means no frame yet."""


def _detect_cli_name_from_cmd(cmd: list[str]) -> str | None:
    """Map ``cmd[0]`` basename to the popola adapter ``cli`` name.

    Used by the F1 silence-timer to pick the branched hint without
    plumbing ``cli`` through :meth:`Supervisor.spawn` (the existing
    callers in :mod:`popolaloom.daemon.server` do not currently pass
    a ``cli`` keyword, and this owned-files patch keeps them out of
    scope). The mapping is intentionally narrow: only ``cursor-agent``
    is special-cased because only the cursor adapter has the buffered-
    text quirk that motivated F1; every other adapter falls through
    to the generic note via :func:`_silence_hint_for`.

    Returns:
        ``"cursor"`` when ``cmd[0]`` (or its basename) is
        ``cursor-agent``; the basename string for any other CLI; or
        ``None`` when the command list is empty (defensive — callers
        always pass a non-empty argv but the type allows the case).
    """
    if not cmd:
        return None
    binary = os.path.basename(cmd[0])
    if binary == "cursor-agent":
        return "cursor"
    return binary or None


def _detect_cursor_output_format_from_cmd(cmd: list[str]) -> str | None:
    """Extract ``--output-format <fmt>`` from a cursor-agent argv (or ``None``).

    The cursor adapter always emits ``--output-format <fmt>`` (see
    :class:`popolaloom.adapters.cursor.CursorAdapter.build_command`);
    a missing / malformed flag is treated as "unknown" so the F1
    branched-hint logic falls back to the text-mode wording, matching
    the adapter's own default of ``output_format="text"``.
    """
    try:
        idx = cmd.index("--output-format")
    except ValueError:
        return None
    if idx + 1 >= len(cmd):
        return None
    return cmd[idx + 1]


def _silence_hint_for(cli_name: str | None, output_format: str | None) -> str:
    """Pick the F1 branched silence-hint per Q-V099-14.

    - cursor + ``stream-json`` → "first frame not yet emitted" wording.
    - cursor + anything else (or missing) → verbatim feedback wording.
    - any other CLI (or unknown) → generic stdout-silence note.

    Returns the operator-facing hint string used as the
    ``process.note`` event's ``data.hint`` field.
    """
    if cli_name == "cursor":
        if output_format == "stream-json":
            return _CURSOR_STREAM_JSON_HINT
        return _CURSOR_TEXT_HINT
    label = cli_name if cli_name else "process"
    return (
        f"{label} stdout has been silent for 30s; "
        "this can be normal for long-running tasks"
    )
