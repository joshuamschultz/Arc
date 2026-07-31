# PLAN: Memory Wiring

**Spec ID**: S004
**Status**: COMPLETE
**Last Updated**: 2026-02-15

---

## Overview

Wire up ArcAgent's memory system in 6 phases, dependency-ordered. TDD throughout. Each phase is independently testable. Total estimated: ~250 new LOC, ~80 modified LOC.

## Phases

### Phase 1: ModuleContext + Module Protocol (MW-004)
### Phase 2: Convention Module Loader (MW-002, MW-007)
### Phase 3: Eval Model + memory_search Wiring (MW-001, MW-003, MW-011, MW-012)
### Phase 4: Memory Guidance + Config (MW-009, MW-010)
### Phase 5: Session-Owns-Context + Compaction Trigger (MW-006, MW-008)
### Phase 6: Integration Test + Quality Gates (MW-016)

---

## Phase 1: ModuleContext + Module Protocol

> Create ModuleContext dataclass and update Module protocol. Unblocks all other phases.

- [x] **T1.1** Create ModuleContext dataclass `[activity: core-development]`
  - [x] T1.1.1 Write test: ModuleContext creation with all fields
  - [x] T1.1.2 Write test: ModuleContext is frozen (cannot mutate attributes)
  - [x] T1.1.3 Write test: ModuleContext fields are accessible
  - [x] T1.1.4 Add `ModuleContext` dataclass to `arcagent/core/module_bus.py`
  - [x] T1.1.5 Verify: `pytest tests/unit/core/test_module_context.py` passes
  - [x] T1.1.6 Verify: `mypy arcagent/core/module_bus.py --strict`
  - _Requirements: REQ-W-07_
  - _Design: SDD Section 2.1_

- [x] **T1.2** Update Module protocol and ModuleBus.startup() `[activity: core-development]`
  - [x] T1.2.1 Write test: Module.startup receives ModuleContext
  - [x] T1.2.2 Write test: ModuleBus.startup(ctx) passes ctx to each module
  - [x] T1.2.3 Change `Module.startup(bus)` to `Module.startup(ctx: ModuleContext)` in protocol
  - [x] T1.2.4 Change `ModuleBus.startup()` to `ModuleBus.startup(ctx: ModuleContext)` -- iterate modules with `await module.startup(ctx)`
  - [x] T1.2.5 Update agent.py: pass ModuleContext to `bus.startup(ctx)`
  - [x] T1.2.6 Update MarkdownMemoryModule.startup() signature (use `ctx.bus` for subscriptions)
  - [x] T1.2.7 Fix all existing tests that call `bus.startup()` or `module.startup(bus)`
  - [x] T1.2.8 Verify: `pytest` full suite passes (no regressions)
  - [x] T1.2.9 Verify: `mypy arcagent/ --strict`
  - _Requirements: REQ-W-08_
  - _Design: SDD Section 3.1_

**Phase 1 gate**: All existing 423+ tests pass. ModuleContext exists. Protocol updated.

---

## Phase 2: Convention Module Loader

> Replace hardcoded _register_modules() with convention-based discovery. Unblocks module drop-in.

- [x] **T2.1** Create ModuleManifest Pydantic model `[activity: core-development]`
  - [x] T2.1.1 Write test: ModuleManifest from valid MODULE.yaml dict
  - [x] T2.1.2 Write test: ModuleManifest raises on missing `name`
  - [x] T2.1.3 Write test: ModuleManifest raises on missing `entry_point`
  - [x] T2.1.4 Write test: ModuleManifest uses defaults for optional fields
  - [x] T2.1.5 Implement ModuleManifest in `arcagent/core/module_loader.py`
  - [x] T2.1.6 Verify: `pytest tests/unit/core/test_module_loader.py` passes
  - _Requirements: REQ-W-05, REQ-W-06_
  - _Design: SDD Section 2.2_

- [x] **T2.2** Implement ModuleLoader.discover() `[activity: core-development]`
  - [x] T2.2.1 Write test: discover() finds MODULE.yaml in subdirectories
  - [x] T2.2.2 Write test: discover() skips directories without MODULE.yaml
  - [x] T2.2.3 Write test: discover() filters by config `[modules.{name}] enabled`
  - [x] T2.2.4 Write test: discover() skips disabled modules
  - [x] T2.2.5 Write test: discover() logs WARNING for malformed optional fields
  - [x] T2.2.6 Write test: discover() raises ConfigError for missing entry_point
  - [x] T2.2.7 Implement discover() method
  - [x] T2.2.8 Verify: tests pass
  - _Requirements: REQ-W-03, REQ-W-04_
  - _Design: SDD Section 2.2_

- [x] **T2.3** Implement ModuleLoader.load() and load_all() `[activity: core-development]`
  - [x] T2.3.1 Write test: load() imports entry_point and instantiates module
  - [x] T2.3.2 Write test: load() handles import error gracefully (logs, skips)
  - [x] T2.3.3 Write test: load_all() returns list of loaded modules
  - [x] T2.3.4 Implement load() and load_all()
  - [x] T2.3.5 Verify: tests pass
  - _Requirements: REQ-W-03_
  - _Design: SDD Section 2.2_

- [x] **T2.4** Replace _register_modules() in agent.py `[activity: core-development]`
  - [x] T2.4.1 Write test: agent startup uses ModuleLoader instead of _register_modules
  - [x] T2.4.2 Replace step 11 in agent.py startup() with convention loader
  - [x] T2.4.3 Delete _register_modules() method
  - [x] T2.4.4 Verify: `pytest` full suite passes
  - [x] T2.4.5 Verify: `mypy arcagent/ --strict`
  - _Requirements: REQ-W-22_
  - _Design: SDD Section 3.4_

**Phase 2 gate**: Convention loader works. _register_modules() deleted. Memory module still loads via convention.

---

## Phase 3: Eval Model + memory_search Wiring

> Fix the two biggest gaps: eval model initialization and search tool registration.

- [x] **T3.1** Eval model lazy init with fallback (MW-001, MW-012) `[activity: core-development]`
  - [x] T3.1.1 Write test: _get_eval_model() creates model from EvalConfig when provider+model set
  - [x] T3.1.2 Write test: _get_eval_model() falls back to llm_config.model when EvalConfig is empty
  - [x] T3.1.3 Write test: _get_eval_model() caches model after first call (lazy)
  - [x] T3.1.4 Write test: _on_post_respond() no longer returns early (calls _get_eval_model)
  - [x] T3.1.5 Add _get_eval_model() to MarkdownMemoryModule
  - [x] T3.1.6 Store llm_config from ModuleContext in startup()
  - [x] T3.1.7 Change _on_post_respond: `model = self._eval_model` -> `model = self._get_eval_model()`
  - [x] T3.1.8 Verify: `pytest tests/unit/modules/memory/` passes
  - _Requirements: REQ-W-01, REQ-W-02_
  - _Design: SDD Section 3.2.1_

- [x] **T3.2** Register memory_search tool (MW-003) `[activity: core-development]`
  - [x] T3.2.1 Write test: startup() registers memory_search in tool_registry
  - [x] T3.2.2 Write test: memory_search tool accepts query, scope, date_from, date_to
  - [x] T3.2.3 Write test: memory_search handler calls HybridSearch.search()
  - [x] T3.2.4 Add _register_search_tool() and _handle_memory_search() to MarkdownMemoryModule
  - [x] T3.2.5 Call _register_search_tool(ctx.tool_registry) in startup()
  - [x] T3.2.6 Verify: tests pass
  - _Requirements: REQ-W-09, REQ-W-10_
  - _Design: SDD Section 3.2.4_

- [x] **T3.3** Add date filtering to HybridSearch (MW-011) `[activity: core-development]`
  - [x] T3.3.1 Write test: search() with date_from filters results
  - [x] T3.3.2 Write test: search() with date_to filters results
  - [x] T3.3.3 Write test: search() with both date_from and date_to
  - [x] T3.3.4 Write test: NULL created_date records always included
  - [x] T3.3.5 Add created_date UNINDEXED column to FTS5 schema
  - [x] T3.3.6 Add date_from/date_to params to search() method
  - [x] T3.3.7 Update SQL query with date WHERE clause
  - [x] T3.3.8 Verify: `pytest tests/unit/modules/memory/test_hybrid_search.py` passes
  - _Requirements: REQ-W-11_
  - _Design: SDD Section 3.3_

- [x] **T3.4** Semaphore-limited background tasks (MW-015) `[activity: core-development]`
  - [x] T3.4.1 Write test: _spawn_background respects semaphore limit
  - [x] T3.4.2 Write test: concurrent tasks beyond limit are queued
  - [x] T3.4.3 Add asyncio.Semaphore to MarkdownMemoryModule init
  - [x] T3.4.4 Wrap _spawn_background coroutine with semaphore acquire
  - [x] T3.4.5 Verify: tests pass
  - _Requirements: REQ-W-20_
  - _Design: SDD Section 3.2 (existing _spawn_background)_

**Phase 3 gate**: Eval model initializes. memory_search is a callable tool. Date filtering works. Background tasks are semaphore-limited.

---

## Phase 4: Memory Guidance + Config

> Tell the agent about its memory capabilities. Enable memory by default.

- [x] **T4.1** Module-injected memory guidance (MW-009) `[activity: core-development]`
  - [x] T4.1.1 Write test: _on_assemble_prompt injects memory_guidance section
  - [x] T4.1.2 Write test: guidance NOT injected when identity.md has `## Memory`
  - [x] T4.1.3 Write test: default guidance text contains memory_search and tool instructions
  - [x] T4.1.4 Add _default_memory_guidance() method to MarkdownMemoryModule
  - [x] T4.1.5 Update _on_assemble_prompt to check identity and inject if no override
  - [x] T4.1.6 Verify: tests pass
  - _Requirements: REQ-W-12, REQ-W-13_
  - _Design: SDD Section 3.2.5_

- [x] **T4.2** Enable memory in Basic_Agent config (MW-010) `[activity: core-development]`
  - [x] T4.2.1 Add `[modules.memory] enabled = true` to `Basic_Agent/arcagent.toml`
  - [x] T4.2.2 Set `entity_extraction_enabled = true` (was false)
  - [x] T4.2.3 Verify: agent startup log shows "Registered memory module"
  - _Requirements: REQ-W-14, REQ-W-15_
  - _Design: SDD Section 3.6_

**Phase 4 gate**: Agent knows about memory. Config enables it by default.

---

## Phase 5: Session-Owns-Context + Compaction Trigger

> Wire session to own context and trigger compaction at threshold.

- [x] **T5.1** Session-owns-context wiring (MW-008) `[activity: core-development]`
  - [x] T5.1.1 Write test: SessionManager accepts context_manager parameter
  - [x] T5.1.2 Write test: session.token_ratio() delegates to context_manager
  - [x] T5.1.3 Write test: session.context_manager property returns the context manager
  - [x] T5.1.4 Add context_manager param to SessionManager.__init__()
  - [x] T5.1.5 Add token_ratio() method to SessionManager
  - [x] T5.1.6 Update agent.py: pass context_manager to SessionManager constructor
  - [x] T5.1.7 Verify: existing session tests pass
  - _Requirements: REQ-W-21_
  - _Design: SDD Section 3.5_

- [x] **T5.2** Compaction trigger in chat() (MW-006) `[activity: core-development]`
  - [x] T5.2.1 Write test: chat() calls _maybe_compact after assistant message
  - [x] T5.2.2 Write test: _maybe_compact triggers compaction when ratio >= 0.85
  - [x] T5.2.3 Write test: _maybe_compact does nothing when ratio < 0.85
  - [x] T5.2.4 Write test: compaction uses eval model (lazy init)
  - [x] T5.2.5 Add _maybe_compact() method to ArcAgent
  - [x] T5.2.6 Add _get_eval_model() to ArcAgent (for compaction summarization)
  - [x] T5.2.7 Call _maybe_compact after append in chat()
  - [x] T5.2.8 Verify: `pytest` full suite passes
  - _Requirements: REQ-W-16, REQ-W-17, REQ-W-19_
  - _Design: SDD Section 2.3_

**Phase 5 gate**: Session owns context. Compaction triggers at 0.85 threshold.

---

## Phase 6: Integration Test + Quality Gates

> Verify the full wiring works end-to-end. Pass all quality gates.

- [x] **T6.1** Integration test: memory wiring flow `[activity: integration-testing]`
  - [x] T6.1.1 Write test: agent startup -> convention loader -> memory module registered
  - [x] T6.1.2 Write test: tool_registry contains memory_search after startup
  - [x] T6.1.3 Write test: chat() -> post_respond fires -> entity extraction task created
  - [x] T6.1.4 Write test: memory_search returns results from notes
  - [x] T6.1.5 Write test: compaction triggers at threshold, pre-flush writes to context.md
  - [x] T6.1.6 Run full integration test
  - _Requirements: REQ-W-01 through REQ-W-22_
  - _Design: SDD Section 7_

- [x] **T6.2** Quality gates `[activity: core-development]`
  - [x] T6.2.1 Verify: `pytest --cov=arcagent` -- all tests pass, coverage >= 80%
  - [x] T6.2.2 Verify: `mypy arcagent/ --strict` -- 0 errors
  - [x] T6.2.3 Verify: `ruff check .` -- 0 errors
  - [x] T6.2.4 Verify: `ruff format --check .` -- no formatting issues
  - [x] T6.2.5 Verify: all 423+ existing tests still pass (zero regression)
  - [x] T6.2.6 Verify: core LOC < 3,500 (new module_loader.py adds ~120 LOC)
  - _Requirements: NFR-W-05, NFR-W-06, NFR-W-07_

**Phase 6 gate**: All tests pass. Quality gates green. Memory is functional.

---

## Completion Counts

| Phase | Tasks | Subtasks | Status |
|-------|-------|----------|--------|
| Phase 1 | 2 | 18 | COMPLETE |
| Phase 2 | 4 | 20 | COMPLETE |
| Phase 3 | 4 | 21 | COMPLETE |
| Phase 4 | 2 | 6 | COMPLETE |
| Phase 5 | 2 | 15 | COMPLETE |
| Phase 6 | 2 | 11 | COMPLETE |
| **Total** | **16** | **91** | **COMPLETE** |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Module protocol change breaks existing tests | High | Medium | Fix in T1.2.7, run full suite after each change |
| Convention loader misses edge cases | Low | Medium | Comprehensive unit tests in T2.2 |
| Eval model fallback fails at runtime | Medium | High | Test fallback chain explicitly in T3.1 |
| Compaction race condition | Low | High | asyncio.Lock already in SessionManager.compact() |
| FTS5 UNINDEXED column breaks existing search | Low | Medium | Separate migration path for existing DBs |
