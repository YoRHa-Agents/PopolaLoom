"""PopolaLoom — 跨 CLI 元编排器 (meta-orchestrator).

DevolaFlow 之上的本机常驻"织机式"元编排器: 通过 popolad daemon
+ ArkTower 任务池 + LangGraph 子图, 在 Cursor / Claude / Codex 等多
CLI 之上提供依赖图、HITL、attach/resume 与跨终端存活的一等公民支持。

参见 .local/memory/specs/popolaloom/spec.md 中 §1 项目使命。
"""

__version__ = "0.5.0"

__all__ = ["__version__"]
