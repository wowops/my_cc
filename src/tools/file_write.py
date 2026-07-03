"""
Write 工具：创建或覆盖文件（完全重写，不是增量编辑）。

对应 TS 源码 claude-code-main/src/tools/FileWriteTool/FileWriteTool.ts。

📖 整体实现思路、设计决策与取舍见：docs/file_write.md
（本文件内的注释只解释局部细节，不重复整体思路。）
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from pydantic import Field

from Tool import BaseTool, ToolResult, ToolUseContext

TOOL_NAME = "Write"

# 对齐 file_edit.py 的 staleness 容差
_MTIME_TOLERANCE = 1e-6


class FileWriteTool(BaseTool):
    """创建新文件或覆盖已有文件。已有文件必须先 Read 才能 Write（防盲目覆盖）。

    Write vs Edit：
        - Write = 完整重写文件内容，适合新建文件或大改
        - Edit  = 精确字符串替换，适合小范围修改
    """

    name: str = TOOL_NAME

    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要写入文件的绝对路径（必须是绝对路径，不能是相对路径）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入文件的完整内容",
                },
            },
            "required": ["file_path", "content"],
        }
    )

    # ★ 写工具：is_read_only=False → is_concurrency_safe 也为 False → 被串行调度，
    #   且在权限系统里走「写操作」分支。
    def is_read_only(self) -> bool:
        return False

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return (
            "把完整内容写入一个文件（覆盖模式）。\n"
            "用法：\n"
            "- 如果目标文件已存在，写入前【必须】先用 Read 工具读过它，否则会报错。\n"
            "- 如果目标文件不存在，会自动创建（包括中间缺失的父目录）。\n"
            "- Write 会【完全覆盖】已有内容；如果只想改文件的一小部分，用 Edit 工具。\n"
            "- 参数：file_path（绝对路径）、content（要写入的完整内容）。"
        )

    async def get_description(self, args: Dict[str, Any], context: ToolUseContext) -> str:
        return f"正在写入文件 {args.get('file_path')}"

    # ---- validate_input：做绝大部分检查（除了真正写盘）----

    async def validate_input(
        self, args: Dict[str, Any], context: ToolUseContext
    ) -> Optional[str]:
        fp_raw = (args.get("file_path") or "").strip()
        if not fp_raw:
            return "缺少 file_path 参数。"
        if not os.path.isabs(fp_raw):
            return f"file_path 必须是绝对路径，而你给的是相对路径：{fp_raw}"

        content = args.get("content")
        if content is None:
            return "缺少 content 参数。"

        fp = os.path.expanduser(fp_raw)

        # 目录不是文件
        if os.path.isdir(fp):
            return f"{fp} 是目录，不能写入文件。"

        # 决策：文件已存在 → 必须先 Read 后 Write（和 Edit 同理）
        if os.path.exists(fp):
            cache = (
                context.read_file_state
                if isinstance(context.read_file_state, dict)
                else None
            )
            cached = cache.get(fp) if cache else None
            if not cached:
                return "这个文件还没有被读过。请先用 Read 工具读它，再写入它。"

            # staleness check：读完之后文件是否被外部改动过
            stale = self._staleness_error(fp, cached)
            if stale:
                return stale

        return None

    # ---- call：写文件 ----

    async def call(
        self,
        args: Dict[str, Any],
        context: ToolUseContext,
        on_progress: Optional[Callable[[Any], None]] = None,
    ) -> ToolResult:
        fp = os.path.expanduser(args["file_path"].strip())
        content = args["content"]

        existed = os.path.exists(fp)

        # 创建父目录（对齐 TS 的 mkdir on parent）
        parent = os.path.dirname(fp)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # 写入文件 — 始终 UTF-8、LF 换行。
        # TS 版也保持 LF 不转换（FileWriteTool.ts:304-305），我们一致。
        with open(fp, "w", encoding="utf-8", newline="") as f:
            f.write(content)

        # 写入后更新 read_file_state：让模型接下来可以直接 Edit 这个文件，
        # 不用再读一遍（和 TS 的行为一致——写入后时间戳刷新，不更新缓存的话
        # 紧接着的 Edit 会因为 mtime 比缓存的新而误报 staleness）。
        cache = (
            context.read_file_state
            if isinstance(context.read_file_state, dict)
            else None
        )
        if cache is not None:
            cache[fp] = self._cache_entry(fp, content)

        action = "更新" if existed else "创建"
        return ToolResult(data=f"已{action}文件 {args['file_path']}。")

    # ---- 内部小工具（复用 file_edit.py 的缓存格式）----

    @staticmethod
    def _cache_entry(fp: str, content: str) -> Dict[str, Any]:
        return {
            "content": content,
            "timestamp": os.path.getmtime(fp),
            "offset": None,
            "limit": None,
        }

    @staticmethod
    def _staleness_error(fp: str, cached: Dict[str, Any]) -> Optional[str]:
        """mtime 比读时更新 → 可能被外部改过。Windows 上做内容回退比较：内容没变就放行。"""
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            return "无法读取文件时间戳，请重新用 Read 工具读一遍。"

        if mtime <= cached.get("timestamp", 0) + _MTIME_TOLERANCE:
            return None  # 没动过

        # mtime 跳了——可能是杀毒/云同步触碰，做内容比较兜底
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                current = f.read()
        except OSError:
            return "无法读取文件内容以验证 staleness，请重新用 Read 工具读一遍。"

        cached_content = str(cached.get("content", ""))
        if current.replace("\r\n", "\n").splitlines() == cached_content.replace("\r\n", "\n").splitlines():
            return None  # mtime 变了但内容一致，放行

        return ("文件在你读取之后被改动过（可能是用户或外部程序改的）。"
                "请重新用 Read 工具读一遍，再尝试写入。")
