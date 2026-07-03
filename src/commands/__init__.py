"""
对应 TS 源码：
    claude-code-main/src/commands.ts        （命令注册中心 COMMANDS / getCommands）
    claude-code-main/src/types/command.ts    （Command 联合类型定义）
    claude-code-main/src/commands/*/index.ts  （各内置命令的元数据 + lazy-load）

本包的结构刻意对齐 TS 的目录布局：
    commands/__init__.py   ≈ commands.ts          —— 注册中心 + 类型 + 各命令【元数据】
    commands/clear.py      ≈ commands/clear/clear.ts     —— /clear 的【实现】
    commands/help.py       ≈ commands/help/help.tsx      —— /help 的【实现】
    commands/compact.py    ≈ commands/compact/compact.ts —— /compact 的【实现】+ 压缩逻辑
    commands/code_review.py≈ commands/insights.ts        —— prompt 命令的【实现】

关键点：每个命令的「实现」住在独立文件里，元数据只存 load() 这个指针。
load() 调用时才用 importlib 真正 import 那个文件——这才是【货真价实的 lazy-load】
（对应 TS 的 load: () => import('./compact.js')）。

配套讲义：src/commands.md（八节，建议先读讲义再读本文件）
演示见同目录的 commands_demo.py / queryengine_demo.py。
"""

from __future__ import annotations

import os
import sys
import functools
import importlib
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

# 让「直接运行 demo」时也能 import 同级 src 目录下的 Tool.py。
# 注意：本文件是 commands/__init__.py，所以 src 目录是【包目录的上一层】。
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SRC_DIR)
from Tool import ToolUseContext  # noqa: E402


# =============================================================================
# 一、ContentBlock —— 发给 Claude 的消息内容块（极简版）
# =============================================================================
#
# 对应 TS 的 ContentBlockParam（Anthropic SDK 类型）。
# 沿用 QueryEngine.py 的约定，用 dict 表示一个 block：
#   文本块：{"type": "text", "text": "..."}
# prompt 类型命令产出这样一串 block，QueryEngine 把它当作「用户消息」发给模型。
ContentBlock = Dict[str, Any]


# =============================================================================
# 二、三种命令的「执行方式」枚举
# =============================================================================
#
# 对应讲义第二节的类型对比表。TS 里 type 字段是字符串字面量联合
# ('prompt' | 'local' | 'local-jsx')，Python 用 Enum 表达同样的「三选一」。
class CommandType(str, Enum):
    PROMPT = "prompt"        # 发给模型 → 进入 Agent Loop（如 /code-review）
    LOCAL = "local"          # 本地执行，返回文本字符串（如 /clear /compact）
    LOCAL_JSX = "local-jsx"  # 本地执行，返回 UI 组件（如 /help /status）


# =============================================================================
# 三、CommandBase —— 所有命令共有的元数据字段
# =============================================================================
#
# 对应讲义第三节、TS 的 CommandBase。
# 关键点：这里只放【元数据】，不放真正的实现代码（实现藏在 load / get_prompt_for_command）。
class CommandBase(ABC, BaseModel):
    type: CommandType                       # 决定走哪条执行路径（见第七节分发）
    name: str                               # 命令名，如 "clear" / "compact"
    description: str = ""                   # 显示给用户的说明
    aliases: List[str] = Field(default_factory=list)  # 别名，如 clear -> ["reset", "new"]

    # 可选：动态判断是否启用。默认 lambda 恒为 True。
    # 对应 TS 的 isEnabled?: () => boolean，常用来接环境变量 / feature flag。
    is_enabled: Callable[[], bool] = Field(default=lambda: True)
    is_hidden: bool = False                 # 是否在帮助/补全里隐藏
    user_invocable: bool = True             # 用户能否手动输入（有些命令仅供内部调用）
    argument_hint: Optional[str] = None     # 参数提示，如 "<optional instructions>"
    is_sensitive: bool = False              # 参数是否需要从历史里脱敏

    class Config:
        arbitrary_types_allowed = True      # 允许 is_enabled 这种可调用对象字段

    @abstractmethod
    async def execute(self, args: str, context: ToolUseContext) -> "CommandExecution":
        """
        统一的执行入口（本教学版的简化设计）。
        三种子类型各自实现自己的执行路径，返回一个 CommandExecution 描述结果。
        真实 TS 源码里是在 QueryEngine 按 type 分支调用 load() / getPromptForCommand()，
        这里用一个抽象方法更能体现「多态分发」。
        """
        ...


# 命令执行的统一返回结构（教学用，方便 demo 打印结果）。
class CommandExecution(BaseModel):
    type: CommandType
    # local / local-jsx：本地产出的文本（或 UI 占位描述）
    local_output: Optional[str] = None
    # prompt：要发给模型的 content blocks（交给 QueryEngine 进 Agent Loop）
    prompt_blocks: Optional[List[ContentBlock]] = None

    class Config:
        arbitrary_types_allowed = True


# load() 返回的「模块对象」：里面有一个 call 方法。
# 对应 TS 里 import('./clear.js') 解析出的模块（含导出的 call 函数）。
class LoadedModule(BaseModel):
    call: Callable[[str, ToolUseContext], Awaitable[Any]]

    class Config:
        arbitrary_types_allowed = True


# =============================================================================
# 四、三种具体命令类型
# =============================================================================

class PromptCommand(CommandBase):
    """对应 TS 的 PromptCommand。执行时产出一段消息发给模型（如 /code-review /insights）。"""
    type: CommandType = CommandType.PROMPT
    progress_message: str = ""   # 执行时 UI 显示的提示，如 "reviewing your code"
    content_length: int = 0      # 命令内容字符数（估算 token 用，本教学版仅占位）

    # 核心字段：返回要发给 Claude 的内容块。
    # 对应 TS 的 getPromptForCommand(args, context): Promise<ContentBlockParam[]>。
    get_prompt_for_command: Callable[[str, ToolUseContext], Awaitable[List[ContentBlock]]] = None  # type: ignore

    class Config:
        arbitrary_types_allowed = True

    async def execute(self, args: str, context: ToolUseContext) -> CommandExecution:
        blocks = await self.get_prompt_for_command(args, context)
        return CommandExecution(type=self.type, prompt_blocks=blocks)


class LocalCommand(CommandBase):
    """对应 TS 的 LocalCommand。本地执行，返回文本字符串，【绕过模型】（如 /clear /compact）。"""
    type: CommandType = CommandType.LOCAL
    supports_non_interactive: bool = False   # 是否支持非交互模式（如管道输入）

    # ★ Lazy-load 核心：load 是个函数，调用它才真正 import 实现文件，返回含 call 的模块对象。
    #   对应 TS 的 load: () => import('./clear.js')。
    load: Callable[[], Awaitable[LoadedModule]] = None  # type: ignore

    class Config:
        arbitrary_types_allowed = True

    async def execute(self, args: str, context: ToolUseContext) -> CommandExecution:
        module = await self.load()              # ← 直到这里才加载实现文件
        text = await module.call(args, context)
        return CommandExecution(type=self.type, local_output=text)


class LocalJSXCommand(CommandBase):
    """对应 TS 的 LocalJSXCommand。本地执行，返回 UI 组件，同样【绕过模型】（如 /help /status）。"""
    type: CommandType = CommandType.LOCAL_JSX

    load: Callable[[], Awaitable[LoadedModule]] = None  # type: ignore

    class Config:
        arbitrary_types_allowed = True

    async def execute(self, args: str, context: ToolUseContext) -> CommandExecution:
        module = await self.load()
        component = await module.call(args, context)
        # Python 里没有 React，用一段文本描述代替「渲染好的 UI 组件」。
        return CommandExecution(type=self.type, local_output=f"[渲染 UI 组件] {component}")


# =============================================================================
# 五、Lazy-load 的「装载器」—— 真正按需 import 独立实现文件
# =============================================================================
#
# 调试开关：默认关闭，避免污染真实 REPL 界面。
# 想在 demo 里【看见】懒加载发生的时机，设环境变量 CC_DEBUG_LAZY=1 即可。
def _debug_lazy(msg: str) -> None:
    if os.environ.get("CC_DEBUG_LAZY", "").lower() in ("1", "true"):
        print(msg)


# 对应讲义第四节、TS 的 load: () => import('./xxx.js')。
# _load_impl("compact") 会在【被调用那一刻】才 import commands/compact.py，
# 把它导出的 call 包成 LoadedModule。设 CC_DEBUG_LAZY=1 可看到 📦 提示，证明加载时机。
async def _load_impl(module_name: str) -> LoadedModule:
    _debug_lazy(f"   📦 [lazy-load] importlib.import_module('commands.{module_name}')")
    mod = importlib.import_module(f"commands.{module_name}")
    return LoadedModule(call=mod.call)


# =============================================================================
# 六、各内置命令的【元数据】（实现都在独立文件里）
# =============================================================================

# /clear（local）—— 实现见 commands/clear.py
clear = LocalCommand(
    name="clear",
    description="Clear conversation history and free up context",
    aliases=["reset", "new"],
    supports_non_interactive=False,
    load=lambda: _load_impl("clear"),
)

# /help（local-jsx）—— 实现见 commands/help.py
help_cmd = LocalJSXCommand(
    name="help",
    description="Show help and available commands",
    load=lambda: _load_impl("help"),
)

# /compact（local）—— 实现见 commands/compact.py（含压缩逻辑）
# 教学要点：讲义第二节表格把它列为 prompt，但真实源码 commands/compact/index.ts
# 其实是 type:'local'，因为压缩要【替换历史】，必须本地完成。
compact = LocalCommand(
    name="compact",
    description="Clear conversation history but keep a summary in context",
    argument_hint="<optional custom summarization instructions>",
    supports_non_interactive=True,
    # 对应讲义第三节：用环境变量动态禁用本命令
    is_enabled=lambda: os.environ.get("DISABLE_COMPACT", "").lower() not in ("1", "true"),
    load=lambda: _load_impl("compact"),
)


# /resume（local）—— 实现见 commands/resume.py
# 列出本项目历史会话，选一个切换过去。会话持久化见 session_persistence.py。
resume_cmd = LocalCommand(
    name="resume",
    description="列出历史会话并切换",
    supports_non_interactive=False,
    load=lambda: _load_impl("resume"),
)

# /rename（local）—— 实现见 commands/rename.py
# 给当前会话起一个名字。标题会出现在 --resume 选单里。
rename_cmd = LocalCommand(
    name="rename",
    description="重命名当前会话",
    argument_hint="[标题]",
    supports_non_interactive=False,
    load=lambda: _load_impl("rename"),
)

# /code-review（prompt）—— 实现见 commands/code_review.py
# 对应讲义第四节 insights 那种「连 getPromptForCommand 都是懒的」写法：
# 直到真正执行才 import 庞大的实现模块。
async def _code_review_prompt(args: str, context: ToolUseContext) -> List[ContentBlock]:
    _debug_lazy("   📦 [lazy import] import commands.code_review（直到执行才加载）")
    mod = importlib.import_module("commands.code_review")
    return await mod.get_prompt_for_command(args, context)


code_review = PromptCommand(
    name="code-review",
    description="Review the current diff for bugs and cleanups",
    progress_message="reviewing your code",
    get_prompt_for_command=_code_review_prompt,
)


# =============================================================================
# 七、COMMANDS —— memoize 缓存的内置命令注册表
# =============================================================================
#
# 对应讲义第五节、TS 的 const COMMANDS = memoize((): Command[] => [...])。
# functools.lru_cache 等价于 lodash memoize：第一次调用构建列表，之后返回缓存。
@functools.lru_cache(maxsize=1)
def COMMANDS() -> List[CommandBase]:
    cmds: List[CommandBase] = [
        clear,        # local
        compact,      # local
        resume_cmd,   # local  —— 切换历史会话
        rename_cmd,   # local  —— 重命名当前会话
        help_cmd,     # local-jsx
        code_review,  # prompt
    ]

    # 条件注册（feature flag）演示，对应 TS 的 ...(feature('BRIDGE_MODE') ? [bridge] : [])。
    if os.environ.get("ENABLE_BRIDGE", "").lower() in ("1", "true"):
        cmds.append(
            LocalCommand(
                name="bridge",
                description="Connect to the IDE bridge (feature-flagged)",
                load=lambda: _load_impl("clear"),  # 教学占位：复用一个已有实现
            )
        )

    return cmds


# =============================================================================
# 八、getCommands() —— 对外入口（合并多源 + 过滤）
# =============================================================================
#
# 对应讲义第六节、TS 的 export async function getCommands(cwd)。
async def _load_skill_dir_commands(cwd: str) -> List[CommandBase]:
    """对应 TS 从 ~/.claude/skills/ 扫描用户自定义命令。教学版返回空列表占位。"""
    return []


def _meets_availability_requirement(cmd: CommandBase) -> bool:
    """对应 TS 的 meetsAvailabilityRequirement：按用户类型筛选。教学版只看 user_invocable。"""
    return cmd.user_invocable


def _is_command_enabled(cmd: CommandBase) -> bool:
    """对应 TS 的 isCommandEnabled：调用命令自己的 is_enabled()。"""
    try:
        return cmd.is_enabled()
    except Exception:
        return True


async def get_commands(cwd: str = ".") -> List[CommandBase]:
    # 1) 合并所有来源（内置 + 技能目录 + 插件 + 工作流……教学版只取两源）
    all_commands: List[CommandBase] = []
    all_commands.extend(await _load_skill_dir_commands(cwd))  # 用户自定义命令（这里为空）
    all_commands.extend(COMMANDS())                            # 核心内置命令

    # 2) 过滤：保留「有权访问」且「已启用」的命令
    return [
        cmd for cmd in all_commands
        if _meets_availability_requirement(cmd) and _is_command_enabled(cmd)
    ]


def find_command(name_or_alias: str, commands: List[CommandBase]) -> Optional[CommandBase]:
    """按 name 或 alias 查找命令（输入 "/clear" 或 "clear" 都能找到）。"""
    key = name_or_alias.lstrip("/").strip()
    for cmd in commands:
        if cmd.name == key or key in cmd.aliases:
            return cmd
    return None


# =============================================================================
# 九、与 QueryEngine 衔接 —— 解析用户输入并按 type 分发
# =============================================================================
#
# 对应讲义第七节 processUserInput。
async def dispatch_user_input(
    user_input: str,
    context: ToolUseContext,
    cwd: str = ".",
) -> CommandExecution:
    if not user_input.startswith("/"):
        # 普通输入：原样作为消息发给模型（等价于一个匿名 prompt 命令）
        return CommandExecution(
            type=CommandType.PROMPT,
            prompt_blocks=[{"type": "text", "text": user_input}],
        )

    # 拆出命令名与参数："/compact 保留要点" → name="compact", args="保留要点"
    body = user_input[1:].strip()
    name, _, args = body.partition(" ")

    commands = await get_commands(cwd)
    cmd = find_command(name, commands)
    if cmd is None:
        return CommandExecution(type=CommandType.LOCAL, local_output=f"未知命令：/{name}")

    # ★ 多态分发：每种命令的 execute() 走自己的路径（见第四节三个子类）。
    return await cmd.execute(args.strip(), context)
