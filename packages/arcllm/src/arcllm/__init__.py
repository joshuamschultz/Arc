"""ArcLLM — Unified LLM abstraction layer for autonomous agents."""

import importlib

from arcllm.config import (
    DefaultsConfig,
    GlobalConfig,
    ModelMetadata,
    ModuleConfig,
    ProviderConfig,
    ProviderSettings,
    VaultConfig,
    load_global_config,
    load_provider_config,
)
from arcllm.exceptions import (
    ArcLLMAPIError,
    ArcLLMConfigError,
    ArcLLMError,
    ArcLLMParseError,
)
from arcllm.registry import clear_cache, load_model
from arcllm.types import (
    ContentBlock,
    ImageBlock,
    LLMProvider,
    LLMResponse,
    Message,
    StopReason,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

# Adapter classes are lazily imported to avoid loading httpx at import time.
# Access via `from arcllm import AnthropicAdapter` still works — __getattr__
# handles the deferred import on first access.
_LAZY_IMPORTS: dict[str, str] = {
    "AnthropicAdapter": "arcllm.adapters.anthropic",
    "BaseAdapter": "arcllm.adapters.base",
    "DeepseekAdapter": "arcllm.adapters.deepseek",
    "FireworksAdapter": "arcllm.adapters.fireworks",
    "GroqAdapter": "arcllm.adapters.groq",
    "HuggingfaceAdapter": "arcllm.adapters.huggingface",
    "Huggingface_TgiAdapter": "arcllm.adapters.huggingface_tgi",
    "MistralAdapter": "arcllm.adapters.mistral",
    "OllamaAdapter": "arcllm.adapters.ollama",
    "OpenaiAdapter": "arcllm.adapters.openai",
    "TogetherAdapter": "arcllm.adapters.together",
    "VllmAdapter": "arcllm.adapters.vllm",
    "AuditModule": "arcllm.modules.audit",
    "BaseModule": "arcllm.modules.base",
    "FallbackModule": "arcllm.modules.fallback",
    "RateLimitModule": "arcllm.modules.rate_limit",
    "RetryModule": "arcllm.modules.retry",
    "OtelModule": "arcllm.modules.otel",
    "SecurityModule": "arcllm.modules.security",
    "TelemetryModule": "arcllm.modules.telemetry",
    "VaultResolver": "arcllm.vault",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        attr = getattr(module, name)
        globals()[name] = attr  # cache for subsequent accesses
        return attr
    raise AttributeError(f"module 'arcllm' has no attribute {name!r}")


__all__ = [
    # Adapters (lazy — loaded on first access)
    "AnthropicAdapter",
    "BaseAdapter",
    "DeepseekAdapter",
    "FireworksAdapter",
    "GroqAdapter",
    "HuggingfaceAdapter",
    "Huggingface_TgiAdapter",
    "MistralAdapter",
    "OllamaAdapter",
    "OpenaiAdapter",
    "TogetherAdapter",
    "VllmAdapter",
    # Modules (lazy — loaded on first access)
    "AuditModule",
    "BaseModule",
    "FallbackModule",
    "OtelModule",
    "RateLimitModule",
    "RetryModule",
    "SecurityModule",
    "TelemetryModule",
    "VaultResolver",
    # Config types
    "DefaultsConfig",
    "GlobalConfig",
    "ModelMetadata",
    "ModuleConfig",
    "ProviderConfig",
    "ProviderSettings",
    "VaultConfig",
    # Config loaders
    "load_global_config",
    "load_provider_config",
    # Types
    "StopReason",
    "ContentBlock",
    "ImageBlock",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "TextBlock",
    "Tool",
    "ToolCall",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
    # Exceptions
    "ArcLLMAPIError",
    "ArcLLMConfigError",
    "ArcLLMError",
    "ArcLLMParseError",
    # Public API
    "clear_cache",
    "load_model",
]
