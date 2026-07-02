"""
/rename 命令的实现 —— 给当前会话起一个名字。

对应 TS：
    claude-code-main/src/commands/rename/rename.ts       —— 命令入口
    claude-code-main/src/commands/rename/generateSessionName.ts —— AI 自动起名（我们不做）
    claude-code-main/src/utils/sessionStorage.ts          —— saveCustomTitle

核心流程：
    1. 有参数 → 直接用做标题
    2. 无参数 → 提示用户输入
    3. 把 custom-title entry 追加到会话 JSONL 尾部
    4. --resume 选单读尾 64KB 时自动扫到它
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Tool import ToolUseContext

__all__ = ["call"]


async def call(args: str, context: "ToolUseContext") -> str:
    """
    /rename [标题] —— 给当前会话起个名字。

    如果有参数就用它做标题；没有则提示用户输入。
    标题会出现在 --resume 选单里，优先级高于「最近一句话」。
    """
    project_dir = context.project_dir
    session_id = context.session_id

    if not session_id or not project_dir:
        return "当前会话没有持久化，无法重命名。"

    new_name = args.strip()

    if not new_name:
        # 无参数 → 交互式输入
        try:
            new_name = input("新标题: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "已取消。"
        if not new_name:
            return "已取消（空标题无效）。"

    # 延迟导入
    from session_persistence import set_custom_title

    set_custom_title(
        session_id,
        Path(project_dir),
        new_name,
        cwd=os.getcwd(),
    )

    return f"会话已重命名为：{new_name}"


from pathlib import Path  # noqa: E402
