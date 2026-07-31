# PLAN: CodeExec — Strategy Selection + Code Execution (003)

**Status:** COMPLETE
**Actual Lines:** 910 total (+288 from 622 baseline)
**Prior Work:** brainstorm (research-enriched) + build decisions

## Phase 1: Strategy ABC + ReactStrategy Refactor

**Goal:** Formalize strategy interface. ReactStrategy wraps existing react_loop. Zero behavior change. All existing tests pass.

### Tasks

- [x] **1.1** Write `tests/test_strategy_abc.py` — Strategy ABC is abstract (can't instantiate), ReactStrategy has name/description, ReactStrategy.__call__ produces LoopResult
- [x] **1.2** Add `Strategy` ABC to `src/arcrun/strategies/__init__.py` — abstract name, description, __call__
- [x] **1.3** Add `ReactStrategy` class to `src/arcrun/strategies/react.py` — wraps react_loop
- [x] **1.4** Update `_load_strategies()` to register `ReactStrategy()` instance instead of function
- [x] **1.5** Update `src/arcrun/__init__.py` — export `Strategy`
- [x] **1.6** Verify: all new tests pass, all 81 existing tests pass (zero regression) — 89 total

**Gate:** ReactStrategy produces identical behavior. `from arcrun import Strategy` works.

---

## Phase 2: ExecuteTool

**Goal:** `make_execute_tool()` creates a working tool that runs Python in a subprocess.

### Tasks

- [x] **2.1** Write `tests/test_execute_tool.py` — factory returns Tool with correct name/schema, simple code execution (print + stdout), stderr capture, exit code on failure, timeout handling, output truncation, structured JSON result format, minimal env (no HOME leak), temp dir isolation
- [x] **2.2** Create `src/arcrun/builtins/__init__.py` — export `make_execute_tool`
- [x] **2.3** Create `src/arcrun/builtins/execute.py` — `make_execute_tool` factory
- [x] **2.4** Update `src/arcrun/__init__.py` — export `make_execute_tool`
- [x] **2.5** Verify: all execute tests pass, runs through executor pipeline (sandbox + schema + events) — 103 total

**Gate:** `make_execute_tool()` returns a Tool. Code runs in subprocess. Structured JSON result returned. Timeout kills process group.

---

## Phase 3: CodeExec Strategy

**Goal:** `CodeExecStrategy` augments system prompt and delegates to react_loop.

### Tasks

- [x] **3.1** Write `tests/test_code_strategy.py` — CodeExecStrategy has name "code" and description, __call__ augments system message, delegates to react loop, custom prefix override works, emits code.prompt.augmented event
- [x] **3.2** Create `src/arcrun/strategies/code.py` — `CodeExecStrategy` class
- [x] **3.3** Update `_load_strategies()` to register `CodeExecStrategy()`
- [x] **3.4** Verify: CodeExecStrategy produces results through react_loop with augmented prompt — 110 total

**Gate:** CodeExecStrategy wraps react_loop. System prompt is augmented. Event emitted. Custom prefix works.

---

## Phase 4: Model-Based Strategy Selection

**Goal:** When multiple strategies are allowed, model picks via tool calling.

### Tasks

- [x] **4.1** Write `tests/test_strategy_selection.py` — None -> "react" (no call), single -> direct (no call), multiple -> model picks (one call), invalid model output -> fallback to "react", selection events emitted, model sees strategy descriptions + tool names + task
- [x] **4.2** Update `select_strategy()` in `src/arcrun/strategies/__init__.py` — model-based selection with tool calling
- [x] **4.3** Verify: selection works end-to-end, fallback works, events correct — 120 total

**Gate:** Strategy selection works for all cases. Fallback to "react" on failure. Events emitted.

---

## Phase 5: Integration + E2E

**Goal:** Full integration test: run() with multiple strategies, ExecuteTool, CodeExec, and selection.

### Tasks

- [x] **5.1** Write `tests/test_codeexec_integration.py` — run() with allowed_strategies=["react", "code"] selects strategy, CodeExec + ExecuteTool end-to-end (model writes code, gets result), sandbox denies execute_python when not in allowlist, event completeness (all new events present), existing integration tests still pass
- [x] **5.2** Update `tests/conftest.py` if needed — no changes needed, MockModel already sufficient
- [x] **5.3** Final line count verification — 910 lines total
- [x] **5.4** Verify: ALL tests pass (existing + new), no regressions — 126 total

**Gate (Phase 2 Complete):**
- [x] `make_execute_tool()` creates a working ExecuteTool
- [x] ExecuteTool runs Python in subprocess with temp file + temp dir + minimal env
- [x] ExecuteTool returns structured JSON result
- [x] ExecuteTool respects timeout with two-phase shutdown
- [x] SandboxConfig.check gates ExecuteTool like any other tool
- [x] Strategy ABC defines name, description, __call__
- [x] ReactStrategy wraps existing react_loop (zero behavior change)
- [x] CodeExecStrategy augments system prompt, delegates to react_loop
- [x] Strategy selection: single -> direct, multiple -> model picks
- [x] All actions emit events
- [x] All existing tests still pass (zero regression)
