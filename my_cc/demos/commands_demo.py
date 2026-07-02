"""
commands.py 的教学演示脚本。

把斜线命令系统的几个核心特性各跑一遍，直观看到：
    · 三种命令类型（local / local-jsx / prompt）走不同的执行路径
    · 别名解析、memoize 缓存、is_enabled 动态开关
    · lazy-load 的「加载时机」（运行时会看到 📦 提示）

【不需要 API key、不需要联网】，直接运行：

    python my_cc/demos/commands_demo.py
"""

from __future__ import annotations

import os
import sys
import asyncio

# 让「直接 python 运行本文件」时也能 import 隔壁 src/ 里的 commands.py / Tool.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
# 本 demo 的重点之一就是「看见」懒加载发生的时机，所以主动打开调试提示（📦）。
# 真实 REPL 默认不开这个开关，界面才干净。
os.environ.setdefault("CC_DEBUG_LAZY", "1")
from Tool import ToolUseContext  # noqa: E402
from commands import (  # noqa: E402
    COMMANDS,
    dispatch_user_input,
    find_command,
    get_commands,
)


async def main() -> None:
    context = ToolUseContext(read_file_state={})
    context.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]
    # 给文件状态缓存塞一条假记录，演示 /clear 会连它一起清（read-before-edit 的依据）
    context.read_file_state["/fake/path.txt"] = {"timestamp": 0, "content": "x"}

    print("=" * 64)
    print("演示 1：/help（local-jsx）—— 本地渲染 UI，绝不发给模型")
    print("=" * 64)
    result = await dispatch_user_input("/help", context)
    print(f"→ type={result.type.value}")
    print(f"→ 本地输出：{result.local_output}\n")

    print("=" * 64)
    print("演示 2：/clear（local）—— 本地执行函数，返回文本")
    print("=" * 64)
    print(f"清空前消息数：{len(context.messages)}，文件缓存条目：{len(context.read_file_state)}")
    result = await dispatch_user_input("/clear", context)
    print(f"→ type={result.type.value}")
    print(f"→ 本地输出：{result.local_output}")
    print(f"清空后消息数：{len(context.messages)}，文件缓存条目：{len(context.read_file_state)}")
    assert len(context.messages) == 0, "/clear 应清空消息历史"
    assert len(context.read_file_state) == 0, "/clear 应同步清空文件状态缓存"
    print("✅ 断言：消息历史 + 文件状态缓存均已清空\n")

    print("=" * 64)
    print("演示 3：/compact（local）—— 调模型生成摘要，替换整段历史以释放上下文")
    print("=" * 64)
    # 演示 2 已清空历史，这里先补几条假消息，好看出压缩效果
    context.messages.extend([
        {"role": "user", "content": [{"type": "text", "text": "帮我重构登录模块"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "好的，我先看一下代码……"}]},
        {"role": "user", "content": [{"type": "text", "text": "顺便加上单元测试"}]},
    ])
    print(f"压缩前消息数：{len(context.messages)}")
    result = await dispatch_user_input("/compact 保留关键决定", context)
    print(f"→ type={result.type.value}")
    print(f"→ 本地输出：{result.local_output}")
    print(f"压缩后消息数：{len(context.messages)}（边界标记 + 摘要）")
    print(f"→ 摘要消息内容：{context.messages[-1]['content']}\n")

    print("=" * 64)
    print("演示 4：别名解析 —— /reset 是 /clear 的别名")
    print("=" * 64)
    cmd = find_command("/reset", COMMANDS())
    print(f"→ /reset 解析到命令：{cmd.name if cmd else '未找到'}\n")

    print("=" * 64)
    print("演示 5：memoize 缓存 —— COMMANDS() 多次调用返回同一个对象")
    print("=" * 64)
    print(f"→ 两次 COMMANDS() 是同一对象？ {COMMANDS() is COMMANDS()}\n")

    print("=" * 64)
    print("演示 6：is_enabled 动态开关 —— 设 DISABLE_COMPACT=1 后 /compact 消失")
    print("=" * 64)
    os.environ["DISABLE_COMPACT"] = "1"
    commands = await get_commands()
    names = [c.name for c in commands]
    print(f"→ 当前可用命令：{names}")
    print(f"→ /compact 还在吗？ {'compact' in names}")
    os.environ.pop("DISABLE_COMPACT", None)  # 恢复环境，避免影响其它演示


if __name__ == "__main__":
    asyncio.run(main())
