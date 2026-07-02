# Tool.ts 讲解笔记

> 对应源码：`claude-code-main/src/Tool.ts`
> 对应实现：`my_cc/src/Tool.py`

---

## 一、为什么需要"工具"？

Claude 大模型本质上只是一个**文字接收器 + 文字生成器**。它没有办法自己执行命令、读写文件、搜索代码。

**"工具"就是给 AI 装上手脚的桥梁。**

整个调用流程如下：

```
你 → "帮我列出当前目录的文件"
                ↓
     Claude 大模型（只会说话）
                ↓
  生成文字："我想用 Bash 工具，命令是 ls"
                ↓
     ← Python/TS 代码真的执行 ls →
                ↓
  把执行结果喂回给 Claude："结果：file1.py file2.py"
                ↓
  Claude 说："当前目录有两个文件：file1.py 和 file2.py"
你 ← 看到最终答案
```

`Tool.ts` 就是这套流程的**接口蓝图**，规定了"一个合法的工具长什么样"。每个具体工具（BashTool、FileReadTool 等）都必须实现这份蓝图。

---

## 二、一个工具必须告诉系统三件事

1. **我叫什么名字** — AI 用名字"点单"
2. **AI 调我时该传什么参数** — 输入格式定义
3. **我被调用时真正执行什么代码** — `call()` 函数

```typescript
// src/Tool.ts 第 362 行（精简版，只看重要字段）
export type Tool = {

  // ① 工具名字 —— AI 通过这个名字点单
  readonly name: string

  // ② 输入参数格式（用 Zod 校验，AI 必须严格按此格式传参）
  readonly inputSchema: Input

  // ③ 真正执行的函数
  call(
    args,           // AI 传来的参数
    context,        // 当前执行环境（见下文）
    canUseTool,     // 权限检查函数
    parentMessage,
    onProgress?,    // 进度回调（显示进度条用）
  ): Promise<ToolResult<Output>>

  // ④ 系统提示词 —— 告诉 AI "这个工具能做什么、怎么用"
  prompt(...): Promise<string>

  // ⑤ 权限检查 —— 执行前要不要弹窗问用户"是否允许"
  checkPermissions(input, context): Promise<PermissionResult>

  // ⑥ 几个重要的"标签"
  isConcurrencySafe(input): boolean   // 可以并发执行吗？
  isReadOnly(input): boolean          // 只读操作？（只读通常不需要问权限）
  isDestructive?(input): boolean      // 危险操作？（如删文件）
  isEnabled(): boolean                // 当前是否可用？
}
```

---

## 三、`ToolUseContext` —— 工具的运行环境

`call()` 的第二个参数 `context: ToolUseContext` 是一个**巨大的对象**，装着工具运行时能用到的一切"全局资源"。

**类比：** 把 `ToolUseContext` 想象成餐厅后厨的"工作台"——每个厨师（工具）干活时都能从工作台上拿到：刀具（工具列表）、食材（消息历史）、对讲机（UI 回调）、计时器（取消信号）。

```typescript
// src/Tool.ts 第 158 行（精简版）
export type ToolUseContext = {
  // 当前对话的消息历史
  messages: Message[]

  // 取消信号 —— 用户按 Ctrl+C 时，工具可以知道"该停下来了"
  abortController: AbortController

  // 读写 UI 状态的回调
  getAppState(): AppState
  setAppState(f): void

  // 在终端 UI 上渲染这个工具的进度组件
  setToolJSX?: SetToolJSXFn

  // 当前是哪个子 Agent 在运行（主 Agent 还是 fork 出来的子 Agent）
  agentId?: AgentId

  // 各种配置（用哪个模型、有哪些工具、debug 模式…）
  options: {
    mainLoopModel: string
    tools: Tools
    // ...
  }
}
```

**Python 等价实现（`Tool.py`）：**

| TypeScript | Python |
|---|---|
| `AbortController` | `threading.Event`（`abort_event`） |
| `getAppState()` | `get_app_state: Callable` |
| `setAppState(f)` | `set_app_state: Callable` |
| `agentId` | `agent_id: Optional[str]` |

---

## 四、`ToolResult` —— 工具执行完返回什么

```typescript
// src/Tool.ts 第 321 行
export type ToolResult<T> = {
  // 核心数据，就是工具执行的结果
  data: T

  // 可选：要追加到消息历史里的新消息
  newMessages?: (UserMessage | AssistantMessage | ...)[]

  // 可选：修改当前 ToolUseContext 的函数
  // （极少数情况：执行完某个工具后需要更新全局状态）
  contextModifier?: (context: ToolUseContext) => ToolUseContext
}
```

Python 版的 `ToolResult` 与此完全对应：`data`、`new_messages`、`context_modifier`。

---

## 五、`description` vs `prompt` —— 两个容易混淆的方法

TS 里 `Tool` 有**两个完全不同用途**的方法，Python 版目前用 `get_description` 混合了这两件事，以后实现 Agent Loop 时需要拆开：

| 方法 | 时机 | 用途 | 例子 |
|---|---|---|---|
| `prompt()` | 对话开始前注入 System Prompt | 静态描述，告诉 AI "这个工具能做什么" | `"Use the Read tool to read files from disk."` |
| `description()` | AI 真正调用某次工具时 | 动态说明，显示在 UI 上给用户看 | `"Reading file /src/Tool.ts (line 1-100)"` |

---

## 六、`ToolPermissionContext` —— 权限的配置对象

权限系统的核心配置单独抽了出来（Python 版目前还缺这个）：

```typescript
// src/Tool.ts 第 123 行
export type ToolPermissionContext = {
  // 当前权限模式：
  //   'default'          — 正常模式，危险操作弹窗问用户
  //   'plan'             — 计划模式，只能读不能写
  //   'bypassPermissions'— 跳过所有权限检查（--dangerouslySkipPermissions）
  //   'auto'             — 自动模式，按规则自动批准
  mode: PermissionMode

  alwaysAllowRules: {...}  // 永久允许规则（用户设置的白名单）
  alwaysDenyRules:  {...}  // 永久拒绝规则
  alwaysAskRules:   {...}  // 永久弹窗规则
  isBypassPermissionsModeAvailable: boolean
}
```

**调用链：** `checkPermissions()` 收到这个对象后，根据 `mode` 决定：
- 直接放行？
- 直接拒绝？
- 弹窗问用户？

---

## 七、学习路线提示

`Tool.ts` 是**所有工具的接口定义**，理解它之后：

- **下一站 → `src/QueryEngine.ts`**：Agent Loop 的核心，驱动整个"AI 调工具 → 拿结果 → 继续对话"的循环
- **再之后 → 具体工具**：比如 `src/tools/BashTool/`，看一个真实工具是如何实现这套接口的

---

## 八、思考题（自测）

做完这些题，说明你真正理解了这节课。

---

**Q1（基础概念）**

Claude 大模型自己能不能直接执行 `ls` 命令？如果不能，它是怎么"让别人帮它执行"的？请用自己的话描述完整的调用链。

<details>
<summary>参考答案（先自己想，再展开）</summary>

不能。Claude 只能生成文字。它会在回复里生成一段结构化的"工具调用请求"，格式大致是 `{ name: "Bash", args: { command: "ls" } }`。外层的 Python/TS 代码（QueryEngine）检测到这段请求后，真正去执行 `ls`，然后把执行结果作为新消息塞回给 Claude，Claude 再根据结果生成最终的自然语言回答。

</details>

---

**Q2（接口理解）**

一个合法的工具必须实现哪三件最核心的事？（不是背字段名，而是用自己的话说它们各自的**作用**）

<details>
<summary>参考答案</summary>

1. **声明名字**：AI 用名字来"点单"，名字不对就找不到工具。
2. **定义输入格式**：规定 AI 传参时必须遵守的格式，防止 AI 乱传参数导致崩溃。
3. **实现 `call()` 函数**：工具的"肉体"，被调用时真正执行的代码逻辑。

</details>

---

**Q3（关键区分）**

`prompt()` 和 `description()` 都是 `Tool` 接口上的方法，但它们的**调用时机**和**服务对象**完全不同。请解释这两者的区别，并各举一个返回内容的例子。

<details>
<summary>参考答案</summary>

- `prompt()`：在对话**开始前**就被调用，内容注入进 System Prompt，服务对象是**AI 模型**（让 AI 知道这个工具能做什么）。例子：`"Use the Bash tool to run shell commands."`
- `description()`：在 AI **某次具体调用**这个工具时才被调用，内容显示在终端 UI 上，服务对象是**用户**（让用户看到 AI 正在做什么）。例子：`"Running: ls -la /src"` 或 `"Reading file /src/Tool.ts (line 1–100)"`

</details>

---

**Q4（上下文理解）**

`ToolUseContext` 里有一个 `abortController`（Python 版里是 `abort_event`）。请解释：这个字段解决了什么问题？如果没有它，会发生什么？

<details>
<summary>参考答案</summary>

它解决"**用户想中途取消任务**"的问题。比如用户按了 Ctrl+C，`abortController` 会发出取消信号。每个工具在执行长时间操作（比如运行一个耗时脚本）时，可以定期检查这个信号，一旦发现被取消就立刻停止，释放资源。

如果没有它，一旦 AI 开始执行一个耗时工具，用户就无法中途叫停，只能等它跑完。

</details>

---

**Q5（权限系统）**

`ToolPermissionContext` 里有一个 `mode` 字段，有四种取值。请解释 `'plan'` 模式和 `'bypassPermissions'` 模式分别在什么场景下会被激活，它们各自的**限制**是什么？

<details>
<summary>参考答案</summary>

- `'plan'` 模式：用户或 AI 进入"计划模式"（`/plan` 命令）时激活。在这个模式下，AI **只能读取**文件和信息，不能写入、删除或执行任何修改操作。目的是让 AI 先"只看不动"、制定好计划，等用户确认后再真正执行。
- `'bypassPermissions'` 模式：用户传入 `--dangerouslySkipPermissions` 参数时激活。在这个模式下，**所有权限检查被跳过**，工具可以直接执行任何操作而不弹窗询问。适用于完全自动化的脚本场景，但非常危险，一般不推荐。

</details>
