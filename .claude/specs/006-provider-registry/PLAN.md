# PLAN: Provider Registry + load_model()

> Implementation tasks for ArcLLM Step 6.
> Status: COMPLETE

---

## Progress

**Completed**: 5/5 tasks
**Remaining**: 0 tasks

---

## Phase 1: Rename for Convention Compliance (Task 1)

### T6.1 Rename OpenAIAdapter to OpenaiAdapter `[activity: refactoring]`

Rename across all source and test files for convention-based class lookup.

- [x] In `src/arcllm/adapters/openai.py`: rename class `OpenAIAdapter` -> `OpenaiAdapter`
- [x] In `src/arcllm/__init__.py`: update import and `__all__` entry
- [x] In `tests/test_openai.py`: update all references
- [x] In `walkthrough/step_05_openai_adapter.ipynb`: update references if present
- [x] Run `pytest -v` — all 125 tests pass with new name

**Verify**: `pytest -v` — 125 passed, 1 skipped, zero failures

---

## Phase 2: Registry Tests (Task 2)

### T6.2 Write test_registry.py — tests first (TDD) `[activity: unit-testing]`

Write tests before implementation. Tests need env vars set for adapter construction.

- [x] Create `tests/test_registry.py`
- [x] **TestLoadModelHappyPath**:
  - [x] `test_load_anthropic_adapter` — returns AnthropicAdapter instance — PASSED
  - [x] `test_load_openai_adapter` — returns OpenaiAdapter instance — PASSED
  - [x] `test_load_default_model` — no model arg uses default_model from TOML — PASSED
  - [x] `test_load_explicit_model` — model arg overrides default — PASSED
  - [x] `test_returns_llm_provider` — return type is LLMProvider — PASSED
- [x] **TestConfigCaching**:
  - [x] `test_config_cached` — second call reuses cached config (mock load_provider_config, verify call count) — PASSED
  - [x] `test_clear_cache_resets` — after clear_cache(), config re-loaded — PASSED
  - [x] `test_different_providers_cached_separately` — "anthropic" and "openai" each cached — PASSED
- [x] **TestErrorHandling**:
  - [x] `test_missing_provider_toml` — ArcLLMConfigError for unknown provider — PASSED
  - [x] `test_missing_adapter_module` — ArcLLMConfigError naming expected module — PASSED
  - [x] `test_missing_adapter_class` — ArcLLMConfigError naming expected class — PASSED
  - [x] `test_invalid_provider_name` — ArcLLMConfigError for path traversal attempt — PASSED
  - [x] `test_empty_provider_name` — ArcLLMConfigError for empty string — PASSED
- [x] Run tests — all 13 FAIL (registry.py didn't exist yet) — RED confirmed

**Verify**: Tests written, all failed with ImportError (RED)

---

## Phase 3: Registry Implementation (Task 3)

### T6.3 Create registry.py `[activity: backend-development]`

- [x] Create `src/arcllm/registry.py`
- [x] Module-level cache: `_provider_config_cache: dict[str, ProviderConfig] = {}`
- [x] `clear_cache()` function:
  - [x] Clears `_provider_config_cache`
- [x] `_get_adapter_class(provider_name: str)` private function:
  - [x] Build module path: `f"arcllm.adapters.{provider_name}"`
  - [x] `importlib.import_module(module_path)` — catch `ModuleNotFoundError` -> `ArcLLMConfigError`
  - [x] Build class name: `f"{provider_name.title()}Adapter"`
  - [x] `getattr(module, class_name)` — catch `AttributeError` -> `ArcLLMConfigError`
  - [x] Return the class
- [x] `load_model(provider: str, model: str | None = None, **kwargs) -> LLMProvider`:
  - [x] Check cache for provider config, else `load_provider_config(provider)` and cache
  - [x] Resolve model: `model or config.provider.default_model`
  - [x] Get adapter class via `_get_adapter_class(provider)`
  - [x] Construct: `adapter_class(config, model_name, **kwargs)`
  - [x] Return adapter instance

**Verify**: `pytest tests/test_registry.py -v` — 13 passed in 0.45s (GREEN)

---

## Phase 4: Wire Up Public API (Task 4)

### T6.4 Update __init__.py `[activity: backend-development]`

- [x] Remove the placeholder `load_model()` function
- [x] Import `load_model` and `clear_cache` from `arcllm.registry`
- [x] Add `clear_cache` to `__all__`
- [x] Verify `load_model` already in `__all__`
- [x] Update `test_types.py` — replaced placeholder test with working `test_load_model_returns_provider`

**Verify**: `python -c "from arcllm import load_model, clear_cache; print('OK')"` — imports cleanly

---

## Phase 5: Full Verification (Task 5)

### T6.5 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL 138 tests pass (125 existing + 13 new registry tests)
- [x] Verify zero regressions in test_types.py, test_config.py, test_anthropic.py, test_openai.py
- [x] Verify `load_model("anthropic")` works end-to-end (with env var)
- [x] Verify `load_model("openai")` works end-to-end (with env var)
- [x] Count total tests: 138 passed, 1 skipped

**Verify**: `pytest -v` — 138 passed, 1 skipped in 4.19s, zero failures

---

## Acceptance Criteria

- [x] `load_model("anthropic")` returns working `AnthropicAdapter`
- [x] `load_model("openai")` returns working `OpenaiAdapter`
- [x] `load_model("anthropic", "claude-haiku-4-5-20251001")` uses specified model
- [x] `load_model("anthropic")` (no model) uses `default_model` from TOML
- [x] Config cached at module level — second call skips TOML parse
- [x] `clear_cache()` resets the cache
- [x] `load_model("nonexistent")` raises `ArcLLMConfigError`
- [x] Missing adapter module raises `ArcLLMConfigError` with module path
- [x] Missing adapter class raises `ArcLLMConfigError` with expected class name
- [x] `OpenAIAdapter` renamed to `OpenaiAdapter` throughout
- [x] All existing tests pass after rename
- [x] `registry.py` has `load_model()`, `clear_cache()`, `_get_adapter_class()`
- [x] `__init__.py` exports `load_model` and `clear_cache` from registry
- [x] Full test suite passes with zero failures
- [x] Zero new dependencies

---

## Implementation Notes

- **Actual test count**: 125 existing (not 115 as state file noted) + 13 new = 138 total
- **TDD cycle**: RED (13 errors) → GREEN (13 passed) → integrated (138 passed)
- **Notebook update**: Used JSON-level replace to update all OpenaiAdapter references
- **test_types.py fix**: Replaced `test_load_model_not_implemented` (tested placeholder) with `test_load_model_returns_provider` (tests real implementation)
- **registry.py**: 73 lines — clean, minimal, convention-driven
- **No TOML changes needed**: Convention-based discovery means zero config changes
