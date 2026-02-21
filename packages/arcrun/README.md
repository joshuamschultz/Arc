```
╭──────────────────────────────────────────────────────╮
│                                                      │
│   ▄▀█ █▀█ █▀▀ █▀█ █ █ █▄ █                         │
│   █▀█ █▀▄ █▄▄ █▀▄ █▄█ █ ▀█                         │
│                                                      │
│   Async Execution Engine                             │
│   for Autonomous Agents at Scale                     │
│                                                      │
├──────────────────────────────────────────────────────┤
│  model + tools + task ──► result · every action audited │
╰──────────────────────────────────────────────────────╯
```

**The execution engine for autonomous agents.** ArcRun receives an [ArcLLM](../arcllm/) model, a set of tools, and a task — then loops until the task is done.

ArcRun is to agents what an engine is to a car. The car (your agent) decides where to go. The engine (ArcRun) makes it move.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CC BY 4.0](https://img.shields.io/badge/license-CC%20BY%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Lines of Code](https://img.shields.io/badge/lines-~1,200-brightgreen.svg)]()

---

## Why ArcRun

Most agent frameworks do too much. They own your prompts, your config, your session management, your UI. You end up fighting the framework.

ArcRun does one thing: **execute the loop**.

- **5 lines to run** — `await run(model, tools, prompt, task)`
- **~1,200 lines** — small enough for a model to reason about
- **Tamper-evident audit trail** — SHA-256 hash-chained events. Every action logged, every log verifiable, non-optional.
- **Deny-by-default sandbox** — Tool calls checked before execution. Container isolation available.
- **36 adversarial tests** — OWASP LLM Top 10 and Agentic AI Top 10 attack vectors validated.
- **Zero opinions** — No agents, no sessions, no config format, no UI.

You build the agent. ArcRun makes it move.

---

## Install

```bash
pip install arcrun
```

With container sandbox support:

```bash
pip install "arcrun[container]"
```

Requires Python 3.12+. Only dependency beyond ArcLLM is `jsonschema` for tool parameter validation.

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
print(result.events)        # full audit trail (hash-chained)
```

That's it. Five lines of setup. One call to `run()`.

---

## Core Concepts

### The Loop

```
run(model, tools, system_prompt, task)
  │
  ├── EMIT: loop.start (hash chain genesis)
  │
  ├── Strategy Selection (react / code)
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
  └── return LoopResult (with verifiable hash chain)
```

The model reasons, picks tools, observes results, repeats. ArcRun handles tool dispatch, sandbox checks, event emission, and message management. The model just sees `invoke()`.

### Tamper-Evident Event Chain

Every event is hash-chained using SHA-256. Each event contains a `sequence` number, `prev_hash`, and `event_hash`. The genesis event uses `"0" * 64` as its previous hash. Verify the integrity of any audit trail:

```python
from arcrun import verify_chain

result = await run(...)
verification = verify_chain(result.events)

print(verification.valid)           # True if chain is intact
print(verification.verified_count)  # Number of events verified
print(verification.first_invalid_index)  # None if valid
```

Events are immutable (`frozen=True` dataclass with `MappingProxyType` data). No post-emission tampering.

### Tools

Tools are what the model can call. You define them, ArcRun validates and dispatches.

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

Return a string. Raise an exception for errors (ArcRun catches it, emits `tool.error`, sends the error back to the model).

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

```python
# Real-time handler
def my_handler(event):
    print(f"[{event.type}] seq={event.sequence} hash={event.event_hash[:12]}")

result = await run(..., on_event=my_handler)

# Post-execution verification
verification = verify_chain(result.events)
assert verification.valid
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

### Container Sandbox

For maximum isolation, run agent-generated code inside Docker containers:

```python
from arcrun import make_contained_execute_tool

tool = make_contained_execute_tool(
    memory_limit="256m",
    cpu_period=100000,
    cpu_quota=50000,       # 50% of one CPU
    network_disabled=True,
    read_only=True,
    timeout_seconds=30,
)

result = await run(model=model, tools=[tool], ...)
```

Container sandbox provides:
- **Memory limits** — OOM kills prevent resource exhaustion
- **CPU quotas** — Prevents CPU monopolization
- **Network isolation** — No outbound connections from agent code
- **Read-only filesystem** — No persistent writes
- **Automatic cleanup** — Containers removed after execution

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

ArcRun supports multiple execution strategies. The model picks (or you constrain):

### ReAct (Default)

Reason -> Act -> Observe -> Repeat. The standard tool-calling loop. Uses whatever tools you pass in. Terminates on `end_turn` or `max_turns`.

```python
result = await run(
    ...,
    allowed_strategies=["react"],  # or omit — it's the default
)
```

### CodeExec

Model writes Python code. `make_execute_tool()` creates a sandboxed subprocess tool. Only available when you include the built-in execute tool.

```python
from arcrun import make_execute_tool

result = await run(
    model=model,
    tools=[*my_tools, make_execute_tool()],
    ...,
    allowed_strategies=["code"],
)
```

### Recursive (Planned)

Model decomposes tasks into sub-problems. `SpawnTool` will create isolated sub-loops with fresh context. Parent gets a compact result — never the child's full conversation. Not yet implemented.

### Strategy Selection

Allow multiple strategies and let the model choose based on the task:

```python
result = await run(
    ...,
    allowed_strategies=["react", "code"],
)
# result.strategy_used tells you which it picked
```

---

## Architecture

```
arcrun/
├── __init__.py            # Public API: run(), Tool, LoopResult, verify_chain, etc.
├── loop.py                # run() + run_async() + RunHandle
├── state.py               # RunState — internal state during execution
├── events.py              # EventBus + Event + hash chain + verify_chain()
├── sandbox.py             # Permission boundary
├── registry.py            # Dynamic tool registry
├── executor.py            # Shared tool execution pipeline
├── types.py               # Tool, LoopResult, SandboxConfig, ToolContext
├── _messages.py           # Message construction helpers
│
├── strategies/
│   ├── __init__.py        # Strategy interface + selection
│   ├── react.py           # ReAct loop
│   └── code.py            # CodeExec strategy
│
└── builtins/
    ├── execute.py         # Sandboxed Python execution
    └── contained_execute.py  # Docker-isolated execution
```

**Total: ~1,200 lines of Python.**

### Layer Separation

```
┌─────────────────────────────────────────────┐
│  YOUR AGENT (you build this)                │
│  System prompt, tool selection, sessions    │
│  Extension system, config, UI, memory       │
│  Passes tools + arcllm model into arcrun    │
├─────────────────────────────────────────────┤
│  arcrun (this package)                      │
│  Execution loop (ReAct / CodeExec)          │
│  Tool dispatch + validation                 │
│  Hash-chained event audit trail             │
│  Sandbox (permission + container)           │
│  Steering (mid-execution intervention)      │
├─────────────────────────────────────────────┤
│  arcllm                                     │
│  load_model("anthropic")                    │
│  await model.invoke(messages, tools=tools)  │
│  Provider abstraction, token tracking       │
│  Security, telemetry, retry, fallback       │
└─────────────────────────────────────────────┘
```

ArcRun calls `model.invoke()`. That's the only touchpoint with ArcLLM. ArcRun never calls `load_model()`, never configures providers, never handles API keys.

---

## Security

ArcRun is built for federal and enterprise deployment. Security is non-optional.

### Threat Model

Formal analysis covers:
- **OWASP Agentic AI (T1-T15)** — tool misuse, resource overload, RCE, agent poisoning
- **OWASP LLM Top 10 (2025)** — prompt injection, excessive agency, unbounded consumption
- **NIST SP 800-53** — 12 controls mapped directly to ArcRun features

### Defense Layers

| Layer | Mechanism |
|---|---|
| **Tool allowlist** | Only explicitly allowed tools can execute |
| **Param validation** | JSON Schema validation before every `execute()` |
| **Sandbox checker** | Caller-provided callback for granular permission logic |
| **Container isolation** | Docker-based execution with memory/CPU/network limits |
| **Hash-chained audit trail** | SHA-256 chain on every event — tamper-evident, verifiable |
| **Immutable events** | `frozen=True` dataclass with `MappingProxyType` data |
| **Tool timeouts** | Per-tool and global timeout enforcement |
| **Dynamic tool denial** | New tools added mid-execution are denied by default |
| **Cancel signal** | Tools receive cancellation signal for clean shutdown |

### Adversarial Test Coverage

36 tests across 8 categories validate resilience against real attack vectors:

| Category | Tests | OWASP Mapping |
|---|---|---|
| Prompt injection | 3 | LLM01, ASI01 |
| Path traversal | 4 | ASI05 |
| Steering injection | 3 | ASI01, ASI06 |
| Tool injection | 3 | ASI02, ASI04 |
| Resource exhaustion | 3 | LLM10, ASI08 |
| Spawn depth bomb | 4 | ASI08 |
| Event tampering | 8 | AU-9, AU-10 |
| Timing attacks | 8 | AU-8 |

### NIST SP 800-53 Coverage

| Control | Title | ArcRun Feature |
|---|---|---|
| AC-3 | Access Enforcement | Sandbox deny-by-default |
| AC-4 | Information Flow | Context transform isolation |
| AC-6 | Least Privilege | Explicit tool allowlist |
| AU-2 | Event Logging | Every action emits event |
| AU-3 | Audit Content | Events include timestamp, run_id, tool, args, duration |
| AU-8 | Timestamps | ISO 8601 on every event |
| AU-9 | Protection of Audit Info | SHA-256 hash chain, immutable events |
| AU-10 | Non-Repudiation | Hash chain verification via `verify_chain()` |
| AU-12 | Audit Generation | Non-optional emission |
| CM-7 | Least Functionality | Tools are opt-in |
| SC-28 | Protection at Rest | State dies when `run()` returns |
| SI-4 | System Monitoring | Events, tokens, cost tracking |
| SI-10 | Input Validation | JSON Schema on every tool call |
| SI-11 | Error Handling | Errors return to model as structured results |

---

## Development

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests
pytest -v
pytest --cov=arcrun
pytest tests/security/       # Adversarial tests

# Type checking
mypy src/arcrun

# Linting
ruff check src/arcrun
ruff format src/arcrun
```

### Quality Thresholds

| Metric | Target |
|---|---|
| Total lines | < 1,500 (~1,200 currently) |
| Test coverage | >= 80% |
| Cyclomatic complexity | <= 10 per function |
| Critical vulnerabilities | 0 |
| Type hints | Required on public API |
| Async-only | No sync wrappers in core |
| Adversarial tests | 36 passing |

---

## Roadmap

| Phase | Name | Goal | Status |
|---|---|---|---|
| 1 | Core Loop + ReAct | `run()` works end-to-end with events and sandbox | **Complete** |
| 2 | CodeExec | Model writes + executes Python in sandboxed subprocess | **Complete** |
| 3 | Recursive | Task decomposition via spawn with isolated context | Planned |
| 4 | Hardening | Container sandbox, event integrity, adversarial testing, NIST docs | **Complete** |
| 5 | RLM | Recursive Language Models for near-infinite context processing | Research |

---

## License

This project is licensed under the [Creative Commons Attribution 4.0 International License (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

You are free to use, share, and adapt this software, provided you give appropriate credit.

Copyright (c) 2025-2026 BlackArc Systems.
