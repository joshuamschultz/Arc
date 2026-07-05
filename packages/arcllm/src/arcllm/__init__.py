"""ArcLLM — Unified LLM abstraction layer for autonomous agents."""

__version__ = "0.5.0"

import importlib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from arcllm.capabilities import supports_tools, tool_capable_models
from arcllm.config import (
    DefaultsConfig,
    EndpointConfig,
    GlobalConfig,
    ModelMetadata,
    ModuleConfig,
    ProviderConfig,
    ProviderSettings,
    TraceEncryptionConfig,
    TraceRetentionConfig,
    VaultConfig,
    load_global_config,
    load_provider_config,
    load_telemetry_retention_config,
)
from arcllm.exceptions import (
    ArcLLMAPIError,
    ArcLLMConfigError,
    ArcLLMError,
    ArcLLMGuardrailError,
    ArcLLMInjectionError,
    ArcLLMParseError,
    ArcLLMTraceIntegrityError,
    ArcLLMTraceNotFoundError,
    QueueFullError,
    QueueTimeoutError,
)
from arcllm.registry import MODULE_NAMES, clear_cache, load_model
from arcllm.types import (
    ContentBlock,
    Delta,
    ImageBlock,
    LLMProvider,
    LLMResponse,
    Message,
    ResponseFormat,
    StopReason,
    TextBlock,
    Tool,
    ToolCall,
    ToolCallDelta,
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
    "Azure_OpenaiAdapter": "arcllm.adapters.azure_openai",
    "BaseAdapter": "arcllm.adapters.base",
    "CohereAdapter": "arcllm.adapters.cohere",
    "DeepseekAdapter": "arcllm.adapters.deepseek",
    "FireworksAdapter": "arcllm.adapters.fireworks",
    "GoogleAdapter": "arcllm.adapters.google",
    "GroqAdapter": "arcllm.adapters.groq",
    "HuggingfaceAdapter": "arcllm.adapters.huggingface",
    "Huggingface_TgiAdapter": "arcllm.adapters.huggingface_tgi",
    "MistralAdapter": "arcllm.adapters.mistral",
    "MoonshotAdapter": "arcllm.adapters.moonshot",
    "OllamaAdapter": "arcllm.adapters.ollama",
    "OpenaiAdapter": "arcllm.adapters.openai",
    "TogetherAdapter": "arcllm.adapters.together",
    "VllmAdapter": "arcllm.adapters.vllm",
    "XaiAdapter": "arcllm.adapters.xai",
    "AuditModule": "arcllm.modules.audit",
    "BaseModule": "arcllm.modules.base",
    "FallbackModule": "arcllm.modules.fallback",
    "GuardrailsModule": "arcllm.modules.guardrails",
    "InjectionModule": "arcllm.modules.injection",
    "LoadBalancerModule": "arcllm.modules.load_balancer",
    "PoolExhaustedError": "arcllm.modules.load_balancer",
    "RateLimitModule": "arcllm.modules.rate_limit",
    "RetryModule": "arcllm.modules.retry",
    "OtelModule": "arcllm.modules.otel",
    "QueueModule": "arcllm.modules.queue",
    "SecurityModule": "arcllm.modules.security",
    "TelemetryModule": "arcllm.modules.telemetry",
    "VaultResolver": "arcllm.vault",
    "AwsSecretsManagerBackend": "arcllm.backends.aws_secrets",
    "EncryptedEnvelope": "arcllm.trace_store",
    "JSONLTraceStore": "arcllm.trace_store",
    "TraceRecord": "arcllm.trace_store",
    "TraceStore": "arcllm.trace_store",
    "ReplayRequest": "arcllm.trace_query",
    "load_for_replay": "arcllm.trace_query",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        attr = getattr(module, name)
        globals()[name] = attr  # cache for subsequent accesses
        return attr
    raise AttributeError(f"module 'arcllm' has no attribute {name!r}")


__all__ = [
    "MODULE_NAMES",
    "AnthropicAdapter",
    "ArcLLMAPIError",
    "ArcLLMConfigError",
    "ArcLLMError",
    "ArcLLMGuardrailError",
    "ArcLLMInjectionError",
    "ArcLLMParseError",
    "ArcLLMTraceIntegrityError",
    "ArcLLMTraceNotFoundError",
    "AuditModule",
    "AwsSecretsManagerBackend",
    "Azure_OpenaiAdapter",
    "BaseAdapter",
    "BaseModule",
    "CohereAdapter",
    "ContentBlock",
    "DeepseekAdapter",
    "DefaultsConfig",
    "Delta",
    "EncryptedEnvelope",
    "EndpointConfig",
    "FallbackModule",
    "FireworksAdapter",
    "GlobalConfig",
    "GoogleAdapter",
    "GroqAdapter",
    "GuardrailsModule",
    "HuggingfaceAdapter",
    "Huggingface_TgiAdapter",
    "ImageBlock",
    "InjectionModule",
    "JSONLTraceStore",
    "LLMProvider",
    "LLMResponse",
    "LoadBalancerModule",
    "Message",
    "MistralAdapter",
    "ModelMetadata",
    "ModuleConfig",
    "MoonshotAdapter",
    "OllamaAdapter",
    "OpenaiAdapter",
    "OtelModule",
    "PoolExhaustedError",
    "ProviderConfig",
    "ProviderSettings",
    "QueueFullError",
    "QueueModule",
    "QueueTimeoutError",
    "RateLimitModule",
    "ReplayRequest",
    "ResponseFormat",
    "RetryModule",
    "SecurityModule",
    "StopReason",
    "TelemetryModule",
    "TextBlock",
    "TogetherAdapter",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolResultBlock",
    "ToolUseBlock",
    "TraceEncryptionConfig",
    "TraceRecord",
    "TraceRetentionConfig",
    "TraceStore",
    "Usage",
    "VaultConfig",
    "VaultResolver",
    "VllmAdapter",
    "XaiAdapter",
    "__version__",
    "clear_cache",
    "load_for_replay",
    "load_global_config",
    "load_model",
    "load_provider_config",
    "load_telemetry_retention_config",
    "supports_tools",
    "tool_capable_models",
]
