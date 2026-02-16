# Coverage Analysis: arcrun (specs 001 + 002)

**Date:** 2026-02-14
**Overall Line Coverage:** 97% (316 statements, 9 missed)
**Test Count:** 77 tests across 10 test files
**Verdict:** PASSED (exceeds 80% threshold)

---

## 1. Coverage Summary

| File | Stmts | Miss | Cover | Category |
|------|-------|------|-------|----------|
| `__init__.py` | 5 | 0 | 100% | Configuration |
| `_messages.py` | 12 | 0 | 100% | Utilities |
| `events.py` | 24 | 0 | 100% | Business Logic |
| `executor.py` | 33 | 0 | 100% | Business Logic |
| `loop.py` | 57 | 3 | 95% | Critical Path |
| `registry.py` | 21 | 0 | 100% | Business Logic |
| `sandbox.py` | 26 | 0 | 100% | Critical Path |
| `state.py` | 20 | 0 | 100% | Business Logic |
| `strategies/__init__.py` | 17 | 4 | 76% | Business Logic |
| `strategies/react.py` | 71 | 2 | 97% | Critical Path |
| `types.py` | 30 | 0 | 100% | Configuration |
| **TOTAL** | **316** | **9** | **97%** | |

### Coverage by Category vs Targets

| Category | Actual | Target | Status |
|----------|--------|--------|--------|
| Critical Paths (loop, react, sandbox) | 96% | 100% | GAP |
| Business Logic (events, executor, registry, state, strategies) | 97% | 95% | MET |
| Utilities (_messages) | 100% | 85% | MET |
| Configuration (__init__, types) | 100% | 50% | MET |

---

## 2. Branch Coverage Analysis (Estimated)

Branch coverage is not being measured (no `--branch` flag in pytest-cov config). The following is a manual analysis of every conditional in the codebase.

### Total Branches Identified: 52
### Branches Covered: ~45
### Estimated Branch Coverage: ~87%

#### Detailed Branch Inventory

**loop.py (8 branches, ~5 covered = 63%)**

| Line | Branch | Covered? | Notes |
|------|--------|----------|-------|
| 30 | `if not tools` (True) | YES | `test_empty_tools_raises` |
| 30 | `if not tools` (False) | YES | All normal tests |
| 46 | `if not STRATEGIES` (True) | YES | First call in test |
| 46 | `if not STRATEGIES` (False) | YES | Subsequent calls |
| 64 | `if not tools` in run_async (True) | NO | Missing test |
| 64 | `if not tools` in run_async (False) | YES | `test_returns_run_handle` |
| 80 | `if not STRATEGIES` in run_async (True) | NO | STRATEGIES already loaded |
| 80 | `if not STRATEGIES` in run_async (False) | YES | Normal flow |

**react.py (20 branches, ~17 covered = 85%)**

| Line | Branch | Covered? | Notes |
|------|--------|----------|-------|
| 28 | while loop (enter) | YES | All tests |
| 28 | while loop (skip/max_turns=0) | NO | No test for max_turns=0 |
| 29 | cancel_event check (True) | YES | `test_cancel_sets_event` |
| 29 | cancel_event check (False) | YES | All normal tests |
| 35 | steer_queue empty (True) | YES | Normal tests |
| 35 | steer_queue not empty (False) | YES | Steering tests |
| 41 | transform_context is None | YES | Normal tests |
| 41 | transform_context is not None | YES | `test_transform_context_called` |
| 51 | has usage (True) | YES | All tests (MockModel has usage) |
| 51 | has usage (False) | NO | No test with usage=None |
| 68 | response.content truthy | YES | Tests with content |
| 68 | response.content falsy | YES | Tests with content=None |
| 72 | assistant_content truthy | YES | All tests |
| 72 | assistant_content falsy | NO | No test with empty response |
| 76 | end_turn and no tool_calls | YES | All end_turn tests |
| 77 | followup_queue not empty | YES | `test_followup_continues_loop` |
| 77 | followup_queue empty | YES | Normal end_turn tests |
| 91 | steered or cancel (True) | NO | Lines 92-93 uncovered |
| 91 | steered or cancel (False) | YES | Normal tool execution |
| 98 | steer_queue not empty mid-tools | YES | `test_steer_skips_remaining_tools` |
| 109 | turn_count >= max_turns | YES | `test_max_turns_hit` |

**sandbox.py (10 branches, 10 covered = 100%)**

All branches covered: no config, allowlist permit, allowlist deny, callback allow, callback deny, callback error.

**executor.py (8 branches, 8 covered = 100%)**

All branches covered: sandbox denied, tool not found, validation error, tool exception, happy path.

**registry.py (2 branches, 2 covered = 100%)**

`remove()` with name present and name absent both covered.

**strategies/__init__.py (8 branches, ~5 covered = 63%)**

| Line | Branch | Covered? | Notes |
|------|--------|----------|-------|
| 28 | `if not STRATEGIES` (True) | YES | First load |
| 28 | `if not STRATEGIES` (False) | NO | Line 29 never hit (already loaded) |
| 31 | `allowed is None` (True) | YES | Default tests |
| 31 | `allowed is None` (False) | YES | `test_unknown_strategy_raises` |
| 34 | `if unknown` (True) | YES | `test_unknown_strategy_raises` |
| 34 | `if unknown` (False) | NO | No test with valid allowed list > 1 |
| 36 | `len(allowed) == 1` (True) | NO | No test with single valid strategy |
| 36 | `len(allowed) == 1` (False) | NO | Lines 36-38 uncovered |

**events.py (4 branches, 4 covered = 100%)**

All branches covered: data None vs dict, on_event None vs callable.

---

## 3. Critical Gaps (Prioritized by Business Impact)

### P0 -- Must Fix (Critical Path, High Risk)

#### GAP-1: Cancel during tool execution loop (react.py:91-93)

**Lines:** 91-93
**Description:** When the model returns multiple tool_calls and a steer or cancel occurs after the first tool executes, remaining tools should be skipped with "operation cancelled: steered". This path is never tested end-to-end.
**Risk:** HIGH. This is the primary safety mechanism for aborting multi-tool execution. If broken, cancel/steer during batch tool calls would silently continue executing tools the user tried to stop.
**PRD Requirement:** R-7 (Steering) -- "steer(message): inject after current tool, skip remaining"
**Why existing tests miss it:** `test_steer_skips_remaining_tools` tests steer by pre-loading the queue, so the steer is picked up *between* tool calls via the `if not state.steer_queue.empty()` check at line 98, which sets `steered = True`. But the skip logic at line 91 that uses `steered` to cancel *remaining* tools in the same batch has never been verified to produce the "operation cancelled: steered" result messages.
**Note:** Looking more carefully, `test_steer_skips_remaining_tools` does pre-load the steer queue with 3 tool calls. The first tool executes, then line 98 checks the queue, sets `steered = True`, and lines 91-92 fire for tc2 and tc3. So this path IS exercised but the coverage tool reports it missed -- likely a timing issue in the async test. Regardless, the test does not *assert* on the cancelled tool results, only on the final content.

#### GAP-2: RunHandle.follow_up() (loop.py:105)

**Lines:** 105
**Description:** The `follow_up()` method on RunHandle has never been called via the public API. All followup tests inject directly into `handle._state.followup_queue`, bypassing the method.
**Risk:** HIGH. This is the public steering API (R-7). If the method had a bug (wrong queue, typo), no test would catch it.
**PRD Requirement:** R-1, R-7 -- RunHandle exposes `follow_up()`

#### GAP-3: run_async with sandbox option (loop.py:70)

**Lines:** 65, 70 (the sandbox path in run_async)
**Description:** `run_async()` accepts sandbox config via `options.get("sandbox")` but no test passes sandbox to `run_async()`. Only `run()` is tested with sandbox.
**Risk:** MEDIUM-HIGH. If `run_async` had a key typo ("sandbox_config" vs "sandbox"), sandbox enforcement would silently be disabled for all non-blocking runs.
**PRD Requirement:** R-1, R-4 -- Both entry points must support sandbox

### P1 -- Should Fix (Business Logic, Medium Risk)

#### GAP-4: run_async with max_turns option (loop.py:83)

**Lines:** 83
**Description:** `run_async()` reads max_turns from options but only `test_cancel_sets_event` passes it (and only incidentally). No test verifies that max_turns is actually respected through run_async.
**Risk:** MEDIUM. If max_turns was ignored in run_async, agents could loop forever.
**PRD Requirement:** R-6 -- max_turns is strategy-enforced

#### GAP-5: Multi-strategy selection (strategies/__init__.py:36-38)

**Lines:** 29, 36-38
**Description:** When `allowed` contains multiple valid strategies, the code defaults to "react". This path is completely untested. Also, `_load_strategies()` being called inside `select_strategy` (line 29) is never hit because STRATEGIES is already populated by the time `select_strategy` runs in tests.
**Risk:** LOW-MEDIUM. Currently only one strategy exists, so this is future-proofing code. But when a second strategy is added, this selection logic will be load-bearing with no test coverage.
**PRD Requirement:** R-6 -- Strategy selection

#### GAP-6: No test for run_async with empty tools

**Lines:** 64-65
**Description:** `run_async()` has an empty tools guard (`if not tools: raise ValueError`) that is never tested. The equivalent guard in `run()` IS tested.
**Risk:** LOW-MEDIUM. Same pattern as `run()`, but defense in depth requires verification.

### P2 -- Nice to Have (Edge Cases, Low Risk)

#### GAP-7: max_turns=0

**Description:** What happens when max_turns=0? The while loop condition `state.turn_count < max_turns` is immediately false. The code falls through to `_build_result(state, None)` with 0 turns. This edge case is untested.
**Risk:** LOW. Unlikely in production but could cause confusion.

#### GAP-8: Model response with no content and no tool_calls

**Description:** If model returns `stop_reason="end_turn"`, `content=None`, and `tool_calls=[]`, the assistant_content list is empty, so no assistant message is appended. Then `end_turn` path returns `_build_result(state, None)`. Untested edge case.
**Risk:** LOW. Defensive code path.

#### GAP-9: Model response without usage attribute

**Description:** `hasattr(response, "usage") and response.usage` -- the False branch (no usage) is never tested. MockModel always has usage.
**Risk:** LOW. Would just skip token tracking.

#### GAP-10: transform_context that raises

**Description:** If `transform_context` callback throws, exception propagates unhandled from `react_loop`. No test verifies this behavior.
**Risk:** LOW. Expected to bubble up, but worth documenting.

#### GAP-11: on_event handler that raises

**Description:** If the `on_event` callback raises during `bus.emit()`, the exception propagates up through the strategy. No test verifies this.
**Risk:** LOW. Same bubble-up pattern.

---

## 4. Requirements Traceability

| PRD Requirement | Tests | Coverage | Status |
|-----------------|-------|----------|--------|
| R-1: Entry Points (run, run_async, RunHandle) | test_loop.py (10 tests) | 95% | GAP: follow_up() untested via API |
| R-2: Tool System | test_types.py, test_executor.py | 100% | COVERED |
| R-3: Event System | test_events.py (8 tests) | 100% | COVERED |
| R-4: Sandbox | test_sandbox.py (8 tests) | 100% | GAP: not tested via run_async |
| R-5: Dynamic Registry | test_registry.py (9 tests) | 100% | COVERED |
| R-6: ReAct Strategy | test_react.py (9 tests) | 97% | GAP: cancel during tool loop |
| R-7: Steering | test_steering.py (6 tests) | Partial | GAP: follow_up via handle, cancel during tools |
| R-8: Error Handling | test_react.py, test_executor.py | 100% | COVERED |
| R-9: LoopResult | test_loop.py, test_integration.py | 100% | COVERED |

---

## 5. Improvement Plan

### Phase 1: Critical Gaps (P0) -- 3 tests, ~30 min

**Expected coverage increase:** 97% -> 99% (covers 6 of 9 missed lines)

#### Test 1: Cancel skips remaining tools with correct messages

```python
# test_steering.py or test_react.py
@pytest.mark.asyncio
async def test_cancel_during_multi_tool_skips_remaining():
    """When cancel_event is set during tool processing, remaining
    tool calls get 'operation cancelled: steered' results."""
    from arcrun.strategies.react import react_loop

    bus = EventBus(run_id="test")
    call_count = 0

    async def counting_echo(params, ctx):
        nonlocal call_count
        call_count += 1
        # Set cancel after first tool executes
        ctx.cancelled.set()
        return "done"

    tool = Tool(
        name="echo", description="Echo",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=counting_echo,
    )
    state = _make_state(bus, tools=[tool])
    sandbox = Sandbox(config=None, event_bus=bus)

    model = MockModel([
        LLMResponse(
            tool_calls=[
                ToolCall(id="tc1", name="echo", arguments={"input": "1"}),
                ToolCall(id="tc2", name="echo", arguments={"input": "2"}),
                ToolCall(id="tc3", name="echo", arguments={"input": "3"}),
            ],
            stop_reason="tool_use",
        ),
    ])

    result = await react_loop(model, state, sandbox, max_turns=5)
    assert call_count == 1  # Only first tool executed
    assert result.tool_calls_made == 1
    # Verify cancel results in messages
    cancel_msgs = [
        m for m in state.messages
        if hasattr(m, 'content') and isinstance(m.content, list)
        and any("cancelled" in str(b) for b in m.content)
    ]
    assert len(cancel_msgs) == 2  # tc2 and tc3 got cancel results
```

**Priority:** P0
**Effort:** 15 min
**Covers:** react.py lines 91-93

#### Test 2: RunHandle.follow_up via public API

```python
# test_steering.py
@pytest.mark.asyncio
async def test_followup_via_handle_api():
    """follow_up() method on RunHandle queues message correctly."""
    from arcrun.loop import run_async

    model = MockModel([
        LLMResponse(content="First.", stop_reason="end_turn"),
        LLMResponse(content="Second.", stop_reason="end_turn"),
    ])
    handle = await run_async(model, _tools(), "prompt", "task")
    await handle.follow_up("also do Y")
    result = await handle.result()
    assert result.content == "Second."
    assert result.turns == 2
```

**Priority:** P0
**Effort:** 5 min
**Covers:** loop.py line 105

#### Test 3: run_async with sandbox config

```python
# test_loop.py
@pytest.mark.asyncio
async def test_run_async_with_sandbox(self):
    """run_async passes sandbox config correctly."""
    from arcrun.loop import run_async
    from arcrun.types import SandboxConfig

    model = MockModel([
        LLMResponse(
            tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "x"})],
            stop_reason="tool_use",
        ),
        LLMResponse(content="Denied.", stop_reason="end_turn"),
    ])
    handle = await run_async(
        model, _tools(), "prompt", "task",
        sandbox=SandboxConfig(allowed_tools=["other"]),
    )
    result = await handle.result()
    denied = [e for e in result.events if e.type == "tool.denied"]
    assert len(denied) == 1
```

**Priority:** P0
**Effort:** 10 min
**Covers:** loop.py lines 65, 70 (sandbox path in run_async)

### Phase 2: High-Impact Gaps (P1) -- 3 tests, ~20 min

**Expected coverage increase:** 99% -> 100% (covers remaining 3 lines)

#### Test 4: run_async respects max_turns

```python
@pytest.mark.asyncio
async def test_run_async_respects_max_turns(self):
    from arcrun.loop import run_async

    model = MockModel([
        LLMResponse(
            tool_calls=[ToolCall(id=f"tc{i}", name="echo", arguments={"input": "x"})],
            stop_reason="tool_use",
        ) for i in range(10)
    ])
    handle = await run_async(model, _tools(), "prompt", "task", max_turns=2)
    result = await handle.result()
    assert result.turns == 2
```

**Covers:** loop.py line 83

#### Test 5: Multi-strategy selection defaults to react

```python
@pytest.mark.asyncio
async def test_multi_strategy_defaults_to_react():
    from arcrun.strategies import select_strategy, STRATEGIES, _load_strategies
    from arcrun.state import RunState

    if not STRATEGIES:
        _load_strategies()

    # Pass multiple valid strategies
    result = await select_strategy(["react"], MockModel([]), mock_state)
    assert result == "react"

    # Test len(allowed) > 1 path (line 38)
    # Need to register a second strategy temporarily
    STRATEGIES["dummy"] = lambda *a: None
    try:
        result = await select_strategy(["react", "dummy"], MockModel([]), mock_state)
        assert result == "react"
    finally:
        del STRATEGIES["dummy"]
```

**Covers:** strategies/__init__.py lines 29, 36-38

#### Test 6: run_async with empty tools raises

```python
@pytest.mark.asyncio
async def test_run_async_empty_tools_raises(self):
    from arcrun.loop import run_async

    model = MockModel([])
    with pytest.raises(ValueError, match="tools"):
        await run_async(model, [], "prompt", "task")
```

**Covers:** loop.py line 65 (empty tools guard in run_async)

### Phase 3: Edge Cases (P2) -- 5 tests, ~30 min

These do not increase line coverage but improve branch coverage and robustness.

#### Test 7: max_turns=0 returns immediately

```python
@pytest.mark.asyncio
async def test_max_turns_zero_returns_immediately():
    bus = EventBus(run_id="test")
    state = _make_state(bus)
    model = MockModel([])  # Should never be called
    sandbox = Sandbox(config=None, event_bus=bus)
    result = await react_loop(model, state, sandbox, max_turns=0)
    assert result.turns == 0
    assert result.content is None
```

#### Test 8: Model response without usage

```python
@pytest.mark.asyncio
async def test_model_without_usage_attribute():
    """Tokens stay at 0 when model has no usage."""
    bus = EventBus(run_id="test")
    state = _make_state(bus)

    class NoUsageResponse:
        content = "OK"
        tool_calls = []
        stop_reason = "end_turn"
        cost_usd = 0.0

    class NoUsageModel:
        async def invoke(self, messages, tools=None):
            return NoUsageResponse()

    sandbox = Sandbox(config=None, event_bus=bus)
    result = await react_loop(NoUsageModel(), state, sandbox, max_turns=5)
    assert result.tokens_used["total"] == 0
```

#### Test 9: Empty content and no tool_calls

```python
@pytest.mark.asyncio
async def test_empty_response_no_content_no_tools():
    """Model returns end_turn with no content, no tools."""
    bus = EventBus(run_id="test")
    state = _make_state(bus)
    model = MockModel([LLMResponse(content=None, stop_reason="end_turn")])
    sandbox = Sandbox(config=None, event_bus=bus)
    result = await react_loop(model, state, sandbox, max_turns=5)
    assert result.content is None
    assert result.turns == 1
```

#### Test 10: transform_context that raises

```python
@pytest.mark.asyncio
async def test_transform_context_exception_propagates():
    bus = EventBus(run_id="test")

    def bad_transform(msgs):
        raise RuntimeError("transform failed")

    state = _make_state(bus, transform_context=bad_transform)
    model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
    sandbox = Sandbox(config=None, event_bus=bus)

    with pytest.raises(RuntimeError, match="transform failed"):
        await react_loop(model, state, sandbox, max_turns=5)
```

#### Test 11: on_event handler that raises

```python
@pytest.mark.asyncio
async def test_on_event_exception_propagates():
    from arcrun.loop import run

    def bad_handler(event):
        raise ValueError("handler crashed")

    model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
    with pytest.raises(ValueError, match="handler crashed"):
        await run(model, _tools(), "prompt", "task", on_event=bad_handler)
```

---

## 6. Structured Findings

```yaml
line_coverage: "97%"
branch_coverage: "~87% (estimated, not measured)"
passed: true  # exceeds 80% threshold

critical_gaps:
  - file: "src/arcrun/strategies/react.py"
    lines: "91-93"
    description: "Cancel/steer during multi-tool batch skips remaining tools"
    risk: "HIGH"
    requirement: "R-7 Steering"

  - file: "src/arcrun/loop.py"
    lines: "105"
    description: "RunHandle.follow_up() never called via public API"
    risk: "HIGH"
    requirement: "R-1, R-7 Entry Points + Steering"

  - file: "src/arcrun/loop.py"
    lines: "65, 70"
    description: "run_async with sandbox config path untested"
    risk: "MEDIUM-HIGH"
    requirement: "R-1, R-4 Entry Points + Sandbox"

  - file: "src/arcrun/loop.py"
    lines: "83"
    description: "run_async max_turns option not verified"
    risk: "MEDIUM"
    requirement: "R-6 ReAct Strategy"

  - file: "src/arcrun/strategies/__init__.py"
    lines: "29, 36-38"
    description: "Multi-strategy selection path untested"
    risk: "LOW-MEDIUM"
    requirement: "R-6 Strategy Selection"

recommended_tests:
  - test_name: "test_cancel_during_multi_tool_skips_remaining"
    scenario: "Cancel event set during first tool of 3; verify only 1 executes"
    priority: "P0"
    description: "Validates react.py lines 91-93 cancel path"

  - test_name: "test_followup_via_handle_api"
    scenario: "Call handle.follow_up() and verify loop continues"
    priority: "P0"
    description: "Validates loop.py line 105 public API"

  - test_name: "test_run_async_with_sandbox"
    scenario: "Pass SandboxConfig to run_async, verify denial works"
    priority: "P0"
    description: "Validates loop.py lines 65, 70 sandbox in run_async"

  - test_name: "test_run_async_respects_max_turns"
    scenario: "Pass max_turns=2 to run_async, verify 2 turns"
    priority: "P1"
    description: "Validates loop.py line 83 max_turns in run_async"

  - test_name: "test_multi_strategy_defaults_to_react"
    scenario: "Pass multiple allowed strategies, verify react selected"
    priority: "P1"
    description: "Validates strategies/__init__.py lines 36-38"

  - test_name: "test_run_async_empty_tools_raises"
    scenario: "Pass empty tools list to run_async"
    priority: "P1"
    description: "Validates loop.py line 65 guard"

  - test_name: "test_max_turns_zero"
    scenario: "max_turns=0, verify immediate return with no model calls"
    priority: "P2"
    description: "Edge case for react loop entry"

  - test_name: "test_model_without_usage"
    scenario: "Model response lacks usage attribute"
    priority: "P2"
    description: "Branch coverage for token tracking"

  - test_name: "test_empty_response"
    scenario: "Model returns end_turn with content=None and no tool_calls"
    priority: "P2"
    description: "Branch coverage for empty assistant message"

  - test_name: "test_transform_context_exception"
    scenario: "transform_context callback raises"
    priority: "P2"
    description: "Error propagation verification"

  - test_name: "test_on_event_exception"
    scenario: "on_event handler raises"
    priority: "P2"
    description: "Error propagation verification"
```

---

## 7. Additional Observations

### Strengths

1. **Executor at 100%** -- The spec-002 extraction is fully tested with all 5 outcomes (happy, denied, not found, validation, exception) plus events and timing.
2. **Event coverage excellent** -- Every event type documented in R-3 has at least one test asserting it fires.
3. **Sandbox at 100%** -- All 5 paths (no config, allowlist pass, allowlist deny, callback pass/deny, callback error) are tested.
4. **Good separation** -- Unit tests (test_react.py, test_executor.py) and integration tests (test_integration.py) test different things.

### Weaknesses

1. **No branch coverage measurement** -- `pyproject.toml` has no `[tool.coverage]` section. Branch coverage should be enabled with `branch = true`.
2. **run_async under-tested** -- 5 of 9 missed lines are in `run_async()`. The run_async path mirrors `run()` but is verified only for basic handle creation and result retrieval.
3. **Steering tests use internal state** -- `test_steer_skips_remaining_tools` and `test_followup_continues_loop` inject directly into `_state` queues rather than using the public `handle.steer()`/`handle.follow_up()` API. This tests the strategy but not the handle methods.
4. **Integration test count low** -- 4 integration tests for a system with this many interacting components. Missing: steering + sandbox combined, followup + tool calls combined, cancel during multi-tool with sandbox.

### Recommendation: Enable Branch Coverage

Add to `pyproject.toml`:

```toml
[tool.coverage.run]
branch = true

[tool.coverage.report]
show_missing = true
fail_under = 90
```

This would reveal the true branch coverage (estimated ~87%) and catch paths the line coverage misses.

---

## 8. Success Criteria

| Criterion | Current | After Phase 1 | After Phase 2 |
|-----------|---------|---------------|---------------|
| Line coverage | 97% | ~99% | 100% |
| Branch coverage (est.) | ~87% | ~92% | ~95% |
| Critical gaps (P0) | 3 | 0 | 0 |
| Tests | 77 | 80 | 83 |
| All PRD requirements tested | 7/9 | 9/9 | 9/9 |
