"""
/code-review 命令的实现（prompt 类型）。

对应 TS：claude-code-main/src/commands/insights.ts 那种「庞大实现 + 懒加载」模式。
prompt 命令不本地处理，而是产出要发给模型的内容块，由 QueryEngine 进 Agent Loop。
被 commands/__init__.py 里 _code_review_prompt 在执行时才 import。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from Tool import ToolUseContext

ContentBlock = Dict[str, Any]


async def get_prompt_for_command(
    args: str, context: "ToolUseContext"
) -> List[ContentBlock]:
    """返回要发给 Claude 的内容块。args 是用户在 /code-review 后附带的额外指示。"""
    extra = f"\n额外关注：{args}" if args.strip() else ""
    return [{
        "type": "text",
        "text": f"请审查当前改动的代码，找出 bug 与可简化之处。{extra}",
    }]
