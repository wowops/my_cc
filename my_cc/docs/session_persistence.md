# 会话持久化：`session_persistence.py`

> 对应 TS：`sessionStorage.ts`（核心）、`sessionStoragePortable.ts`（lite read / sanitizePath）、`listSessionsImpl.ts`（列表）、`sessionRestore.ts`（恢复）。

## 一、它解决什么问题

`QueryEngine.messages` 是纯内存列表——程序退出就没了。真实 Claude Code 把每轮对话**一行一行地追加**到 `~/.claude/projects/<项目>/<uuid>.jsonl`，关闭重开时用 `--continue` 或 `--resume` 找回上次的聊天。

本模块就是把这条「落盘 → 回放」链路在 Python 里复现出来。

## 二、整体存储布局

```
~/.my_cc/
  projects/
    E--university-SOFTWARE-python-practical-claude-code/
      a1b2c3d4-....jsonl    ← 一次会话 = 一个 UUID 命名的文件
      e5f6g7h8-....jsonl
    another-project/
      ...
```

- 根目录用 `~/.my_cc`（不是 `~/.claude`），避免和真实 CC 的会话混在一起。设 `MY_CC_HOME` 环境变量可覆盖。
- `sanitize_path(cwd)` 把工作目录的绝对路径里所有非字母数字字符转 `-`，超过 200 字符则截断 + 加 hash 后缀防冲突。
- 每个会话由一个 `uuid.uuid4()` 标识，`.jsonl` 文件名就是它。

## 三、JSONL 存什么格式

一行一个 JSON 对象（**Entry**），每条 Entry 包装一条 Anthropic message + 元数据：

```json
{"type":"user","uuid":"...","sessionId":"...","cwd":"...","timestamp":"...","message":{"role":"user","content":[{"type":"text","text":"你好"}]}}
{"type":"assistant","uuid":"...","sessionId":"...","cwd":"...","timestamp":"...","message":{"role":"assistant","content":[{"type":"text","text":"你好！"},{"type":"tool_use","id":"...","name":"Read","input":{}}]}}
```

- `type` 字段取值 `"user"` / `"assistant"`，对应 Anthropic 消息的 role
- `uuid` 每条唯一，防止重复写（追加前先读已有 uuid，跳过重复）
- `message` 字段就是 `QueryEngine.messages` 里的那条 dict（role + content）
- 只有 user / assistant 被保存，system（如 `/clear` 的返回文字、引擎提示）不存
- **不做 parentUuid 链**（MVP 精简决策）：我们目前没有 fork / compact 边界 / 孤儿消息这些概念，顺序读回就够了

## 四、写：save_messages 的设计

```python
def save_messages(session_id, project_dir, cwd, messages, *, _checkpoint_index=0) -> int:
```

- 只写 `messages[_checkpoint_index:]` → 增量追加
- 先读已有 JSONL 收集已有 uuid（去重兜底），跳过重复 entry
- 新 entry 一行一行 `json.dumps` + `\n` 追加（`open(..., "a")`）
- 返回新的 checkpoint（= 当前 messages 总长）
- 这个函数被 `QueryEngine._save_session()` 调用——每轮 `submit_message` 结束时自动触发

和真 CC 的关键差异：
- 真 CC 有 `Project` 类管理批量写入队列（100ms 聚集 flush）——我们直接同步写（小规模无压力）
- 真 CC 有 `recordTranscript` 的 `messageSet` 去重 + `parentUuid` 链管理——我们只靠 uuid 去重

## 五、读：load_session_messages 的设计

```python
async def load_session_messages(project_dir, session_id) -> list[dict]:
```

- 逐行解析 JSONL，只取 `type` 为 `user` 或 `assistant` 的 entry
- 提取其 `message` 字段（dict），确保有 `role` 字段
- 返回列表，直接作为 `QueryEngine(initial_messages=...)` 的初始历史

## 六、列：list_sessions 的 lite read 设计

这是 `--resume` 选单和 `/resume` 命令背后的函数，对应 TS 的 `listSessionsImpl`。

**为什么不用 `load_session_messages` 读全文件？** 如果项目有 50 个历史会话，每个都读一遍完整 JSONL 太慢（尤其文件大了之后）。真实 CC 的解法是 **lite read**：只读每个文件**头 + 尾各 64KB**，从中提取首句 + 标题即可做列表。

```python
async def list_sessions(project_dir, *, limit=20) -> list[SessionInfo]:
```

工作步骤：
1. `project_dir.iterdir()` 扫出所有 UUID 命名的 `.jsonl`（正则校验 UUID 格式）
2. 按 mtime 排序（最新的在前）
3. 对前 `limit` 个候选，各读头尾 64KB
4. 从 head 提取第一个有意义的 user message 文本（`_extract_first_prompt`，跳过斜线命令 / 空消息）
5. 从 tail 提取 `customTitle` / `lastPrompt` / 最后一个 user message → 组成 `summary`
6. summary 降级链：手动标题 → lastPrompt → 第一句 → "(空会话)"
7. 返回 `SessionInfo` 列表（session_id / summary / last_modified / file_size / cwd）

**性能**：50 个会话 ≈ 50 次 stat + 50 次读头尾（每次读 128KB max）≈ 6.4MB 总读量，O(会话数)，不随会话文件大小增长。

## 七、`--resume` / `--continue` / `/resume` 三者协作

| 入口 | 在哪 | 做什么 |
|---|---|---|
| `--continue` / `-c` | CLI 启动参数 | `find_most_recent_session` → 自动接最近一次 → 传 `initial_messages` 给 QueryEngine |
| `--resume` | CLI 启动参数 | 无参数 = `list_sessions` → 交互选单；带 UUID = 直接恢复 |
| `/resume` | REPL 里运行 | 列出本项目其他会话 → 用户选 → 存当前 → 加载目标 → 替换 `context.messages` + `context.session_id` |

三层都走 `load_session_messages` 加载，都复用 `session_persistence.py` 的同一个 `list_sessions`。

## 八、`/resume` 命令的切换协议

1. **存旧**：`save_messages` 全量保存当前 messages（以防有未落盘的新消息）
2. **加载**：`load_session_messages` 读目标 JSONL → 清空 `context.messages` → `extend` 填入
3. **切 ID**：`context.session_id = target_sid`（后续对话自动追加到目标文件）
4. **清缓存**：`context.read_file_state.clear()`（和 `/clear` 同理：旧文件缓存在新会话里可能误导 Edit）
5. **擦屏 + banner**：复用 `clear.py` 的 `_clear_terminal_screen()` + `context.render_banner()`，让视觉也跟着切换

## 九、和真 CC 的差异（有意不做的）

- **parentUuid 链**：不建。MVP 按 JSONL 顺序读回消息即可，不做分子/去孤儿/compact 边界截断。
- **批量写入队列**：不建。`Project` 类的 100ms batching + drain 太复杂，直接 `open("a")` 追加足够。
- **remote ingress / CCR v2**：不碰。远程持久化是真 CC 的 cloud 功能。
- **标题系统**：不做 `/rename` / `customTitle`。JSONL 里可以存这些 entry，但我们暂时不写、也不消费。
- **子 agent sidechain**：不做。没有 agent 子系统。
- **worktree 状态恢复**：不做。没有 worktree 功能。
- **`--fork-session`**：不做。暂时没有分叉需求。

这些都在 `improvements.md` 的对应行里标注了。

## 十、`ToolUseContext` 新增字段

为支持 `/resume` 命令访问会话信息，在 `ToolUseContext` 加了两个字段：

```python
session_id: str = ""
project_dir: str = ""
```

和 `render_banner` 一样，由 `main.py` 的 `build_engine` 注入。这两个字段只在持久化开启时才有值（裸启动没有）。
