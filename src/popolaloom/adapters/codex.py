"""CodexAdapter — wraps ``codex exec`` for one-shot non-interactive run.

Source command derivation (出处:
``.local/memory/research/02-cli-capabilities.md`` §"OpenAI Codex CLI 调用形态"):

    codex exec "<prompt>"

Codex 的 ``--sandbox`` 三档 (``read-only`` / ``workspace-write`` /
``danger-full-access``) 是 5 个主流 CLI 中沙箱模型最严谨的; PopolaLoom 派发
协议直接复用此三档 (出处: 02 §"OpenAI Codex CLI 调用形态" + spec §3.5)。

# TODO(phase 2): 切换到 ``codex app-server --listen ws://...`` WebSocket
# 拿到 5 个主流 CLI 中**唯一原生**的 long-running daemon 形态;
# 见 ADR backlog + 02 §"OpenAI Codex CLI 后台与会话"。Day-1 仅 ``codex exec``
# 已足够覆盖 spec §3.2 row "popolaloom-adapter" Phase 1 验收。

Optional ``extra`` keys:

- ``sandbox`` (str): 必须为 ``"read-only"`` / ``"workspace-write"`` /
  ``"danger-full-access"`` 之一; 追加 ``--sandbox <sandbox>``。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_SANDBOX: tuple[str, ...] = (
    "read-only",
    "workspace-write",
    "danger-full-access",
)


class CodexAdapter:
    """OpenAI Codex CLI (``codex``) command builder."""

    name: str = "codex"
    binary: str = "codex"

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Construct ``codex exec <prompt>`` argv list (PURE).

        Args:
            prompt: 主提示词, 作为 ``codex exec`` 的位置参数。
            cwd: 透传以匹配 :class:`Adapter` Protocol; **不进 argv** ——
                Day-1 暂不用 codex 的 ``--cd`` flag, 由 supervisor 通过
                ``Popen(cwd=...)`` 控制。
            extra: 见模块 docstring 一个键。

        Returns:
            list[str]: 形如 ``[codex, exec, <prompt>, [--sandbox, <s>]]``。

        Raises:
            ValueError: 当 ``extra["sandbox"]`` 不在白名单内
                (No Silent Failures)。
        """
        extra = extra or {}

        cmd: list[str] = [self.binary, "exec", prompt]

        sandbox = extra.get("sandbox")
        if sandbox is not None:
            sandbox = str(sandbox)
            if sandbox not in _ALLOWED_SANDBOX:
                raise ValueError(
                    f"codex: sandbox must be one of {_ALLOWED_SANDBOX!r}, "
                    f"got {sandbox!r}"
                )
            cmd.extend(["--sandbox", sandbox])

        return cmd

    def is_available(self) -> bool:
        """Return True iff ``codex`` resolves on ``$PATH``."""
        return shutil.which(self.binary) is not None
