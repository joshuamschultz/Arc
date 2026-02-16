# arcrun — Technical Context

## Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Language | Python 3.11+ | Matches arcllm |
| Async | asyncio | arcllm is async (`await model.invoke()`) |
| Types | dataclasses | Lightweight; pydantic available via arcllm but not required for loop internals |
| LLM calls | arcllm | `load_model()` + `model.invoke(messages, tools=tools)` |
| Distribution | `pip install arc` (umbrella) | Single install covers arcllm + arcrun |
| Testing | pytest + pytest-asyncio | Matches arcllm test setup |

## arcllm Integration Points

arcrun's only touchpoint with arcllm is `model.invoke()`. It never calls `load_model()`, never configures providers, never handles API keys.

### Types arcrun Consumes from arcllm

```python
from arcllm import (
    Message,          # Input messages: role + content
    Tool as LLMTool,  # Tool definition sent to LLM (name, description, parameters)
    LLMResponse,      # Normalized response: .content, .tool_calls, .stop_reason, .usage
    ToolCall,         # Parsed tool call: .id, .name, .arguments (dict)
    TextBlock,        # Content block for text
    ToolUseBlock,     # Content block for tool use
    ToolResultBlock,  # Content block for tool result
    Usage,            # Token accounting
    StopReason,       # "end_turn" | "tool_use" | "max_tokens" | ...
)
```

### Message Format for Tool Results

arcllm expects tool results in this exact format:

```python
# After model returns tool_calls:
assistant_content = []
if response.content:
    assistant_content.append(TextBlock(text=response.content))
for tc in response.tool_calls:
    assistant_content.append(ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments))
messages.append(Message(role="assistant", content=assistant_content))

# Tool results:
for tc in response.tool_calls:
    result = execute_tool(tc.name, tc.arguments)
    messages.append(Message(
        role="tool",
        content=[ToolResultBlock(tool_use_id=tc.id, content=result)],
    ))
```

### arcllm Security/Observability Modules (Already Built)

arcllm has opt-in modules that wrap `model.invoke()`. arcrun inherits these automatically because the caller passes in a model that may already have modules enabled:

| Module | What It Does | arcrun Integration |
|--------|-------------|---------------------|
| Retry | Exponential backoff on 429/500/503 | Transparent — wraps invoke() |
| Fallback | Provider chain on failure | Transparent |
| Rate Limit | Token-bucket throttling | Transparent |
| Telemetry | Timing, token counts, cost per call | Transparent; arcrun also tracks at loop level |
| Audit | Structured compliance logging | Transparent; arcrun adds loop-level audit events |
| Security | PII redaction + HMAC signing | Transparent |
| OTel | Distributed tracing (GenAI conventions) | arcrun creates child spans that nest under arcllm spans |

**Key insight:** arcrun's event system and arcllm's module system are complementary, not competing. arcllm modules handle per-invoke observability. arcrun events handle loop-level observability (turns, tool dispatch, sandbox, spawn).

## Execution Strategies

| Strategy | Description | Requires |
|----------|-------------|----------|
| ReAct | Reason + Act loop. Model reasons, picks tool, observes, repeats. | Default — no special tools needed |
| CodeExec | Model writes Python, ExecuteTool runs it in sandbox | Caller includes `ExecuteTool()` |
| Recursive | Model decomposes task, SpawnTool creates sub-loops | Caller includes `SpawnTool()` |
| RLM | Context as variables in REPL, recursive self-calls for long context | Future — Phase 5 |

## Key Patterns

### Abstraction: Tool Interface

arcrun defines its own `Tool` that wraps an async execute function. Separate from arcllm's `Tool` (which is the LLM-facing schema). arcrun's Tool combines the schema + execution:

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict          # JSON Schema
    execute: async (params, ctx) -> str

    def to_schema(self) -> arcllm.Tool:
        """Convert to arcllm Tool for model.invoke()"""
```

### Abstraction: Event Bus

Synchronous emission. Every action. Handler called inline. Events collected into list returned in LoopResult.

### Abstraction: Sandbox

Permission boundary. Checks before every tool execution. Deny-by-default. Returns reason string for denied actions (fed back to model as tool error).

### Abstraction: Dynamic Tool Registry

Mutable tool collection the loop reads each turn. Tools can be added/removed/replaced during execution. Enables self-extending agents and MCP server loading mid-task.

```python
class ToolRegistry:
    def add(self, tool: Tool) -> None: ...
    def remove(self, name: str) -> None: ...
    def get(self, name: str) -> Tool | None: ...
    def list_schemas(self) -> list[arcllm.Tool]: ...
```

### Hook: Context Transform

Caller-provided callback to prune/compact messages before each LLM call. Prevents context overflow in long-running loops.

```python
result = await run(
    ...,
    transform_context=my_pruner,  # async (messages) -> messages
)
```

### Hook: Steering (Mid-Execution Interrupt)

Inject new instructions while tools are executing. When steering activates, remaining tool executions error out and the steering message enters the context.

```python
handle = await run_async(...)  # non-blocking start
await handle.steer("Stop what you're doing, prioritize X instead")
result = await handle.result()
```

### Streaming Support

Pass-through streaming from arcllm when the model supports it. Callers receive response chunks in real-time via the event bus or a dedicated stream callback.

## Quality Thresholds

| Metric | Threshold |
|--------|-----------|
| Total lines | < 1,000 |
| Test coverage | >= 80% |
| Cyclomatic complexity | <= 10 per function |
| Zero critical vulnerabilities | Enforced |
| Type hints | Required on public API |
| Async-only | No sync wrappers in core |

## Development Commands

```bash
# Tests
pytest -v
pytest --cov=arcrun

# Type checking
mypy src/arcrun

# Linting
ruff check src/arcrun
ruff format src/arcrun
```

## Decision Log

All architectural decisions pushed to user and logged in `.claude/decision-log.md`. Format:

```markdown
## DECISION-XXX: [Title]
**Date:** YYYY-MM-DD
**Context:** Why this decision was needed
**Options:** What was considered
**Decision:** What was chosen
**Reasoning:** Why
**Status:** Accepted | Superseded by DECISION-YYY
```
