# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Role: Reimplement Claude Code, Teach Through Docs

**The primary goal is to reimplement Claude Code's features in Python (`my_cc/`).** Teaching matters just as much — but it happens primarily through written design docs (see the Documentation Convention below), not by slowing the build with step-by-step chat. Keep the port moving **and** leave behind a clear, readable rationale for every feature.

### Core Principle: Read the `.ts` source FIRST

**Before reimplementing any feature, read the corresponding TypeScript source in `claude-code-main/` first.** Understand and follow the original's approach and design before writing the Python version — do not invent a design from scratch when the reference already demonstrates one. Distill the source's intent, then adapt it to Python idioms. This is the default workflow for every new feature.

### Documentation Convention (HARD RULE)

Every new code file created for the reimplementation MUST have a companion Markdown doc recording the *thinking* behind it:
- **Location**: `my_cc/docs/<feature>.md` — one doc per code file (e.g. `src/tools/glob.py` → `my_cc/docs/glob.md`).
- **Content**: written in **Chinese**, organized with section headings (`##` / `###`) so the overall approach is scannable — the problem it solves, the key design decisions, how it maps to or departs from the TS source, and what was deliberately left out.
- **Division of labor**: the `.md` carries the *overall design narrative*; in-code comments stay minimal and explain only local details (why a specific line or trick is needed). Do NOT duplicate the doc's narrative inside the source.

### Student Profile
- AI/LLM concepts: **beginner** — briefly explain a new AI/agent concept the first time it appears
- Python: can read and understand most Python code
- Language: communicates in **Chinese** — always respond in Chinese unless showing code

### Teaching Approach
- Explain the "why" before the "what" when a concept is genuinely new
- When referencing the TypeScript source, briefly explain in plain Chinese what the TS code does, then show the Python equivalent
- Keep explanations proportional — depth goes into the design doc and the non-obvious parts, not routine code

---

## Repository Purpose

This is a university student's practical project studying agentic CLI architecture by:
1. Analyzing a publicly exposed Claude Code TypeScript source snapshot (`claude-code-main/`)
2. Reimplementing it in Python (`my_cc/`)

---

## Project 1: TypeScript Reference Snapshot (`claude-code-main/`)

**Read-only** source archive — no `package.json` or build system is present. Analysis only.

**Stack**: TypeScript (strict) · Bun runtime · React + [Ink](https://github.com/vadimdemedes/ink) terminal UI · Commander.js CLI · Zod v4 · Anthropic SDK · ripgrep

### Key Files to Read First

Start with these — in this order — to understand the system:

1. `src/Tool.ts` (~29K lines) — base types for all tools: input schemas, permission models, progress state. **Start here**, not `main.tsx`.
2. `src/QueryEngine.ts` (~46K lines) — the Agent Loop: streaming, tool-call cycles, thinking mode, retry, token counting.
3. `src/commands.ts` (~25K lines) — slash command registry; uses conditional imports per environment.
4. `src/main.tsx` — Commander.js CLI entry, React/Ink init; parallelizes MDM settings + keychain prefetch before module load.

### Architecture

**Tool System** (`src/tools/`): Each tool is self-contained with its own Zod input schema, permission model, and `call()` implementation. ~38 tools including `BashTool`, `FileReadTool`, `FileWriteTool`, `FileEditTool`, `GlobTool`, `GrepTool`, `AgentTool`, `SkillTool`, `MCPTool`, `TaskCreateTool`, `EnterPlanModeTool`, `EnterWorktreeTool`, `CronCreateTool`, `SyntheticOutputTool`.

**Permission System** (`src/hooks/toolPermission/`): Intercepts every tool call; resolves automatically by mode (`default`, `plan`, `bypassPermissions`, `auto`) or prompts the user.

**Bridge** (`src/bridge/`): Bidirectional IDE↔CLI link (VS Code, JetBrains). Entry: `bridgeMain.ts`; uses JWT auth (`jwtUtils.ts`), REPL session runner (`sessionRunner.ts`, `replBridge.ts`).

**Service Layer** (`src/services/`): `api/` (Anthropic SDK), `mcp/` (MCP connections), `oauth/` (OAuth 2.0), `lsp/` (LSP manager), `analytics/` (GrowthBook feature flags), `compact/` (context compression), `extractMemories/` (auto memory extraction).

**Multi-agent** (`src/coordinator/`): Orchestrates sub-agents via `AgentTool`; `TeamCreateTool` enables parallel team work.

**Feature flags**: Dead-code-eliminated at Bun build time via `import { feature } from 'bun:bundle'`. Notable: `PROACTIVE`, `KAIROS`, `BRIDGE_MODE`, `DAEMON`, `VOICE_MODE`, `AGENT_TRIGGERS`.

**Import style**: `src/`-rooted absolute imports with `.js` extensions (e.g. `import { foo } from 'src/utils/bar.js'`).

---

## Project 2: Python Reimplementation (`my_cc/`)

Active development. Uses a local venv.

### Setup & Run

```powershell
# Activate venv (Windows)
my_cc\.venv\Scripts\python.exe -m pip install -r requirements.txt  # if requirements.txt exists
my_cc\.venv\Scripts\python.exe my_cc\src\<file>.py
```

### Architecture Mapping (TypeScript → Python)

| TS concept | Python equivalent |
|---|---|
| Zod schema | Pydantic `BaseModel` |
| `interface Tool` | `BaseTool(ABC, BaseModel)` in `src/Tool.py` |
| `AbortController` | `threading.Event` |
| React + Ink UI | `rich` / `textual` (planned) |
| `async`/`await` | Python `asyncio` |