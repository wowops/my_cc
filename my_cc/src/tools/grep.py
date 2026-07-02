"""
Grep 工具：用正则在文件【内容】里搜索（Glob 找文件名，Grep 找内容）。
真实 Claude Code 封装 ripgrep；教学版改用纯 Python（os.walk + re），换取零外部依赖。
对应 TS 源码 claude-code-main/src/tools/GrepTool/。

📖 整体实现思路、设计决策与取舍见：my_cc/docs/grep.md
（本文件内的注释只解释局部细节，不重复整体思路。）
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pydantic import Field

from Tool import BaseTool, ToolResult, ToolUseContext


TOOL_NAME = "Grep"

# 默认结果上限：不设 head_limit 时只回这么多条，避免塞爆上下文（对应 DEFAULT_HEAD_LIMIT）。
DEFAULT_HEAD_LIMIT = 250
# 单行超过这么多字符就截断（对应 ripgrep --max-columns 500）。
MAX_COLUMNS = 500
# 这些目录默认跳过（对应 ripgrep 默认遵守 .gitignore 的体感）。
# VCS 目录来自 TS 的 VCS_DIRECTORIES_TO_EXCLUDE；噪声目录是 Python/JS 生态里最常见的生成/依赖目录。
_DEFAULT_IGNORE_DIRS = {
    ".git", ".svn", ".hg", ".bzr", ".jj", ".sl",   # VCS
    ".venv", "venv", ".tox", ".eggs",               # Python
    "node_modules", ".next", ".nuxt",                # JS/TS
    "__pycache__", ".mypy_cache", ".pytest_cache",   # Python cache
    "dist", "build", ".turbo",                       # 构建产物
    ".idea", ".vscode",                              # IDE
    "target",                                        # Rust
}

# type 参数 → 后缀名映射（内置常见几种，对应 rg --type）。
_TYPE_EXTS: Dict[str, Tuple[str, ...]] = {
    "py": (".py", ".pyi"),
    "js": (".js", ".jsx", ".mjs", ".cjs"),
    "ts": (".ts", ".tsx"),
    "rust": (".rs",),
    "go": (".go",),
    "java": (".java",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh"),
    "md": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "html": (".html", ".htm"),
    "css": (".css", ".scss", ".less"),
    "sh": (".sh", ".bash"),
    "txt": (".txt",),
}


def _to_relative(abs_path: str) -> str:
    """cwd 下的路径转相对（省 token）；cwd 之外的保留绝对。"""
    try:
        rel = os.path.relpath(abs_path, os.getcwd())
    except ValueError:
        return abs_path
    return abs_path if rel.startswith("..") else rel


def _expand_braces(pat: str) -> List[str]:
    """单层 {a,b} 展开：*.{ts,tsx} → [*.ts, *.tsx]。不处理嵌套。"""
    m = re.search(r"\{([^{}]*)\}", pat)
    if not m:
        return [pat]
    head, tail = pat[: m.start()], pat[m.end():]
    return [head + opt + tail for opt in m.group(1).split(",")]


def _glob_filters(glob_str: str) -> List[str]:
    """把 glob 参数拆成若干 fnmatch 模式（照 TS：按空格/逗号分隔 + 花括号展开）。"""
    out: List[str] = []
    for raw in re.split(r"\s+", glob_str.strip()):
        if not raw:
            continue
        parts = [raw] if ("{" in raw and "}" in raw) else raw.split(",")
        for p in parts:
            if p:
                out.extend(_expand_braces(p))
    return out


def _apply_head_limit(items: List[Any], limit: Optional[int], offset: int):
    """对应 applyHeadLimit：limit=0 表示无限；返回 (切片后列表, 实际生效的 limit 或 None)。"""
    if limit == 0:
        return items[offset:], None
    eff = DEFAULT_HEAD_LIMIT if limit is None else limit
    sliced = items[offset: offset + eff]
    truncated = len(items) - offset > eff   # 只有真截断了才报 limit，提示模型可翻页
    return sliced, (eff if truncated else None)


def _fmt_limit(applied_limit: Optional[int], applied_offset: int) -> str:
    parts: List[str] = []
    if applied_limit is not None:
        parts.append(f"limit: {applied_limit}")
    if applied_offset:
        parts.append(f"offset: {applied_offset}")
    return ", ".join(parts)


class GrepTool(BaseTool):
    """用正则搜索文件内容；只读、可并行。"""

    name: str = TOOL_NAME
    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "在文件内容里搜索的正则表达式"},
                "path": {"type": "string", "description": "要搜索的文件或目录，默认当前工作目录"},
                "glob": {"type": "string", "description": '按文件名过滤，如 "*.js"、"*.{ts,tsx}"'},
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": 'content=匹配行，files_with_matches=文件路径(默认)，count=每文件匹配数',
                },
                "-B": {"type": "integer", "description": "每个匹配前显示的行数（仅 content 模式）"},
                "-A": {"type": "integer", "description": "每个匹配后显示的行数（仅 content 模式）"},
                "-C": {"type": "integer", "description": "前后各显示的行数（context 的别名）"},
                "context": {"type": "integer", "description": "每个匹配前后显示的行数（仅 content 模式）"},
                "-n": {"type": "boolean", "description": "显示行号（仅 content 模式，默认 true）"},
                "-i": {"type": "boolean", "description": "大小写不敏感"},
                "type": {"type": "string", "description": "按语言类型过滤，如 js/py/rust/go/java"},
                "head_limit": {
                    "type": "integer",
                    "description": "只取前 N 条（默认 250；传 0 表示不限制）",
                },
                "offset": {"type": "integer", "description": "跳过前 N 条再取（配合 head_limit 翻页）"},
                "multiline": {"type": "boolean", "description": "允许 . 跨行、模式跨行匹配（默认 false）"},
            },
            "required": ["pattern"],
        }
    )

    def is_read_only(self) -> bool:
        return True

    async def prompt(self, context: Optional[ToolUseContext] = None) -> str:
        return (
            "基于正则的内容搜索工具。\n"
            "- 用它做内容搜索；不要用 Bash 去跑 grep/rg。\n"
            "- 支持完整正则语法（Python re），如 \"log.*Error\"、\"def \\w+\"\n"
            "- 用 glob 参数（\"*.js\"、\"*.{ts,tsx}\"）或 type 参数（\"js\"、\"py\"）过滤文件\n"
            "- output_mode：content=匹配行，files_with_matches=文件路径(默认)，count=每文件匹配数\n"
            "- 跨行匹配请设 multiline:true"
        )

    async def get_description(self, args: Dict[str, Any], context: ToolUseContext) -> str:
        return f"正在搜索 {args.get('pattern')!r}"

    async def validate_input(
        self, args: Dict[str, Any], context: ToolUseContext
    ) -> Optional[str]:
        pattern = args.get("pattern")
        if not pattern:
            return "缺少 pattern 参数。"
        # 正则本身是否合法，提前编译验证（早报错好过搜到一半崩）
        flags = re.IGNORECASE if args.get("-i") else 0
        if args.get("multiline"):
            flags |= re.DOTALL
        try:
            re.compile(pattern, flags)
        except re.error as e:
            return f"正则表达式无效：{e}"
        path = args.get("path")
        if path:
            abs_path = os.path.expanduser(path)
            if not os.path.exists(abs_path):
                return f"路径不存在：{path}（当前工作目录 {os.getcwd()}）。"
        return None

    # ---- call：收集候选文件 → 逐文件搜索 → 按 output_mode 组装 ----
    async def call(
        self,
        args: Dict[str, Any],
        context: ToolUseContext,
        on_progress: Optional[Callable[[Any], None]] = None,
    ) -> ToolResult:
        pattern = args["pattern"]
        path = args.get("path")
        base = os.path.expanduser(path) if path else os.getcwd()
        output_mode = args.get("output_mode") or "files_with_matches"
        show_n = args.get("-n", True)
        offset = args.get("offset") or 0
        head_limit = args.get("head_limit")  # None=默认250；0=无限
        multiline = bool(args.get("multiline"))

        # 上下文行数：-C/context 优先于 -A/-B（对应 TS 的优先级）
        before = after = 0
        if output_mode == "content":
            ctx = args.get("context")
            c = args.get("-C")
            if ctx is not None:
                before = after = ctx
            elif c is not None:
                before = after = c
            else:
                before = args.get("-B") or 0
                after = args.get("-A") or 0

        flags = re.IGNORECASE if args.get("-i") else 0
        if multiline:
            flags |= re.DOTALL
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(data=f"正则表达式无效：{e}")

        glob_pats = _glob_filters(args["glob"]) if args.get("glob") else None
        type_exts = _TYPE_EXTS.get(args["type"]) if args.get("type") else None

        # ---- 遍历候选文件，逐个搜索 ----
        # 每个命中文件记录：(rel, mtime, content_lines, match_count)
        hits: List[Tuple[str, float, List[str], int]] = []
        for abs_path, rel in self._iter_files(base, glob_pats, type_exts):
            if getattr(context, "is_aborted", False):
                break
            text = self._read_text(abs_path)
            if text is None:
                continue
            lines, count = self._search_file(
                rel, text, regex, multiline,
                want_content=(output_mode == "content"),
                before=before, after=after, show_n=show_n,
            )
            if count == 0:
                continue
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                mtime = 0.0
            hits.append((rel, mtime, lines, count))

        if output_mode == "content":
            return self._render_content(hits, before, after, head_limit, offset)
        if output_mode == "count":
            return self._render_count(hits, head_limit, offset)
        return self._render_files(hits, head_limit, offset)

    # ---- 文件枚举：跳过 VCS 目录，应用 glob/type 过滤 ----
    def _iter_files(self, base, glob_pats, type_exts):
        if os.path.isfile(base):
            if self._pass_filters(base, glob_pats, type_exts):
                yield base, _to_relative(base)
            return
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _DEFAULT_IGNORE_DIRS]  # 原地裁剪，os.walk 就不会进这些目录
            for name in files:
                p = os.path.join(root, name)
                if self._pass_filters(p, glob_pats, type_exts):
                    yield p, _to_relative(p)

    @staticmethod
    def _pass_filters(path, glob_pats, type_exts) -> bool:
        name = os.path.basename(path)
        if type_exts is not None and os.path.splitext(name)[1] not in type_exts:
            return False
        if glob_pats is not None and not any(fnmatch.fnmatch(name, g) for g in glob_pats):
            return False
        return True

    @staticmethod
    def _read_text(abs_path) -> Optional[str]:
        try:
            with open(abs_path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        if b"\x00" in raw[:8192]:   # 疑似二进制，跳过
            return None
        return raw.decode("utf-8", errors="replace")

    # ---- 单文件搜索：返回 (content 行列表, 匹配计数) ----
    def _search_file(self, rel, text, regex, multiline, want_content, before, after, show_n):
        lines = text.split("\n")
        if multiline:
            matched: Set[int] = set()
            count = 0
            for m in regex.finditer(text):
                count += 1
                s = text.count("\n", 0, m.start())
                e = text.count("\n", 0, m.end())
                matched.update(range(s, e + 1))
            if count == 0:
                return [], 0
            content = self._emit_lines(rel, lines, matched, before=0, after=0, show_n=show_n) \
                if want_content else []
            return content, count

        match_idx = {i for i, ln in enumerate(lines) if regex.search(ln)}
        if not match_idx:
            return [], 0
        content = self._emit_lines(rel, lines, match_idx, before, after, show_n) \
            if want_content else []
        return content, len(match_idx)

    @staticmethod
    def _emit_lines(rel, lines, match_idx: Set[int], before, after, show_n) -> List[str]:
        """把要输出的行（匹配行 + 上下文行）渲染成 ripgrep 风格的字符串列表。"""
        emit: Dict[int, bool] = {i: True for i in match_idx}  # idx -> 是否匹配行
        for i in match_idx:
            for j in range(max(0, i - before), i):
                emit.setdefault(j, False)
            for j in range(i + 1, min(len(lines), i + after + 1)):
                emit.setdefault(j, False)

        out: List[str] = []
        prev: Optional[int] = None
        for idx in sorted(emit):
            if prev is not None and idx != prev + 1:
                out.append("--")          # 不连续的块之间插分隔
            is_match = emit[idx]
            sep = ":" if is_match else "-"  # 匹配行用冒号，上下文行用连字符（仿 ripgrep）
            text = lines[idx]
            if len(text) > MAX_COLUMNS:
                text = text[:MAX_COLUMNS] + " [... 超长行已截断]"
            out.append(f"{rel}{sep}{idx + 1}{sep}{text}" if show_n else f"{rel}{sep}{text}")
            prev = idx
        return out

    # ---- 三种输出模式的最终组装（对应 mapToolResultToToolResultBlockParam）----
    def _render_content(self, hits, before, after, head_limit, offset) -> ToolResult:
        blocks: List[str] = []
        for i, (_, _, lines, _) in enumerate(hits):
            if i > 0 and (before or after):
                blocks.append("--")       # 文件块之间也分隔（仅在有上下文时）
            blocks.extend(lines)
        limited, applied_limit = _apply_head_limit(blocks, head_limit, offset)
        body = "\n".join(limited) or "No matches found"
        info = _fmt_limit(applied_limit, offset)
        if info:
            body += f"\n\n[Showing results with pagination = {info}]"
        return ToolResult(data=body)

    def _render_count(self, hits, head_limit, offset) -> ToolResult:
        count_lines = [f"{rel}:{count}" for rel, _, _, count in hits]
        limited, applied_limit = _apply_head_limit(count_lines, head_limit, offset)
        total = sum(int(l.rsplit(":", 1)[1]) for l in limited)
        files = len(limited)
        raw = "\n".join(limited) or "No matches found"
        occ = "occurrence" if total == 1 else "occurrences"
        fw = "file" if files == 1 else "files"
        summary = f"\n\nFound {total} total {occ} across {files} {fw}."
        info = _fmt_limit(applied_limit, offset)
        if info:
            summary += f" with pagination = {info}"
        return ToolResult(data=raw + summary)

    def _render_files(self, hits, head_limit, offset) -> ToolResult:
        # 按修改时间降序，相同按路径名兜底（确定可复现）
        ordered = sorted(hits, key=lambda h: (-h[1], h[0]))
        names = [rel for rel, _, _, _ in ordered]
        limited, applied_limit = _apply_head_limit(names, head_limit, offset)
        if not limited:
            return ToolResult(data="No files found")
        info = _fmt_limit(applied_limit, offset)
        head = f"Found {len(limited)} file{'s' if len(limited) != 1 else ''}"
        if info:
            head += f" {info}"
        return ToolResult(data=head + "\n" + "\n".join(limited))
