"""
/help 命令的实现。

对应 TS：claude-code-main/src/commands/help/help.tsx（真实实现返回一个 React 组件）
Python 里没有 React，用一段「界面描述文本」占位。
被 commands/__init__.py 里 help 命令的 load() 懒加载。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Tool import ToolUseContext


async def call(args: str, context: "ToolUseContext") -> str:
    """列出所有可用命令。真实实现返回 React 组件，这里返回一段描述字符串。"""
    # 延迟 import 避免与注册中心循环依赖：call 运行时 __init__ 早已加载完毕。
    from commands import COMMANDS

    names = ", ".join(f"/{c.name}" for c in COMMANDS())
    return f"可用命令：{names}"
