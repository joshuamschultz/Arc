# PRD: Tool Executor Extraction

## Problem

The tool execution pipeline (sandbox check, registry lookup, schema validation, execute, event emission, error handling) is inline in `react.py` lines 88-148. Future strategies (CodeExec, Recursive) need the identical pipeline. Without extraction, each new strategy copy-pastes ~50 lines, creating maintenance burden and inconsistency risk.

## Goal

Extract the tool execution pipeline into a single shared function that any strategy can call. One place to maintain, one place to add future enhancements (timeout, truncation, rate limiting).

## Requirements

### R1: Single Tool Call Execution
The executor must handle one tool call through the complete pipeline:
1. Emit `tool.start` event
2. Check sandbox permissions
3. Look up tool in registry
4. Validate arguments against input_schema
5. Build ToolContext
6. Execute tool with timing
7. Handle exceptions
8. Emit `tool.end` or `tool.error` event
9. Increment `state.tool_calls_made` on success
10. Return result message and success indicator

### R2: Return Contract
- Returns `tuple[Message, bool]`
- `Message` is a tool result message (arcllm format) for conversation history
- `bool` indicates success (True) or failure (False)
- Failure cases: sandbox denied, tool not found, validation error, execution exception

### R3: No Control Flow Awareness
- Executor does NOT check cancel events
- Executor does NOT check steer queues
- Executor does NOT know about turns, loops, or strategy logic
- Strategy handles all control flow before/after calling executor

### R4: Event Parity
- Must emit the exact same events as current inline code
- `tool.start` with name and arguments
- `tool.end` with name, result_length, duration_ms
- `tool.error` with name and error string
- Sandbox denial events emitted by Sandbox.check() (unchanged)

### R5: State Mutation
- Increments `state.tool_calls_made` on successful execution only
- Does not modify `state.messages` (strategy appends results)
- Does not modify `state.turn_count` (strategy manages)

### R6: Internal API
- Not exported from `__init__.py`
- Strategies import directly: `from arcrun.executor import execute_tool_call`
- Same pattern as `_messages.py`

## Non-Requirements

- No parallel execution (future enhancement — strategy-level concern)
- No tool timeout (future enhancement — will add to executor later)
- No result truncation (future enhancement — will add to executor later)
- No rate limiting (future enhancement — will add to executor later)
- No behavior change from current code

## Success Criteria

- All 70 existing tests pass unchanged
- New executor unit tests cover all 5 outcomes
- react.py has no inline tool execution logic
- mypy and ruff clean
- Net line change: ~+10 lines (extraction overhead)
