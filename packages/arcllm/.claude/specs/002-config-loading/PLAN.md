# PLAN: Config Loading System

> Implementation tasks for ArcLLM Step 2.
> Status: COMPLETE

---

## Progress

**Completed**: 5/5 tasks
**Remaining**: 0 tasks

---

## Phase 1: Config Data Files (Tasks 1-2)

### T2.1 Create global config.toml `[activity: config-design]`

- [x] Create `src/arcllm/config.toml`
- [x] Add `[defaults]` section: provider = "anthropic", temperature = 0.7, max_tokens = 4096
- [x] Add `[modules.routing]` with enabled = false
- [x] Add `[modules.telemetry]` with enabled = false
- [x] Add `[modules.audit]` with enabled = false
- [x] Add `[modules.budget]` with enabled = false, monthly_limit_usd = 500.00
- [x] Add `[modules.retry]` with enabled = false, max_retries = 3, backoff_base_seconds = 1.0
- [x] Add `[modules.fallback]` with enabled = false, chain = ["anthropic", "openai"]
- [x] Add `[modules.rate_limit]` with enabled = false, requests_per_minute = 60

**Verify**: File parses cleanly with `python -c "import tomllib; print(tomllib.load(open('src/arcllm/config.toml', 'rb')))"` **PASSED**

---

### T2.2 Create provider TOML files `[activity: config-design]`

- [x] Create `src/arcllm/providers/anthropic.toml`
  - [x] `[provider]` section: api_format, base_url, api_key_env, default_model, default_temperature
  - [x] `[models.claude-sonnet-4-20250514]`: all 10 metadata fields
  - [x] `[models.claude-haiku-4-5-20251001]`: all 10 metadata fields
- [x] Create `src/arcllm/providers/openai.toml`
  - [x] `[provider]` section: api_format, base_url, api_key_env, default_model, default_temperature
  - [x] `[models.gpt-4o]`: all 10 metadata fields
  - [x] `[models.gpt-4o-mini]`: all 10 metadata fields

**Verify**: Both files parse cleanly with `tomllib.load()` **PASSED**

---

## Phase 2: Config Models + Loader (Task 3)

### T2.3 Create config.py with pydantic models and loaders `[activity: backend-development]`

**Config Models** (all pydantic BaseModel):
- [x] `ModelMetadata` — 10 fields: context_window, max_output_tokens, supports_tools, supports_vision, supports_thinking, input_modalities, cost_input_per_1m, cost_output_per_1m, cost_cache_read_per_1m, cost_cache_write_per_1m
- [x] `ProviderSettings` — 5 fields: api_format, base_url, api_key_env, default_model, default_temperature
- [x] `ProviderConfig` — 2 fields: provider (ProviderSettings), models (dict[str, ModelMetadata])
- [x] `DefaultsConfig` — 3 fields with defaults: provider="anthropic", temperature=0.7, max_tokens=4096
- [x] `ModuleConfig` — 1 validated field (enabled: bool = False), extra="allow" for module-specific settings
- [x] `GlobalConfig` — 2 fields: defaults (DefaultsConfig), modules (dict[str, ModuleConfig])

**Loader Functions**:
- [x] `_get_config_dir() -> Path` — returns `Path(__file__).parent` (internal helper)
- [x] `load_global_config() -> GlobalConfig`
  - [x] Find config.toml via `_get_config_dir()`
  - [x] Open binary, parse with `tomllib.load()`
  - [x] Extract `defaults` → `DefaultsConfig`
  - [x] Extract `modules` → `dict[str, ModuleConfig]`
  - [x] Return `GlobalConfig`
  - [x] Wrap all errors in `ArcLLMConfigError`
- [x] `load_provider_config(provider_name: str) -> ProviderConfig`
  - [x] Find `providers/{provider_name}.toml` via `_get_config_dir()`
  - [x] Open binary, parse with `tomllib.load()`
  - [x] Extract `provider` section → `ProviderSettings`
  - [x] Extract `models` section → iterate and build `dict[str, ModelMetadata]`
  - [x] Return `ProviderConfig`
  - [x] Wrap all errors in `ArcLLMConfigError` with provider name in message

**Verify**: `python -c "from arcllm.config import load_global_config; print(load_global_config())"` prints config **PASSED**

---

## Phase 3: Public API + Tests (Tasks 4-5)

### T2.4 Update __init__.py with config exports `[activity: backend-development]`

- [x] Import config types: GlobalConfig, ProviderConfig, ProviderSettings, ModelMetadata, DefaultsConfig, ModuleConfig
- [x] Import loader functions: load_global_config, load_provider_config
- [x] Add all to `__all__`

**Verify**: `python -c "from arcllm import load_global_config, ProviderConfig"` imports cleanly **PASSED**

---

### T2.5 Create test_config.py `[activity: unit-testing]`

- [x] **test_load_global_config** — PASSED
- [x] **test_global_config_modules_all_disabled** — PASSED
- [x] **test_global_config_module_extra_fields** — PASSED
- [x] **test_load_provider_config_anthropic** — PASSED
- [x] **test_provider_config_model_metadata** — PASSED
- [x] **test_provider_config_multiple_models** — PASSED
- [x] **test_load_provider_config_openai** — PASSED
- [x] **test_missing_provider_raises_config_error** — PASSED
- [x] **test_config_error_is_arcllm_error** — PASSED
- [x] **test_model_metadata_types** — PASSED

**Verify**: `pytest tests/test_config.py -v` — 10/10 pass in 0.11s **PASSED**

---

## Acceptance Criteria

- [x] `load_global_config()` returns typed `GlobalConfig` with correct defaults
- [x] `load_provider_config("anthropic")` returns typed `ProviderConfig` with 2 models
- [x] `load_provider_config("openai")` returns typed `ProviderConfig` with 2 models
- [x] `load_provider_config("nonexistent")` raises `ArcLLMConfigError`
- [x] All config models validate field types correctly
- [x] Module configs preserve extra fields (e.g., `monthly_limit_usd`)
- [x] Config files found via package-relative paths
- [x] `pytest tests/test_config.py -v` shows all tests passing
- [x] No new dependencies added
- [x] `__init__.py` exports config types and loaders
- [x] TOML files included in package (setuptools finds them)

---

## Package Data Note

TOML files included via `[tool.setuptools.package-data]` in pyproject.toml:
```toml
[tool.setuptools.package-data]
arcllm = ["*.toml", "providers/*.toml"]
```

---

## Implementation Notes

- **TDD approach**: Tests written first (RED), then config.py implemented (GREEN), all 10 tests passing
- **Full test suite**: 30 tests total (10 config + 20 types), all passing in 0.10-0.13s
- **ModuleConfig extra="allow"**: Works perfectly — `config.modules["budget"].monthly_limit_usd` accessible via attribute
- **TOML int→float coercion**: Pydantic handles this transparently (e.g., `3` in TOML → `3.0` in float field)
- **Package data**: Added `[tool.setuptools.package-data]` to ensure TOML files ship with installed package
