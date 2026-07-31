# arcrun - Execution Loop

> **Building with Arc**  ·  Build  ·  page 11 of 27  
> **For** Engineers writing code against Arc  
> [← arcllm](arcllm.md)  ·  [Docs home](../../README.md)  ·  [arctrust →](arctrust.md)

---

## Overview

`arcrun` implements the **think-act-observe loop** for Arc agents:
- **Turn execution** - Think, act, observe cycle
- **Sandbox management** - Secure code execution
- **Parallel dispatch** - Multiple tool calls concurrently
- **Budget management** - Token and turn limits

```mermaid
flowchart LR
    classDef loop fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef sandbox fill:#002550,stroke:#001A38,color:#FFFFFF

    Think[Think<br/>LLM Call]:::loop --> Act[Act<br/>Tool Calls]:::loop
    Act --> Observe[Observe<br/>Tool Results]:::loop
    Observe --> Think
    
    Act --> Sandbox[Sandbox<br/>Execution]:::sandbox
```

---

## Think-Act-Observe Loop

### Loop Diagram

```mermaid
flowchart TD
    classDef step fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef check fill:#002550,stroke:#001A38,color:#FFFFFF

    Start[Task Input]:::step --> Build[Build Prompt]:::step
    Build --> LLM[Call LLM]:::step
    LLM --> Parse[Parse Response]:::check
    Parse -->|Has Tools| Auth[Authorize]:::check
    Parse -->|No Tools| Return[Return Result]:::step
    Auth --> Exec[Execute Tools]:::step
    Exec --> Observe[Observe Results]:::step
    Observe --> Build
```

### Implementation

```python
from arcrun import run
from arcagent.core.agent import ArcAgent

async def run(agent: ArcAgent, task: str, session_id: str = None) -> Result:
    """Execute a single turn of the agent loop."""
    
    # Build prompt
    prompt = agent.render_prompt(task, session_id)
    
    # Call LLM
    response = await agent.llm.chat(prompt)
    
    # Check for tool calls
    if not response.tool_calls:
        return Result(content=response.content, turns=1, ...)
    
    # Authorize and execute
    results = []
    for call in response.tool_calls:
        if await agent.authorize(call):
            result = await agent.execute_tool(call)
            results.append(result)
    
    # Continue loop with observations
    return await run(agent, task, session_id, observations=results)
```

---

## Sandbox Execution

### Sandbox Types

```mermaid
flowchart TB
    classDef sandbox fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef tier fill:#0073FE,stroke:#0055BC,color:#FFFFFF

    Personal[Personal<br/>Docker<br/>(relax to local)]:::tier
    Enterprise[Enterprise<br/>Docker]:::tier
    Federal[Federal<br/>Firecracker VM]:::tier
    
    Personal --> Sandbox[arcrun Sandbox]:::sandbox
    Enterprise --> Sandbox
    Federal --> Sandbox
```

### Docker Sandbox

```python
from arcrun.sandbox import Sandbox

sandbox = Sandbox(
    memory_limit="512m",
    cpus=1.0,
    timeout_seconds=60,
    network_disabled=True
)

result = sandbox.execute("""
import pandas as pd
df = pd.read_csv('data.csv')
print(df.describe())
""")

print(result.stdout)
print(result.exit_code)
```

### Firecracker Sandbox

```python
from arcrun.sandbox import Sandbox

sandbox = FirecrackerSandbox(
    vcpu_count=2,
    mem_size_mib=1024,
    timeout_seconds=300
)

result = sandbox.execute(code)
```

### Sandbox Configuration

```toml
[security.sandbox]
backend = "docker"  # docker | firecracker
memory_limit = "512m"
cpus = 1.0
timeout_seconds = 60
network_enabled = false
```

---

## Parallel Dispatch

### Concurrent Tool Execution

```python
from arcrun import parallel_dispatch

# Execute multiple tools concurrently
results = await parallel_dispatch(
    agent,
    [
        ToolCall(name="read_file", arguments={"path": "a.csv"}),
        ToolCall(name="read_file", arguments={"path": "b.csv"}),
        ToolCall(name="read_file", arguments={"path": "c.csv"}),
    ],
    max_concurrency=3
)
```

### Dispatch Strategies

| Strategy | Use Case |
|----------|----------|
| `sequential` | Single tool, ordered |
| `parallel` | Multiple independent tools |
| `batch` | Similar tools batched |
| `priority` | High-priority first |

---

## Budget Management

### Turn Budget

```python
from arcrun import LoopResult

budget = LoopResult(max_turns=50, max_tokens=50000)

# Check budget
if budget.remaining_turns() > 0:
    result = await run(agent, task)
    budget.consume(response.usage.total_tokens)
```

### Token Budget

```python
budget = TokenBudget(
    input_limit=100000,
    output_limit=20000,
    total_limit=120000
)

# Truncate context if needed
if budget.would_exceed(new_messages):
    messages = budget.truncate(messages)
```

---

## API Reference

### Functions

```python
async def run(
    agent: ArcAgent,
    task: str,
    session_id: str = None,
    max_turns: int = 50,
    observations: list = None
) -> Result:
    """Execute a single turn of the agent loop."""

def parse_tool_calls(response: LLMResponse) -> list[ToolCall]:
    """Parse tool calls from LLM response."""

async def execute_tool(agent: ArcAgent, call: ToolCall) -> ToolResult:
    """Execute a single tool call."""
```

### Classes

```python
class Sandbox(Protocol):
    def execute(
        self,
        code: str,
        timeout: int = 60,
        memory_limit: str = "512m"
    ) -> ExecutionResult: ...

class Sandbox(Sandbox):
    def __init__(
        self,
        memory_limit: str = "512m",
        cpus: float = 1.0,
        timeout_seconds: int = 60,
        network_disabled: bool = True
    ): ...

class FirecrackerSandbox(Sandbox):
    def __init__(
        self,
        vcpu_count: int = 2,
        mem_size_mib: int = 1024,
        timeout_seconds: int = 300
    ): ...

class ExecutionResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
```

---

## Error Handling

```python
from arcrun import (
    TurnBudgetExceeded,
    SandboxError,
    ToolExecutionError
)

try:
    result = await run(agent, task)
except TurnBudgetExceeded:
    # Handle budget exceeded
    pass
except SandboxError as e:
    # Handle sandbox failure
    pass
```

---

## Next Steps

- [API Reference](../../reference/api.md) - Complete API documentation
- [Package Index](../package-index.md) - All Arc packages

---

## Verified public surface

> Introspected from the installed package on the current commit. Every name
> below is importable exactly as shown; full signatures are in the
> [API reference](../../reference/api.md#arcrun).

### Classes

| Class | Purpose |
|---|---|
| `CapabilityProvider` | The contract arcrun's loop runs against (ADR-023). |
| `CapabilityResult` | Outcome of a capability invocation. |
| `CapabilitySpec` | Lean advertise unit — all that enters the model's tool list. |
| `ChainVerificationResult` | Result of verify_chain(). |
| `Event` | Immutable event with hash chain fields. |
| `EventBus` | Emits events, collects them, optionally calls handler. Thread-safe. |
| `LoopCheckpoint` | Immutable snapshot of resumable loop state at a turn boundary. |
| `LoopResult` | Returned by run(). |
| `RunHandle` | Control interface for a running execution loop. |
| `RunResult` | Final result of a streamed run, reconstructed by ``collect()``. |
| `SandboxConfig` | Permission boundary. allowed_tools=None means all allowed. |
| `SandboxError` | Base for all container sandbox errors. |
| `SandboxOOMError` | Container killed by OOM (exit code 137). |
| `SandboxRuntimeError` | Script execution failed. |
| `SandboxTimeoutError` | Container exceeded timeout. |
| `SandboxUnavailableError` | No container runtime found. |
| `StaticProvider` | Adapt a fixed ``list[Tool]`` to the CapabilityProvider contract. |
| `Strategy` | Base class for execution strategies. |
| `StreamEvent` | Base class for all stream events. |
| `TokenEvent` | A chunk of text from the model response. |
| `Tool` | A tool the model can call. |
| `ToolContext` | Passed to Tool.execute. |
| `ToolEndEvent` | Emitted when a tool call completes. |
| `ToolRegistry` | Tool collection, mutable until frozen for the run. |
| `ToolStartEvent` | Emitted when the model calls a tool. |
| `TurnEndEvent` | Emitted exactly once, as the final event in the stream. |

### Functions

| Function | Signature |
|---|---|
| `apply_checkpoint` | `(state: 'RunState', cp: 'LoopCheckpoint') -> 'RunState'` |
| `async collect` | `(stream: 'AsyncIterator[StreamEvent]') -> 'RunResult'` |
| `detached_context` | `() -> 'ToolContext'` |
| `get_strategy_prompts` | `(*, allowed_strategies: 'list[str] \| None' = None, tool_names: 'list[str] \| None' = None, resolve: '` |
| `make_execute_tool` | `(*, timeout_seconds: 'float' = 30, max_output_bytes: 'int' = 65536, extra_env: 'dict[str, str] \| Non` |
| `provider_tools` | `(provider: 'CapabilityProvider', *, caller_did: 'str') -> 'list[Tool]'` |
| `async run` | `(model: 'Any', capabilities: 'CapabilityProvider', system_prompt: 'SystemPrompt', task: 'str', *, me` |
| `async run_async` | `(model: 'Any', capabilities: 'CapabilityProvider', system_prompt: 'SystemPrompt', task: 'str', *, me` |
| `async run_shell` | `(command: 'str', *, tier: 'str', workspace: 'Path', readonly_subpaths: 'list[Path] \| None' = None, c` |
| `async run_stream` | `(*, model: 'Any', capabilities: 'CapabilityProvider', system_prompt: 'SystemPrompt', task: 'str', me` |
| `stream_llm_response` | `(*, model: 'Any', messages: 'list[Any]', tools: 'list[Tool] \| None' = None, **invoke_kwargs: 'Any') ` |
| `system_messages` | `(prompt: 'SystemPrompt') -> 'list[Message]'` |
| `to_checkpoint` | `(state: 'RunState') -> 'LoopCheckpoint'` |
| `verify_chain` | `(events: 'list[Event]') -> 'ChainVerificationResult'` |

