# Bash 工具（`src/tools/bash.py`）实现思路

> 对应 TS 源码：`claude-code-main/src/tools/BashTool/`
> （`BashTool.tsx` 主体 / `prompt.ts` / `bashSecurity.ts` 2593 行安全检查 / `bashPermissions.ts`）

## 这个工具解决什么问题

让模型能跑任意 shell 命令——跑测试、看目录、git 操作等。它是**最危险**的工具，因此也是**权限系统第一次真正派上用场**的地方（`Read`/`Edit` 之前基本都自动放行，从没触发过弹窗逻辑）。

## 核心洞察：2593 行安全检查 → 浓缩成三档

TS 的 `bashSecurity.ts` 是一部史诗级防御代码，但结构其实很简单：一堆 validator，每个返回 `allow / ask / passthrough`。这正好对应我们 `Tool.py` 的 `PermissionBehavior`（ALLOW / ASK / DENY）。抓住本质后，浓缩成三档：

1. **只读命令**（`ls`/`cat`/`grep`/`git status`…）→ 自动放行；
2. **写命令** → 弹窗问用户 `(y/n)`；
3. **命中高危特征**（`rm -rf` / `sudo` / `curl|sh`…）→ 弹窗时额外标红警告。

## 关键设计决策

### 1. shell 选择（`_resolve_shell`）
真 CC 在类 Unix 上固定用 bash。我们在 Windows 上**优先找 Git Bash**（这样模型生成的 `ls/cat/grep` 能正常跑），找不到退回 PowerShell，再不行用 `/bin/sh`。
- 刻意**跳过 System32 下的 `bash.exe`**——那是 WSL 入口，路径和行为都不一样，容易踩坑。
- 选择在模块加载时固定下来（`_SHELL_LABEL` / `_SHELL_ARGV`），并写进给模型的 prompt，让模型知道自己在跟哪个 shell 说话。

### 2. "只读"判断（`_is_read_only_command`）—— 一处判断，两处复用
同一个判断既决定**权限**（只读→自动放行），又决定**并发**（`is_concurrency_safe`：只读可并行、写串行，复用 `QueryEngine` 已有的调度）。
- 实现方式：按 shell 操作符（`| || && ; & 换行`）切分命令，每个片段取"基础命令名"（去掉前导 `VAR=val` 和 `sudo`），逐一查只读白名单。
- 含写重定向（`>` / `>>`）直接判为写。
- git 特殊处理：只有纯查询子命令（status/log/diff…）算只读，避开 branch/config 这类可能带破坏性 flag 的。
- **诚实的局限**：真 CC 用 tree-sitter 精确解析 AST，我们用"切分 + 查白名单"的朴素近似。像 `$(...)` 命令替换、花式引号注入挡不住，所以**别拿它当生产级沙箱**——它只是教学够用。

### 3. 高危特征识别（`_dangerous_reasons`）
用一组正则挑出最典型的危险模式（rm -rf、提权、dd、mkfs、fork 炸弹、关机、curl|sh、批量改权限、破坏性 git、命令替换），返回中文原因列表，在弹窗时给用户标红。

### 4. 权限决策（`check_permissions`）
本工具**自己重写**了 `check_permissions`（不走基类默认），决策顺序：bypass → plan（写命令拒绝）→ 只读放行 → auto 模式非高危放行 → 其余写/危险命令问用户。非交互式会话无法弹窗时返回 `ASK` 交上层处理。

### 5. 执行：asyncio 子进程 + 超时 + Esc 中断（`call`）
用 `asyncio.create_subprocess_exec` 而**不是阻塞的 `subprocess.run`**。这是"异步到底"原则的体现：阻塞调用会卡死事件循环，导致 spinner 不转、Esc 监听协程跑不了。
- 用 `0.1s` 轮询 `asyncio.wait`，每轮检查：子进程是否结束、是否被 Esc 中断（`context.is_aborted` → kill）、是否超时（kill）。
- 结果由 `_format_result` 拼装：stdout + `[stderr]` 段 + 备注（超时/中断/退出码），超 `MAX_OUTPUT_CHARS=30000` 截断。

## 有意没做的部分

- 沙箱（SandboxManager）、后台任务（`run_in_background`）、图片输出。
- tree-sitter / shell-quote 的 AST 精确解析。
- 2593 行里那些防 unicode 空格 / zsh 模块 / ANSI-C 引号等极端注入花招。
- 大输出落盘、git 操作追踪、增量进度回调。
