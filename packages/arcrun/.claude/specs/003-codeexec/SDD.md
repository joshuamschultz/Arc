# SDD: CodeExec — Strategy Selection + Code Execution (003)

## Architecture Overview

Phase 2 adds three components to the existing architecture. No existing modules change behavior — only strategies/__init__.py gets refactored (functions -> ABC classes), and __init__.py gets new exports.

```
Caller
  │
  ├── run(model, tools, prompt, task, allowed_strategies=["react", "code"])
  │
  ▼
┌─ arcrun ─────────────────────────────────────────────────────┐
│                                                               │
│  loop.py ── orchestration (unchanged) ─────────────────────  │
│    │                                                          │
│    ├── select_strategy() ── model picks if multiple ────────  │
│    │     Uses tool calling with enum for guaranteed valid      │
│    │     selection. Falls back to "react" on failure.          │
│    │                                                          │
│    ├── ReactStrategy.__call__() ── wraps react_loop ────────  │
│    │     Identical behavior to current react_loop function     │
│    │                                                          │
│    └── CodeExecStrategy.__call__() ── code-first ───────────  │
│          Prepends CodeExec system prompt to messages           │
│          Delegates to react_loop (zero duplication)            │
│          Model writes code → calls execute_python tool         │
│                                                               │
│  builtins/execute.py ── ExecuteTool ────────────────────────  │
│    │  make_execute_tool() factory                             │
│    │  Runs model-generated Python in subprocess               │
│    │  Temp file + temp dir + minimal env + process group      │
│    │  Returns {stdout, stderr, exit_code, duration_ms}        │
│    │                                                          │
│    └── Goes through executor.py pipeline (sandbox, schema,    │
│        events) like every other tool                          │
│                                                               │
│  (all existing modules unchanged) ─────────────────────────  │
└───────────────────────────────────────────────────────────────┘
```

## Module Design

### strategies/__init__.py (~55 lines, refactored from ~38)

Strategy ABC and selection logic.

```python
from abc import ABC, abstractmethod
from typing import Any
from arcrun.types import LoopResult

class Strategy(ABC):
    """Base class for execution strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g., 'react', 'code')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """What this strategy does. Shown to model during selection."""
        ...

    @abstractmethod
    async def __call__(
        self, model: Any, state: RunState, sandbox: Sandbox, max_turns: int
    ) -> LoopResult:
        ...


STRATEGIES: dict[str, Strategy] = {}


def _load_strategies() -> None:
    from arcrun.strategies.react import ReactStrategy
    from arcrun.strategies.code import CodeExecStrategy

    react = ReactStrategy()
    code = CodeExecStrategy()
    STRATEGIES[react.name] = react
    STRATEGIES[code.name] = code


async def select_strategy(allowed, model, state) -> str:
    """Pick strategy. Single=direct. Multiple=model picks. None=react."""
    if not STRATEGIES:
        _load_strategies()

    if allowed is None:
        return "react"
    unknown = [s for s in allowed if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}")
    if len(allowed) == 1:
        return allowed[0]

    # Model-based selection via tool calling
    bus = state.event_bus
    bus.emit("strategy.selection.start", {
        "allowed_strategies": allowed,
        "task": state.messages[-1].content if state.messages else "",
    })

    # Build selection tool with enum
    from arcllm.types import Tool as LLMTool
    select_tool = LLMTool(
        name="select_strategy",
        description="Select the best execution strategy for this task",
        input_schema={
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": allowed,
                },
                "reasoning": {"type": "string"},
            },
            "required": ["strategy"],
        },
    )

    # Build selection prompt with strategy descriptions
    strategy_descriptions = "\n".join(
        f"- {name}: {STRATEGIES[name].description}"
        for name in allowed
    )
    tool_names = state.registry.names()

    selection_messages = [
        system_message(
            f"Select the best execution strategy for the task below.\n\n"
            f"Available strategies:\n{strategy_descriptions}\n\n"
            f"Available tools: {', '.join(tool_names)}\n\n"
            f"Call select_strategy with your choice."
        ),
        user_message(state.messages[-1].content if state.messages else ""),
    ]

    try:
        response = await model.invoke(selection_messages, tools=[select_tool])
        if response.tool_calls:
            chosen = response.tool_calls[0].arguments.get("strategy")
            reasoning = response.tool_calls[0].arguments.get("reasoning", "")
            if chosen in allowed:
                bus.emit("strategy.selection.complete", {
                    "selected": chosen,
                    "reasoning": reasoning,
                })
                return chosen
    except Exception:
        pass

    # Fallback
    bus.emit("strategy.selection.fallback", {
        "attempted": allowed,
        "defaulted_to": "react",
    })
    return "react"
```

### strategies/react.py (~145 lines, minor refactor)

Existing react_loop wrapped in ReactStrategy class.

```python
class ReactStrategy(Strategy):
    @property
    def name(self) -> str:
        return "react"

    @property
    def description(self) -> str:
        return (
            "Iterative tool-calling loop. Reasons about the task, calls tools, "
            "observes results, and repeats until complete. Best for multi-step "
            "problems requiring tool interaction."
        )

    async def __call__(self, model, state, sandbox, max_turns) -> LoopResult:
        return await react_loop(model, state, sandbox, max_turns)
```

The existing `react_loop` function stays as-is. `_build_result` stays as-is. ReactStrategy is a thin class wrapper (~10 lines added).

### strategies/code.py (~30 lines, new)

CodeExecStrategy modifies system prompt and delegates.

```python
_DEFAULT_PREFIX = """You have access to a Python execution tool (execute_python). \
Write executable Python code to solve tasks.

GUIDELINES:
- Write focused scripts (20-50 lines) solving one sub-problem at a time
- You will receive {stdout, stderr, exit_code, duration_ms} after each execution
- Each execution is stateless - variables do NOT persist between calls
- If code fails, examine the error and fix your approach
- After 3 failures on the same approach, try a fundamentally different method
- Use code for: computation, data processing, logic, file operations
- Use other tools for: external APIs, user confirmation, security-sensitive ops
"""


class CodeExecStrategy(Strategy):
    def __init__(self, system_prompt_prefix: str | None = None):
        self._prefix = system_prompt_prefix or _DEFAULT_PREFIX

    @property
    def name(self) -> str:
        return "code"

    @property
    def description(self) -> str:
        return (
            "Write and execute Python code to solve tasks. Best for computation, "
            "data processing, and problems where code is more effective than "
            "predefined tool calls."
        )

    async def __call__(self, model, state, sandbox, max_turns) -> LoopResult:
        # Augment system prompt
        original = state.messages[0].content
        state.messages[0] = system_message(self._prefix + "\n" + original)

        state.event_bus.emit("code.prompt.augmented", {
            "original_length": len(original),
            "augmented_length": len(state.messages[0].content),
        })

        # Delegate to react loop
        return await react_loop(model, state, sandbox, max_turns)
```

### builtins/__init__.py (~5 lines, new)

```python
from arcrun.builtins.execute import make_execute_tool

__all__ = ["make_execute_tool"]
```

### builtins/execute.py (~60 lines, new)

ExecuteTool factory.

```python
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from typing import Any

from arcrun.types import Tool, ToolContext

_DEFAULT_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "en_US.UTF-8",
}

_GRACE_PERIOD = 5.0


def make_execute_tool(
    *,
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    extra_env: dict[str, str] | None = None,
) -> Tool:
    """Create a sandboxed Python execution tool."""

    env = {**_DEFAULT_ENV, **(extra_env or {})}

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        code = params["code"]
        start = time.time()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_path = os.path.join(tmpdir, "script.py")
            with open(code_path, "w") as f:
                f.write(code)

            proc = await asyncio.create_subprocess_exec(
                sys.executable, code_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=env,
                start_new_session=True,
            )

            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    await asyncio.sleep(_GRACE_PERIOD)
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout = b""
                stderr = b"Error: execution timed out"

            duration_ms = (time.time() - start) * 1000

            return json.dumps({
                "stdout": stdout[:max_output_bytes].decode(errors="replace"),
                "stderr": stderr[:max_output_bytes].decode(errors="replace"),
                "exit_code": proc.returncode if not timed_out else -1,
                "duration_ms": round(duration_ms, 1),
            })

    return Tool(
        name="execute_python",
        description="Execute Python code in a sandboxed subprocess. Returns stdout, stderr, exit_code, and duration.",
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
            },
            "required": ["code"],
        },
        execute=_execute,
        timeout_seconds=None,  # Handled internally with two-phase shutdown
    )
```

### __init__.py (~22 lines, updated)

Add new exports:

```python
from arcrun.builtins import make_execute_tool
from arcrun.strategies import Strategy

__all__ = [
    # existing...
    "make_execute_tool",
    "Strategy",
]
```

## Data Flow: Strategy Selection

```
run(allowed_strategies=["react", "code"])
  │
  ├─ allowed is None? → "react" (no LLM call)
  ├─ len(allowed) == 1? → allowed[0] (no LLM call)
  └─ len(allowed) > 1?
       │
       ├─ Build selection prompt with strategy descriptions + tool names + task
       ├─ model.invoke(selection_messages, tools=[select_strategy_tool])
       ├─ Extract strategy name from tool_call arguments
       ├─ Valid? → use it
       └─ Invalid? → fallback to "react"
```

## Data Flow: Code Execution

```
CodeExecStrategy.__call__()
  │
  ├─ Prepend code-execution prefix to system message
  ├─ Emit code.prompt.augmented event
  └─ react_loop(model, state, sandbox, max_turns)
       │
       ├─ Model writes code, returns tool_call: execute_python(code="...")
       ├─ executor.py pipeline: sandbox check → schema validate → execute
       ├─ make_execute_tool._execute():
       │    ├─ Write code to temp file
       │    ├─ asyncio.create_subprocess_exec in temp dir with minimal env
       │    ├─ Wait with timeout → two-phase shutdown on timeout
       │    └─ Return JSON {stdout, stderr, exit_code, duration_ms}
       └─ Result flows back to model as tool result
```

## Event Additions

| Event | Emitted By | Data |
|-------|-----------|------|
| `strategy.selection.start` | strategies/__init__.py | allowed_strategies, task |
| `strategy.selection.complete` | strategies/__init__.py | selected, reasoning |
| `strategy.selection.fallback` | strategies/__init__.py | attempted, defaulted_to |
| `code.prompt.augmented` | strategies/code.py | original_length, augmented_length |

Existing events (tool.start, tool.end, tool.denied, tool.error, llm.call, etc.) apply unchanged.

## Security Model

- ExecuteTool goes through the standard executor pipeline (sandbox check, schema validation, events)
- Without SandboxConfig, ExecuteTool runs arbitrary Python — same as any other tool
- SandboxConfig.check receives `("execute_python", {"code": "..."})` — caller can inspect code
- Subprocess runs with minimal env, isolated temp dir, process group isolation
- Two-phase timeout prevents runaway processes
- **Phase 4** adds container isolation for defense-in-depth

## Line Estimate

| File | Lines | Change |
|------|-------|--------|
| strategies/__init__.py | ~55 | Refactored (+17) |
| strategies/react.py | ~145 | ReactStrategy wrapper (+10) |
| strategies/code.py | ~30 | New |
| builtins/__init__.py | ~5 | New |
| builtins/execute.py | ~60 | New |
| __init__.py | ~22 | Updated (+4) |
| **Total delta** | | **~+121** |
| **Project total** | | **~743** |
