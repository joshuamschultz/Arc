# arcrun

Async execution engine for autonomous agents. Receives an arcllm model, a set of tools, and a task — then runs the ReAct loop until the task is complete.

## Layer position

arcrun depends on arcllm and arctrust. arcagent, arcgateway, and arccli depend on arcrun. arcrun never imports from them.

## What it provides

- `run`, `run_async`, `RunHandle` — synchronous and async task execution; `RunHandle` supports mid-execution steering (inject message), follow-up injection, and cooperative cancellation via `asyncio.Event`
- `run_stream` — streaming variant; yields `StreamEvent`, `TokenEvent`, `ToolStartEvent`, `ToolEndEvent`, `TurnEndEvent` for real-time output
- `make_spawn_tool` — factory for a sandboxed subprocess tool that spawns child arcrun loops; enables parallel tool dispatch (SPEC-017)
- `LoopResult` — result of a completed run: content, turns, tool_calls_made, tokens_used, cost_usd, strategy_used, events
- `Tool`, `ToolContext`, `ToolRegistry` — tool definition and registry; deny-by-default; tools must be explicitly registered; JSON Schema parameter validation on every call
- `SandboxConfig`, `make_execute_tool`, `SandboxError`, `SandboxOOMError`, `SandboxRuntimeError`, `SandboxTimeoutError`, `SandboxUnavailableError` — sandboxed Python execution; stripped environment, process group isolation, two-phase timeout (SIGTERM + SIGKILL), 64KB output cap
- `Event`, `EventBus`, `GENESIS_PREV_HASH`, `ChainVerificationResult`, `verify_chain` — structured event bus; every tool call, LLM invocation, and turn boundary emits an event; events are hash-chained for tamper detection
- `Strategy`, `get_strategy_prompts` — pluggable execution strategies; currently `react` and `code`

## Quick example

```python
from arcllm import load_model
from arcrun import run, Tool, ToolContext

async def read_file(params: dict, ctx: ToolContext) -> str:
    return open(params["path"]).read()

model = load_model("anthropic")
result = await run(
    model=model,
    tools=[Tool(
        name="read_file",
        description="Read a file from the workspace.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        execute=read_file,
    )],
    task="Read /workspace/report.txt and summarize it.",
    max_turns=10,
)
print(result.content)
print(f"{result.turns} turns, ${result.cost_usd:.4f}")
```

## Architecture references

- SPEC-017: Arc Core Hardening — parallel tool dispatch, `task_complete` signal, 5-layer policy pipeline
- ADR-019: Four Pillars Universal — every arcrun call pair-signs via arctrust; all operations audited

## Status

- Tests: 513 (run with `uv run --no-sync pytest packages/arcrun/tests`)
- Coverage: spawn module 92%; overall high
- ruff + mypy --strict: clean
