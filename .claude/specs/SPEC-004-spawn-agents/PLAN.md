# PLAN: Recursive Agent Spawning

**Spec**: SPEC-004
**Status**: COMPLETE
**Route**: Fast-track (single phase)
**Estimated LOC**: ~150 new lines across 6 files
**Research**: Enriched via /deepen (2026-02-16) — 3 parallel research agents confirmed safety and refined implementation details

---

## Research-Informed Implementation Notes

Key findings from /deepen that inform the implementation:

1. **Recursive `run()` is confirmed safe** — completely isolated state per call, no shared mutables, model objects are reentrant (httpx.AsyncClient is concurrent-safe)
2. **Event bubbling uses existing `on_event` parameter** — no new EventBus plumbing needed. Spawn tool passes bubble handler as `on_event` to child `run()`
3. **Spawn tool injection happens AFTER `_build_state()`** — factory needs model ref (not available in `_build_state()`) and state ref (created by it). Inject between `_build_state()` and `_select_and_emit()`.
4. **Parallel execution: `state.tool_calls_made += 1` is safe** — asyncio single-threaded event loop ensures atomic increment between awaits
5. **Testing requires separate MockModel per run level** — shared `_call_count` collides. Use closure to inject child_model into spawn tool. Real LLM APIs are stateless, so sharing model is fine in production.
6. **Tool result ordering via index-based mapping** — `asyncio.gather` preserves order, but mixed sequential+parallel needs explicit index tracking

---

## Phase 1: Implementation (Single Phase)

### Task 1: Add flat fields to RunState
- [x] Add `depth: int = 0` to `RunState`
- [x] Add `max_depth: int = 3` to `RunState`
- [x] Add `parent_run_id: str = ""` to `RunState`
- [x] Add `token_budget: int | None = None` to `RunState`
- [x] Add `cost_budget: float | None = None` to `RunState`
- [x] Run existing tests to verify no regressions

**Files**: `packages/arcrun/src/arcrun/state.py` (edit)

### Task 2: Add depth/max_depth params to run()/run_async()
- [x] Add `depth: int = 0` parameter to `_build_state()`
- [x] Add `max_depth: int = 3` parameter to `_build_state()`
- [x] Pass to `RunState` constructor
- [x] Add `depth: int = 0` parameter to `run()` and `run_async()`
- [x] Add `max_depth: int = 3` parameter to `run()` and `run_async()`
- [x] Thread through to `_build_state()`
- [x] Run existing tests to verify no regressions

**Files**: `packages/arcrun/src/arcrun/loop.py` (edit)

### Task 3: Create spawn tool factory
- [x] Create `packages/arcrun/src/arcrun/builtins/spawn.py`
- [x] Implement `make_spawn_tool(*, model, tools, system_prompt, state, sandbox?, allowed_strategies?)` factory
- [x] Factory captures parent context via closure (same pattern as `make_execute_tool`)
- [x] Tool execute function: validates `state.depth < state.max_depth` (reject with error string)
- [x] Tool execute function: resolves tool subset from parent tools by name (filter, don't expand)
- [x] Tool execute function: creates `_make_bubble_handler(child_run_id, state.event_bus)` for event propagation
- [x] Tool execute function: calls `run(model, resolved_tools, child_prompt, child_task, depth=state.depth+1, max_depth=state.max_depth, on_event=bubble_handler)` — uses existing `on_event` parameter, no new EventBus plumbing
- [x] Tool execute function: returns `result.content or "(no content)"` on success
- [x] Tool execute function: returns `f"Error: {type(exc).__name__}: {str(exc)[:200]}"` on exception
- [x] Tool schema: `task` (required string), `system_prompt` (optional string), `tools` (optional array of strings)
- [x] `_make_bubble_handler()`: emits `f"child.{child_run_id}.{event.type}"` on parent bus with `child_run_id` in data payload

**Files**: `packages/arcrun/src/arcrun/builtins/spawn.py` (new)

### Task 4: Inject spawn tool in loop
- [x] In `run()`, AFTER `_build_state()` returns and BEFORE `_select_and_emit()`: inject spawn_task if `state.depth < state.max_depth`
- [x] Call `make_spawn_tool(model=model, tools=tools, system_prompt=system_prompt, state=state, sandbox=sandbox, allowed_strategies=allowed_strategies)`
- [x] Add to registry via `state.registry.add(spawn_tool)`
- [x] Same injection in `run_async()`
- [x] Do NOT inject if `state.depth >= state.max_depth` (model won't see spawn_task in tools)

**Files**: `packages/arcrun/src/arcrun/loop.py` (edit)

### Task 5: Parallel tool execution for spawns
- [x] Refactor react.py tool call processing (lines 100-118) to use index-based mapping
- [x] Partition: non-spawn tools execute sequentially (current behavior), spawn_task calls queued
- [x] After sequential processing, execute queued spawn_task calls via `asyncio.gather(*coros, return_exceptions=True)`
- [x] Use `return_exceptions=True` so one spawn failure doesn't kill siblings
- [x] Handle exceptions: `isinstance(result, tuple)` for success, else error
- [x] If steered during sequential phase, cancel queued spawns with "operation cancelled: steered"
- [x] Check steer queue after all parallel spawns complete
- [x] Reconstruct ordered results via `sorted(tool_results_map.keys())`
- [x] Append ordered results to `state.messages`

**Files**: `packages/arcrun/src/arcrun/strategies/react.py` (edit)

### Task 6: Export and wire up
- [x] Add `make_spawn_tool` to `arcrun/builtins/__init__.py` imports and `__all__`
- [x] Add `make_spawn_tool` to `arcrun/__init__.py` imports and `__all__`
- [x] Run full test suite

**Files**: `packages/arcrun/src/arcrun/builtins/__init__.py` (edit), `packages/arcrun/src/arcrun/__init__.py` (edit)

### Task 7: Tests (unit + integration)

**Unit tests** (`test_spawn_tool.py`):
- [x] Test: factory returns Tool with correct name, description, schema
- [x] Test: schema has `task` required, `system_prompt` and `tools` optional
- [x] Test: depth limit rejection returns error string when depth >= max_depth
- [x] Test: tool subsetting filters parent tools by name
- [x] Test: unknown tool name in subset returns error (not silently ignored)
- [x] Test: bubble handler emits prefixed events on parent bus

**Integration tests** (`test_spawn_integration.py`):
- [x] Test: single spawn — parent model calls spawn_task, child_model returns result, parent continues (separate MockModel instances per level — shared `_call_count` breaks otherwise)
- [x] Test: nested spawn — parent -> child -> grandchild (depth 0 -> 1 -> 2, each with own MockModel)
- [x] Test: depth limit — spawn_task absent from registry at max_depth; verify model doesn't see it
- [x] Test: parallel spawn — 2 spawn_task calls in one turn, both execute via asyncio.gather, both results returned in correct order
- [x] Test: event bubbling — child events appear on parent bus with `child.{child_run_id}.{event.type}` pattern, `child_run_id` in data payload
- [x] Test: child failure — child exception returns `"Error: ..."` as tool result string, parent continues
- [x] Test: tool subsetting — child only gets named tools, spawn_task still injected if depth allows
- [x] Test: system_prompt override — child uses provided system_prompt, not parent's
- [x] Run `ruff check` and `ruff format`

**Files**: `packages/arcrun/tests/test_spawn_tool.py` (new), `packages/arcrun/tests/test_spawn_integration.py` (new)

---

## Completion Criteria

- [x] All 7 tasks complete
- [x] All existing tests pass (0 regressions)
- [x] All new spawn tests pass
- [x] Linter clean (no new issues)
- [x] 0 new dependencies
- [x] Core LOC still under 3,500

**Total tasks**: 7
**Completed**: 7
**Remaining**: 0
