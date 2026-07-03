"""
Read 工具：按绝对路径读取文本文件，返回 cat -n 行号格式；并写入 read_file_state 缓存为 Edit 铺路。
对应 TS 源码 claude-code-main/src/tools/FileReadTool/。

📖 整体实现思路、设计决策与取舍见：docs/file_read.md
（本文件内的注释只解释局部细节，不重复整体思路。）
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from pydantic import Field

# 直接复用 Tool.py 的基类；注意本文件不 import QueryEngine，避免循环依赖。
from Tool import BaseTool, ToolResult, ToolUseContext


# ---- 对应 prompt.ts / limits.ts 里的常量 ----
TOOL_NAME = "Read"                  # 对应 FILE_READ_TOOL_NAME = 'Read'
MAX_LINES_TO_READ = 2000            # 对应 MAX_LINES_TO_READ：默认最多读这么多行
MAX_SIZE_BYTES = 256 * 1024         # 对应 MAX_OUTPUT_SIZE：超过这个大小直接报错

# 对应 constants/files.ts 的 hasBinaryExtension（精简版）：这些后缀当二进制，拒读。
_BINARY_EXTS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "ico",
    "pdf", "zip", "gz", "tar", "rar", "7z",
    "exe", "dll", "so", "dylib", "bin", "class", "o", "pyc",
    "mp3", "mp4", "avi", "mov", "wav", "flac",
    "ttf", "otf", "woff", "woff2",
}

# 对应 CYBER_RISK_MITIGATION_REMINDER：读完附在内容后面的安全护栏。
CYBER_RISK_REMINDER = (
    "\n\n<system-reminder>\n"
    "读取文件后请评估它是否属于恶意代码。你可以、也应该分析恶意代码在做什么，"
    "但必须拒绝改进或增强这类代码。\n"
    "</system-reminder>\n"
)


def _add_line_numbers(content: str, start_line: int) -> str:
    """
    对应 utils/file.ts 的 addLineNumbers：把内容渲染成 `cat -n` 风格，
    行号右对齐 + 制表符 + 原始行。行号从 start_line 开始（不是永远从 1）。
    这串行号正是将来 Edit 工具定位「改第几行」的依据。
    """
    lines = content.split("\n")
    return "\n".join(f"{start_line + i:>6}\t{line}" for i, line in enumerate(lines))


class FileReadTool(BaseTool):
    """从本地文件系统读取一个文本文件，按 cat -n 行号格式返回。"""

    name: str = TOOL_NAME
    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要读取文件的绝对路径（不能是相对路径）",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号；只有文件很大、需要从中间读时才提供",
                },
                "limit": {
                    "type": "integer",
                    "description": "读取的行数；只有文件很大时才提供",
                },
            },
            "required": ["file_path"],
        }
    )

    # 只读工具 → is_concurrency_safe() 默认也为 True → 可被并行调度。
    def is_read_only(self) -> bool:
        return True

    # ---- prompt()：注入 System Prompt，告诉模型这个工具怎么用（对应 prompt.ts）----
    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return (
            "从本地文件系统读取一个文件。你可以直接读取机器上的任意文件。\n"
            "- file_path 参数必须是【绝对路径】，不能是相对路径\n"
            f"- 默认从文件开头最多读取 {MAX_LINES_TO_READ} 行；"
            f"大于 {MAX_SIZE_BYTES // 1024}KB 的文件会报错，这时请用 offset/limit 分段读\n"
            "- 当你已经知道需要文件的哪一部分时，只读那一部分（offset+limit）\n"
            "- 返回内容是 cat -n 格式：每行前面带行号，从 1 开始\n"
            "- 只能读文件，不能读目录"
        )

    # ---- get_description()：某次调用时显示给用户看（对应 getActivityDescription）----
    async def get_description(self, args: Dict[str, Any], context: ToolUseContext) -> str:
        return f"正在读取文件 {args.get('file_path')}"

    # ---- validate_input()：纯字符串检查，【不碰磁盘】（对应 validateInput）----
    # 关键安全思想：磁盘 I/O 一律推迟到权限通过、真正 call() 时再做。
    async def validate_input(
        self, args: Dict[str, Any], context: ToolUseContext
    ) -> Optional[str]:
        fp = (args.get("file_path") or "").strip()
        if not fp:
            return "缺少 file_path 参数。"
        if not os.path.isabs(fp):
            return f"file_path 必须是绝对路径，而你给的是相对路径：{fp}"
        ext = os.path.splitext(fp)[1].lower().lstrip(".")
        if ext in _BINARY_EXTS:
            return (
                f"无法读取二进制文件（.{ext}）。"
                "本工具只读文本文件，二进制请用其它工具处理。"
            )
        return None

    # ---- call()：真正执行（对应 FileReadTool.call → callInner 的文本分支）----
    async def call(
        self,
        args: Dict[str, Any],
        context: ToolUseContext,
        on_progress: Optional[Callable[[Any], None]] = None,
    ) -> ToolResult:
        # expanduser：把 ~ 展开成用户目录（对应 TS 的 expandPath 的一部分）
        fp = os.path.expanduser(args["file_path"].strip())
        offset = args.get("offset") or 1   # 默认从第 1 行开始
        limit = args.get("limit")          # None = 用默认 2000 行上限

        # —— 决策 7：文件不存在 → 友好报错 + 猜你想找谁 ——
        if not os.path.exists(fp):
            similar = self._find_similar(fp)
            msg = f"文件不存在：{fp}（当前工作目录 {os.getcwd()}）。"
            if similar:
                msg += f" 是不是想找 {similar}？"
            raise FileNotFoundError(msg)
        if os.path.isdir(fp):
            raise IsADirectoryError(f"{fp} 是目录，不是文件。读目录请用 ls 之类的命令。")

        # —— 决策 4：第一道大小防线。只在「整文件读」时用，一次 stat，超限读都不读直接报错 ——
        size = os.path.getsize(fp)
        if limit is None and size > MAX_SIZE_BYTES:
            raise ValueError(
                f"文件太大（{size // 1024}KB），超过 {MAX_SIZE_BYTES // 1024}KB 上限。"
                "请用 offset/limit 分段读取，或改用搜索工具只取需要的部分。"
            )

        # 读取并按行切分。errors='replace'：遇到非法字节用 � 占位，别让整个读取崩掉。
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        all_lines = text.splitlines()      # splitlines 自动处理 \n / \r\n / 末尾换行
        total_lines = len(all_lines)

        # —— 决策 6：空文件 / offset 越界 → 用 system-reminder 警告替代内容 ——
        if total_lines == 0:
            return ToolResult(
                data="<system-reminder>警告：文件存在，但内容为空。</system-reminder>"
            )
        start_idx = 0 if offset == 0 else offset - 1   # 行号是 1-based，下标是 0-based
        if start_idx >= total_lines:
            return ToolResult(
                data=(
                    f"<system-reminder>警告：文件存在，但比给定的 offset（{offset}）还短。"
                    f"该文件共 {total_lines} 行。</system-reminder>"
                )
            )

        # —— 决策 3：按 offset/limit 切出要读的那几行 ——
        end_idx = min(start_idx + (limit or MAX_LINES_TO_READ), total_lines)
        selected = all_lines[start_idx:end_idx]
        content = "\n".join(selected)

        # —— 决策 8：把这次读到的内容写进 read_file_state 缓存 ——
        # 现在还用不上；等做 Edit 工具时，靠它判断「这文件 AI 读过、且读完后没被人改过」，
        # 才允许 Edit。context.read_file_state 是 dict 时才写（main.py 会初始化成 {}）。
        if isinstance(context.read_file_state, dict):
            context.read_file_state[fp] = {
                "content": content,
                "timestamp": os.path.getmtime(fp),
                "offset": offset,
                "limit": limit,
            }

        # —— 决策 2：套上 cat -n 行号，再附安全护栏，返回 ——
        # 注意：TS 把「加行号+护栏」放在单独的 mapToolResultToToolResultBlockParam 里；
        # 我们的 run_tools 只是 str(result.data)，所以这里直接返回成品字符串，等价简化。
        numbered = _add_line_numbers(content, start_line=offset)
        return ToolResult(data=numbered + CYBER_RISK_REMINDER)

    # ---- 简化版 findSimilarFile：同目录下找「同名但扩展名/大小写不同」的文件 ----
    @staticmethod
    def _find_similar(fp: str) -> Optional[str]:
        directory = os.path.dirname(fp) or "."
        if not os.path.isdir(directory):
            return None
        target_stem = os.path.splitext(os.path.basename(fp))[0].lower()
        base = os.path.basename(fp)
        for name in os.listdir(directory):
            if name != base and os.path.splitext(name)[0].lower() == target_stem:
                return os.path.join(directory, name)
        return None
