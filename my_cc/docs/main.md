# main.tsx 教学文档：CLI 入口点与启动流程

## 第一节：main.tsx 在整个系统里的位置

到目前为止，你已经学完了四个核心模块：

```
Tool.ts        → 工具的数据结构和权限系统
QueryEngine.ts → Agent Loop（对话轮次管理）
query.ts       → while(true) 主循环
commands.ts    → 斜线命令注册系统
main.tsx       → ← 你现在在这里：一切的起点
```

`main.tsx` 是**程序的入口**，相当于 Python 里的 `if __name__ == "__main__":`。
当用户在终端输入 `claude` 时，第一行被执行的代码就在这里。

它负责：
1. **解析命令行参数**（`claude -p "问题"` 还是 `claude`（交互模式））
2. **初始化各子系统**（配置、认证、MCP、插件、技能目录）
3. **启动交互式 REPL** 或 **执行单次无头查询**
4. **挂载信号处理**（Ctrl+C、进程退出清理）

---

## 第二节：程序启动时做的第一件事——并行预热

`main.tsx` 开头有一段非常特殊的注释：

```typescript
// These side-effects must run before all other imports:
// 1. profileCheckpoint marks entry before heavy module evaluation begins
// 2. startMdmRawRead fires MDM subprocesses (plutil/reg query) so they run in
//    parallel with the remaining ~135ms of imports below
// 3. startKeychainPrefetch fires both macOS keychain reads (OAuth + legacy API
//    key) in parallel — otherwise reads them sequentially (~65ms on every macOS startup)

import { profileCheckpoint } from './utils/startupProfiler.js'
profileCheckpoint('main_tsx_entry')       // ← 第1行就打时间戳

import { startMdmRawRead } from './utils/settings/mdm/rawRead.js'
startMdmRawRead()                          // ← 启动 MDM 设置读取（并行）

import { startKeychainPrefetch } from './utils/secureStorage/keychainPrefetch.js'
startKeychainPrefetch()                    // ← 启动 Keychain 读取（并行）
```

### 为什么要在 import 之间夹 side-effect？

**问题**：TypeScript/JS 的 `import` 语句是同步执行的，会阻塞后续代码。一共有 ~135ms 的模块加载时间。

**解决方案**：在 import 语句执行的间隙，提前启动耗时的**I/O 操作**（读配置、读 keychain）。这些操作在后台并行执行，等到后面真正需要它们的结果时，已经完成了。

---

## 第三节：两种运行模式——交互式 vs 无头

`main.tsx` 最重要的分支判断：**用户是想聊天，还是想让 Claude 执行一次任务然后退出？**

```typescript
// main.tsx, ~line 800
const hasPrintFlag = cliArgs.includes('-p') || cliArgs.includes('--print')
const isNonInteractive = hasPrintFlag || !process.stdout.isTTY
//                       ↑ 有 -p 参数           ↑ 不是终端（如管道、脚本）

const isInteractive = !isNonInteractive
```

| 模式 | 触发条件 | 进入路径 |
|---|---|---|
| **交互式**（Interactive） | 直接运行 `claude` | 启动 REPL，等待用户输入 |
| **无头**（Non-interactive / Headless） | `claude -p "问题"` 或管道 | 执行一次查询后退出 |

---

## 第四节：Commander.js——CLI 参数解析

`main.tsx` 用 Commander.js（等价于 Python 的 `argparse` 或 `click`）声明所有命令行选项：

```typescript
// main.tsx 里的 run() 函数（简化版）
async function run() {
  const program = new CommanderCommand()
    .name('claude')
    .description('Claude Code - AI coding assistant')
    .argument('[prompt]', 'Initial prompt')
    .option('-p, --print', 'Print response to stdout (non-interactive)')
    .option('--model <model>', 'Model to use')
    .option('--resume <sessionId>', 'Resume a previous session')
    .option('--continue', 'Continue the most recent session')
    // ... 约 30 个选项

  program.action(async (prompt, options) => {
    // 这里是主逻辑：根据 options 决定进入交互式还是无头模式
    if (isNonInteractive) {
      await runHeadless(prompt, options)
    } else {
      await launchRepl(...)
    }
  })

  await program.parseAsync(process.argv)
}
```

---

## 第五节：初始化顺序——启动时做了什么？

用户运行 `claude` 后，在看到交互界面之前，程序按顺序做了以下事情：

```
1. profileCheckpoint('main_tsx_entry')     # 记录启动时间
2. startMdmRawRead()                        # 并行读企业 MDM 配置
3. startKeychainPrefetch()                  # 并行读认证令牌
   ↓（~135ms 模块加载）
4. main() 函数入口
   ├── initializeWarningHandler()           # 注册进程警告处理
   ├── 信号处理 process.on('SIGINT', ...)   # 注册 Ctrl+C 处理
   ├── 解析 -p、--continue 等早期标志
   ├── setIsInteractive(isInteractive)      # 设置交互模式标志
   ├── eagerLoadSettings()                  # 提前加载 --settings 参数
   └── run()                                # 进入主流程
        ├── Commander.js 解析参数
        ├── init()                           # 初始化：配置、认证、数据库
        ├── runMigrations()                  # 数据迁移（模型名变更等）
        ├── getCommands(cwd)                 # 加载所有斜线命令
        ├── getTools()                       # 加载所有工具
        ├── getMcpToolsAndCommands()         # 连接 MCP 服务器
        └── launchRepl() / runHeadless()     # 启动主界面或无头模式
```

### get_tools()：硬编码数组 vs 运行时自动发现（一个语言分歧）

`getTools()` 这一步——"程序怎么知道自己有哪些工具"——在 TS 和我们的 Python 版里走了**两条不同的路**，
原因不在偏好，而在两种语言的运行方式：

- **TS 版（`src/tools.ts`）是一张静态硬编码数组**：手写 `import` 把 ~38 个工具逐个引进来，列进一个
  数组，再 `.filter(t => !t.disabled)`，最后用 lodash `memoize` 缓存。它**做不到**运行时扫描文件系统——
  因为 Bun 打包时做了 tree-shaking，发布出去的是一个打好的 bundle，运行时**根本没有源码目录**可以扫。
  所以 TS 只能"手工登记"。

- **Python 版可以做得更"自动"**：我们不打包、运行时有完整的 `tools/` 源码目录，于是用
  `importlib` + `pkgutil` 真正去**扫描这个包**，把每个 `BaseTool` 的【具体子类】找出来实例化。
  以后再加工具，只要往 `tools/` 丢一个 `.py`，启动时就自动被发现——不用再回 `main.py` 和
  `tools/__init__.py` 两处手动登记（这正是之前加 Glob/Grep 时要改两处、漏一处就不生效的痛点）。

实现上的三个关键点：

1. **怎么判断"哪些类是工具"**：`_is_concrete_tool()` 要求同时满足——是类、是 `BaseTool` 子类、
   不是 `BaseTool` 本身、且**不是抽象类**（`inspect.isabstract`）。因为 `BaseTool(ABC, BaseModel)`
   带 `@abstractmethod`，抽象类不能实例化，必须排除。
2. **天然去重**：`inspect.getmembers` 会把模块里 `import` 进来的类也扫出来（比如每个工具文件都
   `from Tool import BaseTool`）。靠一行 `cls.__module__ != module.__name__` 只保留"定义在本模块"的类，
   每个工具只在它的老家被算一次。
3. **memoize 与稳定顺序**：用 `functools.lru_cache(maxsize=1)` 对应 TS 的 `memoize`（只扫一次）；
   结果按 `name` 排序，保证每次启动工具顺序一致——否则塞进系统提示词的工具顺序会飘、demo 也无法断言。

> 一句话：**TS 因为打包只能静态登记，Python 因为运行时有源码可以动态发现**——这是少数几个
> "Python 版比原版更省事"的地方，而不是简化。

---

## 第六节：startDeferredPrefetches()——首屏渲染后的并行预热

`main.tsx` 有一个重要函数：`startDeferredPrefetches()`。
它在 REPL **渲染完第一帧之后**才执行，避免阻塞初始显示：

```typescript
export function startDeferredPrefetches(): void {
  // 这些在用户看到界面后才开始，不阻塞首屏
  void initUser()              // 获取用户信息
  void getUserContext()        // 获取用户上下文
  prefetchSystemContextIfSafe()   // 获取 git 状态等系统信息
  void getRelevantTips()       // 获取相关提示
  void countFilesRoundedRg()   // 统计项目文件数
  void refreshModelCapabilities()  // 刷新模型能力缓存
  void settingsChangeDetector.initialize()  // 监控配置变化
}
```

**为什么要推迟？** 因为这些操作都涉及 I/O（磁盘、网络、子进程），如果在主流程里同步执行，用户会看到延迟。把它们推到首屏渲染之后，用户**感知到的启动时间**更短。

Python 中可以用 `asyncio.create_task()` 实现同样的效果

---

## 第七节：数据迁移系统——runMigrations()

Claude Code 升级时，配置格式或模型名称可能变化。`main.tsx` 维护一个迁移版本号：

```typescript
const CURRENT_MIGRATION_VERSION = 11

function runMigrations(): void {
  if (getGlobalConfig().migrationVersion !== CURRENT_MIGRATION_VERSION) {
    // 按顺序执行所有迁移
    migrateAutoUpdatesToSettings()
    migrateSonnet1mToSonnet45()      // 旧模型名 → 新模型名
    migrateLegacyOpusToCurrent()
    migrateSonnet45ToSonnet46()
    migrateOpusToOpus1m()
    // ...

    // 更新迁移版本号，下次启动不再重复执行
    saveGlobalConfig(prev => ({ ...prev, migrationVersion: CURRENT_MIGRATION_VERSION }))
  }
}
```

**设计原理**：
- 每次加新迁移，`CURRENT_MIGRATION_VERSION` +1
- 用户升级后第一次启动，检测到版本号不匹配，运行所有迁移
- 之后版本号已是最新，迁移被跳过

---

## 第八节：信号与进程生命周期

```typescript
// main.tsx

// 进程退出时恢复光标（避免终端光标消失）
process.on('exit', () => {
  resetCursor()
})

// Ctrl+C 处理
process.on('SIGINT', () => {
  if (process.argv.includes('-p') || process.argv.includes('--print')) {
    // 无头模式：-p 模式有自己的 SIGINT 处理（中断 API 请求），不在这里处理
    return
  }
  process.exit(0)  // 交互模式：Ctrl+C 直接退出
})

// Windows 安全：防止 PATH 劫持
process.env.NoDefaultCurrentDirectoryInExePath = '1'
```

---

## 第九节：整体架构回顾

学完全部四个模块后，Claude Code 的完整流程如下：

```
main.py / main.tsx
  ↓ 解析参数
  ↓ 初始化工具、命令、MCP
  ↓
  ├── 交互模式 → launch_repl()
  │     ↓ 等待用户输入
  │     ↓ 斜线命令 → local: 本地执行  |  prompt: 下发到模型
  │     ↓ 普通输入
  │     └──→ QueryEngine.submit_message()
  │               ↓
  │            query_loop()  ←──────────────────┐
  │               ↓                             │
  │            call_api() → 模型 API            │
  │               ↓                             │
  │            检测 tool_use blocks             │
  │               ↓                             │
  │            run_tools() → BaseTool.call()    │
  │               ↓                             │
  │            附加 tool_result → 返回循环顶部 ──┘
  │               ↓（无 tool_use）
  │            返回最终回复
  │
  └── 无头模式 → run_headless()
        └──→ QueryEngine.submit_message() (同上)
```

每个模块的职责：

| 模块 | 职责 |
|---|---|
| `main.py` | CLI 入口、参数解析、模式路由 |
| `commands.py` | 斜线命令注册、lazy-load、查找 |
| `QueryEngine.py` | 会话状态、submit_message() |
| `query.py` | Agent Loop while(true)、工具分发 |
| `Tool.py` | 工具定义、权限检查、BaseTool |

---

## 思考题

### Q1：启动顺序
`startKeychainPrefetch()` 为什么要在 import 语句之间执行，而不是在 `main()` 函数里执行？

**答案**：JS/TS 的 import 语句是同步的，大约需要 135ms。如果等 import 全部完成再调用 `startKeychainPrefetch()`，这 135ms 里什么都没做。而在 import 期间就启动 prefetch，可以让 I/O 和模块加载**并行**，总启动时间减少约 65ms。

---

### Q2：两种模式区分
用户运行 `echo "帮我写一个 hello world" | claude` 时，程序会进入哪种模式？为什么？

**答案**：无头模式（Non-interactive）。因为 `process.stdout.isTTY`（Python 里是 `sys.stdout.isatty()`）为 `false`——管道中的输入不是一个真实的终端，所以程序判断当前不在交互环境中，直接执行任务后退出。

---

### Q3：startDeferredPrefetches 的时机
如果把 `startDeferredPrefetches()` 移到首屏渲染之前调用，会有什么问题？

**答案**：用户启动时会感觉**变慢**。这些操作（统计文件数、刷新模型能力、初始化用户信息）都有 I/O 开销。放在渲染之前会延迟界面出现的时间；放在渲染之后，用户立刻看到界面，这些工作在"用户输入第一条消息"的时间窗口里静默完成，不影响体验。

---

### Q4：综合设计
假设你要给 Python 版的 `main.py` 加一个 `--json` 参数，让无头模式以 JSON 格式输出结果。请描述需要修改哪些地方（不需要写完整代码，说出位置即可）。

**答案**：
1. `parse_args()` 里增加 `--json` 参数
2. `run_headless()` 里检查 `args.json`：若为 `True`，把每个 chunk 收集起来，最后输出 `json.dumps({"result": "..."})` 而不是直接 print
3. 可能需要给 `QueryEngine.submit_message()` 传入一个 `output_format` 参数，或者在调用端处理输出格式（推荐在调用端处理，保持 QueryEngine 不关心输出格式）

---

## 恭喜！

你已经完成了 CLAUDE.md 推荐的全部四个核心模块的学习：

- ✅ `Tool.ts` — 工具数据结构与权限系统
- ✅ `QueryEngine.ts` — Agent Loop 与流式输出
- ✅ `commands.ts` — 斜线命令注册系统
- ✅ `main.tsx` — CLI 入口点与启动流程

接下来可以：
1. 把四个模块的 Python 代码整合，跑通一个完整的端到端流程
2. 深入研究感兴趣的子系统（MCP、多 Agent、权限系统等）
3. 为 `my_cc` 加入真实的 Anthropic SDK 调用
