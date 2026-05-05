"""popolaloom-adapter — Day-1 三 CLI 命令构造层 (cursor / claude / codex).

Public surface (v0.2.0 Stage E post R-009 split):

- :class:`CommandBuilder` (``typing.Protocol``, ``runtime_checkable``) — PURE
  contract every per-CLI adapter satisfies. v0.0.1 alias :data:`Adapter`
  preserved for back-compat.
- :class:`Runtime` (``typing.Protocol``) — stateful runtime contract
  (spawn / status / attach / kill / cost-meter); v0.2.0 has no
  implementations (daemon's Supervisor covers it); v0.3.0+ slots
  systemd-run / tmux backends here.
- :func:`register_adapter` / :func:`get_adapter` / :func:`list_registered` —
  global registry primitives (No Silent Failures: duplicate name raises).
- :func:`build_command` — convenience facade; **THE** callable that
  :meth:`popolaloom.daemon.server.Popolad.dispatch_task` accepts as its
  ``adapter`` kwarg (signature ``Callable[[cli, prompt, cwd, extra], list[str]]``
  — Stage E unified 4-arg signature per :data:`popolaloom.daemon.AdapterCallback`).
- :class:`CursorAdapter` / :class:`ClaudeAdapter` / :class:`CodexAdapter` —
  the 3 default concrete classes (Phase 1 CLI subset, 出处: spec §3.2);
  all satisfy :class:`CommandBuilder`.

On module import the 3 default adapters are auto-registered, so
``from popolaloom.adapters import build_command`` works zero-config.
"""

from __future__ import annotations

from popolaloom.adapters.base import (
    Adapter,
    CommandBuilder,
    Runtime,
    build_command,
    get_adapter,
    list_registered,
    register_adapter,
)
from popolaloom.adapters.claude import ClaudeAdapter
from popolaloom.adapters.codex import CodexAdapter
from popolaloom.adapters.cursor import CursorAdapter

__all__ = [
    "Adapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "CommandBuilder",
    "CursorAdapter",
    "Runtime",
    "build_command",
    "get_adapter",
    "list_registered",
    "register_adapter",
]


def _register_defaults() -> None:
    """Auto-register cursor / claude / codex adapters at import time.

    Idempotent — Python module cache ensures this runs once per interpreter,
    but we guard against ``importlib.reload()`` cases (used in some test
    scenarios) by skipping any name already present in ``_REGISTRY``.
    """
    for adapter in (CursorAdapter(), ClaudeAdapter(), CodexAdapter()):
        if adapter.name not in list_registered():
            register_adapter(adapter)


_register_defaults()
