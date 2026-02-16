# PRD: Config Loading System

> Feature-specific requirements for ArcLLM Step 2.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

ArcLLM's core types (Step 1) are pure data contracts. Without config loading, adapters don't know where to connect, which models exist, what their capabilities are, or what defaults to apply. Agents running unattended need config errors caught at startup — not during a live LLM call at 2am.

### Goal

Create a config loading system that:
1. Reads global config (`config.toml`) for defaults and module toggles
2. Reads per-provider configs (`providers/*.toml`) for connection settings and model metadata
3. Validates all config into typed pydantic models on load (fail-fast)
4. Exposes a simple override chain: kwargs > provider > global defaults
5. Discovers config files relative to the installed package

### Success Criteria

- `load_global_config()` returns a typed `GlobalConfig` with all defaults and module toggles
- `load_provider_config("anthropic")` returns a typed `ProviderConfig` with connection settings + model metadata
- Invalid TOML raises `ArcLLMConfigError` (not raw `tomllib` errors)
- Missing provider TOML raises `ArcLLMConfigError` with clear message
- All config models validate field types (wrong type → ValidationError)
- Config files discoverable from installed package (package-relative paths)
- `pytest tests/test_config.py -v` shows all tests passing
- Zero new dependencies (uses stdlib `tomllib`)

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | Global config loads from `src/arcllm/config.toml` | P0 | `load_global_config()` returns `GlobalConfig` |
| FR-2 | Provider config loads from `src/arcllm/providers/{name}.toml` | P0 | `load_provider_config("anthropic")` returns `ProviderConfig` |
| FR-3 | `GlobalConfig` contains `defaults` section (provider, temperature, max_tokens) | P0 | All three fields populated with correct types |
| FR-4 | `GlobalConfig` contains `modules` dict with per-module toggle configs | P0 | Each module has `enabled: bool` + module-specific settings |
| FR-5 | `ProviderConfig` contains connection settings (api_format, base_url, api_key_env, default_model, default_temperature) | P0 | All five fields populated with correct types |
| FR-6 | `ProviderConfig` contains `models` dict mapping model names to `ModelMetadata` | P0 | `config.models["claude-sonnet-4-20250514"]` returns typed metadata |
| FR-7 | `ModelMetadata` includes capability flags (supports_tools, supports_vision, supports_thinking) | P0 | Boolean fields validate correctly |
| FR-8 | `ModelMetadata` includes cost fields (input, output, cache_read, cache_write per 1M tokens) | P0 | Float fields validate correctly |
| FR-9 | `ModelMetadata` includes limits (context_window, max_output_tokens) and modalities | P0 | Int and list[str] fields validate correctly |
| FR-10 | Missing provider TOML raises `ArcLLMConfigError` | P0 | `load_provider_config("nonexistent")` raises with clear message |
| FR-11 | Malformed TOML raises `ArcLLMConfigError` (not raw tomllib.TOMLDecodeError) | P0 | Wraps the underlying error |
| FR-12 | Pydantic validation errors on config raise `ArcLLMConfigError` | P0 | Wrong types in TOML → `ArcLLMConfigError` |
| FR-13 | Config files discovered via package-relative paths | P0 | Works from installed package, not just dev checkout |
| FR-14 | Global config ships with default values for all module toggles (all disabled) | P1 | All modules `enabled = false` by default |
| FR-15 | OpenAI provider TOML with correct model metadata | P1 | `load_provider_config("openai")` returns valid config |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Config load time | <10ms for global + one provider |
| NFR-2 | Zero new dependencies | Uses stdlib `tomllib` only |
| NFR-3 | Config files included in package | `pyproject.toml` includes TOML data files |
| NFR-4 | Clear error messages | Config errors identify which file and what's wrong |

---

## User Stories

### Agent Developer

> As an agent developer, I want to call `load_provider_config("anthropic")` and get back a typed object with model capabilities, so I can check `config.models["claude-sonnet-4-20250514"].supports_tools` before constructing my tool-calling loop.

### Platform Engineer

> As a platform engineer, I want to add a new provider by dropping in one TOML file with connection settings and model metadata, without modifying any Python code.

### Operations Team

> As an ops team member, I want config errors caught at startup (not during an LLM call), so I can fix misconfiguration before agents go live in production.

---

## Out of Scope (Step 2)

- Config override from environment variables (beyond API key env var reference)
- Runtime config reload
- Config validation of API key existence (that's adapter's `validate_config()`)
- Config merge logic at call time (that's Step 6 registry)
- Module-specific config consumption (each module reads its own section when built)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1 (types + exceptions) | Prerequisite | COMPLETE |
| Python 3.11+ (tomllib) | Runtime | Available |
| Pydantic v2 | Library | Already installed |
| ArcLLMConfigError | Exception | Already defined in Step 1 |
