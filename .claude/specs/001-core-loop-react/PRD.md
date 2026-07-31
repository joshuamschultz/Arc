# PRD: Core Loop + ReAct (001)

## Problem Statement

Agent builders need a reliable async execution loop that takes an arcllm model + tools + task and runs until done. Without arcrun, every agent author reimplements: tool dispatch, event emission, sandbox enforcement, message management, turn counting, and error handling. This is the foundational layer that makes agents move.

## User Stories

### US-1: Simple Agent Execution
**As** an agent builder, **I want** to call `await run(model, tools, prompt, task)` and get a result, **so that** I don't have to write my own execution loop.

### US-2: Full Observability
**As** an enterprise deployer, **I want** every action to emit an event (tool calls, denials, LLM calls, turns), **so that** I have a complete audit trail for compliance.

### US-3: Permission Enforcement
**As** a security engineer, **I want** a sandbox that only allows explicitly whitelisted tools, **so that** agents can't execute unauthorized actions.

### US-4: Mid-Execution Steering
**As** a human supervisor, **I want** to inject new instructions while the agent is working (steer to interrupt, followUp to queue), **so that** I can course-correct without restarting.

### US-5: Dynamic Tool Management
**As** a framework author, **I want** to add/remove tools during execution, **so that** agents can self-extend (load MCP servers, write custom tools) mid-task.

### US-6: Context Management
**As** an agent running long tasks, **I want** a transform_context hook to prune messages before each LLM call, **so that** I don't hit context limits.

## Requirements

### R-1: Entry Points
- `run(model, tools, system_prompt, task, **options) -> LoopResult` — blocking
- `run_async(model, tools, system_prompt, task, **options) -> RunHandle` — non-blocking
- RunHandle exposes: `steer()`, `follow_up()`, `cancel()`, `result()`, read-only `state`

### R-2: Tool System
- Tool is a dataclass: name, description, input_schema (JSON Schema), execute (async callable)
- Tool.execute signature: `async (params: dict, ctx: ToolContext) -> str`
- ToolContext: run_id, tool_call_id, turn_number, event_bus, cancelled (asyncio.Event)
- Factory functions for complex/stateful tools (closures, not subclassing)
- Async only — no sync support
- Param validation via jsonschema before execute()
- Validation errors returned to model as tool results (model can retry)

### R-3: Event System
- Generic Event dataclass: type (str), timestamp (float), run_id (str), data (dict)
- EventBus: emit(), events list, on_event callback
- Synchronous emission — handler called inline
- ~12 event types: loop.start, loop.complete, loop.max_turns, strategy.selected, turn.start, turn.end, llm.call, tool.start, tool.end, tool.denied, tool.error, tool.registered, tool.removed
- 100% action coverage — every action emits

### R-4: Sandbox
- SandboxConfig: allowed_tools (list | None), check (async callback | None)
- Allowlist model: only tools in allowed_tools can execute
- allowed_tools=None → no sandbox (all allowed)
- Optional check(tool_name, params) -> (bool, reason) for granular control
- Denied tools: error returned to model + tool.denied event emitted
- Dynamic tools denied by default (not in allowlist)

### R-5: Dynamic Tool Registry
- ToolRegistry: add(), remove(), get(), list_schemas(), names()
- Strategies read registry each turn (tools can change between turns)
- add/remove emit events (tool.registered, tool.removed)
- New tools not in sandbox allowlist → automatically denied

### R-6: ReAct Strategy
- Default execution strategy
- Loop: model.invoke() → process tool_calls → append results → repeat
- Stop conditions: end_turn, max_turns (strategy-enforced)
- Text + tool_calls in response → both preserved in assistant message
- Between tool executions: check steer queue, skip remaining if steered
- At end_turn: check followUp queue, inject if present, continue
- transform_context hook called before each model.invoke()
- No system prompt augmentation (ReAct is the default behavior)

### R-7: Steering
- steer(message): inject after current tool, skip remaining, continue loop
- follow_up(message): queue, inject at end_turn before returning
- cancel(): set cancel signal on ToolContext, return partial LoopResult
- Available via RunHandle from run_async()

### R-8: Error Handling
- Tool exceptions: caught, emitted as tool.error, error string returned to model
- Model API errors: bubble up as exceptions to caller (arcllm handles retries)
- Sandbox check callback errors: treated as denial (fail-safe)

### R-9: LoopResult
- content: str | None (final response text)
- turns: int
- tool_calls_made: int
- tokens_used: dict (input, output, total)
- strategy_used: str
- cost_usd: float
- events: list[Event]

## Non-Requirements (Out of Scope)

- CodeExec strategy (Phase 2)
- Recursive strategy / SpawnTool (Phase 3)
- Container sandbox (Phase 4)
- Event integrity / checksums (Phase 4)
- Streaming pass-through (requires arcllm streaming support)
- NIST compliance documentation (Phase 4)
- RLM integration (Phase 5)

## Dependencies

- arcllm (model.invoke(), Message, LLMResponse, ToolCall, etc.)
- jsonschema (param validation — overrides PRD zero-dep constraint per DECISION-022)

## Success Criteria

1. `await run()` works in 5 lines with a mock arcllm model
2. ReAct loop calls model.invoke(messages, tools=tools) correctly
3. Tool results flow back into messages for next turn
4. Every action emits an event
5. Sandbox denials work and emit events
6. Dynamic tool registry add/remove works mid-execution
7. Steer interrupts the loop correctly
8. FollowUp injects at end_turn correctly
9. Cancel sets signal and returns partial result
10. Under ~610 lines (original 500 + steering/registry additions)
