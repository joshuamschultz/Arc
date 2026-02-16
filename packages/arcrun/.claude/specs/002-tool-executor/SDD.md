# SDD: Tool Executor Extraction

## Architecture

```
Strategy (react.py, future code.py, recursive.py)
    │
    │  for tc in response.tool_calls:
    │      if cancelled or steered: skip
    │      result, ok = await execute_tool_call(tc, state, sandbox)
    │
    ▼
executor.py  ←── NEW: shared tool execution pipeline
    │
    ├── EventBus (emit tool.start, tool.end, tool.error)
    ├── Sandbox (check permissions)
    ├── ToolRegistry (lookup tool definition)
    ├── jsonschema (validate arguments)
    └── Tool.execute (run the tool)
```

## Component: executor.py

**File:** `src/arcrun/executor.py`
**Estimated lines:** ~45

### Function Signature

```python
async def execute_tool_call(
    tc: Any,            # arcllm ToolCall: .id, .name, .arguments
    state: RunState,
    sandbox: Sandbox,
) -> tuple[Message, bool]:
```

Uses `Any` for `tc` parameter to avoid importing arcllm's ToolCall type directly — the function only accesses `.id`, `.name`, `.arguments` attributes (duck typing). This matches the existing pattern in react.py.

### Pipeline (10 steps, extracted from react.py:88-139)

```python
async def execute_tool_call(tc, state, sandbox):
    bus = state.event_bus

    # 1. Emit start
    bus.emit("tool.start", {"name": tc.name, "arguments": tc.arguments})

    # 2. Sandbox check
    allowed, reason = await sandbox.check(tc.name, tc.arguments)
    if not allowed:
        return tool_result(tc.id, f"Error: tool denied — {reason}"), False

    # 3. Registry lookup
    tool_def = state.registry.get(tc.name)
    if tool_def is None:
        return tool_result(tc.id, f"Error: tool '{tc.name}' not found"), False

    # 4. Schema validation
    try:
        jsonschema.validate(tc.arguments, tool_def.input_schema)
    except jsonschema.ValidationError as ve:
        return tool_result(tc.id, f"Error: invalid params — {ve.message}"), False

    # 5. Build context
    ctx = ToolContext(
        run_id=state.run_id,
        tool_call_id=tc.id,
        turn_number=state.turn_count + 1,
        event_bus=bus,
        cancelled=state.cancel_event,
    )

    # 6-7. Execute with timing + error handling
    tool_start = time.time()
    try:
        result = await tool_def.execute(tc.arguments, ctx)
    except Exception as exc:
        bus.emit("tool.error", {"name": tc.name, "error": str(exc)})
        return tool_result(tc.id, f"Error: {exc}"), False

    # 8. Emit end
    duration_ms = (time.time() - tool_start) * 1000
    bus.emit("tool.end", {
        "name": tc.name,
        "result_length": len(result),
        "duration_ms": duration_ms,
    })

    # 9. Track
    state.tool_calls_made += 1

    # 10. Return
    return tool_result(tc.id, result), True
```

### Imports

```python
from __future__ import annotations
import time
from typing import Any
import jsonschema
from arcrun._messages import tool_result
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import ToolContext
```

## Modification: react.py

### Before (lines 88-139, ~50 lines)
Inline tool execution with sandbox, validation, execute, events, error handling.

### After (~15 lines)
```python
from arcrun.executor import execute_tool_call

# Process tool calls
tool_results: list[Any] = []
steered = False
for tc in response.tool_calls:
    if steered or state.cancel_event.is_set():
        tool_results.append(tool_result(tc.id, "operation cancelled: steered"))
        continue

    result_msg, _ok = await execute_tool_call(tc, state, sandbox)
    tool_results.append(result_msg)

    if not state.steer_queue.empty():
        steer_msg = state.steer_queue.get_nowait()
        state.messages.append(user_message(steer_msg))
        steered = True
```

### Removed from react.py
- `import jsonschema`
- All inline sandbox check, registry lookup, validation, execute, timing, error handling code
- ToolContext construction

### Kept in react.py
- Cancel/steer checks (control flow)
- tool_results list assembly
- Message appending to state.messages

## Testing

### New: test_executor.py (~90 lines)

| Test | Scenario | Asserts |
|------|----------|---------|
| test_happy_path | Tool executes successfully | ok=True, tool_calls_made=1, result contains output |
| test_sandbox_denied | Sandbox rejects | ok=False, tool_calls_made=0 |
| test_tool_not_found | Tool name not in registry | ok=False, tool_calls_made=0 |
| test_schema_validation_failure | Invalid arguments | ok=False, tool_calls_made=0 |
| test_tool_exception | Tool raises RuntimeError | ok=False, tool_calls_made=0, tool.error event emitted |
| test_events_emitted_on_success | Happy path events | tool.start and tool.end in event list |
| test_tool_end_has_duration | Duration tracking | tool.end event has duration_ms and result_length |

### Existing: test_react.py (unchanged)
All 10 tests continue to pass — behavior is identical.

## Line Impact

| File | Before | After | Delta |
|------|--------|-------|-------|
| executor.py | 0 | ~45 | +45 |
| react.py | 178 | ~145 | -33 |
| **Net** | | | **+12** |

Total project: 623 → ~635 lines. Well under 1,000.

## Future Enhancement Points

Once extracted, executor.py becomes the single place to add:
- Tool-level timeout (`asyncio.wait_for`)
- Result truncation (after execute, before return)
- Per-tool rate limiting (counter check before execute)
- Output sanitization (after execute, before return)

Each is 5-10 lines added to one file, no strategy changes needed.
