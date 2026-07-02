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
# 真实摘要器：流式调用 API 生成结构化摘要 + 带百分比的进度条。
# -----------------------------------------------------------------------------
# 对应 TS compactConversation() 的 streaming 路径：
#   queryModelWithStreaming(...) → 逐 token 产出 → onCompactProgress 更新进度。
# 我们用 anthropic SDK 的 stream() 拿到每个 text_delta，数 token 数 / 估算总量
# 算出百分比，推给 context.on_progress(msg, percent) 驱动终端进度条。
async def _real_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    """流式调用真实 API 生成对话摘要，带进度条。"""
    import anthropic
    from anthropic_api import _read_settings, _sanitize_messages

    s = _read_settings()

    client_kwargs: Dict[str, Any] = {"api_key": s["api_key"]}
    if s["base_url"]:
        client_kwargs["base_url"] = s["base_url"]
    client = anthropic.AsyncAnthropic(**client_kwargs)

    summary_request = _build_summary_request(messages, custom_instructions)
    api_messages = _sanitize_messages([summary_request])

    # 估算摘要输出长度：基于消息数量，每条约 60 token，上下限 100~2000
    estimated_tokens = max(100, min(2000, len(messages) * 60))

    p = context.on_progress  # 短路别名

    # —— 阶段 1：发送请求（0% → 5%） ——
    if p:
        p("发送摘要请求…", 0.0)

    # ★ 流式调用：逐 token 产出，逐个计数 → 实打实的进度条
    collected: List[str] = []
    token_count = 0

    async with client.messages.stream(
        model=s["model"],
        max_tokens=4096,
        messages=api_messages,
        system=_COMPACT_SYSTEM_PROMPT,
    ) as stream:
        # —— 阶段 2：等待首 token（5% → 10%） ——
        if p:
            p("等待模型响应…", 0.05)

        async for event in stream:
            # 用户按了 Esc → 中断
            if context.is_aborted:
                return "（摘要已中断。）"

            if event.type == "content_block_delta":
                delta = event.delta
                if getattr(delta, "type", None) == "text_delta":
                    collected.append(delta.text)
                    token_count += 1

                    # —— 阶段 3：流式生成中（10% → 95%） ——
                    # 百分比 = 10% + 85% × min(token_count / estimated_tokens, 1)
                    if p and token_count % 3 == 0:   # 每 3 个 token 更新一次，防刷屏
                        progress = min(token_count / estimated_tokens, 1.0)
                        pct = 0.10 + 0.85 * progress
                        p(f"生成摘要… {token_count} tokens", pct)

    full_text = "".join(collected)

    if not full_text:
        return "（摘要生成失败：模型未返回文本。）"

    # —— 阶段 4：完成（95% → 100%） ——
    if p:
        p(f"摘要完成（{token_count} tokens）", 1.0)

    return full_text


async def _safe_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    """先试真实 API，失败则回退 mock。对 mock 也模拟进度条，保持 UI 统一。"""
    # —— 阶段 0：准备压缩（0%） ——
    if context.on_progress:
        context.on_progress("准备压缩…", 0.0)
    try:
        return await _real_summarize(messages, custom_instructions, context)
    except Exception:
        # mock 回退也走一遍模拟进度，让 UI 不突兀
        return await _mock_summarize(messages, custom_instructions, context)


async def _mock_summarize(
    messages: List[Message],
    custom_instructions: str,
    context: "ToolUseContext",
) -> str:
    """mock 摘要：模拟三段进度让进度条走完（不需要联网）。"""
    import asyncio
    p = context.on_progress
    if p:
        p("格式化对话…", 0.10)
        await asyncio.sleep(0.05)
        p("生成摘要（mock）…", 0.40)
        await asyncio.sleep(0.05)
        p("替换历史…", 0.90)
        await asyncio.sleep(0.05)
        p("完成", 1.0)

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
