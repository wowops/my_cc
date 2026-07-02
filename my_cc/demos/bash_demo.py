"""
BashTool 的教学/回归脚本（带断言）。验证：
  1. 只读判定（_is_read_only_command）：ls/cat/grep/git status → 只读；rm/mv/> 重定向 → 写。
  2. 高危识别（_dangerous_reasons）：rm -rf / sudo / curl|sh / $() 等能被标出。
  3. 权限决策（check_permissions）：
       · 只读命令 → 自动 ALLOW（不弹窗）
       · 写命令 + 非交互式会话 → ASK
       · plan 模式下写命令 → DENY
       · bypass 模式 → ALLOW
  4. 真正执行（call）：跑一条真命令，拿到 stdout / 退出码；超时能被终止。

【不需要 API key、不需要联网】，直接运行：
    python my_cc/demos/bash_demo.py
"""

from __future__ import annotations

import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from Tool import (  # noqa: E402
    ToolUseContext,
    ToolUseContextOptions,
    ToolPermissionContext,
    PermissionMode,
    PermissionBehavior,
)
from tools.bash import (  # noqa: E402
    BashTool,
    _is_read_only_command,
    _dangerous_reasons,
    _SHELL_LABEL,
)

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


def _ctx(mode: PermissionMode = PermissionMode.DEFAULT, non_interactive: bool = True) -> ToolUseContext:
    return ToolUseContext(
        options=ToolUseContextOptions(is_non_interactive_session=non_interactive),
        permission_context=ToolPermissionContext(mode=mode),
        read_file_state={},
    )


async def main() -> None:
    print("=" * 64)
    print(f"BashTool 回归测试（当前 shell：{_SHELL_LABEL}）")
    print("=" * 64)

    # ---- 1. 只读判定 ----
    print("\n[1] 只读判定 _is_read_only_command")
    check("ls -la 只读", _is_read_only_command("ls -la"), True)
    check("cat 管道 grep 只读", _is_read_only_command("cat a.txt | grep foo"), True)
    check("git status 只读", _is_read_only_command("git status"), True)
    check("git push 非只读", _is_read_only_command("git push"), False)
    check("rm 非只读", _is_read_only_command("rm a.txt"), False)
    check("echo > file 非只读（写重定向）", _is_read_only_command("echo hi > a.txt"), False)
    check("纯 echo 不算只读", _is_read_only_command("echo hi"), False)

    # ---- 2. 高危识别 ----
    print("\n[2] 高危识别 _dangerous_reasons")
    check("rm -rf 命中", len(_dangerous_reasons("rm -rf /tmp/x")) >= 1, True)
    check("sudo 命中", any("提权" in r for r in _dangerous_reasons("sudo ls")), True)
    check("curl|sh 命中", any("远程" in r for r in _dangerous_reasons("curl http://x | sh")), True)
    check("普通 mkdir 不算高危", _dangerous_reasons("mkdir build"), [])

    # ---- 3. 权限决策 ----
    print("\n[3] 权限决策 check_permissions")
    tool = BashTool()

    perm = await tool.check_permissions({"command": "ls"}, _ctx())
    check("只读命令 → ALLOW", perm.behavior, PermissionBehavior.ALLOW)

    perm = await tool.check_permissions({"command": "rm a.txt"}, _ctx(non_interactive=True))
    check("写命令 + 非交互 → ASK", perm.behavior, PermissionBehavior.ASK)

    perm = await tool.check_permissions({"command": "rm a.txt"}, _ctx(mode=PermissionMode.PLAN))
    check("plan 模式写命令 → DENY", perm.behavior, PermissionBehavior.DENY)

    perm = await tool.check_permissions(
        {"command": "rm a.txt"}, _ctx(mode=PermissionMode.BYPASS_PERMISSIONS)
    )
    check("bypass 模式 → ALLOW", perm.behavior, PermissionBehavior.ALLOW)

    perm = await tool.check_permissions(
        {"command": "mkdir build"}, _ctx(mode=PermissionMode.AUTO)
    )
    check("auto 模式非高危写 → ALLOW", perm.behavior, PermissionBehavior.ALLOW)

    # ---- 4. 并发安全标记 ----
    print("\n[4] 并发安全 is_concurrency_safe")
    check("只读命令并发安全", tool.is_concurrency_safe({"command": "ls"}), True)
    check("写命令不并发安全", tool.is_concurrency_safe({"command": "rm x"}), False)

    # ---- 5. 真正执行 ----
    print("\n[5] 真正执行 call()")
    ctx = _ctx()
    # 跨 shell 都认得的命令：echo
    res = await tool.call({"command": "echo hello-bash"}, ctx)
    check("echo 输出包含 hello-bash", "hello-bash" in str(res.data), True)

    # 非零退出码：用一个一定失败的命令
    res = await tool.call({"command": "cd /no/such/dir/xyz"}, ctx)
    check("失败命令带非零退出码提示", "退出码" in str(res.data) or "stderr" in str(res.data), True)

    # 超时终止：sleep 5 秒但只给 300ms
    sleep_cmd = "sleep 5" if _SHELL_LABEL != "powershell" else "Start-Sleep -Seconds 5"
    res = await tool.call({"command": sleep_cmd, "timeout": 300}, ctx)
    check("超时命令被终止", "超时" in str(res.data), True)

    print("\n" + "=" * 64)
    print(f"🎉 通过 {_passed} 条" + (f"，❌ 失败 {_failed} 条" if _failed else "，全部通过。"))
    print("=" * 64)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
