# SDD: Kimi (Moonshot AI) Provider Support

## Architecture

This follows the established thin-alias adapter pattern. No new abstractions needed.

```
OpenaiAdapter (base)
    ├── DeepseekAdapter    (thin alias)
    ├── GroqAdapter        (thin alias)
    ├── TogetherAdapter    (thin alias)
    ├── FireworksAdapter   (thin alias)
    └── MoonshotAdapter    (thin alias)  <-- NEW
```

## Components

### 1. Adapter: `arcllm/adapters/moonshot.py`

Thin alias inheriting from `OpenaiAdapter`. Overrides only the `name` property.

```python
"""Moonshot AI adapter — OpenAI-compatible cloud inference (Kimi models)."""

from arcllm.adapters.openai import OpenaiAdapter


class MoonshotAdapter(OpenaiAdapter):
    """Thin alias for Moonshot AI's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "moonshot"
```

### 2. Provider Config: `arcllm/providers/moonshot.toml`

TOML configuration with provider settings and model metadata for `kimi-k2` and `kimi-k2.5`.

Key fields:
- `api_format = "openai-chat"` (routes to OpenaiAdapter base)
- `base_url = "https://api.moonshot.ai"` (global endpoint)
- `api_key_env = "MOONSHOT_API_KEY"`
- `default_model = "kimi-k2.5"`

### 3. Lazy Import: `arcllm/__init__.py`

Add `MoonshotAdapter` to `_LAZY_IMPORTS` dict and `__all__` list.

### 4. Tests: `arcllm/tests/test_open_providers.py`

Add moonshot to `CLOUD_PROVIDERS` list and `EXPECTED_NAMES` dict. All parametrized tests automatically cover it.

## Data Flow

```
load_model("moonshot")
    → load_provider_config("moonshot")     # reads moonshot.toml
    → _get_adapter_class("moonshot")       # imports MoonshotAdapter
    → MoonshotAdapter(config, model_name)  # inherits OpenaiAdapter
    → adapter.invoke(messages)             # POST to api.moonshot.ai/v1/chat/completions
```

## Security Considerations

- HTTPS enforced by `ProviderSettings._validate_https`
- API key from env var only (never filesystem)
- No new attack surface (reuses all existing OpenAI adapter validation)

## Dependencies

None new. Uses existing `httpx`, `pydantic`, and `arcllm` internals.
