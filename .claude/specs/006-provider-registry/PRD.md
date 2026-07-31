# PRD: Provider Registry + load_model()

> Feature-specific requirements for ArcLLM Step 6.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

Agents currently need to know the adapter class name, import the config loader, construct the adapter manually, and manage the httpx client lifecycle. This is 4+ lines of boilerplate per agent, creates tight coupling to specific adapter classes, and means switching providers requires code changes (not just config changes). The `load_model()` placeholder in `__init__.py` raises `NotImplementedError`.

Additionally, the `OpenAIAdapter` class name doesn't follow a predictable convention — `"openai".title()` produces `"Openai"`, not `"OpenAI"`. For convention-based class lookup to work, the class must be renamed.

### Goal

Implement `load_model()` as the one-call public API entry point:

```python
model = load_model("anthropic", "claude-haiku-4-5-20251001")
response = await model.invoke(messages, tools=tools)
```

It should:
1. Load provider config from `providers/{name}.toml` (existing `load_provider_config()`)
2. Dynamically import the adapter module from `arcllm.adapters.{name}`
3. Find the adapter class by convention: `{Name}Adapter`
4. Construct and return the adapter with config + model name
5. Cache configs at module level for performance
6. Allow a model name override, or fall back to the provider's `default_model`

### Success Criteria

- `load_model("anthropic")` returns a working `AnthropicAdapter` instance
- `load_model("openai")` returns a working `OpenaiAdapter` instance
- `load_model("anthropic", "claude-haiku-4-5-20251001")` uses the specified model
- `load_model("anthropic")` (no model) uses `default_model` from provider TOML
- Second call to `load_model()` with same provider reuses cached config
- `clear_cache()` resets the cache (for testing)
- `load_model("nonexistent")` raises `ArcLLMConfigError` with clear message
- `load_model("anthropic")` when adapter module doesn't exist raises `ArcLLMConfigError`
- `OpenAIAdapter` renamed to `OpenaiAdapter` throughout codebase
- All 115 existing tests still pass after rename
- New registry tests cover happy path, caching, errors, and edge cases
- Zero new dependencies

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `load_model(provider, model)` returns configured `LLMProvider` | P0 | Working adapter instance returned |
| FR-2 | Provider config loaded from `providers/{name}.toml` | P0 | Uses existing `load_provider_config()` |
| FR-3 | Adapter module imported dynamically via `importlib.import_module("arcllm.adapters.{name}")` | P0 | No static import registry |
| FR-4 | Adapter class found by convention: `provider.title() + "Adapter"` | P0 | `"anthropic"` -> `AnthropicAdapter` |
| FR-5 | When `model` is `None`, use `default_model` from provider config | P0 | Reads `ProviderSettings.default_model` |
| FR-6 | Module-level config cache — configs loaded once per provider | P0 | Second call skips TOML parsing |
| FR-7 | `clear_cache()` function resets all cached configs | P0 | Tests can isolate |
| FR-8 | Missing provider TOML raises `ArcLLMConfigError` | P0 | Clear message naming provider |
| FR-9 | Missing adapter module raises `ArcLLMConfigError` | P0 | Clear message naming module path |
| FR-10 | Missing adapter class in module raises `ArcLLMConfigError` | P0 | Clear message naming expected class |
| FR-11 | `OpenAIAdapter` renamed to `OpenaiAdapter` | P0 | Convention compliance |
| FR-12 | `load_model` and `clear_cache` exported from `__init__.py` | P0 | Public API surface |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | `load_model()` overhead (cached) | <1ms per call |
| NFR-2 | First call loads + caches config | Single TOML parse per provider |
| NFR-3 | Zero new dependencies | Uses stdlib `importlib` |
| NFR-4 | All tests run without real API calls | Mocked where needed |
| NFR-5 | Existing 115 tests unaffected after rename | Zero regressions |

---

## User Stories

### Agent Developer

> As an agent developer, I want to get a configured model object with one function call, so I don't need to know which adapter class to import or how to wire up config.

### Platform Engineer

> As a platform engineer, I want to add a new provider by creating two files (TOML + adapter), without editing a registry mapping, so the system scales without central coordination.

---

## Out of Scope (Step 6)

- Module injection (telemetry, audit, budget — Step 7+)
- Sync wrapper for `load_model()`
- Provider validation beyond config loading
- Connection pooling or shared client instances
- Custom config paths (always package-relative)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1 (types + exceptions) | Prerequisite | COMPLETE |
| Step 2 (config loading) | Prerequisite | COMPLETE |
| Step 3 (BaseAdapter + AnthropicAdapter) | Prerequisite | COMPLETE |
| Step 5 (OpenAIAdapter) | Prerequisite | COMPLETE |
| importlib | stdlib | Available |
