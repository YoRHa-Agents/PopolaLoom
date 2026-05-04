"""CursorAdapter — wraps ``cursor-agent agent --print`` for non-interactive run.

Source command derivation (出处:
``.local/memory/research/02-cli-capabilities.md`` §"Cursor Agent CLI"):

    cursor-agent agent --print --output-format text "<prompt>"

Optional ``extra`` keys:

- ``output_format`` (str): ``"text"`` (default) or ``"stream-json"``.
  ``stream-json`` 让 ``Supervisor`` 端的 NDJSON 解析器逐行消费; 注意官方
  约束 — partial-output 事件必须按 ``timestamp_ms`` + ``model_call_id`` 去重
  (出处: 02 §"Cursor Agent CLI 调用形态")。
- ``cwd_flag`` (bool): 若 True 且 ``cwd`` 非 None, 注入 ``--cwd <cwd>``;
  否则由 ``Supervisor`` 通过 ``Popen(cwd=...)`` 控制工作目录。
- ``session_id`` (str): 追加 ``--session-id <chatId>``; 与 PopolaLoom
  "派发前 ``cursor-agent create-chat`` 预生成 chatId" 的语义对齐
  (出处: 02 §"Cursor Agent CLI 后台与会话")。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_OUTPUT_FORMATS: tuple[str, ...] = ("text", "stream-json")


class CursorAdapter:
    """Cursor Agent CLI (``cursor-agent``) command builder."""

    name: str = "cursor"
    binary: str = "cursor-agent"

    def build_command(
        self,
        prompt: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[str]:
        """Construct ``cursor-agent agent --print`` argv list (PURE).

        Args:
            prompt: 主提示词, 作为最后一个位置参数传给 ``cursor-agent``。
            cwd: 工作目录; 仅在 ``extra["cwd_flag"]`` 为真时下沉到 ``--cwd``。
            extra: 见模块 docstring 三个键。

        Returns:
            list[str]: 形如 ``[cursor-agent, agent, --print, --output-format,
            <fmt>, [--cwd, <cwd>,] <prompt>, [--session-id, <id>]]``。

        Raises:
            ValueError: 当 ``extra["output_format"]`` 不在白名单内
                (No Silent Failures)。
        """
        extra = extra or {}

        output_format = str(extra.get("output_format", "text"))
        if output_format not in _ALLOWED_OUTPUT_FORMATS:
            raise ValueError(
                f"cursor: output_format must be one of {_ALLOWED_OUTPUT_FORMATS!r}, "
                f"got {output_format!r}"
            )

        cmd: list[str] = [
            self.binary,
            "agent",
            "--print",
            "--output-format",
            output_format,
        ]

        if extra.get("cwd_flag"):
            if cwd is None:
                logger.warning(
                    "cursor: cwd_flag=True but cwd=None; skipping --cwd "
                    "(adapter is PURE — won't read os.getcwd())"
                )
            else:
                cmd.extend(["--cwd", str(cwd)])

        cmd.append(prompt)

        if "session_id" in extra:
            cmd.extend(["--session-id", str(extra["session_id"])])

        return cmd

    def is_available(self) -> bool:
        """Return True iff ``cursor-agent`` resolves on ``$PATH``."""
        return shutil.which(self.binary) is not None
