"""
对应 TS 源码：claude-code-main/src/main.tsx —— 整个程序的【入口点】。

这是把前面三个模块（Tool / commands / QueryEngine）真正串起来、能跑起来的地方。
讲义 main.md 第九节那张架构图的「汇合点」就在这里：无论交互式还是无头模式，
最终都流向 QueryEngine.submit_message()。

⚠️ 教学说明：TS 的 main.tsx 有几百行（MDM 预热、keychain、数据迁移、Commander
   30 个参数、Ink 渲染……），那些是生产工程细节。本文件只复现讲义重点讲的【骨架】：
     · 第三节：交互式 REPL  vs  无头 -p 模式
     · 第四节：用 argparse 解析命令行参数（等价 Commander.js）
     · 第五节：初始化顺序 init() → get_commands → get_tools → 启动
     · 第六节：start_deferred_prefetches —— 首屏后用 asyncio.create_task 并行预热
     · 第八节：Ctrl+C / 退出 信号处理

【不需要 API key、不需要联网】（QueryEngine 默认用 mock），直接运行：

    python my_cc/src/main.py                  # 交互式 REPL
    python my_cc/src/main.py -p "读一下 demo"  # 无头：跑一次后退出
    echo "读一下 demo" | python my_cc/src/main.py  # 管道 → 自动无头
"""

from __future__ import annotations

import os
import sys
import time
import uuid as _uuid
import random
import asyncio
import inspect
import pkgutil
import argparse
import importlib
import itertools
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

# msvcrt：Windows 专属，能「不阻塞地」探测/读取键盘单个按键（用于 Esc 中断）。
# 非 Windows（或被重定向）时不存在，置 None，相关功能自动降级为「不监听 Esc」。
try:
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None  # type: ignore

# 让「直接 python 运行本文件」时也能 import 同目录的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 启动时加载 my_cc/.env，把里面的 ANTHROPIC_* 写进环境变量，
# 这样下面的 select_backend() 才能读到密钥、切到真实模型。
# 注意：必须显式指定 .env 路径——用户通常从 claude-code/ 根目录运行，
#      python-dotenv 默认只会从「当前目录往上找」，找不到子目录 my_cc/.env。
# 用 try/except 兜底：没装 python-dotenv 也不报错，程序照样能用 mock 跑。
from pathlib import Path  # noqa: E402
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"  # = my_cc/.env
    load_dotenv(_ENV_PATH)
except ImportError:
    pass  # 未安装 python-dotenv：跳过，仅影响「从 .env 读密钥」这一便利

from Tool import BaseTool, ToolUseContext  # noqa: E402
from QueryEngine import (  # noqa: E402
    QueryEngine,
    QueryEngineConfig,
    StreamEvent,
)
import tools as _tools_pkg  # noqa: E402  工具包：get_tools() 启动时扫描它，自动发现里面所有工具
from commands import get_commands  # noqa: E402
from session_persistence import (  # noqa: E402
    get_project_dir,
    find_most_recent_session,
    list_sessions,
    load_session_messages,
    validate_uuid,
)


# =============================================================================
# 一、get_tools() —— 自动发现并加载 tools/ 包里的所有工具
# =============================================================================
#
# 对应 TS 的 getTools()（src/tools.ts）。这里有个有意思的【语言分歧】，见 main.md：
#   · TS 版是一张【静态硬编码数组】（手写 import 38 个工具再列进去）+ memoize。它
#     【不能】运行时扫文件系统——Bun 打包做了 tree-shaking，运行时只剩 bundle，没源码目录可扫。
#   · Python 不打包、运行时有完整源码目录，所以我们能做得比原版更「自动」：用 importlib
#     真扫 tools/ 包，把每个 BaseTool 的【具体子类】找出来实例化。以后再加工具，只要丢一个
#     .py 进 tools/ 就自动生效，不用回这里登记（这正是之前加 Glob/Grep 时要手改两处的痛点）。
# memoize 用 functools.lru_cache(maxsize=1) 对应（见映射表）：只在首次调用时扫描一次。
def _is_concrete_tool(obj) -> bool:
    """判断 obj 是不是「可直接实例化的工具类」：BaseTool 的子类、非 BaseTool 本身、且不是抽象类。"""
    return (
        inspect.isclass(obj)
        and issubclass(obj, BaseTool)
        and obj is not BaseTool
        and not inspect.isabstract(obj)
    )


@lru_cache(maxsize=1)
def get_tools() -> List[BaseTool]:
    discovered: Dict[str, BaseTool] = {}
    # iter_modules 列出 tools/ 包里的每个子模块（bash / glob / grep …，不含 __init__）
    for mod_info in pkgutil.iter_modules(_tools_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_tools_pkg.__name__}.{mod_info.name}")
        for _, cls in inspect.getmembers(module, _is_concrete_tool):
            # 只认「定义在本模块」的类：BaseTool 或从别处 import 进来的同名类也会被 getmembers
            # 扫到，这一行把它们排除，保证每个工具只在它的「老家」模块被算一次（天然去重）。
            if cls.__module__ != module.__name__:
                continue
            tool = cls()
            discovered[tool.name] = tool
    # 按 name 排序：保证每次启动工具顺序稳定，否则系统提示词里工具顺序会飘、demo 也没法断言。
    return [discovered[name] for name in sorted(discovered)]


# =============================================================================
# 二、init() —— 初始化各子系统
# =============================================================================
#
# 对应 TS 的 init() + runMigrations()（main.tsx 第五/七节）。
# 真实版在这里：加载配置、认证、连数据库、跑数据迁移。教学版只做最小工作并
# 把命令、工具准备好，返回给调用方。
async def init(cwd: str) -> tuple[List, List[BaseTool]]:
    # —— 数据迁移占位（对应 runMigrations）——
    # 真实版会比较 migration_version，按需把旧模型名/旧配置升级到新格式。
    # 我们没有持久化配置文件，这里只留一句说明，表示「这一步存在」。
    # run_migrations()

    commands = await get_commands(cwd)   # 第五节：加载所有斜线命令
    tools = get_tools()                  # 第五节：加载所有工具
    return commands, tools


# =============================================================================
# 二·补、选后端 + 造引擎 —— 真实模型 or mock
# =============================================================================
#
# select_backend()：检测到密钥就接真实大模型（见 anthropic_api.py），否则返回 None，
# 让 QueryEngine 自动回退到内置 mock。这正是 cc-switch 的思路：靠环境变量切后端。
def select_backend():
    if os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
        from anthropic_api import call_anthropic_api  # 延迟导入：没密钥就不碰它
        base = os.environ.get("ANTHROPIC_BASE_URL") or "Anthropic 官方"
        model = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro")
        print(f"   ⚙️  后端：真实 API（{base} · 模型 {model}）")
        return call_anthropic_api
    print("   ⚙️  后端：mock（未检测到 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY，使用本地假回复）")
    return None  # QueryEngine 见 None 会自动用 mock_call_claude_api


# build_engine()：把「加载命令/工具 + 选后端 + 建引擎」收敛成一处，供两种模式复用。
async def build_engine(
    cwd: str,
    *,
    session_id: str = "",
    project_dir: Path | str = "",
    initial_messages: Optional[List[Dict]] = None,
) -> QueryEngine:
    commands, tools = await init(cwd)
    config = QueryEngineConfig(cwd=cwd, tools=tools, call_claude_api=select_backend())
    return QueryEngine(
        config,
        ToolUseContext(
            read_file_state={},
            abort_event=threading.Event(),
            render_banner=lambda: print(_BANNER),
            session_id=session_id,
            project_dir=str(project_dir) if project_dir else "",
        ),
        initial_messages=initial_messages,
        session_id=session_id,
        project_dir=project_dir,
    )


# =============================================================================
# 三、start_deferred_prefetches() —— 首屏渲染后的并行预热
# =============================================================================
#
# 对应 TS 的 startDeferredPrefetches()（main.md 第六节）。
# 关键思想：这些都是「现在不急、但等下会用」的 I/O，放到首屏之后用后台任务并行做，
# 让用户【感知到的启动时间】更短。Python 用 asyncio.create_task 实现「发射后不管」。
def start_deferred_prefetches() -> None:
    async def _warm(name: str, delay: float) -> None:
        await asyncio.sleep(delay)  # 模拟一次磁盘/网络/子进程 I/O
        # 真实版这里会做：统计文件数、刷新模型能力缓存、拉取用户信息等。
        # 教学版静默完成即可，不打扰 REPL 界面。
        _ = name

    # create_task 不会阻塞——任务在事件循环空闲时（用户思考/打字时）静默跑完。
    asyncio.create_task(_warm("count_files", 0.05))
    asyncio.create_task(_warm("refresh_model_capabilities", 0.05))
    asyncio.create_task(_warm("init_user", 0.05))


# =============================================================================
# 四、渲染一个流式事件到终端（极简 UI 层，对应 Ink 的位置）
# =============================================================================
#
# TS 用 React + Ink 把 StreamEvent 渲染成富终端界面。我们没有 Ink，就用最朴素的
# print 充当「UI 层」。注意 main.py 不关心业务逻辑，只负责把事件画出来——这正是
# 讲义反复强调的「关注点分离」：QueryEngine 产出事件，main 只管显示。
#
# ★ 关键 UI 原则（对应 FileReadTool.ts 里那条注释：UI 只显示 summary chrome）：
#   工具的【完整结果只发给模型】，给【人看的终端只显示一行摘要】，绝不刷全文。
#   否则像 Read 这种会把整个文件 + 行号 + 安全护栏全打到屏幕上，非常冗余。
def _brief(text: str, max_len: int = 200) -> str:
    """把可能很长的文本压成一行：短单行就原样显示，否则给『N 行 / X 字』摘要。"""
    text = (text or "").strip()
    if not text:
        return "（空）"
    if "\n" not in text and len(text) <= max_len:
        return text
    return f"{text.count(chr(10)) + 1} 行 / {len(text)} 字"


def render(event: StreamEvent) -> None:
    if event.type == "text_delta":
        print(event.text or "", end="", flush=True)
    elif event.type == "tool_use":
        tu = event.tool_use or {}
        inp = tu.get("input") or {}
        # 优先只显示 file_path（文件类工具最关心这个），避免 dump 整段 old_string/new_string
        target = inp.get("file_path") or inp.get("path")
        brief = target if target else _brief(str(inp))
        print(f"\n   🛠️  {tu.get('name')}({brief})")
    elif event.type == "tool_result":
        tr = event.tool_result or {}
        content = str(tr.get("content") or "")
        if tr.get("is_error"):
            # 出错必须看见（模型也靠它自我纠正）；只在过长时截断，防刷屏。
            shown = content if len(content) <= 300 else content[:300] + " …(已截断)"
            print(f"   ❌ {shown}")
        else:
            # 成功只显示摘要：Edit 的短句原样显示，Read 的全文收成『N 行 / X 字』。
            print(f"   ↩️  {_brief(content)}")
    elif event.type == "message_stop":
        print()  # 一轮 assistant 说完，换行
    elif event.type == "system":
        print(f"\n   ⚙️  {event.text}")


# =============================================================================
# 四·补、Spinner —— 「转圈 + 计时 + 当前在干嘛」状态行
# =============================================================================
#
# 对应 TS 的 components/Spinner.tsx + constants/spinnerVerbs.ts。
# 痛点：模型在等 API 回复 / 在执行工具时，终端一片空白，用户分不清「在思考」还是「卡死」。
# Claude Code 的解法：在同一行原地刷新一个状态行：
#     ✶ Cogitating… (12s · esc to interrupt)
# 我们没有 React/Ink，用一个 asyncio 后台任务 + '\r'（回到行首）原地重画来实现等价效果。
#
# 三个关键设计：
#   1) 后台任务每 ~80ms 重画一次，靠 '\r' 回到行首覆盖旧内容（不换行）。
#   2) 「延迟出现」：resume 后 150ms 内不画——这样快速的逐字文本流（每个字都 pause/resume）
#      根本来不及触发绘制，避免闪烁；只有真正的长等待（API 延迟、工具执行）才会显形。
#   3) 「文本流期间抑制」：正在逐字输出文本时不画 spinner，否则 '\r' 会把刚打的字覆盖掉。

_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # Braille 转圈帧（对应 Spinner 的 DEFAULT_CHARACTERS）
# 致敬 Claude Code 的随机动名词（spinnerVerbs.ts），这里给一份中文版。
_VERBS = ["思考中", "运筹中", "推敲中", "酝酿中", "琢磨中", "盘算中", "构思中", "捣鼓中"]

# 开机吉祥物：真 Claude Code 启动后会画一只橙色螃蟹，这里换成「戴墨镜的笑脸」。
# 纯字符画，不依赖任何库；emoji 😎 放在标题里点睛。
_BANNER = r"""
   .-----------.
  /   ___   ___ \      😎 Your CC
 |   |___| |___| |
 |               |     输入消息开始对话
  \    \___/    /      /help 看命令
   '-----------'
"""


def _enable_ansi() -> None:
    """Windows：开启控制台 VT 转义，让 \\033[K（擦除到行尾）等 ANSI 序列生效。

    现代 Windows Terminal 默认就支持；老 conhost 需要显式把 ENABLE_VIRTUAL_TERMINAL_
    PROCESSING(0x4) 这一位打开。失败也不要紧（不抛错），大不了退回旧的空格清行。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class Spinner:
    """单行、原地刷新的状态指示器。线程模型：一个 asyncio 后台任务负责画。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._active = False          # 是否处于「该显示」状态
        self._suppress = False        # 文本流期间临时抑制绘制
        self._label = ""              # 当前显示的短语（如「思考中…」「执行工具 Read…」）
        self._verb = random.choice(_VERBS)
        self._turn_start = 0.0        # 本轮开始时刻，用于算 elapsed
        self._resume_at = 0.0         # 最近一次 resume 的时刻，用于 150ms 延迟出现
        self._last_len = 0            # 上次画的字符数，用于清行
        # 只有真正的交互式终端才画；被管道/重定向时（isatty=False）保持静默，避免污染输出。
        self._enabled = sys.stdout.isatty()
        if self._enabled:
            _enable_ansi()  # 开启 \033[K 擦除行尾的支持

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.pause()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def new_turn(self) -> None:
        """一轮新对话开始：重置计时、换个动词、解除抑制。"""
        self._turn_start = time.monotonic()
        self._verb = random.choice(_VERBS)
        self._suppress = False

    def thinking_label(self) -> str:
        return f"{self._verb}…"

    def resume(self, label: Optional[str] = None) -> None:
        """重新进入『等待』状态。label=None 表示沿用上一个短语。"""
        if label is not None:
            self._label = label
        self._resume_at = time.monotonic()
        self._active = True

    def pause(self) -> None:
        self._active = False
        self._clear()

    def suppress(self, on: bool) -> None:
        self._suppress = on
        if on:
            self._clear()

    def _clear(self) -> None:
        # \r 回行首，\033[K 擦到行尾——不管行里有多少中文/emoji（占 2 列）都能清干净。
        # 旧版用「空格 * 字符数」清行，但中文按字符数算偏窄，会漏掉右边的尾巴（如「中断)」残留）。
        if self._enabled and self._last_len:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._last_len = 0

    async def _run(self) -> None:
        frames = itertools.cycle(_SPIN_FRAMES)
        while True:
            await asyncio.sleep(0.08)
            if not (self._enabled and self._active and not self._suppress):
                continue
            # 延迟出现：刚 resume 不久先别画，躲开快速文本流造成的闪烁
            if time.monotonic() - self._resume_at < 0.15:
                continue
            glyph = next(frames)
            elapsed = int(time.monotonic() - self._turn_start)
            text = f"{glyph} {self._label} ({elapsed}s · Esc 中断)"
            # 先 \033[K 擦掉上一帧（含中文/emoji 宽字符），再写新帧——不必再算 pad。
            sys.stdout.write("\r\033[K" + text)
            sys.stdout.flush()
            self._last_len = len(text)  # 仅作「当前有没有画东西」的标记，不再用于清行


# 后台协程：流式期间持续「非阻塞」轮询键盘，按 Esc 就 set 掉 abort_event。
# 为什么能行：API 调用是真异步（AsyncAnthropic + async for），每收到一个事件事件循环
# 都会让出，这个协程便有机会跑一次 msvcrt.kbhit() 探测。设标志后，anthropic_api 里
# 那句 `if context.is_aborted: break` 就会断流 → 回到 REPL（只断这一轮，不退程序）。
async def _watch_esc(abort_event: "threading.Event") -> None:
    if msvcrt is None or not sys.stdin.isatty():
        return  # 非 Windows / 管道重定向：无法逐键监听，安静降级
    try:
        while True:
            while msvcrt.kbhit():           # 缓冲区里有按键就读出来
                ch = msvcrt.getch()
                if ch == b"\x1b":           # Esc
                    abort_event.set()
                    return
                if ch in (b"\x00", b"\xe0"):  # 方向键/功能键是双字节，吞掉第二个字节免误触
                    msvcrt.getch()
            await asyncio.sleep(0.03)        # 让出事件循环，别空转烧 CPU
    except asyncio.CancelledError:
        pass


# 把「驱动 submit_message 的事件流 + 联动 spinner」收敛成一处，供 REPL / 无头复用。
async def _stream_response(engine: "QueryEngine", spinner: Spinner, user_input: str) -> None:
    spinner.new_turn()
    spinner.resume(spinner.thinking_label())  # 首个事件到来前 = API 延迟 = 思考中
    pending_tools: List[str] = []
    # 每轮开始先清掉上一轮可能残留的中断标志，并启动 Esc 监听后台协程。
    abort_event = engine.context.abort_event
    if abort_event is not None:
        abort_event.clear()
    esc_task = asyncio.create_task(_watch_esc(abort_event)) if abort_event is not None else None
    try:
        async for event in engine.submit_message(user_input):
            spinner.pause()        # 先清掉 spinner，再打印这次事件
            render(event)

            if event.type == "text_delta":
                spinner.suppress(True)              # 文本流期间不画，免得 \r 覆盖文字
                spinner.resume()
            elif event.type == "tool_use":
                name = (event.tool_use or {}).get("name") or "工具"
                pending_tools.append(name)
                spinner.suppress(False)
                spinner.resume(f"执行工具 {name}…")
            elif event.type == "message_stop":
                spinner.suppress(False)             # 文本结束，恢复绘制
                # message_stop 之后若有待执行工具 → 即将进入「工具执行」的等待间隙
                if pending_tools:
                    spinner.resume(f"执行工具 {'、'.join(pending_tools)}…")
                else:
                    spinner.resume(spinner.thinking_label())
            elif event.type == "tool_result":
                pending_tools.clear()               # 工具结果回来了，下一步又是 API 调用
                spinner.resume(spinner.thinking_label())
            else:  # system 等
                spinner.resume(spinner.thinking_label())
    finally:
        spinner.pause()
        if esc_task is not None:
            esc_task.cancel()                       # 本轮结束，停掉键盘监听
        if abort_event is not None and abort_event.is_set():
            print("\n   ⛔ 已中断本轮（按 Esc）。")    # 给用户一个明确反馈
            abort_event.clear()                     # 清掉，不影响下一轮


# =============================================================================
# 五、无头模式 run_headless() —— 执行一次查询后退出
# =============================================================================
#
# 对应 TS 的 runHeadless()（main.md 第三节）。触发条件：带 -p 参数，或 stdin 是管道。
# 特点：不进 REPL 循环，submit_message 一次，把事件流打印完就 return。
async def run_headless(prompt: str, cwd: str) -> None:
    engine = await build_engine(cwd)  # 无头模式不持久化（不传 session_id / project_dir）

    spinner = Spinner()
    spinner.start()
    try:
        await _stream_response(engine, spinner, prompt)
    finally:
        await spinner.stop()
    print()  # 收尾换行


# =============================================================================
# 六、交互式 REPL launch_repl() —— while True 读用户输入
# =============================================================================
#
# 对应 TS 的 launchRepl()（main.md 第九节那张图的交互模式分支）。
# 这就是「迷你 Claude Code」的主循环：读输入 → submit_message 分发 → 流式打印。
# 一个引擎实例贯穿整个会话，所以 engine.messages 会跨多轮累积（上下文记忆）。


async def launch_repl(
    cwd: str,
    *,
    session_id: str = "",
    project_dir: Path | str = "",
    initial_messages: Optional[List[Dict]] = None,
) -> None:
    engine = await build_engine(
        cwd,
        session_id=session_id,
        project_dir=project_dir,
        initial_messages=initial_messages,
    )

    # 第六节：首屏渲染之后再启动后台预热，不阻塞界面出现
    start_deferred_prefetches()

    print(_BANNER)

    spinner = Spinner()
    spinner.start()  # 后台绘制任务常驻；平时 pause，等待时 resume
    armed_to_exit = False  # 上一次是不是已经按过一次 Ctrl+C（连按两次才退出）
    try:
        while True:  # ★ REPL 的真身：读一条、处理一条、再读下一条
            try:
                # 「Ctrl+C×2 退出」指引每轮都出现；用内置 input() 读一行。
                #   input() 的 Ctrl+C / Ctrl+D 会抛 KeyboardInterrupt / EOFError，
                #   正好被下面两个 except 分支接住。
                print("\n（Ctrl+C×2 退出）")
                user_input = input("> ").strip()
            except EOFError:
                # Ctrl+D：直接干净退出
                print("\n再见！")
                return
            except KeyboardInterrupt:
                # Ctrl+C：对齐真 Claude Code —— 连按两次才退出，单次只是提醒。
                if armed_to_exit:
                    print("\n再见！")
                    return
                armed_to_exit = True
                print("\n（再按一次 Ctrl+C 退出）", end="", flush=True)
                continue

            armed_to_exit = False  # 成功读到一行 → 重置「待退出」状态
            if not user_input:
                continue

            # ★ 汇合点：无论普通对话还是斜线命令，都交给 submit_message 分发
            #   （_stream_response 内部会联动 spinner，显示思考/执行状态）
            await _stream_response(engine, spinner, user_input)
    finally:
        await spinner.stop()


# =============================================================================
# 七、parse_args() —— 命令行参数解析（对应 Commander.js）
# =============================================================================
#
# 对应 main.md 第四节。Commander.js ≈ Python argparse。真实版有约 30 个参数，
# 这里只留最能体现「模式路由」的几个。
def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude",
        description="Claude Code（教学版）- 终端里的 AI 编程助手",
    )
    parser.add_argument("prompt", nargs="?", default=None, help="初始提问；给了就走无头模式")
    parser.add_argument("-p", "--print", action="store_true",
                        help="无头模式：打印回复到 stdout 后退出")
    parser.add_argument("--cwd", default=".", help="工作目录")
    # 会话持久化（对应真 CC 的 --resume / --continue）
    parser.add_argument("--resume", nargs="?", const=True, default=None,
                        help="从历史会话恢复（不传参数=列出选择；传 UUID=恢复指定会话）")
    parser.add_argument("-c", "--continue", dest="continue_", action="store_true",
                        help="自动恢复本项目最近一次会话")
    return parser.parse_args(argv)


# =============================================================================
# 七·补、resolve_session() —— 处理 --resume / --continue 的后台工作
# =============================================================================
#
# 在 build_engine 之前调用，决定「这次启动是全新会话还是接续历史」。
# 返回 (session_id, loaded_messages)。
async def resolve_session(args: argparse.Namespace, cwd: str) -> tuple:
    project_dir = get_project_dir(cwd)

    # —— --continue：自动接本项目最近一次 ——
    if getattr(args, "continue_", False):
        sid = await find_most_recent_session(project_dir)
        if not sid:
            print("没有找到历史会话，开始新会话。")
            return str(_uuid.uuid4()), []
        print(f"继续最近会话 {sid[:8]}…")
        msgs = await load_session_messages(project_dir, sid)
        return sid, msgs

    # —— --resume ——
    if args.resume is not None:
        if args.resume is True:
            # 不带参数：列出历史会话让用户选
            sessions = await list_sessions(project_dir)
            if not sessions:
                print("没有找到历史会话，开始新会话。")
                return str(_uuid.uuid4()), []
            print("\n📋 历史会话：")
            for i, s in enumerate(sessions):
                ts = datetime.fromtimestamp(s.last_modified).strftime("%m-%d %H:%M")
                print(f"  [{i+1}] {ts}  {s.summary[:60]}")
            try:
                choice = input("\n选择编号（回车 = 新会话）: ").strip()
                if not choice:
                    return str(_uuid.uuid4()), []
                idx = int(choice) - 1
                if 0 <= idx < len(sessions):
                    sid = sessions[idx].session_id
                    msgs = await load_session_messages(project_dir, sid)
                    return sid, msgs
            except (ValueError, EOFError):
                pass
            return str(_uuid.uuid4()), []

        # 带参数：--resume <uuid> 直接恢复指定会话
        sid = args.resume
        if not validate_uuid(sid):
            print(f"无效的会话 ID 格式：{sid}")
            return str(_uuid.uuid4()), []
        msgs = await load_session_messages(project_dir, sid)
        if not msgs:
            print(f"会话 {sid[:8]}… 不存在或为空，开始新会话。")
            return str(_uuid.uuid4()), []
        print(f"恢复会话 {sid[:8]}…")
        return sid, msgs

    # —— 普通启动：全新会话 ——
    return str(_uuid.uuid4()), []


# =============================================================================
# 八、main() —— 程序入口：决定走哪种模式
# =============================================================================
#
# 对应 main.md 第三节的核心分支判断 + 第五节的初始化顺序。
async def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # —— 第三节：判断交互 vs 无头 ——
    # stdin 不是终端（管道/重定向）时，sys.stdin.isatty() 为 False → 自动无头。
    stdin_is_pipe = not sys.stdin.isatty()
    is_non_interactive = bool(args.print) or args.prompt is not None or stdin_is_pipe

    if is_non_interactive:
        # 提示从哪来：优先用位置参数，否则从 stdin 读一整段（管道场景）
        prompt = args.prompt
        if prompt is None and stdin_is_pipe:
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("无头模式需要一个提问（位置参数或通过管道传入）。", file=sys.stderr)
            sys.exit(1)
        await run_headless(prompt, args.cwd)
    else:
        # 交互式模式：先检查是否需要恢复历史会话
        sid, initial_msgs = await resolve_session(args, args.cwd)
        project_dir = get_project_dir(args.cwd) if sid else ""
        await launch_repl(
            args.cwd,
            session_id=sid,
            project_dir=project_dir,
            initial_messages=initial_msgs,
        )


# 对应 TS 文件最底部的执行：if __name__ == "__main__" 等价于「这个文件被直接运行」
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 第八节：最外层兜底的 SIGINT 处理，避免打印丑陋的堆栈
        print("\n已退出。")
