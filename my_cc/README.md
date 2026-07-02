# my_cc —— Claude Code 的 Python 复现

用 Python 复现 Claude Code 的核心架构（对照 TS 源码快照 `claude-code-main/`）。
本文件只讲**当前代码结构与怎么跑**；

- 各模块/工具的**实现思路**见 `docs/`（每个代码文件对应一篇）。
- 当前复现版与完整 `.ts` 源码的**差距清单**见 [`improvements.md`](improvements.md)。

---

## 如何运行

在项目根目录 `claude-code/` 下打开 PowerShell，用 venv 里的 python 运行：

```powershell
# 交互式 REPL（启动后在 > 后输入消息；/help 看命令，Esc 中断思考，Ctrl+C×2 退出）
my_cc\.venv\Scripts\python.exe my_cc\src\main.py

# 无头模式：跑一次就退出
my_cc\.venv\Scripts\python.exe my_cc\src\main.py -p "读一下 demo"

# 会话持久化：自动接上次继续聊
my_cc\.venv\Scripts\python.exe my_cc\src\main.py --continue      # 或 -c

# 列出本项目所有历史会话，挑一个恢复
my_cc\.venv\Scripts\python.exe my_cc\src\main.py --resume
```

**默认用 mock**（本地假回复，无需联网）。想接**真实大模型**（如 DeepSeek）：
打开 `my_cc\.env`，把 `ANTHROPIC_API_KEY=` 换成你的密钥即可——程序启动自动加载 `.env`，
检测到密钥就切真实 API，否则回退 mock。换厂商只改 `.env` 里的端点/密钥/模型名三项
（变量名沿用官方 Claude Code，原理见 `src/anthropic_api.py` 顶部）。

---

## 代码结构

```
my_cc/
├─ src/
│  ├─ main.py            # CLI 入口：模式路由（交互 REPL / 无头 -p / 管道）、REPL 循环、Spinner、render()
│  ├─ QueryEngine.py     # Agent Loop：query_loop 主循环、run_tools 调度、build_system_prompt
│  ├─ anthropic_api.py   # 真实后端：Anthropic 兼容端点（DeepSeek 等），mock 回退
│  ├─ Tool.py            # 工具抽象与权限系统：BaseTool / ToolResult / ToolUseContext / 权限决策表
│  ├─ session_persistence.py  # 会话持久化：JSONL 落盘 / 回放 / 列出历史 / --resume / --continue
│  ├─ tools/             # 具体工具（每个文件配一篇 docs/<name>.md 讲思路）
│  │  ├─ file_read.py    # Read：按绝对路径读文本文件，cat -n 行号
│  │  ├─ file_edit.py    # Edit：精确字符串替换（先 Read 后 Edit 闭环）
│  │  ├─ bash.py         # Bash：执行 shell 命令（权限系统的主战场）
│  │  ├─ glob.py         # Glob：按文件名模式（**/*.py）找文件
│  │  └─ grep.py         # Grep：用正则在文件内容里搜索（纯 Python）
│  └─ commands/          # 斜线命令
│     ├─ clear.py        # /clear   清空对话历史 + 文件缓存 + 终端屏幕
│     ├─ compact.py      # /compact 压缩上下文（摘要当前为 mock）
│     ├─ resume.py       # /resume  列出历史会话并切换（支持 r N 重命名）
│     ├─ rename.py       # /rename  给当前会话起名
│     ├─ help.py         # /help    列出命令
│     └─ code_review.py  # /code-review 产出审查提示，进 Agent Loop
├─ docs/                 # 实现思路文档（见下表）
├─ demos/                # 带断言的回归脚本 + 演示（见下表）
├─ structure_ts.md       # TS 源码结构速览
├─ .env / .env.example   # 后端密钥与配置
└─ requirements.txt
```

### 一条用户输入的数据流

```
main.py（入口/模式路由）
  └─→ QueryEngine.submit_message()
        └─→ commands.dispatch_user_input()        # 斜线命令分发
              ├─ local / local-jsx → 本地执行直接返回（不进模型）
              └─ prompt / 普通对话 → query_loop()   # Agent Loop（while True）
                    └─→ 调后端（mock 或 anthropic_api）
                          └─→ 检测 tool_use → run_tools()
                                └─→ BaseTool.check_permissions() + call()
                                      └─→ 工具结果喂回历史，回到循环顶
```

`run_tools()` 按并发安全性分组：**只读工具并行 / 写工具串行**（`partition_tool_calls`）。
权限决策走 `Tool.py` 的 `check_permissions`——写操作要授权，门挂在 `not is_read_only()`。

---

## docs/ —— 实现思路文档

| 文档 | 对应代码 |
|---|---|
| `docs/Tool.md` | `src/Tool.py`（工具抽象 + 权限系统讲义） |
| `docs/QueryEngine.md` | `src/QueryEngine.py`（Agent Loop 讲义） |
| `docs/commands.md` | `src/commands/`（斜线命令系统讲义） |
| `docs/main.md` | `src/main.py`（CLI 入口讲义） |
| `docs/file_read.md` | `src/tools/file_read.py` |
| `docs/file_edit.md` | `src/tools/file_edit.py` |
| `docs/bash.md` | `src/tools/bash.py` |
| `docs/glob.md` | `src/tools/glob.py` |
| `docs/grep.md` | `src/tools/grep.py` |
| `docs/session_persistence.md` | `src/session_persistence.py` |

> 约定：每新增一个代码文件，配一篇同名 `docs/<name>.md` 记录整体思路；代码内注释只讲局部细节。

---

## demos/ —— 回归脚本 / 演示

集中在 `demos/`（与 `src/`、`docs/` 同级）。带断言的可当回归测试，例：`python my_cc/demos/tool_demo.py`。

| 脚本 | 验证对象 | 类型 |
|---|---|---|
| `demos/tool_demo.py` | Tool.py 权限决策表 + 并发/校验/取消 | ✅ 断言（17 条） |
| `demos/file_edit_demo.py` | Edit：读改闭环 / staleness / 唯一性 / 新建 | ✅ 断言（14 条） |
| `demos/bash_demo.py` | Bash：只读判定 / 高危识别 / 权限决策 / 执行+超时 | ✅ 断言（21 条） |
| `demos/glob_demo.py` | Glob：匹配 / 递归 / 排序 / 目录过滤 / 截断 / 校验 | ✅ 断言（14 条） |
| `demos/grep_demo.py` | Grep：三种 output_mode / 上下文 / glob/type / 分页 / multiline | ✅ 断言（23 条） |
| `demos/tool_discovery_demo.py` | get_tools 自动发现：判据 / 扫描结果 / 有序 / memoize | ✅ 断言（10 条） |
| `demos/commands_demo.py` | 命令分发、别名、memoize、lazy-load、is_enabled | 演示 |
| `demos/QueryEngine_demo.py` | submit_message 两条路径 + /compact 就地压缩 | 演示 |
| `demos/main_demo.py` | 三种运行模式（无头 / 管道判定 / REPL） | 演示 |
| `demos/session_persistence_demo.py` | sanitize / write-read / 去重 / lite read / list / load | ✅ 断言（41 条） |
