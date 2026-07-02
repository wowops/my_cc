"""
Bash 工具：执行 shell 命令；只读命令自动放行，写/危险命令弹窗征求同意。
是权限系统真正派上用场的地方。对应 TS 源码 claude-code-main/src/tools/BashTool/。

📖 整体实现思路、设计决策与取舍见：my_cc/docs/bash.md
（本文件内的注释只解释局部细节，不重复整体思路。）
"""

from __future__ import annotations

import os
import re
import time
import shutil
import asyncio
from typing import Any, Callable, Dict, List, Optional

from pydantic import Field

from Tool import (
    BaseTool,
    ToolResult,
    ToolUseContext,
    PermissionResult,
    PermissionBehavior,
    PermissionMode,
)


# 对应 toolName.ts / timeouts.ts
TOOL_NAME = "Bash"
DEFAULT_TIMEOUT_MS = 120_000          # 默认 2 分钟（对应 getDefaultBashTimeoutMs）
MAX_TIMEOUT_MS = 600_000              # 最多 10 分钟
MAX_OUTPUT_CHARS = 30_000             # 输出过长就截断，避免撑爆上下文


# =============================================================================
# 一、决定用哪个 shell 来跑命令
# =============================================================================
# 真 CC 在类 Unix 上固定用 bash。我们在 Windows 上优先找 Git Bash（这样模型生成的
# ls/cat/grep 等命令能正常跑）；找不到就退回 PowerShell。把选择固定在模块加载时，
# 并写进给模型的 prompt，让模型知道自己在跟哪个 shell 说话。
def _resolve_shell() -> tuple[str, List[str]]:
    """返回 (shell 名称, 启动参数前缀)。用法： [*前缀, command] 传给子进程。"""
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return "bash", [path, "-c"]

    which_bash = shutil.which("bash")
    # 跳过 System32 下的 bash.exe —— 那是 WSL 入口，行为和路径都不一样，容易踩坑。
    if which_bash and "System32" not in which_bash and "system32" not in which_bash:
        return "bash", [which_bash, "-c"]

    if os.name == "nt":
        ps = shutil.which("powershell") or "powershell"
        return "powershell", [ps, "-NoProfile", "-Command"]

    return "sh", ["/bin/sh", "-c"]


_SHELL_LABEL, _SHELL_ARGV = _resolve_shell()


# =============================================================================
# 二、判断命令是不是「只读」—— 对应 isSearchOrReadBashCommand + checkReadOnlyConstraints
# =============================================================================
# 只读命令（看、搜、列）→ 自动放行 + 可并行。写命令 → 询问 + 串行。
# 真 CC 用 AST 精确解析；我们用「按操作符切分 + 查白名单」的朴素近似，够教学用。

# 看/读类
_READ_CMDS = {
    "cat", "head", "tail", "less", "more", "wc", "stat", "file", "strings",
    "jq", "awk", "cut", "sort", "uniq", "tr", "diff", "cmp",
    "md5sum", "sha1sum", "sha256sum", "od", "xxd",
}
# 搜索类
_SEARCH_CMDS = {"find", "grep", "rg", "ag", "ack", "locate", "which", "whereis"}
# 列目录类
_LIST_CMDS = {"ls", "tree", "du", "df"}
# 纯输出/状态，位置无关，不影响整体只读性（对应 BASH_SEMANTIC_NEUTRAL_COMMANDS）
_NEUTRAL_CMDS = {"echo", "printf", "true", "false", ":"}
# 其它公认只读的小工具
_MISC_READ_CMDS = {
    "pwd", "date", "whoami", "id", "hostname", "uname", "env", "printenv",
    "basename", "dirname", "realpath", "readlink", "ps", "history",
}
# git 的只读子命令（只收纯查询类，避开 branch/tag/config 这种可能带 -D/--unset 的）
_GIT_READ_SUBCMDS = {
    "status", "log", "diff", "show", "rev-parse", "describe", "blame",
    "ls-files", "ls-tree", "cat-file", "shortlog", "reflog", "whatchanged",
}

_OP_SPLIT = re.compile(r"\|\||&&|[|;&]|\n")          # 按 shell 操作符切分
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")  # 前导环境变量赋值 VAR=val


def _base_command(segment: str) -> str:
    """取一个命令片段的「基础命令名」：去掉前导 VAR=val 和 sudo，取第一个词。"""
    tokens = segment.strip().split()
    while tokens and (_ENV_ASSIGN.match(tokens[0]) or tokens[0] == "sudo"):
        tokens.pop(0)
    return tokens[0] if tokens else ""


def _is_read_only_command(command: str) -> bool:
    """整条命令是否纯只读：所有片段都得是只读命令，且不含写文件的重定向。"""
    command = command.strip()
    if not command:
        return False
    # 含输出重定向（> 或 >>）就是要写文件 → 不算只读。
    # （排除 2>&1 / >/dev/null 这类，简单起见：只要有“> 后面跟非 &”的写法就保守判为写）
    if re.search(r">>?\s*[^&\s]", command):
        return False

    saw_real_command = False
    for seg in _OP_SPLIT.split(command):
        base = _base_command(seg)
        if not base:
            continue
        if base in _NEUTRAL_CMDS:
            continue
        saw_real_command = True
        if base == "git":
            sub = ""
            toks = seg.strip().split()
            # 跳过 git 前面的全局 flag，找子命令
            for t in toks[1:]:
                if not t.startswith("-"):
                    sub = t
                    break
            if sub in _GIT_READ_SUBCMDS:
                continue
            return False
        if base in _READ_CMDS or base in _SEARCH_CMDS or base in _LIST_CMDS or base in _MISC_READ_CMDS:
            continue
        return False  # 出现任何一个非只读命令 → 整条不算只读

    return saw_real_command  # 只有 echo 之类中性命令也不当只读


# =============================================================================
# 三、高危特征识别 —— 浓缩自 bashSecurity.ts 的 validator（只挑最典型的几条）
# =============================================================================
def _dangerous_reasons(command: str) -> List[str]:
    """返回这条命令的高危原因列表（用于在询问时给用户标红警告）。空列表=没发现明显高危。"""
    reasons: List[str] = []
    c = command

    if re.search(r"\brm\b.*\s-{1,2}\w*[rf]", c) or re.search(r"\brm\b\s+-\w*[rf]", c):
        reasons.append("递归/强制删除文件（rm -rf）")
    elif re.search(r"\brm\b", c):
        reasons.append("删除文件（rm）")
    if re.search(r"\bsudo\b|\bsu\b", c):
        reasons.append("提权执行（sudo/su）")
    if re.search(r"\bdd\b", c):
        reasons.append("dd 直接写磁盘")
    if re.search(r"\bmkfs", c):
        reasons.append("格式化磁盘（mkfs）")
    if ":(){" in c or re.search(r":\s*\(\s*\)\s*\{", c):
        reasons.append("疑似 fork 炸弹")
    if re.search(r"\b(shutdown|reboot|halt|poweroff)\b", c):
        reasons.append("关机/重启")
    if re.search(r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh|python)", c):
        reasons.append("下载并直接执行远程脚本（curl|sh）")
    if re.search(r"\bchmod\b.*(777|-R)|\bchown\b.*-R", c):
        reasons.append("批量改权限/属主")
    if re.search(r"git\s+push\s+.*(--force|-f)\b|reset\s+--hard|clean\s+-\w*f|checkout\s+--\s", c):
        reasons.append("破坏性 git 操作")
    if "$(" in c or "`" in c:
        reasons.append("命令替换（$()/反引号），可能隐藏额外命令")

    return reasons


# =============================================================================
# 四、BashTool 本体
# =============================================================================
class BashTool(BaseTool):
    """执行 shell 命令。只读命令自动放行，写/危险命令弹窗征求用户同意。"""

    name: str = TOOL_NAME
    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "number",
                    "description": f"可选超时（毫秒），最大 {MAX_TIMEOUT_MS}",
                },
                "description": {
                    "type": "string",
                    "description": "用一句话描述这条命令在做什么（主动语态，5-10 字）",
                },
            },
            "required": ["command"],
        }
    )

    # bash 整体保守地算「写工具」；具体某条命令是否只读，看 is_concurrency_safe(args)。
    def is_read_only(self) -> bool:
        return False

    # 只读命令可并行；写命令串行。直接复用我们对“只读命令”的判断。
    def is_concurrency_safe(self, args: Dict[str, Any]) -> bool:
        return _is_read_only_command(args.get("command", ""))

    # --- 参数校验：纯字符串检查，不碰系统 ---
    async def validate_input(
        self, args: Dict[str, Any], context: "ToolUseContext"
    ) -> Optional[str]:
        if not (args.get("command") or "").strip():
            return "command 不能为空。"
        return None

    # --- 权限检查：本工具的核心。对应 bashToolHasPermission ---
    async def check_permissions(
        self, args: Dict[str, Any], context: "ToolUseContext"
    ) -> PermissionResult:
        pc = context.permission_context
        command = (args.get("command") or "").strip()
        read_only = _is_read_only_command(command)

        # 1) bypass：跳过一切检查
        if pc.mode == PermissionMode.BYPASS_PERMISSIONS and pc.is_bypass_permissions_mode_available:
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        # 2) plan：只读才放行，写命令一律拒绝
        if pc.mode == PermissionMode.PLAN and not read_only:
            return PermissionResult(
                behavior=PermissionBehavior.DENY,
                updated_input=args,
                message="计划模式（plan）下禁止执行会改动系统的命令。",
            )

        # 3) 只读命令：自动放行（对应真 CC 对 ls/cat/grep 等免确认）
        if read_only:
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        reasons = _dangerous_reasons(command)

        # 4) auto 模式：非高危自动放行
        if pc.mode == PermissionMode.AUTO and not reasons:
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        # 5) 其余（写/危险）：问用户。非交互式会话无法弹窗 → 返回 ASK 交上层。
        if context.options.is_non_interactive_session:
            return PermissionResult(
                behavior=PermissionBehavior.ASK,
                updated_input=args,
                message="该命令会改动系统，但当前为非交互式会话，无法征求授权。",
            )

        warn = ("\n   ⚠️  高危：" + "；".join(reasons)) if reasons else ""
        prompt = (
            f"\n⚠️  AI 想执行命令（shell: {_SHELL_LABEL}）：\n"
            f"    $ {command}{warn}\n"
            f"允许吗？(y/n): "
        )
        answer = input(prompt).strip().lower()
        if answer == "y":
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)
        return PermissionResult(
            behavior=PermissionBehavior.DENY,
            updated_input=args,
            message="用户拒绝了本次命令。",
        )

    # --- 真正执行：asyncio 子进程，支持超时 + Esc 中断 ---
    async def call(
        self,
        args: Dict[str, Any],
        context: "ToolUseContext",
        on_progress: Optional[Callable[[Any], None]] = None,
    ) -> ToolResult:
        command = (args.get("command") or "").strip()
        timeout_ms = min(int(args.get("timeout") or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)

        # 用 asyncio 子进程（而非阻塞的 subprocess.run）：这样命令运行时事件循环不被卡死，
        # spinner 还能转、Esc 监听协程还能跑——和我们「异步到底」的架构一致。
        proc = await asyncio.create_subprocess_exec(
            *_SHELL_ARGV,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )

        comm = asyncio.create_task(proc.communicate())
        start = time.monotonic()
        interrupted = False
        timed_out = False

        # 边等子进程、边盯着「是否被 Esc 中断」「是否超时」。
        while True:
            done, _ = await asyncio.wait({comm}, timeout=0.1)
            if comm in done:
                break
            if context.is_aborted:
                interrupted = True
                proc.kill()
                await comm
                break
            if (time.monotonic() - start) * 1000 > timeout_ms:
                timed_out = True
                proc.kill()
                await comm
                break

        out_bytes, err_bytes = comm.result()
        stdout = out_bytes.decode("utf-8", errors="replace")
        stderr = err_bytes.decode("utf-8", errors="replace")
        code = proc.returncode

        return ToolResult(
            data=_format_result(stdout, stderr, code, timeout_ms, timed_out, interrupted)
        )

    # --- 给模型的静态说明（注入 System Prompt）。精简自 getSimplePrompt ---
    async def prompt(self, context: Optional["ToolUseContext"] = None) -> str:
        return (
            f"在 shell（{_SHELL_LABEL}）里执行一条命令并返回输出。\n"
            "\n"
            "使用须知：\n"
            f"- 可选 timeout（毫秒，最大 {MAX_TIMEOUT_MS}）；默认 {DEFAULT_TIMEOUT_MS}ms。\n"
            "- 路径含空格要用双引号包起来。\n"
            "- 多条独立命令：分多次工具调用并行；有依赖：用 && 串联成一条。\n"
            "- 优先用专用工具而不是 shell：读文件用 Read（别用 cat），改文件用 Edit（别用 sed）。\n"
            "- 只读命令（ls/cat/grep/git status 等）会自动放行；会改动系统的命令会先征求用户同意。"
        )

    async def get_description(self, args: Dict[str, Any], context: "ToolUseContext") -> str:
        return args.get("description") or f"$ {args.get('command', '')}"


def _format_result(
    stdout: str,
    stderr: str,
    code: Optional[int],
    timeout_ms: int,
    timed_out: bool,
    interrupted: bool,
) -> str:
    """把子进程结果拼成给模型看的文本。"""
    parts: List[str] = []
    body = stdout.rstrip("\n")
    if body:
        parts.append(body)
    if stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip("\n"))

    notes: List[str] = []
    if timed_out:
        notes.append(f"⏱️ 命令超时（>{timeout_ms}ms）被终止。")
    if interrupted:
        notes.append("⛔ 命令被用户中断（Esc）。")
    if code not in (0, None) and not timed_out and not interrupted:
        notes.append(f"退出码：{code}")
    if notes:
        parts.append("\n".join(notes))

    text = "\n\n".join(p for p in parts if p) or "（命令执行成功，无输出）"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n\n…（输出过长，已截断到 {MAX_OUTPUT_CHARS} 字符）"
    return text
