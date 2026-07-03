"""
FileEditTool 回归测试（带断言）。覆盖 8 个关键场景，验证「读 → 改」闭环。

【不需要 API key、不需要联网】，直接运行：

    python my_cc/demos/file_edit_demo.py

约定：所有临时文件写在系统临时目录，跑完即弃，不污染项目。
"""

from __future__ import annotations

import os
import sys
import time
import asyncio
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from Tool import ToolUseContext  # noqa: E402
from tools.file_read import FileReadTool  # noqa: E402
from tools.file_edit import FileEditTool  # noqa: E402


_passed = 0


def _check(name: str, got, expected) -> None:
    global _passed
    ok = got == expected
    mark = "✅" if ok else "❌"
    print(f"   {mark} {name}: 得到 {got!r}，期望 {expected!r}")
    if not ok:
        raise AssertionError(f"断言失败：{name}")
    _passed += 1


async def _read(tool: FileReadTool, ctx: ToolUseContext, path: str):
    """模拟模型先调 Read（会把内容写进 read_file_state，为 Edit 铺路）。"""
    err = await tool.validate_input({"file_path": path}, ctx)
    assert err is None, f"Read 预检不该报错：{err}"
    return await tool.call({"file_path": path}, ctx)


async def _validate(tool: FileEditTool, ctx: ToolUseContext, args):
    """跑 Edit 的参数预检，返回错误信息（None=通过）。"""
    return await tool.validate_input(args, ctx)


async def main() -> None:
    read_tool = FileReadTool()
    edit_tool = FileEditTool()
    tmp = tempfile.mkdtemp(prefix="cc_edit_demo_")

    print("=" * 64)
    print("场景 1：没读过就改 → validate 报错『先 Read』")
    print("=" * 64)
    f1 = os.path.join(tmp, "a.txt")
    with open(f1, "w", encoding="utf-8") as f:
        f.write("hello world\n")
    ctx = ToolUseContext(read_file_state={})
    err = await _validate(edit_tool, ctx, {"file_path": f1, "old_string": "hello", "new_string": "hi"})
    _check("未读先改被拦", err is not None and "还没有被读过" in err, True)

    print("\n" + "=" * 64)
    print("场景 2：先 Read 再 Edit → 成功，磁盘内容真的变了")
    print("=" * 64)
    await _read(read_tool, ctx, f1)
    _check("Read 已填充 read_file_state", f1 in ctx.read_file_state, True)
    err = await _validate(edit_tool, ctx, {"file_path": f1, "old_string": "world", "new_string": "Claude"})
    _check("读后改预检通过", err, None)
    result = await edit_tool.call({"file_path": f1, "old_string": "world", "new_string": "Claude"}, ctx)
    with open(f1, encoding="utf-8") as f:
        content = f.read()
    _check("文件内容已替换", content, "hello Claude\n")
    _check("返回成功文案", "已成功更新" in str(result.data), True)

    print("\n" + "=" * 64)
    print("场景 3：old_string == new_string → 报错『没有改动』")
    print("=" * 64)
    err = await _validate(edit_tool, ctx, {"file_path": f1, "old_string": "x", "new_string": "x"})
    _check("空改动被拦", err is not None and "没有需要改动" in err, True)

    print("\n" + "=" * 64)
    print("场景 4：多处匹配但 replace_all=false → 报错；设 true → 全替换")
    print("=" * 64)
    f2 = os.path.join(tmp, "b.txt")
    with open(f2, "w", encoding="utf-8") as f:
        f.write("cat cat cat\n")
    await _read(read_tool, ctx, f2)
    err = await _validate(edit_tool, ctx, {"file_path": f2, "old_string": "cat", "new_string": "dog"})
    _check("多处匹配未开 replace_all 被拦", err is not None and "找到 3 处匹配" in err, True)
    err = await _validate(edit_tool, ctx, {"file_path": f2, "old_string": "cat", "new_string": "dog", "replace_all": True})
    _check("开了 replace_all 预检通过", err, None)
    await edit_tool.call({"file_path": f2, "old_string": "cat", "new_string": "dog", "replace_all": True}, ctx)
    with open(f2, encoding="utf-8") as f:
        _check("全部替换", f.read(), "dog dog dog\n")

    print("\n" + "=" * 64)
    print("场景 5：找不到 old_string → 报错")
    print("=" * 64)
    err = await _validate(edit_tool, ctx, {"file_path": f2, "old_string": "无此内容", "new_string": "x"})
    _check("找不到被拦", err is not None and "找不到要替换的字符串" in err, True)

    print("\n" + "=" * 64)
    print("场景 6：新建文件（old_string=\"\"，文件不存在）→ 创建成功")
    print("=" * 64)
    f3 = os.path.join(tmp, "sub", "new.txt")  # 父目录 sub/ 不存在，应自动创建
    err = await _validate(edit_tool, ctx, {"file_path": f3, "old_string": "", "new_string": "全新内容"})
    _check("新建文件预检通过", err, None)
    await edit_tool.call({"file_path": f3, "old_string": "", "new_string": "全新内容"}, ctx)
    _check("新文件已落盘", os.path.exists(f3) and open(f3, encoding="utf-8").read(), "全新内容")

    print("\n" + "=" * 64)
    print("场景 7：对已存在文件用空 old_string 想『新建』→ 报错")
    print("=" * 64)
    err = await _validate(edit_tool, ctx, {"file_path": f1, "old_string": "", "new_string": "x"})
    _check("已存在文件不能新建", err is not None and "已存在且非空" in err, True)

    print("\n" + "=" * 64)
    print("场景 8：读完后文件被外部改动（mtime 变新）→ staleness 报错")
    print("=" * 64)
    f4 = os.path.join(tmp, "c.txt")
    with open(f4, "w", encoding="utf-8") as f:
        f.write("original\n")
    await _read(read_tool, ctx, f4)
    time.sleep(0.05)  # 确保 mtime 能拉开差距
    with open(f4, "w", encoding="utf-8") as f:        # 模拟用户/linter 在读之后改了文件
        f.write("changed by someone else\n")
    err = await _validate(edit_tool, ctx, {"file_path": f4, "old_string": "original", "new_string": "x"})
    _check("staleness 被拦", err is not None and "被改动过" in err, True)

    print(f"\n🎉 全部 {_passed} 条断言通过。")
    print(f"（临时目录：{tmp}，可自行删除）")


if __name__ == "__main__":
    asyncio.run(main())
