# PLAN: Kimi (Moonshot AI) Provider Support

**Spec**: SPEC-001
**Status**: COMPLETE
**Route**: Fast-track (single phase)
**Estimated LOC**: ~45 new lines across 4 files

---

## Phase 1: Implementation (Single Phase)

### Task 1: Create provider TOML config
- [x] Create `packages/arcllm/src/arcllm/providers/moonshot.toml`
- [x] Include `[provider]` section with Moonshot API settings
- [x] Include `[models.kimi-k2]` with 128K context, tool support
- [x] Include `[models.kimi-k2.5]` with 128K context, tool + vision support
- [x] Verify TOML loads: `load_provider_config("moonshot")`

**Files**: `packages/arcllm/src/arcllm/providers/moonshot.toml` (new)

### Task 2: Create adapter module
- [x] Create `packages/arcllm/src/arcllm/adapters/moonshot.py`
- [x] Thin alias: inherit `OpenaiAdapter`, override `name` property
- [x] Follow exact pattern of `deepseek.py` / `groq.py`

**Files**: `packages/arcllm/src/arcllm/adapters/moonshot.py` (new)

### Task 3: Register lazy import
- [x] Add `"MoonshotAdapter": "arcllm.adapters.moonshot"` to `_LAZY_IMPORTS`
- [x] Add `"MoonshotAdapter"` to `__all__`

**Files**: `packages/arcllm/src/arcllm/__init__.py` (edit)

### Task 4: Add to test parametrization
- [x] Add `("moonshot", "MOONSHOT_API_KEY", True, "https://api.moonshot.ai")` to `CLOUD_PROVIDERS`
- [x] Add `"moonshot": "moonshot"` to `EXPECTED_NAMES`

**Files**: `packages/arcllm/tests/test_open_providers.py` (edit)

### Task 5: Verify
- [x] Run `pytest packages/arcllm/tests/test_open_providers.py` — 90/90 passed
- [x] Run `pytest packages/arcllm/tests/` — 562 passed, 1 skipped, 0 failures
- [x] Run `ruff check` — new files clean (pre-existing issues in __init__.py and notebooks unchanged)
- [x] Run `ruff format --check` — adapter file formatted

---

## Completion Criteria

- [x] All 5 tasks complete
- [x] All existing tests pass
- [x] All new parametrized tests pass for moonshot
- [x] Linter clean (no new issues)
- [x] 0 new dependencies

**Total tasks**: 5
**Completed**: 5
**Remaining**: 0
