# SDD: Config Loading System

> System design for ArcLLM Step 2.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 2 creates the config layer. It reads TOML files, validates them into typed pydantic models, and raises `ArcLLMConfigError` on any failure. By the time an adapter gets config, it's already validated.

Design priorities:
1. **Fail-fast** — Config errors caught at load time, not during LLM calls
2. **Typed** — Pydantic models, not raw dicts
3. **Simple** — Override chain is flat: args > provider > global

---

## Directory Map

```
src/arcllm/
├── config.py                          # NEW: Config models + loader functions
├── config.toml                        # NEW: Global defaults + module toggles
├── providers/
│   ├── __init__.py                    # EXISTS: Empty (from Step 1)
│   ├── anthropic.toml                 # NEW: Anthropic connection + model metadata
│   └── openai.toml                    # NEW: OpenAI connection + model metadata
├── __init__.py                        # MODIFY: Add config exports
tests/
└── test_config.py                     # NEW: Config loading tests
```

---

## Component Design

### 1. ModelMetadata (`config.py`)

One instance per model entry in a provider TOML.

| Field | Type | Notes |
|-------|------|-------|
| `context_window` | `int` | Max input tokens |
| `max_output_tokens` | `int` | Max output tokens |
| `supports_tools` | `bool` | Tool/function calling support |
| `supports_vision` | `bool` | Image input support |
| `supports_thinking` | `bool` | Extended thinking/reasoning |
| `input_modalities` | `list[str]` | e.g., `["text", "image"]` |
| `cost_input_per_1m` | `float` | USD per 1M input tokens |
| `cost_output_per_1m` | `float` | USD per 1M output tokens |
| `cost_cache_read_per_1m` | `float` | USD per 1M cache read tokens |
| `cost_cache_write_per_1m` | `float` | USD per 1M cache write tokens |

Pydantic BaseModel. All fields required — no defaults. If a provider TOML is missing a field, validation fails immediately.

### 2. ProviderSettings (`config.py`)

The `[provider]` section of a provider TOML.

| Field | Type | Notes |
|-------|------|-------|
| `api_format` | `str` | e.g., `"anthropic-messages"`, `"openai-chat"` |
| `base_url` | `str` | API endpoint URL |
| `api_key_env` | `str` | Environment variable name (NOT the key itself) |
| `default_model` | `str` | Default model if none specified |
| `default_temperature` | `float` | Provider-level default temperature |

### 3. ProviderConfig (`config.py`)

Top-level representation of a loaded provider TOML file.

| Field | Type | Notes |
|-------|------|-------|
| `provider` | `ProviderSettings` | Connection settings from `[provider]` section |
| `models` | `dict[str, ModelMetadata]` | Model name → metadata from `[models.*]` sections |

### 4. DefaultsConfig (`config.py`)

The `[defaults]` section of the global config.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `provider` | `str` | `"anthropic"` | Default provider name |
| `temperature` | `float` | `0.7` | Global default temperature |
| `max_tokens` | `int` | `4096` | Global default max output tokens |

### 5. ModuleConfig (`config.py`)

Generic module toggle. Each module in `[modules.*]` gets one of these.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | `bool` | `False` | Whether the module is active |

Extra module-specific fields (like `monthly_limit_usd` for budget, `max_retries` for retry) are stored as additional key-value pairs. Approach: use `model_config = ConfigDict(extra="allow")` on the pydantic model so unknown fields are preserved but `enabled` is always validated.

This means `config.modules["budget"].monthly_limit_usd` works via attribute access on the pydantic model, while `enabled` is always a validated bool.

### 6. GlobalConfig (`config.py`)

Top-level representation of the loaded global `config.toml`.

| Field | Type | Notes |
|-------|------|-------|
| `defaults` | `DefaultsConfig` | From `[defaults]` section |
| `modules` | `dict[str, ModuleConfig]` | Module name → toggle config from `[modules.*]` |

### 7. Loader Functions (`config.py`)

#### `load_global_config() -> GlobalConfig`

1. Compute path: `Path(__file__).parent / "config.toml"`
2. Open in binary mode (`"rb"`) — `tomllib` requirement
3. Parse with `tomllib.load(f)`
4. Extract `defaults` section → `DefaultsConfig`
5. Extract `modules` section → `dict[str, ModuleConfig]`
6. Construct and return `GlobalConfig`
7. On any error (FileNotFoundError, tomllib.TOMLDecodeError, ValidationError): raise `ArcLLMConfigError` with descriptive message

#### `load_provider_config(provider_name: str) -> ProviderConfig`

1. Compute path: `Path(__file__).parent / "providers" / f"{provider_name}.toml"`
2. Open in binary mode
3. Parse with `tomllib.load(f)`
4. Extract `provider` section → `ProviderSettings`
5. Extract all `models.*` sections → `dict[str, ModelMetadata]`
6. Construct and return `ProviderConfig`
7. On any error: raise `ArcLLMConfigError` with descriptive message including the provider name

**TOML parsing note**: `tomllib.load()` returns nested dicts. The `[models.claude-sonnet-4-20250514]` TOML table becomes `data["models"]["claude-sonnet-4-20250514"]` in Python. Iterate over `data.get("models", {})` to build the models dict.

---

## ADRs

### ADR-004: Pydantic for Config Validation

**Context**: Config errors in production autonomous agents are catastrophic — wrong base_url, missing api_key_env, model name typo. These run unattended.

**Decision**: Use pydantic BaseModel for all config types. Validate on load.

**Rationale**: Fail-fast catches config errors before any LLM call. Pydantic v2 is already in deps. Raw dicts delay error discovery to runtime.

**Alternatives rejected**:
- Dataclasses + manual validation — more code, same outcome
- TypedDict / plain dicts — no validation at load time, errors surface late

### ADR-005: ModuleConfig with extra="allow"

**Context**: Different modules have different config fields (budget has `monthly_limit_usd`, retry has `max_retries`). Need a common base that works for all modules.

**Decision**: `ModuleConfig` has `enabled: bool` as a validated field and uses `ConfigDict(extra="allow")` for module-specific fields.

**Rationale**: Avoids creating a separate pydantic model for every module config. The `enabled` field is universal and validated. Extra fields are preserved for each module to consume when it's built (Steps 7-14). Keeps the config layer simple — it doesn't need to know about module internals.

**Alternative rejected**:
- Separate model per module — overkill at this stage, tight coupling between config and modules that don't exist yet

### ADR-006: Package-Relative File Discovery

**Context**: Config TOML files need to be found at runtime. Options: CWD-relative, env var path, package-relative.

**Decision**: Use `Path(__file__).parent` to find config files relative to the installed package.

**Rationale**: Config ships with the library (D-034). `__file__` works in both dev (`pip install -e`) and installed modes. No external path configuration needed. Teams that need custom configs can override at a higher layer (future: env var for override directory).

**Alternative rejected**:
- CWD-relative — breaks when agent runs from different directory
- importlib.resources — more complex, `__file__` is sufficient for files alongside Python code

---

## Edge Cases

| Case | Handling |
|------|----------|
| Provider TOML with no models section | Valid — `models` dict is empty |
| Provider TOML with unknown fields in `[provider]` | Pydantic ignores extra fields by default (or rejects — decide in implementation) |
| Global config with module that has only `enabled` | Valid — no extra fields required |
| Global config missing `[defaults]` section | `ArcLLMConfigError` — required section |
| TOML file with syntax error | `ArcLLMConfigError` wrapping `tomllib.TOMLDecodeError` |
| Model name with dots (e.g., `gpt-4.5-preview`) | TOML handles this: `[models."gpt-4.5-preview"]` or `[models.gpt-4]` (no dots in key) |
| Float cost field provided as integer in TOML | TOML `3` is int, `3.0` is float — pydantic coerces int → float |
| Empty provider name string | `ArcLLMConfigError` — file not found |

---

## TOML File Specifications

### config.toml

```toml
[defaults]
provider = "anthropic"
temperature = 0.7
max_tokens = 4096

[modules.routing]
enabled = false

[modules.telemetry]
enabled = false

[modules.audit]
enabled = false

[modules.budget]
enabled = false
monthly_limit_usd = 500.00

[modules.retry]
enabled = false
max_retries = 3
backoff_base_seconds = 1.0

[modules.fallback]
enabled = false
chain = ["anthropic", "openai"]

[modules.rate_limit]
enabled = false
requests_per_minute = 60
```

### providers/anthropic.toml

```toml
[provider]
api_format = "anthropic-messages"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
default_model = "claude-sonnet-4-20250514"
default_temperature = 0.7

[models.claude-sonnet-4-20250514]
context_window = 200000
max_output_tokens = 8192
supports_tools = true
supports_vision = true
supports_thinking = true
input_modalities = ["text", "image"]
cost_input_per_1m = 3.00
cost_output_per_1m = 15.00
cost_cache_read_per_1m = 0.30
cost_cache_write_per_1m = 3.75

[models.claude-haiku-4-5-20251001]
context_window = 200000
max_output_tokens = 8192
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 0.80
cost_output_per_1m = 4.00
cost_cache_read_per_1m = 0.08
cost_cache_write_per_1m = 1.00
```

### providers/openai.toml

```toml
[provider]
api_format = "openai-chat"
base_url = "https://api.openai.com"
api_key_env = "OPENAI_API_KEY"
default_model = "gpt-4o"
default_temperature = 0.7

[models.gpt-4o]
context_window = 128000
max_output_tokens = 16384
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 2.50
cost_output_per_1m = 10.00
cost_cache_read_per_1m = 1.25
cost_cache_write_per_1m = 2.50

[models.gpt-4o-mini]
context_window = 128000
max_output_tokens = 16384
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 0.15
cost_output_per_1m = 0.60
cost_cache_read_per_1m = 0.075
cost_cache_write_per_1m = 0.15
```

---

## Test Strategy

Tests in `tests/test_config.py`. Coverage targets:

| Area | Tests | Priority |
|------|-------|----------|
| Global config loading | Load, verify defaults, verify modules | P0 |
| Provider config loading (Anthropic) | Load, verify provider settings, verify model metadata | P0 |
| Provider config loading (OpenAI) | Load, verify cross-provider works | P1 |
| Error: missing provider | `ArcLLMConfigError` raised | P0 |
| Error: malformed TOML | `ArcLLMConfigError` wraps parse error | P0 |
| Model metadata fields | All fields present, correct types | P0 |
| Module config defaults | All modules disabled | P0 |
| Module config extra fields | Budget has `monthly_limit_usd`, etc. | P1 |
| Edge: provider with no models | Empty models dict, no error | P1 |

---

## __init__.py Changes

Add exports for config types and loader functions:

```python
# Config types
GlobalConfig, ProviderConfig, ProviderSettings, ModelMetadata, DefaultsConfig, ModuleConfig

# Config loaders
load_global_config, load_provider_config
```
