# arcllm

Provider-agnostic LLM abstraction layer for autonomous agents. Direct HTTP calls to 14 providers — no provider SDKs, no hidden state, no opaque behavior.

## Layer position

arcllm sits at the base of the execution stack. It depends on arctrust (for audit emission), httpx, and Pydantic. arcrun, arcagent, arcgateway, and arccli all depend on arcllm. arcllm never imports from them.

## What it provides

- `load_model`, `clear_cache` — load a provider adapter by name; adapters are lazy-imported; `clear_cache` resets the registry
- `LLMProvider`, `LLMResponse`, `Message`, `Tool`, `ToolCall`, `ToolUseBlock`, `ToolResultBlock`, `TextBlock`, `ImageBlock`, `ContentBlock`, `StopReason`, `Usage` — Pydantic types for messages, tools, and responses; normalized across all providers
- `GlobalConfig`, `ProviderConfig`, `ProviderSettings`, `ModelMetadata`, `ModuleConfig`, `DefaultsConfig`, `VaultConfig`, `load_global_config`, `load_provider_config` — TOML-driven configuration; model metadata (context windows, pricing, capabilities) lives in TOML, not code
- `ArcLLMError`, `ArcLLMAPIError`, `ArcLLMConfigError`, `ArcLLMParseError`, `QueueFullError`, `QueueTimeoutError` — typed exception hierarchy
- Provider adapters (lazy-imported): `AnthropicAdapter`, `OpenaiAdapter`, `Azure_OpenaiAdapter`, `CohereAdapter`, `DeepseekAdapter`, `FireworksAdapter`, `GoogleAdapter`, `GroqAdapter`, `MistralAdapter`, `NvidiaAdapter`, `OllamaAdapter`, `OpenrouterAdapter`, `TogetherAdapter`, `VllmAdapter` — 14 providers, all via direct HTTP
- Module stack (decorator pattern): retry, fallback, rate_limit, security (PII redaction + request signing), audit, telemetry, otel — stacked deterministically: `Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter`
- `TraceRecord` (via `arcllm.trace_store`) — full call telemetry record with latency, tokens, cost, provider, model, and content

## Quick example

```python
from arcllm import load_model, Message

# Load a provider adapter (telemetry + audit enabled)
model = load_model("anthropic", telemetry=True, audit=True)

response = await model.invoke([
    Message(role="user", content="Summarize this document.")
])

print(response.content)          # "The document describes..."
print(response.usage.total_tokens)  # 412
print(response.stop_reason)      # "end_turn"
```

## Architecture references

- ADR-018: No MCP, No Migration, No ACP — explains why Arc uses direct HTTP over SDKs
- SPEC-017: Arc Core Hardening — arcllm audit module integrates with arctrust.audit

## Status

- Tests: 885 (run with `uv run --no-sync pytest packages/arcllm/tests`)
- Coverage: 99%
- ruff + mypy --strict: clean
