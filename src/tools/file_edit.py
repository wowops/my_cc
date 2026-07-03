"""
Edit 工具：对文件做精确字符串替换（old_string → new_string），与 Read 构成「读 → 改」闭环。
对应 TS 源码 claude-code-main/src/tools/FileEditTool/。

📖 整体实现思路、设计决策与取舍见：docs/file_edit.md
（本文件内的注释只解释局部细节，不重复整体思路。）
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from pydantic import Field

from Tool import BaseTool, ToolResult, ToolUseContext


# 对应 constants.ts
TOOL_NAME = "Edit"
FILE_UNEXPECTEDLY_MODIFIED_ERROR = (
    "文件在你读取之后被改动过（可能是你自己、用户或 linter 改的）。"
    "请重新用 Read 工具读一遍，再尝试编辑。"
)

# 时间戳比较容差：mtime 是浮点秒，避免极小误差造成误判。
_MTIME_TOLERANCE = 1e-6


def _read_text(fp: str) -> tuple[str, bool]:
    """读文件，返回（规整成 \\n 的文本, 原文件是否用 CRLF）。简化版：只认 UTF-8。"""
    with open(fp, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    uses_crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), uses_crlf


def _write_text(fp: str, text: str, uses_crlf: bool) -> None:
    """写回磁盘，按原文件的换行风格还原（对应 TS 的 writeTextContent 保留 lineEndings）。"""
    out = text.replace("\n", "\r\n") if uses_crlf else text
    with open(fp, "wb") as f:
        f.write(out.encode("utf-8"))


def _find_similar(fp: str) -> Optional[str]:
    """同目录下找『同名但扩展名/大小写不同』的文件（对应 findSimilarFile）。"""
    directory = os.path.dirname(fp) or "."
    if not os.path.isdir(directory):
        return None
    target_stem = os.path.splitext(os.path.basename(fp))[0].lower()
    base = os.path.basename(fp)
    for name in os.listdir(directory):
        if name != base and os.path.splitext(name)[0].lower() == target_stem:
            return os.path.join(directory, name)
    return None


class FileEditTool(BaseTool):
    """对文件做精确字符串替换；改前必须先用 Read 读过。"""

    name: str = TOOL_NAME
    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要编辑文件的绝对路径",
                },
                "old_string": {
                    "type": "string",
                    "description": "要被替换掉的原文（新建文件时传空字符串）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换成的新内容",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换文件中所有出现处（默认 false，只替换唯一的一处）",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }
    )

    # ★ 写工具：is_read_only=False → is_concurrency_safe 也为 False → 被串行调度，
    #   且在权限系统里走「写操作」分支。
    def is_read_only(self) -> bool:
        return False

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return (
            "对文件做精确的字符串替换。\n"
            "用法：\n"
            "- 编辑前【必须】先用 Read 工具读过该文件；没读过会直接报错。\n"
            "- old_string 必须和文件里的内容【逐字符精确匹配】，包括缩进。\n"
            "  ⚠️ Read 的输出每行前面有『行号 + 制表符』前缀——old_string / new_string 里"
            "【绝对不要】包含这个前缀，只写制表符之后真正的文件内容。\n"
            "- 若 old_string 在文件里不唯一，编辑会失败：要么提供更多上下文让它唯一，"
            "要么设 replace_all=true 替换所有出现。\n"
            "- 新建文件：把 old_string 设为空字符串、new_string 设为文件内容（仅当文件不存在时）。\n"
            "- 参数：file_path（绝对路径）、old_string、new_string、replace_all（可选，默认 false）。"
        )

    async def get_description(self, args: Dict[str, Any], context: ToolUseContext) -> str:
        return f"正在编辑文件 {args.get('file_path')}"

    # ---- validate_input：做绝大部分检查（除了真正写盘）----
    async def validate_input(
        self, args: Dict[str, Any], context: ToolUseContext
    ) -> Optional[str]:
        fp_raw = (args.get("file_path") or "").strip()
        if not fp_raw:
            return "缺少 file_path 参数。"
        if not os.path.isabs(fp_raw):
            return f"file_path 必须是绝对路径，而你给的是相对路径：{fp_raw}"

        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if old_string is None or new_string is None:
            return "必须同时提供 old_string 和 new_string（新建文件时 old_string 传空字符串）。"
        replace_all = bool(args.get("replace_all", False))

        # 决策 5：没有改动
        if old_string == new_string:
            return "old_string 和 new_string 完全相同，没有需要改动的地方。"

        fp = os.path.expanduser(fp_raw)
        exists = os.path.exists(fp)

        # 决策 6 + 7（文件不存在）：空 old_string = 新建文件；否则友好报错
        if not exists:
            if old_string == "":
                return None  # 合法：将创建新文件
            similar = _find_similar(fp)
            msg = f"文件不存在：{fp}（当前工作目录 {os.getcwd()}）。"
            if similar:
                msg += f" 是不是想找 {similar}？"
            return msg

        if os.path.isdir(fp):
            return f"{fp} 是目录，不能编辑。"

        try:
            normalized, _ = _read_text(fp)
        except OSError as e:
            return f"读取文件失败：{e}"

        # 决策 6（文件已存在）：给了空 old_string 想新建，但文件已有内容 → 拒绝
        if old_string == "":
            if normalized.strip() != "":
                return "无法新建文件——该文件已存在且非空。"
            return None  # 空文件 + 空 old_string：合法（往空文件里填内容）

        if fp.endswith(".ipynb"):
            return "这是 Jupyter Notebook，请用专门的 NotebookEdit 工具来改。"

        # 决策 2：必须先 Read 后 Edit
        cache = context.read_file_state if isinstance(context.read_file_state, dict) else None
        cached = cache.get(fp) if cache else None
        if not cached:
            return "这个文件还没有被读过。请先用 Read 工具读它，再编辑它。"

        # 决策 3：staleness —— 读完之后文件是否被改过
        stale = self._staleness_error(fp, normalized, cached)
        if stale:
            return stale

        # 决策 4：唯一性
        count = normalized.count(old_string)
        if count == 0:
            return f"在文件里找不到要替换的字符串。\nold_string:\n{old_string}"
        if count > 1 and not replace_all:
            return (
                f"找到 {count} 处匹配，但 replace_all=false。"
                "要全部替换请设 replace_all=true；要只改一处，请在 old_string 里"
                f"加入更多上下文使它唯一。\nold_string:\n{old_string}"
            )
        return None

    # ---- call：原子读-改-写 ----
    async def call(
        self,
        args: Dict[str, Any],
        context: ToolUseContext,
        on_progress: Optional[Callable[[Any], None]] = None,
    ) -> ToolResult:
        fp = os.path.expanduser(args["file_path"].strip())
        old_string = args["old_string"]
        new_string = args["new_string"]
        replace_all = bool(args.get("replace_all", False))

        cache = context.read_file_state if isinstance(context.read_file_state, dict) else None

        # 决策 6：新建文件分支（validate 已确保此时 old_string=="" 且文件不存在）
        if not os.path.exists(fp):
            parent = os.path.dirname(fp)
            if parent:
                os.makedirs(parent, exist_ok=True)
            _write_text(fp, new_string, uses_crlf=False)
            if cache is not None:
                cache[fp] = self._cache_entry(fp, new_string)
            return ToolResult(data=f"已创建新文件 {args['file_path']}。")

        # 编辑已有文件：进入原子段，这中间不做任何 await，避免并发交错。
        normalized, uses_crlf = _read_text(fp)

        # 决策 7：写之前【再查一次】staleness（validate 与 call 之间文件可能又被改）
        if cache is not None:
            cached = cache.get(fp)
            if cached and self._staleness_error(fp, normalized, cached):
                raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)

        # 执行替换
        if old_string == "":
            updated = new_string  # 往空文件里填内容
        elif replace_all:
            updated = normalized.replace(old_string, new_string)
        else:
            updated = normalized.replace(old_string, new_string, 1)

        _write_text(fp, updated, uses_crlf)

        # 决策 8：更新缓存（新内容 + 新 mtime），让后续可连续 Edit 同一文件
        if cache is not None:
            cache[fp] = self._cache_entry(fp, updated)

        if replace_all:
            return ToolResult(data=f"文件 {args['file_path']} 已更新：所有匹配处都已替换。")
        return ToolResult(data=f"文件 {args['file_path']} 已成功更新。")

    # ---- 内部小工具 ----
    @staticmethod
    def _cache_entry(fp: str, content: str) -> Dict[str, Any]:
        # offset/limit = None 表示「整文件视图」，对应 TS 写缓存时的 offset:undefined。
        return {
            "content": content,
            "timestamp": os.path.getmtime(fp),
            "offset": None,
            "limit": None,
        }

    @staticmethod
    def _staleness_error(fp: str, current_normalized: str, cached: Dict[str, Any]) -> Optional[str]:
        """
        mtime 比上次读取时间新 → 可能被改过。但 Windows 上 mtime 会无故跳动
        （云同步、杀毒等），所以对「整读」做内容回退比较：内容没变就放行。
        """
        mtime = os.path.getmtime(fp)
        if mtime <= cached["timestamp"] + _MTIME_TOLERANCE:
            return None  # 没动过，安全
        is_full_read = cached.get("limit") is None
        if is_full_read and current_normalized.splitlines() == str(cached["content"]).splitlines():
            return None  # mtime 变了但内容一致，放行
        return FILE_UNEXPECTEDLY_MODIFIED_ERROR
