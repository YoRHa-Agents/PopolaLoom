"""Popolad facade — top-level entry exposing dispatch / status / tail.

v0.2.0 (Stage A) 这是一个**纯 Python 类**, 不再注册任何模块级 singleton
(R-013 修复: 删除 ``_default_popolad`` 与配套 ``dispatch_task`` /
``get_status`` / ``tail_events`` 模块级 wrapper)。

进程模型职责拆分 (v0.2.0):

- :class:`Popolad`: pure orchestration class, 复用为 RPC handler。
- :mod:`popolaloom.daemon.rpc`: FastAPI app factory + 模块级 Popolad
  singleton (运行期 daemon 进程内唯一实例)。
- :mod:`popolaloom.daemon.main`: ``python -m popolaloom.daemon`` 进程
  入口 — uvicorn UDS server + signal handlers + PID file。

v0.2.0 patches inline (Stage A A6):

- **R-006**: ``self._event_logs`` dict 现在用 ``self._event_logs_lock``
  保护读写, 与 ``StateStore._lock`` / ``EventLog._lock`` 严格对称。
- **R-008**: ``_on_subprocess_exit`` KeyError 路径 emit
  ``state.ghost_exit`` event (No Silent Failures); ``_maybe_create_arktower_task``
  失败时返回 ``None`` 并保持 ``TaskHandle.persisted=False``。
- **R-013** (part): 删除 module-level ``_default_popolad`` 单例和配套
  wrapper; rpc.py 持有 daemon-process-level 单例。
- **R-014** (part): 提取 :meth:`Popolad._task_summary` 统一
  ``list_active`` / ``get_status`` 返回 shape, 让 CLI 不再有"shape 不一致"
  的小坑。

v0.2.0 Stage B (R-003 closing):

- :meth:`dispatch_task` now defaults to **routing through LangGraph**
  (``POPOLA_USE_GRAPH`` env var, default ``"1"``). The graph wraps the
  same dispatch → spawn → wait → emit_terminal flow and adds
  ``graph.step`` events at each node so consumers can see plan-level
  progress in addition to the existing subprocess events.
- Setting ``POPOLA_USE_GRAPH=0`` keeps the legacy direct-Supervisor
  path (kept for Stage C bootstrap; can be removed once C ship-tests
  pass with the graph path on).

v0.2.0 Stage C (R-004 closing — ArkTower真接入):

- :meth:`__init__` accepts an optional :class:`TaskPersistence` (4-tuple
  of ``task_service`` / ``repository`` / ``connection`` / ``event_bus``)
  injected from :func:`popolaloom.daemon.repository.make_persistence`.
- :meth:`_maybe_create_arktower_task` now prefers the real async
  :meth:`TaskService.create_task` path when ``persistence`` is provided,
  bridged to the sync dispatch thread via :func:`asyncio.run`.  Falls
  back to the Stage A in-memory ``ArkTask`` model when ``persistence``
  is ``None`` (test mode) so existing tests asserting
  ``status["arktower_task_id"] is not None`` keep passing.
- :class:`PopolaEventBusBridge` may be injected to translate ArkTower
  ``TASK_TRANSITION_EVENT`` notifications into ``task.transition``
  NDJSON events on the same per-task event log.
- New :meth:`rehydrate_from_persistence` recovers in-flight tasks from
  ArkTower SQLite at daemon startup, hydrating
  :attr:`StateStore` with handles whose ``state == TaskState.RUNNING``.
- New :meth:`shutdown_persistence_bridge` (called from rpc.py lifespan
  finally) unsubscribes the bridge and closes the SQLite connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from popolaloom.adapters.cursor_cloud import (
    CloudCursorClient,
    CursorCloudConflictError,
    CursorCloudError,
)
from popolaloom.daemon.event_log import EventLog
from popolaloom.daemon.graph import GraphCallbacks, build_main_graph
from popolaloom.daemon.graph import TaskState as GraphTaskState
from popolaloom.daemon.state import StateStore, TaskHandle, TaskState
from popolaloom.daemon.supervisor import Supervisor
from popolaloom.handoff import (
    HandoffEnvelope,
    generate_handoff_id,
    write_envelope,
)

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from popolaloom.daemon.event_bus import PopolaEventBusBridge
    from popolaloom.daemon.repository import TaskPersistence

    _Saver = BaseCheckpointSaver[Any]
    _Compiled = CompiledStateGraph[Any, Any, Any, Any]

logger = logging.getLogger(__name__)


AdapterCallback = Callable[[str, str, Path | None, dict[str, Any] | None], list[str]]
"""Adapter callback signature — v0.2.0 Stage E unified 4-arg form (R-009 closure).

Canonical signature is ``(cli, prompt, cwd, extra) -> argv``. The
:meth:`Popolad._call_adapter` shim still tolerates legacy 3-arg
callbacks (``(cli, prompt, cwd) -> argv``) by catching ``TypeError`` so
v0.0.1 fakes in test fixtures do not break — but the *type alias* is
strict 4-arg per AC #8 of v0.2.0 Stage E.
"""


def _resolve_handoff_path(env: dict[str, str] | None) -> str | None:
    """Resolve the active handoff envelope path for v0.7.2 C5 flag-injection.

    Order of precedence:

    1. ``env["POPOLA_HANDOFF_FILE"]`` if set (this is what
       :meth:`Popolad.dispatch_with_envelope` injects into the spawn env).
    2. ``os.environ["POPOLA_HANDOFF_FILE"]`` if the caller didn't pass an
       explicit env dict (legacy ``dispatch_task`` paths that don't
       construct a merged env).
    3. ``None`` — no flag will be injected.

    The flag injection itself is gated on ``extra["popola_handoff_flag"]``
    in :meth:`Popolad._call_adapter`; this helper only resolves WHERE the
    file lives so the flag value is correct when injection fires.
    """
    if env is not None:
        path = env.get("POPOLA_HANDOFF_FILE")
        if path:
            return path
    return os.environ.get("POPOLA_HANDOFF_FILE")


def _default_events_dir() -> Path:
    """``~/.popola/events/`` per spec §10 canonical paths."""
    return Path.home() / ".popola" / "events"


def _redact_cmd_for_persistence(cmd: list[str]) -> list[str]:
    """Return a copy of ``cmd`` safe to persist / log / notify with.

    v0.9.2: when ``cmd`` is a cursor-cloud sentinel argv (the JSON
    marker payload from
    :meth:`popolaloom.adapters.cursor_cloud.CursorCloudAdapter.build_command`),
    the embedded ``extra.api_key`` value is redacted to a placeholder.
    Non-cloud cmds (vanilla ``cursor-agent`` / ``claude`` / ``codex``
    argv) pass through unchanged.

    Used at three persistence boundaries: :class:`TaskHandle.cmd`, the
    NDJSON ``task.dispatched`` event payload, and the ArkTower SQLite
    row. The unredacted cmd remains in-memory only long enough to reach
    :meth:`Supervisor.spawn`, which extracts ``api_key`` from the
    payload (or, more commonly, falls through to the credential
    resolver in :mod:`popolaloom.credentials`).
    """
    from popolaloom.adapters.cursor_cloud import redact_cloud_marker_cmd

    return redact_cloud_marker_cmd(cmd)


class Popolad:
    """Minimal popolad daemon facade — dispatch / status / tail / list.

    Args:
        events_dir: 事件文件根目录 (每个 task 一个 ``<task_id>.jsonl``)。
            默认 ``~/.popola/events/``; 测试传 ``tmp_path`` 隔离。
        adapter: 可选的命令构造回调 ``(cli, prompt, cwd, extra=None) -> [argv...]``。
            未提供时 dispatch_task 会 raise ``RuntimeError``, 而非静默失败。
        task_repository: 可选 :class:`arktower.store.repository.TaskRepository`
            实现 — **Stage A 兼容形态**, 保留以避免破坏既有测试; Stage C
            起优先用 ``persistence`` 注入完整三件套 (``task_service`` /
            ``repository`` / ``connection`` / ``event_bus``)。
        persistence: v0.2.0 Stage C 注入的 :class:`TaskPersistence`
            (ArkTower 真持久化栈)。``None`` 时 ``_maybe_create_arktower_task``
            走 Stage A 的 in-memory ArkTask fallback (保持
            ``arktower_task_id`` 非 None 以满足既有测试)。
        event_bus_bridge: v0.2.0 Stage C 注入的
            :class:`PopolaEventBusBridge`; 若提供, ``__init__`` 自动调
            ``subscribe()``, daemon 关闭时 :meth:`shutdown_persistence_bridge`
            会调 ``unsubscribe()``。
        checkpointer: 可选 :class:`langgraph.checkpoint.base.BaseCheckpointSaver`
            实现 (Stage B); ``None`` 时使用 in-memory 的 ``MemorySaver``。
            默认行为: 如未提供且 ``POPOLA_USE_GRAPH != "0"``, 在第一次
            graph dispatch 时**lazily**通过
            :func:`popolaloom.daemon.checkpoint.make_checkpointer` 在
            ``~/.popola/state.sqlite`` 上构造一个共享 saver。
        use_graph: 显式开关 LangGraph 路径; ``None`` 表示读
            ``POPOLA_USE_GRAPH`` 环境变量 (默认 ``"1"`` 即开)。Stage B 起
            production daemon 始终走 graph 路径; Stage C 可显式 ``False``
            做 bootstrap 调试。
        cloud_client: v0.8.5 可选 :class:`~popolaloom.adapters.cursor_cloud.CloudCursorClient`
            （测试注入）；``None`` 时在首次云取消时按 ``CURSOR_API_KEY`` 惰性构造。
    """

    def __init__(
        self,
        events_dir: Path | None = None,
        adapter: AdapterCallback | None = None,
        task_repository: Any = None,
        persistence: TaskPersistence | None = None,
        event_bus_bridge: PopolaEventBusBridge | None = None,
        checkpointer: _Saver | None = None,
        use_graph: bool | None = None,
        cloud_client: CloudCursorClient | None = None,
    ) -> None:
        self._events_dir = Path(events_dir) if events_dir is not None else _default_events_dir()
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._adapter = adapter
        self._task_repository = task_repository
        self._persistence = persistence
        self._event_bus_bridge = event_bus_bridge
        self._cloud_client: CloudCursorClient | None = cloud_client
        self._state = StateStore()
        self._supervisor = Supervisor(state_store=self._state)
        self._event_logs: dict[str, EventLog] = {}
        self._event_logs_lock = threading.Lock()
        if use_graph is None:
            self._use_graph = os.environ.get("POPOLA_USE_GRAPH", "1") != "0"
        else:
            self._use_graph = use_graph
        self._checkpointer: _Saver | None = checkpointer
        self._checkpointer_lock = threading.Lock()
        # v0.3.0 F4.C: optional HITL store (set by daemon main; tests
        # may inject directly).  When None, /hitl/answer returns 503 so
        # callers know HITL is not wired up rather than failing silently.
        self._hitl_store: Any = None
        # v0.4.1 Stage L2.B/L2.C: optional asyncio loop reference, used by
        # ``_on_subprocess_exit`` to schedule the Lark proactive
        # notifier coroutine from the supervisor wait-thread; and a slot
        # for the LarkSupervisor instance the daemon owns when env vars
        # opt in. Both default ``None`` so unit tests that construct
        # :class:`Popolad` outside an asyncio context (no running loop)
        # still work — the notifier scheduler simply skips with an
        # explicit log line per workspace rule "No Silent Failures".
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._lark_supervisor: Any = None
        if event_bus_bridge is not None:
            event_bus_bridge.subscribe()

    @property
    def events_dir(self) -> Path:
        """事件根目录 (供测试 / 外部消费者读取)."""
        return self._events_dir

    @property
    def state_store(self) -> StateStore:
        """暴露 StateStore (主要给测试 / TUI 直接 introspect)."""
        return self._state

    @property
    def supervisor(self) -> Supervisor:
        """暴露 Supervisor (主要给 RPC ``cancel`` 端点 lookup pid)."""
        return self._supervisor

    @property
    def persistence(self) -> TaskPersistence | None:
        """Stage C: injected ArkTower task pool (``None`` in test mode)."""
        return self._persistence

    @property
    def event_bus_bridge(self) -> PopolaEventBusBridge | None:
        """Stage C: injected ArkTower → NDJSON bridge (``None`` if not wired)."""
        return self._event_bus_bridge

    @property
    def hitl_store(self) -> Any:
        """v0.3.0 F4.C: optional :class:`popolaloom.hitl.HITLStore` instance.

        ``None`` when HITL persistence is not wired (RPC ``/hitl/answer``
        replies 503 in that case).
        """
        return self._hitl_store

    @hitl_store.setter
    def hitl_store(self, value: Any) -> None:
        """Inject a :class:`popolaloom.hitl.HITLStore` (used by daemon main + tests)."""
        self._hitl_store = value

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the daemon's main asyncio loop for cross-thread scheduling.

        v0.4.1 Stage L2.B: :meth:`_on_subprocess_exit` runs in the
        supervisor wait-thread (NOT on the asyncio loop), so the Lark
        proactive notifier coroutine has to be scheduled via
        :func:`asyncio.run_coroutine_threadsafe`. The daemon's
        :func:`popolaloom.daemon.main.main` (and the rpc.py lifespan
        for daemon-process construction) calls this once at startup
        when the loop is known. Tests may also call it manually.

        Idempotent: subsequent calls overwrite the stored reference
        (matches v0.3.0 ``hitl_store`` setter semantics — last writer
        wins, no race protection because lifespan runs once).
        """
        self._loop = loop

    @property
    def lark_supervisor(self) -> Any:
        """v0.4.1 Stage L2.C: the daemon-owned :class:`LarkSupervisor` (or ``None``).

        Set by :func:`popolaloom.daemon.main._build_default_popolad`
        when ``LARK_HITL_TARGET_OPEN_ID`` (or ``LARK_NOTIFY_TARGET_OPEN_ID``)
        is configured AND ``lark-cli`` is on PATH; otherwise stays
        ``None``. Tests may inject a stub directly via the underlying
        attribute for verification.
        """
        return self._lark_supervisor

    def event_log_for_arktower_id(self, ark_task_id: str) -> EventLog | None:
        """Resolve an ArkTower task id → :class:`EventLog` for the popola task.

        Used by :class:`PopolaEventBusBridge` to route ``TASK_TRANSITION``
        events to the right NDJSON file.  Walks
        :meth:`StateStore.list_all` looking for
        ``handle.arktower_task_id == ark_task_id`` (O(N) over in-flight
        tasks; v0.2.0 has ≤ ~10 simultaneously, so negligible).

        Returns:
            EventLog | None: the bound event log, or ``None`` when no
            popola task tracks the given ArkTower id (which is a normal
            case for tasks created outside popolad — e.g. directly via
            ``arktower task add``).
        """
        for handle in self._state.list_all():
            if handle.arktower_task_id == ark_task_id:
                return self.event_log(handle.task_id)
        return None

    def dispatch_with_envelope(
        self,
        envelope: HandoffEnvelope,
        *,
        adapter: AdapterCallback | None = None,
        base_env: dict[str, str] | None = None,
        handoff_root: Path | str | None = None,
    ) -> str:
        """Canonical dispatch entry — v0.8.0 hands-off envelope (E3 internal unification).

        New in v0.7.2 (per ``feedback_for_v0.8.0.md`` item #1, user-decided
        Q5=E3): the file-backed envelope is the single source of truth for
        dispatch payload. This method:

        1. Writes the envelope to ``<handoff_root>/<handoff_id>.md`` (atomic via
           :func:`popolaloom.handoff.write_envelope`; ``handoff_root`` resolved as
           explicit arg → ``$POPOLA_HANDOFF_DIR`` env → ``DEFAULT_HANDOFF_ROOT``
           = ``.local/.agent/handoff``).
        2. Builds a env overlay containing ``POPOLA_HANDOFF_FILE`` (absolute
           path) + ``POPOLA_HANDOFF_ID`` (slug-hash). The overlay always takes
           precedence over caller-provided ``base_env`` to prevent
           handoff-impersonation.
        3. Delegates to the existing :meth:`_dispatch_via_graph` /
           :meth:`_dispatch_legacy` with the merged env, target_cli, prompt,
           cwd, and adapter_extra all sourced from the envelope.

        :meth:`dispatch_task` is now a thin wrapper that builds an envelope
        from kwargs and calls this method (E3 internal unification — there is
        only one canonical dispatch path internally; the kwargs surface is
        preserved for backward compat with rpc.py / cli/main.py / tests).

        Args:
            envelope: validated :class:`HandoffEnvelope` (Pydantic v2). Caller
                is responsible for building it; if you have a raw prompt /
                cli pair, use :meth:`dispatch_task` instead which builds the
                envelope for you.
            adapter: optional adapter callback override; defaults to
                ``self._adapter`` injected at __init__.
            base_env: optional base environment dict (typically a caller-
                provided sandbox env); merged with the handoff overlay so
                the child process sees both. ``None`` means the daemon's
                ``os.environ`` is the base (typical case).
            handoff_root: optional override for the active root directory.
                ``None`` means resolve from ``$POPOLA_HANDOFF_DIR`` env var,
                falling back to :data:`popolaloom.handoff.DEFAULT_HANDOFF_ROOT`.

        Returns:
            str: ``task_id`` (popola internal id, distinct from
            ``envelope.handoff_id``; the two are linked via
            ``extra["_handoff_envelope_path"]`` + the env vars).

        Raises:
            TypeError: if ``envelope`` is not a :class:`HandoffEnvelope`.
            RuntimeError: if no adapter is available (same as
                :meth:`dispatch_task`).
            ValueError: if adapter returns invalid cmd (same).
            OSError: if envelope file cannot be written (re-raised, No
                Silent Failures).
        """
        if not isinstance(envelope, HandoffEnvelope):
            raise TypeError(
                f"dispatch_with_envelope expects HandoffEnvelope, "
                f"got {type(envelope).__name__}"
            )

        # Resolve handoff dir: explicit arg > $POPOLA_HANDOFF_DIR > default
        if handoff_root is None:
            env_dir = os.environ.get("POPOLA_HANDOFF_DIR")
            if env_dir:
                handoff_root = env_dir
        env_path = write_envelope(envelope, base_dir=handoff_root)

        # Build env overlay; handoff vars always take precedence to avoid
        # caller-provided POPOLA_HANDOFF_* keys impersonating the dispatch.
        overlay = {
            "POPOLA_HANDOFF_FILE": str(env_path.resolve()),
            "POPOLA_HANDOFF_ID": envelope.handoff_id,
        }
        merged_env = (
            {**os.environ, **overlay}
            if base_env is None
            else {**base_env, **overlay}
        )

        cwd = Path(envelope.cwd) if envelope.cwd else None
        # Adapter-extra is the user-controlled passthrough; envelope-internal
        # bookkeeping (path, id) flows via env vars (POPOLA_HANDOFF_FILE/ID)
        # so adapters / supervisors don't need to peek at the extra dict shape.
        # Pass None when empty so 3-arg legacy adapter fallback still triggers
        # in _call_adapter (test fixtures from v0.0.1 still rely on it).
        extra: dict[str, Any] | None = (
            dict(envelope.adapter_extra) if envelope.adapter_extra else None
        )

        if self._use_graph:
            return self._dispatch_via_graph(
                envelope.target_cli,
                envelope.prompt,
                cwd,
                merged_env,
                adapter,
                extra,
            )
        return self._dispatch_legacy(
            envelope.target_cli,
            envelope.prompt,
            cwd,
            merged_env,
            adapter,
            extra,
        )

    def dispatch_task(
        self,
        cli: str,
        prompt: str,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        adapter: AdapterCallback | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Dispatch a new task and return its ``task_id``.

        v0.7.2 (Stage v0.8.0 patch 2, E3 internal unification): this method
        is now a thin wrapper that builds a :class:`HandoffEnvelope` from the
        kwargs and delegates to :meth:`dispatch_with_envelope`. The public
        signature is preserved unchanged so rpc.py / cli/main.py / tests
        keep working without modification.

        For new code prefer :meth:`dispatch_with_envelope` directly — it
        gives you control over the full envelope (including audit fields
        like ``reason`` / ``tags`` that the kwargs surface does not expose).

        v0.2.0 Stage B: routes through LangGraph by default
        (``self._use_graph=True``); legacy direct path is kept behind the
        flag for Stage C bootstrap.

        Args:
            cli: CLI 名 (``cursor`` / ``claude`` / ``codex`` / 测试中可任意)。
            prompt: 给 CLI 的提示词 (透传给 adapter 由后者翻译)。
            cwd: 子进程工作目录, ``None`` 沿用 popolad CWD。
            env: 子进程环境变量字典; 与 envelope 注入的
                ``POPOLA_HANDOFF_FILE`` / ``POPOLA_HANDOFF_ID`` overlay 合并
                (overlay 优先)。``None`` 表示用 ``os.environ`` 作 base。
            adapter: 临时 adapter 覆盖 (优先级高于 ``__init__`` 时注入的)。
            extra: 可选 ``--cli-flag KEY=VAL`` 解析结果, 透传给 adapter。

        Returns:
            str: task_id (popola 内部 ID, 与 ArkTower ``Task.id`` 不同;
            后者经 ``arktower_task_id`` 字段关联)。

        Raises:
            RuntimeError: 当未注入 adapter 且本次也未传时。
            ValueError: 当 adapter 返回非 list[str] / 空 list, 或当
                ``prompt`` 为空导致 envelope 验证失败。
        """
        # E3 internal unification: every dispatch goes through
        # dispatch_with_envelope. The envelope is the single source of truth.
        envelope = HandoffEnvelope(
            handoff_id=generate_handoff_id(
                cli,
                prompt,
                adapter_extra=extra,
            ),
            created_at=datetime.now(UTC),
            target_cli=cli,
            prompt=prompt,
            cwd=str(cwd) if cwd is not None else None,
            adapter_extra=extra or {},
        )
        return self.dispatch_with_envelope(
            envelope,
            adapter=adapter,
            base_env=env,
        )

    def _dispatch_legacy(
        self,
        cli: str,
        prompt: str,
        cwd: str | Path | None,
        env: dict[str, str] | None,
        adapter: AdapterCallback | None,
        extra: dict[str, Any] | None,
    ) -> str:
        """Pre-Stage-B direct path: adapter → state → event log → supervisor.spawn.

        Kept verbatim from v0.0.1 + Stage A patches. Selected by
        ``POPOLA_USE_GRAPH=0`` for Stage C bootstrap; the production
        default is :meth:`_dispatch_via_graph`.
        """
        adapter_fn = adapter or self._adapter
        if adapter_fn is None:
            raise RuntimeError(
                "No adapter provided; pass adapter= to Popolad() or dispatch_task()"
            )

        task_id = self._make_task_id(cli)
        cwd_path = Path(cwd) if cwd is not None else None

        handoff_path = _resolve_handoff_path(env)
        cmd = self._call_adapter(
            adapter_fn, cli, prompt, cwd_path, extra, handoff_path=handoff_path
        )
        if not isinstance(cmd, list) or not cmd:
            raise ValueError(f"adapter returned invalid cmd: {cmd!r}")

        # v0.9.2: redact secrets (CURSOR_API_KEY in cloud marker payload)
        # before persisting to TaskHandle.cmd / NDJSON / ArkTower so
        # `popola list` / `popola status` / event_log don't expose them.
        # The unredacted ``cmd`` is still passed to ``supervisor.spawn``
        # so the cloud spawn-path can resolve the override; that resolution
        # also goes through the credential resolver (env / keyring).
        persistable_cmd = _redact_cmd_for_persistence(cmd)

        arktower_task_id, persisted = self._maybe_create_arktower_task(
            task_id=task_id, cli=cli, prompt=prompt, cmd=persistable_cmd
        )

        events_dir = self._resolve_events_dir(extra)
        event_log_path = events_dir / f"{task_id}.jsonl"
        event_log = EventLog(event_log_path, source=f"popola/{task_id}")
        with self._event_logs_lock:
            self._event_logs[task_id] = event_log

        # 直接以 RUNNING 注册, 避免 supervisor 的 wait 线程在 dispatch
        # 返回前就 fire on_exit 把 state 推到 COMPLETED 后, 我们再 update
        # 回 RUNNING 的 race。
        handle = TaskHandle(
            task_id=task_id,
            cli=cli,
            pid=None,
            state=TaskState.RUNNING,
            started_at=datetime.now(UTC),
            event_log_path=event_log_path,
            arktower_task_id=arktower_task_id,
            cmd=persistable_cmd,
            persisted=persisted,
        )
        self._state.register(handle)

        event_log.append(
            "task.dispatched",
            {
                "task_id": task_id,
                "cli": cli,
                "prompt": prompt,
                "cwd": str(cwd_path) if cwd_path else None,
                "cmd": persistable_cmd,
                "arktower_task_id": arktower_task_id,
                "persisted": persisted,
            },
        )

        pid = self._supervisor.spawn(
            task_id=task_id,
            cmd=cmd,
            cwd=cwd_path,
            env=env,
            event_log=event_log,
            on_exit=self._on_subprocess_exit,
        )

        self._state.update(task_id, pid=pid)
        self._record_popola_dispatch(arktower_task_id)
        return task_id

    def _dispatch_via_graph(
        self,
        cli: str,
        prompt: str,
        cwd: str | Path | None,
        env: dict[str, str] | None,
        adapter: AdapterCallback | None,
        extra: dict[str, Any] | None,
    ) -> str:
        """Stage B graph path: legacy effects + LangGraph-recorded graph.step events.

        Important design note: the **subprocess is spawned synchronously**
        before the graph starts, so ``dispatch_task`` returns with a live
        pid (matching the legacy contract; existing tests rely on it).
        The graph is then an *observer* — its ``supervisor_spawn`` callback
        just returns the already-spawned pid, and ``supervisor_wait`` blocks
        on a :class:`threading.Event` set by the supervisor's ``on_exit``
        wait-thread. This keeps the side-effect ordering identical to
        Stage A (NDJSON contract preserved per AC #5) while still giving us:

        - ``thread_id = task_id`` checkpointing in SqliteSaver
        - ``graph.step`` events at each of the 4 main-graph nodes
        - A clean place to slot HITL nodes (Stage E) without re-architecting
        """
        adapter_fn = adapter or self._adapter
        if adapter_fn is None:
            raise RuntimeError(
                "No adapter provided; pass adapter= to Popolad() or dispatch_task()"
            )

        task_id = self._make_task_id(cli)
        cwd_path = Path(cwd) if cwd is not None else None

        handoff_path = _resolve_handoff_path(env)
        cmd = self._call_adapter(
            adapter_fn, cli, prompt, cwd_path, extra, handoff_path=handoff_path
        )
        if not isinstance(cmd, list) or not cmd:
            raise ValueError(f"adapter returned invalid cmd: {cmd!r}")

        # v0.9.2: redact secrets in the persisted cmd (see _dispatch_legacy
        # for rationale). The supervisor still receives the unredacted
        # cmd so the cloud spawn-path can read the override.
        persistable_cmd = _redact_cmd_for_persistence(cmd)

        arktower_task_id, persisted = self._maybe_create_arktower_task(
            task_id=task_id, cli=cli, prompt=prompt, cmd=persistable_cmd
        )

        events_dir = self._resolve_events_dir(extra)
        event_log_path = events_dir / f"{task_id}.jsonl"
        event_log = EventLog(event_log_path, source=f"popola/{task_id}")
        with self._event_logs_lock:
            self._event_logs[task_id] = event_log

        handle = TaskHandle(
            task_id=task_id,
            cli=cli,
            pid=None,
            state=TaskState.RUNNING,
            started_at=datetime.now(UTC),
            event_log_path=event_log_path,
            arktower_task_id=arktower_task_id,
            cmd=persistable_cmd,
            persisted=persisted,
        )
        self._state.register(handle)

        event_log.append(
            "task.dispatched",
            {
                "task_id": task_id,
                "cli": cli,
                "prompt": prompt,
                "cwd": str(cwd_path) if cwd_path else None,
                "cmd": persistable_cmd,
                "arktower_task_id": arktower_task_id,
                "persisted": persisted,
            },
        )

        exit_event = threading.Event()
        exit_holder: dict[str, int | None] = {"exit_code": None}

        def _on_exit_internal(tid: str, exit_code: int) -> None:
            try:
                self._on_subprocess_exit(tid, exit_code)
            finally:
                exit_holder["exit_code"] = exit_code
                exit_event.set()

        pid = self._supervisor.spawn(
            task_id=task_id,
            cmd=cmd,
            cwd=cwd_path,
            env=env,
            event_log=event_log,
            on_exit=_on_exit_internal,
        )
        self._state.update(task_id, pid=pid)
        self._record_popola_dispatch(arktower_task_id)

        callbacks = self._make_graph_callbacks(
            task_id=task_id,
            event_log=event_log,
            adapter_fn=adapter_fn,
            existing_pid=pid,
            exit_event=exit_event,
            exit_holder=exit_holder,
        )
        graph = build_main_graph(
            checkpointer=self._get_or_create_checkpointer(),
            callbacks=callbacks,
        )
        initial_state = GraphTaskState(
            task_id=task_id,
            cli=cli,
            cwd=cwd_path,
            prompt=prompt,
            extra=extra or {},
            cmd=persistable_cmd,
            status="pending",
        )

        worker = threading.Thread(
            target=self._run_graph_for_task,
            args=(graph, initial_state, task_id),
            name=f"popolad-graph-{task_id}",
            daemon=True,
        )
        worker.start()
        return task_id

    @staticmethod
    def _call_adapter(
        adapter_fn: AdapterCallback,
        cli: str,
        prompt: str,
        cwd: Path | None,
        extra: dict[str, Any] | None,
        *,
        handoff_path: str | None = None,
    ) -> list[str]:
        """Call ``adapter_fn`` with the canonical 4-arg signature, then
        optionally inject ``--popola-handoff-file <path>`` (v0.7.2 C5 双通道).

        The :data:`AdapterCallback` type alias is strict 4-arg
        ``(cli, prompt, cwd, extra) -> argv`` (Stage E R-009 closure),
        but a few legacy test fixtures still ship 3-arg adapters
        ``(cli, prompt, cwd) -> argv``. We tolerate them with a
        ``TypeError`` fallback so the v0.2.0 → v0.3.0 migration window
        stays open without breaking those tests.

        Strategy:

        - When ``extra`` is non-None, we MUST call the 4-arg form (a
          3-arg adapter that didn't ask for extras would silently drop
          them — No Silent Failures rule); ``TypeError`` propagates.
        - When ``extra`` is ``None``, try 4-arg first; on ``TypeError``
          fall back to 3-arg (legacy fixtures).

        v0.7.2 (per design D-080 Q3=C5 双通道): after the adapter returns
        a base ``cmd``, if ``handoff_path`` is non-None **and** the user
        opted in via ``extra["popola_handoff_flag"] = True``, append
        ``["--popola-handoff-file", handoff_path]`` to the cmd. This is
        opt-in because vanilla ``cursor-agent`` / ``claude`` / ``codex``
        do not yet recognise the flag — auto-injecting would break their
        argv parsing. The env-var channel (``POPOLA_HANDOFF_FILE``) is
        always live and is the primary delivery; the flag is a
        forward-compat hook for sub-CLIs that gain native support later.
        """
        if extra is not None:
            cmd = adapter_fn(cli, prompt, cwd, extra)
        else:
            try:
                cmd = adapter_fn(cli, prompt, cwd, None)
            except TypeError:
                cmd = adapter_fn(cli, prompt, cwd)  # type: ignore[call-arg]

        # v0.7.2 C5 flag-channel injection (opt-in via popola_handoff_flag).
        if (
            handoff_path is not None
            and extra is not None
            and bool(extra.get("popola_handoff_flag"))
        ):
            cmd = list(cmd) + ["--popola-handoff-file", handoff_path]

        return cmd

    def _resolve_events_dir(self, extra: dict[str, Any] | None) -> Path:
        """Resolve the events directory for a single dispatch (R-014 closure).

        v0.2.0 Stage A wired ``--events-dir`` from CLI to
        ``extra["__events_dir"]`` (advisory hint stored in the extras
        bag); Stage E now honors it: when set, the per-task event log is
        written to that directory **for that one task only**, while
        :attr:`self._events_dir` remains the daemon-wide default.

        This lets multi-tenant tooling (e.g. self-bootstrap S3 recursive
        dispatch) silo per-task events into a tmp dir without touching
        the daemon process, satisfying spec §10 canonical paths while
        keeping the override scope minimal.

        The ``__events_dir`` key is **not stripped** from ``extra`` —
        adapters that don't recognise it ignore it harmlessly, and the
        existing :func:`tests.test_cli_httpx.test_cli_events_dir_advisory_passthrough`
        contract asserts the adapter sees the hint.

        Args:
            extra: optional adapter extras dict; may contain
                ``__events_dir`` (string path, set by CLI / RPC layer).

        Returns:
            Path: the resolved per-task events directory; parent
            directories are auto-created (matching :class:`EventLog`
            ``__init__`` semantics).
        """
        if extra is not None:
            override = extra.get("__events_dir")
            if override:
                events_dir = Path(str(override)).expanduser().resolve()
                events_dir.mkdir(parents=True, exist_ok=True)
                return events_dir
        return self._events_dir

    def get_status(self, task_id: str) -> dict[str, Any]:
        """Return runtime status for ``task_id`` (full shape).

        See :meth:`_task_summary` for the unified return shape.

        Raises:
            KeyError: 当 ``task_id`` 未注册。
        """
        handle = self._state.get(task_id)
        if handle is None:
            raise KeyError(f"task_id not found: {task_id}")
        return self._task_summary(handle, full=True)

    def tail_events(self, task_id: str, since_index: int = 0) -> list[dict[str, Any]]:
        """Return CloudEvents envelopes from ``since_index`` onward.

        Args:
            task_id: 已注册的 task_id。
            since_index: 起始下标 (0=从头, 历史 polling 用上次 ``len()``)。

        Raises:
            KeyError: 当 ``task_id`` 未注册。
        """
        if self._state.get(task_id) is None:
            raise KeyError(f"task_id not found: {task_id}")
        with self._event_logs_lock:
            event_log = self._event_logs.get(task_id)
        if event_log is None:
            return []
        return event_log.tail(since_index=since_index)

    def list_active(self) -> list[dict[str, Any]]:
        """Return summary dicts for all currently-running (non-terminal) tasks.

        v0.2.0 R-014: shape 与 :meth:`get_status` 通过 :meth:`_task_summary`
        统一; ``list_active`` 用 ``full=False`` (不含 ``exit_code`` /
        ``completed_at`` / ``latest_event_index`` 等终态字段)。
        """
        return [
            self._task_summary(h, full=False)
            for h in self._state.list_active()
        ]

    def list_all(self, *, include_terminal: bool = False) -> list[dict[str, Any]]:
        """Return summary dicts for all tasks (active or terminal).

        Args:
            include_terminal: ``False`` 等价于 :meth:`list_active`;
                ``True`` 返回所有 task (含 COMPLETED/FAILED/CANCELED)。
        """
        handles = self._state.list_all() if include_terminal else self._state.list_active()
        return [self._task_summary(h, full=include_terminal) for h in handles]

    def _has_popola_dispatch_row(self, arktower_task_id: str | None) -> bool:
        """Return True iff popola_dispatch has a row for this ArkTower task id."""
        if arktower_task_id is None or self._persistence is None:
            return False
        try:
            conn = self._persistence.connection.get_connection()
            row = conn.execute(
                "SELECT 1 FROM popola_dispatch WHERE task_id = ? LIMIT 1",
                (arktower_task_id,),
            ).fetchone()
            return row is not None
        except Exception:
            logger.exception(
                "popola_dispatch lookup failed for arktower_task_id=%s; "
                "treating as 'row present' so cancel keeps its strict default",
                arktower_task_id,
            )
            return True

    def _record_popola_dispatch(
        self,
        arktower_task_id: str | None,
        *,
        runtime: str = "popen",
        supervisor: str = "in-process",
    ) -> None:
        """Insert a ``popola_dispatch`` row marking this task as successfully spawned.

        v0.7.1 BUG-B prerequisite: :meth:`rehydrate_from_persistence` and
        :meth:`cancel_task` use the presence of a ``popola_dispatch``
        row to distinguish *"spawned, then orphaned by a daemon crash"*
        from *"never spawned because dispatch crashed pre-spawn"*. Per
        ``migrations/005_popolaloom_extensions.sql``, the table was
        introduced in v0.2.0 as occupied schema with the comment
        *"Populating it is a v0.3.0 concern"*. v0.7.1 finally fills the
        gap so the BUG-A/B heuristics are sound end-to-end (the
        NFR-8 recovery test in tests/matrix/nfr/ regresses without this
        insert because every legitimately-spawned task would otherwise
        be flagged as ``spawn_aborted`` on rehydrate).

        Per workspace rule "No Silent Failures": failures here are
        logged at ERROR + swallowed (the task itself spawned fine; we
        don't want to fail dispatch on an audit-table write). The
        downside is that the next rehydrate may flag this task as
        spawn_aborted; we accept that trade-off because the alternative
        — raising and failing the user-visible dispatch — is worse.

        Args:
            arktower_task_id: ArkTower row id; ``None`` (no persistence
                wired) is a no-op.
            runtime: ``"popen"`` for v0.7.x in-process supervision;
                future tmux / systemd-run runtimes will populate this
                column accordingly.
            supervisor: ``"in-process"`` for v0.7.x; future detached
                supervisors set ``"subprocess"``.
        """
        if arktower_task_id is None or self._persistence is None:
            return
        try:
            conn = self._persistence.connection.get_connection()
            conn.execute(
                "INSERT OR IGNORE INTO popola_dispatch "
                "(dispatch_id, task_id, runtime, supervisor) "
                "VALUES (?, ?, ?, ?)",
                (
                    f"dispatch-{arktower_task_id}",
                    arktower_task_id,
                    runtime,
                    supervisor,
                ),
            )
            conn.commit()
        except Exception:
            logger.exception(
                "_record_popola_dispatch: failed to insert row for "
                "arktower_task_id=%s; rehydrate will treat this task as "
                "spawn_aborted on next daemon restart",
                arktower_task_id,
            )

    def cancel_task(
        self,
        task_id: str,
        *,
        sigterm_grace_s: float = 5.0,
        daemon_started_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Send SIGTERM to the task subprocess, escalate to SIGKILL after grace.

        v0.2.0 Stage A 简化实现: 仅 SIGTERM (与 SIGKILL fallback) 加事件
        log; Stage E 会与 ArkTower advance_task(CANCELED) 联动。

        v0.4.1 Stage L1.A: state-first ordering — :attr:`TaskState.CANCELED`
        is set on the StateStore **before** SIGTERM goes out, and
        :attr:`TaskHandle.cancel_escalated_to_sigkill` is set
        **before** the SIGKILL syscall. This guarantees the supervisor
        wait-thread (now consulting :class:`StateStore`) reads an
        accurate verdict regardless of which side wins the proc.wait
        race, and the resulting NDJSON terminal event is always
        ``task.canceled`` with the correct ``sigkill_escalated`` field
        — closing the [research §F.3](
        ../../../.local/memory/research/v0.5.0-skill-install-lark-research.md)
        contract bug consumed by ``evaluation/runner.py:325-331``.

        v0.7.1 BUG-A fix: when ``daemon_started_at`` is supplied AND the
        rehydrated handle predates the current daemon AND no
        ``popola_dispatch`` row exists for the underlying ArkTower task,
        fall through to :meth:`_soft_cancel_orphan` instead of raising
        the legacy "race window between dispatch and spawn" error
        forever. See Step 2 of fix/v0.7.1-* dispatch.

        Args:
            task_id: 已注册的 task_id。
            sigterm_grace_s: SIGTERM 后等待秒数, 超时 SIGKILL。
            daemon_started_at: optional UTC start time of the running
                popolad process; supplied by :mod:`popolaloom.daemon.rpc`
                so we can identify orphan handles created by a previous
                daemon. ``None`` keeps legacy behavior (no orphan-reap).

        Returns:
            dict: ``{task_id, requested_signal, escalated_to_sigkill, pid}``.

        Raises:
            KeyError: 当 ``task_id`` 未注册。
            RuntimeError: 当任务已是终态 / 没有 pid。
        """
        import os as _os
        import signal as _signal
        import time as _time

        handle = self._state.get(task_id)
        if handle is None:
            raise KeyError(f"task_id not found: {task_id}")
        if handle.is_terminal():
            raise RuntimeError(
                f"task {task_id} already in terminal state {handle.state}; cannot cancel"
            )
        if handle.runtime == "cloud":
            return self._cancel_cloud_task(handle)

        pid = handle.pid
        if pid is None:
            # Orphan-reap path: rehydrated handles from a previous daemon process that
            # crashed before populating popola_dispatch will sit forever in this state.
            # We can identify them by (a) no popola_dispatch row AND (b) handle.started_at
            # older than the *current* daemon's started_at — in that case the in-memory
            # handle was created by rehydrate, not by this daemon's dispatch path.
            is_orphan = (
                daemon_started_at is not None
                and handle.started_at < daemon_started_at
                and not self._has_popola_dispatch_row(handle.arktower_task_id)
            )
            if is_orphan:
                return self._soft_cancel_orphan(task_id, handle)
            raise RuntimeError(
                f"task {task_id} has no pid yet "
                "(race window between dispatch and spawn)"
            )

        with self._event_logs_lock:
            event_log = self._event_logs.get(task_id)

        try:
            self._state.update(
                task_id,
                state=TaskState.CANCELED,
                cancel_escalated_to_sigkill=False,
            )
        except KeyError:
            logger.warning(
                "cancel: task %s vanished from state store before signal", task_id
            )

        try:
            _os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            logger.warning("cancel: pid=%d already gone for task=%s", pid, task_id)
            if event_log is not None:
                event_log.append(
                    "task.cancel_requested",
                    {"task_id": task_id, "pid": pid, "result": "process_already_gone"},
                )
            return {
                "task_id": task_id,
                "requested_signal": "SIGTERM",
                "escalated_to_sigkill": False,
                "pid": pid,
                "result": "process_already_gone",
            }

        if event_log is not None:
            event_log.append(
                "task.cancel_requested",
                {"task_id": task_id, "pid": pid, "signal": "SIGTERM"},
            )

        deadline = _time.monotonic() + max(0.0, sigterm_grace_s)
        escalated = False
        while _time.monotonic() < deadline:
            try:
                _os.kill(pid, 0)
            except ProcessLookupError:
                break
            _time.sleep(0.05)
        else:
            try:
                self._state.update(
                    task_id, cancel_escalated_to_sigkill=True
                )
            except KeyError:
                logger.warning(
                    "cancel: task %s vanished before SIGKILL escalation marker",
                    task_id,
                )
            try:
                _os.kill(pid, _signal.SIGKILL)
                escalated = True
                if event_log is not None:
                    event_log.append(
                        "task.cancel_escalated",
                        {"task_id": task_id, "pid": pid, "signal": "SIGKILL"},
                    )
            except ProcessLookupError:
                try:
                    self._state.update(
                        task_id, cancel_escalated_to_sigkill=False
                    )
                except KeyError:
                    logger.warning(
                        "cancel: task %s vanished while reverting escalation marker",
                        task_id,
                    )

        try:
            self._state.update(task_id, state=TaskState.CANCELED)
        except KeyError:
            logger.warning(
                "cancel: task %s vanished from state store", task_id
            )

        return {
            "task_id": task_id,
            "requested_signal": "SIGTERM",
            "escalated_to_sigkill": escalated,
            "pid": pid,
        }

    def _resolve_cloud_cursor_client(self) -> CloudCursorClient:
        """Return the injected client or lazily construct one from credentials.

        v0.9.2: resolves through :func:`popolaloom.credentials.resolve_cursor_api_key`
        so cloud cancel honours both the historical ``CURSOR_API_KEY`` env
        var path and the OS keyring storage. Tests inject
        ``Popolad(cloud_client=...)`` to bypass the resolver entirely.
        """
        if self._cloud_client is not None:
            return self._cloud_client
        from popolaloom.credentials import resolve_cursor_api_key

        api_key = resolve_cursor_api_key()
        if not api_key:
            raise RuntimeError(
                "Cursor API key is required to cancel cloud-runtime tasks "
                "(set CURSOR_API_KEY env, run `popola auth cursor set`, "
                "or inject Popolad(cloud_client=...) in tests)"
            )
        self._cloud_client = CloudCursorClient(api_key)
        return self._cloud_client

    def _cancel_cloud_task(self, handle: TaskHandle) -> dict[str, Any]:
        """Cancel a cloud-runtime task via Cursor ``POST .../runs/.../cancel``."""
        task_id = handle.task_id
        event_log = self._ensure_task_event_log(task_id, handle)

        if not handle.cursor_agent_id or not handle.cursor_run_id:
            err_detail = (
                "missing cursor_agent_id or cursor_run_id "
                f"(agent_id={handle.cursor_agent_id!r}, run_id={handle.cursor_run_id!r})"
            )
            logger.error("cloud cancel: %s for task=%s", err_detail, task_id)
            event_log.append(
                "task.failed",
                {
                    "task_id": task_id,
                    "runtime": "cloud",
                    "error_kind": "cloud_cancel_no_handle",
                    "error_detail": err_detail,
                    "agent_id": handle.cursor_agent_id,
                    "run_id": handle.cursor_run_id,
                },
            )
            raise RuntimeError(f"cloud cancel failed: {err_detail}")

        client = self._resolve_cloud_cursor_client()
        agent_id = handle.cursor_agent_id
        run_id = handle.cursor_run_id
        assert agent_id is not None and run_id is not None

        now = datetime.now(UTC)
        try:
            client.cancel_run(agent_id, run_id)
        except CursorCloudConflictError as exc:
            logger.warning(
                "cloud cancel 409 for task=%s (agent_busy / not cancellable): %s",
                task_id,
                exc,
            )
            try:
                self._state.update(
                    task_id,
                    state=TaskState.CANCELED,
                    completed_at=now,
                )
            except KeyError:
                logger.warning(
                    "cloud cancel 409: task %s vanished from state store", task_id
                )
            event_log.append(
                "task.canceled",
                {
                    "task_id": task_id,
                    "runtime": "cloud",
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "cancel_kind": "best_effort_after_409",
                },
            )
            return {
                "task_id": task_id,
                "requested_signal": "cloud_cancel",
                "escalated_to_sigkill": False,
                "pid": None,
                "result": "cloud_cancel_best_effort_409",
            }
        except CursorCloudError as exc:
            event_log.append(
                "task.failed",
                {
                    "task_id": task_id,
                    "runtime": "cloud",
                    "error_kind": (
                        "cloud_cancel_network_error"
                        if exc.status_code is None
                        else "cloud_cancel_failed"
                    ),
                    "error_detail": str(exc),
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "error": {
                        "error_type": type(exc).__name__,
                        "is_retryable": exc.is_retryable,
                        "message": str(exc),
                        "status_code": exc.status_code,
                    },
                },
            )
            if exc.status_code is None:
                logger.error(
                    "cloud cancel network/transport error for task=%s: %s",
                    task_id,
                    exc,
                )
            else:
                logger.error(
                    "cloud cancel HTTP error for task=%s (status=%s): %s",
                    task_id,
                    exc.status_code,
                    exc,
                )
            raise

        try:
            self._state.update(
                task_id,
                state=TaskState.CANCELED,
                completed_at=now,
            )
        except KeyError:
            logger.warning("cloud cancel: task %s vanished from state store", task_id)
        event_log.append(
            "task.canceled",
            {
                "task_id": task_id,
                "runtime": "cloud",
                "agent_id": agent_id,
                "run_id": run_id,
            },
        )
        return {
            "task_id": task_id,
            "requested_signal": "cloud_cancel",
            "escalated_to_sigkill": False,
            "pid": None,
            "result": "cloud_canceled",
        }

    def _ensure_task_event_log(self, task_id: str, handle: TaskHandle) -> EventLog:
        """Return the task's :class:`EventLog`, registering one if needed."""
        with self._event_logs_lock:
            existing = self._event_logs.get(task_id)
            if existing is not None:
                return existing
        log = EventLog(handle.event_log_path, source=f"popola/{task_id}")
        with self._event_logs_lock:
            self._event_logs[task_id] = log
        return log

    def _soft_cancel_orphan(
        self,
        task_id: str,
        handle: TaskHandle,
    ) -> dict[str, Any]:
        """Reap an orphaned task left behind by a prior daemon process.

        Triggered from :meth:`cancel_task` when ``popola_dispatch`` has no
        row AND the handle's ``started_at`` predates this daemon's
        startup time — meaning :meth:`rehydrate_from_persistence` placed
        it in :class:`StateStore` but the original subprocess was
        already gone with no recovery path. We persist the cancellation
        in ArkTower + ``task_history`` (``trigger='cancel_orphan'``) and
        emit a ``task.canceled`` NDJSON event so consumers see a clean
        terminal state instead of looping on
        "race window between dispatch and spawn" forever.

        Per workspace rule "No Silent Failures": every persistence /
        event-log failure inside this method is logged via
        :func:`logger.exception` with context; we never bare-except.
        """
        self._state.update(
            task_id,
            state=TaskState.CANCELED,
            completed_at=datetime.now(UTC),
        )

        arktower_task_id = handle.arktower_task_id
        if self._persistence is not None and arktower_task_id is not None:
            from popolaloom._vendored.arktower.core.models import (
                TaskStatus as _ArkTaskStatus,
            )
            from popolaloom._vendored.arktower.core.models import (
                TaskUpdate as _ArkTaskUpdate,
            )

            try:
                existing = self._persistence.repository.get(arktower_task_id)
                from_status_value = existing.status.value if existing else "submitted"
            except Exception:
                logger.exception(
                    "orphan-reap: repository.get failed for arktower_task_id=%s; "
                    "defaulting from_status='submitted' for audit",
                    arktower_task_id,
                )
                from_status_value = "submitted"

            try:
                self._persistence.repository.update(
                    arktower_task_id,
                    _ArkTaskUpdate(status=_ArkTaskStatus.CANCELED),
                )
            except Exception:
                logger.exception(
                    "orphan-reap: ArkTower repository.update(CANCELED) failed for %s; "
                    "audit row + event still emitted",
                    arktower_task_id,
                )

            try:
                conn = self._persistence.connection.get_connection()
                now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                conn.execute(
                    """INSERT INTO task_history
                           (event_id, task_id, trigger, from_status, to_status,
                            actor, notes, timestamp)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        arktower_task_id,
                        "cancel_orphan",
                        from_status_value,
                        "canceled",
                        "popolad-orphan-reaper",
                        "orphaned_by_daemon_restart",
                        now_iso,
                    ),
                )
                conn.commit()
            except Exception:
                logger.exception(
                    "orphan-reap: task_history INSERT failed for %s",
                    arktower_task_id,
                )

        with self._event_logs_lock:
            event_log = self._event_logs.get(task_id)
        if event_log is None:
            try:
                event_log = EventLog(
                    handle.event_log_path,
                    source=f"popola/{task_id}",
                )
                with self._event_logs_lock:
                    self._event_logs[task_id] = event_log
            except Exception:
                logger.exception(
                    "orphan-reap: failed to (re)open EventLog at %s; "
                    "skipping task.canceled emission for %s",
                    handle.event_log_path,
                    task_id,
                )
                event_log = None

        if event_log is not None:
            try:
                event_log.append(
                    "task.canceled",
                    {
                        "task_id": task_id,
                        "reason": "orphaned_by_daemon_restart",
                        "trigger": "cancel_orphan",
                        "arktower_task_id": arktower_task_id,
                    },
                )
            except Exception:
                logger.exception(
                    "orphan-reap: event_log.append(task.canceled) failed for %s",
                    task_id,
                )

        return {
            "task_id": task_id,
            "requested_signal": "none",
            "escalated_to_sigkill": False,
            "pid": None,
            "result": "orphaned_by_daemon_restart",
        }

    def event_log(self, task_id: str) -> EventLog | None:
        """Return the :class:`EventLog` for ``task_id`` if any (thread-safe)."""
        with self._event_logs_lock:
            return self._event_logs.get(task_id)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _make_task_id(cli: str) -> str:
        """Generate ``<cli>-<12hex>`` task_id (短 + 可读)."""
        cli_safe = cli.strip().lower() or "task"
        return f"{cli_safe}-{uuid.uuid4().hex[:12]}"

    def _task_summary(self, handle: TaskHandle, *, full: bool) -> dict[str, Any]:
        """Unified summary shape for ``list`` / ``status`` (R-014 fix).

        Args:
            handle: registered :class:`TaskHandle`.
            full: ``True`` 返回包含 exit_code / completed_at / latest_event_index
                / arktower_task_id / persisted 的完整 dict; ``False`` 返回
                ``list_active`` 简化集 (task_id / cli / state / pid / started_at)。

        Returns:
            dict 形如:

                {
                    "task_id": str,
                    "cli": str,
                    "state": str,
                    "pid": int | None,
                    "started_at": str (ISO ms + Z),
                    # full=True 时额外字段:
                    "exit_code": int | None,
                    "completed_at": str | None,
                    "latest_event_index": int,
                    "arktower_task_id": str | None,
                    "persisted": bool,
                    "runtime": str,
                    "cursor_agent_id": str | None,
                    "cursor_run_id": str | None,
                    "cloud_phase": str | None,
                }
        """
        summary: dict[str, Any] = {
            "task_id": handle.task_id,
            "cli": handle.cli,
            "state": str(handle.state),
            "pid": handle.pid,
            "started_at": handle.started_at.isoformat(timespec="milliseconds"),
            "runtime": handle.runtime,
            "cursor_agent_id": handle.cursor_agent_id,
            "cursor_run_id": handle.cursor_run_id,
            "cloud_phase": handle.cloud_phase,
        }
        if not full:
            return summary

        with self._event_logs_lock:
            event_log = self._event_logs.get(handle.task_id)
        latest_event_index = len(event_log) if event_log is not None else 0

        summary.update(
            {
                "exit_code": handle.exit_code,
                "completed_at": (
                    handle.completed_at.isoformat(timespec="milliseconds")
                    if handle.completed_at is not None
                    else None
                ),
                "latest_event_index": latest_event_index,
                "arktower_task_id": handle.arktower_task_id,
                "persisted": handle.persisted,
            }
        )
        return summary

    def _maybe_create_arktower_task(
        self,
        *,
        task_id: str,
        cli: str,
        prompt: str,
        cmd: list[str],
    ) -> tuple[str | None, bool]:
        """Persist (or schema-parity) an ArkTower task for this popola dispatch.

        Resolution order:

        1. **persistence injected** (Stage C real path) → call
           :meth:`TaskService.create_task` (async, bridged via
           :func:`asyncio.run`).  Success → ``(task.id, True)``.  Failure
           → log + return ``(None, False)`` per R-008 (No Silent Failures).
        2. **legacy task_repository injected** (Stage A bridging
           form) → call ``repository.create(ark_task)``.  Success →
           ``(persisted.id, True)``.  Failure → log + ``(None, False)``.
        3. **neither injected** → construct an ArkTask model in-memory
           and return its UUID with ``persisted=False`` (Stage A schema
           parity placeholder; satisfies tests asserting
           ``status["arktower_task_id"] is not None``).
        4. **arktower import fails** → ``(None, False)``.

        Args:
            task_id: popola internal task id (stored in
                ``ArkTask.parameters["popola_task_id"]`` so the bridge
                can correlate transitions back to the popola handle).
            cli: adapter name (used in title + ``preferred_agent_type``).
            prompt: prompt text (truncated to 80 chars in title).
            cmd: subprocess argv (stored in ``parameters["cmd"]``).

        Returns:
            tuple ``(arktower_task_id, persisted)``.
        """
        try:
            from popolaloom._vendored.arktower.core.models import (
                Task as ArkTask,
            )
            from popolaloom._vendored.arktower.core.models import TaskCreate
        except ImportError:
            logger.warning(
                "popolaloom._vendored.arktower not importable; "
                "skipping Task model construction"
            )
            return None, False

        if self._persistence is not None:
            try:
                task_create = TaskCreate(
                    title=f"[{cli}] {prompt[:80]}",
                    description=prompt,
                    parameters={
                        "popola_task_id": task_id,
                        "cli": cli,
                        "cmd": cmd,
                    },
                    kind="popola.dispatch",
                    preferred_agent_type=cli,
                )
                created = asyncio.run(self._persistence.task_service.create_task(task_create))
                return created.id, True
            except Exception:
                logger.exception(
                    "ArkTower TaskService.create_task failed for popola task %s; "
                    "returning (None, persisted=False)",
                    task_id,
                )
                return None, False

        ark_task = ArkTask(
            title=f"[{cli}] {prompt[:80]}",
            description=prompt,
            parameters={
                "popola_task_id": task_id,
                "cli": cli,
                "cmd": cmd,
            },
            kind="popola.dispatch",
            preferred_agent_type=cli,
        )

        if self._task_repository is None:
            return ark_task.id, False

        try:
            persisted = self._task_repository.create(ark_task)
            return persisted.id, True
        except Exception:
            logger.exception(
                "ArkTower repo.create failed for task %s; returning None (persisted=False)",
                task_id,
            )
            return None, False

    def _on_subprocess_exit(self, task_id: str, exit_code: int) -> None:
        """Wait-thread 回调: 把 StateStore 状态推到 COMPLETED / FAILED.

        v0.2.0 R-008: 当 ``state.update`` 抛 KeyError (task 已从 store
        消失, 例如 cancel 已经把它清掉了或者在 stage C rehydrate 错位时),
        emit ``state.ghost_exit`` 事件而不是静默吞噬。

        v0.2.0 Stage C: 由于 EventLog 改为 fd-held buffered (R-011),
        诊断性 ghost_exit 必须立即 fsync 到磁盘以保证 forensic 可读 (避免
        bug report 时 daemon 崩溃后丢失 ghost_exit 行); 若是临时构造的
        EventLog (任务不在 ``self._event_logs`` 里), 还要 close 释放 fd。

        v0.4.1 Stage L2.B (carry-over fix from L1): consult StateStore
        BEFORE deciding the new state — if the handle is already
        :attr:`TaskState.CANCELED` (set by :meth:`cancel_task` before
        SIGTERM/SIGKILL), do NOT clobber with COMPLETED/FAILED. The
        supervisor wait-thread already emits the right NDJSON event
        (``task.canceled`` with ``sigkill_escalated``); this guard is
        the StateStore-side mirror so :meth:`get_status` and
        :meth:`list_all` keep returning ``CANCELED`` after the
        subprocess actually exits.

        v0.4.1 Stage L2.B (notifier hook): after the StateStore update
        succeeds, schedule the Lark proactive notifier coroutine on
        :attr:`_loop` via :func:`asyncio.run_coroutine_threadsafe` so
        the wait-thread does not block on ``lark-cli``. When
        :attr:`_loop` is ``None`` (test path, or daemon constructed
        outside an asyncio context) the schedule step is skipped with
        an explicit INFO log per workspace rule "No Silent Failures".
        """
        existing = self._state.get(task_id)
        already_canceled = (
            existing is not None and existing.state == TaskState.CANCELED
        )
        if already_canceled:
            new_state = TaskState.CANCELED
        else:
            new_state = TaskState.COMPLETED if exit_code == 0 else TaskState.FAILED
        try:
            if already_canceled:
                self._state.update(task_id, exit_code=exit_code)
            else:
                self._state.update(task_id, state=new_state, exit_code=exit_code)
        except KeyError:
            logger.warning(
                "on_exit for unknown task_id=%s exit_code=%d; emitting state.ghost_exit",
                task_id,
                exit_code,
            )
            with self._event_logs_lock:
                event_log = self._event_logs.get(task_id)
            owned = event_log is not None
            if event_log is None:
                event_log_path = self._events_dir / f"{task_id}.jsonl"
                event_log = EventLog(event_log_path, source=f"popola/{task_id}")
            event_log.append(
                "state.ghost_exit",
                {
                    "task_id": task_id,
                    "exit_code": exit_code,
                    "reason": "task disappeared from StateStore before subprocess exit",
                },
            )
            if not owned:
                event_log.close()
            else:
                event_log.fsync()
            return

        self._schedule_lark_terminal_notification(task_id, new_state, exit_code)

    def _schedule_lark_terminal_notification(
        self,
        task_id: str,
        terminal_state: TaskState,
        exit_code: int,
    ) -> None:
        """Schedule :func:`send_terminal_notification` on the daemon's loop.

        Helper extracted from :meth:`_on_subprocess_exit` so the
        scheduling decision (loop available vs not) is tested in
        isolation. When :attr:`_loop` is ``None`` (e.g. unit tests
        constructing :class:`Popolad` outside asyncio) the call is
        skipped with an explicit INFO log; the notifier itself logs
        every skip / failure case so this method's job is purely
        cross-thread dispatch.

        Per workspace rule "No Silent Failures": cross-thread
        scheduling exceptions are caught + logged but do NOT propagate
        — losing a notification card is not worth crashing the
        wait-thread (which would orphan the per-task subprocess
        bookkeeping and cause downstream cancel storms).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.info(
                "lark.notify.unscheduled task_id=%s state=%s reason=no_loop",
                task_id,
                terminal_state,
            )
            return
        try:
            from popolaloom.lark.notifier import send_terminal_notification

            asyncio.run_coroutine_threadsafe(
                send_terminal_notification(
                    self, task_id, terminal_state, exit_code
                ),
                loop,
            )
        except Exception:
            logger.exception(
                "lark.notify.schedule_failed task_id=%s state=%s",
                task_id,
                terminal_state,
            )

    # -- graph integration (Stage B) --------------------------------------

    def _get_or_create_checkpointer(self) -> _Saver | None:
        """Return the shared :class:`BaseCheckpointSaver` for graph dispatches.

        Lazy initialisation: the checkpointer is only built on the first
        graph dispatch so import-time / unit-test paths that never touch
        the graph don't pay the SQLite open cost. Idempotent + thread-safe
        via :attr:`_checkpointer_lock`.

        Returns ``None`` if SqliteSaver construction fails (logged) — the
        graph still works, it just won't checkpoint, which is acceptable
        for v0.2.0 (Stage E will require strict checkpointing).
        """
        if self._checkpointer is not None:
            return self._checkpointer
        with self._checkpointer_lock:
            if self._checkpointer is not None:
                return self._checkpointer
            try:
                from popolaloom.daemon.checkpoint import make_checkpointer

                self._checkpointer = make_checkpointer()
                logger.info("popolad checkpointer initialised at default path")
            except Exception:
                logger.exception(
                    "Failed to initialise SqliteSaver; graph will run without checkpointing"
                )
                self._checkpointer = None
        return self._checkpointer

    def _make_graph_callbacks(
        self,
        *,
        task_id: str,
        event_log: EventLog,
        adapter_fn: AdapterCallback,
        existing_pid: int,
        exit_event: threading.Event,
        exit_holder: dict[str, int | None],
    ) -> GraphCallbacks:
        """Build :class:`GraphCallbacks` bound to an already-spawned subprocess.

        The supervisor was spawned by :meth:`_dispatch_via_graph` *before*
        the graph started so the synchronous pid contract is preserved.
        Therefore:

        - ``supervisor_spawn`` returns the *existing* pid (no re-spawn).
        - ``supervisor_wait`` blocks on the ``threading.Event`` signalled by
          the supervisor's wait thread; the actual exit code is read from
          ``exit_holder``.

        ``adapter_build_command`` and ``event_log_emit`` are straightforward
        re-exports of the existing helpers.
        """
        outer = self

        class _GraphCallbacksImpl:
            def adapter_build_command(
                self,
                cli: str,
                prompt: str,
                cwd: Path | None,
                extra: dict[str, Any] | None,
            ) -> list[str]:
                # v0.7.2 C5: re-resolve handoff path from os.environ since
                # the graph callback doesn't have direct access to the
                # outer dispatch's `env` dict; we relied on the daemon's
                # process env carrying POPOLA_HANDOFF_FILE only when the
                # parent dispatch_with_envelope set it via merged_env.
                # That overlay landed in the SUBPROCESS env, not the
                # daemon's own env, so this fallback returns None unless
                # a higher-level caller exported the var explicitly. The
                # graph re-call happens AFTER the actual dispatch already
                # built the cmd, so a None here is safe — it just means
                # the graph-mode introspective re-call won't double-add
                # the flag (the actual cmd already has it from the first
                # _call_adapter invocation).
                handoff_path = _resolve_handoff_path(None)
                return outer._call_adapter(
                    adapter_fn,
                    cli,
                    prompt,
                    cwd,
                    extra,
                    handoff_path=handoff_path,
                )

            def supervisor_spawn(
                self,
                tid: str,
                cmd: list[str],
                cwd: Path | None,
                env_unused: dict[str, str] | None,
            ) -> int:
                # Pre-spawned by _dispatch_via_graph; just record the pid.
                return existing_pid

            def supervisor_wait(self, tid: str) -> tuple[int, int]:
                exit_event.wait()
                code = exit_holder["exit_code"] if exit_holder["exit_code"] is not None else -1
                return code, len(event_log)

            def event_log_emit(
                self,
                tid: str,
                type_: str,
                data: dict[str, Any],
            ) -> None:
                event_log.append(type_, data)

        return _GraphCallbacksImpl()

    def _run_graph_for_task(
        self,
        graph: _Compiled,
        initial_state: GraphTaskState,
        task_id: str,
    ) -> None:
        """Run ``graph.invoke`` for a single task in this background thread.

        Errors propagate from the graph nodes; we catch + log + force the
        :class:`StateStore` handle into ``FAILED`` so external observers
        see a clean terminal state. We do *not* re-raise — the wait-thread
        path (supervisor.spawn -> _on_subprocess_exit) already owns the
        canonical state transition; this is a defensive fallback for the
        case where the graph itself blows up before reaching wait_node.
        """
        try:
            from langchain_core.runnables import RunnableConfig

            config: RunnableConfig = {"configurable": {"thread_id": task_id}}
            graph.invoke(initial_state, config=config)
        except Exception:
            logger.exception(
                "graph.invoke raised for task %s; forcing state=FAILED", task_id
            )
            try:
                self._state.update(task_id, state=TaskState.FAILED)
            except KeyError:
                logger.warning(
                    "graph fallback: task %s already absent from StateStore", task_id
                )

    # -- Stage C: persistence rehydrate + shutdown ------------------------

    def rehydrate_from_persistence(self) -> int:
        """Load in-flight ArkTower tasks back into the in-memory StateStore.

        Called by the daemon on startup (rpc.py lifespan / main.py boot)
        so that ``popolad`` survives restarts without losing visibility
        into long-running tasks.  AC #8 of v0.2.0 Stage C + Stage E
        (R-002 closure) per S1 self-bootstrap scenario.

        Strategy:

        - Filter ArkTower's ``tasks`` table for any **non-terminal**
          status: ``SUBMITTED`` (popolad-created but not yet advanced;
          v0.2.0 dispatch leaves the new row in this state because the
          ArkTower lifecycle is owned by the upstream service layer
          which doesn't auto-advance), ``QUEUED``, ``IN_PROGRESS``,
          ``REVIEW``, ``INPUT_REQUIRED`` (HITL paused — Stage E
          ``supply_feedback`` resume target), ``BLOCKED``.  Terminal
          statuses (``COMPLETED`` / ``FAILED`` / ``CANCELED`` /
          ``TIMED_OUT``) are excluded.
        - For each task, build a :class:`TaskHandle` whose ``state`` is
          ``RUNNING`` (popolad's coarser state), ``arktower_task_id`` is
          the ArkTower id, ``persisted=True``, and ``pid=None`` (the
          subprocess is gone — popolad cannot resurrect it across a
          restart; v0.3.0's R-010 supervisor work will fix this for
          systemd-run / tmux managed children).
        - :meth:`StateStore.rehydrate` writes them all in one locked batch.
        - :meth:`_emit_recovered_events` writes a per-task
          ``popolad.recovered`` event on each rehydrated NDJSON file.

        Returns:
            int: count of tasks rehydrated.  ``0`` when no persistence
            is configured or no in-flight tasks were found.

        Notes:
            - Idempotent: re-calling overwrites existing handles with
              fresh DB state (matches :meth:`StateStore.rehydrate`
              authoritative semantics).
            - Does NOT spawn any subprocesses; rehydrated handles are
              "observe-only" placeholders until v0.3.0 adds resume.
        """
        if self._persistence is None:
            return 0

        try:
            from popolaloom._vendored.arktower.core.models import (
                TaskFilter,
                TaskStatus,
            )
        except ImportError:
            logger.warning(
                "popolaloom._vendored.arktower not importable; skipping rehydrate"
            )
            return 0

        non_terminal = [
            TaskStatus.SUBMITTED,
            TaskStatus.QUEUED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.REVIEW,
            TaskStatus.INPUT_REQUIRED,
            TaskStatus.BLOCKED,
        ]

        try:
            ark_tasks = self._persistence.repository.list(
                TaskFilter(status=non_terminal, limit=1000)
            )
        except Exception:
            logger.exception("repository.list failed during rehydrate; returning 0")
            return 0

        ark_tasks = list(ark_tasks)
        if not ark_tasks:
            logger.info("rehydrate_from_persistence: no in-flight tasks found")
            return 0

        handles: list[TaskHandle] = []
        spawn_aborted_count = 0
        for ark_task in ark_tasks:
            params = ark_task.parameters or {}
            popola_task_id = params.get("popola_task_id")
            cli = params.get("cli") or ark_task.preferred_agent_type or "unknown"
            cmd = params.get("cmd") or []

            # v0.7.1 BUG-B fix: only popolad-owned tasks (those with popola_task_id)
            # need a popola_dispatch row to be considered alive. If we have a
            # popola_task_id but NO matching popola_dispatch row, dispatch crashed
            # before spawn — surface as failed rather than reviving a ghost RUNNING
            # handle in StateStore. Tasks WITHOUT popola_task_id (legitimately
            # created outside popolad — e.g. ``arktower task add``) keep current
            # behavior because they were never expected to have a dispatch row.
            if popola_task_id is not None and not self._has_popola_dispatch_row(ark_task.id):
                self._handle_pre_dispatch_orphan(ark_task, str(popola_task_id), str(cli))
                spawn_aborted_count += 1
                continue

            if not popola_task_id:
                popola_task_id = self._make_task_id(str(cli))

            event_log_path = self._events_dir / f"{popola_task_id}.jsonl"
            handle = TaskHandle(
                task_id=str(popola_task_id),
                cli=str(cli),
                pid=None,
                state=TaskState.RUNNING,
                started_at=ark_task.started_at or ark_task.created_at or datetime.now(UTC),
                event_log_path=event_log_path,
                arktower_task_id=ark_task.id,
                cmd=list(cmd) if isinstance(cmd, list) else [],
                persisted=True,
            )
            handles.append(handle)

        seen: set[str] = set()
        deduped: list[TaskHandle] = []
        for h in handles:
            if h.task_id in seen:
                logger.warning(
                    "rehydrate: duplicate popola_task_id=%s; keeping first",
                    h.task_id,
                )
                continue
            seen.add(h.task_id)
            deduped.append(h)

        self._state.rehydrate(deduped)
        recovered_task_ids = [h.task_id for h in deduped]
        self._emit_recovered_events(deduped, recovered_task_ids)
        logger.info(
            "rehydrated %d in-flight task(s) from ArkTower SQLite "
            "(skipped %d spawn-aborted orphan(s))",
            len(deduped),
            spawn_aborted_count,
        )
        return len(deduped)

    def _handle_pre_dispatch_orphan(
        self,
        ark_task: Any,
        popola_task_id: str,
        cli: str,
    ) -> None:
        """Mark an orphan ArkTower task as failed when ``popola_dispatch`` is missing.

        Triggered from :meth:`rehydrate_from_persistence` when a task
        carries a ``popola_task_id`` but no ``popola_dispatch`` row —
        meaning the original daemon process crashed between
        :meth:`TaskService.create_task` and :meth:`Supervisor.spawn`. We
        close the loop persistently so the next rehydrate doesn't see
        it again, and emit a forensic ``popolad.spawn_aborted`` event.

        Per workspace rule "No Silent Failures": the persistence /
        event-log writes are wrapped in try/except + logger.exception so
        the rehydrate loop continues for the remaining tasks even if
        one orphan write fails.
        """
        from popolaloom._vendored.arktower.core.models import (
            TaskStatus as _ArkTaskStatus,
        )
        from popolaloom._vendored.arktower.core.models import (
            TaskUpdate as _ArkTaskUpdate,
        )

        if self._persistence is not None:
            try:
                self._persistence.repository.update(
                    ark_task.id,
                    _ArkTaskUpdate(
                        status=_ArkTaskStatus.FAILED,
                        error="spawn_aborted_pre_dispatch",
                    ),
                )
            except Exception:
                logger.exception(
                    "rehydrate: ArkTower repository.update(FAILED, "
                    "error='spawn_aborted_pre_dispatch') failed for %s",
                    ark_task.id,
                )

        event_log_path = self._events_dir / f"{popola_task_id}.jsonl"
        try:
            log = EventLog(event_log_path, source=f"popola/{popola_task_id}")
            try:
                log.append(
                    "popolad.spawn_aborted",
                    {
                        "popola_task_id": popola_task_id,
                        "arktower_task_id": ark_task.id,
                        "cli": cli,
                        "reason": "spawn_aborted_pre_dispatch",
                        "detail": (
                            "no popola_dispatch row present at rehydrate time; "
                            "previous daemon crashed before subprocess spawn"
                        ),
                    },
                )
            finally:
                log.close()
        except Exception:
            logger.exception(
                "rehydrate: failed to emit popolad.spawn_aborted event for %s "
                "(arktower_task_id=%s)",
                popola_task_id,
                ark_task.id,
            )

    def _emit_recovered_events(
        self,
        handles: list[TaskHandle],
        recovered_task_ids: list[str],
    ) -> None:
        """Emit ``popolad.recovered`` event in each rehydrated task's event log.

        v0.2.0 Stage E E1 (R-002 closure): the S1 self-bootstrap test
        kills popolad with SIGKILL, restarts the daemon, and asserts
        that each previously-in-flight task's NDJSON file contains a
        ``popolad.recovered`` envelope.  This makes the rehydrate
        operation observable end-to-end (forensic) and gives operators
        a clear marker for "task survived a daemon restart".

        For each rehydrated handle:

        - Open / re-open an :class:`EventLog` at the task's
          ``event_log_path`` (parent dir auto-created).  Stage A's
          fd-held buffered writer means we keep this fd around in
          :attr:`_event_logs` so any subsequent activity (e.g. v0.3.0
          resume) lands in the same file without re-opening.
        - Append a single ``popolad.recovered`` envelope with
          ``recovered_count`` (total in this batch), ``task_ids`` (full
          list — repeated per file so consumers tailing one task can
          still see the cohort), and ``popola_task_id`` (this task).

        Errors per individual handle are caught + logged but do not
        block the remaining tasks (No Silent Failures: every failure
        is logged with the offending task_id).
        """
        for handle in handles:
            try:
                with self._event_logs_lock:
                    existing = self._event_logs.get(handle.task_id)
                if existing is None:
                    log = EventLog(
                        handle.event_log_path,
                        source=f"popola/{handle.task_id}",
                    )
                    with self._event_logs_lock:
                        self._event_logs[handle.task_id] = log
                else:
                    log = existing
                log.append(
                    "popolad.recovered",
                    {
                        "popola_task_id": handle.task_id,
                        "arktower_task_id": handle.arktower_task_id,
                        "cli": handle.cli,
                        "recovered_count": len(recovered_task_ids),
                        "task_ids": list(recovered_task_ids),
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to emit popolad.recovered for task %s; "
                    "rehydrate continues for remaining tasks",
                    handle.task_id,
                )

    def shutdown_persistence_bridge(self) -> None:
        """Tear down the Stage C persistence + bridge wiring (idempotent).

        Called from rpc.py's lifespan ``finally`` block so that:

        - The :class:`PopolaEventBusBridge` is unsubscribed from the
          ArkTower :class:`EventBus` (avoids handler leaks across daemon
          restart in test contexts).
        - The :class:`TaskPersistence` SQLite connection is closed
          (avoids "database is locked" warnings in subsequent test runs
          that re-open the same file).

        Safe to call when no persistence / bridge was injected — both
        attributes are checked for ``None`` first.
        """
        if self._event_bus_bridge is not None:
            try:
                self._event_bus_bridge.unsubscribe()
            except Exception:
                logger.exception("event_bus_bridge.unsubscribe() failed")
        if self._persistence is not None:
            try:
                self._persistence.close()
            except Exception:
                logger.exception("persistence.close() failed")
