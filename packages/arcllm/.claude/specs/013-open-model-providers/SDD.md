# SDD — Open Model Providers (Step 15)

## Design Overview

Step 15 adds 9 new providers to ArcLLM by leveraging the fact that the open-model ecosystem has converged on the OpenAI Chat Completions API format. The design uses thin alias adapters that inherit from OpenaiAdapter, with minimal changes to the core layer for optional authentication.

### Key Insight

8 of 9 new providers use the exact same API format as OpenAI. Only Mistral has minor quirks needing overrides. This means:
- **No new adapter logic** for 8 providers — just TOML config + class alias
- **Small BaseAdapter change** — conditional auth validation
- **Small OpenaiAdapter change** — conditional auth headers
- **One real adapter** — MistralAdapter with tool_choice + stop_reason overrides

### Architecture Fit

```
Agent calls load_model("ollama", model="llama3.2")
  │
  ├── Registry: convention-based lookup
  │   ├── Load providers/ollama.toml (api_key_required=false)
  │   ├── Import adapters.ollama.OllamaAdapter
  │   └── OllamaAdapter inherits OpenaiAdapter
  │
  ├── BaseAdapter.__init__:
  │   ├── api_key_required=false → skip key validation
  │   └── self._api_key = "" (env var not set)
  │
  └── OllamaAdapter.invoke():
      ├── _build_headers(): no Authorization header (key empty)
      ├── POST http://localhost:11434/v1/chat/completions
      └── Parse OpenAI-format response → LLMResponse
```

## Directory Map

### New Files

```
src/arcllm/
├── adapters/
│   ├── ollama.py              # OllamaAdapter (alias)
│   ├── vllm.py                # VllmAdapter (alias)
│   ├── together.py            # TogetherAdapter (alias)
│   ├── groq.py                # GroqAdapter (alias)
│   ├── fireworks.py           # FireworksAdapter (alias)
│   ├── deepseek.py            # DeepseekAdapter (alias)
│   ├── mistral.py             # MistralAdapter (quirk overrides)
│   ├── huggingface.py         # HuggingfaceAdapter (alias)
│   └── huggingface_tgi.py     # Huggingface_TgiAdapter (alias)
├── providers/
│   ├── ollama.toml            # Local, no auth, common models
│   ├── vllm.toml              # Local, no auth, common models
│   ├── together.toml          # Cloud, auth required, model catalog
│   ├── groq.toml              # Cloud, auth required, model catalog
│   ├── fireworks.toml         # Cloud, auth required, model catalog
│   ├── deepseek.toml          # Cloud, auth required, model catalog
│   ├── mistral.toml           # Cloud, auth required, model catalog
│   ├── huggingface.toml       # Cloud, auth required, HF models
│   └── huggingface_tgi.toml   # Self-hosted, no auth, user models
```

### Modified Files

```
src/arcllm/
├── config.py                  # api_key_required field on ProviderSettings
├── adapters/base.py           # Conditional auth validation in __init__
├── adapters/openai.py         # Conditional Authorization header
├── providers/anthropic.toml   # Add api_key_required = true (explicit)
├── providers/openai.toml      # Add api_key_required = true (explicit)
├── __init__.py                # Lazy imports for new adapters
└── .env.example               # New env var names
```

### New Test Files

```
tests/
├── test_open_providers.py     # Parametrized tests for all alias adapters
└── test_mistral.py            # Mistral-specific quirk tests
```

## Component Design

### 1. ProviderSettings Change (`config.py`)

```
Modified: ProviderSettings
  New field:
    api_key_required: bool = True

  Default: True (backward compatible)
  When False: BaseAdapter skips key validation
```

### 2. BaseAdapter Auth Change (`adapters/base.py`)

```
Modified: BaseAdapter.__init__()
  Current: Always reads env var, raises if empty
  New: Check config.provider.api_key_required
    If True: current behavior (raise on missing key)
    If False: read env var but don't raise if empty
      self._api_key = os.environ.get(env_var, "")
```

### 3. OpenaiAdapter Header Change (`adapters/openai.py`)

```
Modified: OpenaiAdapter._build_headers()
  Current: Always includes Authorization: Bearer {key}
  New: Only include if self._api_key is non-empty
    headers = {"Content-Type": "application/json"}
    if self._api_key:
        headers["Authorization"] = f"Bearer {self._api_key}"
```

### 4. Thin Alias Adapter Pattern (8 providers)

```
Class: {Name}Adapter(OpenaiAdapter)
  Overrides:
    name -> str: returns provider name (e.g., "ollama")

  Inherits all other behavior from OpenaiAdapter:
    - _build_headers() (with conditional auth)
    - _format_messages()
    - _build_request_body()
    - _parse_response()
    - invoke()
```

Example (OllamaAdapter):
```
Class: OllamaAdapter(OpenaiAdapter)
  Properties:
    name -> "ollama"
```

### 5. MistralAdapter (with quirk overrides)

```
Class: MistralAdapter(OpenaiAdapter)
  Properties:
    name -> "mistral"

  Constants:
    _MISTRAL_STOP_REASON_MAP: dict mapping Mistral finish_reasons to StopReason
      "stop" -> "end_turn"
      "tool_calls" -> "tool_use"
      "length" -> "max_tokens"
      "model_length" -> "max_tokens"

  Overrides:
    _build_request_body(messages, tools, **kwargs) -> dict
      Calls super()._build_request_body(messages, tools, **kwargs)
      If "tool_choice" in kwargs and value is "required":
        Replace with "any" (Mistral's equivalent)
      Returns modified body

    _map_stop_reason(finish_reason: str) -> StopReason
      Uses _MISTRAL_STOP_REASON_MAP instead of OpenAI's map
      Falls back to "end_turn" for unknown values
```

### 6. Provider TOML Structure

#### Local Provider (e.g., ollama.toml)
```toml
[provider]
api_format = "openai-chat"
base_url = "http://localhost:11434"
api_key_env = "OLLAMA_API_KEY"
api_key_required = false
default_model = "llama3.2"
default_temperature = 0.7
vault_path = ""

[models.llama3.2]
context_window = 128000
max_output_tokens = 4096
supports_tools = true
supports_vision = false
supports_thinking = false
input_modalities = ["text"]
cost_input_per_1m = 0.0
cost_output_per_1m = 0.0
cost_cache_read_per_1m = 0.0
cost_cache_write_per_1m = 0.0
```

#### Cloud Provider (e.g., together.toml)
```toml
[provider]
api_format = "openai-chat"
base_url = "https://api.together.xyz"
api_key_env = "TOGETHER_API_KEY"
api_key_required = true
default_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
default_temperature = 0.7
vault_path = ""

[models.meta-llama/Llama-3.3-70B-Instruct-Turbo]
context_window = 128000
max_output_tokens = 4096
supports_tools = true
supports_vision = false
supports_thinking = false
input_modalities = ["text"]
cost_input_per_1m = 0.88
cost_output_per_1m = 0.88
cost_cache_read_per_1m = 0.0
cost_cache_write_per_1m = 0.0
```

## Architecture Decision Records

### ADR-1: Thin Alias Over Format-Based Resolution

**Context**: 9 of 11 providers use the OpenAI Chat Completions format. We could modify the registry to resolve adapters by `api_format` field, or create thin alias files per provider.

**Decision**: Thin alias adapters (one file per provider inheriting OpenaiAdapter).

**Rationale**:
- Preserves convention-based registry (D-041, D-042) unchanged
- Each provider has a clear entry point for future customization
- No registry logic changes needed
- Provider-specific behavior can be added by overriding methods
- Adding a new provider is still just: TOML + thin adapter file

**Alternatives Rejected**:
- Format-based resolution: Would require modifying registry core logic and break the convention pattern
- No separate files (TOML-only): Would require registry changes for adapter class resolution

### ADR-2: Explicit api_key_required Flag Over Convention

**Context**: Local providers don't need API keys. Could detect this from empty env var, special sentinel, or explicit flag.

**Decision**: `api_key_required: bool = True` field in ProviderSettings.

**Rationale**:
- Explicit > implicit — clear in TOML what the provider expects
- Backward compatible (defaults to True)
- BaseAdapter change is minimal (one conditional in __init__)
- Operators can still provide optional auth by setting the env var

**Alternatives Rejected**:
- Empty api_key_env convention: Ambiguous — does empty mean "no key" or "forgot to set"?
- LocalBaseAdapter subclass: Adds class hierarchy complexity for one conditional

### ADR-3: Mistral Quirk Overrides in Adapter

**Context**: Mistral's API is 95% OpenAI-compatible but has minor differences in tool_choice and stop_reason values.

**Decision**: MistralAdapter overrides `_build_request_body` and `_map_stop_reason`.

**Rationale**:
- Small, focused overrides on specific methods
- Clear documentation of what's different (in code, not just comments)
- If Mistral becomes fully compatible, overrides can be removed
- Follows open/closed principle — extend without modifying base

**Alternatives Rejected**:
- Pure alias: Would silently fail on tool_choice="required"
- Generic "quirks" config in TOML: Over-engineers the problem

### ADR-4: Pre-Populated Model Metadata with Graceful Fallback

**Context**: Local providers (Ollama, vLLM) can run any model. Cloud providers have known catalogs.

**Decision**: Pre-populate popular models in TOML. Adapter handles missing models gracefully (uses provider defaults).

**Rationale**:
- Popular models get accurate metadata out of the box (context window, capabilities, cost)
- Custom/unknown models still work — adapter uses default max_tokens from config
- No runtime model discovery (avoids latency, works air-gapped)
- TOML is easily extensible — users add models as needed

**Alternatives Rejected**:
- Empty models: Loses intelligence about model capabilities
- Runtime discovery: Adds latency, requires network, complex error handling

## Edge Cases

| Case | Handling |
|------|----------|
| Local provider unreachable (Ollama not running) | httpx.ConnectError → ArcLLMAPIError, retry module can retry |
| Unknown model name for local provider | Missing from TOML models → model_meta is None → use DEFAULT_MAX_OUTPUT_TOKENS |
| API key set for local provider (optional auth) | api_key_required=false but key exists → key sent in headers |
| API key missing for cloud provider | api_key_required=true → ArcLLMConfigError at construction |
| vLLM with custom port | base_url in TOML is configurable (e.g., http://localhost:8000) |
| HuggingFace rate limiting | HTTP 429 → retry module handles it (existing behavior) |
| Mistral tool_choice not "required" | Pass through unchanged — only "required"→"any" mapping needed |
| Provider returns unexpected stop_reason | Default to "end_turn" (existing fallback in all adapters) |
| Model name with slashes (HF format: org/model) | TOML keys allow slashes: [models."meta-llama/Llama-3.3-70B"] |
| Fallback chain mixing local + cloud | Works — FallbackModule calls load_model() per chain entry |

## Test Strategy

### Test File 1: `tests/test_open_providers.py`

Parametrized tests covering all 9 new providers:

1. **Config loading** — Each provider TOML loads without errors
2. **Adapter creation** — load_model() returns correct adapter type
3. **Name property** — Each adapter returns correct provider name
4. **Auth headers (no key, not required)** — No Authorization header for local providers
5. **Auth headers (key present, not required)** — Authorization header included for optional auth
6. **Auth headers (key present, required)** — Authorization header included for cloud providers
7. **Invoke basic** — Mock HTTP response parsed correctly (inherited from OpenAI)
8. **Registry integration** — Convention-based discovery works for all providers

Parametrize over: `["ollama", "vllm", "together", "groq", "fireworks", "deepseek", "huggingface", "huggingface_tgi"]`

### Test File 2: `tests/test_mistral.py`

Mistral-specific quirk tests:

1. **tool_choice translation** — "required" maps to "any" in request body
2. **tool_choice passthrough** — "auto", "none" pass unchanged
3. **tool_choice dict passthrough** — Specific function selection passes unchanged
4. **Stop reason mapping** — Mistral finish_reasons map correctly
5. **model_length stop reason** — Mistral-specific "model_length" maps to "max_tokens"
6. **Name property** — Returns "mistral"
7. **Basic invoke** — Full request/response cycle with mock
8. **Tool calling invoke** — Tools + tool_choice with mock

### Existing Test Updates

- `tests/test_config.py` — Add test for api_key_required=false parsing
- `tests/test_registry.py` — Add integration test for loading new providers

### Estimated Test Counts

| Test File | Estimated Tests |
|-----------|----------------|
| test_open_providers.py | ~35 (8 providers × ~4 parametrized + common tests) |
| test_mistral.py | ~10 |
| test_config.py additions | ~3 |
| test_registry.py additions | ~2 |
| **Total new** | **~50** |
