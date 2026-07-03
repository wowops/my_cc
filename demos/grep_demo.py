"""
GrepTool 的教学/回归脚本（带断言）。验证：
  1. validate_input：缺 pattern / 非法正则 / path 不存在 → 报错。
  2. files_with_matches（默认）：列出含匹配的文件，按 mtime 降序。
  3. content：匹配行 + 行号；-i 大小写不敏感；-C 上下文 + 连字符/分隔。
  4. count：每文件匹配数 + 汇总。
  5. glob / type 过滤；head_limit 分页；multiline 跨行；VCS 目录被跳过。
  6. check_permissions：只读工具默认放行。

【不需要 API key、不需要联网】，直接运行：
    python my_cc/demos/grep_demo.py
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
import tools.grep as grep_mod  # noqa: E402
from tools.grep import GrepTool  # noqa: E402

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


def checkt(label: str, cond: bool) -> None:
    """断言一个布尔条件为真。"""
    check(label, bool(cond), True)


def _ctx() -> ToolUseContext:
    return ToolUseContext(
        options=ToolUseContextOptions(is_non_interactive_session=True),
        permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
        read_file_state={},
    )


def _write(path: str, content: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


async def main() -> None:
    print("=" * 64)
    print("GrepTool 回归测试")
    print("=" * 64)

    tool = GrepTool()

    with tempfile.TemporaryDirectory() as tmp:
        # a.py：含两处 TODO；b.py：含一处 todo（小写）；notes.md：含 TODO；
        # sub/c.py：含 def foo；.git/x.py：应被跳过
        _write(os.path.join(tmp, "a.py"),
               "import os\n# TODO: fix this\ndef run():\n    pass  # TODO again\n", mtime=1000)
        _write(os.path.join(tmp, "b.py"),
               "def foo():\n    return 'todo lower'\n", mtime=2000)
        _write(os.path.join(tmp, "notes.md"), "# notes\n- TODO: write docs\n", mtime=1500)
        _write(os.path.join(tmp, "sub", "c.py"), "def foo():\n    return 1\n", mtime=1800)
        _write(os.path.join(tmp, ".git", "x.py"), "# TODO inside git\n", mtime=9999)

        # ---- 1. validate_input ----
        print("\n[1] validate_input")
        checkt("缺 pattern 报错", await tool.validate_input({}, _ctx()))
        checkt("非法正则报错", await tool.validate_input({"pattern": "("}, _ctx()))
        checkt("path 不存在报错",
               await tool.validate_input({"pattern": "x", "path": os.path.join(tmp, "no")}, _ctx()))
        check("合法输入通过", await tool.validate_input({"pattern": "TODO", "path": tmp}, _ctx()), None)

        # ---- 2. files_with_matches（默认）----
        print("\n[2] files_with_matches（默认模式）")
        r = await tool.call({"pattern": "TODO", "path": tmp}, _ctx())
        checkt("含匹配文件被列出（a.py / notes.md）",
               "a.py" in r.data and "notes.md" in r.data)
        checkt("不含匹配的 b.py 不在结果", "b.py" not in r.data)
        checkt(".git 目录被跳过", "x.py" not in r.data)
        checkt("结果头部 Found N files", r.data.startswith("Found 2 file"))

        # ---- 3. content 模式 ----
        print("\n[3] content 模式")
        r = await tool.call({"pattern": "TODO", "path": tmp, "output_mode": "content"}, _ctx())
        checkt("a.py 两处 TODO 都在", r.data.count("a.py:") >= 2)
        checkt("带行号（a.py:2:）", "a.py:2:" in r.data)

        r = await tool.call({"pattern": "todo", "path": tmp,
                             "output_mode": "content", "-i": True}, _ctx())
        checkt("-i 大小写不敏感：命中大写 TODO 与小写 todo",
               "a.py:" in r.data and "b.py:" in r.data)

        # -C 上下文：匹配行冒号，上下文行连字符
        r = await tool.call({"pattern": "fix this", "path": tmp,
                             "output_mode": "content", "-C": 1}, _ctx())
        checkt("上下文：匹配行 a.py:2: 用冒号", "a.py:2:" in r.data)
        checkt("上下文：上文行 a.py-1- 用连字符", "a.py-1-" in r.data)

        # ---- 4. count 模式 ----
        print("\n[4] count 模式")
        r = await tool.call({"pattern": "TODO", "path": tmp, "output_mode": "count"}, _ctx())
        checkt("a.py 计数为 2", "a.py:2" in r.data)
        checkt("汇总行 Found 3 total occurrences", "Found 3 total occurrences" in r.data)

        # ---- 5. glob / type 过滤 ----
        print("\n[5] glob / type 过滤")
        r = await tool.call({"pattern": "TODO", "path": tmp, "glob": "*.md"}, _ctx())
        checkt("glob=*.md 只剩 notes.md", "notes.md" in r.data and "a.py" not in r.data)
        r = await tool.call({"pattern": "foo", "path": tmp, "type": "py"}, _ctx())
        checkt("type=py 命中 b.py / sub/c.py", "b.py" in r.data and "c.py" in r.data)

        # ---- 6. head_limit 分页 ----
        print("\n[6] head_limit 分页")
        r = await tool.call({"pattern": "def foo", "path": tmp,
                             "output_mode": "content", "head_limit": 1}, _ctx())
        checkt("head_limit=1 只剩 1 行内容",
               len([l for l in r.data.splitlines() if l and not l.startswith("[")]) == 1)
        checkt("截断时带分页提示", "pagination" in r.data)

        # ---- 7. multiline 跨行 ----
        print("\n[7] multiline 跨行匹配")
        _write(os.path.join(tmp, "ml.txt"), "start\nMIDDLE\nend\n", mtime=1200)
        r = await tool.call({"pattern": r"start.*end", "path": tmp,
                             "glob": "ml.txt", "output_mode": "content"}, _ctx())
        checkt("默认逐行：跨行模式无匹配", r.data == "No matches found")
        r = await tool.call({"pattern": r"start.*end", "path": tmp,
                             "glob": "ml.txt", "output_mode": "content", "multiline": True}, _ctx())
        checkt("multiline=True：命中跨行", "ml.txt:" in r.data)

        # ---- 8. 无匹配 ----
        print("\n[8] 无匹配")
        r = await tool.call({"pattern": "ZZZ_nonexistent", "path": tmp}, _ctx())
        check("files 模式无匹配 → No files found", r.data, "No files found")

    # ---- 9. 权限 ----
    print("\n[9] check_permissions")
    res = await tool.check_permissions({"pattern": "x"}, _ctx())
    check("只读工具默认 ALLOW", res.behavior, PermissionBehavior.ALLOW)

    print("\n" + "=" * 64)
    print(f"结果：{_passed} 通过 / {_failed} 失败")
    print("=" * 64)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
