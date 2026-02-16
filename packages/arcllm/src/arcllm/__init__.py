"""ArcLLM — Unified LLM abstraction layer for autonomous agents."""

import importlib
from pathlib import Path

from dotenv import load_dotenv

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

# Load .env for API keys. Vault replaces this in production.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
load_dotenv()  # Also check cwd

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
    "MoonshotAdapter": "arcllm.adapters.moonshot",
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
    "AnthropicAdapter",
    "ArcLLMAPIError",
    "ArcLLMConfigError",
    "ArcLLMError",
    "ArcLLMParseError",
    "AuditModule",
    "BaseAdapter",
    "BaseModule",
    "ContentBlock",
    "DeepseekAdapter",
    "DefaultsConfig",
    "FallbackModule",
    "FireworksAdapter",
    "GlobalConfig",
    "GroqAdapter",
    "HuggingfaceAdapter",
    "Huggingface_TgiAdapter",
    "ImageBlock",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "MistralAdapter",
    "ModelMetadata",
    "ModuleConfig",
    "MoonshotAdapter",
    "OllamaAdapter",
    "OpenaiAdapter",
    "OtelModule",
    "ProviderConfig",
    "ProviderSettings",
    "RateLimitModule",
    "RetryModule",
    "SecurityModule",
    "StopReason",
    "TelemetryModule",
    "TextBlock",
    "TogetherAdapter",
    "Tool",
    "ToolCall",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
    "VaultConfig",
    "VaultResolver",
    "VllmAdapter",
    "clear_cache",
    "load_global_config",
    "load_model",
    "load_provider_config",
]
