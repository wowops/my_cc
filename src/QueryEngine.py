"""
对应 TS 源码：
    claude-code-main/src/QueryEngine.ts   （会话管理器）
    claude-code-main/src/query.ts         （queryLoop 主循环）
    claude-code-main/src/services/tools/toolOrchestration.ts （runTools 工具调度）

学习目标：用最少的代码把「Agent Loop 的五个步骤」讲清楚——
    1. 整理消息历史（必要时压缩）
    2. 调用 Claude API（流式拿回复）
    3. 回复里有没有工具调用？没有 → 结束循环
    4. 有 → 调度执行工具（只读并行 / 写操作串行）
    5. 把工具结果塞回消息历史，回到第 1 步

⚠️ 教学说明：本文件用一个【假的】call_claude_api（mock_call_claude_api）来
   模拟 Claude 的流式返回，所以【不需要 API key、不需要联网就能直接运行】：

       python src/QueryEngine.py

   等你理解了循环结构，只要把 config.call_claude_api 换成真正调用
   Anthropic SDK 的函数，整套循环就能驱动真实的 Claude。
"""

from __future__ import annotations

import os
import sys
import uuid
import asyncio
import platform
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

# 让「直接 python 运行本文件」时也能 import 同目录的 Tool.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from Tool import (  # noqa: E402
    BaseTool,
    ToolResult,
    ToolUseContext,
    PermissionBehavior,
)
# 斜线命令系统（见 commands.py）：submit_message 用它解析 / 开头的输入。
from commands import (  # noqa: E402
    CommandType,
    dispatch_user_input,
)
# 会话持久化（见 session_persistence.py）：每轮对话后自动把 message 追加进 JSONL。
from session_persistence import save_messages  # noqa: E402


# =============================================================================
# 一、消息与流式事件的数据结构
# =============================================================================
#
# Anthropic 的消息格式：一条消息是 {"role": ..., "content": [...blocks...]}。
# 每个 content block 有一个 type 字段：
#   - 文本块：     {"type": "text", "text": "..."}
#   - 工具调用块： {"type": "tool_use", "id": "...", "name": "Bash", "input": {...}}
#   - 工具结果块： {"type": "tool_result", "tool_use_id": "...", "content": "..."}
#
# 为了简单，我们直接用 dict 表示这些 block，用 Message 类型别名表示一条消息。
Message = Dict[str, Any]


# 流式事件：API 不是一次性返回整条消息，而是「一小块一小块」地吐出来。
# 我们把每一小块包装成一个 StreamEvent，从最底层一路 yield 到最上层 UI。
# 这就是「打字机效果」的来源。
class StreamEvent(BaseModel):
    # 事件类型：
    #   "text_delta"   —— 文本增量（AI 正在逐字说话）
    #   "tool_use"     —— AI 决定调用某个工具（一个完整的 tool_use block）
    #   "message_stop" —— 本轮 assistant 消息说完了
    #   "tool_result"  —— 某个工具执行完，产出了结果
    #   "system"       —— 引擎自己的提示（如「达到最大轮次」「用户取消」）
    type: str
    text: Optional[str] = None              # text_delta / system 用
    tool_use: Optional[Dict[str, Any]] = None   # tool_use 用：完整的 tool_use block
    tool_result: Optional[Dict[str, Any]] = None # tool_result 用：完整的 tool_result block

    class Config:
        arbitrary_types_allowed = True


# =============================================================================
# 二、QueryEngineConfig —— 配置对象（把所有依赖注入进来）
# =============================================================================
#
# 对应 TS 的 QueryEngineConfig（src/QueryEngine.ts 第 130 行）。
# 这里只保留教学需要的字段，注释里标注了对应作用。
class QueryEngineConfig(BaseModel):
    cwd: str = "."                              # 当前工作目录
    tools: List[BaseTool] = Field(default_factory=list)  # 所有可用工具
    system_prompt: str = ""                     # 系统提示词（构建好的）
    max_turns: Optional[int] = 10               # 最大循环圈数，防无限循环
    max_budget_usd: Optional[float] = None      # 费用上限（本教学版只占位）

    # ★ 把「调用 Claude API 的函数」做成可注入字段：
    #   - 教学时注入 mock_call_claude_api（假的、本地的）
    #   - 真实使用时换成调用 Anthropic SDK 的函数
    # 签名：async def fn(messages, config, context) -> AsyncGenerator[StreamEvent]
    call_claude_api: Optional[Callable[..., AsyncGenerator[StreamEvent, None]]] = None

    class Config:
        arbitrary_types_allowed = True


# =============================================================================
# 三、环境信息注入 —— 告诉模型「你在哪、有哪些文件」
# =============================================================================
#
# 对应 TS 的 computeEnvInfo（src/constants/prompts.ts 第 606 行）。
# 为什么需要它：没有这块，模型不知道当前工作目录是什么，读文件时只能瞎猜路径
# （实测 DeepSeek 会猜成它训练数据里的 macOS 路径 /Users/xxx/...）。
# Anthropic 的 <env> 块只放 cwd / 是否 git 仓库 / 平台 / OS —— 不放文件树，
# 因为它的模型有 LS/Glob/Grep 工具可以自己探索，塞全树是浪费 token。
def _is_git_repo(abs_cwd: str) -> bool:
    return os.path.isdir(os.path.join(abs_cwd, ".git"))


def build_env_info(cwd: str) -> str:
    """严格照抄 computeEnvInfo 的 <env> 块（只是改用 Python 的取值方式）。"""
    abs_cwd = os.path.abspath(cwd)
    is_git = "Yes" if _is_git_repo(abs_cwd) else "No"
    return (
        "Here is useful information about the environment you are running in:\n"
        "<env>\n"
        f"Working directory: {abs_cwd}\n"
        f"Is directory a git repo: {is_git}\n"
        f"Platform: {sys.platform}\n"
        f"OS Version: {platform.platform()}\n"
        "</env>"
    )


# 〔已撤掉的拐杖〕早期没有 Glob/Grep 时，这里曾有一个 build_dir_snapshot()，
# 往系统提示词里塞一层顶层目录树，帮模型定位文件（非 Anthropic 默认行为）。
# 现在 Glob/Grep 工具已就位，模型能自己探索目录，这个拐杖就撤掉了——
# 这也正是 Anthropic 原版只在 <env> 放 cwd、不放文件树的原因（见上方注释）。


# =============================================================================
# 四、System Prompt 构建 —— 拼环境信息 + 消费每个工具的 prompt()
# =============================================================================
#
# 对应 TS 的 fetchSystemPromptParts（src/QueryEngine.ts 第 289 行）。
# 关键点：遍历所有工具，把它们的 prompt() 拼进系统提示词，
#        这就是「告诉 AI 有哪些工具、怎么用」。
async def build_system_prompt(
    tools: List[BaseTool],
    cwd: str = ".",
    custom_system_prompt: str = "",
) -> str:
    parts: List[str] = [
        "你是 Claude Code，一个运行在终端里的 AI 编程助手。",
        build_env_info(cwd),  # 注入 cwd / git / 平台（对应 computeEnvInfo）
    ]
    # ↓↓↓ 这就是 Tool.py 里 prompt() 方法被「消费」的地方 ↓↓↓
    for tool in tools:
        tool_prompt = await tool.prompt()
        parts.append(f"## 工具 {tool.name}\n{tool_prompt}")
    if custom_system_prompt:
        parts.append(custom_system_prompt)
    return "\n\n".join(parts)


# =============================================================================
# 四、工具调度 runTools —— 只读并行 / 写操作串行
# =============================================================================
#
# 对应 TS 的 runTools（toolOrchestration.ts 第 19 行）。
# 核心规则：
#   is_concurrency_safe() == True  （只读）→ 并行执行（快）
#   is_concurrency_safe() == False （写）  → 串行执行（安全）

class _ToolGroup(BaseModel):
    """一批「并发安全性相同」且「在原列表里连续」的工具调用。"""
    is_concurrency_safe: bool
    blocks: List[Dict[str, Any]]


def _find_tool(name: str, config: QueryEngineConfig) -> Optional[BaseTool]:
    for t in config.tools:
        if t.name == name:
            return t
    return None


def partition_tool_calls(
    tool_use_blocks: List[Dict[str, Any]],
    config: QueryEngineConfig,
) -> List[_ToolGroup]:
    """
    对应 TS 的 partitionToolCalls。
    把工具调用按「并发安全性」分组，并保持原始顺序：
    例如 [读, 读, 写, 读] → [组(读,读,并行), 组(写,串行), 组(读,并行)]
    （注意：写操作会切断并行批次，保证写操作前后的顺序不被打乱。）
    """
    groups: List[_ToolGroup] = []
    for block in tool_use_blocks:
        tool = _find_tool(block["name"], config)
        # 找不到工具的，保守当作「非并发安全」串行处理
        safe = bool(tool and tool.is_concurrency_safe(block.get("input", {})))
        # 能并到上一组就并入，否则开新组
        if groups and groups[-1].is_concurrency_safe == safe and safe:
            groups[-1].blocks.append(block)
        else:
            groups.append(_ToolGroup(is_concurrency_safe=safe, blocks=[block]))
    return groups


async def _execute_one_tool(
    block: Dict[str, Any],
    config: QueryEngineConfig,
    context: ToolUseContext,
) -> StreamEvent:
    """
    执行单个工具调用，返回一个 tool_result 事件。
    流程：找工具 → 权限检查 → 参数校验 → call() → 包装成 tool_result。
    """
    tool_use_id = block["id"]
    name = block["name"]
    args = block.get("input", {})

    def make_result(content: str, is_error: bool = False) -> StreamEvent:
        return StreamEvent(
            type="tool_result",
            tool_result={
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            },
        )

    tool = _find_tool(name, config)
    if tool is None:
        return make_result(f"错误：找不到名为 [{name}] 的工具。", is_error=True)

    # 1) 权限检查（对应 canUseTool）
    perm = await tool.check_permissions(args, context)
    if perm.behavior != PermissionBehavior.ALLOW:
        return make_result(
            f"权限被拒绝/需询问：{perm.message or '未授权'}", is_error=True
        )
    args = perm.updated_input  # 权限层可能「净化」了参数

    # 2) 参数校验（对应 validateInput）
    err = await tool.validate_input(args, context)
    if err is not None:
        return make_result(f"参数不合法：{err}", is_error=True)

    # 3) 真正执行
    try:
        result: ToolResult = await tool.call(args, context)
        return make_result(str(result.data))
    except Exception as e:  # 工具崩溃也要把错误喂回给 AI，让它自己决定怎么办
        return make_result(f"工具执行异常：{e}", is_error=True)


async def run_tools(
    tool_use_blocks: List[Dict[str, Any]],
    config: QueryEngineConfig,
    context: ToolUseContext,
) -> AsyncGenerator[StreamEvent, None]:
    """
    对应 TS 的 runTools。把工具分批，只读批并行、写操作批串行。
    """
    for group in partition_tool_calls(tool_use_blocks, config):
        if group.is_concurrency_safe and len(group.blocks) > 1:
            # —— 并行执行（只读）——
            # asyncio.as_completed：谁先跑完就先 yield 谁，体现「并行」。
            tasks = [
                asyncio.create_task(_execute_one_tool(b, config, context))
                for b in group.blocks
            ]
            for coro in asyncio.as_completed(tasks):
                yield await coro
        else:
            # —— 串行执行（写操作，或只有一个的只读）——
            for b in group.blocks:
                yield await _execute_one_tool(b, config, context)


# =============================================================================
# 五、query_loop —— Agent Loop 的真身（while True）
# =============================================================================
#
# 对应 TS 的 queryLoop（src/query.ts 第 241 行）。这是整个系统的心脏。
async def query_loop(
    messages: List[Message],
    config: QueryEngineConfig,
    context: ToolUseContext,
) -> AsyncGenerator[StreamEvent, None]:
    turn = 0
    while True:  # ★★★ Agent Loop 的真面目：一个不停转的循环 ★★★
        turn += 1

        # —— 退出条件 a：超过最大轮次，强制停止，防止无限循环烧钱 ——
        if config.max_turns is not None and turn > config.max_turns:
            yield StreamEvent(type="system", text=f"⛔ 达到最大轮次 {config.max_turns}，停止。")
            return

        # —— 退出条件 b：用户按了 Ctrl+C（abort_event 被 set）——
        if context.is_aborted:
            yield StreamEvent(type="system", text="⛔ 用户已取消。")
            return

        # —— 第一步：整理消息历史（真实系统这里会做上下文压缩 compact）——
        messages_for_query = messages  # 教学版先不压缩，留作占位

        # —— 第二步：调用 Claude API（流式），边收边往外 yield ——
        assistant_content: List[Dict[str, Any]] = []  # 本轮 assistant 消息的内容块
        tool_use_blocks: List[Dict[str, Any]] = []     # 本轮 AI 要求调用的工具
        text_buffer = ""                                # 累积文本增量

        async for event in config.call_claude_api(messages_for_query, config, context):
            yield event  # ← 实时往上冒泡，UI 的打字机效果就靠这一行
            if event.type == "text_delta":
                text_buffer += event.text or ""
            elif event.type == "tool_use":
                tool_use_blocks.append(event.tool_use)
                assistant_content.append(event.tool_use)
            elif event.type == "message_stop":
                # 把累积的文本作为一个 text block 放到 assistant 内容的最前面
                if text_buffer:
                    assistant_content.insert(0, {"type": "text", "text": text_buffer})

        # 把这一轮 assistant 的完整回复加入消息历史
        messages.append({"role": "assistant", "content": assistant_content, "uuid": uuid.uuid4().hex})

        # —— 第三步：AI 不需要调工具？说明任务完成，正常退出循环 ——
        if not tool_use_blocks:
            yield StreamEvent(type="system", text="✅ 对话结束（AI 没有再调用工具）。")
            return

        # —— 第四步：执行 AI 要求的工具，收集结果 ——
        tool_results: List[Dict[str, Any]] = []
        async for update in run_tools(tool_use_blocks, config, context):
            yield update
            if update.type == "tool_result":
                tool_results.append(update.tool_result)

        # —— 第五步：把工具结果作为一条 user 消息塞回历史，回到 while 顶部 ——
        # （Anthropic 约定：工具结果用 role="user" 的消息承载）
        messages.append({"role": "user", "content": tool_results, "uuid": uuid.uuid4().hex})
        # 下一圈循环会带着「包含工具结果」的新历史，再次去问 Claude。


# =============================================================================
# 六、QueryEngine —— 会话管理器（整合一切）
# =============================================================================
#
# 对应 TS 的 class QueryEngine。维护整个对话的状态，对外暴露 submit_message。
class QueryEngine:
    def __init__(
        self,
        config: QueryEngineConfig,
        context: ToolUseContext,
        initial_messages: Optional[List[Message]] = None,
        *,
        # 会话持久化（见 session_persistence.py）：
        #   session_id    —— 当前会话的 UUID，退出后靠它找回「上次聊到哪」
        #   project_dir   —— JSONL 落盘目录（= ~/.my_cc/projects/<sanitize(cwd)>/）
        #   _save_checkpoint —— 上一轮保存时 messages 的长度；submit_message 末尾只追加增量
        session_id: str = "",
        project_dir: Path | str = "",
    ):
        self.config = config
        self.context = context
        self.messages: List[Message] = initial_messages or []
        self.session_id = session_id
        self._project_dir = Path(project_dir) if project_dir else Path()
        self._save_checkpoint = len(self.messages)  # 初始的 messages 已保存
        # 没注入真实 API 时，默认用教学 mock
        if self.config.call_claude_api is None:
            self.config.call_claude_api = mock_call_claude_api

    def _save_session(self) -> None:
        """每轮对话后把新增的 messages 追加进 JSONL（只写增量、去重靠 uuid）。

        注意：这里读取的是 self.context.session_id，而不是 self.session_id。
        因为 /resume 切换会话时只更新了 context.session_id（ToolUseContext），
        没走 QueryEngine.__init__，self.session_id 还是旧的。
        用 context.session_id 保证 /resume 之后的新消息追到正确的目标文件。
        """
        sid = self.context.session_id
        if not sid or not self._project_dir:
            return  # 没有会话 ID / 目录 → 不持久化（如 --resume 之前的裸启动）
        # /resume 切过会话？→ 同步 self.session_id，并把 checkpoint 重置到当前长度
        # （loaded messages 已经在目标 JSONL 里了，不能从旧 checkpoint 开始算）
        if sid != self.session_id:
            self.session_id = sid
            self._save_checkpoint = len(self.messages)
        self._save_checkpoint = save_messages(
            sid,
            self._project_dir,
            cwd=self.config.cwd,
            messages=self.messages,
            _checkpoint_index=self._save_checkpoint,
        )

    async def submit_message(self, user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """
        对应 TS 的 async *submitMessage()。
        提交一条用户输入，先经过斜线命令系统分发，再按需进入 Agent Loop，
        流式 yield 出每一个事件。
        """
        # 1) 构建 System Prompt（消费每个工具的 prompt()）
        if not self.config.system_prompt:
            self.config.system_prompt = await build_system_prompt(
                self.config.tools, self.config.cwd
            )
        # 把消息历史同步进 context（让工具/权限层、以及斜线命令都能读到）
        self.context.messages = self.messages

        # 2) 斜线命令分发（对应讲义第七节 processUserInput）：
        #    解析用户输入，得到一个 CommandExecution。
        execution = await dispatch_user_input(user_input, self.context, self.config.cwd)

        # 3) 按命令类型分两条路：
        #    local / local-jsx —— 本地执行的结果直接作为 system 事件吐出，【不进 Agent Loop】。
        if execution.type in (CommandType.LOCAL, CommandType.LOCAL_JSX):
            yield StreamEvent(type="system", text=execution.local_output or "")
            self._save_session()
            return

        #    prompt（含普通对话）—— 把命令产出的内容块作为 user 消息推入历史，进入主循环。
        #    注意：这里用的是 execution.prompt_blocks，而不是原始字符串——
        #    /compact 之类的命令会把输入「改写」成发给模型的真正提示。
        self.messages.append({"role": "user", "content": execution.prompt_blocks, "uuid": uuid.uuid4().hex})

        # 4) 进入主循环，把循环产生的每个事件「流」出去
        async for event in query_loop(self.messages, self.config, self.context):
            yield event

        # 5) 本轮结束：把新增的消息落盘（只在有 session_id 时生效）
        self._save_session()


# =============================================================================
# 七、教学用的【假】Claude API + 一个演示工具
# =============================================================================
#
# mock_call_claude_api：用脚本化逻辑假装自己是 Claude。
#   规则很简单——
#     · 如果历史里【还没有】任何工具结果 → AI「决定」调用 Read 工具读一个真实文件
#     · 如果历史里【已经有】工具结果   → AI 总结一下，纯文本结束（退出循环）
#   这样就能完整演示一轮「问 AI → 调工具 → 把结果喂回 AI → AI 收尾」的循环。
#   注意：mock 现在调的是【真正的】FileReadTool（name="Read"），读的是 demos/edit_test.txt，
#   所以即便不联网、用 mock，你也能看到真实的文件内容被读出来。
_DEMO_FILE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demos", "edit_test.txt")
)
async def mock_call_claude_api(
    messages: List[Message],
    config: QueryEngineConfig,
    context: ToolUseContext,
) -> AsyncGenerator[StreamEvent, None]:
    # 检查历史里是否出现过 tool_result
    has_tool_result = any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in messages
    )

    if not has_tool_result:
        # 第一轮：先说一句话（逐字流式），再发起一个工具调用
        for ch in "好的，我来读一下那个文件。":
            await asyncio.sleep(0.01)  # 模拟网络延迟，制造打字机效果
            yield StreamEvent(type="text_delta", text=ch)
        yield StreamEvent(
            type="tool_use",
            tool_use={
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:8]}",
                "name": "Read",
                "input": {"file_path": _DEMO_FILE},
            },
        )
        yield StreamEvent(type="message_stop")
    else:
        # 第二轮：拿到工具结果后，纯文本总结，不再调工具 → 循环结束
        for ch in "我已经读到了文件内容，任务完成。":
            await asyncio.sleep(0.01)
            yield StreamEvent(type="text_delta", text=ch)
        yield StreamEvent(type="message_stop")


# 注：原来的 FakeReadFileTool（假装读文件、返回固定字符串）已被删除，
# 由真正的 tools/file_read.py::FileReadTool 取代。mock 现在调用 name="Read"，
# 走的就是那个真实工具。
