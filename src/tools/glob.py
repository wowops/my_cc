"""
Glob 工具：按文件名模式（如 **/*.py）查找文件，返回按修改时间排序的路径列表。
只管「按名字找」，不看内容——「按内容找」交给 Grep。对应 TS 源码 claude-code-main/src/tools/GlobTool/。

📖 整体实现思路、设计决策与取舍见：docs/glob.md
（本文件内的注释只解释局部细节，不重复整体思路。）
"""

from __future__ import annotations

import glob as _glob
import os
from typing import Any, Callable, Dict, List, Optional

from pydantic import Field

from Tool import BaseTool, ToolResult, ToolUseContext


TOOL_NAME = "Glob"
DEFAULT_LIMIT = 100   # 对应 globLimits.maxResults ?? 100


def _to_relative(abs_path: str) -> str:
    """把 cwd 下的绝对路径转相对（省 token）；cwd 之外的（结果以 .. 开头）保留绝对。"""
    try:
        rel = os.path.relpath(abs_path, os.getcwd())
    except ValueError:
        return abs_path  # Windows 跨盘符无法算相对路径
    return abs_path if rel.startswith("..") else rel


class GlobTool(BaseTool):
    """按 glob 模式查找文件，结果按修改时间倒序返回。"""

    name: str = TOOL_NAME
    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "用来匹配文件的 glob 模式，例如 **/*.js 或 src/**/*.ts",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "要搜索的目录。不传则用当前工作目录。"
                        "重要：想用默认目录就直接省略这个字段，不要填 \"undefined\" 或 \"null\"。"
                    ),
                },
            },
            "required": ["pattern"],
        }
    )

    # 只读工具 → 基类据此让 is_concurrency_safe 也为 True → 可并行调度。
    def is_read_only(self) -> bool:
        return True

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return (
            "快速的文件名模式匹配工具，适用于任意规模的代码库。\n"
            "- 支持 glob 模式，如 \"**/*.js\"、\"src/**/*.ts\"\n"
            "- 返回的文件路径按修改时间排序（最近改过的在前）\n"
            "- 当你需要按文件名规律找文件时用它；按文件内容找请用 Grep\n"
            "- pattern 必填；path 可选（不传则搜当前工作目录）"
        )

    async def get_description(self, args: Dict[str, Any], context: ToolUseContext) -> str:
        return f"正在查找文件 {args.get('pattern')}"

    # ---- validate_input：纯字符串检查 + 忠实照搬 TS 的一次目录 stat ----
    async def validate_input(
        self, args: Dict[str, Any], context: ToolUseContext
    ) -> Optional[str]:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return "缺少 pattern 参数。"
        path = args.get("path")
        if path:
            abs_path = os.path.expanduser(path)
            # 与 TS 一致：给了 path 就先确认它存在且是目录，尽早反馈
            if not os.path.exists(abs_path):
                return f"目录不存在：{path}（当前工作目录 {os.getcwd()}）。"
            if not os.path.isdir(abs_path):
                return f"路径不是目录：{path}"
        return None

    # ---- call：glob → 只留文件 → 按 mtime 排序 → 截断 → 相对化 ----
    async def call(
        self,
        args: Dict[str, Any],
        context: ToolUseContext,
        on_progress: Optional[Callable[[Any], None]] = None,
    ) -> ToolResult:
        pattern = args["pattern"].strip()
        path = args.get("path")
        base = os.path.expanduser(path) if path else os.getcwd()

        # root_dir 让 pattern 与 base 分离：pattern 保持纯净，返回相对 base 的路径。
        # recursive=True 是 ** 跨层匹配的开关。
        rel_matches = _glob.glob(pattern, root_dir=base, recursive=True)
        abs_matches = [os.path.join(base, m) for m in rel_matches]

        # 只保留文件（glob 可能匹配到目录），同时拿到 mtime 用于排序。
        files: List[tuple[str, float]] = []
        for p in abs_matches:
            try:
                if os.path.isfile(p):
                    files.append((p, os.path.getmtime(p)))
            except OSError:
                continue  # 枚举后被删等情况，跳过

        # 按 mtime 降序；相同则按路径名兜底，保证结果确定可复现。
        files.sort(key=lambda t: (-t[1], t[0]))

        truncated = len(files) > DEFAULT_LIMIT
        selected = files[:DEFAULT_LIMIT]
        filenames = [_to_relative(p) for p, _ in selected]

        if not filenames:
            return ToolResult(data="No files found")

        body = "\n".join(filenames)
        if truncated:
            body += "\n（结果已截断，请用更具体的路径或模式缩小范围。）"
        return ToolResult(data=body)
