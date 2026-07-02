# Edit 工具（`src/tools/file_edit.py`）实现思路

> 对应 TS 源码：`claude-code-main/src/tools/FileEditTool/`（`FileEditTool.ts` / `prompt.ts` / `constants.ts`）

## 这个工具解决什么问题

让模型对文件做**精确字符串替换**：把 `old_string` 换成 `new_string`。配合 `Read`（读）构成"读 → 改"闭环。之所以用"字符串替换"而不是"整文件重写"，是因为替换更安全、改动更可控，也更省 token——只描述要变的那一小段，而不是把整个文件吐出来。

## 与 TS 源码的对应关系

TS 原版除了下面这些决策，还做了引号归一化（`findActualString` / 保留引号风格）、LSP/VSCode 通知、`fileHistory` 备份、git diff、UTF-16 编码、Claude settings 文件校验等。我们复刻核心 8 条决策，其余省略。

## 关键设计决策

### 1. 必须"先 Read 后 Edit"
编辑前，`context.read_file_state` 里必须有这个文件的缓存（也就是 AI 之前用 `Read` 读过它）。没读过直接报错。**这正是 `Read` 工具写缓存的用途**，闭环在此接上。

### 2. 防覆盖：staleness（陈旧）检查
如果文件的 mtime 比"上次读取时记录的时间戳"更新，说明读完之后文件被人/linter/你自己改过——这时**拒绝编辑**，要求重新 Read。否则 AI 会基于过时的内容做替换，可能毁掉别人的改动。
- Windows 友好回退：Windows 上 mtime 会因云同步/杀毒无故跳动，所以对"整文件读"额外做一次**内容比对**——mtime 变了但内容其实没变，就放行。

### 3. `old_string` 必须唯一
找不到 → 报错；命中多处但没开 `replace_all` → 报错，要求加上下文使其唯一，或显式 `replace_all=true` 全替换。避免"我以为只改一处，结果全改了"。

### 4. 新建文件的约定
`old_string` 传**空字符串**且文件不存在 = 新建文件，`new_string` 就是文件全部内容。若文件已存在且非空却传空 `old_string`，报错（防止误覆盖）。

### 5. 其它防呆
- `old_string == new_string` → 报错"没有改动"。
- 目标是目录、是 `.ipynb` → 分别给出对应错误（notebook 让用 NotebookEdit）。

### 6. 校验与执行分工 + 原子读-改-写
- `validate_input` 做**绝大部分检查**（绝对路径、唯一性、staleness…），但**不写盘**。
- `call` 进入"原子段"：再读一次文件、**再查一次 staleness**（validate 与 call 之间文件可能又被改），然后替换、写回。这中间不做任何 `await`，避免并发交错。
- 写完**更新 `read_file_state`**（新内容 + 新 mtime），于是可以连续多次 Edit 同一文件而不会触发 staleness 误报。

### 7. 换行风格保留
读文件时探测原本是 LF 还是 CRLF，写回时还原，避免无意中把整个文件的换行符全改了（会造成巨大无意义 diff）。

## 有意没做的部分

- 引号归一化（智能匹配不同引号风格）。
- LSP / VSCode 通知、fileHistory 备份、git diff。
- UTF-16 编码（只处理 UTF-8）。
- Claude settings 文件的特殊校验。
