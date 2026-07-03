"""
演示：Write 工具（file_write.py）—— 创建 / 覆盖 / 权限 / 校验。

验证对象：
    · 文件写入主线（创建 + 覆盖）
    · 安全防线：必须先 Read 后 Write（已存在文件）
    · 参数校验：相对路径、目录、缺参数
    · 缓存更新（连续写入不需要重读）
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

# 让「直接运行 demo」时也能 import 同级 src 目录
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC_DIR))

from Tool import ToolUseContext, ToolUseContextOptions  # noqa: E402
from tools.file_write import FileWriteTool  # noqa: E402
from tools.file_read import FileReadTool  # noqa: E402

tool = FileWriteTool()
read_tool = FileReadTool()

PASS = 0
FAIL = 0


def check(label: str, ok: bool) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}")


async def main() -> None:
    global PASS, FAIL
    tmp = tempfile.mkdtemp()
    test_file = os.path.join(tmp, "test.txt")

    # 手动建一个文件，模拟「外部已有文件、工具没读过」
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("preexisting content")

    print("━" * 64)
    print("测试 1：参数校验")
    print("━" * 64)
    ctx = ToolUseContext(read_file_state={}, options=ToolUseContextOptions())

    err = await tool.validate_input({"file_path": "", "content": "x"}, ctx)
    check("空 file_path 被拒绝", err is not None and "缺少 file_path" in err)

    err = await tool.validate_input({"file_path": "rel/path.txt", "content": "x"}, ctx)
    check("相对路径被拒绝", err is not None and "绝对路径" in err)

    err = await tool.validate_input({"file_path": tmp, "content": "x"}, ctx)
    check("目录路径被拒绝", err is not None and "目录" in err)

    err = await tool.validate_input({"file_path": test_file}, ctx)
    check("缺少 content 被拒绝", err is not None and "缺少 content" in err)

    print("\n" + "━" * 64)
    print("测试 2：文件已存在但未 Read → 拒绝写入")
    print("━" * 64)
    err = await tool.validate_input({"file_path": test_file, "content": "overwrite"}, ctx)
    check("validate 拒绝未读覆盖", err is not None and "还没有被读过" in err)

    print("\n" + "━" * 64)
    print("测试 3：创建新文件（不存在 → 不需要先读）")
    print("━" * 64)
    new_file = os.path.join(tmp, "brand_new.txt")
    ctx2 = ToolUseContext(read_file_state={}, options=ToolUseContextOptions())
    r = await tool.call({"file_path": new_file, "content": "hello world\nline 2\n"}, ctx2)
    check("call 返回创建消息", "创建" in str(r.data))
    check("文件确实生成了", os.path.exists(new_file))
    with open(new_file, "r", encoding="utf-8") as f:
        check("文件内容正确", f.read() == "hello world\nline 2\n")
    # 缓存应更新
    check("写入后缓存已更新", new_file in ctx2.read_file_state)

    print("\n" + "━" * 64)
    print("测试 4：先 Read 后 Write → 覆盖成功")
    print("━" * 64)
    ctx3 = ToolUseContext(read_file_state={}, options=ToolUseContextOptions())
    await read_tool.call({"file_path": test_file}, ctx3)
    check("Read 后缓存有记录", test_file in ctx3.read_file_state)
    r2 = await tool.call({"file_path": test_file, "content": "updated content"}, ctx3)
    check("call 返回更新消息", "更新" in str(r2.data))
    with open(test_file, "r", encoding="utf-8") as f:
        check("文件内容被覆盖", f.read() == "updated content")

    print("\n" + "━" * 64)
    print("测试 5：连续写入（缓存已刷新，不需要重读）")
    print("━" * 64)
    r3 = await tool.call({"file_path": test_file, "content": "third write"}, ctx3)
    check("第二次写入成功", "更新" in str(r3.data))
    with open(test_file, "r", encoding="utf-8") as f:
        check("内容正确", f.read() == "third write")

    print("\n" + "━" * 64)
    print("测试 6：staleness —— 外部改文件后应报错")
    print("━" * 64)
    # 模拟：先读 → 外部改 → 再写应报 staleness
    ext_file = os.path.join(tmp, "external.txt")
    with open(ext_file, "w", encoding="utf-8") as f:
        f.write("v1")
    ctx4 = ToolUseContext(read_file_state={}, options=ToolUseContextOptions())
    await read_tool.call({"file_path": ext_file}, ctx4)
    # 外部改动（绕过工具）
    import time
    time.sleep(0.01)  # 确保 mtime 会变
    with open(ext_file, "w", encoding="utf-8") as f:
        f.write("v2 externally modified")
    err2 = await tool.validate_input({"file_path": ext_file, "content": "v3"}, ctx4)
    check("staleness 被检测到", err2 is not None and "改动过" in err2)

    print("\n" + "━" * 64)
    print("测试 7：is_read_only / 权限标记")
    print("━" * 64)
    check("is_read_only() == False", not tool.is_read_only())
    check("name == 'Write'", tool.name == "Write")

    # 清理
    shutil.rmtree(tmp)

    print("\n" + "━" * 64)
    total = PASS + FAIL
    print(f"结果：{PASS} / {total} 通过" + (f"，{FAIL} 失败" if FAIL else ""))
    print("━" * 64)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
