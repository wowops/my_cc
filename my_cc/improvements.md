# 与完整 `.ts` 源码的差距 / 待改进清单

记录当前 Python 复现版（`my_cc/`）相对真正的 Claude Code（`claude-code-main/`）**有意简化或尚未实现**的部分。
这是"下一步复现什么"的总账本；README 只讲已有结构，差距集中在这里。

> 已实现部分见 `README.md`（代码结构）与 `docs/`（各模块实现思路）。

---

## 一、按模块 / 工具的简化

### 工具（`src/tools/`）

| 工具 | 已做 | 相对 TS 的主要简化（详见各 docs） |
|---|---|---|
| Read | 文本读取主线 | 无图片/PDF/Jupyter 多模态；无 token 精确计数（第二道防线）；无去重 stub |
| Edit | 精确替换 + 读改闭环 | 无引号归一化；无 LSP/VSCode 通知、fileHistory、git diff；仅 UTF-8 |
| Bash | 三档权限 + asyncio 执行 | 用"切分+白名单"近似只读判断（非 tree-sitter AST）；无沙箱、后台任务、大输出落盘；防注入花招远不及 2593 行原版 |
| Glob | 按文件名 glob 查找 | 纯 Python `glob`；不读 `.gitignore`、无 offset 分页 |
| Grep | 正则内容搜索（content/files/count 三模式） | 引擎用纯 Python `re` 而非 ripgrep；glob 过滤仅 fnmatch basename；不读 `.gitignore`；无执行超时 |

- [√] `get_tools()` 改为运行时扫描 `tools/` 包自动发现工具（`importlib` + `pkgutil` + `inspect`，
      `lru_cache` memoize、按 name 排序）。以后加工具丢个 `.py` 即可，不再两处手动登记。见 `docs/main.md` 第五节。
- [√] 补搜索工具：`Glob` / `Grep` 已落地（见 `docs/glob.md`、`docs/grep.md`）。仍缺 `LS` 等。
- [√] 补 `Write` 工具：`file_write.py`（创建/覆盖文件，先 Read 后 Write 闭环，staleness check，17 条回归断言）。
- [√] 撤掉 `QueryEngine.build_dir_snapshot()` 临时目录树拐杖 —— Glob/Grep 就位后模型能自己探索目录，已移除。
- [ ] **Grep 默认忽略目录 / `.gitignore`**（与真实 Grep「日常体感」差距最大的一条）：当前 `grep.py::_iter_files`
      只排除 6 个 VCS 目录，会一头扎进 `.venv`/`node_modules`/`__pycache__`/`dist`，结果被噪声淹没。
      真实 Grep 靠 ripgrep 默认遵守 `.gitignore`。最低成本改进：给 `_iter_files` 加一个默认忽略目录集；
      进一步可解析项目根的简易 `.gitignore`。Glob 的 `glob.py::_glob` 同理也吃这个亏。
- [ ] Grep 其余与真实版的功能差距（按需补）：`glob` 过滤支持路径层级 `src/**/*.ts` 与否定 `!`（现仅 fnmatch basename）；
      执行超时（现只有 `is_aborted`）；`multiline` 叠加 `-A/-B/-C` 上下文；UNC 路径安全跳过；结果字符数硬上限。

### 接真实模型（`src/anthropic_api.py`）

- [√] mock → 真实流式调用（Anthropic 兼容端点，已支持 DeepSeek / 真 Claude / Kimi 等）。
- [√] `/compact` 的 `summarize_conversation` 接真实模型摘要 —— 对应 `src/services/compact/compact.ts`（当前 mock）。
      摘要器现在是 `_safe_summarize`：先试真实 API（非流式、关闭 thinking、中文结构化 prompt），失败自动回退 mock。
      Demo 流程不变，无 API 时照样跑。

### 权限系统（`src/Tool.py` + 真 CC 的 `src/hooks/toolPermission/`）

- [√] 写操作要授权（弹窗 / 非交互返回 ASK）。
- [√] **Shift+Tab 模式切换**：default → acceptEdits → plan → bypassPermissions 四档循环；
      acceptEdits 下 Edit/Write/Bash 安全文件命令自动放行；底部 toolbar 显示当前模式指示器。
      对应 TS `getNextPermissionMode.ts` + `modeValidation.ts` + `PromptInputFooterLeftSide.tsx`。
- [ ] 交互式权限提示的**富 UI**（y/n 弹窗仍用 `input()`，尚未换成 prompt_toolkit 对话框）。
- [ ] 权限规则**持久化**（allow/deny/ask 规则现在只在内存里，重启即失）。

### 上下文与会话

- [√] 修 `/clear`：补齐「状态」与「屏幕」两层（详见 `docs/commands.md`「/clear 到底清了什么」）。
      ① 清状态——原先只 `messages.clear()`，现补 `read_file_state.clear()`，对齐 TS
         `clear/conversation.ts` 的 `readFileState.clear()`；否则模型「忘了」读过文件，`Edit` 仍凭旧缓存
         绕过 read-before-edit 校验。
      ② 清屏幕 + 重画 banner——移植 `src/ink/clearTerminal.ts`：用 ANSI 转义码擦屏 + 擦回滚缓冲
         （按平台降级），只在交互式终端（`isatty`）生效，Windows 先开 VT。缺了它 /clear 后旧对话仍留
         在屏上「看起来没生效」。擦完屏再经 `ToolUseContext.render_banner` 回调把开机 banner 重画回来
         （对应真 CC 用 `conversationId` 强制重渲染 logo），否则擦完一片空白显得太秃。
      注：TS `clearConversation()` 其余动作（tasks/MCP/session 存储/hooks/worktree/analytics）均属未复现子系统。
- [ ] `query_loop` 第一步的**真实上下文压缩**（当前占位，不压缩）—— 对应 `src/services/compact/`。
- [√] 会话持久化 / `--resume` / `--continue` —— `session_persistence.py`（对应 TS `sessionStorage.ts` +
      `sessionStoragePortable.ts` + `listSessionsImpl.ts` + `sessionRestore.ts` 的最小子集）。
      ① JSONL 落盘：`~/.my_cc/projects/<sanitize(cwd)>/<uuid>.jsonl`，每轮对话后追加（append-only），uuid 去重；
      ② `--continue` / `-c`：自动接本项目最近一次会话；
      ③ `--resume [UUID]`：列出所有历史会话让用户选（head/tail 64KB lite read，O(会话数)不随文件大小涨）；
      ④ `/resume` 交互式命令：REPL 里列会话、选一个切过去，存旧 → 加载新 → 替换 messages + 清 read_file_state + 擦屏重画 banner。
      MVP 精简约掉的：parentUuid 链（不做分子/去孤儿/compact 截断）、`Project` 批量写入队列（直接 `open("a")`）、
      `--fork-session`、remote ingress / CCR v2、子 agent sidechain、worktree 恢复。
      详见 `docs/session_persistence.md`。
- [√] `/rename` 命令（对应 TS `commands/rename/rename.ts` + `saveCustomTitle`）：
      ① `set_custom_title()` in `session_persistence.py` —— 追加 `custom-title` entry 到 JSONL 尾部，
        `--resume` 选单的 lite read 自动扫到；
      ② `commands/rename.py` —— `/rename [标题]`（local 命令，绕过模型）；
      ③ `/resume` 交互列表里也可以 `r N` 重命名（选单变成 `while True` 循环，重命名后回到列表）。
- [√] `/resume` 会话删除（真 CC 无此功能，my_cc 原创）：
      ① `delete_session()` in `session_persistence.py` —— 直接删除 .jsonl 文件，返回 True/False；
      ② `/resume` 交互列表里 `d N` 删除，带二次确认（y/N），删完后自动从列表移除并刷新。
- [ ] `runMigrations()` 数据迁移（当前 `init()` 里只有占位注释）。

---

## 二、整块尚未触及的子系统

真 CC 的这些大模块当前**完全没有**复现，按价值/难度排序：

- [ ] 更多斜线命令（`/config`、`/cost`、`/status` 等）—— `src/commands/*`。
- [ ] 真正的富终端 UI：用 `rich` / `textual` 替换 `main.py` 里 `render()` 的 `print()` —— 对应 React + Ink。
- [√] **REPL 输入框的富文本行编辑**：已用 `prompt_toolkit` 替换内置 `input()`，支持
      方向键 / Home/End / ↑↓ 翻历史 / Shift+Tab 切换权限模式。**不开 `mouse_support`**
      （之前开启后拖拽选中要按 Shift，体验差）。底部 toolbar 显示当前模式指示器。
- [ ] MCP 连接（外部工具/数据源协议）—— `src/services/mcp/`。
- [ ] 多 Agent / 子任务（`AgentTool`、`TaskCreateTool`、`TeamCreateTool`）—— `src/coordinator/`。
- [ ] IDE ↔ CLI 桥接（VS Code / JetBrains）—— `src/bridge/`。
- [ ] OAuth 2.0 登录、LSP 管理、分析/特性开关、自动记忆抽取 —— `src/services/` 各子目录。
- [ ] 沙箱执行（SandboxManager）、计划模式 / worktree / 定时任务等高级工具。

---

## 三、TS → Python 关键映射（速查）

| TS 概念 | Python 对应 |
|---|---|
| Zod schema | Pydantic `BaseModel` |
| `interface Tool` | `BaseTool(ABC, BaseModel)` |
| `AbortController` | `threading.Event` |
| `async`/`await` | `asyncio` |
| lodash `memoize` | `functools.lru_cache(maxsize=1)` |
| `import('./x.js')`（lazy） | `importlib.import_module("x")` |
| `process.stdout.isTTY` | `sys.stdin.isatty()` |
| Commander.js | `argparse` |
| `void promise`（发射后不管） | `asyncio.create_task()` |
| React + Ink | `render()` 里的 `print()`（计划换 `rich`/`textual`） |
