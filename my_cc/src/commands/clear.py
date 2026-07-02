"""
/clear 命令的实现。

对应 TS：
    claude-code-main/src/commands/clear/clear.ts        —— 命令入口
    claude-code-main/src/commands/clear/conversation.ts —— clearConversation()：清「状态」
    claude-code-main/src/ink/clearTerminal.ts           —— 清「屏幕」（本文件移植了它）

被 commands/__init__.py 里 clear 命令的 load() 懒加载（importlib.import_module('commands.clear')）。

整体思路见 docs/commands.md「/clear 到底清了什么」。要点：/clear 分两层——
  1) 清状态：messages + read_file_state（模型「记得」的东西）；
  2) 清屏幕：把终端已打印的滚动文字也擦掉（人眼看到的东西）。
缺了第 2 层，/clear 后旧对话仍留在屏幕上，看起来像没生效。
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:                      # 仅类型检查时需要，运行时不 import，避免耦合
    from Tool import ToolUseContext


# =============================================================================
# 擦屏：移植 src/ink/clearTerminal.ts
# =============================================================================
#
# ANSI/CSI 转义码（对应 TS 的 csi.ts 常量）：
_ERASE_SCREEN = "\x1b[2J"          # 清整屏
_ERASE_SCROLLBACK = "\x1b[3J"      # 清回滚缓冲（往上翻也看不到旧内容）
_CURSOR_HOME = "\x1b[H"            # 光标回到左上角
_CURSOR_HOME_WINDOWS = "\x1b[0f"  # 旧版 Windows 控制台的「光标归位」（HVP）


def _is_modern_terminal() -> bool:
    """对应 clearTerminal.ts 的 isModernWindowsTerminal：判断终端是否支持清回滚缓冲。"""
    if sys.platform != "win32":
        return True  # 非 Windows 一律按现代终端处理
    # Windows Terminal 会设 WT_SESSION
    if os.environ.get("WT_SESSION"):
        return True
    # VS Code 集成终端（ConPTY）
    if os.environ.get("TERM_PROGRAM") == "vscode" and os.environ.get("TERM_PROGRAM_VERSION"):
        return True
    # mintty（GitBash / MSYS2）：TERM_PROGRAM=mintty 或设了 MSYSTEM
    if os.environ.get("TERM_PROGRAM") == "mintty" or os.environ.get("MSYSTEM"):
        return True
    return False


def _clear_sequence() -> str:
    """对应 clearTerminal.ts 的 getClearTerminalSequence()：按平台/终端能力拼转义码。"""
    if sys.platform == "win32" and not _is_modern_terminal():
        # 旧版 Windows 控制台清不了回滚缓冲
        return _ERASE_SCREEN + _CURSOR_HOME_WINDOWS
    return _ERASE_SCREEN + _ERASE_SCROLLBACK + _CURSOR_HOME


def _enable_windows_vt() -> bool:
    """
    在 Windows 控制台开启 VT 转义处理（ENABLE_VIRTUAL_TERMINAL_PROCESSING）。
    Windows Terminal 默认已开，但旧 conhost 默认关——不开的话转义码会原样打成「←[2J」乱码。
    返回是否成功；失败就别擦屏（打乱码比不擦更糟）。
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def _clear_terminal_screen() -> bool:
    """
    擦屏。只在真正的交互式终端里做：管道/无头模式（isatty=False）不擦，免得污染输出。
    返回是否真的擦了——擦了才需要让 UI 把 banner 重画回来。
    """
    if not sys.stdout.isatty():
        return False
    if not _enable_windows_vt():  # VT 开不起来就放弃，避免打乱码
        return False
    sys.stdout.write(_clear_sequence())
    sys.stdout.flush()
    return True


# =============================================================================
# 命令入口
# =============================================================================
async def call(args: str, context: "ToolUseContext") -> str:
    """清空对话历史 + 文件缓存 + 终端屏幕。返回给用户看的提示文本。"""
    # ① 清状态：对话历史（保持和 engine.messages 同一个 list 对象，就地清空）
    context.messages.clear()

    # ② 清状态：文件状态缓存（对应 TS clearConversation 的 readFileState.clear()）。
    # 否则 /clear 后模型"忘了"读过文件，但 Edit 仍凭 clear 前的旧缓存绕过
    # read-before-edit 校验——新会话本应要求重新 Read 才能改文件。
    if isinstance(context.read_file_state, dict):
        context.read_file_state.clear()

    # ③ 清屏幕：擦掉终端里残留的旧对话（对应 TS 的 clearTerminal）。
    # 这是 UI 层动作，所以只在交互式终端生效；缺了它 /clear 会"看起来没反应"。
    # ④ 擦屏后让 UI 把开机 banner 重画回来（对应真 CC 用 conversationId 强制重渲染 logo），
    #    否则擦完一片空白、显得太秃。banner 画什么由 main.py 经 context 注入。
    if _clear_terminal_screen() and callable(getattr(context, "render_banner", None)):
        context.render_banner()

    return "对话历史已清空。"
