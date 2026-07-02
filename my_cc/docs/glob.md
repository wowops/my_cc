# Glob 工具实现思路

> 对应代码：`my_cc/src/tools/glob.py`
> 对应 TS 源码：`claude-code-main/src/tools/GlobTool/GlobTool.ts` + `prompt.ts`

## 解决什么问题

模型需要「按文件名规律找文件」的能力——比如 `**/*.py` 找出所有 Python 文件、`src/**/*.ts`
找某目录下的 TS 文件。在有 Glob 之前，我们靠 `QueryEngine.build_dir_snapshot()` 往系统提示词里
塞一层目录树当**拐杖**，让模型至少知道根目录有什么。Glob 落地后，模型可以自己主动探索任意层级，
这个拐杖就能撤掉（见 `improvements.md`）。

Glob 只管「按名字找」，不看文件内容；「按内容找」是 Grep 的事。两者配合，构成模型探索代码库的
两大基本搜索能力。

## 与 TS 源码的对应

TS 的 `GlobTool` 是只读、并发安全的工具，核心就一句：调用内部 `glob()` 工具函数拿到匹配文件，
按修改时间排序，截断到 100 条，再把绝对路径**相对化**（省 token）后返回。我们逐条照搬了这套行为，
只是把底层换成 Python 标准库的 `glob` 模块。

| TS 做法 | 我们的做法 |
|---|---|
| 内部 `glob()`（基于第三方 glob 库） | Python 标准库 `glob.glob(pattern, root_dir=base, recursive=True)` |
| `isReadOnly/isConcurrencySafe = true` | `is_read_only() → True`（基类据此让 `is_concurrency_safe` 也为 True） |
| 结果按 mtime 排序 | `os.path.getmtime` 降序排序（最近修改的在前） |
| `limit = 100`，超出标 `truncated` | `DEFAULT_LIMIT = 100`，同样的截断 + 提示 |
| `toRelativePath` 相对化路径 | `_to_relative()`：在 cwd 下转相对路径，否则保留绝对 |
| 空结果返回 `"No files found"` | 同 |

## 关键设计决策

### 1. 为什么用 `root_dir` 而不是把 base 拼进 pattern

`glob.glob(pattern, root_dir=base, recursive=True)`（Python 3.10+）让 base 目录与 pattern 分离：
pattern 保持纯净（不必担心 base 路径里有 `[` `*` 等会被 glob 当通配符的字符），返回的也是
**相对 base** 的干净路径，再拼回绝对路径即可。`recursive=True` 是 `**` 能跨层匹配的开关，必须开。

### 2. 只返回文件，不返回目录

glob 的匹配结果可能包含目录（如 `src/*` 会匹配到子目录）。Glob 工具语义是「找文件」，
所以用 `os.path.isfile` 过滤掉目录，让 `numFiles` 名副其实。

### 3. 按修改时间降序排序

和 TS 一致：最近改过的文件往往最相关，排在前面。mtime 相同时用文件名作为兜底排序键，
保证结果**确定可复现**（否则同一目录两次调用顺序可能不同，demo 断言会飘）。
排序要对每个匹配 `stat` 一次；文件可能在枚举后、stat 前被删，用 try 兜底（按 mtime 0 处理）。

### 4. 截断到 100 条

大仓库下 `**/*` 可能匹配上万文件，全塞回去既浪费 token 又淹没重点。截断到 100 并附一句
「结果已截断，请用更具体的路径或模式」，提示模型缩小范围。

### 5. 路径相对化省 token

匹配到的绝对路径往往很长且前缀重复（都在 cwd 下）。`_to_relative()` 把 cwd 下的路径转成相对路径
（`os.path.relpath`），cwd 之外的（相对结果以 `..` 开头）则保留绝对路径——避免给出反而更难读的
`..\..\..\x` 这种路径。

## 与 TS / 真实行为的差异（有意简化）

- **不匹配隐藏文件**：Python `glob` 默认不匹配以 `.` 开头的条目（除非 pattern 里显式写 `.`），
  与 TS glob 库默认 `dot:false` 行为一致，保持默认即可，未额外暴露开关。
- **不读 `.gitignore`**：真实 Glob 会结合权限上下文里的忽略规则。我们没有那套权限/忽略体系，
  只在排序/枚举层面不做额外过滤。
- **无 `offset` 分页**：TS 的内部 `glob()` 支持 `offset`，我们只实现了 `limit` + 截断标志，
  足够当前用途。
- **`validateInput` 里做了一次 `stat`**：这点**忠实照搬 TS**——给了 `path` 就先确认它存在且是目录，
  尽早把「目录不存在」反馈给模型。（注意这与 Read 工具「validate 不碰盘」的取舍不同，是因为两边
  TS 源码本身的取舍就不同。）
