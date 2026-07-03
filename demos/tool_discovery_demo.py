"""
get_tools() 自动发现工具的教学/回归脚本（带断言）。验证：
  1. _is_concrete_tool 判据：BaseTool 本身 / 抽象子类 → False；真实工具类 → True。
  2. get_tools() 扫描 tools/ 包，恰好发现 6 个已知工具（Bash/Edit/Glob/Grep/Read/Write）。
  3. 结果稳定有序（按 name 排序）、每个都是 BaseTool 的具体实例。
  4. memoize：lru_cache 让多次调用返回同一对象（只扫描一次）。

【不需要 API key、不需要联网】，直接运行：
    python my_cc/demos/tool_discovery_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import main  # noqa: E402  被测对象：main.get_tools / main._is_concrete_tool
from Tool import BaseTool  # noqa: E402

_passed = 0
_failed = 0


def check(label: str, got, expected) -> None:
    global _passed, _failed
    ok = got == expected
    print(f"   {'✅' if ok else '❌'} {label}: 得到 {got!r}，期望 {expected!r}")
    if ok:
        _passed += 1
    else:
        _failed += 1


# 一个「没实现抽象方法」的子类：仍是抽象类，应被判据排除。
class _AbstractTool(BaseTool):
    pass


def run() -> None:
    print("=" * 64)
    print("get_tools() 自动发现工具 回归测试")
    print("=" * 64)

    # ---- 1. _is_concrete_tool 判据 ----
    print("\n[1] _is_concrete_tool 判据")
    check("BaseTool 本身 → False", main._is_concrete_tool(BaseTool), False)
    check("未实现抽象方法的子类 → False", main._is_concrete_tool(_AbstractTool), False)
    check("非类（字符串）→ False", main._is_concrete_tool("Read"), False)

    tools = main.get_tools()
    sample_cls = type(tools[0])  # 一个真实工具类
    check(f"真实工具类 {sample_cls.__name__} → True", main._is_concrete_tool(sample_cls), True)

    # ---- 2. 扫描结果 ----
    print("\n[2] get_tools() 扫描结果")
    names = [t.name for t in tools]
    check("发现 6 个工具", len(tools), 6)
    check("恰好是这 6 个", sorted(names), ["Bash", "Edit", "Glob", "Grep", "Read", "Write"])

    # ---- 3. 有序 + 都是具体实例 ----
    print("\n[3] 稳定有序 + 类型正确")
    check("按 name 排序", names, sorted(names))
    check("都是 BaseTool 实例", all(isinstance(t, BaseTool) for t in tools), True)
    check("无抽象实例", all(not __import__("inspect").isabstract(type(t)) for t in tools), True)

    # ---- 4. memoize ----
    print("\n[4] memoize（lru_cache）")
    check("多次调用返回同一对象", main.get_tools() is tools, True)

    print("\n" + "=" * 64)
    print(f"结果：{_passed} 通过 / {_failed} 失败")
    print("=" * 64)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    run()
