"""
/compact 命令的实现 —— 上下文压缩。

对应 TS：
    claude-code-main/src/commands/compact/compact.ts       （命令入口 call）
    claude-code-main/src/services/compact/compact.ts        （compactConversation 核心）

压缩做两件事：
    1. 把整段历史发给模型，请它生成一段【摘要】（真实是 queryModelWithStreaming）
    2. 用「边界标记 + 摘要消息」【替换掉】原来的全部历史 → 释放上下文
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from Tool import ToolUseContext

# 一个 content block / 一条消息都用 dict 表示（沿用 QueryEngine.py 的约定）
ContentBlock = Dict[str, Any]
Message = Dict[str, Any]


# -----------------------------------------------------------------------------
# 摘要器：真实实现是调用模型（services/api/claude.ts 的 queryModelWithStreaming）。
# 教学版默认用本地 mock，不联网也能跑。接真实模型时整体替换这个名字即可
# （和 QueryEngine 里 call_claude_api 同理）。
# -----------------------------------------------------------------------------
async def _mock_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    extra = f"（按用户要求侧重：{custom_instructions}）" if custom_instructions else ""
    return f"【对话摘要】本次会话共 {len(messages)} 条消息，已压缩为本摘要。{extra}"


# 模块级、可替换：默认 mock。
summarize_conversation = _mock_summarize


def _create_compact_boundary_message(pre_compact_count: int) -> Message:
    """对应 TS 的 createCompactBoundaryMessage：一条系统标记，记录此处发生过压缩。"""
    return {
        "role": "system",
        "content": [{"type": "text", "text": "--- 上下文已在此压缩 ---"}],
        "is_compact_boundary": True,        # 标记位，UI / 自动压缩逻辑会读它
        "pre_compact_count": pre_compact_count,
    }


def _get_compact_user_summary_message(summary: str) -> List[ContentBlock]:
    """对应 TS 的 getCompactUserSummaryMessage：把摘要包成一条 user 消息的内容。"""
    return [{"type": "text", "text": f"这是之前对话的摘要，请基于它继续：\n{summary}"}]


async def call(args: str, context: "ToolUseContext") -> str:
    messages = context.messages
    # 对应 TS：if (messages.length === 0) throw new Error('No messages to compact')
    if not messages:
        return "没有可压缩的消息。"

    pre_count = len(messages)
    custom_instructions = args.strip()

    # 1) 请模型生成摘要（教学版走 mock）
    summary = await summarize_conversation(messages, custom_instructions, context)

    # 2) 构建「边界标记 + 摘要消息」（对应 boundaryMarker + summaryMessages）
    boundary = _create_compact_boundary_message(pre_count)
    summary_msg: Message = {
        "role": "user",
        "content": _get_compact_user_summary_message(summary),
        "is_compact_summary": True,
    }

    # 3) ★【就地替换】历史 —— 必须 clear()+extend()，不能重新赋值！
    #    因为 QueryEngine 里 context.messages 与 engine.messages 是【同一个 list 对象】，
    #    重新赋值只会让 context 指向新 list，engine 那边还指着旧的，历史就没真正变短。
    context.messages.clear()
    context.messages.extend([boundary, summary_msg])

    return f"已把 {pre_count} 条消息压缩为摘要（当前历史 {len(context.messages)} 条）。"
