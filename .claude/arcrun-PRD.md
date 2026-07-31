# arcLoop — Product Requirements Document

## Version 0.3 | February 2026

---

## What arcLoop Is

arcLoop is the execution engine. It receives an arcllm model, a set of tools, and a task — then loops until the task is done. That's it.

It does not configure models. It does not discover extensions. It does not define agents. It does not manage sessions. It does not have opinions about what you're building.

arcLoop is to agents what an engine is to a car. The car (your agent) decides where to go. The engine (arcLoop) makes it move.

---

## Layer Map

```
┌─────────────────────────────────────────────────┐
│  YOUR AGENT (you build this)                    │
│  - System prompt, tool selection, session mgmt  │
│  - Extension system, config, UI                 │
│  - Passes tools + arcllm model into arcLoop     │
├─────────────────────────────────────────────────┤
│  arcLoop (this package)                         │
│  - Execution loop (ReAct / CodeExec / Recursive)│
│  - Tool execution + validation                  │
│  - Event emission (every action, always)        │
│  - Sandbox (permission boundary)                │
│  - Spawn (context isolation primitive)          │
├─────────────────────────────────────────────────┤
│  arcllm (existing package)                      │
│  - load_model("anthropic")                      │
│  - await model.invoke(messages, tools=tools)    │
│  - Provider abstraction, token tracking         │
└─────────────────────────────────────────────────┘
```

---

## Design Priorities

1. **Simple and clear** — Core must be small enough for a model to reason about
2. **Security first** — Every action auditable. Deny-by-default. Log everything.
3. **Modular and extensible** — Tiny core. Hooks for the layer above. No opinions beyond the loop.

---

## Locked Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Extends arcllm |
| Distribution | `pip install arc` (umbrella) | Single install |
| Async | Default. `await run(...)` | arcllm is async (`await model.invoke()`) |
| arcllm integration | Receives model object from `load_model()`, calls `await model.invoke(messages, tools=tools)` | Caller configures model, loop just calls it |
| What arcLoop owns | Loop, tool dispatch, events, sandbox, spawn | The engine |
| What arcLoop does NOT own | Model config, agents, extensions, sessions, config format, UI | Caller's problem |
| Strategies | ReAct, CodeExec, Recursive | Three ways to loop |
| Tools | Passed in by caller | name + description + schema + async execute() |
| Events | Every action, always, synchronous emission | Non-optional audit trail |
| Sandbox | Permission boundary, caller declares policy | Deny-by-default |
| spawn / execute | Built-in tools, caller includes if wanted | Separate tools for auditability |
| Line budget | Under 1,000 lines total | Forces simplicity |

---

## arcllm Integration (Confirmed)

```python
from arcllm import load_model

model = load_model("anthropic")
# or: model = load_model("anthropic", "claude-sonnet-4")

response = await model.invoke(messages, tools=tools)

# LLMResponse:
response.content        # str | None — text, null if pure tool calls
response.tool_calls     # list[ToolCall] — .id, .name, .arguments (parsed dict)
response.stop_reason    # "end_turn" | "tool_use" | "max_tokens"
response.usage          # .input_tokens, .output_tokens, .total_tokens
response.model          # which model responded
response.thinking       # reasoning content if available
response.raw            # original provider response

# ToolCall:
tc.id                   # correlation ID
tc.name                 # tool name
tc.arguments            # dict (already parsed by arcllm adapter)
```

arcLoop calls `model.invoke()`. That's the only touchpoint. arcLoop never calls `load_model()`, never configures providers, never handles API keys.

---

## Architecture

```
caller passes in:
  ├── model (from arcllm load_model — already configured)
  ├── tools[] (Tool objects with async execute)
  ├── system_prompt (string)
  ├── task (string)
  └── options (max_turns, allowed_strategies, sandbox, callbacks)
        │
        ▼
┌─ arcLoop ──────────────────────────────────────────────┐
│                                                         │
│  Event Bus (emits everything, always)                  │
│    │                                                    │
│  Strategy Router                                       │
│    │  picks ReAct / CodeExec / Recursive               │
│    │  (constrained by caller's allowed_strategies)     │
│    ▼                                                    │
│  ┌───────────────────────────────────────────────────┐ │
│  │ LOOP                                               │ │
│  │                                                    │ │
│  │  response = await model.invoke(messages, tools)    │ │
│  │                                                    │ │
│  │  if response.stop_reason == "end_turn":           │ │
│  │    return LoopResult                               │ │
│  │                                                    │ │
│  │  for tc in response.tool_calls:                   │ │
│  │    sandbox.check(tc.name, tc.arguments)           │ │
│  │    result = await tool.execute(tc.arguments, ctx) │ │
│  │    emit events                                     │ │
│  │                                                    │ │
│  │  messages.append(tool_results)                     │ │
│  │  continue                                          │ │
│  └───────────────────────────────────────────────────┘ │
│                                                         │
│  Built-in tools (caller includes if wanted):           │
│    SpawnTool   → new run() with isolated context       │
│    ExecuteTool → sandboxed Python subprocess            │
│                                                         │
└─────────────────────────────────────────────────────────┘
        │
        ▼
  LoopResult
    ├── content          # final response text
    ├── turns            # loop iterations
    ├── tool_calls_made  # total tool invocations
    ├── tokens_used      # {input, output, total}
    ├── strategy_used    # "react" | "code" | "recursive"
    ├── cost_usd         # estimated
    └── events[]         # full event log
```

---

## Package Structure

```
arcloop/
├── __init__.py            # Public API: run(), Tool, LoopResult
├── loop.py                # The loop + run() entry point
├── events.py              # Event bus + event dataclasses
├── sandbox.py             # Permission boundary
├── types.py               # Tool, LoopResult, SandboxConfig
│
├── strategies/
│   ├── __init__.py        # Strategy interface + selection
│   ├── react.py           # ReAct
│   ├── code.py            # CodeExec
│   └── recursive.py       # Recursive
│
└── builtins/
    ├── spawn.py           # Sub-loop with isolated context
    └── execute.py         # Sandboxed Python execution
```

---

## Core Interface

### run() — The only entry point

```python
from arcloop import run, Tool, SandboxConfig
from arcllm import load_model

model = load_model("anthropic")

read_tool = Tool(
    name="read",
    description="Read file contents",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
    },
    execute=my_read_fn       # async def my_read_fn(params, ctx) -> str
)

result = await run(
    model=model,
    tools=[read_tool, bash_tool, write_tool],
    system_prompt="You are a helpful assistant.",
    task="List the files in /data and summarize the largest one",

    # All optional:
    max_turns=25,
    allowed_strategies=["react"],
    sandbox=SandboxConfig(
        allowed_paths=["/data"],
        denied_paths=["/etc", "/root"],
        allow_network=False,
    ),
    on_event=my_event_handler,
    max_spawn_depth=3,
    max_total_spawns=20,
    max_cost_usd=5.00,
)

print(result.content)
print(result.turns)
print(result.events)
```

### Tool — What the caller passes in

```python
from arcloop import Tool

# Functional style
read_tool = Tool(
    name="read",
    description="Read file contents",
    input_schema={...},
    execute=my_async_function
)

# Class style for complex tools
class SearchContracts(Tool):
    name = "search_contracts"
    description = "Search federal contract database"
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }

    async def execute(self, params: dict, context: dict) -> str:
        return results
```

arcLoop validates `params` against `input_schema` before calling `execute()`. Validation failures return to the model as tool results so it can retry.

### Built-in Tools

```python
from arcloop.builtins import SpawnTool, ExecuteTool

# Caller includes if they want these capabilities
result = await run(
    model=model,
    tools=[*my_tools, SpawnTool(), ExecuteTool()],
    ...
)
```

**SpawnTool**: New `run()` call, fresh context. Parent gets compact result, never child's full conversation.

**ExecuteTool**: Model-written Python in sandboxed subprocess. Returns stdout/stderr.

No SpawnTool in tools → can't spawn. No ExecuteTool → can't execute code. Caller decides.

---

## The Loop

```
await run(model, tools, system_prompt, task, **options)
    │
    EMIT: loop.start {task, tool_names, strategy_options}
    │
    Strategy Selection (if multiple allowed)
    │  EMIT: strategy.selected {strategy}
    │
    ┌─ LOOP (max_turns) ────────────────────────────────┐
    │                                                     │
    │  EMIT: turn.start {turn_number}                    │
    │                                                     │
    │  response = await model.invoke(messages, tools)     │
    │  EMIT: llm.call {                                  │
    │    tokens: response.usage,                         │
    │    model: response.model,                          │
    │    stop_reason: response.stop_reason,              │
    │    latency_ms, cost_usd                            │
    │  }                                                  │
    │                                                     │
    │  if response.stop_reason == "end_turn":            │
    │    EMIT: loop.complete {content, totals}            │
    │    return LoopResult                                │
    │                                                     │
    │  for tc in response.tool_calls:                    │
    │    EMIT: tool.start {name: tc.name, args: tc.arguments}
    │    if not sandbox.check(tc.name, tc.arguments):    │
    │      EMIT: tool.denied {name, args, reason}        │
    │      → error result back to model                  │
    │      continue                                       │
    │    result = await find_tool(tc.name).execute(       │
    │        tc.arguments, context)                       │
    │    EMIT: tool.end {name, result_len, duration_ms}  │
    │                                                     │
    │  messages.append(tool_results)                      │
    │  EMIT: turn.end {turn_number}                      │
    └────────────────────────────────────────────────────┘
    │
    EMIT: loop.max_turns {turns_used}
    return LoopResult
```

---

## Execution Strategies

### ReAct (Default)

Model reasons → picks tool → observes → repeats. Default if no `allowed_strategies`. Uses caller's tools. Terminates on `end_turn` or `max_turns`.

### CodeExec

Model writes Python. ExecuteTool runs it. Only available if caller includes `ExecuteTool()`. System prompt augmented to encourage code-writing.

### Recursive

Model decomposes task. SpawnTool creates sub-loops. Only available if caller includes `SpawnTool()`. Sub-loops get fresh context. Parent gets compact result.

### Strategy Selection

Multiple allowed → model picks, logged as event. Single allowed → used directly.

---

## Events

Every action emits. Always. Non-negotiable.

| Event | When |
|---|---|
| `loop.start` | run() called |
| `loop.complete` | Finished successfully |
| `loop.max_turns` | Hit turn limit |
| `strategy.selected` | Model picked strategy |
| `turn.start` / `turn.end` | Each iteration |
| `llm.call` | Every model.invoke() call |
| `tool.start` / `tool.end` | Every tool execution |
| `tool.denied` | Sandbox denied |
| `tool.error` | Execution failed |
| `spawn.start` / `spawn.end` | Sub-loop lifecycle |
| `spawn.denied` | Budget exceeded |

```python
# Real-time
result = await run(..., on_event=my_handler)

# After execution
for event in result.events:
    print(event.type, event.timestamp)
```

---

## Sandbox

```python
sandbox = SandboxConfig(
    allowed_paths=["/data/contracts", "/tmp/work"],
    denied_paths=["/etc", "/root", "/home"],
    allow_network=False,
    allow_file_write=True,
)
```

Checks before every tool execution. Denied → error to model + event. Loop continues. Spawn inherits parent sandbox. Children cannot expand permissions.

---

## Spawn Guardrails

```python
result = await run(
    ...,
    max_spawn_depth=3,
    max_total_spawns=20,
    max_cost_usd=5.00,
)
```

Enforced at SpawnTool. Limit hit → denied → error to model + event. Parent continues.

---

## What arcLoop Does NOT Have

| Thing | Where It Lives |
|---|---|
| Agent definitions | Your agent code |
| Extension system | Your agent code |
| Session management | Your agent code |
| Config file loading | Your agent code |
| Model configuration | arcllm (`load_model()`) |
| Tool discovery | Your agent code |
| Memory / RAG | Your agent code |
| UI / CLI | Your agent code |

---

## Build Phases

### Phase 1: Core Loop + ReAct
- `run()`, Tool, LoopResult, EventBus, Sandbox, ReAct strategy
- Integration: `await model.invoke(messages, tools=tools)`
- **Gate:** `await run(model, tools, prompt, task)` end-to-end with events

### Phase 2: CodeExec
- ExecuteTool, CodeExec strategy, strategy selection
- **Gate:** Model writes and runs Python

### Phase 3: Recursive
- SpawnTool, spawn budgets, recursive strategy, sandbox inheritance
- **Gate:** Model decomposes into sub-loops, results flow back

### Phase 4: Hardening
- Container sandbox, event integrity, adversarial testing, NIST documentation

---

## Success Criteria

1. `await run()` works in 5 lines with an arcllm model and tools
2. Under 1,000 lines of Python
3. Every tool call emits an auditable event
4. Sandbox prevents unauthorized access
5. Spawn creates isolated contexts
6. Works with any arcllm provider
7. A coding agent can pass its own tools in and they just work
