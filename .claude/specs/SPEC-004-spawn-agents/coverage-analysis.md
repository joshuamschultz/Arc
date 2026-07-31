# SPEC-004: Recursive Agent Spawning - Coverage Analysis

**Date**: 2026-02-16
**Tests**: 21 (12 unit + 9 integration) -- all passing
**Test Duration**: 0.82s

---

## 1. Coverage Summary

| File | Stmts | Miss | Branch | BrPart | Cover |
|------|-------|------|--------|--------|-------|
| `builtins/spawn.py` | 35 | 0 | 8 | 0 | **100%** |
| `state.py` | 27 | 0 | 0 | 0 | **100%** |
| `loop.py` | 60 | 17 | 8 | 2 | **69%** |
| `strategies/react.py` | 103 | 28 | 44 | 12 | **67%** |
| **TOTAL** | **225** | **45** | **60** | **14** | **76%** |

### By Component Category

| Category | Coverage | Target | Status |
|----------|----------|--------|--------|
| spawn.py (Core feature) | 100% | 90% | PASS |
| state.py (Data model) | 100% | 90% | PASS |
| loop.py (Orchestration) | 69% | 90% | **FAIL** |
| react.py (Strategy) | 67% | 90% | **FAIL** |

---

## 2. PRD Requirement Coverage Matrix

| Req | Description | Unit Test | Integration Test | Status |
|-----|-------------|-----------|------------------|--------|
| FR-1 | `spawn_task` built-in tool exists | `test_factory_returns_tool` | -- | COVERED |
| FR-2 | Accepts task, system_prompt, tools | `test_schema_has_task_required`, `test_schema_has_optional_system_prompt`, `test_schema_has_optional_tools` | `test_child_uses_provided_system_prompt`, `test_child_gets_only_named_tools` | COVERED |
| FR-3 | Child run() at depth+1 | -- | `test_parent_spawns_child_and_gets_result`, `test_parent_child_grandchild` | COVERED |
| FR-4 | Child inherits tools unless subset | `test_valid_tool_subset_succeeds`, `test_unknown_tool_name_returns_error` | `test_child_gets_only_named_tools` | COVERED |
| FR-5 | Child returns LoopResult.content | -- | `test_parent_spawns_child_and_gets_result` | COVERED |
| FR-6 | Child failure returns error string | -- | `test_child_exception_returns_error_string` | COVERED |
| FR-7 | RunState tracks depth/max_depth/parent_run_id | `_make_parent_state()` verifies fields | -- | PARTIALLY COVERED |
| FR-8 | Spawn rejected at depth >= max_depth | `test_depth_at_max_returns_error`, `test_depth_exceeds_max_returns_error` | `test_spawn_not_available_at_max_depth` | COVERED |
| FR-9 | run()/run_async() accept depth/max_depth | -- | `test_parent_spawns_child_and_gets_result` (run only) | **GAP: run_async not tested** |
| FR-10 | Default max_depth is 3 | -- | -- | **GAP: no assertion on default** |
| FR-11 | Child events propagate with prefix | `test_bubble_handler_emits_prefixed_event`, `test_bubble_handler_preserves_child_data` | `test_child_events_appear_on_parent_bus` | COVERED |
| FR-12 | Parallel spawn via asyncio.gather | -- | `test_two_parallel_spawns` | COVERED |
| FR-13 | Export from arcrun.builtins + arcrun.__init__ | -- | -- | **GAP: no import test** |
| FR-14 | Factory pattern matches make_execute_tool | `test_factory_returns_tool`, `test_tool_timeout_is_none` | -- | COVERED |

### Coverage Score: 11/14 fully covered, 1 partial, 2 gaps

---

## 3. Critical Gaps (Prioritized by Business Impact)

### P0 -- CRITICAL: run_async() spawn injection completely untested (FR-9)

**File**: `loop.py` lines 127-146
**Impact**: `run_async()` is the primary entry point for production use (non-blocking with RunHandle steering). The spawn injection path at lines 136-142 is duplicated from `run()` but has **zero test coverage**. If the injection silently breaks in `run_async()`, agents in production cannot spawn children.

**Risk**: High. `run_async()` + RunHandle is the intended production API. Spawning via `run()` works, but `run_async()` is untested.

**Uncovered lines**: 127-146 (entire `run_async()` function body including spawn injection branch)

**Recommended tests**:
1. `test_run_async_injects_spawn_below_max_depth` -- Verify spawn_task appears in tool schemas
2. `test_run_async_omits_spawn_at_max_depth` -- Verify spawn_task absent at depth == max_depth
3. `test_run_async_spawn_produces_result` -- Parent spawns child via run_async handle, child returns content

---

### P0 -- CRITICAL: Steer during parallel spawn execution untested

**File**: `react.py` lines 136-141
**Impact**: When a steer message arrives during parallel spawn execution (the `spawn_queue and steered` branch at line 139), spawns should be cancelled. This is a safety-critical path -- steering is the human override mechanism. If broken, the human loses control over spawned agents.

**Uncovered lines**:
- Line 136-138: steer check after `asyncio.gather` completes
- Line 139-141: steered=True cancels remaining spawns

**Recommended tests**:
1. `test_steer_cancels_pending_spawn_tasks` -- Queue a steer, verify spawn_task results contain "operation cancelled: steered"
2. `test_steer_after_parallel_spawns_complete` -- Steer arrives after gather returns, verify next iteration picks it up

---

### P1 -- HIGH: RunHandle methods (steer, follow_up, cancel, result) completely untested

**File**: `loop.py` lines 149-175
**Impact**: RunHandle is the production control interface. None of its methods are exercised by spawn tests. While these may be covered by other test files, they are the mechanism by which operators steer spawned agent trees.

**Uncovered lines**: 153-154, 158, 162, 166, 170, 175

**Recommended tests**:
1. `test_run_async_steer_interrupts_spawn` -- Steer while child is running
2. `test_run_async_cancel_stops_spawn` -- Cancel while child is spawning
3. `test_run_async_result_awaits_completion` -- Basic result retrieval

---

### P1 -- HIGH: Default max_depth=3 not asserted (FR-10)

**File**: `state.py` line 27, `loop.py` line 86/124
**Impact**: If someone changes the default from 3 to 1 or 100, no test catches it. The default depth limit is a security boundary (prevents runaway recursive spawning).

**Recommended test**:
```python
def test_default_max_depth_is_3():
    state = RunState(
        messages=[], registry=..., event_bus=...
    )
    assert state.max_depth == 3
```

---

### P1 -- HIGH: react.py steer-during-sequential-tool-execution untested for spawn context

**File**: `react.py` lines 108-109, 114-120
**Impact**: When a sequential (non-spawn) tool call triggers a steer, subsequent tool calls (including spawn_task) should be cancelled. The branch at lines 108-109 (`if steered or state.cancel_event.is_set()`) and lines 117-120 (steer detection after sequential tool) are not covered by spawn tests.

**Recommended test**:
1. `test_steer_during_sequential_tool_cancels_later_spawn` -- Mixed sequential + spawn tools in one turn, steer after sequential

---

### P2 -- MEDIUM: Export verification (FR-13)

**File**: `builtins/__init__.py`, `__init__.py`
**Impact**: If `make_spawn_tool` disappears from `__all__`, downstream code breaks silently at import time.

**Recommended tests**:
```python
def test_make_spawn_tool_exported_from_builtins():
    from arcrun.builtins import make_spawn_tool
    assert callable(make_spawn_tool)

def test_make_spawn_tool_exported_from_arcrun():
    from arcrun import make_spawn_tool
    assert callable(make_spawn_tool)
```

---

### P2 -- MEDIUM: LoopResult.content is None returns "(no content)"

**File**: `spawn.py` line 78
**Impact**: When a child run returns `LoopResult(content=None)`, the spawn tool returns "(no content)" instead of None. This is covered implicitly but no test directly asserts this edge case.

**Recommended test**:
```python
async def test_child_returning_none_content_gives_no_content_string():
    # Model returns end_turn with content=None
    ...
    result = await tool.execute({"task": "empty"}, ctx)
    assert result == "(no content)"
```

---

### P2 -- MEDIUM: cancel_event checked during tool processing

**File**: `react.py` lines 50-51, 107
**Impact**: If `cancel_event` is set during spawn execution, the loop should break. Not tested in spawn context.

---

### P3 -- LOW: token_budget and cost_budget fields in RunState

**File**: `state.py` lines 29-30
**Impact**: These fields exist in RunState but are not used by any spawn logic yet. They are placeholders for future budget enforcement. No test needed now, but they should be tested when budget enforcement is implemented.

---

## 4. Uncovered Branches in react.py (Spawn-Specific)

| Lines | Branch | Description | Priority |
|-------|--------|-------------|----------|
| 87->91 | `response.content` is falsy | No text content, only tool calls | P3 |
| 93-96 | `followup_queue` not empty at end_turn | Follow-up injection during spawn | P2 |
| 108-109 | `steered or cancel_event` on tool loop | Cancel/steer skips remaining tools | P1 |
| 114-120 | Steer detected after sequential tool | Mid-turn steer affects later spawns | P1 |
| 132-133 | `asyncio.gather` returns exception | Spawn child raises during gather | P1 |
| 136-141 | Steer after parallel spawns + steered spawn cancellation | Human override during parallel spawns | P0 |
| 149-155 | `max_turns` reached | Spawn runs out of turns | P2 |

---

## 5. Edge Cases Not Tested

| Edge Case | Risk | Priority |
|-----------|------|----------|
| Spawn with empty task string `""` | Child gets empty user message | P2 |
| Spawn with very large tool subset (all tools) | Should behave same as no subset | P3 |
| Concurrent steer + spawn completing simultaneously | Race condition | P1 |
| Child spawn at depth=2 with max_depth=3 (spawn_task available but grandchild at limit) | Spawn injection logic edge | P2 |
| Parent model exhausted during child run | MockModel raises RuntimeError | P2 |
| Two children fail in parallel gather | Both return error strings | P1 |
| Spawn with custom system_prompt AND tool subset combined | Both overrides together | P2 |
| `_build_state` with `messages` parameter + spawn injection | Session continuation + spawn | P1 |

---

## 6. Improvement Plan

### Phase 1: Critical (P0) -- Must Fix

| Test | File | Expected Coverage Gain |
|------|------|----------------------|
| `test_run_async_injects_spawn_below_max_depth` | `test_spawn_integration.py` | +5% (loop.py 136-142) |
| `test_run_async_omits_spawn_at_max_depth` | `test_spawn_integration.py` | +2% (loop.py 136 false branch) |
| `test_run_async_spawn_produces_result` | `test_spawn_integration.py` | +8% (loop.py 127-146) |
| `test_steer_cancels_pending_spawn_tasks` | `test_spawn_integration.py` | +3% (react.py 139-141) |

**Effort**: ~2 hours
**Expected total coverage after**: ~85%

### Phase 2: High Impact (P1) -- Should Fix

| Test | File | Expected Coverage Gain |
|------|------|----------------------|
| `test_run_async_steer_interrupts_spawn` | `test_spawn_integration.py` | +3% (loop.py 158, react.py 108-109) |
| `test_run_async_cancel_stops_spawn` | `test_spawn_integration.py` | +2% (loop.py 166) |
| `test_default_max_depth_is_3` | `test_spawn_tool.py` | +0% (assertion only) |
| `test_parallel_spawn_one_fails` | `test_spawn_integration.py` | +2% (react.py 132-133) |
| `test_steer_during_sequential_cancels_later_spawn` | `test_spawn_integration.py` | +3% (react.py 114-120) |

**Effort**: ~3 hours
**Expected total coverage after**: ~92%

### Phase 3: Medium (P2) -- Nice to Have

| Test | File | Expected Coverage Gain |
|------|------|----------------------|
| `test_make_spawn_tool_exported` | `test_spawn_tool.py` | +0% |
| `test_child_none_content` | `test_spawn_tool.py` | +0% (already covered path) |
| `test_max_turns_during_spawn` | `test_spawn_integration.py` | +1% (react.py 149-155) |
| `test_followup_at_end_turn_with_spawn` | `test_spawn_integration.py` | +1% (react.py 93-96) |

**Effort**: ~2 hours
**Expected total coverage after**: ~95%

---

## 7. Summary

**Current state**: spawn.py and state.py are at 100%. The gaps are in loop.py (run_async path) and react.py (steer/cancel during parallel spawn). The spawn tool factory itself is thoroughly tested.

**Top 3 critical gaps**:
1. `run_async()` spawn injection is completely untested (the production API path)
2. Steer during parallel spawn execution is untested (safety-critical human override)
3. Default max_depth=3 has no regression guard (security boundary)

**Recommended next step**: Implement Phase 1 tests (4 tests, ~2 hours) to bring coverage from 76% to ~85% and close the two P0 gaps.
