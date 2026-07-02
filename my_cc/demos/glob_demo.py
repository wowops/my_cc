"""
GlobTool 的教学/回归脚本（带断言）。验证：
  1. validate_input：pattern 缺失 / path 不存在 / path 是文件 → 报错；正常 → None。
  2. call：
       · "*.py" 只匹配当前层，"**/*.py" 跨层递归
       · 结果只含文件、不含目录
       · 按修改时间降序（新文件排前）
       · 无匹配 → "No files found"
       · 超过 limit → 截断 + 提示
  3. check_permissions：只读工具默认放行（ALLOW）。

【不需要 API key、不需要联网】，直接运行：
    python my_cc/demos/glob_demo.py
"""

from __future__ import annotations

import os
import sys
import asyncio
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from Tool import (  # noqa: E402
    ToolUseContext,
    ToolUseContextOptions,
    ToolPermissionContext,
    PermissionMode,
    PermissionBehavior,
)
import tools.glob as glob_mod  # noqa: E402
from tools.glob import GlobTool  # noqa: E402

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


def _ctx() -> ToolUseContext:
    return ToolUseContext(
        options=ToolUseContextOptions(is_non_interactive_session=True),
        permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        read_file_state={},
    )


def _touch(path: str, mtime: float) -> None:
    """建文件并设定明确的修改时间，让排序断言可复现。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")
    os.utime(path, (mtime, mtime))


def _basenames(data: str) -> list[str]:
    """把 call 返回的多行结果转成文件名列表（去掉截断提示行）。"""
    out = []
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith("（"):
            continue
        out.append(os.path.basename(line))
    return out


async def main() -> None:
    print("=" * 64)
    print("GlobTool 回归测试")
    print("=" * 64)

    tool = GlobTool()

    with tempfile.TemporaryDirectory() as tmp:
        # 布局：a.py(旧) b.py(新) readme.md  sub/c.py
        _touch(os.path.join(tmp, "a.py"), mtime=1000)
        _touch(os.path.join(tmp, "b.py"), mtime=2000)   # 比 a.py 新
        _touch(os.path.join(tmp, "readme.md"), mtime=1500)
        _touch(os.path.join(tmp, "sub", "c.py"), mtime=1800)

        # ---- 1. validate_input ----
        print("\n[1] validate_input")
        check("缺 pattern 报错", bool(await tool.validate_input({}, _ctx())), True)
        check("path 不存在报错",
              bool(await tool.validate_input({"pattern": "*.py", "path": os.path.join(tmp, "nope")}, _ctx())),
              True)
        check("path 是文件报错",
              bool(await tool.validate_input({"pattern": "*.py", "path": os.path.join(tmp, "a.py")}, _ctx())),
              True)
        check("合法输入通过（返回 None）",
              await tool.validate_input({"pattern": "*.py", "path": tmp}, _ctx()),
              None)

        # ---- 2. call：匹配规则 ----
        print("\n[2] call：匹配 / 递归 / 排序 / 目录过滤")
        r = await tool.call({"pattern": "*.py", "path": tmp}, _ctx())
        names = _basenames(r.data)
        check("*.py 只匹配当前层（2 个）", sorted(names), ["a.py", "b.py"])
        check("排序：新文件 b.py 在前", names[0], "b.py")

        r = await tool.call({"pattern": "**/*.py", "path": tmp}, _ctx())
        check("**/*.py 递归到 sub（3 个）", sorted(_basenames(r.data)), ["a.py", "b.py", "c.py"])

        r = await tool.call({"pattern": "*.md", "path": tmp}, _ctx())
        check("*.md 命中 readme.md", _basenames(r.data), ["readme.md"])

        r = await tool.call({"pattern": "*", "path": tmp}, _ctx())
        check("'*' 结果不含目录 sub", "sub" in _basenames(r.data), False)

        r = await tool.call({"pattern": "*.txt", "path": tmp}, _ctx())
        check("无匹配 → No files found", r.data, "No files found")

        # ---- 3. 截断 ----
        print("\n[3] 截断（临时把 limit 调到 2）")
        old_limit = glob_mod.DEFAULT_LIMIT
        glob_mod.DEFAULT_LIMIT = 2
        try:
            r = await tool.call({"pattern": "**/*.py", "path": tmp}, _ctx())
            check("超出 limit 时只剩 2 行", len(_basenames(r.data)), 2)
            check("超出 limit 时带截断提示", "截断" in r.data, True)
        finally:
            glob_mod.DEFAULT_LIMIT = old_limit

    # ---- 4. 权限：只读工具默认放行 ----
    print("\n[4] check_permissions")
    res = await tool.check_permissions({"pattern": "*.py"}, _ctx())
    check("只读工具默认 ALLOW", res.behavior, PermissionBehavior.ALLOW)
    check("is_read_only() 为 True", tool.is_read_only(), True)

    print("\n" + "=" * 64)
    print(f"结果：{_passed} 通过 / {_failed} 失败")
    print("=" * 64)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
