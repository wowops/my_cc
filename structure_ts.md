### 🏛️ Claude Code 整体架构拆解

整个系统并不是线性的，而是由多个协同工作的核心模块组成。源码共约 1332 个 TypeScript 文件、200+ 个目录。

---

#### 1. 终端与表现层 (UI & CLI)

**作用：** 负责你在终端里看到的进度条、文字变色，以及解析命令行指令。它不是传统 CLI，而是用前端框架 **React** 写的终端界面。

- `src/main.tsx` — Commander.js CLI 入口，初始化 React/Ink 渲染
- `src/commands.ts` — 斜线命令注册总表（同时 `src/commands/` 目录下有 50+ 个子目录，每个对应一条 `/xxx` 命令的具体实现）
- `src/ink/` — Ink 专属终端组件、键盘事件、布局引擎
- `src/components/` — 所有 React UI 组件（权限弹窗、消息气泡、设置面板等）
- `src/keybindings/` — 键盘快捷键绑定
- `src/vim/` — Vim 模式支持
- `src/voice/` — 语音输入模式

**Python 复现思路：** 用 `rich` 或 `textual` 库平替。

---

#### 2. 核心编排器 (The Brain: Query Engine)

**作用：** 这就是 **"Agent Loop（智能体循环）"**。它把用户问题和系统 Prompt 打包发给 Claude API，判断模型是要"说话"还是"调工具"，并不断循环直到任务完成。

- `src/QueryEngine.ts` — **全项目最核心的文件**，实现流式输出、工具调用循环、thinking 模式、重试、token 计数
- `src/query.ts` — Query 入口适配层
- `src/query/` — 辅助模块：`tokenBudget.ts`（token 预算）、`stopHooks.ts`（停止钩子）、`config.ts`、`deps.ts`

---

#### 3. 工具系统 (The Hands: Tool System)

**作用：** AI 本身没有手脚，不能直接操作电脑。这层代码定义了 ~40 个工具，规定 AI 调工具失败时如何把错误"喂"回模型（Self-Repair 自我修复）。

- `src/Tool.ts` — 工具的基础抽象（接口定义、权限模型、输入 Schema）
- `src/tools/` — 各具体工具目录，包括：
  - 文件操作：`FileReadTool`、`FileWriteTool`、`FileEditTool`、`GlobTool`、`GrepTool`
  - Shell 执行：`BashTool`（含沙箱、安全检查）、`PowerShellTool`
  - 智能体：`AgentTool`（派生子 Agent）、`TeamCreateTool`（并行团队）
  - 任务管理：`TaskCreateTool`、`TaskGetTool`、`TaskListTool`、`TaskUpdateTool`、`TaskStopTool`
  - 调度：`ScheduleCronTool`（含 `CronCreateTool`、`CronDeleteTool`、`CronListTool`）
  - 通信：`SendMessageTool`、`AskUserQuestionTool`
  - 计划模式：`EnterPlanModeTool`、`ExitPlanModeTool`
  - 工作树：`EnterWorktreeTool`、`ExitWorktreeTool`
  - 网络：`WebFetchTool`、`WebSearchTool`
  - MCP：`MCPTool`、`ListMcpResourcesTool`、`ReadMcpResourceTool`、`McpAuthTool`
  - 其他：`SkillTool`、`NotebookEditTool`、`LSPTool`、`SyntheticOutputTool`、`TodoWriteTool`、`ToolSearchTool`、`SleepTool`、`BriefTool`、`REPLTool`、`RemoteTriggerTool`

---

#### 4. 上下文与记忆管理 (Memory & Compaction)

**作用：** 对话太长会撑爆 Token 上限。这套系统决定哪些记忆该压缩保留、哪些该丢弃，以及如何自动提取长期记忆写入文件。

- `src/services/compact/` — **上下文自动压缩算法**（Context Compaction）
- `src/services/extractMemories/` — 自动从对话中提取记忆写入 MEMORY.md
- `src/services/SessionMemory/` — 会话级别的临时记忆
- `src/memdir/` — 记忆目录（memdir）管理
- `src/context.ts` — 上下文对象定义
- `src/history.ts` — 对话历史管理

---

#### 5. 权限与护栏 (Permissions & Guardrails)

**作用：** 防止 AI 擅自执行危险命令。每次工具调用前都会触发权限检查，根据当前模式（`default`/`plan`/`bypassPermissions`/`auto`）自动放行或弹窗询问用户。

- `src/hooks/toolPermission/` — 权限拦截主逻辑，`handlers/` 子目录含各工具专属处理器
- `src/hooks/notifs/` — 通知钩子
- `src/components/permissions/` — 11 种权限弹窗 UI 组件（`BashPermissionRequest`、`FileEditPermissionRequest`、`WebFetchPermissionRequest` 等）

---

#### 6. IDE 桥接层 (Bridge)

**作用：** 让 Claude Code CLI 和 VS Code / JetBrains 插件双向通信，实现 IDE 内嵌使用。

- `src/bridge/bridgeMain.ts` — 桥接入口
- `src/bridge/jwtUtils.ts` — JWT 鉴权
- `src/bridge/sessionRunner.ts`、`replBridge.ts` — REPL 会话管理
- `src/bridge/bridgeMessaging.ts` — 双向消息通道

---

#### 7. 多智能体与任务系统 (Multi-Agent & Tasks)

**作用：** 允许主 Agent 派生出子 Agent 并行工作，或把长任务打包成异步 Task 在后台运行。

- `src/coordinator/` — 子 Agent 编排器
- `src/tasks/` — Task 实现：`LocalAgentTask`、`RemoteAgentTask`、`InProcessTeammateTask`、`DreamTask`
- `src/tools/AgentTool/runAgent.ts` — 子 Agent 运行逻辑
- `src/tools/shared/spawnMultiAgent.ts` — 并行多 Agent 工具

---

#### 8. 服务层 (Services)

**作用：** 各种后台服务，各自独立。

| 服务目录 | 作用 |
|---|---|
| `services/api/` | Anthropic SDK 封装 |
| `services/mcp/` | MCP 协议连接管理 |
| `services/oauth/` | OAuth 2.0 认证 |
| `services/lsp/` | LSP（语言服务协议）管理器 |
| `services/analytics/` | GrowthBook 功能开关（Feature Flags） |
| `services/compact/` | 上下文压缩 |
| `services/extractMemories/` | 自动记忆提取 |
| `services/plugins/` | 插件加载与管理 |
| `services/policyLimits/` | 速率限制与策略 |
| `services/settingsSync/` | 设置同步 |
| `services/teamMemorySync/` | 团队记忆同步 |
| `services/MagicDocs/` | 文档自动生成 |
| `services/autoDream/` | 自动 Dream 任务 |

---

#### 9. 技能与插件系统 (Skills & Plugins)

**作用：** 用户或 Anthropic 可以编写自定义技能（Skill）和插件（Plugin）扩展 Claude Code 的能力。

- `src/skills/` + `src/skills/bundled/` — 内置技能
- `src/plugins/` + `src/plugins/bundled/` — 内置插件
- `src/services/plugins/` — 插件服务层

---

### 📍 学习起点：你应该从哪个文件开始看？

**千万不要从 `main.tsx` 开始看**，因为那是 UI 渲染，会让你迷失在 React 组件里。

**推荐阅读顺序（来自 CLAUDE.md）：**

1. **`src/Tool.ts`** — 先看工具的基础接口定义，弄懂"工具"长什么样
2. **`src/QueryEngine.ts`** — 再看 Agent Loop 如何驱动整个对话循环
3. **`src/commands.ts`** — 了解斜线命令是如何注册和分发的
4. **`src/main.tsx`** — 最后看入口和 CLI 初始化流程

**为什么从 `Tool.ts` 开始：** 所有 Agent 行为都是"工具驱动"的。弄懂了工具接口，你就弄懂了 AI 和现实世界沟通的桥梁——这正是 Python 复现的核心。
