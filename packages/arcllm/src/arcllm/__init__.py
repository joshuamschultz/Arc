"""ArcLLM — Unified LLM abstraction layer for autonomous agents."""

__version__ = "0.7.0"

import importlib
from typing import Any

from dotenv import find_dotenv, load_dotenv

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
    ArcLLMEmbeddingUnavailableError,
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

# Load the operator's own .env (their cwd) for API keys — Vault replaces this in
# production. Deliberately NOT loading any package-internal .env: a library must
# never inject credentials from inside its own source tree, or an editable
# install leaks the framework's dev key into every deployment (and made
# `arc agent build --check` report keys the deployment never configured).
load_dotenv(find_dotenv(usecwd=True))

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
    # Embeddings (SPEC-041) — lazy so `import arcllm` never pulls httpx/torch.
    "DEFAULT_EMBED_MODEL": "arcllm.embeddings",
    "EmbeddingProvider": "arcllm.embeddings",
    "EmbeddingResponse": "arcllm.embeddings",
    "LocalEmbedder": "arcllm.embeddings",
    "NoneEmbedder": "arcllm.embeddings",
    "ProviderEmbedder": "arcllm.embeddings",
    "clear_embedder_cache": "arcllm.embeddings",
    "embed": "arcllm.embeddings",
    "resolve_embedder": "arcllm.embeddings",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        attr = getattr(module, name)
        globals()[name] = attr  # cache for subsequent accesses
        return attr
    raise AttributeError(f"module 'arcllm' has no attribute {name!r}")


__all__ = [
    "DEFAULT_EMBED_MODEL",
    "MODULE_NAMES",
    "AnthropicAdapter",
    "ArcLLMAPIError",
    "ArcLLMConfigError",
    "ArcLLMEmbeddingUnavailableError",
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
    "EmbeddingProvider",
    "EmbeddingResponse",
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
    "LocalEmbedder",
    "Message",
    "MistralAdapter",
    "ModelMetadata",
    "ModuleConfig",
    "MoonshotAdapter",
    "NoneEmbedder",
    "OllamaAdapter",
    "OpenaiAdapter",
    "OtelModule",
    "PoolExhaustedError",
    "ProviderConfig",
    "ProviderEmbedder",
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
    "clear_embedder_cache",
    "embed",
    "load_for_replay",
    "load_global_config",
    "load_model",
    "load_provider_config",
    "load_telemetry_retention_config",
    "resolve_embedder",
    "supports_tools",
    "tool_capable_models",
]
