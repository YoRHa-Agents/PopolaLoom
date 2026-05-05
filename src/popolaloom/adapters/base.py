"""Adapter Protocol + global registry — Day-1 命令构造层公共骨架.

v0.2.0 Stage E (R-009 P1 closure)
---------------------------------

The original v0.0.1 :class:`Adapter` Protocol carried only ``build_command``
+ ``is_available`` (2 of the 6 adapter actions documented in
``.local/memory/specs/popolaloom/spec.md`` §3.2). Stage E splits the
contract into two Protocols so the boundary between **pure command
construction** (owned by per-CLI adapter classes, no IO) and **stateful
runtime concerns** (spawn / status / attach / kill / cost-meter, owned by
:class:`popolaloom.daemon.supervisor.Supervisor` for v0.2.0) is explicit:

- :class:`CommandBuilder` — PURE: takes ``(prompt, cwd, extra)`` and
  returns ``argv``. No subprocess, no fs reads beyond ``shutil.which``,
  no time / random. v0.2.0 implements this for ``cursor`` / ``claude`` /
  ``codex``.
- :class:`Runtime` — Stateful Protocol DOCUMENTED for v0.3.0+ when
  systemd-run / tmux backends arrive. v0.2.0 has no implementation
  classes for it; the daemon's :class:`Supervisor` already covers
  spawn / kill / attach via direct subprocess/threading code, but Stage
  E declares the interface so v0.3.0 alternative backends (e.g.
  ``SystemdRunRuntime``, ``TmuxRuntime``) slot in without re-architecting.
- :data:`Adapter` — alias of :class:`CommandBuilder` kept so v0.2.0
  callers (``tests/test_adapters.py``, public ``from popolaloom.adapters
  import Adapter`` users) continue to work unchanged. Future deprecation
  path: warn in v0.3.0, remove in v0.4.0.

Module structure (post-Stage-E):

- :class:`CommandBuilder` — the new Protocol; identical signature to the
  v0.0.1 :class:`Adapter`.
- :class:`Runtime` — new Protocol stub (v0.2.0 has no implementations).
- :data:`Adapter` = :class:`CommandBuilder` — alias for back-compat.
- ``_REGISTRY``: process-level dict of name → CommandBuilder instance.
- :func:`register_adapter` / :func:`get_adapter` / :func:`list_registered`:
  registry primitives (No Silent Failures: duplicate name raises).
- :func:`build_command`: convenience facade that
  :class:`popolaloom.daemon.server.Popolad` consumes as its ``adapter``
  callback.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from popolaloom.daemon.event_log import EventLog

logger = logging.getLogger(__name__)


@runtime_checkable
class CommandBuilder(Protocol):
    """PURE per-CLI command-list builder Protocol (R-009 split).

    实现该 Protocol 的类必须满足:

    - ``name`` / ``binary`` 是 *实例可访问* 的字符串属性 (class attr 即可).
    - ``build_command`` 必须 PURE: 不 ``subprocess`` / 不读写文件 / 不读
      ``os.environ`` / 不 ``time.sleep``; 仅做确定性的 list 构造。
    - ``is_available`` 仅可 ``shutil.which`` 一次, 返回 bool。

    Stage E split rationale: keeping pure command construction separate
    from stateful runtime concerns (:class:`Runtime`) makes it trivial
    to unit-test adapters without spawning subprocesses, and makes the
    v0.3.0 backend switch (systemd-run / tmux) a single dependency
    injection rather than a refactor.
    """

    name: str
    binary: str

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Translate ``(prompt, cwd, extra)`` into a subprocess argv list."""
        ...

    def is_available(self) -> bool:
        """Return True iff ``binary`` resolves on ``$PATH``."""
        ...


@runtime_checkable
class Runtime(Protocol):
    """Stateful adapter runtime — spawn / status / attach / kill / cost-meter.

    v0.2.0 (Stage E R-009) declares this Protocol but has **no concrete
    implementations**: :class:`popolaloom.daemon.supervisor.Supervisor`
    already covers ``spawn`` / ``kill`` (subprocess + threading); attach
    is done via :func:`popolaloom.daemon.event_log.EventLog.tail`; status
    is owned by :class:`popolaloom.daemon.state.StateStore`; ``cost_meter``
    is N/A in v0.2.0 (planned for v0.3.0+ when token-budget tracking
    arrives, see spec §6 NFR-9).

    The protocol is documented now so v0.3.0+ alternative backends
    (``SystemdRunRuntime``, ``TmuxRuntime``, ``DockerRuntime``) implement
    against a stable contract:

    - ``spawn`` MUST detach the child from the popolad supervisor's
      session group (``setsid`` / ``--scope`` / ``new-session``) so a
      popolad SIGTERM does not propagate to the workload. Returns the
      child pid (or backend-specific identifier rendered as int).
    - ``status`` MUST be cheap (≤ 10 ms) — typically a ``waitpid(WNOHANG)``
      / ``systemctl --user is-active`` / ``tmux has-session`` poll.
    - ``attach`` MUST be a *streaming* async iterator yielding NDJSON-
      compatible event dicts; backpressure is the caller's responsibility.
    - ``kill`` MUST first SIGTERM with grace, then SIGKILL — never the
      reverse. Returns ``True`` if the workload was running and SIGTERM
      was delivered, ``False`` if it had already exited.
    - ``cost_meter`` returns a backend-specific dict (e.g. ``{"prompt_tokens":
      ..., "completion_tokens": ..., "wall_seconds": ...}``); v0.3.0
      defines the canonical schema.

    Why a Protocol and not an ABC: lets v0.3.0 backends import zero
    popolaloom code; structural typing keeps the dependency direction
    clean (popolaloom-runtime → popolaloom-adapter, never the reverse).
    """

    name: str

    def spawn(
        self,
        task_id: str,
        cmd: list[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        event_log: EventLog,
    ) -> int:
        """Spawn the workload subprocess; return pid/handle as int."""
        ...

    def status(self, task_id: str) -> str:
        """Return the workload's runtime status (string FSM label)."""
        ...

    def attach(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """Stream events (NDJSON-compatible dicts) from the workload."""
        ...

    def kill(self, task_id: str) -> bool:
        """SIGTERM (with grace) then SIGKILL the workload.

        Returns:
            True iff the workload was running and a signal was delivered.
        """
        ...

    def cost_meter(self, task_id: str) -> dict[str, Any]:
        """Return cost / usage metrics for the workload."""
        ...


# ─── back-compat alias (v0.0.1 → v0.2.0+ migration window) ───────────────


Adapter = CommandBuilder
"""Back-compat alias for v0.0.1 :class:`Adapter` Protocol.

Stage E (R-009) renamed the canonical type to :class:`CommandBuilder` to
make the pure / stateful split explicit, but legacy callers like
``tests/test_adapters.py`` and ``from popolaloom.adapters import Adapter``
external users continue to work unchanged. v0.3.0 will emit a
:class:`DeprecationWarning` on direct ``Adapter`` use; v0.4.0 removes the
alias entirely.
"""


_REGISTRY: dict[str, CommandBuilder] = {}


def register_adapter(adapter: CommandBuilder) -> None:
    """Insert ``adapter`` into the global registry keyed by ``adapter.name``.

    Args:
        adapter: 满足 :class:`CommandBuilder` Protocol 的实例。

    Raises:
        ValueError: 当同名 adapter 已注册。No Silent Failures: 不允许覆盖,
            避免 import 顺序差异导致行为漂移; 测试场景需要替换时, 应直接
            操作 ``_REGISTRY`` 或在 fixture 中 snapshot/restore。
    """
    name = adapter.name
    if name in _REGISTRY:
        existing = type(_REGISTRY[name]).__name__
        incoming = type(adapter).__name__
        raise ValueError(
            f"adapter {name!r} already registered "
            f"(existing={existing}, new={incoming})"
        )
    _REGISTRY[name] = adapter
    logger.debug("registered adapter %r -> %s", name, type(adapter).__name__)


def get_adapter(name: str) -> CommandBuilder:
    """Lookup adapter by name.

    Raises:
        KeyError: 当 ``name`` 未注册; 错误消息附上当前可用名字列表,
            便于排错 (出处: spec §6 NFR-3 friendly errors)。
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        available = ", ".join(sorted(_REGISTRY)) or "<empty>"
        raise KeyError(
            f"no adapter registered for cli={name!r}; available: [{available}]"
        ) from exc


def list_registered() -> list[str]:
    """Return sorted snapshot of registered adapter names."""
    return sorted(_REGISTRY)


def build_command(
    cli: str,
    prompt: str,
    cwd: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Convenience facade — ``get_adapter(cli).build_command(prompt, cwd, extra)``.

    This is **the** callable that
    :meth:`popolaloom.daemon.server.Popolad.dispatch_task` accepts via its
    ``adapter`` kwarg; the daemon now invokes it as
    ``adapter_fn(cli, prompt, cwd, extra)`` (Stage E unified 4-arg
    :data:`AdapterCallback` signature, R-009 closure).

    Args:
        cli: 注册名 (``cursor`` / ``claude`` / ``codex`` 等)。
        prompt: 主提示词 (透传给具体 adapter)。
        cwd: 子进程工作目录; 仅传给 adapter, 不在此层处理。
        extra: 可选的 per-adapter 旁路参数 (e.g. session_id / sandbox)。

    Returns:
        list[str]: ``subprocess.Popen`` 可直接消费的 argv list。

    Raises:
        KeyError: 当 ``cli`` 未注册 (透传自 :func:`get_adapter`)。
    """
    return get_adapter(cli).build_command(prompt, cwd, extra)
