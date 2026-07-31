# SDD: Recursive Agent Spawning

## Architecture

Spawn is a **tool, not a strategy**. The react loop gains a built-in `spawn_task` tool that starts a child `run()`. No new strategies, no new abstractions. A child is just another `run()` call with `depth + 1`.

```
arcrun/
    builtins/
        __init__.py      # Add make_spawn_tool export
        execute.py       # Existing (unchanged)
        spawn.py         # NEW: spawn_task tool factory
    loop.py              # Add depth/max_depth params + spawn injection
    state.py             # Add flat fields
    strategies/
        react.py         # Parallel tool execution for spawns
    types.py             # Unchanged
    __init__.py          # Add make_spawn_tool export
```

## Components

### 1. Spawn Tool: `arcrun/builtins/spawn.py`

Factory function `make_spawn_tool()` that creates a `Tool` for spawning child runs. Follows the exact pattern of `make_execute_tool()`.

**Factory signature:**
```python
def make_spawn_tool(
    *,
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    state: RunState,
    sandbox: SandboxConfig | None = None,
    allowed_strategies: list[str] | None = None,
) -> Tool:
```

The factory captures parent context via closure. The tool's `execute` function:

1. Validates `depth < max_depth` (reject with error if at limit)
2. Resolves tool subset (filter parent tools by names if `tools` param provided)
3. Passes `on_event` callback that bubbles child events to parent bus with `child.{child_run_id}.` prefix
4. Calls `arcrun.loop.run()` with:
   - Same `model` (inherited)
   - Resolved tools (subset or all parent tools, minus `spawn_task` if at `max_depth - 1`)
   - Child's `system_prompt` (from tool args, or parent's)
   - Child's `task` (from tool args)
   - `depth=parent_depth + 1`, `max_depth=parent_max_depth`
5. Returns `LoopResult.content` or `"(no content)"` as the tool result string
6. On any exception: returns `"Error: {type}: {message}"` (same pattern as `execute_tool_call`)

**Tool schema:**
```json
{
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The task for the child agent to accomplish"
        },
        "system_prompt": {
            "type": "string",
            "description": "Optional system prompt to specialize the child's role. If omitted, inherits parent's."
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of tool names the child can use. If omitted, inherits all parent tools."
        }
    },
    "required": ["task"]
}
```

### 2. State Changes: `arcrun/state.py`

Add flat fields to `RunState`:

```python
depth: int = 0
max_depth: int = 3
parent_run_id: str = ""
token_budget: int | None = None    # Observability only (v1)
cost_budget: float | None = None   # Observability only (v1)
```

### 3. Loop Changes: `arcrun/loop.py`

Add parameters to `run()` and `run_async()`:

```python
async def run(
    model, tools, system_prompt, task,
    *,
    # ... existing params ...
    depth: int = 0,
    max_depth: int = 3,
) -> LoopResult:
```

In `_build_state()`:
- Pass `depth` and `max_depth` to `RunState`

After state is built, **inject `spawn_task` into the tool registry** if `depth < max_depth`:
- Call `make_spawn_tool(model=model, tools=tools, state=state, ...)` to create the tool
- Add it to `state.registry` via `registry.add(spawn_tool)`
- This makes `spawn_task` available to the model alongside user-provided tools

If `depth >= max_depth`, do NOT inject `spawn_task` — the model won't see it and can't attempt to spawn.

### 4. Event Bubbling

The spawn tool uses `run()`'s existing `on_event` parameter to bubble child events to the parent bus. No new EventBus plumbing needed.

```python
def _make_bubble_handler(child_run_id: str, parent_bus: EventBus) -> Callable:
    """Create on_event callback that bubbles child events to parent bus."""
    def handler(event: Event) -> None:
        parent_bus.emit(
            f"child.{child_run_id}.{event.type}",
            {**event.data, "child_run_id": child_run_id},
        )
    return handler
```

The spawn tool passes this handler as `on_event` to the child `run()` call. The child's EventBus calls it on every emit (events.py:36-40), which creates a new event on the parent bus with the `child.` prefix.

### 5. Parallel Tool Execution: `arcrun/strategies/react.py`

Currently, tool calls execute sequentially in a for-loop (lines 100-116). For parallel spawning, use index-based mapping to preserve original tool call order:

```python
tool_results_map: dict[int, Any] = {}
spawn_coros: dict[int, tuple[Any, Any]] = {}  # idx -> (tc, coroutine)
steered = False

for idx, tc in enumerate(response.tool_calls):
    if steered or state.cancel_event.is_set():
        tool_results_map[idx] = tool_result(tc.id, "operation cancelled: steered")
        continue

    if tc.name == "spawn_task":
        # Queue for parallel execution
        spawn_coros[idx] = (tc, execute_tool_call(tc, state, sandbox))
    else:
        # Execute non-spawn tools sequentially (preserve existing behavior)
        result_msg, _ok = await execute_tool_call(tc, state, sandbox)
        tool_results_map[idx] = result_msg

        if not state.steer_queue.empty():
            steer_msg = state.steer_queue.get_nowait()
            state.messages.append(user_message(steer_msg))
            steered = True

# Execute all spawn_task calls concurrently
if spawn_coros and not steered:
    indices = list(spawn_coros.keys())
    coros = [spawn_coros[i][1] for i in indices]
    tcs = [spawn_coros[i][0] for i in indices]

    results = await asyncio.gather(*coros, return_exceptions=True)

    for idx, tc, result in zip(indices, tcs, results):
        if isinstance(result, tuple):
            tool_results_map[idx] = result[0]  # result_msg
        else:
            tool_results_map[idx] = tool_result(tc.id, f"Error: {result}")

    # Check steer queue after all spawns complete
    if not state.steer_queue.empty():
        steer_msg = state.steer_queue.get_nowait()
        state.messages.append(user_message(steer_msg))
        steered = True
elif spawn_coros and steered:
    # Steered during sequential phase — cancel queued spawns
    for idx, (tc, _) in spawn_coros.items():
        tool_results_map[idx] = tool_result(tc.id, "operation cancelled: steered")

# Reconstruct ordered results and append to messages
for idx in sorted(tool_results_map.keys()):
    state.messages.append(tool_results_map[idx])
```

### 6. Public API: `arcrun/__init__.py` and `arcrun/builtins/__init__.py`

Export `make_spawn_tool` from both modules for standalone usage.

## Data Flow

```
Parent run(model, tools, prompt, task, depth=0, max_depth=3)
    |
    v
_build_state() creates RunState with depth=0
    |
    v
Inject spawn_task tool into registry (depth < max_depth)
    |
    v
ReactStrategy loop — model sees spawn_task in available tools
    |
    v
Model calls spawn_task(task="research quantum computing", tools=["grep", "read"])
    |
    v
spawn_task.execute():
    1. Check depth (0) < max_depth (3) ✓
    2. Filter tools to ["grep", "read"] subset
    3. Create bubble handler → on_event callback
    4. Call run(model, filtered_tools, prompt, child_task,
               depth=1, max_depth=3, on_event=bubble_handler)
    |
    v
Child run (depth=1):
    - Gets own RunState, EventBus, ToolRegistry (completely isolated)
    - Gets spawn_task injected (depth 1 < max_depth 3)
    - Runs react loop with filtered tools + spawn_task
    - Every child event fires bubble_handler → parent bus gets prefixed event
    - Could spawn grandchild if needed (depth=2)
    - Returns LoopResult
    |
    v
Parent receives LoopResult.content as tool result string
    |
    v
React loop continues with child's answer in context
```

## Research Insights

**Enriched**: 2026-02-16 | **Sources**: 3 parallel research agents (recursive safety, parallel execution, event/test patterns)

### R1 — Recursive `run()` Safety: Confirmed Safe

Thorough analysis of `loop.py:17-53` confirms each `run()` call creates **completely isolated state**:

- Fresh `run_id` (UUID) — `loop.py:32`
- Fresh `EventBus` instance — `loop.py:33`
- Fresh `ToolRegistry` instance — `loop.py:34`
- Fresh `Sandbox` instance — `loop.py:35`
- Fresh `RunState` dataclass — `loop.py:44-51`
- Fresh message list — `loop.py:39-42`

**No global mutable state detected**: Searched for `asyncio.Lock`, `threading.Lock`, and global mutables. Only found read-only constants (`_MAX_ERROR_LEN`, `_DEFAULT_PREFIX`, `_DEFAULT_ENV`, `_GRACE_PERIOD`) and the lazily-loaded `STRATEGIES` dict (read-only after initialization).

**Model object reentrancy**: Model objects use `httpx.AsyncClient` internally, which is explicitly designed for concurrent async use. No mutable state modified during `invoke()`. Safe to share one model across parent + child runs.

### R2 — Event Bubbling: Use Existing `on_event` Parameter

**Critical simplification discovered**: `run()` already accepts an `on_event` callback (`loop.py:80`). The spawn tool passes a bubbling handler as `on_event` to child `run()`. No need to create or inject custom EventBus instances.

**`run_id` preservation**: When bubbling via `on_event`, the parent's `EventBus.emit()` creates a new Event with the **parent's** `run_id` — the child's `run_id` is lost. Solution: include `child_run_id` in the data payload (not a schema change, just data enrichment).

**Performance at 3 levels of nesting**: ~6-10us per event propagation. 10,000 events at 3 levels = ~3MB memory. Negligible for typical workloads.

### R3 — Parallel Execution: `state.tool_calls_made` Is Safe

The integer increment at `executor.py:81` (`state.tool_calls_made += 1`) is safe under concurrent asyncio execution. Python's single-threaded event loop ensures atomic increment between `await` points. No locks needed.

**Tool result ordering**: Tool results are matched by `tool_use_id` (not position), but order matters for conversation coherence. `asyncio.gather` preserves input order in its return value, so ordered reconstruction is straightforward.

**Steer/cancel during parallel execution**: The `cancel_event` is shared via `ToolContext` — if set during one spawn, other spawns can detect it via `ctx.cancelled.is_set()`. Steer queue is checked after all parallel spawns complete (not between them).

### R4 — Testing: Separate MockModel Per Run Level Required

**MockModel at `conftest.py:55-69` uses shared `_call_count`** — if parent and child share a MockModel, the call counter collides. Parent's first invoke sets `_call_count=1`, child then gets response index 1 instead of 0.

**Solution**: Create separate `MockModel` instances for each run level. The spawn tool's `execute` function receives the parent's model, but for testing, mock the `run()` call or use a model-per-level factory:

```python
child_model = MockModel([
    LLMResponse(content="Child done.", stop_reason="end_turn"),
])
parent_model = MockModel([
    LLMResponse(
        tool_calls=[ToolCall(id="tc1", name="spawn_task", arguments={"task": "do X"})],
        stop_reason="tool_use",
    ),
    LLMResponse(content="Parent done.", stop_reason="end_turn"),
])
```

**Key test pattern**: Use closure to inject child_model into spawn tool's execute function. For real implementation, the spawn tool uses the same model for parent and child (which works because real LLM APIs are stateless). The separate-model pattern is only needed for MockModel in tests.

### R5 — Design Refinement: Spawn Tool Injection Location

The spawn tool must be injected AFTER `_build_state()` returns, not inside it. This is because the spawn tool factory needs:
- The `model` reference (available in `run()` but not `_build_state()`)
- The `state` reference (created by `_build_state()`)
- The original `tools` list (to support subsetting)

Injection should happen between `_build_state()` and `_select_and_emit()` in `run()`:

```python
state, sandbox_obj = _build_state(...)

# Inject spawn_task if recursion depth allows
if state.depth < state.max_depth:
    from arcrun.builtins.spawn import make_spawn_tool
    spawn_tool = make_spawn_tool(
        model=model, tools=tools, system_prompt=system_prompt,
        state=state, sandbox=sandbox, allowed_strategies=allowed_strategies,
    )
    state.registry.add(spawn_tool)

strategy_fn = await _select_and_emit(allowed_strategies, model, state)
```

## Security Considerations

| Threat | Mitigation |
|--------|------------|
| Runaway recursion (ASI08) | `max_depth` enforcement. Default 3. Configurable. spawn_task not injected at max depth — model can't even attempt it. |
| Unbounded consumption (LLM10) | Budget fields on RunState (observability v1, enforcement v2). Depth limit caps total possible runs to `max_depth * max_turns`. |
| Privilege escalation (ASI03) | Children inherit parent's sandbox. Can't access tools parent doesn't have. Tool subsetting only restricts, never expands. |
| Event injection (ASI06) | Child events are prefixed by parent's bubble handler, not by child. Child can't forge parent event types. |
| Cascading failure (ASI08) | Children are completely isolated (confirmed: separate RunState, EventBus, ToolRegistry). One child's failure returns error string. No shared mutable state between siblings. |
| Model reentrancy | Confirmed safe: httpx.AsyncClient is concurrent-safe, model invoke() is stateless. Same model object shared across all run levels. |

## Dependencies

None new. Uses existing `asyncio`, `uuid`, and `arcrun` internals.
