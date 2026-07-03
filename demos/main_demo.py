"""
main.py 的教学演示脚本。

把 main.py 的三种运行模式各跑一遍，固化成可重跑的回归脚本：
    · 无头 -p         —— run_headless()，执行一次 Agent Loop 后结束
    · 管道自动无头     —— isatty()=False 的判定逻辑（这里直接验证判定，不真正起子进程）
    · 交互式 REPL     —— launch_repl()，用 monkeypatch 假装用户连续输入

【不需要 API key、不需要联网】，直接运行：

    python my_cc/demos/main_demo.py

注意：真正的命令行体验请直接跑 main.py（见文件末尾几行注释）。本文件是「在 Python
     里把 main 的各入口函数驱动一遍」，方便以后改了 main.py 后一键回归。
"""

from __future__ import annotations

import os
import sys
import builtins
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import main  # noqa: E402


async def demo() -> None:
    print("=" * 64)
    print("演示 1：无头模式 run_headless() —— 跑一次完整 Agent Loop 后退出")
    print("=" * 64)
    await main.run_headless("读一下 edit_test.txt", cwd=".")

    print("\n" + "=" * 64)
    print("演示 2：模式判定 —— 管道/重定向时 isatty()=False 会自动走无头")
    print("=" * 64)
    # 不真正起子进程，只演示 main.main() 里那行判定逻辑的依据。
    print(f"→ 当前 sys.stdin.isatty() = {sys.stdin.isatty()}")
    print("  （直接敲 python main.py 时为 True→交互；`echo ... | python main.py` 时为 False→无头）")

    print("\n" + "=" * 64)
    print("演示 3：交互式 REPL launch_repl() —— monkeypatch 假装用户连续输入三条")
    print("=" * 64)
    # 把内置 input 换成一个「按脚本吐字」的假函数，模拟用户敲键盘。
    scripted = iter([
        "你好，帮我读个文件",  # 普通对话 → 进 Agent Loop
        "/help",               # local-jsx 命令 → 本地执行，可看到 lazy-load
        "/quit",               # 跳出 REPL
    ])
    original_input = builtins.input
    builtins.input = lambda prompt="": next(scripted)
    try:
        await main.launch_repl(cwd=".")
    finally:
        builtins.input = original_input  # 恢复，避免污染后续

    print("\n🎉 三种模式演示完毕。要体验真·命令行，请直接运行：")
    print("     python main.py                 # 交互式")
    print('     python main.py -p "读一下 demo" # 无头')


if __name__ == "__main__":
    asyncio.run(demo())
