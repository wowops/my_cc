# QueryEngine.ts 讲解笔记

> 对应源码：`claude-code-main/src/QueryEngine.ts` + `src/query.ts` + `src/services/tools/toolOrchestration.ts`
> 下一步实现：`my_cc/src/QueryEngine.py`

---

## 一、Agent Loop 是什么？为什么需要它？

上一节我们学了 `Tool.ts`——工具的"接口蓝图"。但光有接口没有用，必须有一个**驱动引擎**来：

1. 把用户的问题发给 Claude API
2. 拿到 Claude 的回复
3. 判断：Claude 是要"说话结束"还是"调工具"？
4. 如果要调工具 → 执行工具 → 把结果塞回 Claude → 回到第 1 步

这个**不停循环**直到任务完成的东西，就叫 **Agent Loop（智能体循环）**。

```
╔══════════════════════════════════════════╗
║              Agent Loop                  ║
║                                          ║
║  用户提问 ──→ 发送给 Claude API           ║
║                    ↓                     ║
║            Claude 回复（流式）            ║
║                    ↓                     ║
║         ┌── 只是说话？──→ 输出给用户，结束 ║
║         │                                ║
║         └── 要调工具？──→ 执行工具         ║
║                              ↓           ║
║                  把工具结果塞回消息历史    ║
║                              ↓           ║
║                    再次发给 Claude API ── ╝
╚══════════════════════════════════════════╝
```

**类比：** Agent Loop 就像一个**不知疲倦的接线员**。它一直在：接收 Claude 的"需求"→ 帮 Claude 做事 → 把结果汇报给 Claude → 等待下一条指令，直到 Claude 说"好了，我完成了"。

---

## 二、源码里谁负责 Agent Loop？

TS 源码把 Agent Loop 拆成了两个层次：

| 文件 | 职责 | 类比 |
|---|---|---|
| `QueryEngine.ts` | **会话管理器**：维护整个对话的状态（消息历史、Token 统计、权限记录） | 项目经理 |
| `query.ts` 中的 `queryLoop()` | **真正的循环体**：`while(true)` 不停调 Claude API、处理工具 | 流水线工人 |
| `toolOrchestration.ts` 中的 `runTools()` | **工具调度器**：决定哪些工具并行、哪些串行执行 | 调度员 |

---

## 三、QueryEngine 类 —— 会话管理器

```typescript
// src/QueryEngine.ts 第 184 行（精简版）
export class QueryEngine {
  private mutableMessages: Message[]      // 当前对话的完整消息历史
  private abortController: AbortController // 取消信号
  private totalUsage: NonNullableUsage    // 累计 Token 消耗统计

  constructor(config: QueryEngineConfig) {
    this.mutableMessages = config.initialMessages ?? []
    this.abortController = config.abortController ?? createAbortController()
    // ...
  }

  // ★ 核心方法：提交一条新消息，返回一个异步生成器（流式输出）
  async *submitMessage(
    prompt: string,
  ): AsyncGenerator<SDKMessage> {
    // 1. 构建 System Prompt（把所有工具的 prompt() 注入进去）
    // 2. 处理斜线命令（/clear, /compact...）
    // 3. 把用户消息推入 mutableMessages
    // 4. 调用 query()，进入真正的循环
    for await (const message of query({ messages, systemPrompt, ... })) {
      // 把循环里产生的每条消息"流"出去
      yield* normalizeMessage(message)
    }
  }
}
```

**关键理解：** `submitMessage()` 是一个 **`async generator`（异步生成器）**。它不是一次性返回结果，而是像水龙头一样，每产生一条消息就 `yield` 出去，调用方可以边收边显示。

Python 里对应的是 `async def submitMessage(...): yield ...`（异步生成器函数）。

---

## 四、queryLoop() —— 真正的死循环

这是整个系统的心脏。在 `src/query.ts` 的 `queryLoop()` 函数里：

```typescript
// src/query.ts 第 241 行（精简版，去掉大量细节）
async function* queryLoop(params): AsyncGenerator<...> {
  let state = { messages: params.messages, ... }

  // ★ 这就是 Agent Loop 的真面目：一个永不退出的循环
  while (true) {
    const { messages, toolUseContext } = state

    // --- 第一步：处理上下文压缩（消息太多时自动压缩） ---
    let messagesForQuery = compactIfNeeded(messages)

    // --- 第二步：调用 Claude API（流式）---
    const toolUseBlocks = []
    let needsFollowUp = false

    for await (const message of callModel({ messages: messagesForQuery, ... })) {
      yield message  // 把流式内容实时传出去（用户看到的打字效果就是这里）

      if (message.type === 'assistant') {
        // 检测 Claude 是否要调工具
        const toolBlocks = message.content.filter(b => b.type === 'tool_use')
        if (toolBlocks.length > 0) {
          toolUseBlocks.push(...toolBlocks)
          needsFollowUp = true  // ← 标记"需要继续循环"
        }
      }
    }

    // --- 第三步：如果 Claude 不需要调工具，退出循环 ---
    if (!needsFollowUp) {
      return { reason: 'stop' }  // 正常结束
    }

    // --- 第四步：执行 Claude 要求的工具 ---
    for await (const update of runTools(toolUseBlocks, ...)) {
      if (update.message) {
        yield update.message    // 把工具执行过程也流出去
        messages.push(update.message)  // 工具结果加入消息历史
      }
    }

    // --- 第五步：把工具结果追加到消息里，回到 while(true) 顶部 ---
    state = { ...state, messages: [...messages] }
    // ↑ 下一轮循环会拿着包含工具结果的新 messages 再次问 Claude
  }
}
```

**流程用中文总结：**

```
while (永远) {
    1. 整理消息历史（可能压缩）
    2. 调 Claude API → 流式拿回复
    3. 回复里有工具调用吗？
       没有 → return，结束整个循环
       有  → 执行工具，把结果追加到消息历史
    4. 带着新消息历史，继续 while 下一圈
}
```

---

## 五、工具是怎么被执行的？

当 `queryLoop` 发现 Claude 要调工具时，它调用 `runTools()`。

```typescript
// src/services/tools/toolOrchestration.ts 第 19 行（精简版）
export async function* runTools(
  toolUseBlocks: ToolUseBlock[],   // Claude 要求调用的工具列表
  canUseTool: CanUseToolFn,        // 权限检查函数
  context: ToolUseContext,
) {
  // ★ 关键优化：把工具分成两组再执行
  for (const { isConcurrencySafe, blocks } of partitionToolCalls(toolUseBlocks)) {
    if (isConcurrencySafe) {
      // 只读工具 → 全部并行执行（速度快）
      for await (const update of runToolsConcurrently(blocks, ...)) {
        yield update
      }
    } else {
      // 写操作工具 → 逐个串行执行（保证安全）
      for await (const update of runToolsSerially(blocks, ...)) {
        yield update
      }
    }
  }
}
```

**核心规则：**
- `is_concurrency_safe()` 为 `True`（只读操作）→ **并行执行**，比如同时读 3 个文件
- `is_concurrency_safe()` 为 `False`（写/危险操作）→ **串行执行**，一个接一个

这就是为什么 `Tool.py` 里要定义 `is_concurrency_safe()` 方法——它直接影响工具执行的并发策略。

---

## 六、流式输出是怎么工作的？

Claude Code 的回复不是一次性给你的，而是**逐字流式输出**的（就像 ChatGPT 那样）。这是通过**异步生成器**实现的。

整个调用链，每一层都用 `yield` 往外"冒泡"：

```
Claude API 流式响应
    ↓ yield (每个 token)
callModel() 异步生成器
    ↓ yield
queryLoop() 异步生成器
    ↓ yield
query() 异步生成器
    ↓ yield
QueryEngine.submitMessage() 异步生成器
    ↓ yield
调用方（UI / SDK）实时显示
```

---

## 七、QueryEngineConfig —— 配置对象

`QueryEngine` 初始化时需要一个巨大的配置对象，把所有依赖都注入进来：

```typescript
// src/QueryEngine.ts 第 130 行
export type QueryEngineConfig = {
  cwd: string                    // 当前工作目录
  tools: Tools                   // 所有可用工具列表
  commands: Command[]            // 斜线命令列表
  mcpClients: MCPServerConnection[] // MCP 服务器连接
  canUseTool: CanUseToolFn       // 权限检查函数（由外部注入）
  getAppState: () => AppState    // 读全局状态
  setAppState: (f) => void       // 写全局状态
  initialMessages?: Message[]    // 初始消息（用于 /resume）
  customSystemPrompt?: string    // 自定义系统 Prompt
  maxTurns?: number              // 最大循环圈数（防无限循环）
  maxBudgetUsd?: number          // 最大花费上限（美元）
  thinkingConfig?: ThinkingConfig // 是否启用 extended thinking
  // ...
}
```

**重要字段解释：**

| 字段 | 作用 | 不设会怎样 |
|---|---|---|
| `maxTurns` | 限制 while 循环最多跑多少圈 | 可能无限循环，耗尽 Token |
| `maxBudgetUsd` | API 费用上限 | 可能花很多钱 |
| `canUseTool` | 权限检查，外部注入 | 工具无法检查是否允许执行 |
| `abortController` | 取消信号 | 用户按 Ctrl+C 无法停止 |

---

## 八、System Prompt 是怎么构建的？

`submitMessage()` 每次被调用时，都会先构建 **System Prompt（系统提示词）**：

```typescript
// src/QueryEngine.ts 第 289 行（概念版）
const { defaultSystemPrompt, ... } = await fetchSystemPromptParts({
  tools,      // 把每个工具的 prompt() 方法调一遍，拼接进去
  mainLoopModel,
  mcpClients,
  customSystemPrompt,
})
```

**系统 Prompt 的来源（按顺序拼接）：**

1. Claude Code 默认的系统 Prompt（告诉 AI 它是 Claude Code、当前目录等）
2. **每个工具的 `prompt()` 方法的返回值**（告诉 AI 有哪些工具、怎么用）
3. 用户自定义的 `customSystemPrompt`（来自 `CLAUDE.md` 文件）
4. 附加的 `appendSystemPrompt`

这就解释了为什么 `Tool.ts` 里要有 `prompt()` 方法——它在这里被消费。

---

## 九、Python 版本的对比总结

| TypeScript 概念 | Python 等价 | 当前状态 |
|---|---|---|
| `class QueryEngine` | `class QueryEngine` | ⚠️ 尚未实现 |
| `async *submitMessage()` | `async def submit_message(): yield ...` | ⚠️ 尚未实现 |
| `async function* queryLoop()` | `async def query_loop(): yield ...` | ⚠️ 尚未实现 |
| `for await (const msg of callModel(...))` | `async for chunk in call_claude_api(...)` | ⚠️ 尚未实现 |
| `runTools()` 并行/串行调度 | `async def run_tools()` + `asyncio.gather()` | ⚠️ 尚未实现 |
| `QueryEngineConfig` | `QueryEngineConfig(BaseModel)` | ⚠️ 尚未实现 |
| `AbortController` | `threading.Event` / `asyncio.Event` | ✅ Tool.py 已有 |
| `ToolUseContext` | `ToolUseContext(BaseModel)` | ✅ Tool.py 已有 |

**实现优先级（从简到难）：**

1. `QueryEngineConfig`（配置数据类，最简单）
2. `async def call_claude_api()` 调用 Anthropic SDK
3. `async def query_loop()` while True 主循环
4. `async def run_tools()` 工具调度
5. `class QueryEngine` 整合上面四个

---

## 十、学习路线提示

理解了 Agent Loop 后：

- **下一站 → `src/commands.ts`**：斜线命令（`/clear`、`/compact`、`/plan`）的注册与分发机制
- **或者 → 动手实现**：把 `QueryEngine.py` 写出来，这是整个项目最核心的 Python 文件

---

## 十一、思考题（自测）

---

**Q1（基础）**

Agent Loop 为什么必须是一个**循环**，而不是"问一次 Claude，拿到答案，结束"？

<details>
<summary>参考答案</summary>

因为 Claude 可能需要**多次调用工具**才能完成一个任务。比如用户说"帮我找到项目里所有 TODO 注释并整理成表格"，Claude 可能需要：
1. 先调 GrepTool 搜索 TODO
2. 看到结果后再调 FileReadTool 读具体文件
3. 最后才能生成表格

每次工具调用都是一个"问 Claude → 执行工具 → 把结果告诉 Claude → 再问"的循环。不循环就只能执行一次工具，无法完成复杂任务。

</details>

---

**Q2（流式输出）**

为什么 `queryLoop()` 和 `submitMessage()` 都使用 `async generator`（`async function*` / Python 里的 `async def + yield`），而不是直接 `return` 最终结果？

<details>
<summary>参考答案</summary>

因为 Claude API 的输出是**流式的**（streaming），而用户期望看到**实时打字效果**而不是等待很久再看到完整结果。

使用 async generator：
- API 每返回一个 token，就立刻 `yield` 出去给 UI 显示
- 用户看到文字逐字出现，体验好
- 工具执行的过程（比如"正在读取文件…"）也能实时显示

如果改成 `return`，必须等所有 token 都到齐、所有工具都执行完，用户才能看到结果，体验极差。

</details>

---

**Q3（工具调度）**

`runTools()` 为什么要把工具分成"只读批次"和"写操作批次"分别执行？如果全部串行执行会怎样？如果全部并行执行又会怎样？

<details>
<summary>参考答案</summary>

- **全部串行**：安全但慢。比如 Claude 同时要读 5 个文件，却得一个一个读，浪费时间。
- **全部并行**：快但危险。如果 Claude 同时要写两个文件，并行执行可能导致竞争条件（race condition），比如两个工具同时修改同一个文件，结果不可预期。

分批执行是折中方案：
- **只读操作**（`is_concurrency_safe() = True`）→ 并行，因为读不会相互干扰
- **写操作**（`is_concurrency_safe() = False`）→ 串行，确保每次只有一个修改在进行

</details>

---

**Q4（System Prompt 构建）**

在 `submitMessage()` 里，System Prompt 的内容是从哪里来的？和 `Tool.ts` 里的哪个方法有关联？

<details>
<summary>参考答案</summary>

System Prompt 由多个部分拼接而成，其中**最关键的部分**来自每个 `Tool` 的 `prompt()` 方法。

`fetchSystemPromptParts()` 会遍历所有可用工具，对每个工具调用 `tool.prompt()` 方法，把返回的字符串（如 `"Use the Bash tool to run shell commands on this machine..."`) 拼进 System Prompt 里。

这就是为什么工具必须实现 `prompt()` 方法：它是"告诉 AI 我能做什么"的接口，在每次对话开始前注入。

</details>

---

**Q5（循环终止）**

Agent Loop 如何知道"该停下来了"？有哪些情况会让循环退出？

<details>
<summary>参考答案</summary>

循环退出的条件（来自 `queryLoop` 的返回值 `Terminal`）：

1. **正常停止**：Claude 的回复里没有 `tool_use` 块（`needsFollowUp === false`），说明 AI 已经完成任务，只是在说话，不需要再调工具了。
2. **超过最大轮次**（`max_turns`）：循环圈数超过 `config.maxTurns`，强制退出，防止无限循环。
3. **Token 上下文太长**（`blocking_limit`）：消息历史撑满了 Claude 的上下文窗口，无法继续。
4. **用户取消**：`abortController.signal.aborted` 为 true（用户按了 Ctrl+C）。
5. **API 错误**：调 Claude API 时出错且无法重试。

</details>
