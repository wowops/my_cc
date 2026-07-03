# 在 Python 中，没有 Interface 关键字，我们通常用 **abstract base classes（`abc` 模块）** 和 **数据类/Pydantic** 来实现同样的高级架构。

from enum import Enum
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable, Set, Union, Awaitable
from pydantic import BaseModel, Field
import threading # 或者 import asyncio

# 标准化工具返回结果
class ToolResult(BaseModel):
    data: Any
    # 工具是否要在会话中静默插入额外的对话消息
    new_messages: Optional[List[Dict[str, Any]]] = None
    # 极少数情况下，工具可以返回一个修改 context 的回调函数
    context_modifier: Optional[Callable[['ToolUseContext'], 'ToolUseContext']] = None

# 权限校验结果
class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    # ASK = 既不直接放行也不直接拒绝，交给上层 UI 弹窗问用户
    # （对应源码 alwaysAskRules 命中后的行为）
    ASK = "ask"

class PermissionResult(BaseModel):
    behavior: PermissionBehavior
    # 允许权限校验器“净化”工具参数（例如把危险的 rm -rf / 篡改为 rm -rf ./tmp）
    updated_input: Dict[str, Any]
    # 被拒绝 / 询问时，给用户或 AI 看的原因
    message: Optional[str] = None


# ToolPermissionContext（权限 mode 管理）
# 对应源码 src/Tool.ts 第 123 行的 ToolPermissionContext。
# 它把“当前处于哪种权限模式 + 各种白/黑/询问名单”单独抽成一个配置对象，
# 这样 check_permissions() 才有据可依，而不是无脑弹窗。
class PermissionMode(str, Enum):
    DEFAULT = "default"                    # 正常模式：危险操作弹窗问用户
    PLAN = "plan"                          # 计划模式：只能读，不能写
    ACCEPT_EDITS = "acceptEdits"           # 接受编辑：安全文件操作 + Edit/Write 自动放行
    BYPASS_PERMISSIONS = "bypassPermissions"  # 跳过一切权限检查（--dangerouslySkipPermissions）
    AUTO = "auto"                          # 自动模式：按规则自动批准


# =============================================================================
# 权限模式元数据与循环逻辑
# =============================================================================
# 对应 TS src/utils/permissions/PermissionMode.ts（符号/标题/颜色）
#        + src/utils/permissions/getNextPermissionMode.ts（循环顺序）
#
# 循环顺序（外部用户）：
#   default → acceptEdits → plan → bypassPermissions（如果可用）→ default
#
# default 模式不显示指示器（和 TS 一样：isDefaultMode 时不渲染 modePart）。

_MODE_META: dict = {
    PermissionMode.DEFAULT:            {"title": "Default",              "symbol": ""},
    PermissionMode.PLAN:               {"title": "Plan Mode",            "symbol": "⏸"},
    PermissionMode.ACCEPT_EDITS:       {"title": "Accept Edits",         "symbol": "⏵⏵"},
    PermissionMode.BYPASS_PERMISSIONS: {"title": "Bypass Permissions",   "symbol": "⏵⏵"},
    PermissionMode.AUTO:               {"title": "Auto Mode",            "symbol": "⏵⏵"},
}


def get_mode_title(mode: PermissionMode) -> str:
    """返回模式的用户可见标题（如 'Plan Mode'）。"""
    return _MODE_META.get(mode, {}).get("title", str(mode))


def get_mode_symbol(mode: PermissionMode) -> str:
    """返回模式的终端符号（如 '⏸'）。default 模式返回空字符串。"""
    return _MODE_META.get(mode, {}).get("symbol", "")


def is_default_mode(mode: PermissionMode) -> bool:
    """default 模式下不显示指示器（对齐 TS 的 isDefaultMode）。"""
    return mode == PermissionMode.DEFAULT


def get_next_permission_mode(pc: "ToolPermissionContext") -> PermissionMode:
    """计算 Shift+Tab 循环的下一个模式。

    循环顺序：
    default → acceptEdits → plan → bypassPermissions（如果可用）→ default
    如果 bypassPermissions 不可用，plan 之后直接回 default。

    对应 TS src/utils/permissions/getNextPermissionMode.ts。
    """
    current = pc.mode
    bypass_available = pc.is_bypass_permissions_mode_available

    if current == PermissionMode.DEFAULT:
        return PermissionMode.ACCEPT_EDITS
    elif current == PermissionMode.ACCEPT_EDITS:
        return PermissionMode.PLAN
    elif current == PermissionMode.PLAN:
        if bypass_available:
            return PermissionMode.BYPASS_PERMISSIONS
        return PermissionMode.DEFAULT
    elif current == PermissionMode.BYPASS_PERMISSIONS:
        return PermissionMode.DEFAULT
    else:
        # AUTO 等其他模式也回到 default
        return PermissionMode.DEFAULT


class ToolPermissionContext(BaseModel):
    mode: PermissionMode = PermissionMode.DEFAULT
    # 三类规则：键是工具名，值是该工具下被允许/拒绝/必问的具体规则集合
    always_allow_rules: Dict[str, Set[str]] = Field(default_factory=dict)
    always_deny_rules: Dict[str, Set[str]] = Field(default_factory=dict)
    always_ask_rules: Dict[str, Set[str]] = Field(default_factory=dict)
    # bypass 模式是否被允许使用（某些企业环境会禁用它）
    is_bypass_permissions_mode_available: bool = True

    class Config:
        arbitrary_types_allowed = True

# 占位系统状态与选项类
class AppState(BaseModel):
    pass

class ToolUseContextOptions(BaseModel):
    tools: List[Any] = Field(default_factory=list)
    commands: List[Any] = Field(default_factory=list)
    mcp_clients: List[Any] = Field(default_factory=list)
    debug: bool = False
    verbose: bool = False
    main_loop_model: str = "claude-3-5-sonnet"
    max_budget_usd: Optional[float] = 10.0
    is_non_interactive_session: bool = False

# 复现 ToolUseContext
class ToolUseContext(BaseModel):
    # 1. 核心选项
    options: ToolUseContextOptions = Field(default_factory=ToolUseContextOptions)
    
    # 2. 会话记录
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    
    # 3. 任务生命周期控制 (用 Event 模拟 TS 的 AbortController)
    abort_event: Optional[threading.Event] = None
    
    # 4. 全局状态读写挂载点
    get_app_state: Optional[Callable[[], AppState]] = None
    set_app_state: Optional[Callable[[Callable[[AppState], AppState]], None]] = None
    
    # 5. 文件系统缓存
    read_file_state: Optional[Any] = None # 可以是字典或自定义 Cache 类
    
    # 6. UI / 通知层回调挂载点
    add_notification: Optional[Callable[[Dict], None]] = None
    request_prompt: Optional[Callable[[str], Any]] = None # 请求用户输入
    # /clear 擦屏后让 UI 重画头部 banner（对应真 CC 用 conversationId 强制重渲染 logo）。
    # 擦屏是通用动作（在 clear.py），但「画什么 banner」是 UI 特有的，故由 main.py 注入。
    render_banner: Optional[Callable[[], None]] = None
    # 会话持久化：/resume 命令需要知道当前会话 ID 和项目目录才能「存旧 + 加载新」。
    # 和 render_banner 一样由 main.py 注入——持久化模块细节命令层不直接 import。
    session_id: str = ""
    project_dir: str = ""

    # 7. 子代理与权限追踪
    agent_id: Optional[str] = None
    tool_decisions: Dict[str, Any] = Field(default_factory=dict)
    # 缺陷 ② 修复：把权限配置对象挂进运行环境，check_permissions() 据此决策
    permission_context: 'ToolPermissionContext' = Field(default_factory=lambda: ToolPermissionContext())
    
    class Config:
        arbitrary_types_allowed = True # 允许注入函数或非 Pydantic 对象
        
    # 进度回调：由 UI 层（main.py）注入，供压缩/加载等场景实时更新提示文字 + 百分比。
    # percent=None 表示无进度条（只更新文字）；0.0~1.0 表示百分比进度条。
    # 对应 TS src/services/compact/compact.ts 的 onCompactProgress。
    on_progress: Optional[Callable[[str, Optional[float]], None]] = None


    @property
    def is_aborted(self) -> bool:
        """检查任务是否应该取消"""
        return self.abort_event.is_set() if self.abort_event else False
    

# 复现 Tool 接口 (使用 Pydantic BaseModel + 抽象基类)
# 对应源码中的 export type Tool = { ... }
class BaseTool(ABC, BaseModel):
    """
    所有工具的最高统帅类。
    继承它的子类必须实现 @abstractmethod 标记的方法。
    """
    name: str
    input_schema: Dict[str, Any]
    
    # 元数据扩展
    aliases: List[str] = Field(default_factory=list)
    search_hint: Optional[str] = None
    max_result_size_chars: int = 100_000  # 自动截断防爆屏
    
    class Config:
        arbitrary_types_allowed = True

    # --- 核心执行 (对应源码的 call) ---
    @abstractmethod
    async def call(
        self, 
        args: Dict[str, Any], 
        context: 'ToolUseContext', 
        on_progress: Optional[Callable[[Any], None]] = None
    ) -> ToolResult:
        """实际执行工具的逻辑 (必须是异步)"""
        pass

    # 把 prompt() 和 get_description() 拆成两个独立方法
    # 二者时机和服务对象完全不同（见 Tool.md 第五节）：
    #   prompt()          —— 对话开始前注入 System Prompt，服务对象是【AI 模型】，静态描述工具能做什么
    #   get_description()  —— AI 真正调用某次工具时，服务对象是【用户】，动态显示这次在做什么
    @abstractmethod
    async def prompt(self, context: Optional['ToolUseContext'] = None) -> str:
        """静态：注入 System Prompt，告诉 AI 这个工具能做什么、怎么用。"""
        pass

    @abstractmethod
    async def get_description(self, args: Dict[str, Any], context: 'ToolUseContext') -> str:
        """动态：某次调用时显示在 UI 上给用户看，例如 'Reading file /src/Tool.py'。"""
        pass

    # --- 安全与权限防线 (复刻源码里的 defaultable keys) ---
    # 下面这些方法自带默认值，这就等同于源码里的 `buildTool` 和 `TOOL_DEFAULTS` 逻辑
    def is_enabled(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        """默认假设工具会修改系统，以策安全"""
        return False

    def is_destructive(self) -> bool:
        """是否是破坏性操作 (如 rm -rf)"""
        return False

    # 并发安全标记。
    # 对应源码 isConcurrencySafe(input)：Agent Loop 一次返回多个工具调用时，
    # 只有并发安全的工具才能被并行执行（默认：只读工具天然并发安全）。
    def is_concurrency_safe(self, args: Dict[str, Any]) -> bool:
        return self.is_read_only()

    # validate_input 独立于 check_permissions。
    # 两者职责不同：
    #   validate_input   —— 检查“参数本身合不合法”，错了是【报错给 AI】让它重传
    #   check_permissions —— 检查“允不允许执行”，需要时【弹窗问用户】
    async def validate_input(
        self, args: Dict[str, Any], context: 'ToolUseContext'
    ) -> Optional[str]:
        """校验参数合法性。合法返回 None；不合法返回错误信息字符串（给 AI 看）。"""
        return None

    async def check_permissions(self, args: Dict[str, Any], context: 'ToolUseContext') -> PermissionResult:
        """
        对应源码的 checkPermissions。
        缺陷 ④ 修复（后半）：不再无脑弹窗，而是按 permission_context.mode 决策。
        决策顺序与源码一致：bypass > plan > 黑名单 > 白名单 > 询问名单 > 默认。
        """
        pc = context.permission_context
        # 把“工具名 -> 规则集合”里属于本工具的规则取出来
        allow_rules = pc.always_allow_rules.get(self.name, set())
        deny_rules = pc.always_deny_rules.get(self.name, set())
        ask_rules = pc.always_ask_rules.get(self.name, set())

        # 1) bypass：跳过一切检查，直接放行
        if pc.mode == PermissionMode.BYPASS_PERMISSIONS and pc.is_bypass_permissions_mode_available:
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        # 2) acceptEdits：Edit / Write 工具自动放行（它们是"接受编辑"的核心）。
        #    对应 TS：acceptEdits 模式下各工具的 checkPermissions 自行判断；
        #    Bash/PowerShell 有独立 modeValidation，Edit/Write 无显式检查但语义上本质就是"接受编辑"。
        if pc.mode == PermissionMode.ACCEPT_EDITS and self.name in ("Edit", "Write"):
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        # 3) plan：计划模式只读，任何写操作一律拒绝
        if pc.mode == PermissionMode.PLAN and not self.is_read_only():
            return PermissionResult(
                behavior=PermissionBehavior.DENY,
                updated_input=args,
                message=f"计划模式（plan）下禁止执行非只读工具 [{self.name}]。",
            )

        # 4) 黑名单优先于白名单：命中即拒
        if deny_rules:
            return PermissionResult(
                behavior=PermissionBehavior.DENY,
                updated_input=args,
                message=f"工具 [{self.name}] 命中永久拒绝规则。",
            )

        # 5) 白名单：命中直接放行
        if allow_rules:
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        # 6) 询问名单 / 写操作：需要问用户
        #    ★ 安全模型核心：弹窗开关挂在「是不是写操作」(not is_read_only) 上，
        #      而不是「够不够破坏性」(is_destructive)。否则像 Edit 这种「会改盘、
        #      但算不上 rm -rf 破坏性」的工具会从缝里漏过去、静默改文件。
        #      凡是写操作，默认就要问。
        needs_ask = bool(ask_rules) or not self.is_read_only()
        if pc.mode == PermissionMode.AUTO and not self.is_destructive():
            # auto 模式比 default 更宽松：非破坏性操作（含普通写）自动批准，
            #   只有真正破坏性的才落到下面去问。所以这里仍用 is_destructive。
            return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

        if needs_ask:
            # 非交互式会话无法弹窗，统一返回 ASK 交给上层处理
            if context.options.is_non_interactive_session:
                return PermissionResult(
                    behavior=PermissionBehavior.ASK,
                    updated_input=args,
                    message=f"工具 [{self.name}] 需要用户授权，但当前为非交互式会话。",
                )
            # 用工具自己的 get_description 做摘要，避免把 Edit 的整段 new_string
            #   (可能是一整个文件) 原样打印到弹窗里。
            try:
                desc = await self.get_description(args, context)
            except Exception:
                desc = str(args)
            user_input = input(
                f"\n⚠️ 警告: AI 想执行工具 [{self.name}]：{desc}\n允许吗？(y/n): "
            )
            if user_input.lower() != "y":
                return PermissionResult(
                    behavior=PermissionBehavior.DENY,
                    updated_input=args,
                    message="用户拒绝了本次操作。",
                )

        # 7) 默认：非破坏性操作放行
        return PermissionResult(behavior=PermissionBehavior.ALLOW, updated_input=args)

    # --- UI 渲染层 (极简复现) ---
    def render_tool_message(self, args: Dict[str, Any]) -> str:
        """对应 renderToolUseMessage"""
        return f"🛠️ 工具 [{self.name}] 正在运行..."