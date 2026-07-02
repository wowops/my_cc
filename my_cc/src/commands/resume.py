"""
/resume 命令的实现 —— 交互式切换历史会话。

对应 TS：
    claude-code-main/src/commands/resume/resume.tsx     —— 命令入口 + 选单 UI
    claude-code-main/src/utils/listSessionsImpl.ts       —— 列出会话
    claude-code-main/src/utils/sessionRestore.ts         —— 恢复逻辑（我们取最小子集）

核心流程：
    1. 列出当前项目的所有历史会话（只读头/尾各 64KB 取摘要，不读全文件）
    2. 用户选一个（或取消）
    3. 先把当前会话（如果有的话）落盘
    4. 加载目标会话的 messages，替换 context.messages
    5. 清 read_file_state（和 /clear 同理：新会话不应该用旧缓存）
    6. 擦屏 + 重画 banner

整体思路见 docs/session_persistence.md。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Tool import ToolUseContext

# 向 call 所在的包声明「我用到了同级的命令入口约定」
__all__ = ["call"]

# 复用 clear.py 的擦屏逻辑（同目录，按懒加载约定 import）
from commands.clear import _clear_terminal_screen  # type: ignore[import]


def _print_message_history(messages: list) -> None:
    """把加载回来的消息渲染成聊天记录摘要（和 banner 一起构成「恢复成功」的视觉反馈）。

    只取每条消息的前 120 字；工具调用只显示工具名。
    """
    if not messages:
        return

    print("\n" + "━" * 58)
    print(f"  📋 已恢复 {len(messages)} 条消息")
    print("━" * 58)

    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", [])

        texts: list[str] = []
        tool_names: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    t = (block.get("text") or "").replace("\n", " ").strip()
                    if t:
                        texts.append(t)
                elif block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    tool_names.append(name)

        # 跳过纯工具结果消息（user role 只有 tool_result block，没有文本也没有工具调用）
        # —— 这些是模型调工具后喂回来的执行结果，内部记账用的，给用户看没意义。
        if not texts and not tool_names:
            continue

        text = " ".join(texts)
        if len(text) > 120:
            text = text[:120].rstrip() + "…"

        if role == "user":
            prefix = "🙂 你  "
        else:
            prefix = "🤖 助手"

        suffix = f"  [🔧 {', '.join(tool_names)}]" if tool_names else ""
        print(f"  {prefix}: {text}{suffix}")

    print("━" * 58 + "\n")


async def call(args: str, context: "ToolUseContext") -> str:
    """
    /resume [会话编号] —— 列出本项目历史会话并切换。

    交互用法：
        /resume          → 列出所有历史会话，选一个
        /resume <编号>   → 直接切到列表中第 N 个会话
    """
    project_dir = context.project_dir
    session_id = context.session_id

    if not project_dir:
        return "当前没有设置项目目录，无法使用 /resume（非交互式会话不支持）。"

    # —— 延迟导入：到真正执行时才 import 持久化模块 ——
    from session_persistence import (
        list_sessions,
        load_session_messages,
        save_messages,
    )

    sessions = await list_sessions(Path(project_dir))
    if not sessions:
        return "本项目还没有历史会话。"

    # 过滤掉「当前会话」，不让自己出现在列表里（切成自己没意义）
    others = [s for s in sessions if s.session_id != session_id]
    if not others:
        return "当前是本项目唯一的会话。"

    target_sid: str | None = None

    # 用户直接传了编号（如 /resume 2）
    choice_arg = args.strip()
    if choice_arg:
        try:
            idx = int(choice_arg) - 1
            if 0 <= idx < len(others):
                target_sid = others[idx].session_id
            else:
                return f"编号超出范围（1-{len(others)}）。"
        except ValueError:
            # 尝试当作 UUID 处理
            target_sid = choice_arg
    else:
        # 交互式选单（支持选择、重命名、取消）
        from datetime import datetime
        from session_persistence import set_custom_title

        while True:  # 重命名/删除后回到列表，让用户能马上看到结果
            print("\n📋 历史会话（当前项目）：")
            for i, s in enumerate(others):
                ts = datetime.fromtimestamp(s.last_modified).strftime("%m-%d %H:%M")
                # 有自定义标题时优先显示标题（已经在 summary 降级链里处理了）
                print(f"  [{i+1}] {ts}  {s.summary[:60]}")
            print("  [0] 取消  |  [r N] 重命名  |  [d N] 删除")

            try:
                choice = input("\n选择: ").strip()
            except (EOFError, KeyboardInterrupt):
                return "已取消。"
            if not choice:
                return "已取消。"

            # —— 重命名：r <编号> ——
            if choice.lower().startswith("r"):
                parts = choice.split(maxsplit=1)
                if len(parts) < 2:
                    print("  用法：r <编号>（例如 r 1）")
                    continue
                try:
                    ridx = int(parts[1]) - 1
                    if 0 <= ridx < len(others):
                        new_name = input(f"  新标题（{others[ridx].summary[:30]}…）: ").strip()
                        if not new_name:
                            print("  已取消（空标题无效）。")
                            continue
                        set_custom_title(
                            others[ridx].session_id,
                            Path(project_dir),
                            new_name,
                            cwd=os.getcwd(),
                        )
                        # 更新内存中的 summary，让列表立刻反映新标题
                        others[ridx].summary = new_name
                        others[ridx].custom_title = new_name
                        print(f"  ✅ 已重命名为：{new_name}")
                        continue
                    else:
                        print(f"  编号超出范围（1-{len(others)}）。")
                        continue
                except ValueError:
                    print(f"  无效编号：{parts[1]}")
                    continue

            # —— 删除：d <编号> ——
            if choice.lower().startswith("d"):
                parts = choice.split(maxsplit=1)
                if len(parts) < 2:
                    print("  用法：d <编号>（例如 d 1）")
                    continue
                try:
                    didx = int(parts[1]) - 1
                    if 0 <= didx < len(others):
                        target = others[didx]
                        # 二次确认（删文件不可逆）
                        confirm = input(
                            f"  确认删除「{target.summary[:50]}」？此操作不可撤销 (y/N): "
                        ).strip().lower()
                        if confirm != "y":
                            print("  已取消。")
                            continue
                        from session_persistence import delete_session

                        ok = delete_session(target.session_id, Path(project_dir))
                        if ok:
                            print(f"  ✅ 已删除：{target.summary[:50]}")
                            # 从列表中移除，回到循环重新显示
                            others.pop(didx)
                        else:
                            print(f"  ❌ 删除失败（文件可能不存在）。")
                        continue
                    else:
                        print(f"  编号超出范围（1-{len(others)}）。")
                        continue
                except ValueError:
                    print(f"  无效编号：{parts[1]}")
                    continue

            # —— 选择会话 ——
            try:
                idx = int(choice)
                if idx == 0:
                    return "已取消。"
                if 1 <= idx <= len(others):
                    target_sid = others[idx - 1].session_id
                    break  # 退出 while 循环，进入切换流程
                else:
                    print(f"  编号超出范围（1-{len(others)}）。")
            except ValueError:
                print(f"  无效输入：{choice}")

    if not target_sid:
        return "无效的会话标识。"

    target_path = Path(project_dir)  # 和当前会话在同一个项目目录

    # ① 先把当前会话落盘（如果有 session_id）
    if session_id:
        save_messages(
            session_id,
            target_path,
            cwd=os.getcwd(),
            messages=context.messages,
            _checkpoint_index=0,  # 全量保存（确保不丢）
        )

    # ② 加载目标会话
    loaded = await load_session_messages(target_path, target_sid)
    if not loaded:
        return f"会话 {target_sid[:8]}… 加载失败或为空。"

    # ③ 替换当前 messages
    context.messages.clear()
    context.messages.extend(loaded)

    # ④ 更新 session_id（后续的新对话会追到目标会话文件后）
    #    用 object.__setattr__ 绕过 Pydantic 的验证——context 不是 frozen，
    #    但直接赋值也 OK，因为 Pydantic 允许直接属性赋值。
    context.session_id = target_sid

    # ⑤ 清 read_file_state（和 /clear 同理）
    if isinstance(context.read_file_state, dict):
        context.read_file_state.clear()

    # ⑥ 擦屏 + 重画 banner
    if _clear_terminal_screen() and callable(getattr(context, "render_banner", None)):
        context.render_banner()

    # ⑥·补 把加载的历史消息渲染出来（否则用户切完屏只看到 banner，不知道聊过什么）
    _print_message_history(loaded)

    # 找一下摘要用于反馈
    target_summary = "（空）"
    for s in others:
        if s.session_id == target_sid:
            target_summary = s.summary[:60]
            break

    # 异步调度：让调用方（submit_message 里那条 "yield system event"）先返回、
    # 用户看到切换提示；再在后台把本次切换后的 messages 写进目标文件。
    async def _save_resumed():
        """后台任务：把切换后的 messages 落盘到目标 session 文件。"""
        # type: ignore[import] —— 避免循环导入
        save_messages(
            target_sid,
            target_path,
            cwd=os.getcwd(),
            messages=context.messages,
        )

    asyncio.create_task(_save_resumed())

    return f"已切换到会话 {target_sid[:8]}…（{target_summary}）"


# ---------- 导入 Path（只给 call() 用，不污染模块顶层） ----------
from pathlib import Path  # noqa: E402
