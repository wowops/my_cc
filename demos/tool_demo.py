"""
对应Tool.ts

重点演示 check_permissions() 的【六分支决策表】——这是 Tool.py 里逻辑最密、
最容易在重构时悄悄改坏的地方，平时其它 demo 又只覆盖了「default 模式 + 只读工具」
一种情况。本文件把六条分支逐一跑一遍，并用断言锁住预期结果，可当回归测试用。

顺带演示：ToolResult、is_concurrency_safe、validate_input、abort_event（取消）。

【不需要 API key、不需要联网】，直接运行：

    python my_cc/demos/tool_demo.py

退出码 0 = 全部断言通过；非 0 = 有分支行为变了，需要排查。
"""

from __future__ import annotations

import sys
import os
import asyncio
import threading
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from Tool import (  # noqa: E402
    BaseTool,
    ToolResult,
    ToolUseContext,
    ToolUseContextOptions,
    ToolPermissionContext,
    PermissionMode,
    PermissionBehavior,
)


# =============================================================================
# 一、三个最小工具：只读 / 写 / 破坏性
# =============================================================================
# 用最少的代码各造一个，覆盖 is_read_only / is_destructive 的不同组合，
# 好让 check_permissions 的不同分支都能被触发。

class ReadTool(BaseTool):
    name: str = "read_tool"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    def is_read_only(self) -> bool:
        return True  # 只读 → 天然并发安全

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return "一个只读工具。"

    async def get_description(self, args: Dict[str, Any], context: ToolUseContext) -> str:
        return "读取中…"

    async def call(self, args, context, on_progress=None) -> ToolResult:
        return ToolResult(data="read ok")


class WriteTool(BaseTool):
    name: str = "write_tool"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    # is_read_only 默认 False、is_destructive 默认 False → 「会改系统但不算破坏性」

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return "一个写文件工具。"

    async def get_description(self, args, context) -> str:
        return f"写入 {args.get('path')}"

    async def call(self, args, context, on_progress=None) -> ToolResult:
        return ToolResult(data="write ok")

    async def validate_input(self, args, context) -> Optional[str]:
        # 演示参数校验：缺 path 就报错（这条信息是给 AI 看的，让它重传）
        if not args.get("path"):
            return "缺少必填参数 path"
        return None


class DangerTool(BaseTool):
    name: str = "danger_tool"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    def is_destructive(self) -> bool:
        return True  # 破坏性 → 默认需要问用户

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return "一个破坏性工具（如 rm -rf）。"

    async def get_description(self, args, context) -> str:
        return "执行危险操作…"

    async def call(self, args, context, on_progress=None) -> ToolResult:
        return ToolResult(data="danger done")


# =============================================================================
# 二、小工具函数：造一个带指定权限配置的 context
# =============================================================================

def make_context(
    mode: PermissionMode = PermissionMode.DEFAULT,
    *,
    allow: Optional[Dict[str, set]] = None,
    deny: Optional[Dict[str, set]] = None,
    ask: Optional[Dict[str, set]] = None,
    non_interactive: bool = True,   # ★ 默认非交互，避免 ASK 分支卡在 input()
) -> ToolUseContext:
    return ToolUseContext(
        options=ToolUseContextOptions(is_non_interactive_session=non_interactive),
        permission_context=ToolPermissionContext(
            mode=mode,
            always_allow_rules=allow or {},
            always_deny_rules=deny or {},
            always_ask_rules=ask or {},
        ),
    )


# 断言计数器：把每条断言的结果打印出来，方便 debug 时一眼看出是哪条挂了。
_passed = 0


def check(label: str, got, expected) -> None:
    global _passed
    ok = got == expected
    mark = "✅" if ok else "❌"
    print(f"   {mark} {label}: 得到 {got}，期望 {expected}")
    assert ok, f"断言失败：{label}（得到 {got}，期望 {expected}）"
    _passed += 1


# =============================================================================
# 三、check_permissions 六分支逐一验证
# =============================================================================
async def demo_permissions() -> None:
    print("=" * 64)
    print("check_permissions 决策表：bypass > plan > 黑名单 > 白名单 > 询问/auto/破坏 > 默认")
    print("=" * 64)
    read, write, danger = ReadTool(), WriteTool(), DangerTool()
    B = PermissionBehavior

    # 分支 1：bypass —— 跳过一切检查，连破坏性工具也直接放行
    r = await danger.check_permissions({}, make_context(PermissionMode.BYPASS_PERMISSIONS))
    check("bypass 模式放行破坏性工具", r.behavior, B.ALLOW)

    # 分支 2：plan —— 只读放行、写操作拒绝
    r = await read.check_permissions({}, make_context(PermissionMode.PLAN))
    check("plan 模式放行只读工具", r.behavior, B.ALLOW)
    r = await write.check_permissions({"path": "a"}, make_context(PermissionMode.PLAN))
    check("plan 模式拒绝写工具", r.behavior, B.DENY)

    # 分支 3：黑名单优先于白名单（同时命中也拒绝）
    ctx = make_context(
        allow={"write_tool": {"*"}},
        deny={"write_tool": {"*"}},
    )
    r = await write.check_permissions({"path": "a"}, ctx)
    check("黑名单优先于白名单 → 拒绝", r.behavior, B.DENY)

    # 分支 4：白名单命中 → 放行
    r = await write.check_permissions({"path": "a"}, make_context(allow={"write_tool": {"*"}}))
    check("白名单命中放行写工具", r.behavior, B.ALLOW)

    # 分支 5a：auto 模式 + 非破坏性 → 自动放行
    r = await write.check_permissions({"path": "a"}, make_context(PermissionMode.AUTO))
    check("auto 模式自动放行非破坏性工具", r.behavior, B.ALLOW)

    # 分支 5b：破坏性工具 + 非交互会话 → 无法弹窗，返回 ASK（绝不阻塞 input()）
    r = await danger.check_permissions({}, make_context(PermissionMode.DEFAULT))
    check("破坏性工具在非交互会话下返回 ASK", r.behavior, B.ASK)

    # 分支 5c：命中询问名单 + 非交互 → ASK
    r = await read.check_permissions({}, make_context(ask={"read_tool": {"*"}}))
    check("命中询问名单返回 ASK", r.behavior, B.ASK)

    # 分支 6：默认 —— 只读工具直接放行
    r = await read.check_permissions({}, make_context(PermissionMode.DEFAULT))
    check("默认模式放行只读工具", r.behavior, B.ALLOW)

    # 分支 5d：默认模式下「普通写工具」也要授权（弹窗开关挂在 not is_read_only 上，
    #   而不是 is_destructive）。非交互会话无法弹窗 → 返回 ASK。
    #   ★ 这条是修复点：以前写工具会被静默放行（漏权限），现在写操作默认要问。
    r = await write.check_permissions({"path": "a"}, make_context(PermissionMode.DEFAULT))
    check("默认模式下普通写工具需授权（非交互→ASK）", r.behavior, B.ASK)


# =============================================================================
# 四、其它行为：并发安全 / 参数校验 / 取消
# =============================================================================
async def demo_misc() -> None:
    print("\n" + "=" * 64)
    print("其它：is_concurrency_safe / validate_input / abort_event")
    print("=" * 64)
    read, write = ReadTool(), WriteTool()

    # 只读工具默认并发安全；写工具默认不并发安全
    check("只读工具并发安全", read.is_concurrency_safe({}), True)
    check("写工具非并发安全", write.is_concurrency_safe({"path": "a"}), False)

    # 参数校验：缺 path 报错；给了 path 通过
    ctx = make_context()
    check("缺 path 时 validate_input 报错", await write.validate_input({}, ctx), "缺少必填参数 path")
    check("有 path 时 validate_input 通过", await write.validate_input({"path": "a"}, ctx), None)

    # ToolResult 正常返回
    res = await read.call({}, ctx)
    check("call 返回 ToolResult.data", res.data, "read ok")

    # 取消：abort_event 被 set 后，context.is_aborted 为 True
    ev = threading.Event()
    ctx2 = ToolUseContext(abort_event=ev)
    check("未取消时 is_aborted=False", ctx2.is_aborted, False)
    ev.set()
    check("set() 后 is_aborted=True", ctx2.is_aborted, True)


async def main() -> None:
    await demo_permissions()
    await demo_misc()
    print(f"\n🎉 全部 {_passed} 条断言通过。")


if __name__ == "__main__":
    asyncio.run(main())
