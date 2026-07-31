# PRD: Module Decoupling

**Spec**: SPEC-003
**Status**: PENDING

## Problem

Core (`arcagent/core/`) contains module-specific code that should live in the modules themselves:

1. **Config coupling** — `MemoryConfig`, `SchedulerConfig` defined in `core/config.py` and embedded in `ArcAgentConfig`. Adding/removing a module requires editing core.
2. **Error coupling** — `AgentMemoryError`, `EntityExtractionError`, `SearchError`, `PolicyEvalError` defined in `core/errors.py`. Module-specific errors pollute core's error hierarchy.
3. **Prompt coupling** — `context_manager.py` hardcodes `policy.md` in `_PROMPT_FILES` and `notes`, `memory_guidance`, `policy` in `_SECTION_ORDER`.
4. **Policy entanglement** — `PolicyEngine` lives inside the memory module as a sub-component. It's a separate concern (adaptation) that should be independently loadable/disablable.
5. **Comment coupling** — `agent.py` and `module_loader.py` contain memory-specific comments that reveal assumptions about which modules exist.

## Goal

After this change:
- Disabling `[modules.memory]` in TOML removes all memory functionality with zero core changes
- Disabling `[modules.policy]` in TOML removes all policy functionality independently
- Adding a new module never requires editing any core file
- Module configs, errors, and logic are fully self-contained

## Requirements

### R1: Module-Owned Configs

- **R1.1**: `MemoryConfig` moves to `modules/memory/config.py`
- **R1.2**: `SchedulerConfig` moves to `modules/scheduler/config.py`
- **R1.3**: New `PolicyConfig` created at `modules/policy/config.py`
- **R1.4**: Root `ArcAgentConfig` no longer has `memory`, `scheduler` fields
- **R1.5**: Module loader passes `ModuleEntry.config` dict; modules validate internally
- **R1.6**: TOML config moves from `[memory]` to `[modules.memory.config]`

### R2: Module-Owned Errors

- **R2.1**: `AgentMemoryError`, `EntityExtractionError`, `SearchError` move to `modules/memory/errors.py`
- **R2.2**: `PolicyEvalError` moves to `modules/policy/errors.py`
- **R2.3**: Core `errors.py` retains only core component errors

### R3: Policy as Independent Module

- **R3.1**: `PolicyEngine` moves from `modules/memory/` to `modules/policy/`
- **R3.2**: New `PolicyModule` (bus subscriber) handles lifecycle: subscribes to `agent:post_respond`, `agent:assemble_prompt`, `agent:shutdown`
- **R3.3**: `MODULE.yaml` for policy module with proper event declarations
- **R3.4**: Memory module no longer references policy at all (no imports, no turn counting for policy eval, no session message accumulation for policy)

### R4: Dynamic Prompt Assembly

- **R4.1**: `_PROMPT_FILES` contains only `identity.md` and `context.md` (core files)
- **R4.2**: `_SECTION_ORDER` removed; replaced with dynamic ordering (identity first, context last, rest sorted alphabetically)
- **R4.3**: Policy module injects `policy.md` content via `agent:assemble_prompt` event
- **R4.4**: Memory module continues injecting `notes` and `memory_guidance` via the same event

### R5: Core Cleanup

- **R5.1**: `module_loader.py` uses `ModuleEntry.config` dict instead of `getattr(ctx.config, manifest.name)`
- **R5.2**: `protocols.py` docstrings cleaned of module-specific references
- **R5.3**: `agent.py` comments cleaned of memory-specific references
- **R5.4**: `module_loader.py` comment about memory cleaned

### R6: Test Updates

- **R6.1**: All test imports updated to new module locations
- **R6.2**: Memory error tests moved from `test_errors.py` to module test directory
- **R6.3**: All existing tests pass after refactoring

## Non-Requirements

- No behavioral changes — all functionality stays identical
- No new features
- No TOML schema versioning (this is pre-1.0)
- `EvalConfig` stays in core (shared infrastructure)
- `HybridSearch._discover_files` stays as-is (filesystem-driven, not import-driven)

## Success Criteria

1. `ruff check .` passes with 0 errors
2. `mypy arcagent/ --strict` passes
3. All existing tests pass with updated imports
4. Disabling memory module in config: agent starts, runs, and shuts down without errors
5. Disabling policy module in config: agent starts, runs, and shuts down without errors
6. No `from arcagent.core.config import MemoryConfig` or `SchedulerConfig` anywhere
7. No `from arcagent.core.errors import AgentMemoryError` (or subclasses) anywhere
8. `core/config.py` has zero module-specific config classes
9. `core/errors.py` has zero module-specific error classes
10. `context_manager.py` has zero hardcoded module names
