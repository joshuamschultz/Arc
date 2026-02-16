# PLAN: Core Loop + ReAct (001)

**Status:** COMPLETE
**Estimated Lines:** ~610
**Actual Lines:** 621
**Line Budget:** 1,000 (total), ~610 (Phase 1)

## Phase 1: Project Skeleton + Types

**Goal:** Package structure, pyproject.toml, type definitions. Everything compiles and imports.

### Tasks

- [x] **1.1** Create project skeleton: `src/arcrun/`, `tests/`, `pyproject.toml` (Hatchling, DECISION-007)
- [x] **1.2** Create `src/arcrun/types.py` — Tool, ToolContext, SandboxConfig, LoopResult dataclasses
- [x] **1.3** Create `src/arcrun/__init__.py` — public exports (empty implementations for now)
- [x] **1.4** Write `tests/test_types.py` — Tool construction (functional), ToolContext fields, SandboxConfig defaults, LoopResult fields
- [x] **1.5** Verify: `pytest tests/test_types.py` passes, `from arcrun import Tool, LoopResult` works

**Completion:** 11/11 tests pass, package imports work
**Lines added:** ~69 (types.py: 51, __init__.py: 18)

---

## Phase 2: Event Bus

**Goal:** Event emission and collection. Every action will use this.

### Tasks

- [x] **2.1** Write `tests/test_events.py` — Event creation, EventBus emit, event collection, on_event callback, handler exception propagation
- [x] **2.2** Create `src/arcrun/events.py` — Event dataclass, EventBus class
- [x] **2.3** Verify: all event tests pass

**Completion:** 8/8 tests pass. EventBus emits, collects, calls handler
**Lines added:** ~42

---

## Phase 3: Sandbox

**Goal:** Permission boundary enforces allowlist model.

### Tasks

- [x] **3.1** Write `tests/test_sandbox.py` — No config (all allowed), allowlist enforcement, tool not in list (denied), check callback (allowed/denied), callback exception (fail-safe denial), tool.denied event emission
- [x] **3.2** Create `src/arcrun/sandbox.py` — Sandbox class
- [x] **3.3** Verify: all sandbox tests pass

**Completion:** 8/8 tests pass. Sandbox enforces allowlist, emits denials
**Lines added:** ~55

---

## Phase 4: Tool Registry

**Goal:** Dynamic mutable tool collection with events.

### Tasks

- [x] **4.1** Write `tests/test_registry.py` — Init from tool list, add tool, remove tool, replace (duplicate name), get by name, list_schemas conversion, names list, add/remove events emitted
- [x] **4.2** Create `src/arcrun/registry.py` — ToolRegistry class
- [x] **4.3** Verify: all registry tests pass

**Completion:** 9/9 tests pass. Registry add/remove/get works, emits events
**Lines added:** ~43

---

## Phase 5: RunState

**Goal:** Internal mutable state container.

### Tasks

- [x] **5.1** Write `tests/test_state.py` — RunState construction, default values, steer/followup queues work, cancel_event works
- [x] **5.2** Create `src/arcrun/state.py` — RunState dataclass
- [x] **5.3** Verify: all state tests pass

**Completion:** 5/5 tests pass. RunState holds all execution state
**Lines added:** ~29

---

## Phase 6: ReAct Strategy

**Goal:** The actual loop. This is the core.

### Tasks

- [x] **6.1** Write `tests/test_react.py`:
  - Single turn: model returns end_turn -> LoopResult with content
  - Multi-turn: model calls tool -> result flows back -> model responds
  - Tool denied by sandbox -> error returned to model, tool.denied event
  - Tool exception -> caught, error to model, tool.error event
  - Param validation failure -> error to model
  - Max turns hit -> loop.max_turns event, returns LoopResult
  - Text + tool_calls -> both in assistant message
  - All events emitted (loop.start, turn.start, llm.call, tool.start, tool.end, turn.end, loop.complete)
  - transform_context hook called before each invoke
- [x] **6.2** Create `src/arcrun/strategies/__init__.py` — strategy map, select_strategy
- [x] **6.3** Create `src/arcrun/strategies/react.py` — react_loop function
- [x] **6.4** Verify: all react tests pass

**Completion:** 9/9 tests pass. ReAct loop works end-to-end with mock model
**Lines added:** ~214 (react.py: 179, strategies/__init__.py: 35)

---

## Phase 7: Entry Points (run + run_async)

**Goal:** Wire everything together. Public API works.

### Tasks

- [x] **7.1** Write `tests/test_loop.py`:
  - run() end-to-end: mock model + tools -> LoopResult
  - run() with on_event callback -> events received
  - run() with sandbox -> denials work
  - run() with transform_context -> hook called
  - run() with empty tools -> ValueError
  - run() model error -> exception bubbles up
  - run_async() returns RunHandle
  - RunHandle.result() -> LoopResult
- [x] **7.2** Create `src/arcrun/loop.py` — run(), run_async(), RunHandle
- [x] **7.3** Update `src/arcrun/__init__.py` — wire all exports
- [x] **7.4** Verify: all loop tests pass, `from arcrun import run` works end-to-end

**Completion:** 8/8 tests pass. `await run(model, tools, prompt, task)` works
**Lines added:** ~117

---

## Phase 8: Steering

**Goal:** Steer + followUp + cancel via RunHandle.

### Tasks

- [x] **8.1** Write `tests/test_steering.py`:
  - steer() -> message injected after current tool, remaining tools skipped
  - follow_up() -> message injected at end_turn, loop continues
  - cancel() -> cancel_event set, ToolContext.cancelled.is_set(), partial LoopResult returned
  - Steer during multi-tool response -> only completed tools in result
  - FollowUp with no pending -> normal return
- [x] **8.2** Update `src/arcrun/strategies/react.py` — steer/followUp queue checks (built-in from Phase 6)
- [x] **8.3** Update `src/arcrun/loop.py` — RunHandle steer/follow_up/cancel implementations (built-in from Phase 7)
- [x] **8.4** Verify: all steering tests pass

**Completion:** 6/6 tests pass. Full steering works via RunHandle
**Lines added:** ~0 (steering built into react.py and loop.py from the start)

---

## Phase 9: Integration + Cleanup

**Goal:** End-to-end integration test, line count check, final verification.

### Tasks

- [x] **9.1** Write `tests/test_integration.py`:
  - Full scenario: model reasons, calls tools, gets results, makes more calls, finishes
  - Dynamic tool: add tool mid-execution, verify denied by sandbox
  - Event log completeness: every action has corresponding event
  - Cost/token accumulation: LoopResult totals match individual events
- [x] **9.2** Run full test suite: `pytest -v` — 68/68 pass
- [x] **9.3** Line count audit: 621 lines total (under 1,000 budget)
- [x] **9.4** Type check: `mypy src/arcrun` — clean
- [x] **9.5** Lint: `ruff check src/arcrun` — clean

**Completion:** All tests pass, line count verified, types clean, lint clean
**Lines added:** ~52 (_messages.py extracted during cleanup)

---

## Summary

| Phase | Focus | Est. Lines | Actual Lines |
|-------|-------|------------|-------------|
| 1 | Skeleton + Types | ~90 | ~69 |
| 2 | Event Bus | ~60 | ~42 |
| 3 | Sandbox | ~80 | ~55 |
| 4 | Registry | ~50 | ~43 |
| 5 | RunState | ~60 | ~29 |
| 6 | ReAct Strategy | ~150 | ~214 |
| 7 | Entry Points | ~120 | ~117 |
| 8 | Steering | ~30 | ~0 |
| 9 | Integration | ~0 | ~52 |
| **Total** | | **~640** | **621** |

## Phase Gate Checklist

- [x] `await run(model, tools, prompt, task)` works end-to-end
- [x] ReAct loop calls model.invoke(messages, tools=tools) correctly
- [x] Tool results flow back into messages for next turn
- [x] Every action emits an event
- [x] Sandbox denials work and emit events
- [x] Dynamic tool registry add/remove works
- [x] Steer interrupts correctly
- [x] FollowUp injects at end_turn
- [x] Cancel sets signal and returns partial result
- [x] ToolContext cancel signal propagates to tools
- [x] transform_context hook works
- [x] Under ~640 lines total (621 actual)
- [x] All tests pass (68/68)
- [x] mypy clean
- [x] ruff clean
