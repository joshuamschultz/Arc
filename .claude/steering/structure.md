# arcrun — Structure Context

## Package Layout

```
arcrun/
├── __init__.py            # Public API: run(), RunHandle, Tool, LoopResult, etc.
├── loop.py                # run() entry point + RunHandle (~120 lines)
├── state.py               # RunState — internal state during execution (~60 lines)
├── events.py              # Event bus + Event dataclass (~60 lines)
├── sandbox.py             # Permission boundary (~80 lines)
├── registry.py            # Dynamic tool registry (~50 lines)
├── types.py               # Tool, LoopResult, SandboxConfig (~70 lines)
│
├── strategies/
│   ├── __init__.py        # Strategy interface + selection (~30 lines)
│   ├── react.py           # ReAct loop (~120 lines)
│   ├── code.py            # CodeExec strategy (~80 lines)
│   └── recursive.py       # Recursive decomposition (~80 lines)
│
└── builtins/
    ├── spawn.py           # Sub-loop with isolated context (~80 lines)
    └── execute.py         # Sandboxed Python execution (~80 lines)
```

**Line budget:** ~750 lines, leaving headroom for Phase 4+ additions under 1,000.

## Project Root Layout

```
arcrun/                   # Git repo root
├── src/
│   └── arcrun/           # Package source (above)
├── tests/
│   ├── test_loop.py       # End-to-end loop tests
│   ├── test_events.py     # Event bus tests
│   ├── test_sandbox.py    # Sandbox permission tests
│   ├── test_types.py      # Type construction tests
│   ├── test_react.py      # ReAct strategy tests
│   ├── test_code.py       # CodeExec strategy tests
│   ├── test_recursive.py  # Recursive strategy tests
│   ├── test_spawn.py      # SpawnTool tests
│   └── test_execute.py    # ExecuteTool tests
├── .claude/
│   ├── steering/          # These docs
│   ├── decision-log.md    # All architectural decisions
│   └── specs/             # Feature specifications
├── pyproject.toml
├── README.md
└── arcrun-PRD.md         # Product requirements (locked decisions)
```

## Module Boundaries

### `__init__.py` — Public API Surface

Exports only: `run`, `RunHandle`, `Tool`, `ToolRegistry`, `LoopResult`, `SandboxConfig`, `Event`, `EventBus`

Everything else is internal. Users should never import from submodules directly.

### `loop.py` — Entry Point + RunHandle

- Contains `run()` — blocking entry point (returns LoopResult)
- Contains `run_async()` — non-blocking entry point (returns RunHandle for steering)
- `RunHandle`: steer(), cancel(), result(), state (read-only access)
- Wires together: RunState, EventBus, Sandbox, ToolRegistry, strategy selection
- No business logic in this file — it's pure orchestration

### `state.py` — Run-Level State

- `RunState`: messages, turn count, token/cost accumulators, tool registry ref, event log
- Internal to the run. Dies when run() returns.
- Read-only access via RunHandle for callers
- Enables steering, streaming, context transform

### `events.py` — Event System

- `Event` dataclass: timestamp, event_type, run_id, parent_run_id, depth, data
- `EventBus`: emit(), events list, on_event callback
- Synchronous emission — handler called inline
- Auto-populates timestamp, run_id, depth

### `sandbox.py` — Permission Boundary

- `Sandbox` class: takes SandboxConfig + EventBus
- `check(tool_name, params)` -> (allowed: bool, reason: str)
- Emits events for both allowed and denied actions
- Path checking, network checking, write checking

### `registry.py` — Dynamic Tool Registry

- `ToolRegistry`: mutable collection of Tool objects
- `add(tool)`, `remove(name)`, `get(name)`, `list_schemas()`
- Read by strategy each turn to get current tool set
- Mutations emit events (tool.registered, tool.removed)

### `types.py` — Contracts

- `Tool`: name, description, input_schema, execute (async callable)
- `LoopResult`: content, turns, tool_calls_made, tokens_used, strategy_used, cost_usd, events
- `SandboxConfig`: allowed_paths, denied_paths, allow_network, allow_file_write
- `RunHandle`: steer(), cancel(), result(), state

### `strategies/` — Execution Strategies

Each strategy is a single async function that operates on RunState:

```python
async def react_loop(model, state: RunState, sandbox, max_turns) -> LoopResult
async def code_loop(model, state: RunState, sandbox, max_turns) -> LoopResult
async def recursive_loop(model, state: RunState, sandbox, max_turns, spawn_config) -> LoopResult
```

Strategies read tools from `state.registry` each turn (enabling dynamic tool changes).

Strategy selection: if multiple allowed, model picks on first turn. Single allowed → used directly.

### `builtins/` — Optional Built-in Tools

- `SpawnTool`: Creates a new `run()` call with isolated context. Parent gets compact result.
- `ExecuteTool`: Runs model-written Python in sandboxed subprocess. Returns stdout/stderr.

Caller includes these if they want the capability. No SpawnTool = can't spawn. No ExecuteTool = can't execute code.

## Data Flow

```
run(model, tools, system_prompt, task, **options)
    |
    v
Create RunState (messages, registry, event_bus, accumulators)
    |
    v
EventBus.emit("loop.start")
    |
    v
Strategy Router → picks react/code/recursive
    |
    v
LOOP:
    check for steering messages → inject if present
        |
        v
    transform_context(messages) if hook provided
        |
        v
    tools = state.registry.list_schemas()  (dynamic — re-read each turn)
        |
        v
    response = model.invoke(messages, tools)   [streaming if supported]
        |
        v
    if end_turn → return LoopResult
        |
        v
    for each tool_call:
        sandbox.check() → allowed? → tool.execute() → result
                        → denied?  → error result back to model
        |
        v
    messages.append(tool_results)
    continue LOOP
        |
        v
LoopResult (content, turns, events, tokens, cost)
```

## Dependency Direction

```
Your Agent Code
    ↓ depends on
arcrun (run, Tool, LoopResult)
    ↓ depends on
arcllm (Message, LLMResponse, load_model)
    ↓ depends on
pydantic, httpx
```

arcrun NEVER depends upward. It has no knowledge of what calls it.

## Integration with arcllm Observability

arcllm's OTel module creates spans for `arcllm.invoke`. arcrun creates its own spans:

```
arcrun.run                        ← arcrun creates
  ├── arcrun.turn[0]              ← arcrun creates
  │   ├── arcllm.invoke            ← arcllm OTel module creates (if enabled)
  │   │   └── arcllm.retry         ← arcllm retry module (if enabled)
  │   ├── arcrun.tool.read        ← arcrun creates
  │   └── arcrun.tool.bash        ← arcrun creates
  ├── arcrun.turn[1]
  │   ├── arcllm.invoke
  │   └── arcrun.tool.search
  └── arcrun.complete
```

This nesting happens automatically via OTel context propagation. arcrun just needs to create spans — arcllm spans auto-nest as children.
