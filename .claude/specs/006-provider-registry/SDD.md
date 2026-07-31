# SDD: Provider Registry + load_model()

> System design for ArcLLM Step 6.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 6 creates the public API entry point (`load_model()`) and the convention-based registry that backs it. The key insight: the file structure IS the registry. The provider name string is the single key that drives config discovery, module import, and class lookup. No mapping dict, no config field, no plugin system — just naming conventions.

Design priorities:
1. **Zero-config discovery** — provider name drives everything by convention
2. **Performance at scale** — module-level caching so thousands of agents don't re-parse TOML
3. **Clear errors** — every failure path tells you exactly what went wrong
4. **Convention compliance** — rename `OpenAIAdapter` to `OpenaiAdapter` for predictable lookup

---

## Directory Map

```
src/arcllm/
├── registry.py                        # NEW: load_model(), clear_cache(), convention logic
├── __init__.py                        # MODIFY: Replace placeholder, export from registry
├── adapters/
│   └── openai.py                      # MODIFY: Rename OpenAIAdapter -> OpenaiAdapter
tests/
├── test_registry.py                   # NEW: Registry tests
├── test_openai.py                     # MODIFY: Update class references
```

---

## Component Design

### 1. Convention-Based Discovery

The provider name `"anthropic"` drives three lookups:

```
"anthropic" -> providers/anthropic.toml          (config)
"anthropic" -> arcllm.adapters.anthropic          (module)
"anthropic" -> AnthropicAdapter                   (class)
```

Name-to-class rule: `provider_name.title() + "Adapter"`
- `"anthropic".title()` -> `"Anthropic"` -> `AnthropicAdapter`
- `"openai".title()` -> `"Openai"` -> `OpenaiAdapter`
- `"ollama".title()` -> `"Ollama"` -> `OllamaAdapter` (future)

### 2. Registry Module (`registry.py`)

| Function | Purpose | Notes |
|----------|---------|-------|
| `load_model(provider, model, **kwargs)` | Public API entry point | Returns configured LLMProvider |
| `clear_cache()` | Reset cached configs | For testing isolation |
| `_get_adapter_class(provider_name)` | Convention-based class lookup | importlib + getattr |

#### Module-Level Cache

```python
_provider_config_cache: dict[str, ProviderConfig] = {}
```

On `load_model("anthropic")`:
1. Check `_provider_config_cache["anthropic"]`
2. If miss: `load_provider_config("anthropic")` and cache it
3. Import `arcllm.adapters.anthropic` via `importlib.import_module()`
4. `getattr(module, "AnthropicAdapter")`
5. Construct: `AnthropicAdapter(config, model_name)`
6. Return the adapter instance

`clear_cache()` empties `_provider_config_cache`.

Note: We cache configs but NOT adapter instances. Each `load_model()` call returns a fresh adapter with its own httpx client. This is intentional — agents should own their own client lifecycle.

#### Model Name Resolution

```python
model_name = model or config.provider.default_model
```

If `model` is `None`, use the provider's `default_model` from TOML. Explicit model always wins.

#### Error Handling

| Failure | Error | Message |
|---------|-------|---------|
| Provider TOML not found | `ArcLLMConfigError` | Propagated from `load_provider_config()` |
| Adapter module not found | `ArcLLMConfigError` | `"No adapter module found for provider '{name}'. Expected module: arcllm.adapters.{name}"` |
| Adapter class not found in module | `ArcLLMConfigError` | `"No adapter class '{ClassName}' found in module 'arcllm.adapters.{name}'"` |
| Invalid provider name | `ArcLLMConfigError` | Propagated from `_validate_provider_name()` in config.py |

### 3. OpenAIAdapter Rename

`OpenAIAdapter` -> `OpenaiAdapter` in:
- `src/arcllm/adapters/openai.py` (class definition)
- `src/arcllm/__init__.py` (import and __all__)
- `tests/test_openai.py` (all references)
- `walkthrough/step_05_openai_adapter.ipynb` (if referenced)

### 4. __init__.py Changes

Replace the placeholder `load_model()` function with an import from `registry.py`:

```python
from arcllm.registry import clear_cache, load_model
```

Add `clear_cache` to `__all__`. Rename `OpenAIAdapter` references to `OpenaiAdapter`.

---

## ADRs

### ADR-015: Convention-Based Registry

**Context**: `load_model()` needs to map a provider name string to the right adapter class. Options: static dict, TOML field + importlib, naming convention + importlib.

**Decision**: Pure naming convention. Provider name drives TOML path (`providers/{name}.toml`), module path (`arcllm.adapters.{name}`), and class name (`{name.title()}Adapter`). No config field, no mapping dict.

**Rationale**: Adding a provider = add two files. No central registry to edit. The file structure IS the registry. Minimizes coordination overhead for teams adding providers.

**Alternatives rejected**:
- Static mapping dict — requires editing registry.py for each new provider
- TOML `adapter_module` field — adds config surface for something derivable by convention

### ADR-016: Module-Level Config Cache

**Context**: Thousands of concurrent agents each calling `load_model()` would re-parse TOML files on every call.

**Decision**: Cache `ProviderConfig` objects in a module-level dict. `clear_cache()` for test isolation.

**Rationale**: Config is immutable after load. Caching is safe, simple, and eliminates redundant I/O. Module-level is thread-safe for reads (Python GIL). `clear_cache()` keeps tests deterministic.

**Alternatives rejected**:
- Load every time — wasteful at scale
- Explicit Registry object — adds boilerplate for agents, requires passing around or singleton

### ADR-017: OpenaiAdapter Rename

**Context**: `"openai".title()` produces `"Openai"`, not `"OpenAI"`. Convention-based class lookup needs predictable names.

**Decision**: Rename `OpenAIAdapter` to `OpenaiAdapter`. Accept that it looks slightly unusual for the sake of pure convention.

**Rationale**: The alternative (exception dict) adds state that must be maintained. Every new provider with non-standard casing would need an entry. Pure convention means zero maintenance.

---

## Edge Cases

| Case | Handling |
|------|----------|
| `load_model("anthropic")` — no model specified | Uses `config.provider.default_model` |
| `load_model("anthropic", "nonexistent-model")` | Adapter constructed (model_meta will be None in BaseAdapter) |
| `load_model("ANTHROPIC")` — uppercase | Fails provider name validation (lowercase required, D-035) |
| `load_model("")` — empty string | Fails provider name validation |
| `load_model("../etc/passwd")` — path traversal | Fails provider name validation regex |
| Same provider loaded twice | Second call uses cached config |
| `clear_cache()` then `load_model()` | Re-loads from TOML |
| Module exists but has no adapter class | `ArcLLMConfigError` with expected class name |
| Module exists but class doesn't subclass BaseAdapter | Not checked — duck typing, adapter just needs invoke() |

---

## Test Strategy

Tests in `tests/test_registry.py`. Mix of unit tests (mocked imports) and integration-style tests (real config files, env vars).

| Area | Tests | Priority |
|------|-------|----------|
| Happy path — load Anthropic adapter | Config loaded, correct class returned | P0 |
| Happy path — load OpenAI adapter | Config loaded, OpenaiAdapter returned | P0 |
| Default model — no model arg | Uses default_model from TOML | P0 |
| Explicit model — model arg provided | Uses specified model | P0 |
| Config caching — same provider twice | Second call doesn't re-parse | P0 |
| clear_cache — resets cache | After clear, config re-loaded | P0 |
| Missing provider TOML | ArcLLMConfigError | P0 |
| Missing adapter module | ArcLLMConfigError with module path | P0 |
| Missing adapter class in module | ArcLLMConfigError with class name | P0 |
| Invalid provider name | ArcLLMConfigError (from validation) | P0 |
| Return type | Instance of LLMProvider | P1 |
| kwargs pass-through | Extra kwargs forwarded to adapter | P1 |
