# PRD: CodeExec — Strategy Selection + Code Execution (003)

## Problem Statement

arcrun currently has one execution strategy (ReAct). Agents that need to solve problems by writing and running code are limited to treating code execution as just another tool — with no system prompt guidance encouraging code-first problem solving. Additionally, when multiple strategies exist, there's no mechanism for the model to autonomously pick the right one.

## User Stories

### US-1: Code Execution Tool
**As** an agent builder, **I want** a builtin ExecuteTool that runs model-generated Python in a sandboxed subprocess, **so that** my agents can solve problems by writing and executing code without me building subprocess management from scratch.

### US-2: Code-First Strategy
**As** an agent builder, **I want** a CodeExec strategy that augments the system prompt to encourage code-writing, **so that** my agent preferentially writes and runs Python instead of only using predefined tools.

### US-3: Autonomous Strategy Selection
**As** an agent builder, **I want** the model to pick the best strategy for the task when multiple strategies are allowed, **so that** different tasks automatically get the most effective execution approach.

### US-4: Strategy Extensibility
**As** a framework author, **I want** a formal Strategy interface (ABC) with name and description, **so that** I can implement custom strategies that plug into arcrun's selection and execution system.

### US-5: Sandbox Control Over Code Execution
**As** a security engineer, **I want** ExecuteTool to go through the same SandboxConfig.check gate as every other tool, **so that** I can enforce code execution policies (or deny it entirely) using the existing sandbox system.

## Requirements

### R-1: ExecuteTool (Builtin)
- Factory function: `make_execute_tool(*, timeout_seconds=30, max_output_bytes=65536, extra_env=None) -> Tool`
- Lives in `src/arcrun/builtins/execute.py`
- Caller must explicitly add to their tools list (not auto-injected)
- Tool name: `execute_python`
- Input schema: `{"code": str}` (required)
- Runs code in subprocess via `asyncio.create_subprocess_exec`
- Writes code to temp file (not `python -c`)
- Working directory: isolated `tempfile.TemporaryDirectory()`
- Environment: hardcoded minimal `{PATH, HOME, LANG}` + optional `extra_env`
- Process isolation: `start_new_session=True` for group cleanup
- Timeout: two-phase (SIGTERM -> 5s grace -> SIGKILL via `os.killpg`)
- Output: JSON string `{stdout, stderr, exit_code, duration_ms}`
- stdout/stderr truncated to `max_output_bytes`
- Respects `ToolContext.cancelled` for cooperative cancellation
- Emits standard tool events (tool.start, tool.end, tool.error) via executor pipeline

### R-2: Strategy ABC
- Abstract base class in `src/arcrun/strategies/__init__.py`
- Abstract properties: `name: str`, `description: str`
- Abstract method: `async __call__(self, model, state, sandbox, max_turns) -> LoopResult`
- ReactStrategy: wraps existing `react_loop` function
- CodeExecStrategy: modifies system prompt, delegates to react_loop
- STRATEGIES dict values change from functions to Strategy instances

### R-3: CodeExec Strategy
- Class: `CodeExecStrategy` in `src/arcrun/strategies/code.py`
- name: `"code"`
- description: Explains code-first problem solving approach
- Default system prompt prefix (hardcoded, ~10 lines of guidance)
- Configurable: `CodeExecStrategy(system_prompt_prefix=...)` overrides default
- Per-call override: strategy constructor accepts optional prefix
- __call__: prepends prefix to state.messages[0] (system message), calls react_loop
- Emits `code.prompt.augmented` event with original/augmented lengths
- Hybrid: allows both code execution AND regular tool calls (same as ReAct)

### R-4: Strategy Selection
- `select_strategy()` updated to handle model-based selection
- No `allowed_strategies` or `None` -> default "react" (no LLM call)
- Single allowed -> use directly (no LLM call)
- Multiple allowed -> one `model.invoke()` call with selection tool
- Selection tool: `select_strategy` with enum of allowed strategy names
- Model sees: strategy names + descriptions + tool names + task
- Fallback: if model returns invalid selection, default to "react"
- Emits `strategy.selection.start` and `strategy.selection.complete` events
- Selection cost tracked separately in LoopResult (not mixed with strategy execution)

### R-5: Public API Additions
- Export `make_execute_tool` from `arcrun.__init__`
- Export `Strategy` ABC from `arcrun.__init__`
- Existing exports unchanged

## Phase Gate

- [ ] `make_execute_tool()` creates a working ExecuteTool
- [ ] ExecuteTool runs Python in subprocess with temp file + temp dir + minimal env
- [ ] ExecuteTool returns structured JSON result `{stdout, stderr, exit_code, duration_ms}`
- [ ] ExecuteTool respects timeout with two-phase shutdown
- [ ] SandboxConfig.check gates ExecuteTool like any other tool
- [ ] Strategy ABC defines name, description, __call__
- [ ] ReactStrategy wraps existing react_loop (zero behavior change)
- [ ] CodeExecStrategy augments system prompt, delegates to react_loop
- [ ] Strategy selection: single -> direct, multiple -> model picks
- [ ] All actions emit events
- [ ] All existing tests still pass (zero regression)

## Non-Goals

- Container/VM isolation (Phase 4)
- Stateful execution across turns (future)
- Package installation from generated code (caller concern)
- Import restriction/AST analysis (caller's SandboxConfig.check)
- Streaming code output (depends on arcllm streaming)
