```

   ╭───────────────────────────────────────────╮
   │                                           │
   │     ┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐                    │
   │     ├─┤├┬┘│  ├┬┘│ ││ │┌┐                  │
   │     ┴ ┴┴└─└─┘┴└─└─┘┘└┘┘                   │
   │                                           │
   │     async execution engine for agents     │
   │                                           │
   │     model + tools + task  ──►  result     │
   │     every action audited. always.         │
   │                                           │
   ├───────────────────────────────────────────┤
   │                                           │
   │     ┌───────────────────────────┐         │
   │     │  YOUR AGENT               │         │
   │     │  prompts · tools · config │         │
   │     ├───────────────────────────┤         │
   │     │  arcrun            ◄ here │         │
   │     │  loop · sandbox · events  │         │
   │     ├───────────────────────────┤         │
   │     │  arcllm                   │         │
   │     │  providers · transport    │         │
   │     └───────────────────────────┘         │
   │                                           │
   ╰───────────────────────────────────────────╯
```

# arcrun

**The execution engine for autonomous agents.** arcrun receives an [arcllm](https://github.com/joshuamschultz/arcllm) model, a set of tools, and a task — then loops until the task is done.

arcrun is to agents what an engine is to a car. The car (your agent) decides where to go. The engine (arcrun) makes it move.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Lines of Code](https://img.shields.io/badge/lines-<1000-brightgreen.svg)]()

---

## Why arcrun

Most agent frameworks do too much. They own your prompts, your config, your session management, your UI. You end up fighting the framework.

arcrun does one thing: **execute the loop**.

- **5 lines to run** — `await run(model, tools, prompt, task)`
- **Under 1,000 lines** — small enough for a model to reason about
- **Every action auditable** — events emit for everything, always, non-optional
- **Deny-by-default sandbox** — tool calls checked before execution
- **Zero opinions** — no agents, no sessions, no config format, no UI

You build the agent. arcrun makes it move.

---

## Install

```bash
pip install arcrun
```

Requires Python 3.11+. Only dependency beyond arcllm is `jsonschema` for tool parameter validation.

---

## Quickstart

```python
from arcllm import load_model
from arcrun import run, Tool

model = load_model("anthropic")

read_tool = Tool(
    name="read_file",
    description="Read contents of a file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    execute=my_read_fn,  # async def my_read_fn(params, ctx) -> str
)

result = await run(
    model=model,
    tools=[read_tool],
    system_prompt="You are a helpful assistant.",
    task="Read /data/report.txt and summarize it",
)

print(result.content)       # final response
print(result.turns)         # loop iterations
print(result.tool_calls_made)  # total tool invocations
print(result.tokens_used)   # {"input": N, "output": N, "total": N}
print(result.cost_usd)      # estimated cost
print(result.events)        # full audit trail
```

That's it. Five lines of setup. One call to `run()`.

---

## Core Concepts

### The Loop

```
run(model, tools, system_prompt, task)
  │
  ├── EMIT: loop.start
  │
  ├── Strategy Selection (react / code / recursive)
  │   EMIT: strategy.selected
  │
  ├── LOOP ──────────────────────────────────────┐
  │   │                                           │
  │   ├── EMIT: turn.start                       │
  │   │                                           │
  │   ├── response = model.invoke(messages, tools)│
  │   │   EMIT: llm.call                         │
  │   │                                           │
  │   ├── if end_turn → return LoopResult         │
  │   │                                           │
  │   ├── for each tool_call:                     │
  │   │     sandbox.check() → allowed? → execute  │
  │   │                     → denied?  → error    │
  │   │     EMIT: tool.start / tool.end           │
  │   │                                           │
  │   ├── messages.append(results)                │
  │   │   EMIT: turn.end                         │
  │   └───────────────────────── continue ────────┘
  │
  ├── EMIT: loop.complete
  │
  └── return LoopResult
```

The model reasons, picks tools, observes results, repeats. arcrun handles tool dispatch, sandbox checks, event emission, and message management. The model just sees `invoke()`.

### Tools

Tools are what the model can call. You define them, arcrun validates and dispatches.

```python
from arcrun import Tool

# Simple: pass a function
search_tool = Tool(
    name="search",
    description="Search the database",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    execute=my_search_fn,
)

# Complex: use a factory for stateful tools
def make_db_tool(connection):
    async def execute(params, ctx):
        return await connection.query(params["sql"])

    return Tool(
        name="query_db",
        description="Run SQL query",
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
        execute=execute,
    )
```

Every `execute` function receives:
- `params` — validated against `input_schema` before your code runs
- `ctx` — a `ToolContext` with `run_id`, `tool_call_id`, `turn_number`, `event_bus`, and `cancelled` signal

Return a string. Raise an exception for errors (arcrun catches it, emits `tool.error`, sends the error back to the model).

### Events

Every action emits an event. Always. Non-negotiable. This is the audit trail.

| Event | When |
|---|---|
| `loop.start` | `run()` called |
| `loop.complete` | Finished successfully |
| `loop.max_turns` | Hit turn limit |
| `strategy.selected` | Strategy chosen |
| `turn.start` / `turn.end` | Each iteration |
| `llm.call` | Every `model.invoke()` |
| `tool.start` / `tool.end` | Every tool execution |
| `tool.denied` | Sandbox denied a call |
| `tool.error` | Tool execution failed |
| `spawn.start` / `spawn.end` | Sub-loop lifecycle |
| `spawn.denied` | Budget exceeded |

```python
# Real-time handler
def my_handler(event):
    print(f"[{event.type}] {event.data}")

result = await run(..., on_event=my_handler)

# Post-execution analysis
for event in result.events:
    if event.type == "tool.denied":
        print(f"DENIED: {event.data['name']} — {event.data['reason']}")
```

### Sandbox

Deny-by-default permission boundary. Checks before every tool execution.

```python
from arcrun import SandboxConfig

# Allowlist: only these tools can run
sandbox = SandboxConfig(
    allowed_tools=["read_file", "search"],
)

# Custom checker for granular control
async def my_checker(tool_name, params):
    if tool_name == "read_file" and "/etc" in params.get("path", ""):
        return False, "access to /etc denied"
    return True, ""

sandbox = SandboxConfig(
    allowed_tools=["read_file", "search", "write_file"],
    check=my_checker,
)

result = await run(..., sandbox=sandbox)
```

When sandbox denies a tool call:
1. `tool.denied` event emits with the reason
2. Error message returns to the model (it can adjust)
3. Loop continues

### Dynamic Tool Registry

Tools can be added, removed, or replaced during execution. The loop re-reads the registry each turn.

```python
from arcrun import run_async

handle = await run_async(model, tools, prompt, task)

# Agent discovers it needs a new tool mid-task
handle.state.registry.add(new_tool)

# Security: dynamically-added tools are denied by default
# when sandbox is configured — caller must also update sandbox
```

### Steering

Inject instructions while the loop is running. Two modes:

```python
handle = await run_async(model, tools, prompt, task)

# Interrupt: inject after current tool, skip remaining
await handle.steer("Stop analyzing and focus on section 3 instead")

# Queue: inject at end_turn before returning
await handle.follow_up("Also summarize the key findings")

# Hard stop
await handle.cancel()

result = await handle.result()
```

### Context Transform

Prevent context overflow in long-running loops with a caller-provided hook:

```python
def my_pruner(messages):
    """Keep system + last 20 messages."""
    return [messages[0]] + messages[-20:]

result = await run(
    ...,
    transform_context=my_pruner,
)
```

Called before every `model.invoke()`. You control the strategy.

---

## Execution Strategies

arcrun supports multiple execution strategies. The model picks (or you constrain):

### ReAct (Default)

Reason → Act → Observe → Repeat. The standard tool-calling loop. Uses whatever tools you pass in. Terminates on `end_turn` or `max_turns`.

```python
result = await run(
    ...,
    allowed_strategies=["react"],  # or omit — it's the default
)
```

### CodeExec

Model writes Python code. `ExecuteTool` runs it in a sandboxed subprocess. Only available when you include the built-in `ExecuteTool`.

```python
from arcrun.builtins import ExecuteTool

result = await run(
    model=model,
    tools=[*my_tools, ExecuteTool()],
    ...,
    allowed_strategies=["code"],
)
```

### Recursive

Model decomposes tasks into sub-problems. `SpawnTool` creates isolated sub-loops with fresh context. Parent gets a compact result — never the child's full conversation.

```python
from arcrun.builtins import SpawnTool

result = await run(
    model=model,
    tools=[*my_tools, SpawnTool()],
    ...,
    allowed_strategies=["recursive"],
    max_spawn_depth=3,
    max_total_spawns=20,
    max_cost_usd=5.00,
)
```

### Strategy Selection

Allow multiple strategies and let the model choose based on the task:

```python
result = await run(
    ...,
    allowed_strategies=["react", "code", "recursive"],
)
# result.strategy_used tells you which it picked
```

---

## Architecture

```
arcrun/
├── __init__.py            # Public API: run(), Tool, LoopResult, etc.
├── loop.py                # run() + run_async() + RunHandle
├── state.py               # RunState — internal state during execution
├── events.py              # Event bus + Event dataclass
├── sandbox.py             # Permission boundary
├── registry.py            # Dynamic tool registry
├── types.py               # Tool, LoopResult, SandboxConfig, ToolContext
│
├── strategies/
│   ├── __init__.py        # Strategy interface + selection
│   ├── react.py           # ReAct loop
│   ├── code.py            # CodeExec strategy
│   └── recursive.py       # Recursive decomposition
│
└── builtins/
    ├── spawn.py           # Sub-loop with isolated context
    └── execute.py         # Sandboxed Python execution
```

**Total budget: under 1,000 lines of Python.**

### Layer Separation

```
┌─────────────────────────────────────────────┐
│  YOUR AGENT (you build this)                │
│  System prompt, tool selection, sessions    │
│  Extension system, config, UI, memory       │
│  Passes tools + arcllm model into arcrun    │
├─────────────────────────────────────────────┤
│  arcrun (this package)                      │
│  Execution loop (ReAct / CodeExec / Recurse)│
│  Tool dispatch + validation                 │
│  Event emission (every action, always)      │
│  Sandbox (permission boundary)              │
│  Spawn (context isolation)                  │
├─────────────────────────────────────────────┤
│  arcllm                                     │
│  load_model("anthropic")                    │
│  await model.invoke(messages, tools=tools)  │
│  Provider abstraction, token tracking       │
│  Security, telemetry, retry, fallback       │
└─────────────────────────────────────────────┘
```

arcrun calls `model.invoke()`. That's the only touchpoint with arcllm. arcrun never calls `load_model()`, never configures providers, never handles API keys.

### What arcrun Does NOT Have

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

## Security

arcrun is built for federal and enterprise deployment. Security is non-optional.

### Threat Model

Formal analysis covers:
- **OWASP Agentic AI (T1-T15)** — tool misuse, resource overload, spawn bombs, RCE, agent poisoning
- **OWASP LLM Top 10 (2025)** — prompt injection, excessive agency, unbounded consumption
- **NIST SP 800-53** — 12 controls mapped directly to arcrun features

### Defense Layers

| Layer | Mechanism |
|---|---|
| **Tool allowlist** | Only explicitly allowed tools can execute |
| **Param validation** | JSON Schema validation before every `execute()` |
| **Sandbox checker** | Caller-provided callback for granular permission logic |
| **Event audit trail** | Every action logged — non-optional, non-disableable |
| **Spawn budgets** | Depth limit, total limit, cost ceiling — prevents spawn bombs |
| **Dynamic tool denial** | New tools added mid-execution are denied by default |
| **Cancel signal** | Tools receive cancellation signal for clean shutdown |
| **Context isolation** | Child spawns get fresh context, cannot access parent conversation |
| **Sandbox inheritance** | Children inherit parent sandbox — cannot expand permissions |

### NIST SP 800-53 Coverage

| Control | Title | arcrun Feature |
|---|---|---|
| AC-3 | Access Enforcement | Sandbox deny-by-default |
| AC-4 | Information Flow | Spawn context isolation |
| AC-6 | Least Privilege | Explicit tool allowlist |
| AU-2 | Event Logging | Every action emits event |
| AU-3 | Audit Content | Events include timestamp, run_id, tool, args, duration |
| AU-8 | Timestamps | ISO 8601 on every event |
| AU-12 | Audit Generation | Non-optional emission |
| CM-7 | Least Functionality | Tools are opt-in |
| SC-28 | Protection at Rest | State dies when `run()` returns |
| SI-4 | System Monitoring | Events, tokens, cost tracking |
| SI-10 | Input Validation | JSON Schema on every tool call |
| SI-11 | Error Handling | Errors return to model as structured results |

### arcllm Security (Inherited)

arcrun inherits arcllm's transport-layer security automatically:

| Module | Capability |
|---|---|
| SecurityModule | PII redaction, HMAC request signing |
| AuditModule | Structured compliance logging |
| OtelModule | Distributed tracing (OpenTelemetry GenAI conventions) |
| RateLimitModule | Token-bucket throttling |
| RetryModule | Exponential backoff on transient failures |
| FallbackModule | Provider failover chain |
| VaultResolver | Secrets management integration |

arcrun focuses on execution-layer security. arcllm handles transport-layer security. No duplication.

---

## API Reference

### `run()`

```python
result = await run(
    model=model,                          # arcllm model (required)
    tools=[tool1, tool2],                 # list of Tool (required, non-empty)
    system_prompt="You are...",           # str (required)
    task="Do the thing",                  # str (required)

    # Optional:
    max_turns=25,                         # int — default 25
    allowed_strategies=["react"],         # list[str] — default ["react"]
    sandbox=SandboxConfig(...),           # permission boundary
    on_event=my_handler,                  # callback for real-time events
    transform_context=my_pruner,          # context management hook
    max_spawn_depth=3,                    # recursive spawn limit
    max_total_spawns=20,                  # total spawn budget
    max_cost_usd=5.00,                   # cost ceiling (USD)
)
```

### `run_async()`

```python
handle = await run_async(model, tools, prompt, task, **options)

await handle.steer("new instructions")    # interrupt
await handle.follow_up("also do this")    # queue for end_turn
await handle.cancel()                     # hard stop
result = await handle.result()            # await completion
state = handle.state                      # read-only state access
```

### `Tool`

```python
Tool(
    name="tool_name",           # unique identifier
    description="What it does", # shown to model
    input_schema={...},         # JSON Schema for params
    execute=async_fn,           # async (params, ctx) -> str
)
```

### `ToolContext`

```python
@dataclass
class ToolContext:
    run_id: str                 # execution run ID
    tool_call_id: str           # correlation ID for this call
    turn_number: int            # current loop turn
    event_bus: EventBus | None  # emit custom events
    cancelled: asyncio.Event    # check for cancellation
```

### `LoopResult`

```python
@dataclass
class LoopResult:
    content: str | None         # final response text
    turns: int                  # loop iterations
    tool_calls_made: int        # total tool invocations
    tokens_used: dict           # {"input": N, "output": N, "total": N}
    strategy_used: str          # "react" | "code" | "recursive"
    cost_usd: float             # estimated cost
    events: list[Event]         # full audit trail
```

### `SandboxConfig`

```python
SandboxConfig(
    allowed_tools=["read", "search"],     # allowlist (None = no sandbox)
    check=my_async_checker,               # async (name, params) -> (bool, reason)
)
```

### `Event`

```python
@dataclass
class Event:
    type: str                   # "tool.start", "llm.call", etc.
    timestamp: float            # time.time()
    run_id: str                 # correlation ID
    data: dict[str, Any]        # event-specific payload
```

---

## Development

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests
pytest -v
pytest --cov=arcrun

# Type checking
mypy src/arcrun

# Linting
ruff check src/arcrun
ruff format src/arcrun
```

### Quality Thresholds

| Metric | Target |
|---|---|
| Total lines | < 1,000 |
| Test coverage | >= 80% |
| Cyclomatic complexity | <= 10 per function |
| Critical vulnerabilities | 0 |
| Type hints | Required on public API |
| Async-only | No sync wrappers in core |

---

## Roadmap

| Phase | Name | Goal | Status |
|---|---|---|---|
| 1 | Core Loop + ReAct | `run()` works end-to-end with events and sandbox | **Active** |
| 2 | CodeExec | Model writes + executes Python in sandboxed subprocess | Planned |
| 3 | Recursive | Task decomposition via spawn with isolated context | Planned |
| 4 | Hardening | Container sandbox, event integrity, adversarial testing, NIST docs | Planned |
| 5 | RLM | Recursive Language Models for near-infinite context processing | Research |

---

## Design Decisions

All architectural decisions are logged with context, options, and reasoning. 25 decisions made so far, including:

- **Package name** — `arcrun` over `arcloop`, `arcexec`, `arcengine` (action-oriented, verb-based)
- **Tool.execute async-only** — matches arcllm's async-native design
- **Tool.execute returns str** — simplest thing that works, errors via exceptions
- **Generic Event with dict data** — one dataclass saves ~190 lines vs typed events
- **Allowlist security model** — deny-by-default, dynamic tools denied automatically
- **Strategy enforces max_turns** — strategies own the loop, define what "turn" means
- **Caller-provided sandbox checker** — arcrun makes zero assumptions about tool internals
- **Steer + followUp** — two delivery modes for mid-execution intervention
- **jsonschema for validation** — correctness on every tool call worth the dependency

Full log: [`.claude/decision-log.md`](.claude/decision-log.md)

---

## License

MIT
