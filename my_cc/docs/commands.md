# commands.ts 教学文档：斜线命令系统

## 第一节：为什么需要斜线命令？

想象你在使用 Claude Code，有时候你想：
- 输入 `/clear` 清空对话历史（**不需要** Claude 帮你做，本地直接执行）
- 输入 `/help` 看帮助信息（**不需要** Claude 帮你做，本地渲染 UI）
- 输入 `/compact` 压缩上下文（**不需要** Claude 帮你做，本地调函数）

但有时候你想：
- 输入 `/code-review` 让 Claude **真正分析代码**（需要发给 Claude 模型）

所以斜线命令系统需要区分：**"这个命令该让本地执行" vs "该让 Claude 执行"**。

这就是 Command 类型系统存在的原因。

---

## 第二节：三种 Command 类型

TypeScript 里 `Command` 是一个**联合类型**（Union Type），意思是"它可以是这三种之一"：

```typescript
// src/types/command.ts

type Command = CommandBase & (PromptCommand | LocalCommand | LocalJSXCommand)
//                            ↑ 发给 Claude    ↑ 本地执行    ↑ 本地执行+返回 UI
```

### 类型对比表

| 类型 | `type` 字段值 | 执行方式 | 返回值 | 例子 |
|---|---|---|---|---|
| `PromptCommand` | `'prompt'` | 发给模型 | 无（模型直接生成流式回复） | `/code-review`, `/insights` |
| `LocalCommand` | `'local'` | 本地 Python/JS 函数 | 文本字符串 | `/clear`, `/cost`, `/compact` |
| `LocalJSXCommand` | `'local-jsx'` | 本地函数 | React UI 组件 | `/help`, `/status` |

> ⚠️ **注意**：把 `/compact` 认为是 `prompt` 类型，这是**错的**。
> 查真实源码 `claude-code-main/src/commands/compact/index.ts`，`/compact` 是 **`local`** 类型——
> 因为「压缩」不是发一句话给模型，而是要**替换整段历史**，必须本地完成。详见第九节。

### PromptCommand 的关键字段

```typescript
type PromptCommand = {
  type: 'prompt'
  progressMessage: string  // 执行时显示的提示，如 "analyzing your sessions"
  contentLength: number    // 命令内容字符数（用于估算 token 数）
  getPromptForCommand(     // 返回要发给 Claude 的消息内容
    args: string,
    context: ToolUseContext,
  ): Promise<ContentBlockParam[]>
}
```

`getPromptForCommand` 是核心：调用后返回一段消息，QueryEngine 会把它发给 Claude。

### LocalCommand 的关键字段

```typescript
type LocalCommand = {
  type: 'local'
  supportsNonInteractive: boolean  // 是否支持非交互模式（如管道输入）
  load: () => Promise<{ call: LocalCommandCall }>  // ← 重要！lazy-load
}
```

`load` 是一个**函数**，调用时才真正加载实现代码。这叫**延迟加载**（见第四节）。

### LocalJSXCommand 的关键字段

```typescript
type LocalJSXCommand = {
  type: 'local-jsx'
  load: () => Promise<{ call: LocalJSXCommandCall }>  // 加载后返回 React 组件
}
```

---

## 第三节：CommandBase——所有命令共有的字段

`CommandBase` 是三种类型都继承的**基础字段集**：

```typescript
type CommandBase = {
  name: string           // 命令名，如 "clear"、"compact"
  description: string    // 显示给用户的说明
  aliases?: string[]     // 别名，如 clear 的别名是 ["reset", "new"]

  isEnabled?: () => boolean   // 可选：动态判断是否启用（默认 true）
  isHidden?: boolean          // 是否在帮助和补全里隐藏（默认 false）

  userInvocable?: boolean     // 用户能否手动输入这个命令（有些命令仅供内部使用）
  argumentHint?: string       // 参数提示，如 "<optional instructions>"
  isSensitive?: boolean       // 参数是否需要从历史里脱敏
}
```

### 重点：`isEnabled()` 动态开关

```typescript
// src/commands/compact/index.ts
const compact = {
  type: 'local',
  name: 'compact',
  isEnabled: () => !isEnvTruthy(process.env.DISABLE_COMPACT),
  //               ↑ 如果环境变量 DISABLE_COMPACT=true，则该命令不出现
  load: () => import('./compact.js'),
}
```

这样可以通过环境变量或 feature flag 动态禁用某个命令，而不需要修改代码。

---

## 第四节：Lazy-Load 模式——为什么命令要延迟加载？

### 问题

如果程序启动时就把所有 143 个命令的实现代码全部加载，会：
1. **启动变慢**：大量模块被解析、执行
2. **内存浪费**：用户可能只用 `/clear`，但加载了 `/insights`（一个 113KB 的分析模块！）

### 解决方案：load() 函数

每个命令只存储**元数据**（name、description 等），真正的实现代码藏在 `load()` 里：

```typescript
// src/commands/clear/index.ts（只有 19 行！）
const clear = {
  type: 'local',
  name: 'clear',
  description: 'Clear conversation history and free up context',
  aliases: ['reset', 'new'],
  supportsNonInteractive: false,
  load: () => import('./clear.js'),   // ← 只有被调用时，才真正加载 clear.js
}
```

### `/clear` 到底清了什么（不只是消息历史）

`index.ts` 只是元数据；真正干活的是 `clear/clear.ts → conversation.ts` 的 `clearConversation()`。
那个函数很长，但绝大多数操作的对象（tasks、MCP、session 存储、hooks、worktree、analytics）都是
本复现版尚未触及的子系统。**落在我们当前范围内的关键动作只有两个：**

```ts
setMessages(() => [])     // 清空消息历史
readFileState.clear()     // 清空文件状态缓存
```

第二条容易被漏掉，但少了它就是一个真 bug：`read_file_state` 是 `Read` 写、`Edit` 读的
「文件路径 → {时间戳, 内容}」缓存，是 read-before-edit 安全校验的依据。`/clear` 的语义是
「开一个全新会话」——若只清消息、不清缓存，会出现矛盾状态：**模型忘了自己读过文件，工具层却
还留着 clear 前的旧缓存**，于是模型没重新 `Read` 就能让 `Edit` 改文件，保险被旧数据绕过。
所以我们的 `clear.py` 在 `messages.clear()` 之后必须再 `read_file_state.clear()`。

#### 还有第二层：清「屏幕」≠ 清「状态」

上面两条清的都是**状态**——模型「记得」什么。但用户在终端里还看得到 `/clear` 之前
`print()` 出来的旧对话（滚动缓冲 scrollback）。这是**两码事**：

| 层 | 清的对象 | 谁负责 |
|---|---|---|
| 状态 | `messages` + `read_file_state`（模型的记忆） | `conversation.ts` 的 `clearConversation()` |
| 屏幕 | 终端已打印的滚动文字（人眼看到的） | `src/ink/clearTerminal.ts`（UI 层） |

真 CC 两层都做，所以 `/clear` 后屏幕也变干净。我们最初只做了状态层，于是 `/clear` 后旧对话
仍留在屏幕上——**看起来像没生效**（其实模型已经失忆了）。补法是移植 `clearTerminal.ts`：
用 ANSI 转义码 `ERASE_SCREEN(\x1b[2J) + ERASE_SCROLLBACK(\x1b[3J) + CURSOR_HOME(\x1b[H)` 擦屏，
并按平台/终端能力降级（旧 Windows 控制台清不了回滚缓冲，改用 `\x1b[0f` 归位）。

两个工程细节：
- **只在交互式终端擦屏**（`sys.stdout.isatty()`）：管道/无头 `-p` 模式不擦，免得把转义码混进输出。
- **Windows 要先开 VT**：`ENABLE_VIRTUAL_TERMINAL_PROCESSING`（`ctypes` 调 `SetConsoleMode`）。
  Windows Terminal 默认开了，旧 conhost 默认没开——不开的话转义码会原样打成「←[2J」乱码，
  所以开不起来时宁可不擦。

#### 第三层：擦完屏要把 banner 画回来

擦屏后只剩一行「对话历史已清空」，太秃。真 CC 在 `/clear` 时会 `setConversationId(randomUUID())`
**强制重渲染 logo**——所以 clear 后会回到开机界面的样子。我们也照做：擦完屏，把启动时那张
表情符号 banner 重画一遍。

这里有个**分层选择**：擦屏是通用动作（任何终端都一样），但「画什么 banner」是 UI 特有的。
所以不把 banner 内容塞进 `clear.py`，而是让 `main.py` 经 `ToolUseContext.render_banner` 回调注入
（和已有的 `add_notification` / `request_prompt` 是同一种「UI 回调槽」模式）。`clear.py` 擦完屏
若确实擦了（`_clear_terminal_screen()` 返回 True），就调 `context.render_banner()`，不关心画的是什么。
非交互模式擦屏返回 False，自然也不会画 banner。

注：擦屏本属 UI 层（真 CC 在 Ink 里做），我们没有 Ink 事件系统，就近放在 `clear.py` 里直接写
stdout——耦合很轻，且让「/clear 干的所有清理」集中在一处，便于阅读；而真正属于 UI 的「banner
内容」则通过回调留在 `main.py`，不让命令层知道长什么样。

### 特殊例子：insights 命令的极端延迟加载

```typescript
// src/commands.ts（注释原文：insights.ts 是 113KB，3200 行）
const usageReport: Command = {
  type: 'prompt',
  name: 'insights',
  async getPromptForCommand(args, context) {
    // 直到用户真正输入 /insights，才在这里 import 真实模块
    const real = (await import('./commands/insights.js')).default
    return real.getPromptForCommand(args, context)
  },
}
```

连 `getPromptForCommand` 本身也是懒的——函数里面才 `import`。

---

## 第五节：COMMANDS 数组——命令注册中心

所有内置命令都注册在 `commands.ts` 里的 `COMMANDS` 数组中：

```typescript
// src/commands.ts
import memoize from 'lodash-es/memoize.js'  // 记忆化函数，只执行一次

const COMMANDS = memoize((): Command[] => [
  clear,        // local
  compact,      // local
  help,         // local-jsx
  config,       // local-jsx
  // ... 约 80 个内置命令
])
```

`memoize` 的作用：**`COMMANDS()` 第一次调用时构建数组，之后直接返回缓存结果**，不重复构建。

### 条件注册（Feature Flag）

有些命令只在特定条件下注册：

```typescript
// 只有 Anthropic 员工账号才有 INTERNAL_ONLY_COMMANDS
...(process.env.USER_TYPE === 'ant' ? INTERNAL_ONLY_COMMANDS : [])

// 只有开启 BRIDGE_MODE 特性才有 bridge 命令
const bridge = feature('BRIDGE_MODE')
  ? require('./commands/bridge/index.js').default
  : null

// 加入数组时用展开语法，null 自动被过滤
...(bridge ? [bridge] : [])
```

---

## 第六节：getCommands()——对外暴露的入口

外部代码不直接访问 `COMMANDS`，而是调用 `getCommands(cwd)`：

```typescript
export async function getCommands(cwd: string): Promise<Command[]> {
  // 1. 加载所有命令源（内置 + skills + plugins + workflows）
  const allCommands = await loadAllCommands(cwd)

  // 2. 过滤：只保留当前用户有权访问、且已启用的命令
  const baseCommands = allCommands.filter(
    cmd => meetsAvailabilityRequirement(cmd) && isCommandEnabled(cmd),
  )

  return baseCommands
}
```

`meetsAvailabilityRequirement` 检查用户类型（claude.ai 订阅者 vs Console API 用户），`isCommandEnabled` 调用命令自己的 `isEnabled()` 函数。

完整的命令来源（按优先级）：

```
bundledSkills（内置技能）
builtinPluginSkills（内置插件技能）
skillDirCommands（~/.claude/skills/ 目录下的用户自定义命令）
workflowCommands（工作流命令）
pluginCommands（插件命令）
pluginSkills（插件技能）
COMMANDS()（核心内置命令）
```

---

## 第七节：与 QueryEngine 的衔接——命令是怎么被执行的？

用户输入 `/compact` 后，流程如下：

```
用户输入 "/compact"
    ↓
processUserInput() 检测到 "/" 开头
    ↓
getCommands(cwd) → 找到 compact 命令对象
    ↓
根据 command.type 分支：

  if type === 'local':
    const module = await command.load()   // 延迟加载实现
    const result = await module.call(args, context)
    // result 是文本，直接显示，不发给模型

  if type === 'local-jsx':
    const module = await command.load()
    const component = await module.call(onDone, context, args)
    // 渲染 React 组件到终端

  if type === 'prompt':
    const blocks = await command.getPromptForCommand(args, context)
    // 把 blocks 作为用户消息发给模型 → 进入 Agent Loop
```

关键点：**只有 `prompt` 类型的命令才会进入 QueryEngine 的 Agent Loop**。`local` 和 `local-jsx` 命令完全绕过模型。

---

## 第八节：本节总结

| 概念 | 核心要点 |
|---|---|
| Command 联合类型 | `prompt`/`local`/`local-jsx` 三种，决定执行路径 |
| CommandBase | 所有命令共有的元数据字段 |
| `isEnabled()` | 动态开关，支持 feature flag / 环境变量条件 |
| Lazy-load | `load()` 函数推迟真实模块加载，减少启动时间 |
| COMMANDS 数组 | memoize 缓存，避免重复构建 |
| `getCommands()` | 对外入口，合并多源命令 + 过滤不可用命令 |
| 与 QueryEngine 衔接 | `prompt` 命令进入 Agent Loop，`local` 命令直接执行 |

---

## 思考题（请自己回答后再看答案）

### Q1：基础理解
`/help` 是 `local-jsx` 类型，`/code-review` 是 `prompt` 类型。如果用户输入 `/help`，模型会不会收到任何消息？

**答案**：不会。`local-jsx` 命令完全绕过模型，直接在本地渲染 UI 组件并显示给用户。只有 `prompt` 类型的命令才会进入 QueryEngine 的 Agent Loop 并发给模型。

---

### Q2：Lazy-load 场景
假设 `/insights` 命令的实现文件有 113KB（3200 行），但用户 99% 的情况下不使用这个命令。如果不用 lazy-load，会有什么问题？如果用了 lazy-load，实际加载发生在什么时候？

**答案**：
- 不用 lazy-load：每次程序启动都要解析这 113KB 文件，浪费约 0.5-2 秒启动时间，且占用不必要内存。
- 用了 lazy-load：只有用户真正输入 `/insights` 时，`load()` 函数才被调用，真实模块才被 import。对于 99% 不用这个命令的用户，这段代码永远不会被加载。

---

### Q3：综合理解
`getCommands()` 和 `COMMANDS()` 的区别是什么？为什么外部代码应该调用前者而不是后者？

**答案**：
- `COMMANDS()`（内部）：只包含**核心内置命令**，是个 memoized 列表。
- `getCommands(cwd)`（对外）：合并了内置命令 + skills 目录命令 + 插件命令 + 工作流命令，还会过滤掉当前用户无权使用或已禁用的命令。外部代码调用 `getCommands()` 才能获得完整且正确的可用命令集。

---

## 下一步

**下节课**：`src/main.tsx`——CLI 入口点，React/Ink 初始化，整个 Claude Code 是如何启动的。
目标是把 `QueryEngine` + `commands` + `Tool` 三者接进一个真正的终端 REPL 循环
（读用户输入 → `dispatch_user_input` 分发 → 流式打印），串成一个能跑的「迷你 Claude Code」。
