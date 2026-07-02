"""
QueryEngine × commands 衔接演示。

展示 submit_message 经过斜线命令分发后的两条路径：
    · /help（local-jsx）—— 本地执行，直接吐出结果，【不进 Agent Loop】
    · 普通对话         —— 进入 Agent Loop，触发 mock 工具调用后收尾

【不需要 API key、不需要联网】，直接运行：

    python my_cc/demos/QueryEngine_demo.py
"""

from __future__ import annotations

import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from Tool import ToolUseContext  # noqa: E402
from QueryEngine import (  # noqa: E402
    QueryEngine,
    QueryEngineConfig,
)
from tools.file_read import FileReadTool  # noqa: E402


def _render(event) -> None:
    """把一个 StreamEvent 渲染到终端（极简 UI 层）。"""
    if event.type == "text_delta":
        print(event.text or "", end="", flush=True)
    elif event.type == "tool_use":
        tu = event.tool_use
        print(f"\n   🛠️  调用工具 {tu['name']}({tu.get('input')})")
    elif event.type == "tool_result":
        tr = event.tool_result
        print(f"   ↩️  工具结果：{tr['content']}")
    elif event.type == "message_stop":
        print()  # 换行
    elif event.type == "system":
        print(f"   ⚙️  {event.text}")


async def main() -> None:
    config = QueryEngineConfig(tools=[FileReadTool()])
    context = ToolUseContext(read_file_state={})
    engine = QueryEngine(config, context)

    print("=" * 64)
    print("场景 1：输入 /help —— 本地命令，结果直接显示，不发给模型")
    print("=" * 64)
    async for event in engine.submit_message("/help"):
        _render(event)
    print(f"\n（此时消息历史长度：{len(engine.messages)}，说明没有真正进对话循环）\n")

    print("=" * 64)
    print("场景 2：输入普通问题 —— 进入 Agent Loop，触发工具调用后收尾")
    print("=" * 64)
    async for event in engine.submit_message("帮我看看 edit_test.txt 里有什么"):
        _render(event)
    print(f"\n（此时消息历史长度：{len(engine.messages)}，包含 user/assistant/工具结果多轮）\n")

    print("=" * 64)
    print("场景 3：输入 /compact —— local 命令，把上面 4 条历史压缩成摘要")
    print("=" * 64)
    async for event in engine.submit_message("/compact"):
        _render(event)
    print(f"（压缩后消息历史长度：{len(engine.messages)}，原来的 4 条被『边界+摘要』取代）")
    print(f"   注意 engine.messages 真的变短了 —— 证明 context.messages 是同一对象，就地替换生效")


if __name__ == "__main__":
    asyncio.run(main())
