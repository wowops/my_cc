# Grep 工具实现思路

> 对应代码：`my_cc/src/tools/grep.py`
> 对应 TS 源码：`claude-code-main/src/tools/GrepTool/GrepTool.ts` + `prompt.ts`

## 解决什么问题

Glob 按**文件名**找文件，Grep 按**文件内容**找——用正则在代码库里搜「哪里用到了这个函数 /
这段字符串在哪些文件出现」。这是模型探索陌生代码库最常用的能力之一。

## 与 TS 源码的最大差异：引擎换成纯 Python

真实 Claude Code 的 Grep 是 **ripgrep（`rg` 二进制）的薄封装**：它拼一长串 `rg` 命令行参数，
`subprocess` 调用系统的 ripgrep，再解析其输出。优点是快、功能全；代价是**用户机器必须装 ripgrep**。

本项目是教学复现，选择了**纯 Python 方案**（`os.walk` + `re` 逐行匹配），换取「零外部依赖、
任何机器都能跑」。代价是大仓库下比 ripgrep 慢。**对外的参数 schema 与输出格式仍尽量对齐 TS**，
让模型用起来感觉不到差别——差别只在内部引擎。

## 输入参数（与 TS 对齐）

| 参数 | 含义 | 我们的支持度 |
|---|---|---|
| `pattern` | 正则（Python `re` 语法） | ✅ |
| `path` | 搜索的文件或目录，默认 cwd | ✅ |
| `glob` | 按文件名过滤，如 `*.py`、`*.{ts,tsx}` | ✅（支持逗号/空格分隔、单层 `{}` 展开） |
| `type` | 按语言类型过滤，如 `py`/`js`/`go` | ✅（内置常见几种映射） |
| `output_mode` | `content` / `files_with_matches` / `count` | ✅ 三种全实现 |
| `-A`/`-B`/`-C`/`context` | content 模式的前后上下文行数 | ✅ |
| `-n` | content 模式显示行号（默认 true） | ✅ |
| `-i` | 大小写不敏感 | ✅ |
| `head_limit`/`offset` | 结果分页（默认 250，0=无限） | ✅ |
| `multiline` | 让 `.` 跨行、模式可跨行匹配 | ✅（简化版，见下） |

## 关键设计决策

### 1. 三种输出模式（照搬 TS 的 `mapToolResultToToolResultBlockParam`）

- **files_with_matches**（默认）：只列**有匹配的文件路径**，按修改时间降序。空 → `No files found`，
  否则 `Found N files\n` + 路径列表。
- **content**：列出**匹配的行**，格式 `路径:行号:内容`（带 `-n`）。支持上下文行（见决策 3）。
  空 → `No matches found`。
- **count**：每个文件 `路径:匹配行数`，末尾汇总 `Found X total occurrences across Y files.`。

输出里的固定短语（`No files found` / `Found N files` 等）**刻意保留英文**，与 TS 原版逐字一致——
工具结果是给模型读的，对齐原版能减少模型的理解成本；中文解释都放在本文档里。

### 2. head_limit / offset 分页（照搬 `applyHeadLimit`）

不设上限时默认只回 **250** 条，避免一次宽泛搜索（可能上万行）塞爆上下文。`head_limit=0` 是
「我确实要全部」的逃生口。`offset` 用于翻页。只有**真的发生截断**时才在结果尾部附
`[Showing results with pagination = limit: N, offset: M]`，提示模型「还有更多，可以翻页」。

### 3. 上下文行（-A/-B/-C）与 ripgrep 风格的分隔

content 模式下，围绕每个匹配行输出前 `-B` / 后 `-A` 行（`-C`/`context` 同时设前后，优先级最高）。
模仿 ripgrep 的呈现：**匹配行**用冒号 `路径:行号:内容`，**上下文行**用连字符 `路径-行号-内容`，
不连续的行块之间插一行 `--`。相邻/重叠的上下文会自动合并（用「行号→是否匹配」的字典去重）。

### 4. 二进制文件跳过 & 超长行截断

读文件时先看前 8KB 有没有 `\x00`，有就当二进制跳过（避免乱码污染结果）。单行超过 500 字符
（对应 ripgrep `--max-columns 500`）会被截断 + 标注，防止压缩/minified 文件刷屏。

### 5. 自动排除版本控制目录

`os.walk` 时跳过 `.git .svn .hg .bzr .jj .sl`（与 TS 的 `VCS_DIRECTORIES_TO_EXCLUDE` 一致），
否则版本库元数据会制造大量噪声。

### 6. multiline 简化

`multiline=True` 时改为对**整个文件**用 `re.finditer`（`re.DOTALL`，让 `.` 匹配换行），
能命中跨行模式。简化点：multiline 下不再叠加 `-A/-B/-C` 上下文，只输出匹配跨越的那些行。

## 与 TS / ripgrep 的差异（有意简化）

- **引擎**：纯 Python `re` 逐行，而非 ripgrep——正则方言是 Python `re`，不是 Rust regex
  （大体兼容，但少数高级语法不同）。
- **glob 过滤**：用 `fnmatch` 匹配**文件名**（basename），支持逗号/空格分隔与单层 `{}` 展开；
  不支持 ripgrep 那种带路径层级的复杂 glob。
- **不读 `.gitignore`**：真实版会应用权限上下文里的忽略规则，我们只排除 VCS 目录。
- **count 语义**：与 ripgrep `-c` 一致，按**匹配的行数**计（multiline 下按匹配次数计）。
- **无超时**：真实版给 ripgrep 设了执行超时；我们靠 `context.is_aborted` 不做硬超时。
