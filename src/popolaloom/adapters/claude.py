"""ClaudeAdapter — wraps ``claude -p`` with ``stream-json --verbose``.

Source command derivation (出处:
``.local/memory/research/02-cli-capabilities.md`` §"Claude Code"):

    claude -p "<prompt>" --output-format stream-json --verbose

``--verbose`` 是关键: ``stream-json`` 不开 verbose 会把 tool-use 事件吞掉
(来源: ``backgroundclaude.com/blog/stream-json``, 02 §"Claude Code 调用形态")。
``cwd`` 不进 argv (claude 没有 ``--cwd`` flag), 由 ``Supervisor`` 通过
``Popen(cwd=...)`` 控制。

Optional ``extra`` keys:

- ``session_id`` (str): 追加 ``--session-id <UUID>``; PopolaLoom 派发器可
  ``uuid.uuid4()`` 预生成 UUID, 实现"先分配 ID 再 spawn"的统一接口
  (出处: 02 §"Claude Code 后台与会话")。
- ``max_turns`` (int): 追加 ``--max-turns <n>``, 限制对话轮数避免长任务失控。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ClaudeAdapter:
    """Claude Code CLI (``claude``) command builder."""

    name: str = "claude"
    binary: str = "claude"

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Construct ``claude -p ... --output-format stream-json --verbose`` argv (PURE).

        Args:
            prompt: 主提示词, 作为 ``-p`` 的值。
            cwd: 透传给签名以匹配 :class:`Adapter` Protocol; **不进 argv** —
                claude 没有 ``--cwd``, 由 supervisor 通过 ``Popen(cwd=...)`` 控制。
            extra: 见模块 docstring 两个键。

        Returns:
            list[str]: 形如 ``[claude, -p, <prompt>, --output-format, stream-json,
            --verbose, [--session-id, <id>], [--max-turns, <n>]]``。
        """
        extra = extra or {}

        cmd: list[str] = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if "session_id" in extra:
            cmd.extend(["--session-id", str(extra["session_id"])])

        if "max_turns" in extra:
            cmd.extend(["--max-turns", str(int(extra["max_turns"]))])

        return cmd

    def is_available(self) -> bool:
        """Return True iff ``claude`` resolves on ``$PATH``."""
        return shutil.which(self.binary) is not None
