# arcrun — Product Context

## What arcrun Is

arcrun is the execution engine for autonomous agents. It receives an arcllm model, a set of tools, and a task — then runs until the task is done.

**arcrun is to agents what an engine is to a car.** The car (your agent) decides where to go. The engine (arcrun) makes it move.

## What arcrun Is NOT

- Not an agent framework (no agent definitions, no sessions, no config format)
- Not a model layer (arcllm handles that)
- Not a UI or CLI
- Not an extension system or plugin architecture
- Not a memory/RAG system

## Vision

An extremely simple execution loop that uses arcllm and can handle simple tasks, complex tasks, and use the appropriate resources for each — routing, loop, sub-agent, RLM strategies — while maintaining full observability and federal-grade security.

## Design Priorities (Ordered)

1. **Simple and clear** — Core must be small enough for a model to reason about. Under 1,000 lines total.
2. **Security first** — Every action auditable. Deny-by-default. Log everything. Built for federal and enterprise.
3. **Modular and extensible** — Tiny core. Hooks for the layer above. No opinions beyond the loop. Add what you need when you need it.

## Target Personas

### Primary: Agent Builder

- Building autonomous agents on top of arcllm
- Needs a reliable execution loop they don't have to write
- Wants to pass in their model + tools + task and get results
- Cares about: simplicity, debuggability, cost control, security

### Secondary: Enterprise/Federal Deployer

- Running thousands of concurrent agents in production
- Needs audit trails, sandbox enforcement, cost ceilings
- Cares about: NIST compliance, observability, rate control, permission boundaries

### Tertiary: Framework Author

- Building higher-level agent frameworks (orchestrators, multi-agent systems)
- Needs arcrun as a primitive they compose into larger systems
- Cares about: clean interfaces, event streams, spawn isolation, strategy extensibility

## Success Metrics

| Metric | Target |
|--------|--------|
| Lines of code | Under 1,000 |
| Time to first working loop | 5 lines of code |
| Event coverage | 100% of actions emit events |
| Sandbox coverage | Every tool call checked |
| Provider compatibility | Works with any arcllm provider |
| Dependencies | Zero beyond arcllm (which brings pydantic + httpx) |

## Layer Map

```
YOUR AGENT (you build this)
  - System prompt, tool selection, session management
  - Extension system, config, UI
  - Passes tools + arcllm model into arcrun
        |
arcrun (this package)
  - Execution loop (ReAct / CodeExec / Recursive / RLM)
  - Tool execution + validation
  - Event emission (every action, always)
  - Sandbox (permission boundary)
  - Spawn (context isolation primitive)
        |
arcllm (existing package)
  - load_model("anthropic")
  - await model.invoke(messages, tools=tools)
  - Provider abstraction, token tracking
  - Security, telemetry, audit, retry, fallback modules
```

## Constraints

- Python 3.11+ (extends arcllm)
- Async default: `await run(...)`
- arcllm is the only dependency
- Caller configures model; loop just calls `model.invoke()`
- Caller passes tools in; no discovery, no registration
- Events emit for every action; non-optional
- Sandbox checks every tool call; deny-by-default
- All decisions logged in `.claude/decision-log.md`
