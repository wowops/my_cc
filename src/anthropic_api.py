"""
真实模型后端适配器（Anthropic 兼容端点）。

替换 QueryEngine.py 里的 mock_call_claude_api，让 Agent Loop 驱动【真正的大模型】。

==== 为什么是这个文件、为什么这么简单 ====
我们这个项目是在复现 Claude Code，内部消息一直用的就是 Anthropic 的 content blocks 格式
（text / tool_use / tool_result，见 QueryEngine.py 的 query_loop）。而 DeepSeek 官方提供了一个
【Anthropic 格式兼容】的端点 https://api.deepseek.com/anthropic —— 这正是 cc-switch 给官方
Claude Code 配置的端点。

于是「最难的消息格式翻译」几乎不存在：我们的消息原样就能发出去。这个适配器只剩两件小事：
    1) 把我们的 BaseTool 列表 → Anthropic 工具格式（name/description/input_schema，几乎 1:1）
    2) 把模型流式吐回的事件 → 我们的 StreamEvent（text_delta / tool_use / message_stop）

==== 怎么用（改环境变量即可切换任意 Anthropic 兼容厂商）====
    DeepSeek：
        ANTHROPIC_BASE_URL = https://api.deepseek.com/anthropic
        ANTHROPIC_AUTH_TOKEN = sk-xxxx        （或 ANTHROPIC_API_KEY）
        ANTHROPIC_MODEL = deepseek-v4-pro
    真 Claude：把 BASE_URL 留空、换成 Anthropic 官方 key、模型名换成 claude-xxx 即可。
这套环境变量名刻意和 cc-switch / 官方 Claude Code 保持一致，方便互通——等于用 Python 复现了
cc-switch「改配置就切模型」的思想。
"""

from __future__ import annotations

import os
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

import anthropic

# 复用 QueryEngine 里定义的事件/类型，保证产出的事件和 mock 完全同构。
from QueryEngine import StreamEvent, QueryEngineConfig, Message  # noqa: E402
from Tool import BaseTool, ToolUseContext  # noqa: E402


# =============================================================================
# 一、读配置：从环境变量拿 base_url / 密钥 / 模型名
# =============================================================================
def _read_settings() -> Dict[str, Any]:
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    # 兼容两种密钥变量名：cc-switch/官方用 AUTH_TOKEN，SDK 习惯用 API_KEY，两个都认。
    api_key = (
        os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    model = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro").strip()
    max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16384"))

    if not api_key:
        raise RuntimeError(
            "缺少 API 密钥。请设置环境变量 ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY）。\n"
            "DeepSeek 示例（PowerShell）：\n"
            '  $env:ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"\n'
            '  $env:ANTHROPIC_AUTH_TOKEN="sk-xxxx"\n'
            '  $env:ANTHROPIC_MODEL="deepseek-v4-pro"'
        )
    return {"base_url": base_url, "api_key": api_key, "model": model, "max_tokens": max_tokens}


# =============================================================================
# 二、工具转换：BaseTool → Anthropic 工具格式
# =============================================================================
#
# Anthropic 的工具格式是 {"name", "description", "input_schema"}。
# 我们的 BaseTool 正好有 name、input_schema，描述则用它的 prompt()（本就是写给模型看的）。
# 几乎一对一——这也是当初把工具设计成自带 schema + prompt() 的好处。
async def _to_anthropic_tools(tools: List[BaseTool]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tools:
        out.append({
            "name": t.name,
            "description": await t.prompt(),
            "input_schema": t.input_schema,
        })
    return out


# =============================================================================
# 三、消息清洗：发出去前去掉内部专用的标记消息
# =============================================================================
#
# 我们内部消息本就是 Anthropic 格式，绝大多数能原样发送。唯一例外：/compact 生成的
# 「边界标记」是一条 role="system" 的消息（见 commands/compact.py），它只是给本地用的标记，
# Anthropic 的 messages 数组只接受 user / assistant，所以发送前要过滤掉。
# （真正的摘要文字在另一条 role="user" 消息里，不会丢。）
def _sanitize_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue  # 丢弃 compact 边界等内部 system 标记
        cleaned.append({"role": m["role"], "content": m["content"]})
    return cleaned


# =============================================================================
# 四、主函数：调用真实模型，把流式响应翻译成 StreamEvent
# =============================================================================
#
# 签名和 mock_call_claude_api 完全一致，所以能直接塞进 config.call_claude_api，
# query_loop 一行都不用改。
async def call_anthropic_api(
    messages: List[Message],
    config: QueryEngineConfig,
    context: ToolUseContext,
) -> AsyncGenerator[StreamEvent, None]:
    s = _read_settings()

    # base_url 为空时不传，SDK 走 Anthropic 官方；非空则指向兼容端点（如 DeepSeek）。
    client_kwargs: Dict[str, Any] = {"api_key": s["api_key"]}
    if s["base_url"]:
        client_kwargs["base_url"] = s["base_url"]
    client = anthropic.AsyncAnthropic(**client_kwargs)

    tools = await _to_anthropic_tools(config.tools)
    api_messages = _sanitize_messages(messages)

    # 组装请求参数。system / tools 为空时不传，避免个别兼容端点对空值挑剔。
    req: Dict[str, Any] = {
        "model": s["model"],
        "max_tokens": s["max_tokens"],
        "messages": api_messages,
    }
    if config.system_prompt:
        req["system"] = config.system_prompt
    if tools:
        req["tools"] = tools

    # 当前正在累积的 tool_use 块（流式时 input 是一段段 JSON 字符串拼起来的）。
    cur_tool: Optional[Dict[str, Any]] = None
    cur_json = ""  # 当前 tool_use 的 input JSON 累积串

    # client.messages.stream(...) 是个异步上下文管理器，迭代它就能拿到一连串「事件」。
    async with client.messages.stream(**req) as stream:
        async for event in stream:
            # 用户按了 Ctrl+C → 立刻停（对应 query_loop 的 abort 检查）。
            if context.is_aborted:
                break

            etype = event.type

            # —— 新内容块开始：如果是 tool_use，初始化累积器 ——
            if etype == "content_block_start":
                block = event.content_block
                if getattr(block, "type", None) == "tool_use":
                    cur_tool = {"id": block.id, "name": block.name}
                    cur_json = ""

            # —— 内容增量：文本 or 工具入参的 JSON 片段 ——
            elif etype == "content_block_delta":
                delta = event.delta
                dtype = getattr(delta, "type", None)
                if dtype == "text_delta":
                    # AI 正在逐字说话 → 打字机效果
                    yield StreamEvent(type="text_delta", text=delta.text)
                elif dtype == "input_json_delta":
                    # 工具入参一段段流回来，先攒着，等块结束再整体解析
                    cur_json += delta.partial_json

            # —— 内容块结束：如果刚才在攒 tool_use，现在把它产出 ——
            elif etype == "content_block_stop":
                if cur_tool is not None:
                    try:
                        tool_input = json.loads(cur_json) if cur_json.strip() else {}
                    except json.JSONDecodeError:
                        tool_input = {}
                    yield StreamEvent(
                        type="tool_use",
                        tool_use={
                            "type": "tool_use",
                            "id": cur_tool["id"],
                            "name": cur_tool["name"],
                            "input": tool_input,
                        },
                    )
                    cur_tool = None
                    cur_json = ""

            # —— 整条 assistant 消息说完 ——
            elif etype == "message_stop":
                yield StreamEvent(type="message_stop")
