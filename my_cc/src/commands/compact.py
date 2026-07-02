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


# =============================================================================
# 摘要系统提示词 —— 对齐 TS src/services/compact/prompt.ts 的 BASE_COMPACT_PROMPT
# =============================================================================
# 真实版要求模型输出 9 段结构化 XML（<summary>/<analysis>），然后 strip <analysis>。
# 我们简化为中文 prompt，让模型直接输出纯文本摘要。
_COMPACT_SYSTEM_PROMPT = """\
You are a helpful AI assistant tasked with summarizing conversations.
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. Do NOT use XML tags."""


def _format_messages_for_summary(messages: List[Message]) -> str:
    """把消息列表格式化为一段可读文本，发给模型做摘要。

    只取 user / assistant 消息的文本部分；工具调用只记名字、跳过工具结果。
    """
    lines: List[str] = []
    for m in messages:
        role = m.get("role", "?")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            t = block.get("type", "")
            if t == "text":
                text = (block.get("text") or "").strip()
                if text:
                    label = "用户" if role == "user" else "助手"
                    # 截断过长的单条消息，防 prompt 爆炸
                    if len(text) > 2000:
                        text = text[:2000] + "…"
                    lines.append(f"[{label}] {text}")
            elif t == "tool_use":
                name = block.get("name", "?")
                lines.append(f"[助手调用工具: {name}]")
    return "\n".join(lines)


def _build_summary_request(
    messages: List[Message], custom_instructions: str
) -> Message:
    """构建发给模型的「请总结这段对话」请求消息。"""
    conversation_text = _format_messages_for_summary(messages)
    extra = f"\n（用户要求侧重：{custom_instructions}）" if custom_instructions else ""
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"""请把以下对话总结为一段结构化摘要。用中文回复。

总结应包含：
1. 主要用户需求：用户想做什么
2. 技术概念：涉及的关键技术点
3. 文件与代码：涉及哪些文件、做了什么改动
4. 错误与修复：遇到过什么问题、怎么解决的
5. 当前状态：做完了什么、还有什么待做

对话内容：
{conversation_text}{extra}

请直接给出摘要，不要加「以下是总结」之类的前缀。""",
            }
        ],
    }


# -----------------------------------------------------------------------------
# 真实摘要器：调用 API 生成结构化摘要。
# API 不可用时（如未设密钥）自动回退 mock。
# -----------------------------------------------------------------------------
async def _real_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    """调用真实 API 生成对话摘要。"""
    # 延迟导入，避免 compact.py 加载时就依赖 anthropic
    import anthropic
    from anthropic_api import _read_settings, _sanitize_messages

    s = _read_settings()

    client_kwargs: Dict[str, Any] = {"api_key": s["api_key"]}
    if s["base_url"]:
        client_kwargs["base_url"] = s["base_url"]
    client = anthropic.AsyncAnthropic(**client_kwargs)

    summary_request = _build_summary_request(messages, custom_instructions)
    api_messages = _sanitize_messages([summary_request])

    # 非流式调用 —— 摘要不需要打字机效果，一次性拿到结果更快
    response = await client.messages.create(
        model=s["model"],
        max_tokens=4096,
        messages=api_messages,
        system=_COMPACT_SYSTEM_PROMPT,
    )

    # 取 assistant 返回的文本
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text

    return "（摘要生成失败：模型未返回文本。）"


async def _safe_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    """先试真实 API，失败则回退 mock。"""
    try:
        return await _real_summarize(messages, custom_instructions, context)
    except Exception:
        return await _mock_summarize(messages, custom_instructions, context)


async def _mock_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    extra = f"（按用户要求侧重：{custom_instructions}）" if custom_instructions else ""
    return f"【对话摘要】本次会话共 {len(messages)} 条消息，已压缩为本摘要。{extra}"


# 模块级、可替换：默认走 safe（真实 API / 回退 mock）。
summarize_conversation = _safe_summarize


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
