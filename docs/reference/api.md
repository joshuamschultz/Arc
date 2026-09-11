# API Reference

> **Reference**  ·  Look up  ·  page 1 of 8  
> **For** Anyone looking something up  
> [Docs home](../README.md)  ·  [CLI →](cli.md)

Each package exports its public surface from its top-level module, so
`from <package> import <Name>` works for everything listed below unless a
different import path is given.

---

## Packages

| Package | Layer | Public symbols |
|---|---|---|
| [`arcllm`](#arcllm) | Foundation — provider-agnostic LLM calls | 92 |
| [`arcrun`](#arcrun) | Foundation — the agentic loop | 43 |
| [`arcagent`](#arcagent) | Agent — orchestrator wiring the layers together | 7 |
| [`arcmemory`](#arcmemory) | Agent — dual-speed memory | 87 |
| [`arctrust`](#arctrust) | Foundation — identity, signing, policy, audit (imports no sibling) | 75 |
| [`arcstore`](#arcstore) | Foundation — operational storage | 7 |
| [`arcteam`](#arcteam) | Agent — inter-agent messaging | 22 |
| [`arcprompt`](#arcprompt) | Foundation — signed, overlay-able system prompts | 20 |
| [`arcskill`](#arcskill) | Agent — skills and the skill improver | 2 |
| [`arcmodel`](#arcmodel) | Foundation — model metadata | 0 |
| [`arcui`](#arcui) | Surface — web dashboard | 4 |
| [`arctui`](#arctui) | Surface — terminal client | 0 |
| [`arcmas`](#arcmas) | Surface — multi-agent system helpers | 0 |
| [`arcgateway`](#arcgateway) | Surface — remote platform data plane | 8 |
| [`arccli`](#arccli) | Surface — command line | 2 |

---

## arcllm

**Layer:** Foundation — provider-agnostic LLM calls

ArcLLM — Unified LLM abstraction layer for autonomous agents.

### Classes

#### `AnthropicAdapter`

Translates ArcLLM types to/from the Anthropic Messages API.

```python
from arcllm import AnthropicAdapter
```

Constructor: `AnthropicAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `ArcLLMAPIError`

Raised when a provider API returns an HTTP error.

```python
from arcllm import ArcLLMAPIError
```

Constructor: `ArcLLMAPIError(status_code: 'int', body: 'str', provider: 'str', retry_after: 'float | None' = None) -> 'None'`

#### `ArcLLMConfigError`

Raised on configuration validation failure.

```python
from arcllm import ArcLLMConfigError
```

#### `ArcLLMEmbeddingUnavailableError`

Raised when no embedder is available for an ``embed()`` call.

```python
from arcllm import ArcLLMEmbeddingUnavailableError
```

Constructor: `ArcLLMEmbeddingUnavailableError(model: 'str', reason: 'str') -> 'None'`

#### `ArcLLMError`

Base exception for all ArcLLM errors.

```python
from arcllm import ArcLLMError
```

#### `ArcLLMGuardrailError`

Raised when GuardrailsModule finds a structural violation in block mode.

```python
from arcllm import ArcLLMGuardrailError
```

Constructor: `ArcLLMGuardrailError(violations: 'list[Violation]') -> 'None'`

#### `ArcLLMInjectionError`

Raised when InjectionModule detects a prompt-injection pattern in block mode.

```python
from arcllm import ArcLLMInjectionError
```

Constructor: `ArcLLMInjectionError(findings: 'list[InjectionFinding]') -> 'None'`

#### `ArcLLMParseError`

Raised when tool call arguments cannot be parsed.

```python
from arcllm import ArcLLMParseError
```

Constructor: `ArcLLMParseError(raw_string: 'str', original_error: 'Exception') -> 'None'`

#### `ArcLLMTraceIntegrityError`

Raised when an encrypted trace envelope fails tamper-evidence checks.

```python
from arcllm import ArcLLMTraceIntegrityError
```

#### `ArcLLMTraceNotFoundError`

Raised by ``load_for_replay`` when no record matches the given trace_id.

```python
from arcllm import ArcLLMTraceNotFoundError
```

Constructor: `ArcLLMTraceNotFoundError(trace_id: 'str') -> 'None'`

#### `AuditModule`

Wraps invoke() to log audit metadata for compliance and debugging.

```python
from arcllm import AuditModule
```

Constructor: `AuditModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `AwsSecretsManagerBackend`

VaultBackend backed by AWS Secrets Manager.

```python
from arcllm import AwsSecretsManagerBackend
```

Constructor: `AwsSecretsManagerBackend(*, region_name: 'str | None' = None, profile_name: 'str | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `get_secret` | `(path: 'str') -> 'str | None'` | Fetch a secret by name (boto3's SecretId). |
| `is_available` | `() -> 'bool'` | True if boto3 imported, the client built, and no fatal error has been recorded since. |

#### `Azure_OpenaiAdapter`

Azure OpenAI Service adapter.

```python
from arcllm import Azure_OpenaiAdapter
```

Constructor: `Azure_OpenaiAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `BaseAdapter`

Concrete base class for provider adapters.

```python
from arcllm import BaseAdapter
```

Constructor: `BaseAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `BaseModule`

Base class for ArcLLM modules.

```python
from arcllm import BaseModule
```

Constructor: `BaseModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `CohereAdapter`

Thin alias for Cohere's OpenAI-compatible API.

```python
from arcllm import CohereAdapter
```

Constructor: `CohereAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `DeepseekAdapter`

Thin alias for DeepSeek's OpenAI-compatible API.

```python
from arcllm import DeepseekAdapter
```

Constructor: `DeepseekAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `DefaultsConfig`

Global defaults from [defaults] section.

```python
from arcllm import DefaultsConfig
```

#### `Delta`

One frame from a streaming LLM response.

```python
from arcllm import Delta
```

#### `EmbeddingProvider`

One backend that turns texts into vectors. arcmemory depends on this, never on a concrete provider (mirrors ``LLMProvider`` for completions).

```python
from arcllm import EmbeddingProvider
```

| Method | Signature | Purpose |
|---|---|---|
| `async embed` | `(texts: 'list[str]') -> 'EmbeddingResponse'` | Embed ``texts`` into vectors. Raises ArcLLMEmbeddingUnavailableError when this backend cannot serve (the 'none |

#### `EmbeddingResponse`

Normalized embedding result — vectors plus their shape and provenance.

```python
from arcllm import EmbeddingResponse
```

#### `EncryptedEnvelope`

Envelope-encrypted trace bodies at rest ( D-438).

```python
from arcllm import EncryptedEnvelope
```

#### `EndpointConfig`

One endpoint in a load-balanced pool ( [[endpoints]]).

```python
from arcllm import EndpointConfig
```

#### `FallbackModule`

Falls back to alternative providers when the primary fails.

```python
from arcllm import FallbackModule
```

Constructor: `FallbackModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `FireworksAdapter`

Thin alias for Fireworks AI's OpenAI-compatible API.

```python
from arcllm import FireworksAdapter
```

Constructor: `FireworksAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `GlobalConfig`

Loaded global config.toml — defaults + module toggles.

```python
from arcllm import GlobalConfig
```

#### `GoogleAdapter`

Translates ArcLLM types to/from the Google Gemini OpenAI-compatible API.

```python
from arcllm import GoogleAdapter
```

Constructor: `GoogleAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `GroqAdapter`

Thin alias for Groq's OpenAI-compatible API.

```python
from arcllm import GroqAdapter
```

Constructor: `GroqAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `GuardrailsModule`

Validates the resolved response's STRUCTURE only — schema, regex, length, stop-list.
Semantic guardrails (grounding, factual correctness, toxicity) are deliberately out
of scope and live in `arcagent`/`arcrun`, which have the agent's world model.

The docstring in `packages/arcllm/src/arcllm/modules/guardrails.py` cites this as
"ADR-429". That is not one of the repo-level ADRs — it is a spec-local decision
record, `ADR-429: Semantic guardrails out of scope`, recorded inline in the
content-guardrails spec as part of that spec's own ADR-419–434 series.

```python
from arcllm import GuardrailsModule
```

Constructor: `GuardrailsModule(config: 'dict[str, Any]', inner: 'LLMProvider') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: 'list[Message]', tools: 'list[Tool] | None' = None, **kwargs: 'Any') -> 'LLMResponse'` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `HuggingfaceAdapter`

Thin alias for HuggingFace's OpenAI-compatible Inference API.

```python
from arcllm import HuggingfaceAdapter
```

Constructor: `HuggingfaceAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `Huggingface_TgiAdapter`

Thin alias for HuggingFace Text Generation Inference (TGI).

```python
from arcllm import Huggingface_TgiAdapter
```

Constructor: `Huggingface_TgiAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `ImageBlock`

Models

```python
from arcllm import ImageBlock
```

#### `InjectionModule`

Scans inbound content for prompt-injection signals before the provider.

```python
from arcllm import InjectionModule
```

Constructor: `InjectionModule(config: 'dict[str, Any]', inner: 'LLMProvider') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: 'list[Message]', tools: 'list[Tool] | None' = None, **kwargs: 'Any') -> 'LLMResponse'` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `JSONLTraceStore`

Append-only JSONL store with SHA-256 hash chain and daily rotation.

```python
from arcllm import JSONLTraceStore
```

Constructor: `JSONLTraceStore(agent_root: pathlib.Path, *, retention_max_age_days: int | None = None, retention_max_bytes: int | None = None, checkpoint_sink: collections.abc.Callable[[dict[str, typing.Any]], None] | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async append` | `(record: arcllm.trace_store.TraceRecord) -> None` | Append a record with hash chain linkage. |
| `async close` | `() -> None` | No resources to release for file-based store. |
| `async get` | `(trace_id: str) -> arcllm.trace_store.TraceRecord | None` | Get a single record by trace_id. Scans newest files first. |
| `iter_records` | `() -> collections.abc.AsyncIterator[dict[str, typing.Any]]` | Yield every record as a dict, oldest day first. |
| `async query` | `(*, limit: int = 50, cursor: str | None = None, provider: str | None = None, agent: str | None = None, status: str | None = None, start: str | None = None, end: str | None = None) -> tuple[list[arcllm.trace_store.TraceRecord], str | None]` | Query records with filters. Reads newest-first. |
| `async verify_chain` | `(start_seq: int = 0) -> bool` | Verify hash chain integrity across all JSONL files. |

#### `LLMProvider`

Helper class that provides a standard way to create an ABC using inheritance.

```python
from arcllm import LLMProvider
```

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `LLMResponse`

Models

```python
from arcllm import LLMResponse
```

#### `LoadBalancerModule`

Distributes invoke() calls across a pool of same-provider endpoints.

```python
from arcllm import LoadBalancerModule
```

Constructor: `LoadBalancerModule(config: dict[str, typing.Any], pool: list[arcllm.modules.load_balancer.PoolEndpoint], provider_name: str) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close every endpoint adapter, tolerating individual failures. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` | All pool adapters must be valid. |

#### `LocalEmbedder`

Offline ``sentence-transformers`` backend (default all-MiniLM-L6-v2).

```python
from arcllm import LocalEmbedder
```

Constructor: `LocalEmbedder(model: 'str') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async embed` | `(texts: 'list[str]') -> 'EmbeddingResponse'` | Embed ``texts`` into vectors. Raises ArcLLMEmbeddingUnavailableError when this backend cannot serve (the 'none |

#### `Message`

Models

```python
from arcllm import Message
```

#### `MistralAdapter`

Translates ArcLLM types to/from the Mistral API.

```python
from arcllm import MistralAdapter
```

Constructor: `MistralAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `ModelMetadata`

Per-model metadata from provider TOML [models.*] sections.

```python
from arcllm import ModelMetadata
```

#### `ModuleConfig`

Module toggle config. Extra fields preserved for module-specific settings.

```python
from arcllm import ModuleConfig
```

#### `MoonshotAdapter`

Thin alias for Moonshot AI's OpenAI-compatible API.

```python
from arcllm import MoonshotAdapter
```

Constructor: `MoonshotAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `NoneEmbedder`

Sentinel backend — always signals 'no embedder available'.

```python
from arcllm import NoneEmbedder
```

Constructor: `NoneEmbedder(model: 'str') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async embed` | `(texts: 'list[str]') -> 'EmbeddingResponse'` | Embed ``texts`` into vectors. Raises ArcLLMEmbeddingUnavailableError when this backend cannot serve (the 'none |

#### `OllamaAdapter`

Thin alias for Ollama's OpenAI-compatible API.

```python
from arcllm import OllamaAdapter
```

Constructor: `OllamaAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `OpenaiAdapter`

Translates ArcLLM types to/from the OpenAI Chat Completions API.

```python
from arcllm import OpenaiAdapter
```

Constructor: `OpenaiAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `OtelModule`

Creates root 'arcllm.invoke' span with GenAI semantic convention attributes.

```python
from arcllm import OtelModule
```

Constructor: `OtelModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `PoolExhaustedError`

Raised when every endpoint in a pool is unhealthy.

```python
from arcllm import PoolExhaustedError
```

Constructor: `PoolExhaustedError(pool_id: str, endpoint_count: int) -> None`

#### `ProviderConfig`

Loaded provider TOML — connection settings + model metadata + endpoint pool.

```python
from arcllm import ProviderConfig
```

#### `ProviderEmbedder`

Optional remote embeddings endpoint (OpenAI-compatible ``/embeddings``).

```python
from arcllm import ProviderEmbedder
```

Constructor: `ProviderEmbedder(model: 'str', *, base_url: 'str', api_key: 'str' = '', client: 'httpx.AsyncClient | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async embed` | `(texts: 'list[str]') -> 'EmbeddingResponse'` | Embed ``texts`` into vectors. Raises ArcLLMEmbeddingUnavailableError when this backend cannot serve (the 'none |

#### `ProviderSettings`

Provider connection settings from [provider] section.

```python
from arcllm import ProviderSettings
```

#### `QueueFullError`

Raised when queue backpressure rejects a call.

```python
from arcllm import QueueFullError
```

Constructor: `QueueFullError(current_waiters: 'int', max_queued: 'int') -> 'None'`

#### `QueueModule`

Concurrency-limiting wrapper with backpressure and send-time timeout.

```python
from arcllm import QueueModule
```

Constructor: `QueueModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Gate the inner invoke() through the concurrency semaphore. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `queue_stats` | `() -> dict[str, typing.Any]` | Return current queue state for REST API and UI display. |
| `validate_config` | `() -> bool` |  |

#### `QueueTimeoutError`

Raised when a call exceeds the send-time timeout.

```python
from arcllm import QueueTimeoutError
```

Constructor: `QueueTimeoutError(timeout: 'float') -> 'None'`

#### `RateLimitModule`

Acquires a token from a per-provider bucket before each invoke().

```python
from arcllm import RateLimitModule
```

Constructor: `RateLimitModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `ReplayRequest`

A byte-exact, reconstructed LLM request. Data only — no I/O.

```python
from arcllm import ReplayRequest
```

Constructor: `ReplayRequest(provider: str, model: str, messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None, options: dict[str, typing.Any], lineage: dict[str, typing.Any] | None, classification: str) -> None`

#### `ResponseFormat`

Structured-output enforcement hint, OpenAI-compatible shape.

```python
from arcllm import ResponseFormat
```

#### `RetryModule`

Retries transient failures with exponential backoff + jitter.

```python
from arcllm import RetryModule
```

Constructor: `RetryModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `SecurityModule`

Per-invoke security middleware: PII redaction + request signing.

```python
from arcllm import SecurityModule
```

Constructor: `SecurityModule(config: 'dict[str, Any]', inner: 'LLMProvider') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `async invoke` | `(messages: 'list[Message]', tools: 'list[Tool] | None' = None, **kwargs: 'Any') -> 'LLMResponse'` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `TelemetryModule`

Wraps invoke() to log timing, token usage, and cost.

```python
from arcllm import TelemetryModule
```

Constructor: `TelemetryModule(config: dict[str, typing.Any], inner: arcllm.types.LLMProvider) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Close resources held by the inner provider. |
| `get_budget_state` | `() -> dict[str, typing.Any] | None` | Return current budget state for REST API queries. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, *, response_format: arcllm.types.ResponseFormat | None = None, **kwargs: Any) -> 'AsyncIterator[Delta]'` | Stream incremental Deltas from the model. |
| `validate_config` | `() -> bool` |  |

#### `TextBlock`

Models

```python
from arcllm import TextBlock
```

#### `TogetherAdapter`

Thin alias for Together AI's OpenAI-compatible API.

```python
from arcllm import TogetherAdapter
```

Constructor: `TogetherAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `Tool`

Models

```python
from arcllm import Tool
```

#### `ToolCall`

Models

```python
from arcllm import ToolCall
```

#### `ToolCallDelta`

Incremental tool-call fragment from a streaming response.

```python
from arcllm import ToolCallDelta
```

#### `ToolResultBlock`

Models

```python
from arcllm import ToolResultBlock
```

#### `ToolUseBlock`

Models

```python
from arcllm import ToolUseBlock
```

#### `TraceEncryptionConfig`

Envelope-encryption settings for trace bodies at rest ( D-438).

```python
from arcllm import TraceEncryptionConfig
```

#### `TraceRecord`

Single LLM call record. Immutable, hashable, serializable.

```python
from arcllm import TraceRecord
```

| Method | Signature | Purpose |
|---|---|---|
| `compute_hash` | `() -> str` | Compute SHA-256 hash of this record using JCS canonical JSON. |
| `with_hash` | `(prev_hash: str) -> 'TraceRecord'` | Return a new record with prev_hash set and record_hash computed. |

#### `TraceRetentionConfig`

Retention purge bounds for rotated trace files ( D-440).

```python
from arcllm import TraceRetentionConfig
```

#### `TraceStore`

Protocol for trace persistence backends.

```python
from arcllm import TraceStore
```

| Method | Signature | Purpose |
|---|---|---|
| `async append` | `(record: arcllm.trace_store.TraceRecord) -> None` | Append a record to the store. Computes hash chain automatically. |
| `async close` | `() -> None` | Release resources. |
| `async get` | `(trace_id: str) -> arcllm.trace_store.TraceRecord | None` | Get a single record by trace_id. |
| `iter_records` | `() -> collections.abc.AsyncIterator[dict[str, typing.Any]]` | Stream all records as plain dicts in chronological storage order. |
| `async query` | `(*, limit: int = 50, cursor: str | None = None, provider: str | None = None, agent: str | None = None, status: str | None = None, start: str | None = None, end: str | None = None) -> tuple[list[arcllm.trace_store.TraceRecord], str | None]` | Query records with filters and cursor pagination. |
| `async verify_chain` | `(start_seq: int = 0) -> bool` | Verify SHA-256 hash chain integrity from start_seq. |

#### `Usage`

Models

```python
from arcllm import Usage
```

#### `VaultConfig`

Vault backend configuration from [vault] section.

```python
from arcllm import VaultConfig
```

#### `VaultResolver`

Resolve API keys from vault with TTL cache and env var fallback.

```python
from arcllm import VaultResolver
```

Constructor: `VaultResolver(backend: 'VaultBackend | None', cache_ttl_seconds: 'int' = 300) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `resolve_api_key` | `(api_key_env: 'str', vault_path: 'str | None') -> 'str'` | Resolve API key: vault first (if configured), then env var. |

#### `VllmAdapter`

Thin alias for vLLM's OpenAI-compatible API.

```python
from arcllm import VllmAdapter
```

Constructor: `VllmAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

#### `XaiAdapter`

Thin alias for xAI's OpenAI-compatible API.

```python
from arcllm import XaiAdapter
```

Constructor: `XaiAdapter(config: arcllm.config.ProviderConfig, model_name: str, resolved_api_key: str | None = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `async close` | `() -> None` | Release resources held by this provider. No-op by default. |
| `async invoke` | `(messages: list[arcllm.types.Message], tools: list[arcllm.types.Tool] | None = None, **kwargs: Any) -> arcllm.types.LLMResponse` | Make a single LLM call. |
| `invoke_stream` | `(messages, tools=None, **kwargs)` | Stream Deltas using OpenAI's ``stream: true`` SSE protocol. |
| `validate_config` | `() -> bool` |  |

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `clear_cache` | `() -> None` | Reset all registry caches. Use in tests for isolation. |
| `clear_embedder_cache` | `() -> 'None'` | Drop cached local backends (test isolation / config reload). |
| `async embed` | `(texts: 'list[str]', *, model: 'str', provider: 'EmbeddingProvider | None' = None, backend: 'str' = 'local', telemetry: 'dict[str, Any] | None' = None, on_event: 'Callable[[SpoolRecord], None] | None' = None) -> 'EmbeddingResponse'` | Embed ``texts`` into vectors, budget-routed and telemetered. |
| `async load_for_replay` | `(traces_dir: pathlib.Path, trace_id: str, *, wrapping_key_resolver: collections.abc.Callable[[str], bytes] | None = None) -> arcllm.trace_query.ReplayRequest` | Reconstruct a byte-exact :class:`ReplayRequest` for ``trace_id``. |
| `load_global_config` | `() -> arcllm.config.GlobalConfig` | Load and validate the global config.toml. |
| `load_model` | `(provider: str, model: str | None = None, *, budget_scope: str | None = None, on_event: collections.abc.Callable[[typing.Any], None] | None = None, trace_store: typing.Any | None = None, agent_label: str | None = None, agent_did: str | None = None, lineage: dict[str, typing.Any] | None = None, routing: bool | dict[str, typing.Any] | None = None, retry: bool | dict[str, typing.Any] | None = None, fallback: bool | dict[str, typing.Any] | None = None, rate_limit: bool | dict[str, typing.Any] | None = None, load_balance: bool | dict[str, typing.Any] | None = None, circuit_breaker: bool | dict[str, typing.Any] | None = None, telemetry: bool | dict[str, typing.Any] | None = None, queue: bool | dict[str, typing.Any] | None = None, audit: bool | dict[str, typing.Any] | None = None, security: bool | dict[str, typing.Any] | None = None, injection: bool | dict[str, typing.Any] | None = None, guardrails: bool | dict[str, typing.Any] | None = None, otel: bool | dict[str, typing.Any] | None = None) -> arcllm.types.LLMProvider` | Load a configured model object for the given provider. |
| `load_provider_config` | `(provider_name: str) -> arcllm.config.ProviderConfig` | Load and validate a provider TOML file. |
| `load_telemetry_retention_config` | `() -> arcllm.config.TraceRetentionConfig` | Load ``[modules.telemetry.retention]`` as a typed, validated config. |
| `resolve_embedder` | `(model: 'str', *, backend: 'str' = 'local', base_url: 'str | None' = None, api_key: 'str' = '') -> 'EmbeddingProvider'` | Return the ``EmbeddingProvider`` for ``backend``. |
| `supports_tools` | `(provider: 'str', model: 'str') -> 'bool'` | Return True iff provider TOML metadata declares ``supports_tools = true``. |
| `tool_capable_models` | `(provider: 'str') -> 'list[str]'` | Return the model names this provider's TOML marks as tool-capable. |

### Constants and type aliases

| Name | Value |
|---|---|
| `ContentBlock` | `typing.Annotated[arcllm.types.TextBlock \| arcllm.types.ImageBlock \| arcllm.types` |
| `DEFAULT_EMBED_MODEL` | `'all-MiniLM-L6-v2'` |
| `MODULE_NAMES` | `frozenset({'guardrails', 'load_balance', 'routing', 'otel', 'audit', 'telemetry'` |
| `StopReason` | `typing.Literal['end_turn', 'tool_use', 'max_tokens', 'stop_sequence', 'content_f` |
| `__version__` | `'0.7.0'` |

---

## arcrun

**Layer:** Foundation — the agentic loop

arcrun — async execution engine for autonomous agents.

### Classes

#### `CapabilityProvider`

The contract arcrun's loop runs against (ADR-023).

```python
from arcrun import CapabilityProvider
```

| Method | Signature | Purpose |
|---|---|---|
| `advertise` | `() -> 'list[CapabilitySpec]'` | Lean manifest for the model: name · kind · "use when" · schema. |
| `async invoke` | `(name: 'str', args: 'dict[str, Any]', *, caller_did: 'str') -> 'CapabilityResult'` | Dispatch a call — runs through the provider's trust/policy layer. |
| `async load` | `(name: 'str', *, caller_did: 'str') -> 'str | None'` | Lazily fetch the heavy body for one capability (a skill's full instructions). ``None`` for plain tools or unkn |

#### `CapabilityResult`

Outcome of a capability invocation.

```python
from arcrun import CapabilityResult
```

Constructor: `CapabilityResult(content: 'str', is_error: 'bool' = False, extra: 'dict[str, Any] | None' = None) -> None`

#### `CapabilitySpec`

Lean advertise unit — all that enters the model's tool list.

```python
from arcrun import CapabilitySpec
```

Constructor: `CapabilitySpec(name: 'str', description: 'str', input_schema: 'dict[str, Any]', kind: 'str' = 'tool', signals_completion: 'bool' = False, timeout_seconds: 'float | None' = None) -> None`

#### `ChainVerificationResult`

Result of verify_chain().

```python
from arcrun import ChainVerificationResult
```

Constructor: `ChainVerificationResult(valid: 'bool', event_count: 'int', first_broken_index: 'int | None' = None, error: 'str | None' = None) -> None`

#### `Event`

Immutable event with hash chain fields.

```python
from arcrun import Event
```

Constructor: `Event(type: 'str', timestamp: 'float', run_id: 'str', data: 'MappingProxyType', sequence: 'int' = 0, prev_hash: 'str' = '', event_hash: 'str' = '') -> None`

#### `EventBus`

Emits events, collects them, optionally calls handler. Thread-safe.

```python
from arcrun import EventBus
```

Constructor: `EventBus(run_id: 'str', on_event: 'Callable[[Event], Awaitable[None] | None] | None' = None, *, spool_actor_did: 'str | None' = None, store_raw_bodies: 'bool' = False, sample_rate: 'float' = 1.0) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `emit` | `(event_type: 'str', data: 'dict[str, Any] | None' = None) -> 'Event'` | Create event with hash chain, append to log, call handler if set. |

#### `LoopCheckpoint`

Immutable snapshot of resumable loop state at a turn boundary.

```python
from arcrun import LoopCheckpoint
```

Constructor: `LoopCheckpoint(run_id: 'str', parent_run_id: 'str', strategy_name: 'str', turn_count: 'int', tokens_used: 'dict[str, int]', cost_usd: 'float', tool_calls_made: 'int', tool_names: 'list[str]', completion_payload: 'dict[str, Any] | None', completion_tool: 'str | None', max_turns: 'int', max_tokens: 'int | None', max_cost_usd: 'float | None', messages: 'list[Any]' = <factory>) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `to_record` | `() -> 'dict[str, Any]'` | Serialize the scalar metadata (no transcript) for durable persistence. |

#### `LoopResult`

Returned by run().

```python
from arcrun import LoopResult
```

Constructor: `LoopResult(content: 'str | None', turns: 'int', tool_calls_made: 'int', tokens_used: 'dict[str, Any]', strategy_used: 'str', cost_usd: 'float', events: 'list[Event]' = <factory>, completion_payload: 'dict[str, Any] | None' = None, completion_tool: 'str | None' = None) -> None`

| Method | Signature | Purpose |
|---|---|---|
| `verify_integrity` | `() -> 'ChainVerificationResult'` | Verify tamper-evidence of the event chain. |

#### `RunHandle`

Control interface for a running execution loop.

```python
from arcrun import RunHandle
```

Constructor: `RunHandle(state: 'RunState', task: 'asyncio.Task[LoopResult]') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async cancel` | `(caller_did: 'str', reason: 'str | None' = None) -> 'None'` | Hard stop, attributed to a verified caller. Drains queues, sets signal. |
| `async follow_up` | `(caller_did: 'str', message: 'str') -> 'None'` | Queue: inject at end_turn before returning. |
| `async result` | `() -> 'LoopResult'` | Await completion. Returns final result. |
| `async steer` | `(caller_did: 'str', message: 'str') -> 'None'` | Interrupt: inject after current tool, skip remaining. |

#### `RunResult`

Final result of a streamed run, reconstructed by ``collect()``.

```python
from arcrun import RunResult
```

Constructor: `RunResult(content: 'str', turns: 'int' = 0, tool_calls_made: 'int' = 0, cost_usd: 'float' = 0.0, tokens_used: 'dict[str, Any]' = <factory>, completion_payload: 'dict[str, Any] | None' = None, completion_tool: 'str | None' = None) -> None`

#### `SandboxConfig`

Permission boundary. allowed_tools=None means all allowed.

```python
from arcrun import SandboxConfig
```

Constructor: `SandboxConfig(allowed_tools: 'list[str] | None' = None, check: 'Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]] | None' = None) -> None`

#### `SandboxError`

Base for all container sandbox errors.

```python
from arcrun import SandboxError
```

#### `SandboxOOMError`

Container killed by OOM (exit code 137).

```python
from arcrun import SandboxOOMError
```

#### `SandboxRuntimeError`

Script execution failed.

```python
from arcrun import SandboxRuntimeError
```

#### `SandboxTimeoutError`

Container exceeded timeout.

```python
from arcrun import SandboxTimeoutError
```

#### `SandboxUnavailableError`

No container runtime found.

```python
from arcrun import SandboxUnavailableError
```

#### `StaticProvider`

Adapt a fixed ``list[Tool]`` to the CapabilityProvider contract.

```python
from arcrun import StaticProvider
```

Constructor: `StaticProvider(tools: 'list[Tool]') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `advertise` | `() -> 'list[CapabilitySpec]'` |  |
| `async invoke` | `(name: 'str', args: 'dict[str, Any]', *, caller_did: 'str') -> 'CapabilityResult'` |  |
| `async load` | `(name: 'str', *, caller_did: 'str') -> 'str | None'` |  |
| `raw_tools` | `() -> 'list[Tool]'` | The wrapped Tools themselves. |

#### `Strategy`

Base class for execution strategies.

```python
from arcrun import Strategy
```

#### `StreamEvent`

Base class for all stream events.

```python
from arcrun import StreamEvent
```

Constructor: `StreamEvent() -> None`

#### `TokenEvent`

A chunk of text from the model response.

```python
from arcrun import TokenEvent
```

Constructor: `TokenEvent(text: 'str') -> None`

#### `Tool`

A tool the model can call.

```python
from arcrun import Tool
```

Constructor: `Tool(name: 'str', description: 'str', input_schema: 'dict[str, Any]', execute: 'Callable[[dict[str, Any], ToolContext], Awaitable[str]]', timeout_seconds: 'float | None' = None, signals_completion: 'bool' = False, classification: 'str' = 'state_modifying') -> None`

#### `ToolContext`

Passed to Tool.execute.

```python
from arcrun import ToolContext
```

Constructor: `ToolContext(run_id: 'str', tool_call_id: 'str', turn_number: 'int', event_bus: 'EventBus | None', cancelled: 'asyncio.Event', parent_state: 'Any' = None, tool_extra: 'dict[str, Any] | None' = None) -> None`

#### `ToolEndEvent`

Emitted when a tool call completes.

```python
from arcrun import ToolEndEvent
```

Constructor: `ToolEndEvent(result: 'str' = '') -> None`

#### `ToolRegistry`

Tool collection, mutable until frozen for the run.

```python
from arcrun import ToolRegistry
```

Constructor: `ToolRegistry(tools: 'list[Tool]', event_bus: 'EventBus') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `add` | `(tool: 'Tool') -> 'None'` | Add or replace tool. Raises once frozen (mid-run mutation is denied). |
| `freeze` | `() -> 'None'` | Seal the registry for the run. After this, add/remove raise. |
| `get` | `(name: 'str') -> 'Tool | None'` |  |
| `get_classification` | `(name: 'str') -> 'str'` | Return a tool's dispatch classification. |
| `list_schemas` | `() -> 'list[LLMTool]'` | Convert tools to arcllm Tool format for model.invoke(). |
| `names` | `() -> 'list[str]'` |  |
| `remove` | `(name: 'str') -> 'None'` | Remove tool by name. Raises once frozen; no-op if not found otherwise. |

#### `ToolStartEvent`

Emitted when the model calls a tool.

```python
from arcrun import ToolStartEvent
```

Constructor: `ToolStartEvent(name: 'str', args: 'dict[str, Any]' = <factory>) -> None`

#### `TurnEndEvent`

Emitted exactly once, as the final event in the stream.

```python
from arcrun import TurnEndEvent
```

Constructor: `TurnEndEvent(final_text: 'str', turns: 'int' = 0, tool_calls_made: 'int' = 0, cost_usd: 'float' = 0.0, tokens_used: 'dict[str, Any]' = <factory>, completion_payload: 'dict[str, Any] | None' = None, completion_tool: 'str | None' = None) -> None`

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `apply_checkpoint` | `(state: 'RunState', cp: 'LoopCheckpoint') -> 'RunState'` | Restore ``cp`` onto a freshly built ``state``, verifying the tool set. |
| `async collect` | `(stream: 'AsyncIterator[StreamEvent]') -> 'RunResult'` | Drain a StreamEvent iterator to a final RunResult. |
| `detached_context` | `() -> 'ToolContext'` | A minimal ToolContext for providers that wrap plain ``args -> str`` tools. |
| `get_strategy_prompts` | `(*, allowed_strategies: 'list[str] | None' = None, tool_names: 'list[str] | None' = None, resolve: 'PromptResolve' = <function load_stock at 0x1085502c0>) -> 'dict[str, str]'` | Return prompt guidance fragments keyed by section name. |
| `make_execute_tool` | `(*, timeout_seconds: 'float' = 30, max_output_bytes: 'int' = 65536, extra_env: 'dict[str, str] | None' = None, tier: 'str' = 'personal', relax: 'str | None' = None, caller_did: 'str | None' = None, audit_sink: 'Any | None' = None) -> 'Tool'` | Create a tier-routed Python execution tool. |
| `provider_tools` | `(provider: 'CapabilityProvider', *, caller_did: 'str') -> 'list[Tool]'` | Build the loop's internal ToolRegistry tools from ``provider.advertise()``. |
| `async run` | `(model: 'Any', capabilities: 'CapabilityProvider', system_prompt: 'SystemPrompt', task: 'str', *, messages: 'list[Any] | None' = None, max_turns: 'int' = 25, allowed_strategies: 'list[str] | None' = None, sandbox: 'SandboxConfig | None' = None, on_event: 'Callable[..., Any] | None' = None, transform_context: 'Callable[..., Any] | None' = None, tool_timeout: 'float | None' = None, depth: 'int' = 0, max_depth: 'int' = 3, tool_choice: 'dict[str, Any] | None' = None, actor_did: 'str | None' = None, store_raw_bodies: 'bool' = False, sample_rate: 'float' = 1.0, max_tokens: 'int | None' = None, max_cost_usd: 'float | None' = None, on_checkpoint: 'Callable[[LoopCheckpoint], None] | None' = None, approval_provider: 'Callable[..., Any] | None' = None, approval_required_tools: 'frozenset[str]' = frozenset(), max_parallel: 'int' = 10, max_repeat: 'int | None' = None, max_consecutive_errors: 'int | None' = None, resume_from: 'LoopCheckpoint | None' = None, run_id: 'str | None' = None, on_handle: 'Callable[[RunHandle], None] | None' = None) -> 'LoopResult'` | Blocking entry point. Runs until task complete, a breaker trip, or resume. |
| `async run_async` | `(model: 'Any', capabilities: 'CapabilityProvider', system_prompt: 'SystemPrompt', task: 'str', *, messages: 'list[Any] | None' = None, max_turns: 'int' = 25, allowed_strategies: 'list[str] | None' = None, sandbox: 'SandboxConfig | None' = None, on_event: 'Callable[..., Any] | None' = None, transform_context: 'Callable[..., Any] | None' = None, tool_timeout: 'float | None' = None, depth: 'int' = 0, max_depth: 'int' = 3, tool_choice: 'dict[str, Any] | None' = None, actor_did: 'str | None' = None, store_raw_bodies: 'bool' = False, sample_rate: 'float' = 1.0, max_tokens: 'int | None' = None, max_cost_usd: 'float | None' = None, on_checkpoint: 'Callable[[LoopCheckpoint], None] | None' = None, approval_provider: 'Callable[..., Any] | None' = None, approval_required_tools: 'frozenset[str]' = frozenset(), max_parallel: 'int' = 10, max_repeat: 'int | None' = None, max_consecutive_errors: 'int | None' = None, resume_from: 'LoopCheckpoint | None' = None, run_id: 'str | None' = None) -> 'RunHandle'` | Non-blocking entry point. Returns handle for steering. |
| `async run_shell` | `(command: 'str', *, tier: 'str', workspace: 'Path', readonly_subpaths: 'list[Path] | None' = None, caller_did: 'str | None' = None, audit_sink: 'Any | None' = None, platform_supports_vm: 'bool | None' = None, relax: 'str | None' = None, timeout: 'float' = 30.0) -> 'str'` | Run a shell ``command`` inside the tier-routed isolation backend. |
| `async run_stream` | `(*, model: 'Any', capabilities: 'CapabilityProvider', system_prompt: 'SystemPrompt', task: 'str', messages: 'list[Any] | None' = None, max_turns: 'int' = 25, sandbox: 'SandboxConfig | None' = None, allowed_strategies: 'list[str] | None' = None, tool_timeout: 'float | None' = None, on_event: 'Callable[[Event], None] | None' = None, transform_context: 'Callable[..., Any] | None' = None, tool_choice: 'dict[str, Any] | None' = None, actor_did: 'str | None' = None, store_raw_bodies: 'bool' = False, audit_sink: 'Any | None' = None, ui_reporter: 'Any | None' = None, max_tokens: 'int | None' = None, max_cost_usd: 'float | None' = None, on_checkpoint: 'Callable[[Any], None] | None' = None, approval_provider: 'Callable[..., Any] | None' = None, approval_required_tools: 'frozenset[str]' = frozenset(), max_parallel: 'int' = 10, max_repeat: 'int | None' = None, max_consecutive_errors: 'int | None' = None, resume_from: 'Any | None' = None, run_id: 'str | None' = None, on_handle: 'Callable[[RunHandle], None] | None' = None) -> 'AsyncIterator[StreamEvent]'` | Run the agent loop and stream events as they occur. |
| `stream_llm_response` | `(*, model: 'Any', messages: 'list[Any]', tools: 'list[Tool] | None' = None, **invoke_kwargs: 'Any') -> 'AsyncIterator[StreamEvent]'` | Stream one ``model.invoke_stream`` call as StreamEvents. |
| `system_messages` | `(prompt: 'SystemPrompt') -> 'list[Message]'` | One system message per segment, dropping empties. |
| `to_checkpoint` | `(state: 'RunState') -> 'LoopCheckpoint'` | Capture the resumable state of ``state`` at a turn boundary. |
| `verify_chain` | `(events: 'list[Event]') -> 'ChainVerificationResult'` | Verify integrity of an event chain. |

### Constants and type aliases

| Name | Value |
|---|---|
| `GENESIS_PREV_HASH` | `'0000000000000000000000000000000000000000000000000000000000000000'` |
| `SystemPrompt` | `str \| collections.abc.Sequence[str]` |
| `__version__` | `'0.9.0'` |

---

## arcagent

**Layer:** Agent — orchestrator wiring the layers together

ArcAgent: Enterprise-grade autonomous agent nucleus.

### Classes

#### `ArcAgentError`

Base error for all ArcAgent failures.

```python
from arcagent import ArcAgentError
```

Constructor: `ArcAgentError(code: 'str', message: 'str', component: 'str' = '', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `ConfigError`

TOML parse failure or Pydantic validation error.

```python
from arcagent import ConfigError
```

Constructor: `ConfigError(code: 'str', message: 'str', component: 'str' = '', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `ContextError`

Token budget exceeded, compaction failure, or prompt assembly error.

```python
from arcagent import ContextError
```

Constructor: `ContextError(code: 'str', message: 'str', component: 'str' = '', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `IdentityError`

Key generation, signing, verification, or DID creation failure.

```python
from arcagent import IdentityError
```

Constructor: `IdentityError(code: 'str', message: 'str', component: 'str' = '', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `ModuleBusError`

Handler failure, timeout, or module lifecycle error.

```python
from arcagent import ModuleBusError
```

Constructor: `ModuleBusError(code: 'str', message: 'str', component: 'str' = '', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `ToolError`

Tool execution failure, timeout, or transport error.

```python
from arcagent import ToolError
```

Constructor: `ToolError(code: 'str', message: 'str', component: 'str' = '', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `ToolVetoedError`

Tool execution was vetoed by a pre_tool handler.

```python
from arcagent import ToolVetoedError
```

Constructor: `ToolVetoedError(message: 'str', details: 'dict[str, Any] | None' = None) -> 'None'`

---

## arcmemory

**Layer:** Agent — dual-speed memory

arcmemory — Arc's dual-speed analogical memory substrate.

### Classes

#### `ACLViolation`

Raised when a memory operation violates the session ACL.

```python
from arcmemory import ACLViolation
```

Constructor: `ACLViolation(reason: 'str', caller_did: 'str' = '', target_did: 'str' = '') -> 'None'`

#### `AgenticResult`

Outcome of one agentic consolidation pass.

```python
from arcmemory import AgenticResult
```

Constructor: `AgenticResult(degraded: 'bool' = False, reason: 'str | None' = None, turns: 'int' = 0, tool_calls_made: 'int' = 0) -> None`

#### `ArcLLMDistiller`

arcmemory ``Distiller`` seam backed by an arcllm structured completion.

```python
from arcmemory import ArcLLMDistiller
```

Constructor: `ArcLLMDistiller(provider_factory: 'ProviderFactory', *, model: 'str | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async confirm_entity_merges` | `(groups: 'list[list[EntityRef]]') -> 'list[list[str]]'` | One bounded, conservative call per candidate cluster -> confirmed same-entity subgroups. |
| `async disambiguate_entity` | `(name: 'str', entity_type: 'str', candidates: 'list[str]') -> 'str | None'` | One bounded call → the existing slug this candidate IS, or None (new). |
| `async extract_events` | `(episodes: 'list[Event]') -> 'EventExtraction'` | One structured completion → things that happened in the USER's life. |
| `async extract_facts` | `(events: 'list[Event]') -> 'FactExtraction'` | One structured completion → additive semantic facts. |
| `async extract_procedures` | `(events: 'list[Event]', existing: 'list[Procedure]') -> 'ProcedureExtraction'` | One structured completion → the MERGED how-to cards (methods evolve). |
| `async mint_insights` | `(events: 'list[Event]', facts: 'list[Fact]') -> 'InsightMint'` | One structured completion → minted abstractions, the centerpiece. |
| `async summarize_day` | `(events: 'list[Event]') -> 'DaySummaryDraft'` | One structured completion → meeting-minutes daily notes (chronological). |

#### `ArcLLMEmbedder`

arcmemory ``Embedder`` seam backed by ``arcllm.embed`` (async, loop-safe).

```python
from arcmemory import ArcLLMEmbedder
```

Constructor: `ArcLLMEmbedder(*, model: 'str | None' = None, backend: 'str' = 'local', provider: 'Any' = None, base_url: 'str | None' = None, api_key: 'str' = '', telemetry: 'dict[str, Any] | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async embed_texts` | `(texts: 'list[str]') -> 'list[list[float]]'` | Embed via arcllm; translate any 'cannot serve' into the degrade signal. |

#### `ArcMemoryBrain`

arcmemory's implementation of arcagent's structural ``Brain`` seam.

```python
from arcmemory import ArcMemoryBrain
```

Constructor: `ArcMemoryBrain(workspace: 'Path | str', agent_did: 'str', *, config: 'MemoryConfig | None' = None, embedder: 'Embedder | None' = None, distiller: 'Distiller | None' = None, audit_sink: 'AuditSink | None' = None, seed_vocabulary: 'Iterable[str] | None' = None, model: 'object | None' = None, identity: 'AgentIdentity | None' = None, policy_pipeline: 'PolicyPipeline | None' = None, react_loop: 'ReactLoop' = <function run_react_loop at 0x108e64c20>) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async authorize` | `(operation: 'str', *, caller_did: 'str' = '') -> 'bool'` | Provider-side ACL gate the host's generic memory adapter consults per op. |
| `async capture` | `(text: 'str', *, kind: 'str' = 'observation', salience: 'float' = 0.0, classification: 'str' = 'unclassified', session_id: 'str | None' = None) -> 'None'` | Fast, zero-LLM capture of one untrusted text. |
| `async consolidate` | `(*, session_id: 'str | None' = None) -> 'Mapping[str, object]'` | Slow "sleep" consolidation over the raw stream. |
| `async rebuild_index` | `(*, session_id: 'str | None' = None) -> 'None'` | Re-derive the disposable indices from the glass-box files + stream. |
| `async recall` | `(query: 'str', *, clearance: 'str' = 'unclassified', top_k: 'int' = 5, budget: 'int' = 1024, summary: 'str' = '', cues: 'list[str] | None' = None, session_id: 'str | None' = None) -> 'list[RecallCard]'` | Structured glass-box recall — ranked cards WITH provenance + ``[[links]]``. |
| `async retrieve` | `(query: 'str', *, clearance: 'str' = 'unclassified', top_k: 'int' = 5, budget: 'int' = 1024, summary: 'str' = '', cues: 'list[str] | None' = None, session_id: 'str | None' = None) -> 'str'` | Single-pass, clearance-gated, boundary-marked recall. |

#### `Bundle`

The bounded, boundary-marked result of a single retrieval pass.

```python
from arcmemory import Bundle
```

#### `Confidence`

Whether a memory may be acted on directly or must be verified first.

```python
from arcmemory import Confidence
```

#### `ConsolidationResult`

Summary of one slow-path consolidation run (audit + observability).

```python
from arcmemory import ConsolidationResult
```

#### `Consolidator`

Orchestrates one bounded consolidation run for a single agent scope.

```python
from arcmemory import Consolidator
```

Constructor: `Consolidator(db: 'MemoryDB', workspace: 'Path', scope: 'Scope', *, distiller: 'distill.Distiller', config: 'MemoryConfig | None' = None, audit_sink: 'AuditSink | None' = None, embedder: 'Embedder | None' = None, confirmer: 'distill.EntityMergeConfirmer | None' = None, seed_vocabulary: 'Iterable[str] | None' = None, model: 'object | None' = None, identity: 'AgentIdentity | None' = None, policy_pipeline: 'PolicyPipeline | None' = None, react_loop: 'ReactLoop' = <function run_react_loop at 0x108e64c20>) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `due` | `(*, now: 'datetime', interval_minutes: 'float') -> 'bool'` | Whether the cadence interval has elapsed since the last run. |
| `hygiene_due` | `(*, now: 'datetime') -> 'bool'` | Whether the heavier nightly hygiene pass is due (first call of a new local day). |
| `last_run` | `() -> 'datetime | None'` | When consolidation last completed (None if it has never run here). |
| `async merge_cues` | `() -> 'list[tuple[str, str]]'` | Merge near-duplicate cues (embedding-cluster); repoint their links. |
| `async merge_entities` | `() -> 'list[tuple[str, str]]'` | Confirm-gated de-dup: candidate clusters -> ONE LLM call -> fold only confirmed. |
| `async recover` | `() -> 'bool'` | Recover from an interrupted run: rebuild the index from truth, clear marker. |
| `async run` | `(window: 'TimeWindow | None' = None, *, now: 'datetime | None' = None) -> 'ConsolidationResult'` | Run one bounded consolidation cycle; return its mutation counts. |
| `async run_hygiene` | `(*, now: 'datetime | None' = None) -> 'ConsolidationResult'` | Run the light pass, then the day-level hygiene: merge + backlink repair + dedup. |

#### `DedupReport`

Everything dedup did (or would do) for one workspace.

```python
from arcmemory import DedupReport
```

Constructor: `DedupReport(workspace: 'Path', stores: 'list[StoreReport]') -> None`

#### `Distiller`

The bounded structured-completion seam. Injected, never imported.

```python
from arcmemory import Distiller
```

| Method | Signature | Purpose |
|---|---|---|
| `async confirm_entity_merges` | `(groups: 'list[list[EntityRef]]') -> 'list[list[str]]'` |  |
| `async disambiguate_entity` | `(name: 'str', entity_type: 'str', candidates: 'list[str]') -> 'str | None'` |  |
| `async extract_events` | `(episodes: 'list[Event]') -> 'EventExtraction'` |  |
| `async extract_facts` | `(events: 'list[Event]') -> 'FactExtraction'` |  |
| `async extract_procedures` | `(events: 'list[Event]', existing: 'list[Procedure]') -> 'ProcedureExtraction'` |  |
| `async mint_insights` | `(events: 'list[Event]', facts: 'list[Fact]') -> 'InsightMint'` |  |
| `async summarize_day` | `(events: 'list[Event]') -> 'DaySummaryDraft'` |  |

#### `Embedder`

Vector seam: turn texts into fixed-width vectors. Injected, not imported.

```python
from arcmemory import Embedder
```

| Method | Signature | Purpose |
|---|---|---|
| `async embed_texts` | `(texts: 'list[str]') -> 'list[list[float]]'` |  |

#### `EmbeddingUnavailableError`

A *wired* embedder that cannot serve this call — arcmemory degrades.

```python
from arcmemory import EmbeddingUnavailableError
```

#### `Entity`

A person/place/project — a node in the semantic graph.

```python
from arcmemory import Entity
```

#### `EntityDisambiguator`

The single-method seam used by search-before-write identity resolution.

```python
from arcmemory import EntityDisambiguator
```

| Method | Signature | Purpose |
|---|---|---|
| `async disambiguate_entity` | `(name: 'str', entity_type: 'str', candidates: 'list[str]') -> 'str | None'` |  |

#### `EntityRecord`

One semantic entity as the operator view sees it.

```python
from arcmemory import EntityRecord
```

#### `EpisodicStore`

Append + read the raw event stream for one scope.

```python
from arcmemory import EpisodicStore
```

Constructor: `EpisodicStore(db: 'MemoryDB', workspace: 'Path') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `append` | `(event: 'Event') -> 'None'` | Persist one raw event to the stream with a per-scope monotonic seq. |
| `count` | `(scope_key: 'str') -> 'int'` | Total number of events stored for a scope. |
| `delete` | `(scope_key: 'str', event_id: 'str') -> 'bool'` | Remove an event by id; return whether a row was affected. |
| `events` | `(scope_key: 'str') -> 'list[Event]'` | Return all events for a scope, in stream (seq) order. |
| `get` | `(scope_key: 'str', event_id: 'str') -> 'Event | None'` | Fetch a single event by id within a scope (None if absent). |
| `page` | `(scope_key: 'str', *, limit: 'int', offset: 'int') -> 'list[Event]'` | Return one page of a scope's events, newest first (for the operator view). |
| `update_salience` | `(scope_key: 'str', event_id: 'str', salience: 'float') -> 'bool'` | Set an event's salience (the decay-slowing / importance field). |
| `update_text` | `(scope_key: 'str', event_id: 'str', text: 'str') -> 'bool'` | Replace an event's text; return whether a row was affected. |

#### `Event`

One raw episodic event — the high-volume append-only stream row.

```python
from arcmemory import Event
```

#### `EventCandidate`

One thing-that-happened the distiller proposes (the structured-output shape).

```python
from arcmemory import EventCandidate
```

#### `EventExtraction`

The structured result of the life-event-extraction completion.

```python
from arcmemory import EventExtraction
```

#### `EventStore`

Read/write life-event cards for one scope.

```python
from arcmemory import EventStore
```

Constructor: `EventStore(workspace: 'Path') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `path_for` | `(slug: 'str') -> 'Path'` | Absolute path to an event card (slug canonicalized). |
| `read` | `(slug: 'str') -> 'LifeEvent | None'` | Load an event card (None if absent). |
| `slugs` | `() -> 'list[str]'` | Every event slug currently on disk (sorted). |
| `upsert` | `(slug: 'str', title: 'str', *, date: 'str' = '', event_type: 'str' = 'unknown', participants: 'list[str] | None' = None, summary: 'str' = '', outcome: 'str' = '', classification: 'str' = 'unclassified') -> 'LifeEvent'` | Create or refresh an event card; participants union, classification only rises. |
| `write` | `(event: 'LifeEvent') -> 'Path'` | Render an event to markdown and atomically write it. |

#### `Fact`

A compact semantic fact-triplet about an entity.

```python
from arcmemory import Fact
```

#### `FactCandidate`

One fact the distiller proposes for a window (the structured-output shape).

```python
from arcmemory import FactCandidate
```

#### `FactExtraction`

The structured result of the fact-extraction completion.

```python
from arcmemory import FactExtraction
```

#### `FastCapture`

Wires the deterministic capture pipeline for one scope.

```python
from arcmemory import FastCapture
```

Constructor: `FastCapture(db: 'MemoryDB', workspace: 'Path', scope: 'Scope', graph: 'WeightedGraph', *, config: 'MemoryConfig | None' = None, audit_sink: 'AuditSink | None' = None, seed_vocabulary: 'Iterable[str] | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `capture` | `(text: 'str', *, kind: 'str' = 'observation', salience: 'float' = 0.0, classification: 'str' = 'unclassified') -> 'Event | None'` | Capture one untrusted text; return the ``Event`` or None if deduped. |

#### `GroupMerge`

One canonical slug and the variant files that collapse onto it.

```python
from arcmemory import GroupMerge
```

Constructor: `GroupMerge(canonical: 'str', sources: 'list[str]', deleted: 'int') -> None`

#### `IndexRebuilder`

Re-derives fts_chunks + vec0 + edges from files + the raw stream.

```python
from arcmemory import IndexRebuilder
```

Constructor: `IndexRebuilder(db: 'MemoryDB', workspace: 'Path', scope: 'Scope', *, config: 'MemoryConfig | None' = None, embedder: 'Embedder | None' = None, seed_vocabulary: 'Iterable[str] | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async rebuild` | `() -> 'None'` | Wipe every derived table and re-derive it from truth (idempotent). |

#### `Insight`

A minted abstraction — the centerpiece store.

```python
from arcmemory import Insight
```

#### `InsightBundle`

The enriched neighborhood of one matched insight (SDD 7 "spot, then enrich").

```python
from arcmemory import InsightBundle
```

#### `InsightCandidate`

One insight the distiller proposes (the minted-abstraction shape).

```python
from arcmemory import InsightCandidate
```

#### `InsightMint`

The structured result of the insight-minting completion.

```python
from arcmemory import InsightMint
```

#### `InsightStore`

Read/write insight cards for one scope.

```python
from arcmemory import InsightStore
```

Constructor: `InsightStore(workspace: 'Path') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `all_ids` | `() -> 'list[str]'` | Every insight id currently on disk (sorted). |
| `path_for` | `(insight_id: 'str') -> 'Path'` | Absolute path to an insight card (id canonicalized). |
| `read` | `(insight_id: 'str') -> 'Insight | None'` | Load an insight card (None if absent). |
| `write` | `(insight: 'Insight') -> 'Path'` | Render an insight to markdown and atomically write it. |

#### `LifeEvent`

A thing that HAPPENED in the user's life — a meeting, a sale, a call, a shipment.

```python
from arcmemory import LifeEvent
```

#### `LinkRecord`

A navigable edge from a memory or entity to a linked node.

```python
from arcmemory import LinkRecord
```

#### `MemoryACLConfig`

Tier-driven defaults for cross-session visibility.

```python
from arcmemory import MemoryACLConfig
```

| Method | Signature | Purpose |
|---|---|---|
| `default_for_tier` | `() -> 'CrossSessionVisibility'` | Return the default visibility for the configured tier. |

#### `MemoryConfig`

Immutable dynamics constants + budgets for one deployment tier.

```python
from arcmemory import MemoryConfig
```

#### `MemoryDB`

Opens/creates the per-agent index DB and owns its schema.

```python
from arcmemory import MemoryDB
```

Constructor: `MemoryDB(workspace: 'Path', *, dims: 'int' = 384) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `close` | `() -> 'None'` | Close the connection (idempotent). |
| `connect` | `() -> 'sqlite3.Connection'` | Open the DB (creating the file + schema on first call). |

#### `MemoryOperator`

Public read/mutation facade over one agent's memory database.

```python
from arcmemory import MemoryOperator
```

Constructor: `MemoryOperator(workspace: 'Path | str', agent_did: 'str', *, config: 'MemoryConfig | None' = None, embedder: 'Embedder | None' = None, seed_vocabulary: 'Iterable[str] | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `delete_entry` | `(entry_id: 'str', *, actor_did: 'str', session_id: 'str | None' = None) -> 'MutationResult'` | Delete a memory entry. |
| `edit_entry` | `(entry_id: 'str', text: 'str', *, actor_did: 'str', session_id: 'str | None' = None) -> 'MutationResult'` | Replace a memory entry's text. |
| `get_entity` | `(slug: 'str', *, session_id: 'str | None' = None) -> 'EntityRecord | None'` | Fetch a single entity record (None if absent). |
| `get_entry` | `(entry_id: 'str', *, session_id: 'str | None' = None) -> 'MemoryRecord | None'` | Fetch a single episodic memory (None if absent). |
| `links` | `(node_id: 'str', *, session_id: 'str | None' = None) -> 'list[LinkRecord]'` | Linked entities/memories for a memory or entity, navigable. |
| `list_daily_notes` | `() -> 'list[DaySummary]'` | Every day's curated notes, newest day first. |
| `list_entities` | `(*, session_id: 'str | None' = None) -> 'list[EntityRecord]'` | Return every semantic entity with its metadata. |
| `list_entries` | `(*, limit: 'int' = 50, offset: 'int' = 0, session_id: 'str | None' = None) -> 'MemoryPage'` | Return one page of episodic memories, newest first. |
| `list_events` | `() -> 'list[LifeEvent]'` | Every life-event card, most recent occurrence first (the user's timeline). |
| `list_insights` | `() -> 'list[Insight]'` | Every minted insight card, sorted by id (the curated glass-box centerpiece). |
| `list_procedures` | `() -> 'list[Procedure]'` | Every how-to procedure card, sorted by slug. |
| `read_daily_note` | `(day: 'str') -> 'DaySummary | None'` | Fetch one day's curated notes (None if absent). |
| `async search` | `(query: 'str', *, clearance: 'str' = 'unclassified', top_k: 'int' = 5, budget: 'int' = 1024, session_id: 'str | None' = None) -> 'list[Recall]'` | Ranked recall for ``query``, delegating to the production Retriever. |
| `set_metadata` | `(entry_id: 'str', *, actor_did: 'str', importance: 'int | None' = None, salience: 'float | None' = None, session_id: 'str | None' = None) -> 'MutationResult'` | Adjust a memory entry's importance / decay-relevant salience. |
| `summary` | `(*, session_id: 'str | None' = None) -> 'MemorySummary'` | Aggregate counts for the knowledge overview: stream + files + graph. |

#### `MemoryPage`

A page of episodic memories plus the totals needed to paginate.

```python
from arcmemory import MemoryPage
```

#### `MemoryRecord`

One episodic memory as the operator view sees it.

```python
from arcmemory import MemoryRecord
```

#### `MemoryTool`

A neutral tool spec — arcrun-agnostic (the adapter maps it onto arcrun).

```python
from arcmemory import MemoryTool
```

Constructor: `MemoryTool(name: 'str', description: 'str', input_schema: 'dict[str, Any]', execute: 'Callable[[dict[str, Any]], Awaitable[str]]', classification: 'str' = 'state_modifying') -> None`

#### `MutationResult`

The honest result of a single mutation — applied or error.

```python
from arcmemory import MutationResult
```

#### `MutationStatus`

Outcome of a facade mutation. There is no ``partial``.

```python
from arcmemory import MutationStatus
```

#### `ProceduralStore`

Read/write how-to cards for one scope.

```python
from arcmemory import ProceduralStore
```

Constructor: `ProceduralStore(workspace: 'Path') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `increment_use` | `(slug: 'str') -> 'int'` | Bump a card's use-count; return the new count (0 if the card is absent). |
| `path_for` | `(slug: 'str') -> 'Path'` | Absolute path to a procedure card (slug canonicalized). |
| `read` | `(slug: 'str') -> 'Procedure | None'` | Load a procedure card (None if absent). |
| `slugs` | `() -> 'list[str]'` | Every procedure slug currently on disk (sorted). |
| `upsert` | `(slug: 'str', title: 'str', *, when_to_use: 'str' = '', steps: 'list[str]', dropped: 'Sequence[str]' = (), classification: 'str' = 'unclassified') -> 'Procedure'` | Create or EVOLVE a card: steps merge (:func:`merge_steps`), use_count bumps. |
| `write` | `(procedure: 'Procedure') -> 'Path'` | Render a procedure to markdown and atomically write it. |

#### `Procedure`

A how-to card — a repeatable process, findable by its trigger.

```python
from arcmemory import Procedure
```

#### `ReactOutcome`

The engine-neutral result of one bounded ReAct run.

```python
from arcmemory import ReactOutcome
```

Constructor: `ReactOutcome(content: 'str | None' = None, degraded: 'bool' = False, reason: 'str | None' = None, turns: 'int' = 0, tool_calls_made: 'int' = 0, tokens_used: 'dict[str, Any]' = <factory>) -> None`

#### `Recall`

One retrieved item, ready to be boundary-marked and injected.

```python
from arcmemory import Recall
```

#### `RecallCard`

A glass-box recall result — a ranked card WITH provenance and links.

```python
from arcmemory import RecallCard
```

#### `Reranker`

Cross-encoder seam (D-9): score how well the situation instances each candidate.

```python
from arcmemory import Reranker
```

| Method | Signature | Purpose |
|---|---|---|
| `async rerank` | `(situation: 'str', candidates: 'list[str]') -> 'list[float]'` |  |

#### `Retriever`

One bounded retrieval path over the surface + structural indices for a scope.

```python
from arcmemory import Retriever
```

Constructor: `Retriever(db: 'MemoryDB', workspace: 'Path', scope: 'Scope', *, config: 'MemoryConfig | None' = None, embedder: 'Embedder | None' = None, audit_sink: 'AuditSink | None' = None, seed_vocabulary: 'Iterable[str] | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async index` | `() -> 'None'` | Incrementally (re)build both derived indices (content-gated, LLM10). |
| `async recall_cards` | `(situation: 'Situation', *, clearance: 'Classification', top_k: 'int' = 5, budget: 'int' = 1024, reranker: 'Reranker | None' = None) -> 'list[RecallCard]'` | Structured, glass-box recall: ranked cards WITH provenance + outbound links. |
| `async retrieve` | `(situation: 'Situation', *, clearance: 'Classification', top_k: 'int' = 5, budget: 'int' = 1024, reranker: 'Reranker | None' = None) -> 'Bundle'` | Fuse both channels, gate on clearance, and return a bounded bundle. |

#### `Scope`

Per-agent, shared-nothing isolation key.

```python
from arcmemory import Scope
```

#### `SemanticStatus`

The full readout: both halves of the channel, plus per-agent coverage.

```python
from arcmemory import SemanticStatus
```

#### `SemanticStore`

Read/write entity markdown + maintain the wiki-link graph for one scope.

```python
from arcmemory import SemanticStore
```

Constructor: `SemanticStore(workspace: 'Path', graph: 'WeightedGraph', scope: 'str') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `add_link` | `(src_slug: 'str', dst_slug: 'str') -> 'bool'` | Create a directed wiki-link edge and record it in ``src``'s frontmatter. |
| `aliases_index` | `() -> 'dict[str, str]'` | Map ``canonical(alias) -> owning entity slug`` across every card's aliases. |
| `merge_into` | `(canonical_slug_: 'str', other_slug: 'str') -> 'bool'` | Fold the ``other`` entity card into ``canonical`` and delete ``other``'s file. |
| `path_for` | `(slug: 'str') -> 'Path'` | Absolute path to an entity's markdown file (slug canonicalized). |
| `read` | `(slug: 'str') -> 'Entity | None'` | Load an entity from disk (None if it does not exist). |
| `resolve` | `(slug: 'str', name: 'str' = '') -> 'str'` | Deterministic identity resolution: exact file, then alias, else the raw slug. |
| `slugs` | `() -> 'list[str]'` | Every entity slug currently on disk (sorted). |
| `write_fact` | `(slug: 'str', predicate: 'str', value: 'str', *, confidence: 'float' = 0.5, name: 'str | None' = None, entity_type: 'str' = 'unknown', classification: 'str' = 'unclassified') -> 'Entity'` | Add/update a fact for an entity, folding a contradiction into a ``was:`` trail. |

#### `SessionACL`

Access control list for a session.

```python
from arcmemory import SessionACL
```

| Method | Signature | Purpose |
|---|---|---|
| `allows_read_by` | `(caller_did: 'str', agent_did: 'str') -> 'bool'` | Return True if ``caller_did`` may read memory from this session. |

#### `Situation`

The current turn abstracted for structural retrieval.

```python
from arcmemory import Situation
```

#### `StoreReport`

Per-store (entities/procedures/insights) dedup outcome for one workspace.

```python
from arcmemory import StoreReport
```

Constructor: `StoreReport(store: 'str', merges: 'list[GroupMerge]') -> None`

#### `StructuralIndex`

Trigger-embedding + cue-graph spreading over minted insights for one scope.

```python
from arcmemory import StructuralIndex
```

Constructor: `StructuralIndex(db: 'MemoryDB', workspace: 'Path', scope: 'Scope', *, config: 'MemoryConfig | None' = None, embedder: 'Embedder | None' = None, audit_sink: 'AuditSink | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `cue_match` | `(situation: 'Situation', *, top_k: 'int' = 5) -> 'list[tuple[str, float]]'` | Spread activation from the situation's cue nodes to the insight nodes. |
| `enrich` | `(insight_id: 'str', *, hops: 'int | None' = None) -> 'InsightBundle'` | Bundle a matched insight with its instances, neighbors, and stream context. |
| `async match` | `(situation: 'Situation', *, top_k: 'int' = 5, reranker: 'Reranker | None' = None) -> 'StructuralResult'` | Retrieve the insights the situation structurally instances (bounded). |
| `async trigger_index` | `() -> 'int'` | Embed each insight ``trigger`` into the separate table; content-gated. |
| `async trigger_match` | `(situation: 'Situation', *, top_k: 'int' = 5) -> 'list[tuple[str, float]] | None'` | Cosine-match the abstracted situation against insight triggers. |

#### `StructuralResult`

The bounded structural-channel result: confidence-gated recalls + degrade flag.

```python
from arcmemory import StructuralResult
```

#### `SurfaceIndex`

Incremental surface index + fused search for one agent scope.

```python
from arcmemory import SurfaceIndex
```

Constructor: `SurfaceIndex(db: 'MemoryDB', workspace: 'Path', scope: 'Scope', *, config: 'MemoryConfig | None' = None, embedder: 'Embedder | None' = None, audit_sink: 'AuditSink | None' = None, seed_vocabulary: 'Iterable[str] | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async index_if_needed` | `() -> 'int'` | Embed + index only new/changed chunks; return how many were (re)indexed. |
| `async search` | `(text: 'str', *, top_k: 'int' = 5) -> 'SurfaceResult'` | Fuse vec + bm25 + graph + recency; return the top-k boundary-ready recalls. |

#### `SurfaceResult`

The bounded surface-channel result: ranked recalls + a degrade flag.

```python
from arcmemory import SurfaceResult
```

#### `TimeWindow`

The slice of the raw stream one consolidation run reads.

```python
from arcmemory import TimeWindow
```

| Method | Signature | Purpose |
|---|---|---|
| `contains` | `(ts: 'str') -> 'bool'` | Whether ``ts`` falls within the (inclusive) window bounds. |

#### `WeightedGraph`

Hebbian/decay/spreading dynamics over the per-agent ``edges`` table.

```python
from arcmemory import WeightedGraph
```

Constructor: `WeightedGraph(db: 'MemoryDB', config: 'MemoryConfig | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `decay` | `(scope: 'str', *, now: 'datetime | None' = None, lam: 'float | None' = None) -> 'int'` | Decay every edge in ``scope``; forget those below the floor. |
| `hebbian_bump` | `(scope: 'str', a: 'str', b: 'str', *, kind: 'str' = 'assoc', m: 'float' = 1.0, salience: 'float' = 0.0, directed: 'bool' = False, ts: 'str | None' = None) -> 'float'` | Strengthen the ``a``-``b`` edge; return the new (saturating) weight. |
| `link` | `(scope: 'str', src: 'str', dst: 'str', *, kind: 'str', weight: 'float' = 1.0, ts: 'str | None' = None) -> 'None'` | Create/refresh a directed edge (wiki-link, insight->cue, insight->instance). |
| `neighbor_edges` | `(scope: 'str', node: 'str') -> 'list[tuple[str, str, float]]'` | Undirected neighbors of ``node`` as ``(neighbor, kind, weight)`` triples. |
| `neighbors` | `(scope: 'str', node: 'str') -> 'list[tuple[str, float]]'` | Undirected neighbors of ``node`` with edge weights. |
| `rename_node` | `(scope: 'str', old: 'str', new: 'str') -> 'int'` | Repoint every edge touching ``old`` onto ``new`` (cue-merge, T-054). |
| `spreading_activation` | `(scope: 'str', sources: 'dict[str, float]', *, max_hops: 'int | None' = None) -> 'dict[str, float]'` | Flow activation from ``sources`` over the weighted graph (ACT-R fan effect). |
| `weight` | `(scope: 'str', a: 'str', b: 'str', *, kind: 'str' = 'assoc') -> 'float'` | Current stored weight of an edge (0.0 if absent). Undirected by default. |

#### `WorkspaceVectors`

How much of one agent's index actually carries a vector.

```python
from arcmemory import WorkspaceVectors
```

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `boundary_mark` | `(recall: 'Recall') -> 'str'` | Wrap one recall in a ``<memory-result>`` block framed as DATA (LLM01). |
| `build_brain` | `(context: 'dict[str, Any]') -> 'ArcMemoryBrain'` | Build an arcllm-wired :class:`ArcMemoryBrain` from arcagent's generic context. |
| `build_memory_tools` | `(*, workspace: 'Path | str', db: 'MemoryDB', config: 'MemoryConfig', caller_did: 'str', session_id: 'str | None' = None, identity: 'AgentIdentity | None' = None, policy_pipeline: 'PolicyPipeline | None' = None, audit_sink: 'AuditSink | None' = None, embedder: 'Embedder | None' = None, distiller: 'EntityDisambiguator | None' = None) -> 'list[MemoryTool]'` | Build the wrapped memory tool set over one agent's memory (the agentic surface). |
| `confidence_from_hits` | `(hits: 'float', gamma: 'float') -> 'float'` | Memory confidence ``1 - e^(-gamma*hits)`` — rises, saturating, with corroboration. |
| `dedup_workspace` | `(workspace: 'Path', *, apply: 'bool') -> 'DedupReport'` | Dedup all three stores of one workspace's ``memory/`` directory (dry-run default). |
| `discover_workspaces` | `(root: 'Path') -> 'list[Path]'` | Return the workspace dir(s) reachable from ``root``. |
| `extract_acl_from_session_data` | `(session_data: 'dict[str, Any]', config: 'MemoryACLConfig', owner_did: 'str' = '') -> 'SessionACL'` | Extract a SessionACL from a session-metadata dict (e.g. a JSONL session record). |
| `async extract_events` | `(episodes: 'list[Event]', *, distiller: 'Distiller', store: 'EventStore', graph: 'WeightedGraph', scope: 'Scope', config: 'MemoryConfig') -> 'list[LifeEvent]'` | Record what happened in the USER's life; wire each participant as a graph edge. |
| `async extract_facts` | `(events: 'list[Event]', *, distiller: 'Distiller', store: 'SemanticStore', config: 'MemoryConfig', embedder: 'Embedder | None' = None) -> 'list[tuple[str, Fact]]'` | Apply the distiller's facts additively; return the (slug, fact) mutations. |
| `gate_no_read_up` | `(recalls: 'list[Recall]', *, clearance: 'Classification', strict: 'bool', actor_did: 'str', tier: 'str', audit_sink: 'AuditSink') -> 'list[Recall]'` | Drop every recall the caller's clearance does not dominate. |
| `async mint_insights` | `(events: 'list[Event]', facts: 'list[Fact]', *, distiller: 'Distiller', store: 'InsightStore', graph: 'WeightedGraph', scope: 'Scope', config: 'MemoryConfig') -> 'list[Insight]'` | Mint/corroborate insights; wire each cue as a graph node; return the cards. |
| `render_recalls` | `(recalls: 'list[Recall]') -> 'str'` | Render a bounded recall set into one boundary-marked, data-framed block. |
| `repair_backlinks` | `(store: 'SemanticStore') -> 'int'` | Write the reciprocal backlink into every wiki-link target card. Idempotent. |
| `reset_degrade_warnings` | `() -> 'None'` | Re-arm every warning (test isolation; the flag is process-global). |
| `async resolve_entity` | `(store: 'SemanticStore', *, slug: 'str', name: 'str', entity_type: 'str', embedder: 'Embedder | None' = None, distiller: 'EntityDisambiguator | None' = None, config: 'MemoryConfig | None' = None) -> 'str'` | Resolve a distiller-proposed entity onto its canonical slug (search-before-write). |
| `async run_agentic_consolidation` | `(*, episodes: 'list[Event]', model: 'object', tools: 'list[MemoryTool]', config: 'MemoryConfig', actor_did: 'str', react_loop: 'ReactLoop' = <function run_react_loop at 0x108e64c20>) -> 'AgenticResult'` | Run one bounded agentic consolidation; never raise, degrade on breach/timeout. |
| `async run_react_loop` | `(*, model: 'Any', tools: 'list[MemoryTool]', system_prompt: 'str', task: 'str', max_turns: 'int', max_tokens: 'int', timeout_seconds: 'float', actor_did: 'str') -> 'ReactOutcome'` | Run one bounded ReAct loop over the memory tools; never raise, degrade instead. |
| `semantic_degraded` | `() -> 'bool'` | True once any semantic-degrade reason has fired in this process. |
| `async semantic_status` | `(workspaces: 'Sequence[Path]' = (), *, embedder: 'Embedder | None', backend: 'str' = 'local') -> 'SemanticStatus'` | Probe the semantic channel end to end and report it (never raises). |
| `sqlite_vec_loadable` | `() -> 'bool'` | True if the sqlite-vec extension can load in this interpreter. |

### Constants and type aliases

| Name | Value |
|---|---|
| `CrossSessionVisibility` | `typing.Literal['private', 'shared-with-agent', 'shared-with-others-via-agent']` |
| `ReactLoop` | `collections.abc.Callable[..., collections.abc.Awaitable[arcmemory.react_adapter.` |
| `Tier` | `typing.Literal['personal', 'enterprise', 'federal']` |
| `__version__` | `'0.6.0'` |

---

## arctrust

**Layer:** Foundation — identity, signing, policy, audit (imports no sibling)

arctrust — Identity, keypair, audit, and policy primitives for Arc.

### Classes

#### `AgentIdentity`

Ed25519 identity with DID and sign/verify capabilities.

```python
from arctrust import AgentIdentity
```

Constructor: `AgentIdentity(did: 'str', public_key: 'bytes', _signing_key: 'SigningKey | None' = None, clearance: 'Classification' = <Classification.UNCLASSIFIED: 0>) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `save_keys` | `(key_dir: 'Path') -> 'None'` | Save keypair to filesystem with secure permissions. |
| `sign` | `(message: 'bytes') -> 'bytes'` | Sign a message with Ed25519. Returns 64-byte signature bytes. |
| `verify` | `(message: 'bytes', signature: 'bytes') -> 'bool'` | Verify a signature against this identity's public key. |

#### `AppendOnlyMediumWitness`

Offline/air-gapped witness: append the head to a second custodied file.

```python
from arctrust import AppendOnlyMediumWitness
```

Constructor: `AppendOnlyMediumWitness(medium_path: 'Path') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `submit` | `(checkpoint: 'dict[str, Any]', signature: 'bytes') -> 'str'` |  |
| `verify_inclusion` | `(checkpoint: 'dict[str, Any]', proof: 'str') -> 'bool'` |  |

#### `ArcTrustFipsError`

The federal FIPS floor was not met — refuse to proceed (fail-closed).

```python
from arctrust import ArcTrustFipsError
```

#### `ArtifactSignature`

Detached signature manifest written beside a signed artifact.

```python
from arctrust import ArtifactSignature
```

| Method | Signature | Purpose |
|---|---|---|
| `to_json` | `() -> 'str'` | Serialise to the ``.arcsig`` sidecar payload. |

#### `AuditEvent`

Immutable structured audit event.

```python
from arctrust import AuditEvent
```

#### `AuditSink`

Protocol for audit event sinks.

```python
from arctrust import AuditSink
```

| Method | Signature | Purpose |
|---|---|---|
| `write` | `(event: 'AuditEvent') -> 'None'` |  |

#### `CapabilitySource`

Source bundle to evaluate.

```python
from arctrust import CapabilitySource
```

Constructor: `CapabilitySource(name: 'str', source: 'str', signed: 'bool' = False) -> None`

#### `ChildIdentity`

Derived identity for a spawned child agent.

```python
from arctrust import ChildIdentity
```

#### `Classification`

US Government classification hierarchy (total order, low to high).

```python
from arctrust import Classification
```

#### `ClassificationLayer`

No-read-up gate at the tool surface — a pure predicate.

```python
from arctrust import ClassificationLayer
```

Constructor: `ClassificationLayer(*, enforced: 'bool' = False, relaxable: 'bool' = False) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async evaluate` | `(call: 'ToolCall', ctx: 'PolicyContext') -> 'Decision'` |  |

#### `ClearanceContext`

Caller clearance + resource classification for a call — filled by arcagent.

```python
from arctrust import ClearanceContext
```

#### `Decision`

Immutable result of a policy evaluation.

```python
from arctrust import Decision
```

| Method | Signature | Purpose |
|---|---|---|
| `is_deny` | `() -> 'bool'` | True when the outcome is deny. |

#### `FileNotaryTransit`

Reference :class:`VaultTransit`: signs via a separate notary process.

```python
from arctrust import FileNotaryTransit
```

Constructor: `FileNotaryTransit(keystore: 'Path', algorithm: 'str' = 'ed25519') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `public_key` | `(key_ref: 'str') -> 'bytes'` |  |
| `sign` | `(key_ref: 'str', message: 'bytes') -> 'bytes'` |  |

#### `InProcessSigner`

Signs in-process with a seed held in memory (Ed25519 or ECDSA-P256).

```python
from arctrust import InProcessSigner
```

Constructor: `InProcessSigner(seed: 'bytes', algorithm: 'str' = 'ed25519') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `sign` | `(message: 'bytes') -> 'bytes'` |  |

#### `KeyPair`

Immutable Ed25519 keypair.

```python
from arctrust import KeyPair
```

Constructor: `KeyPair(public_key: 'bytes', private_key: 'bytes') -> None`

#### `NullSink`

No-op audit sink. Events are discarded immediately.

```python
from arctrust import NullSink
```

| Method | Signature | Purpose |
|---|---|---|
| `write` | `(event: 'AuditEvent') -> 'None'` | Discard the event silently. |

#### `OperatorKey`

Ed25519 audit-signing credential for a deployment.

```python
from arctrust import OperatorKey
```

Constructor: `OperatorKey(seed: 'bytes', public_key: 'bytes') -> None`

| Method | Signature | Purpose |
|---|---|---|
| `into_signer` | `(algorithm: 'str' = 'ed25519') -> 'Signer'` | Adapt this on-disk operator key into an in-process :class:`Signer`. |
| `save` | `(path: 'Path') -> 'None'` | Persist the seed to ``path`` at ``0600`` via an atomic exclusive publish. |

#### `OperatorKeyIntegrityError`

The operator key is missing-after-present, symlinked, mis-owned, or swapped.

```python
from arctrust import OperatorKeyIntegrityError
```

#### `PolicyContext`

Runtime context for policy evaluation.

```python
from arctrust import PolicyContext
```

#### `PolicyLayer`

Single decision boundary within the pipeline.

```python
from arctrust import PolicyLayer
```

| Method | Signature | Purpose |
|---|---|---|
| `async evaluate` | `(call: 'ToolCall', ctx: 'PolicyContext') -> 'Decision'` |  |

#### `PolicyPipeline`

Ordered, short-circuiting, fail-closed policy evaluator.

```python
from arctrust import PolicyPipeline
```

Constructor: `PolicyPipeline(layers: 'list[PolicyLayer]', *, cache_ttl_seconds: 'float' = 0.0, max_bundle_age_seconds: 'float | None' = None, safe_set: 'set[str] | None' = None, shadow: 'bool' = False, audit_sink: 'AuditSink | None' = None, monotonic: 'MonotonicClock | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async evaluate` | `(call: 'ToolCall', ctx: 'PolicyContext') -> 'Decision'` | Run layered evaluation. First DENY wins. Exceptions are DENY. |

#### `Signer`

A source of non-repudiable signatures over arbitrary bytes.

```python
from arctrust import Signer
```

| Method | Signature | Purpose |
|---|---|---|
| `sign` | `(message: 'bytes') -> 'bytes'` |  |

#### `SignerConfig`

Config that selects a signer: custody model + algorithm + key reference.

```python
from arctrust import SignerConfig
```

#### `SignerError`

A signer could not be constructed or a custody invariant was violated.

```python
from arctrust import SignerError
```

#### `TofuDecision`

Outcome of a TOFU evaluation.

```python
from arctrust import TofuDecision
```

#### `TofuLayer`

Per-tier source-approval gate.

```python
from arctrust import TofuLayer
```

Constructor: `TofuLayer(tier: 'str', validators: 'ValidatorsConfig') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `evaluate` | `(target: 'CapabilitySource') -> 'TofuDecision'` |  |

#### `ToolCall`

Immutable request to invoke a tool.

```python
from arctrust import ToolCall
```

| Method | Signature | Purpose |
|---|---|---|
| `signing_bytes` | `() -> 'bytes'` | Canonical bytes the signature covers — every field except the auth pair. |

#### `TransparencyLogWitness`

Online Rekor-style witness — a thin submitter over an injected transport.

```python
from arctrust import TransparencyLogWitness
```

Constructor: `TransparencyLogWitness(*, transport: 'TransparencyLogTransport') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `submit` | `(checkpoint: 'dict[str, Any]', signature: 'bytes') -> 'str'` |  |
| `verify_inclusion` | `(checkpoint: 'dict[str, Any]', proof: 'str') -> 'bool'` |  |

#### `TrustStoreError`

Trust-store load, permission, or key-format failure.

```python
from arctrust import TrustStoreError
```

Constructor: `TrustStoreError(code: 'str', message: 'str', details: 'dict[str, Any] | None' = None) -> 'None'`

#### `ValidatorEntry`

A single TOFU-approved validator script (R-042 / R-043).

```python
from arctrust import ValidatorEntry
```

#### `ValidatorsConfig`

``[security.validators]`` block — TOFU policy state.

```python
from arctrust import ValidatorsConfig
```

#### `VaultSigner`

Signs by reference through a :class:`VaultTransit`; the seed never enters this process. The public key is cached from the transit.

```python
from arctrust import VaultSigner
```

Constructor: `VaultSigner(transit: 'VaultTransit', key_ref: 'str', algorithm: 'str' = 'ed25519') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `sign` | `(message: 'bytes') -> 'bytes'` |  |

#### `VaultTransit`

The out-of-process signing boundary (sign-by-reference).

```python
from arctrust import VaultTransit
```

| Method | Signature | Purpose |
|---|---|---|
| `public_key` | `(key_ref: 'str') -> 'bytes'` |  |
| `sign` | `(key_ref: 'str', message: 'bytes') -> 'bytes'` |  |

#### `WitnessAnchor`

External witness for an operator-signed checkpoint head.

```python
from arctrust import WitnessAnchor
```

| Method | Signature | Purpose |
|---|---|---|
| `submit` | `(checkpoint: 'dict[str, Any]', signature: 'bytes') -> 'str'` |  |
| `verify_inclusion` | `(checkpoint: 'dict[str, Any]', proof: 'str') -> 'bool'` |  |

#### `WitnessDivergenceError`

The local operator-signed head is not attested by the external witness.

```python
from arctrust import WitnessDivergenceError
```

#### `WormSink`

Durable, append-only, Ed25519-signed hash-chained audit log.

```python
from arctrust import WormSink
```

Constructor: `WormSink(path: 'Path', signer: 'Signer', *, genesis_tip: 'str' = '0000000000000000000000000000000000000000000000000000000000000000', max_records: 'int' = 100000, max_bytes: 'int' = 52428800) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `close` | `() -> 'None'` | Release the lock and close the file descriptor. |
| `verify_chain` | `(public_key: 'bytes | None' = None) -> 'bool'` | Self-check this chain. Delegates to the lock-free module verifier. |
| `write` | `(event: 'AuditEvent') -> 'None'` | Append one signed, chained record. Fail-open (AU-5). |

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `algorithm_is_fips_approved` | `(algorithm: 'str') -> 'bool'` | Return True when ``algorithm`` is FIPS-approved for Arc's backends. |
| `approve` | `(config_path: 'Path', *, name: 'str', source: 'str', approver: 'str', timestamp: 'str') -> 'ValidatorsConfig'` | Pin ``name`` to ``source``'s hash in ``config_path`` and persist (D1). |
| `approve_source` | `(validators: 'ValidatorsConfig', *, name: 'str', source: 'str', approver: 'str', timestamp: 'str') -> 'ValidatorsConfig'` | Record a TOFU approval — pin ``name`` to the current source hash (D1). |
| `arc_home` | `() -> 'Path'` | Return the user-wide Arc config root: ``${ARC_CONFIG_DIR:-~/.arc}``. |
| `assert_fips_if_required` | `(*, require_fips: 'bool', algorithm: 'str') -> 'None'` | Federal startup floor (SC-13 / IA-7). Fail-closed. |
| `build_pipeline` | `(*, tier: '_Tier', agent_registry: 'dict[str, bytes] | None' = None, global_deny_rules: 'dict[str, str] | None' = None, agent_allowlists: 'dict[str, set[str]] | None' = None, provider_limits: 'dict[str, ProviderLimit] | None' = None, team_roles: 'dict[str, frozenset[str]] | None' = None, classification_enforced: 'bool' = False, forbidden_compositions: 'list[frozenset[str]] | None' = None, cache_ttl_seconds: 'float' = 30.0, max_bundle_age_seconds: 'float | None' = None, safe_set: 'set[str] | None' = None, shadow: 'bool' = False, audit_sink: 'AuditSink | None' = None) -> 'PolicyPipeline'` | Build a tier-specific policy pipeline. |
| `build_signer` | `(config: 'SignerConfig', *, seed: 'bytes | None' = None, vault_transit: 'VaultTransit | None' = None) -> 'Signer'` | Resolve a :class:`Signer` from config. |
| `canonical_json` | `(obj: 'Any') -> 'bytes'` | Serialize ``obj`` to deterministic canonical-JSON bytes for signing. |
| `content_sha256` | `(content: 'bytes') -> 'str'` | Return the ``sha256:<hex>`` digest of ``content``. |
| `default_operator_key_path` | `() -> 'Path'` | Return the on-box operator-key file: ``<arc_home>/operator/operator.key``. |
| `derive_child_identity` | `(*, parent_sk_bytes: 'bytes', spawn_id: 'str', wallclock_timeout_s: 'float | None' = None, parent_clearance: 'Classification' = <Classification.UNCLASSIFIED: 0>, requested_clearance: 'Classification | None' = None) -> 'ChildIdentity'` | Derive a deterministic child identity from parent secret key and spawn id. |
| `disapprove` | `(config_path: 'Path', *, name: 'str') -> 'bool'` | Remove the pin for ``name`` from ``config_path`` and persist (drift / revoke). |
| `dominates` | `(clearance: 'Classification', resource: 'Classification') -> 'bool'` | True iff ``clearance`` is cleared for ``resource`` — the lattice ``⊒``. |
| `emit` | `(event: 'AuditEvent', sink: 'AuditSink') -> 'None'` | Emit an audit event to a sink, swallowing all sink errors. |
| `fips_backend_active` | `() -> 'bool'` | Return True when the loaded PyCA OpenSSL provider is FIPS-140-3-approved. |
| `generate_did` | `(verify_key: 'VerifyKey', *, org: 'str', agent_type: 'str') -> 'str'` | Derive a DID from an Ed25519 verify key. |
| `generate_keypair` | `() -> 'KeyPair'` | Generate a fresh Ed25519 keypair using a cryptographically secure RNG. |
| `hash_source` | `(source: 'str') -> 'str'` | Return the ``sha256:<hex>`` digest TOFU pins a capability source to. |
| `invalidate_cache` | `() -> 'None'` | Flush operator and issuer pubkey caches. |
| `load_issuer_pubkey` | `(did: 'str', *, trust_dir: 'Path | None' = None) -> 'bytes'` | Return the 32-byte Ed25519 pubkey for a manifest-issuer DID. |
| `load_operator_pubkey` | `(did: 'str', *, trust_dir: 'Path | None' = None) -> 'bytes'` | Return the 32-byte Ed25519 pubkey for an operator DID. |
| `load_validators` | `(config_path: 'Path') -> 'ValidatorsConfig'` | Read the ``[security.validators]`` block from an agent's ``arcagent.toml``. |
| `parse_classification` | `(value: 'str', *, strict: 'bool') -> 'Classification'` | Parse a classification label to the ladder. |
| `parse_did` | `(did: 'str') -> 'dict[str, str]'` | Parse a DID string into its component parts. |
| `persist_validators` | `(config_path: 'Path', validators: 'ValidatorsConfig') -> 'None'` | Rewrite only the ``[security.validators]`` block in ``arcagent.toml``. |
| `read_verified_anchor` | `(chain_path: 'Path', public_key: 'bytes', *, action: 'str' = 'trace.checkpoint', genesis_tip: 'str' = '0000000000000000000000000000000000000000000000000000000000000000') -> 'dict[str, Any] | None'` | Read the newest verified checkpoint anchor from a WORM chain. |
| `register_operator` | `(did: 'str', public_key: 'bytes', *, trust_dir: 'Path | None' = None, notes: 'str' = '') -> 'None'` | Create or update an operator's Ed25519 pubkey entry in operators.toml. |
| `sign` | `(message: 'bytes', private_key: 'bytes') -> 'bytes'` | Sign a message with an Ed25519 private key. |
| `sign_artifact` | `(content: 'bytes', *, signer_did: 'str', private_key: 'bytes') -> 'ArtifactSignature'` | Sign ``content`` with an Ed25519 private-key seed under ``signer_did``. |
| `validate_did` | `(did: 'str') -> 'str'` | Validate a DID string; return it if valid or empty string if blank. |
| `verify` | `(message: 'bytes', signature: 'bytes', public_key: 'bytes') -> 'bool'` | Verify an Ed25519 signature. |
| `verify_artifact` | `(content: 'bytes', manifest: 'ArtifactSignature', *, trusted_public_key: 'bytes | None' = None) -> 'bool'` | Re-verify signed ``content`` against its manifest at load time. |
| `verify_chain` | `(path: 'Path', public_key: 'bytes', *, genesis_tip: 'str' = '0000000000000000000000000000000000000000000000000000000000000000') -> 'bool'` | Validate a durable WORM chain on disk — no write lock required. |
| `verify_local_head_witnessed` | `(local_checkpoint: 'dict[str, Any] | None', witness: 'WitnessAnchor', *, federal: 'bool') -> 'None'` | Verify the local operator-signed head is attested by the external witness. |
| `verify_signature` | `(algorithm: 'str', message: 'bytes', signature: 'bytes', public_key: 'bytes') -> 'bool'` | Algorithm-dispatched verification. Never raises — returns False on any error. |
| `worm_policy_sink` | `(sink: 'AuditSink') -> 'Callable[[str, dict[str, Any]], None]'` | Adapt the policy pipeline's ``(event_type, payload)`` callback to a sink. |

### Constants and type aliases

| Name | Value |
|---|---|
| `ECDSA_P256` | `'ecdsa-p256'` |
| `ED25519` | `'ed25519'` |
| `__version__` | `'0.9.0'` |

---

## arcstore

**Layer:** Foundation — operational storage

arcstore — operational / observability data plane for Arc.

### Classes

#### `ArcStoreConfig`

The one canonical ``[arcstore]`` block (that teardown, §13.1).

```python
from arcstore import ArcStoreConfig
```

| Method | Signature | Purpose |
|---|---|---|
| `resolve_data_dir` | `() -> 'Path'` | Resolve this config's data dir with the shared env > toml > default rule. |

#### `SpoolRecord`

Immutable operational telemetry record.

```python
from arcstore import SpoolRecord
```

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `read` | `(path: 'Path') -> 'Iterator[SpoolRecord]'` | Iterate records from a spool file, skipping corrupt/torn lines. |
| `record` | `(rec: 'SpoolRecord', *, path: 'Path | None' = None) -> 'None'` | Append one record to the spool. Always-on, fail-open (AU-5). |
| `resolve_data_dir` | `(configured: 'str | Path | None' = None) -> 'Path'` | Resolve the Arc data directory with a single, shared precedence rule. |
| `spool_path` | `(*, data_dir: 'Path | None' = None) -> 'Path'` | Default spool file for today under the resolved Arc data dir. |
| `store_db_path` | `(data_dir: 'str | Path | None' = None) -> 'Path'` | Canonical path to the shared operational store DB (``store/arcui.db``). |

---

## arcteam

**Layer:** Agent — inter-agent messaging

ArcTeam: Multi-agent team coordination and lifecycle management.

### Classes

#### `AuditLogger`

Append-only audit trail with a chained per-record signature. AU-2/AU-9/AU-10.

```python
from arcteam import AuditLogger
```

Constructor: `AuditLogger(backend: 'StorageBackend', signer: 'Signer') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async initialize` | `() -> 'None'` | Load state from existing audit stream. Call once after construction. |
| `async log` | `(event_type: 'str', subject: 'str', actor_id: 'str', detail: 'str', stream: 'str' = '', msg_seq: 'int | None' = None, target_id: 'str | None' = None, classification: 'str' = 'UNCLASSIFIED') -> 'None'` | Append an audit record with a chained asymmetric signature. |
| `async verify_chain` | `() -> 'tuple[bool, int]'` | Verify the signature chain in batches. Returns (valid, last_verified_seq). |

#### `AuditRecord`

Tamper-evident audit entry.

```python
from arcteam import AuditRecord
```

#### `Channel`

Channel definition.

```python
from arcteam import Channel
```

#### `Cursor`

Per-entity read position in a stream.

```python
from arcteam import Cursor
```

#### `Entity`

Registered agent or user.

```python
from arcteam import Entity
```

#### `EntityRegistry`

DID-keyed agent and user registration with role-based queries.

```python
from arcteam import EntityRegistry
```

Constructor: `EntityRegistry(backend: 'StorageBackend', audit: 'AuditLogger') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async by_role` | `(role: 'str') -> 'list[Entity]'` | All entities with this role. Used for role-based addressing. |
| `async get` | `(ref: 'str') -> 'Entity | None'` | Read an entity by any address ref (DID, @handle, URI, bare handle). |
| `async list_entities` | `(role: 'str | None' = None) -> 'list[Entity]'` | All entities, optionally filtered by role. |
| `async register` | `(entity: 'Entity') -> 'None'` | Register a new entity. Rejects a duplicate DID or handle. |
| `async update` | `(entity: 'Entity') -> 'None'` | Replace an existing entity record. Emits `entity.updated`. |

#### `EntityStatus`

Registration state of an entity. A registered entity is ``active``.

```python
from arcteam import EntityStatus
```

#### `EntityType`

Type of registered entity.

```python
from arcteam import EntityType
```

#### `MemoryBackend`

In-memory storage backend for unit tests. Dict-backed, no filesystem.

```python
from arcteam import MemoryBackend
```

Constructor: `MemoryBackend() -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async append` | `(collection: 'str', key: 'str', entry: 'dict[str, Any]') -> 'int'` |  |
| `async append_auto_seq` | `(collection: 'str', key: 'str', entry: 'dict[str, Any]') -> 'tuple[int, int]'` | Atomically assign seq and append. Returns (seq, byte_offset). |
| `async delete` | `(collection: 'str', key: 'str') -> 'bool'` |  |
| `async exists` | `(collection: 'str', key: 'str') -> 'bool'` |  |
| `async get_stream_end_byte_pos` | `(collection: 'str', key: 'str') -> 'int'` | Return the current end-of-stream byte offset ( R-005). |
| `async list_keys` | `(collection: 'str', prefix: 'str | None' = None) -> 'list[str]'` |  |
| `async open_consumer` | `(collection: 'str', key: 'str', durable: 'str') -> 'Consumer'` | Bind a durable in-memory consumer that resumes from its ack floor. |
| `async query` | `(collection: 'str', filters: 'dict[str, Any] | None' = None, prefix: 'str | None' = None) -> 'list[dict[str, Any]]'` |  |
| `async read` | `(collection: 'str', key: 'str') -> 'dict[str, Any] | None'` |  |
| `async read_last` | `(collection: 'str', key: 'str') -> 'dict[str, Any] | None'` | Return the last entry in the stream, or None. |
| `async read_stream` | `(collection: 'str', key: 'str', after_seq: 'int' = 0, byte_pos: 'int' = 0, limit: 'int' = 100) -> 'list[dict[str, Any]]'` |  |
| `async write` | `(collection: 'str', key: 'str', data: 'dict[str, Any]') -> 'None'` |  |

#### `Message`

Message envelope. Maps to a NATS JetStream message.

```python
from arcteam import Message
```

#### `MessagingService`

Push + pull messaging. Zero arcagent dependency. Standalone service.

```python
from arcteam import MessagingService
```

Constructor: `MessagingService(backend: 'StorageBackend', registry: 'EntityRegistry', audit: 'AuditLogger', signer: 'MessageSigner | None' = None, strict_classification: 'bool' = False) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async ack` | `(stream: 'str', entity_id: 'str', seq: 'int', byte_pos: 'int') -> 'None'` | Advance cursor after successful processing. Forward-only. |
| `async cleanup_stale_cursors` | `(max_age_hours: 'int' = 24) -> 'int'` | Remove cursors older than max_age_hours. Returns count of removed cursors. |
| `async create_channel` | `(channel: 'Channel') -> 'None'` | Create a channel and its stream directory. |
| `async dlq_list` | `(limit: 'int' = 50) -> 'list[dict[str, Any]]'` | List Dead Letter Queue entries. |
| `async get_cursor` | `(stream: 'str', entity_id: 'str') -> 'Cursor | None'` | Get current cursor position (cached). |
| `async get_thread` | `(stream: 'str', thread_id: 'str') -> 'list[Message]'` | All messages in a thread, chronologically. |
| `async join_channel` | `(channel_name: 'str', entity_id: 'str') -> 'None'` | Add entity to channel membership. |
| `async leave_channel` | `(channel_name: 'str', entity_id: 'str') -> 'None'` | Remove entity from channel membership. |
| `async list_channel_messages` | `(channel_name: 'str', after_seq: 'int' = 0, limit: 'int' = 100) -> 'list[Message]'` | Read messages on a channel chronologically. |
| `async list_channels` | `() -> 'list[Channel]'` | Query channel definitions. |
| `async poll` | `(stream: 'str', entity_id: 'str', max_messages: 'int' = 10) -> 'list[Message]'` | Pull unread messages from a stream for this entity. |
| `async poll_all` | `(entity_id: 'str', max_per_stream: 'int' = 10) -> 'dict[str, list[Message]]'` | Poll all subscribed streams (inbox + channels + roles). |
| `async receive` | `(stream: 'str', entity_id: 'str', max_messages: 'int' = 10) -> 'list[Message]'` | Consume unread messages, verifying each before delivery. |
| `resolve_subscriptions` | `(entity_id: 'str', roles: 'list[str] | None' = None) -> 'list[str]'` | Resolve all streams an entity should poll. |
| `async send` | `(message: 'Message') -> 'Message'` | Send a message. Routes to appropriate stream(s) based on `to` URIs. |
| `async subscribe` | `(entity_id: 'str', handler: 'MessageHandler', *, durable_name: 'str | None' = None) -> 'Subscription'` | Push live messages to ``handler`` over durable consumers. |

#### `MsgType`

Message classification type.

```python
from arcteam import MsgType
```

#### `NatsBackend`

StorageBackend backed by NATS JetStream (records via KV, streams via JS).

```python
from arcteam import NatsBackend
```

Constructor: `NatsBackend(js: 'JetStreamContext', nc: 'Client | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async append` | `(collection: 'str', key: 'str', entry: 'dict[str, Any]') -> 'int'` | Append to a stream. Returns the assigned JetStream sequence. |
| `async append_auto_seq` | `(collection: 'str', key: 'str', entry: 'dict[str, Any]') -> 'tuple[int, int]'` | Publish to the stream. Returns ``(jetstream_seq, 0)``. |
| `async close` | `() -> 'None'` | Drain and close the underlying connection, if this owns one. |
| `async connect` | `(servers: 'str | list[str]', *, connect_timeout: 'float' = 3.0) -> 'NatsBackend'` | Open a JetStream-enabled NATS connection and wrap it. |
| `async delete` | `(collection: 'str', key: 'str') -> 'bool'` | Delete a record. Returns True if it existed. |
| `async exists` | `(collection: 'str', key: 'str') -> 'bool'` | Check if a record exists. |
| `async get_stream_end_byte_pos` | `(collection: 'str', key: 'str') -> 'int'` | Byte offsets are unused on JetStream (seq-addressed); always 0. |
| `async list_keys` | `(collection: 'str', prefix: 'str | None' = None) -> 'list[str]'` | List all keys in a collection. |
| `async open_consumer` | `(collection: 'str', key: 'str', durable: 'str') -> 'NatsConsumer'` | Bind (or create) a durable pull consumer for ``(collection, key)``. |
| `async query` | `(collection: 'str', filters: 'dict[str, Any] | None' = None, prefix: 'str | None' = None) -> 'list[dict[str, Any]]'` | Query records by field match or key prefix. |
| `async read` | `(collection: 'str', key: 'str') -> 'dict[str, Any] | None'` | Read a single JSON record. |
| `async read_last` | `(collection: 'str', key: 'str') -> 'dict[str, Any] | None'` | Read the last entry from a stream. |
| `async read_stream` | `(collection: 'str', key: 'str', after_seq: 'int' = 0, byte_pos: 'int' = 0, limit: 'int' = 100) -> 'list[dict[str, Any]]'` | Read stream entries with ``seq > after_seq`` (seq-addressed). |
| `async write` | `(collection: 'str', key: 'str', data: 'dict[str, Any]') -> 'None'` | Write/overwrite a single JSON record. |

#### `Priority`

Message priority level.

```python
from arcteam import Priority
```

#### `RetryableDeliveryError`

Signal from a subscribe handler that delivery hit transient backpressure.

```python
from arcteam import RetryableDeliveryError
```

#### `StorageBackend`

Swappable storage abstraction shared by the messenger, registry, and audit.

```python
from arcteam import StorageBackend
```

| Method | Signature | Purpose |
|---|---|---|
| `async append` | `(collection: 'str', key: 'str', entry: 'dict[str, Any]') -> 'int'` | Append to a stream. Returns an opaque sequence/offset. |
| `async append_auto_seq` | `(collection: 'str', key: 'str', entry: 'dict[str, Any]') -> 'tuple[int, int]'` | Atomically assign a sequence and append. Returns ``(seq, offset)``. |
| `async delete` | `(collection: 'str', key: 'str') -> 'bool'` | Delete a record. Returns True if it existed. |
| `async exists` | `(collection: 'str', key: 'str') -> 'bool'` | Check if a record exists. |
| `async get_stream_end_byte_pos` | `(collection: 'str', key: 'str') -> 'int'` | Return the current end-of-stream byte offset (0 for seq-addressed backends). |
| `async list_keys` | `(collection: 'str', prefix: 'str | None' = None) -> 'list[str]'` | List all keys in a collection. |
| `async open_consumer` | `(collection: 'str', key: 'str', durable: 'str') -> 'Consumer'` | Open (or rebind) a durable consumer for a stream. |
| `async query` | `(collection: 'str', filters: 'dict[str, Any] | None' = None, prefix: 'str | None' = None) -> 'list[dict[str, Any]]'` | Query records by field match or key prefix. |
| `async read` | `(collection: 'str', key: 'str') -> 'dict[str, Any] | None'` | Read a single JSON record. |
| `async read_last` | `(collection: 'str', key: 'str') -> 'dict[str, Any] | None'` | Read the last entry from a stream. |
| `async read_stream` | `(collection: 'str', key: 'str', after_seq: 'int' = 0, byte_pos: 'int' = 0, limit: 'int' = 100) -> 'list[dict[str, Any]]'` | Read entries from a stream starting after ``after_seq``. |
| `async write` | `(collection: 'str', key: 'str', data: 'dict[str, Any]') -> 'None'` | Write/overwrite a single JSON record. |

#### `Team`

A named group of member entities coordinated together.

```python
from arcteam import Team
```

#### `TeamConfig`

ArcTeam configuration with sensible defaults.

```python
from arcteam import TeamConfig
```

#### `TeamFileStore`

Store and retrieve files in the team's shared directory.

```python
from arcteam import TeamFileStore
```

Constructor: `TeamFileStore(team_root: 'Path') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async list_files` | `(agent_name: 'str | None' = None) -> 'list[dict[str, Any]]'` | List files in the shared directory. |
| `async store` | `(source_path: 'Path', agent_name: 'str') -> 'dict[str, Any]'` | Copy a file into the team's shared directory for an agent. |

#### `TeamMemoryConfig`

Team memory configuration. All fields have defaults.

```python
from arcteam import TeamMemoryConfig
```

#### `TeamMemoryService`

Shared team knowledge graph.

```python
from arcteam import TeamMemoryService
```

Constructor: `TeamMemoryService(config: 'TeamMemoryConfig', audit_logger: 'AuditLogger | None' = None, messenger: 'object | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async get_entity` | `(entity_id: 'str', agent_classification: 'Classification' = <Classification.UNCLASSIFIED: 0>) -> 'EntityFile | None'` | Get entity by ID. Returns None if not found or above clearance. |
| `async list_entities` | `(entity_type: 'str | None' = None, agent_classification: 'Classification' = <Classification.UNCLASSIFIED: 0>) -> 'list[IndexEntry]'` | List entities from index, classification-filtered. |
| `async promote` | `(entity_id: 'str', content: 'str', metadata: 'EntityMetadata', agent_id: 'str') -> 'PromotionResult'` | Write entry point. Validates, classifies, audits, writes. |
| `async rebuild_index` | `() -> 'dict[str, IndexEntry]'` | Force a full index rebuild. Returns the rebuilt index. |
| `async record_decision` | `(decision: 'dict[str, Any]', agent_id: 'str') -> 'None'` | Append decision to decisions JSONL log. |
| `async search` | `(query: 'str', agent_classification: 'Classification' = <Classification.UNCLASSIFIED: 0>, max_results: 'int' = 20, agent_id: 'str' = '') -> 'list[SearchResult]'` | BM25 search with wiki-link traversal, classification-filtered. |
| `async status` | `() -> 'MemoryStatus'` | Service status snapshot. |

#### `TeamStore`

Persist and mutate teams on a :class:`StorageBackend`, auditing each op.

```python
from arcteam import TeamStore
```

Constructor: `TeamStore(backend: 'StorageBackend', audit: 'AuditLogger') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async add_member` | `(team_id: 'str', did: 'str') -> 'Team'` | Add a member DID to a team (idempotent). Audits the addition. |
| `async create` | `(team: 'Team') -> 'Team'` | Persist a new team. Rejects a duplicate id. |
| `async get` | `(team_id: 'str') -> 'Team | None'` | Read a team by id, or None if absent. |
| `async list_teams` | `() -> 'list[Team]'` | Enumerate all teams. |
| `async remove_member` | `(team_id: 'str', did: 'str') -> 'Team'` | Remove a member DID from a team. Audits the removal. |

---

## arcprompt

**Layer:** Foundation — signed, overlay-able system prompts

arcprompt — editable, signed, inspectable system prompts for Arc.

### Classes

#### `PromptCatalog`

Enumerate stock prompts across a fixed set of installed packages.

```python
from arcprompt import PromptCatalog
```

Constructor: `PromptCatalog(packages: 'Sequence[str]' = ('arcrun', 'arcagent', 'arcmemory', 'arcskill')) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `catalog` | `() -> 'list[PromptRef]'` | Return every discoverable stock prompt, sorted by (package, name). |
| `stock_path` | `(package: 'str', name: 'str') -> 'Path | None'` | Return the stock file path for one prompt, or None if not packaged. |

#### `PromptDocument`

A resolved prompt: its validated frontmatter, body, and derived identity.

```python
from arcprompt import PromptDocument
```

#### `PromptError`

Base class for every arcprompt failure.

```python
from arcprompt import PromptError
```

#### `PromptFrontmatter`

Validated frontmatter carried by every prompt file.

```python
from arcprompt import PromptFrontmatter
```

#### `PromptMissing`

A packaged stock prompt is absent at load — a packaging error.

```python
from arcprompt import PromptMissing
```

Constructor: `PromptMissing(package: 'str', name: 'str') -> 'None'`

#### `PromptRef`

A discovered stock prompt: enough to list and locate it without loading overlays.

```python
from arcprompt import PromptRef
```

#### `PromptResolver`

Resolve a prompt to its effective body, overlay-over-stock, first-match-wins.

```python
from arcprompt import PromptResolver
```

Constructor: `PromptResolver(*, overlay_root: 'Path', trusted_public_key: 'bytes | None', posture: 'TrustPosture', catalog: 'PromptCatalog | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `overlay_path` | `(package: 'str', name: 'str') -> 'Path'` | Return the overlay file path for one prompt (may or may not exist). |
| `resolve` | `(package: 'str', name: 'str') -> 'PromptDocument'` | Resolve ``package:name`` to its effective document. |

#### `PromptSnapshot`

Immutable per-run mapping of ``(package, name)`` to its resolved document.

```python
from arcprompt import PromptSnapshot
```

Constructor: `PromptSnapshot(entries: 'Mapping[tuple[str, str], PromptDocument]') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `get` | `(package: 'str', name: 'str') -> 'PromptDocument'` | Return the frozen document resolved for this run, or raise KeyError. |
| `items` | `() -> 'list[tuple[tuple[str, str], PromptDocument]]'` |  |

#### `PromptUnparseable`

An overlay or stock file has malformed frontmatter or an empty body.

```python
from arcprompt import PromptUnparseable
```

#### `PromptUnsigned`

An overlay's Ed25519 signature is missing, invalid, or wrong-key.

```python
from arcprompt import PromptUnsigned
```

#### `SignatureVerifier`

Verify an overlay's detached signature against a single pinned public key.

```python
from arcprompt import SignatureVerifier
```

Constructor: `SignatureVerifier(trusted_public_key: 'bytes | None') -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `verify` | `(content: 'bytes', manifest: 'ArtifactSignature') -> 'bool'` | Return True only when the pin exists AND the artifact verifies against it. |

#### `TrustPosture`

Deployment stringency carried for provenance/audit context.

```python
from arcprompt import TrustPosture
```

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `load_stock` | `(package: 'str', name: 'str') -> 'str'` | Return the *body* of a package's stock prompt (see :func:`load_stock_document`). |
| `load_stock_document` | `(package: 'str', name: 'str') -> 'PromptDocument'` | Load one package's *stock* prompt directly, ignoring overlays. |
| `parse_prompt` | `(raw: 'bytes', *, source: 'Source', signer_did: 'str | None' = None) -> 'PromptDocument'` | Parse raw prompt-file bytes into a validated, frozen :class:`PromptDocument`. |
| `render_prompt` | `(body: 'str', *, name: 'str', description: 'str', tunable: 'bool' = True) -> 'bytes'` | Author a stock/overlay file from a body + frontmatter, honoring the newline rule. |
| `snapshot` | `(resolver: 'PromptResolver', refs: 'Sequence[PromptRef]', *, actor_did: 'str', sink: 'AuditSink', request_id: 'str | None' = None) -> 'PromptSnapshot'` | Resolve and freeze every prompt in ``refs``; emit one provenance event. |

### Constants and type aliases

| Name | Value |
|---|---|
| `DEFAULT_PROMPT_PACKAGES` | `('arcrun', 'arcagent', 'arcmemory', 'arcskill')` |
| `PromptResolve` | `collections.abc.Callable[[str, str], str]` |
| `__version__` | `'0.1.0'` |

---

## arcskill

**Layer:** Agent — skills and the skill improver

arcskill — Skill management hub for Arc.

### Classes

#### `PackageNotFoundError`

The package was not found.

```python
from arcskill import PackageNotFoundError
```

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `version` | `(distribution_name)` | Get the version string for the named package. |

---

## arcmodel

**Layer:** Foundation — model metadata

arcmodel — Arc model management. Coming soon.

This package exposes no public top-level symbols; it is consumed through
its submodules or its console entry point.

---

## arcui

**Layer:** Surface — web dashboard

arcui — Arc LLM telemetry dashboard.

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `attach_llm` | `(app: 'Starlette', instance: 'Any', label: 'str | None' = None) -> 'None'` | Register an LLM provider's modules for REST introspection. |
| `create_app` | `(*, auth_config: 'AuthConfig | None' = None, config_controller: 'Any | None' = None, agent_info: 'dict[str, str] | None' = None, max_agents: 'int' = 100, team_root: 'Path | None' = None, gateway_config: 'Any | None' = None, messaging_service: 'Any | None' = None, team_post_forwarder: 'Any | None' = None, team_stream_interval: 'float' = 1.0, data_dir: 'Path | None' = None, workspace_dir: 'Path | None' = None, allow_external_task_refs: 'bool' = False) -> 'Starlette'` | Build a Starlette application with all ArcUI routes. |
| `serve` | `(llm: 'Any' = None, *, host: 'str' = '127.0.0.1', port: 'int' = 8420, config_controller: 'Any | None' = None, auth_config: 'AuthConfig | None' = None) -> 'None'` | One-liner to start ArcUI dashboard. |

### Constants and type aliases

| Name | Value |
|---|---|
| `__version__` | `'0.3.0'` |

---

## arctui

**Layer:** Surface — terminal client

arctui — single-process Textual terminal UI for chatting with an ArcAgent.

This package exposes no public top-level symbols; it is consumed through
its submodules or its console entry point.

---

## arcmas

**Layer:** Surface — multi-agent system helpers

arcmas — the full Arc autonomous agent framework.

This package exposes no public top-level symbols; it is consumed through
its submodules or its console entry point.

---

## arcgateway

**Layer:** Surface — remote platform data plane

arcgateway — long-running daemon that makes ArcAgents reachable from any chat platform.

### Classes

#### `AsyncioExecutor`

In-process executor using asyncio tasks.

```python
from arcgateway import AsyncioExecutor
```

Constructor: `AsyncioExecutor(agent_factory: 'AgentFactory | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `async run` | `(event: 'InboundEvent') -> 'AsyncIterator[Delta]'` | Run ArcAgent in-process for the given event. |
| `set_agent_factory` | `(agent_factory: 'AgentFactory | None') -> 'None'` | Replace the agent factory after construction. |

#### `DeliveryTarget`

Parsed destination for an outbound message.

```python
from arcgateway import DeliveryTarget
```

#### `Delta`

One streamed chunk from an executor run.

```python
from arcgateway import Delta
```

#### `Executor`

Contract for running an ArcAgent in response to an InboundEvent.

```python
from arcgateway import Executor
```

| Method | Signature | Purpose |
|---|---|---|
| `async run` | `(event: 'InboundEvent') -> 'AsyncIterator[Delta]'` | Execute agent run for the given inbound event. |

#### `GatewayRunner`

Supervises platform adapters and routes messages to the SessionRouter.

```python
from arcgateway import GatewayRunner
```

Constructor: `GatewayRunner(adapters: 'list[BasePlatformAdapter] | None' = None, executor: 'Executor | None' = None, runtime_dir: 'Path | None' = None, pairing_store: 'Any | None' = None, user_allowlist: 'set[str] | None' = None, session_epoch_db_path: 'Path | None' = None) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `add_adapter` | `(adapter: 'BasePlatformAdapter') -> 'None'` | Register a platform adapter before run() is called. |
| `async run` | `() -> 'None'` | Start the gateway daemon. |
| `set_pairing_store` | `(pairing_store: 'Any') -> 'None'` | Register a PairingStore for background cleanup scheduling. |

#### `InboundEvent`

Normalised inbound message from any platform adapter.

```python
from arcgateway import InboundEvent
```

#### `SessionRouter`

Routes inbound events to per-session agent tasks.

```python
from arcgateway import SessionRouter
```

Constructor: `SessionRouter(executor: 'Executor', *, pairing_store: 'object | None' = None, user_allowlist: 'set[str] | None' = None, pairing_db_path: 'Path | None' = None, identity_graph: 'IdentityGraph | None' = None, adapter: 'BasePlatformAdapter | None' = None, adapter_map: 'dict[str, BasePlatformAdapter] | None' = None, delivery_target_factory: 'Any | None' = None, command_registry: 'CommandRegistry | None' = None, session_epoch_db_path: 'Path | None' = None, _test_hooks: 'bool' = True) -> 'None'`

| Method | Signature | Purpose |
|---|---|---|
| `active_session_count` | `() -> 'int'` | Return the number of currently active sessions. |
| `add_approved_user` | `(user_did: 'str') -> 'None'` | Add a user DID to the allowlist (called after pairing approval). |
| `current_session_key` | `(agent_did: 'str', user_did: 'str') -> 'str'` | Resolve the (agent, user) pair's *current* session key. |
| `dispatch_and_await` | `(event: 'InboundEvent', *, timeout: 'float' = 120.0) -> 'AsyncIterator[Delta]'` | Request/response dispatch — push an event, stream deltas back. |
| `async handle` | `(event: 'InboundEvent') -> 'None'` | Route an inbound event to its session. |
| `new_session` | `(agent_did: 'str', user_did: 'str') -> 'str'` | Rotate the (agent, user) session; return the new session key. |
| `queue_depth` | `(session_key: 'str') -> 'int'` | Return the number of queued (pending) events for a session. |
| `register_adapter` | `(adapter: 'BasePlatformAdapter') -> 'None'` | Register an adapter as the outbound channel for its platform. |
| `remove_approved_user` | `(user_did: 'str') -> 'None'` | Remove a user DID from the allowlist (e.g. on ban or re-pair). |
| `async send` | `(target: 'DeliveryTarget', message: 'str', *, agent_did: 'str' = '') -> 'None'` | Deliver an unsolicited outbound message to ``target``'s platform. |
| `set_adapter` | `(adapter: 'BasePlatformAdapter') -> 'None'` | Backwards-compatible alias for :meth:`register_adapter`. |

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `build_session_key` | `(agent_did: 'str', user_did: 'str', *, generation: 'int' = 0) -> 'str'` | Build a deterministic 16-hex-char session key from (agent, user) pair. |

---

## arccli

**Layer:** Surface — command line

arccmd — Unified CLI for Arc products.

### Classes

#### `PackageNotFoundError`

The package was not found.

```python
from arccli import PackageNotFoundError
```

### Functions

| Function | Signature | Purpose |
|---|---|---|
| `version` | `(distribution_name)` | Get the version string for the named package. |

