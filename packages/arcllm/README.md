<div align="center">

# 🌐 arcllm

### **One LLM Client. 16 Providers. Zero SDKs.**
*Direct HTTP to every major model provider. PII redaction, request signing, OpenTelemetry, and audit baked in.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-885-0055BC.svg)](#status)
[![Coverage](https://img.shields.io/badge/coverage-99%25-003B82.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#status)
[![Providers](https://img.shields.io/badge/providers-16-F68D2E.svg)](#-supported-providers)
[![No SDKs](https://img.shields.io/badge/vendor_SDKs-zero-54585C.svg)](#-zero-provider-sdks)

</div>

---

## ✨ What is arcllm?

`arcllm` is a provider-agnostic LLM client built around one principle: **never import a vendor SDK.**

Every call to OpenAI, Anthropic, Google, Cohere, Mistral, Groq — all 16 supported providers — is a direct HTTP request via `httpx`. You can read every byte. You can audit the wire format. You can run in environments where pulling in a transitive dependency you don't control isn't an option.

It also handles the boring-but-critical stuff that every production LLM client eventually grows: PII redaction, request signing, retries with exponential backoff, fallback chains across providers, rate limiting, OpenTelemetry export, structured audit events.

> 🛡️ **Three runtime dependencies. No vendor SDKs. Every byte on the wire is yours to inspect.**

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef entry fill:#D6E6FF,stroke:#0073FE,color:#002550

    arcrun[arcrun]:::runtime --> arcllm
    arcagent[arcagent]:::agent --> arcllm
    arccli[arccli]:::entry --> arcllm
    arcllm[arcllm<br/>16 providers · direct HTTP]:::llm --> arcstore[arcstore]:::found
```

Depends on one internal package — `arcstore` (hash-chained trace/audit storage) — plus three third-party libraries: `httpx`, `pydantic`, `opentelemetry-api`. **No vendor SDKs. That's the entire runtime dependency graph.**

---

## 🚀 Install

```bash
pip install arcllm           # standalone
# or
pip install arcmas           # full Arc stack
```

---

## 🧪 Quick Example

```python
from arcllm import load_model, Message

# Load a provider adapter with telemetry + audit enabled
model = load_model("anthropic", telemetry=True, audit=True)

response = await model.invoke([
    Message(role="user", content="Summarize this document.")
])

print(response.content)              # "The document describes..."
print(response.usage.total_tokens)   # 412
print(response.usage.cost_usd)       # 0.00318
print(response.stop_reason)          # "end_turn"
```

That's it. Switch to OpenAI? Change `"anthropic"` to `"openai"`. Switch to a local Ollama? Change it to `"ollama"`. **No code changes downstream.**

---

## 🌐 Supported Providers

All 16 go through direct HTTP. None pulls in a vendor SDK.

| Cloud | On-Prem (air-gapped) |
|---|---|
| **Anthropic** · Claude | **Ollama** — `localhost:11434` (Llama, Mistral, etc.) |
| **OpenAI** · GPT, o1, o3 | **vLLM** — `localhost:8000` (high-throughput GPU) |
| **Azure OpenAI** | **HuggingFace TGI** — `localhost:8080` |
| **Google** · Gemini | |
| **Cohere** · Command | |
| **Mistral** · Mistral, Codestral | |
| **Groq** · Llama, Mixtral | |
| **DeepSeek** · DeepSeek-V3, R1 | |
| **xAI** · Grok | |
| **Together** · open-weight models | |
| **Fireworks** · open-weight models | |
| **OpenRouter** · multi-provider gateway | |
| **NVIDIA** · NIM-hosted models | |
| **Moonshot** · Kimi | |
| **HuggingFace** · Inference API | |

Browse from the CLI:

```bash
arc llm providers                # list configured providers
arc llm provider anthropic       # show one provider's models + pricing
arc llm models --tools           # all models that support tool calling
arc llm models --vision          # all models that support vision
arc llm validate                 # test API key + connectivity per provider
```

---

## 🧩 The Module Stack (Decorator Pattern)

`arcllm` wraps the bare adapter in a stack of opt-in modules. The stacking order is deterministic:

```
Otel → Queue → Telemetry → Audit → Guardrails → Injection → Security → CircuitBreaker → Retry → Fallback → RateLimit → [Routing | LoadBalancer | Adapter]
```

Each module is one decorator that adds one concern.

| Module | What It Does | Why You Want It |
|---|---|---|
| **Otel** | Creates the root OpenTelemetry span; GenAI semantic conventions | Distributed tracing across services |
| **Telemetry** | Wall-clock timing, per-call USD cost, full raw prompt/response capture (encrypted at rest on the federal tier) | Cost attribution + forensic replay |
| **Audit** | Emits structured `arctrust` audit events | Compliance, forensics, post-hoc replay |
| **Guardrails** | Structural response validation: JSON-schema, regex allow/deny, length, banned-content (opt-in, per call) | Improper-output handling (LLM05) |
| **Injection** | Scans inbound user + tool-result content for prompt-injection (opt-in, off by default) | Prompt injection / context poisoning (LLM01, ASI06) |
| **Security** | Bidirectional PII + secret redaction (checksum-gated, gov/CUI entities, pluggable detector), HMAC-SHA256 request signing | Lethal Trifecta protection, tamper-evidence |
| **Retry** | Exponential backoff, jitter, retryable-error classification | Survives transient provider failures |
| **Fallback** | Failover chain across providers / models | Continuity when a provider is down |
| **LoadBalancer** | Intra-provider distribution across endpoints/keys — weighted RR, health-aware, sticky (opt-in) | Throughput + quota headroom (SC-5, LLM10) |
| **RateLimit** | Token bucket per provider | Stay inside provider quotas |
| **Adapter** | Direct HTTP to the provider | The actual call |

Toggle any of them per call:

```python
model = load_model(
    "anthropic",
    telemetry=True,      # cost tracking
    audit=True,          # arctrust audit events
    security=True,       # PII redaction + signing
    retry=True,          # exponential backoff
    rate_limit=True,
    otel=True,           # OpenTelemetry export
    injection=True,      # prompt-injection scan (off by default)
    guardrails={...},    # structural output validation, per call
    load_balance=True,   # spread across endpoints/keys of this provider
)
```

---

## 🛡️ Zero Provider SDKs

This is the headline. Arc imports **nothing** from `openai`, `anthropic`, `google-cloud-aiplatform`, `cohere`, `mistralai`, etc.

**Why it matters:**

- ❌ **No transitive dependency risk.** You can't be compromised by something your model SDK pulled in.
- ❌ **No opaque SDK behavior** in the trust boundary. SDKs do clever retries, masked headers, hidden state — Arc surfaces all of that explicitly.
- ❌ **No SDK version churn.** Provider releases a new SDK with breaking changes? Doesn't affect you.
- ✅ **Auditable byte-for-byte.** You can `tcpdump` the traffic. You can verify it.
- ✅ **Minimal supply chain.** `pydantic`, `httpx`, `opentelemetry-api`. Everything else is a dev dependency.

---

## 🛡️ Security Features

### 🚫 PII Redaction (Bidirectional)

Sensitive data gets redacted before it leaves your environment **and** when it comes back.

```python
# Built-in detectors (checksum-gated where applicable)
SSN, Credit Card (Luhn), Email, Phone, IPv4/IPv6, IBAN (mod-97), ABA routing

# Gov/CUI entities (opt-in via pii_entities)
US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN

# SECRETS category (togglable)
AWS keys, GitHub tokens, JWT, PEM blocks, DB URLs, sk-ant-/sk-/AIza/xox

# Bring your own
- pii_detector_class = "module:Class"   # allowlisted spaCy / Presidio / custom
```

Redacted patterns are replaced with `[PII:TYPE]` / `[SECRET:TYPE]` placeholders, in **both** the outbound request and the returned response (including `tool_calls` arguments).

### 🛡️ Prompt-Injection Detection (opt-in)

`InjectionModule` scans inbound **user turns and tool results** — the LLM01 and ASI06 vectors — before they reach the provider. It NFKC/zero-width-normalizes the scan copy to defeat encoding evasion, then matches an attack-pattern corpus (zero-dep) or, behind `arcllm[injection-semantic]`, an embedding-similarity tier. It **flags or blocks only** — it never interprets, rewrites, or executes content, and passes the messages through byte-identical. Off by default; `enforcement="warn"` runs observe-only before you flip to `block`.

### ✅ Output Guardrails (opt-in, per call)

`GuardrailsModule` validates the **final** response the caller receives: JSON-schema conformance, regex allow/deny lists, a length cap, and a banned-content stop-list. Structural only (LLM05) — semantic judgement (grounding, toxicity) stays in the agent layer. Configured per call via `guardrails={...}`.

### 🔐 Full Trace Capture & Encryption

Every call captures the full raw request and response for forensic replay (`load_for_replay` reconstructs the exact request; execution stays in arcrun). On the federal tier, bodies are sealed with an **AES-256-GCM envelope** (DEK wrapped via AES Key Wrap RFC 3394, KEK from vault, AAD bound to the record identity) — plaintext never touches disk. Records carry a `classification` tag; age/size retention purge bounds growth. The SHA-256 hash chain covers the ciphertext, so capture is tamper-evident for in-place mutation, reorder, and mid-chain deletion. (Head-truncation/rollback detection requires the arctrust `SignedChainSink` external anchor.) Right-to-erasure is intentionally **not** provided — it conflicts with federal audit retention.

### ✍️ Request Signing

Every LLM request can be cryptographically signed:

1. Messages, tools, and model name are serialized to **canonical JSON** (`sort_keys=True`, compact separators).
2. The canonical bytes are signed with **HMAC-SHA256** (Ed25519 in progress).
3. Signature + algorithm get attached to the response metadata.

Downstream systems can verify exactly what was sent — no "trust me, the prompt was..." in compliance reports.

### 🌐 HTTPS Enforcement

Provider base URLs are validated at config load. HTTP is **rejected** for all remote hosts. HTTP is permitted only for `localhost`, `127.0.0.1`, `[::1]` so local model servers (Ollama, vLLM) still work.

### 🔐 Vault Integration

API keys resolve from an external vault with TTL caching. The vault is a Protocol — any backend implementing `get_secret(path) -> str` works (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault, custom).

```toml
[vault]
backend = "https://vault.example.com"     # or empty for env-var fallback
token_path = "secret/arc/api-keys"
ttl_seconds = 300
```

Environment variable fallback is automatic when the vault is unreachable.

### 🪵 Log Injection Prevention

All structured log output sanitizes control characters (`\n`, `\r`, `\t`). Error bodies are truncated to 500 characters. Audit logs emit metadata only by default — content requires explicit DEBUG opt-in.

### 🛡️ Stateless Model Layer

The model object holds **configuration**, not conversation state. There is no hidden message history accumulating inside the provider abstraction. **Your code owns the message list.** State is explicit, inspectable, and serializable at every point.

---

## ⚙️ Configuration: TOML, Not Code

Adding a new provider that speaks the OpenAI API format takes a 5-line adapter file and a TOML config. **Zero registry edits, zero import changes.**

`providers/anthropic.toml`:

```toml
[provider]
name = "anthropic"
base_url = "https://api.anthropic.com/v1"
api_key_env = "ANTHROPIC_API_KEY"

[[models]]
id = "claude-sonnet-4-5-20250929"
context_window = 200000
max_output = 8192
supports_tools = true
supports_vision = true
input_price_per_1m = 3.00
output_price_per_1m = 15.00

[[models]]
id = "claude-haiku-4-5-20251001"
context_window = 200000
max_output = 8192
supports_tools = true
supports_vision = true
input_price_per_1m = 0.80
output_price_per_1m = 4.00
```

That's the entire model registry. No code change required to add a new model.

---

## 🧱 Public API

```python
from arcllm import (
    # Loader
    load_model, clear_cache,

    # Types
    LLMProvider, LLMResponse,
    Message, Tool, ToolCall, ToolUseBlock, ToolResultBlock,
    TextBlock, ImageBlock, ContentBlock,
    StopReason, Usage,

    # Config
    GlobalConfig, ProviderConfig, ProviderSettings,
    ModelMetadata, ModuleConfig, DefaultsConfig, VaultConfig,
    load_global_config, load_provider_config,

    # Errors
    ArcLLMError, ArcLLMAPIError, ArcLLMConfigError, ArcLLMParseError,
    QueueFullError, QueueTimeoutError,
    ArcLLMInjectionError, ArcLLMGuardrailError,
    ArcLLMTraceNotFoundError, ArcLLMTraceIntegrityError,
)
```

Provider adapters are lazy-imported — they're only loaded when you call `load_model("provider_name")`.

---

## 📋 Compliance Mapping

| NIST 800-53 | What `arcllm` Provides |
|---|---|
| AC-4 (Information Flow) | Bidirectional PII redaction at the trust boundary |
| AU-2, AU-12 | Audit module emits structured events on every call |
| AU-9 | Audit module integrates with `arctrust.WormSink` (durable signed hash chain) |
| AU-3, AU-11 | Full raw prompt/response capture with age/size retention purge |
| IA-5 | Vault-backed API key resolution; TTL caching; no plaintext on disk |
| SC-5 | Intra-provider load balancing distributes calls across endpoints/keys |
| SC-8 | HTTPS enforcement; HTTP only for loopback addresses |
| SC-13 | HMAC-SHA256 request signing; FIPS fail-closed self-check on the encryption path |
| SC-28 | AES-256-GCM envelope encryption of trace bodies at rest (federal tier) |
| SI-4 | OpenTelemetry export with GenAI semantic conventions |
| SI-10, SI-15 | Structural output guardrails; injection scanning of inbound content; checksum-gated PII/secret redaction |

| OWASP LLM (2025) | Mitigation |
|---|---|
| LLM01 (Prompt Injection) | `InjectionModule` scans inbound user + tool-result content (normalized against encoding evasion) before the model sees it |
| LLM02 (Sensitive Info Disclosure) | Bidirectional PII + secret detection; trace bodies encrypted at rest on the federal tier |
| LLM03 (Supply Chain) | Zero SDK imports; allowlisted pluggable detector loader |
| LLM04 (Data Poisoning) | Canonical-JSON request signing; tamper-evident hash-chained trace |
| LLM05 (Improper Output Handling) | `GuardrailsModule` — JSON-schema, regex allow/deny, banned-content on the response |
| LLM07 (System Prompt Leakage) | Vault-backed credentials, secret scanner, no secrets in prompts |
| LLM10 (Unbounded Consumption) | Token/cost tracking, rate limiter, retry caps, load balancing for quota headroom |
| ASI06 (Context Poisoning) | Tool-result injection scanning |

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcllm/tests
```

- **Tests:** 885
- **Coverage:** 99%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
