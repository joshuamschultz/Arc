# SDD: Core Loop + ReAct (001)

## Architecture Overview

arcrun is the execution engine layer between the caller's agent code and arcllm's model abstraction. Phase 1 implements the ReAct strategy with full event emission, sandbox enforcement, dynamic tool registry, and steering.

```
Caller
  │
  ├── run(model, tools, prompt, task, **options) ─► LoopResult
  │
  └── run_async(model, tools, prompt, task, **options) ─► RunHandle
        │                                                    │
        ▼                                                    │
  ┌─ arcrun ──────────────────────────────────────────────┐  │
  │                                                        │  │
  │  loop.py ── orchestration ──────────────────────────  │  │
  │    │  Creates RunState, Sandbox, EventBus, Registry   │  │
  │    │  Picks strategy                                  │  │
  │    │  Returns LoopResult                              │  │
  │    ▼                                                   │  │
  │  strategies/react.py ── the actual loop ────────────  │  │
  │    │  model.invoke() → tool dispatch → message mgmt  │  │
  │    │  Enforces max_turns                              │  │
  │    │  Checks steer/followUp queues                    │  │
  │    │  Emits all events                                │  │
  │    │                                                   │  │
  │    ├── sandbox.py ── permission check before each tool │  │
  │    ├── registry.py ── dynamic tool collection          │  │
  │    ├── events.py ── emit + collect                     │  │
  │    └── state.py ── mutable run state                   │  │
  │                                                        │  │
  │  types.py ── Tool, ToolContext, SandboxConfig,        │  │
  │              LoopResult, Event, RunHandle               │  │
  └────────────────────────────────────────────────────────┘  │
        │                                                      │
        ▼                                                      │
  arcllm (model.invoke(messages, tools=tools))                 │
        │                                                      │
        ▼                                                      │
  RunHandle.steer() / .follow_up() / .cancel() ◄──────────────┘
```

## Module Design

### types.py (~70 lines)

Public contracts. No business logic.

```python
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any
import asyncio

@dataclass
class Tool:
    """A tool the model can call. Use factory functions for complex tools."""
    name: str
    description: str
    input_schema: dict
    execute: Callable[[dict, "ToolContext"], Awaitable[str]]

@dataclass
class ToolContext:
    """Passed to Tool.execute. Provides environment awareness and cancel signal."""
    run_id: str
    tool_call_id: str
    turn_number: int
    event_bus: "EventBus"
    cancelled: asyncio.Event

@dataclass
class SandboxConfig:
    """Permission boundary. allowed_tools=None means no sandbox (all allowed)."""
    allowed_tools: list[str] | None = None
    check: Callable[[str, dict], Awaitable[tuple[bool, str]]] | None = None

@dataclass
class LoopResult:
    """Returned by run(). Complete execution summary."""
    content: str | None
    turns: int
    tool_calls_made: int
    tokens_used: dict
    strategy_used: str
    cost_usd: float
    events: list["Event"] = field(default_factory=list)
```

### events.py (~60 lines)

Generic event system. Synchronous inline emission.

```python
@dataclass
class Event:
    """Every action emits one. Generic with dict data."""
    type: str           # "loop.start", "tool.end", etc.
    timestamp: float
    run_id: str
    data: dict

class EventBus:
    """Emits events, collects them, optionally calls handler."""

    def __init__(self, run_id: str, on_event: Callable | None = None): ...

    def emit(self, event_type: str, data: dict | None = None) -> Event:
        """Create event, append to log, call handler if set."""

    @property
    def events(self) -> list[Event]: ...
```

**Event types and their data keys:**

| Event | Data Keys |
|-------|-----------|
| `loop.start` | task, tool_names, strategy |
| `loop.complete` | content, turns, tool_calls, tokens, cost |
| `loop.max_turns` | turns_used, max_turns |
| `strategy.selected` | strategy, allowed |
| `turn.start` | turn_number |
| `turn.end` | turn_number |
| `llm.call` | model, stop_reason, tokens, latency_ms, cost_usd |
| `tool.start` | name, arguments |
| `tool.end` | name, result_length, duration_ms |
| `tool.denied` | name, arguments, reason |
| `tool.error` | name, error, traceback |
| `tool.registered` | name |
| `tool.removed` | name |

### sandbox.py (~80 lines)

Permission boundary. Allowlist model. Deny-by-default when configured.

```python
class Sandbox:
    """Checks permissions before every tool execution."""

    def __init__(self, config: SandboxConfig | None, event_bus: EventBus): ...

    async def check(self, tool_name: str, params: dict) -> tuple[bool, str]:
        """
        Returns (allowed, reason).

        Check order:
        1. No config → (True, "")
        2. allowed_tools set and tool not in list → (False, "not in allowed tools")
        3. check callback provided → delegate to callback
        4. All checks pass → (True, "")

        Emits tool.denied for denials.
        check() callback exceptions → treated as denial (fail-safe).
        """
```

### registry.py (~50 lines)

Dynamic mutable tool collection.

```python
class ToolRegistry:
    """Mutable tool collection. Strategies read this each turn."""

    def __init__(self, tools: list[Tool], event_bus: EventBus): ...

    def add(self, tool: Tool) -> None:
        """Add or replace tool. Emits tool.registered."""

    def remove(self, name: str) -> None:
        """Remove tool by name. No-op if not found. Emits tool.removed."""

    def get(self, name: str) -> Tool | None: ...

    def list_schemas(self) -> list:
        """Convert tools to arcllm schema format for model.invoke()."""

    def names(self) -> list[str]: ...
```

### state.py (~60 lines)

Internal mutable state for a single run() execution. Dies when run() returns.

```python
@dataclass
class RunState:
    """Internal state during execution. Not part of public API."""
    messages: list                     # Conversation history (arcllm Message objects)
    registry: ToolRegistry
    event_bus: EventBus
    turn_count: int = 0
    tokens_used: dict = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})
    cost_usd: float = 0.0
    tool_calls_made: int = 0
    run_id: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    steer_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    followup_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    transform_context: Callable | None = None
```

### loop.py (~120 lines)

Entry points + RunHandle. Pure orchestration.

```python
async def run(
    model,
    tools: list[Tool],
    system_prompt: str,
    task: str,
    *,
    max_turns: int = 25,
    allowed_strategies: list[str] | None = None,
    sandbox: SandboxConfig | None = None,
    on_event: Callable | None = None,
    transform_context: Callable | None = None,
    max_cost_usd: float | None = None,
) -> LoopResult:
    """
    Blocking entry point. Runs until task complete or max_turns.

    Raises:
        ValueError: empty tools, missing prompt/task
        Exception: model API errors bubble through
    """

async def run_async(
    model,
    tools: list[Tool],
    system_prompt: str,
    task: str,
    **options,
) -> RunHandle:
    """
    Non-blocking entry point. Returns handle for steering.
    Strategy runs in background asyncio.Task.
    """

class RunHandle:
    """Control interface for a running execution loop."""

    async def steer(self, message: str) -> None:
        """Interrupt: inject after current tool, skip remaining."""

    async def follow_up(self, message: str) -> None:
        """Queue: inject at end_turn before returning."""

    async def cancel(self) -> None:
        """Hard stop. Sets cancel signal. Returns partial result."""

    async def result(self) -> LoopResult:
        """Await completion. Returns final result."""

    @property
    def state(self) -> RunState:
        """Read-only access to current state."""
```

**Orchestration flow:**
1. Validate inputs (tools not empty, prompt/task are strings)
2. Create EventBus with on_event handler
3. Create ToolRegistry from tools list
4. Create Sandbox from config
5. Create RunState
6. Build initial messages: system message + user task message
7. Emit loop.start
8. Select strategy (Phase 1: always "react")
9. Call strategy function, get LoopResult
10. Return LoopResult

### strategies/__init__.py (~30 lines)

Strategy interface and selection.

```python
STRATEGIES = {
    "react": react_loop,
    # Phase 2: "code": code_loop,
    # Phase 3: "recursive": recursive_loop,
}

async def select_strategy(
    allowed: list[str] | None,
    model,
    state: RunState,
) -> str:
    """
    Single allowed → use it.
    Multiple → model picks on first turn (emits strategy.selected).
    None → default to "react".
    """
```

### strategies/react.py (~120 lines)

The actual execution loop.

```python
async def react_loop(
    model,
    state: RunState,
    sandbox: Sandbox,
    max_turns: int,
) -> LoopResult:
    """
    ReAct: Reason → Act → Observe → Repeat.

    Loop:
        1. Check steer queue → if message, inject as user message
        2. transform_context(messages) if hook provided
        3. tools = state.registry.list_schemas()
        4. response = await model.invoke(messages, tools=tools)
        5. Emit llm.call event
        6. Accumulate tokens/cost
        7. If end_turn:
           a. Check followUp queue → if message, inject, continue
           b. Else return LoopResult
        8. For each tool_call:
           a. Emit tool.start
           b. sandbox.check() → denied? emit tool.denied, return error to model
           c. Validate params via jsonschema → invalid? return error to model
           d. Create ToolContext with cancel signal
           e. result = await tool.execute(params, ctx)
           f. Emit tool.end
           g. Check steer queue → if message, skip remaining tools
        9. Append tool results to messages
        10. Emit turn.end, increment turn_count
        11. If turn_count >= max_turns → emit loop.max_turns, return LoopResult
        12. Continue loop

    Message format (arcllm):
        Assistant: [TextBlock(text=...), ToolUseBlock(id=..., name=..., arguments=...)]
        Tool results: Message(role="tool", content=[ToolResultBlock(tool_use_id=..., content=...)])
    """
```

### __init__.py (~20 lines)

Public API surface.

```python
from arcrun.types import Tool, ToolContext, SandboxConfig, LoopResult
from arcrun.events import Event, EventBus
from arcrun.registry import ToolRegistry
from arcrun.loop import run, run_async, RunHandle

__all__ = [
    "run", "run_async", "RunHandle",
    "Tool", "ToolContext", "ToolRegistry",
    "LoopResult", "SandboxConfig",
    "Event", "EventBus",
]
```

## Data Flow

```
run(model, tools, prompt, task)
  │
  ▼
Create: EventBus, ToolRegistry, Sandbox, RunState
  │
  ▼
messages = [system_message(prompt), user_message(task)]
  │
  ▼
EventBus.emit("loop.start")
  │
  ▼
react_loop(model, state, sandbox, max_turns)
  │
  ┌─ LOOP ──────────────────────────────────────────────────┐
  │                                                          │
  │  [check steer_queue → inject if present]                │
  │  [transform_context(messages) if hook]                  │
  │  tools = registry.list_schemas()                        │
  │                                                          │
  │  response = model.invoke(messages, tools)               │
  │  EMIT: llm.call                                         │
  │                                                          │
  │  if end_turn:                                           │
  │    [check followup_queue → inject + continue if present]│
  │    EMIT: loop.complete                                  │
  │    return LoopResult                                     │
  │                                                          │
  │  assistant_msg = TextBlock(s) + ToolUseBlock(s)         │
  │  messages.append(assistant_msg)                          │
  │                                                          │
  │  for tc in tool_calls:                                  │
  │    EMIT: tool.start                                     │
  │    (ok, reason) = sandbox.check(tc.name, tc.arguments)  │
  │    if not ok:                                            │
  │      EMIT: tool.denied                                  │
  │      → error result to model                            │
  │      continue                                            │
  │    validate(tc.arguments, tool.input_schema)            │
  │    ctx = ToolContext(cancel=state.cancel_event, ...)     │
  │    try:                                                  │
  │      result = await tool.execute(tc.arguments, ctx)     │
  │    except Exception as e:                                │
  │      EMIT: tool.error                                   │
  │      → error string to model                            │
  │    EMIT: tool.end                                       │
  │    [check steer_queue → skip remaining if present]      │
  │                                                          │
  │  messages.append(tool_results)                          │
  │  EMIT: turn.end                                         │
  │  turn_count += 1                                        │
  │  if turn_count >= max_turns:                            │
  │    EMIT: loop.max_turns                                 │
  │    return LoopResult                                     │
  └──────────────────────────────────────────────────────────┘
```

## Steering Flow

```
Caller holds RunHandle from run_async()
  │
  ├── handle.steer("change course")
  │     └── message put on steer_queue
  │         └── Strategy checks between tool executions:
  │             └── Message found → inject as user message
  │                 → remaining tool calls in current response SKIPPED
  │                 → their results returned as "operation cancelled: steered"
  │                 → next model.invoke() sees steering message
  │
  ├── handle.follow_up("also do X")
  │     └── message put on followup_queue
  │         └── Strategy checks at end_turn:
  │             └── Message found → inject as user message
  │                 → instead of returning LoopResult, continue loop
  │
  └── handle.cancel()
        └── state.cancel_event.set()
            └── ToolContext.cancelled.is_set() → True
                → Tool can check and abort
                → Strategy detects cancel → returns partial LoopResult
```

## arcllm Integration

arcrun uses these arcllm types:

```python
from arcllm import (
    Message,           # role + content
    LLMResponse,       # .content, .tool_calls, .stop_reason, .usage
    ToolCall,          # .id, .name, .arguments
    TextBlock,         # text content
    ToolUseBlock,      # tool call content
    ToolResultBlock,   # tool result content
    Usage,             # .input_tokens, .output_tokens, .total_tokens
)
```

**Message construction for tool results:**
```python
# Assistant message with text + tool calls:
assistant_content = []
if response.content:
    assistant_content.append(TextBlock(text=response.content))
for tc in response.tool_calls:
    assistant_content.append(ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments))
messages.append(Message(role="assistant", content=assistant_content))

# Tool results:
for tc, result in zip(tool_calls, results):
    messages.append(Message(
        role="tool",
        content=[ToolResultBlock(tool_use_id=tc.id, content=result)],
    ))
```

## Line Budget

| File | Estimated Lines |
|------|-----------------|
| `__init__.py` | ~20 |
| `types.py` | ~70 |
| `events.py` | ~60 |
| `sandbox.py` | ~80 |
| `registry.py` | ~50 |
| `state.py` | ~60 |
| `loop.py` | ~120 |
| `strategies/__init__.py` | ~30 |
| `strategies/react.py` | ~120 |
| **Total** | **~610** |

Phase 1 budget: ~500 (revised to ~610 with steering/registry additions)
Total budget: 1,000 lines

## Dependencies

| Package | Why | Notes |
|---------|-----|-------|
| arcllm | Model invocation, message types | Core dependency |
| jsonschema | Tool param validation | Overrides zero-dep PRD constraint (DECISION-022) |

## Testing Strategy

- Mock arcllm model (returns predetermined responses)
- Test each module independently
- Integration test: full run() with mock model + tools
- Event coverage: verify every action emits expected events
- Sandbox tests: allowlist enforcement, denial events, callback errors
- Registry tests: add/remove/replace mid-execution
- Steering tests: steer interrupt, followUp queue, cancel signal
- Edge cases: empty tools (ValueError), all tools denied, max_turns=0, model error propagation
