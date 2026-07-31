# PLAN: Module Decoupling

**Spec**: SPEC-003
**Status**: COMPLETE
**Phases**: 4

---

## Phase 1: Create Module-Owned Configs and Errors

> Foundation files that other phases depend on. No existing code modified yet.

- [x] 1.1 Create `modules/memory/config.py` with `MemoryConfig` (fields from core minus `policy_eval_interval_turns`)
- [x] 1.2 Create `modules/memory/errors.py` with `AgentMemoryError`, `EntityExtractionError`, `SearchError`
- [x] 1.3 Create `modules/policy/config.py` with `PolicyConfig` (`eval_interval_turns`, `max_bullets`, `max_bullet_text_length`)
- [x] 1.4 Create `modules/policy/errors.py` with `PolicyEvalError`
- [x] 1.5 Create `modules/scheduler/config.py` with `SchedulerConfig` (same fields as core version)

**Verify**: All new files import correctly (`python -c "from arcagent.modules.memory.config import MemoryConfig"` etc.)

---

## Phase 2: Create Policy Module

> New module with its own lifecycle. Extracts policy logic from memory.

- [x] 2.1 Create `modules/policy/policy_engine.py` -- copy from `modules/memory/policy_engine.py`, update imports to use `PolicyConfig` and local errors, make `max_bullets`/`max_bullet_text_length` configurable from `PolicyConfig`
- [x] 2.2 Create `modules/policy/policy_module.py` -- new `PolicyModule` bus subscriber:
  - Constructor: `config: dict | None`, `eval_config`, `telemetry`, `workspace`, `llm_config`
  - `startup()`: subscribe to `agent:post_respond` (110), `agent:assemble_prompt` (60), `agent:shutdown` (60)
  - `_on_post_respond`: track turn count, fire policy eval at interval
  - `_on_assemble_prompt`: inject `policy.md` content
  - `_on_shutdown`: final policy eval
  - Background task spawner with semaphore/timeout/backpressure
- [x] 2.3 Create `modules/policy/MODULE.yaml`
- [x] 2.4 Create `modules/policy/__init__.py`

**Verify**: `python -c "from arcagent.modules.policy import PolicyModule"` succeeds

---

## Phase 3: Update Existing Modules

> Rewire memory, scheduler, and their imports to use module-owned configs/errors.

### Memory Module
- [x] 3.1 Update `modules/memory/markdown_memory.py`:
  - Import `MemoryConfig` from `modules/memory/config` (not core)
  - Change constructor `config` param to `dict[str, Any] | None`, validate internally
  - Remove `PolicyEngine` import and instantiation
  - Remove `self._session_messages`, `self._turn_count`
  - Remove `_on_shutdown` handler and its `agent:shutdown` subscription
  - Remove policy eval block from `_on_post_respond`
  - Remove `policy_eval_interval_turns` usage
- [x] 3.2 Update `modules/memory/hybrid_search.py` -- import `MemoryConfig` from local config
- [x] 3.3 Update `modules/memory/__init__.py` -- remove all policy exports, add config/error exports
- [x] 3.4 Delete `modules/memory/policy_engine.py` (moved to policy module)

### Scheduler Module
- [x] 3.5 Update `modules/scheduler/__init__.py` -- import `SchedulerConfig` from local config, change constructor to accept `dict`
- [x] 3.6 Update `modules/scheduler/scheduler.py` -- import `SchedulerConfig` from local config
- [x] 3.7 Update `modules/scheduler/tools.py` -- import `SchedulerConfig` from local config

**Verify**: `python -c "from arcagent.modules.memory import MarkdownMemoryModule"` and `"from arcagent.modules.scheduler import SchedulerModule"` succeed

---

## Phase 4: Update Core and Tests

> Final cleanup. Core becomes module-agnostic.

### Core
- [x] 4.1 Update `core/config.py`:
  - Remove `MemoryConfig` class
  - Remove `SchedulerConfig` class
  - Remove `memory: MemoryConfig = MemoryConfig()` from `ArcAgentConfig`
  - Remove `scheduler: SchedulerConfig = SchedulerConfig()` from `ArcAgentConfig`
- [x] 4.2 Update `core/errors.py`:
  - Remove `AgentMemoryError`, `EntityExtractionError`, `SearchError`, `PolicyEvalError`
- [x] 4.3 Update `core/context_manager.py`:
  - `_PROMPT_FILES` -> `_CORE_PROMPT_FILES = ["identity.md", "context.md"]`
  - Remove `_SECTION_ORDER` constant
  - Replace ordered assembly with: identity first, context last, rest sorted alphabetically
- [x] 4.4 Update `core/module_loader.py`:
  - `_instantiate()`: use `ctx.config.modules.get(manifest.name).config` instead of `getattr(ctx.config, manifest.name)`
  - Remove memory-specific comment on line 167
- [x] 4.5 Update `core/protocols.py` -- remove "EntityExtractor, PolicyEngine" from `EvalModelProtocol` docstring
- [x] 4.6 Update `core/agent.py`:
  - Remove comment "Include messages and session_id for memory module" (line 312)
  - Clean any other module-specific comments

### Tests
- [x] 4.7 Update `tests/unit/core/test_config.py` -- remove `TestMemoryConfig` class and memory config tests (config no longer in core)
- [x] 4.8 Update `tests/unit/core/test_errors.py` -- remove `TestAgentMemoryError`, `TestEntityExtractionError`, `TestSearchError`, `TestPolicyEvalError`
- [x] 4.9 Update `tests/unit/modules/memory/test_*.py` -- change all `from arcagent.core.config import MemoryConfig` to `from arcagent.modules.memory.config import MemoryConfig`; remove unused MemoryConfig imports where config changed to `{}`
- [x] 4.10 Move `tests/unit/modules/memory/test_policy_engine.py` to `tests/unit/modules/policy/`, update imports; delete old file
- [x] 4.11 Update `tests/unit/modules/memory/test_memory_wiring.py` -- remove policy-related tests, update config imports
- [x] 4.12 Update `tests/unit/modules/memory/test_markdown_memory.py` -- update config imports, remove `TestOnShutdownModelNone`
- [x] 4.13 Update `tests/unit/modules/memory/test_memory_guidance.py` -- update config imports
- [x] 4.14 Update `tests/integration/test_memory_wiring.py` -- update config imports
- [x] 4.15 Update `tests/integration/test_memory_integration.py` -- update config imports
- [x] 4.16 Verify `tests/unit/core/test_module_loader.py` -- clean, no stale imports
- [x] 4.17 Verify `tests/integration/test_agent_integration.py` -- clean, no stale imports
- [x] 4.18 Create `tests/unit/modules/policy/__init__.py`

**Verify**:
1. `ruff check packages/arcagent/` -- no new errors (pre-existing only)
2. `pytest packages/arcagent/tests/ -x` -- 709 unit + 51 integration pass
3. Grep verification: `grep -r "from arcagent.core.config import MemoryConfig" packages/arcagent/` returns 0 results
4. Grep verification: `grep -r "from arcagent.core.config import SchedulerConfig" packages/arcagent/` returns 0 results
5. Grep verification: `grep -r "from arcagent.core.errors import.*AgentMemoryError" packages/arcagent/` returns 0 results
6. Grep verification: `grep -r "from arcagent.modules.memory.policy_engine import" packages/arcagent/` returns 0 results

---

## Completion Criteria

- [x] All 4 phases complete
- [x] All existing tests pass (709 unit + 51 integration, excluding pre-existing failures)
- [x] Zero module-specific imports in core
- [x] Zero hardcoded module names in context_manager
- [x] Policy module loadable/disablable independently
- [x] Memory module loadable/disablable independently
