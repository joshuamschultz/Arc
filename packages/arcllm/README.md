```
╭──────────────────────────────────────────────────────╮
│                                                      │
│   ▄▀█ █▀█ █▀▀ █   █   █▀▄▀█                        │
│   █▀█ █▀▄ █▄▄ █▄▄ █▄▄ █ ▀ █                        │
│                                                      │
│   Unified LLM Abstraction Layer                      │
│   for Autonomous Agents at Scale                     │
│                                                      │
├──────────────────────────────────────────────────────┤
│  13 Providers · 8 Modules · 2 Dependencies · <1ms   │
╰──────────────────────────────────────────────────────╯
```

**A minimal, security-first LLM abstraction layer built for autonomous agents at scale.**

ArcLLM normalizes communication across LLM providers into a single, clean interface designed for agentic tool-calling loops. One function to load a model, one method to invoke it, normalized responses every time — regardless of provider.

```python
from arcllm import load_model, Message

model = load_model("anthropic")

response = await model.invoke([
    Message(role="user", content="What is 2 + 2?")
])

print(response.content)       # "4"
print(response.usage)         # input_tokens=12 output_tokens=4 total_tokens=16
print(response.stop_reason)   # "end_turn"
```

Switch providers by changing one string. Your agent code stays the same.

---

## Why ArcLLM

**Built for federal and enterprise production environments** where thousands of autonomous agents run concurrently and security, auditability, and control are non-negotiable.

- **Security first** — API keys from environment variables or vault backends. PII redaction, HMAC request signing, and audit trails built in. No secrets in config files, ever.
- **Agent-native** — Purpose-built for agentic tool-calling loops, not chat interfaces. Stateless model objects. Your agent manages its own conversation history.
- **Minimal core** — Two runtime dependencies (`pydantic`, `httpx`). No provider SDKs. Direct HTTP to every provider. Import time under 100ms, abstraction overhead under 1ms per call.
- **Budget enforcement** — Per-scope spend tracking with calendar period resets. Pre-flight cost estimation. Per-call, daily, and monthly limits with configurable enforcement (`block`, `warn`, `log`).
- **Classification-aware routing** — Route LLM calls to specific providers based on data classification. CUI stays on cleared infrastructure. Unclassified goes to cost-optimized providers.
- **Opt-in complexity** — Need just Anthropic with no extras? That's all that loads. Need retry, fallback, telemetry, audit, routing, and budget controls? Enable them with a flag. Nothing runs that you didn't ask for.
- **Config-driven** — Model metadata, provider settings, and module toggles live in TOML files. Add a provider by dropping in one `.toml` file. Zero code changes.

---

## Supported Providers

| Provider | Type | Adapter |
|----------|------|---------|
| Anthropic | Cloud | `anthropic` |
| OpenAI | Cloud | `openai` |
| Azure OpenAI | Cloud (GCC) | `azure_openai` |
| DeepSeek | Cloud | `deepseek` |
| Mistral | Cloud | `mistral` |
| Moonshot | Cloud | `moonshot` |
| Groq | Cloud | `groq` |
| Together AI | Cloud | `together` |
| Fireworks AI | Cloud | `fireworks` |
| Hugging Face Inference | Cloud | `huggingface` |
| Hugging Face TGI | Self-hosted | `huggingface_tgi` |
| Ollama | Local | `ollama` |
| vLLM | Self-hosted | `vllm` |

Every adapter translates provider-specific quirks (role names, content formats, tool call schemas) so your agent code never has to.

---

## Opt-In Modules

All disabled by default. Enable via config or at load time.

| Module | What It Does |
|--------|-------------|
| **Retry** | Exponential backoff on transient errors (429, 500, 503). Respects `Retry-After` headers. |
| **Fallback** | Provider chain — if Anthropic fails, try OpenAI. Configurable order. |
| **Rate Limit** | Token-bucket throttling per provider. Prevents quota exhaustion across concurrent agents. |
| **Telemetry** | Timing, token counts, cost-per-call, and budget enforcement with automatic pricing from model metadata. |
| **Audit** | Structured call logging with metadata for compliance trails. PII-safe by default. |
| **Security** | PII redaction on requests/responses, HMAC request signing, vault-based key resolution. |
| **Routing** | Classification-aware provider/model selection. Route CUI to cleared providers, unclassified to cost-optimized. |
| **OpenTelemetry** | Distributed tracing export via OTLP (gRPC or HTTP). GenAI semantic conventions. |

Enable at load time:

```python
model = load_model("anthropic", retry=True, telemetry=True, audit=True)
```

Or override with custom settings:

```python
model = load_model("anthropic", retry={"max_retries": 5, "backoff_base_seconds": 2.0})
```

Or enable globally in `config.toml`:

```toml
[modules.retry]
enabled = true
max_retries = 3
backoff_base_seconds = 1.0
```

### Budget Enforcement

Control LLM spend at every level — per-call, daily, and monthly — with configurable enforcement:

```toml
[modules.telemetry]
enabled = true
monthly_limit_usd = 500.00
daily_limit_usd = 50.00
per_call_max_usd = 5.00
alert_threshold_pct = 80
enforcement = "block"        # block | warn | log
budget_scope = "my-agent"
```

Budget scopes are validated with NFKC Unicode normalization. Costs are clamped to prevent negative injection. Calendar period resets are automatic (UTC monthly/daily). Thread-safe for concurrent agents.

### Classification-Aware Routing

Route LLM calls based on data classification:

```toml
[modules.routing]
enabled = true
enforcement = "block"
default_classification = "unclassified"

[modules.routing.rules.cui]
provider = "anthropic"
model = "claude-sonnet-4-6"

[modules.routing.rules.unclassified]
provider = "openai"
model = "gpt-4o-mini"
```

CUI data stays on cleared providers. Unclassified data goes to cost-optimized providers. Enforcement modes: `block` (hard stop), `warn` (log + continue), `log` (silent).

---

## Installation

```bash
pip install -e "."
```

With dev tools:

```bash
pip install -e ".[dev]"
```

With OpenTelemetry export:

```bash
pip install -e ".[otel]"
```

With ECDSA request signing:

```bash
pip install -e ".[signing]"
```

**Requirements:** Python 3.12+

---

## Setup

### 1. Set your API key

ArcLLM reads API keys from environment variables by default. Never from config files.

```bash
export ANTHROPIC_API_KEY=your-key-here
```

See `.env.example` for all supported providers.

#### Vault integration (optional)

For enterprise environments, ArcLLM resolves API keys from vault backends with TTL caching and automatic env var fallback. Configure in `config.toml`:

```toml
[vault]
backend = "my_vault_module:HashicorpVaultBackend"
cache_ttl_seconds = 300
```

Then set vault paths per provider in their TOML files:

```toml
[provider]
api_key_env = "ANTHROPIC_API_KEY"
vault_path = "secret/data/llm/anthropic"
```

Resolution order: vault (cached) -> environment variable -> error. The vault backend is a pluggable protocol — implement `get_secret(path)` and `is_available()` for any secrets manager (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault, etc.).

### 2. Load and invoke

```python
from arcllm import load_model, Message

model = load_model("anthropic")

response = await model.invoke([
    Message(role="user", content="Summarize this document.")
])

print(response.content)
```

Use the async context manager to ensure clean connection shutdown:

```python
async with load_model("anthropic") as model:
    response = await model.invoke(messages)
```

### 3. Switch providers

```python
model = load_model("openai")                          # OpenAI
model = load_model("azure_openai", "my-deployment")   # Azure OpenAI (GCC)
model = load_model("groq")                            # Groq
model = load_model("ollama")                          # Local Ollama
model = load_model("together")                        # Together AI
```

Same `Message` types, same `invoke()` call, same `LLMResponse` back.

---

## Tool-Calling (Agentic Loop)

This is what ArcLLM was built for. Define tools, send them with your messages, and handle the loop:

```python
from arcllm import load_model, Message, Tool, TextBlock, ToolUseBlock, ToolResultBlock

model = load_model("anthropic")

# Define a tool
search_tool = Tool(
    name="web_search",
    description="Search the web for current information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"}
        },
        "required": ["query"],
    },
)

messages = [Message(role="user", content="Search for the latest Python release.")]

# Agentic loop
while True:
    response = await model.invoke(messages, tools=[search_tool])

    if response.stop_reason == "end_turn":
        print(response.content)
        break

    if response.stop_reason == "tool_use":
        # Pack the assistant's response back into messages
        assistant_content = []
        if response.content:
            assistant_content.append(TextBlock(text=response.content))
        for tc in response.tool_calls:
            assistant_content.append(
                ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments)
            )
        messages.append(Message(role="assistant", content=assistant_content))

        # Execute tools and send results back
        for tc in response.tool_calls:
            result = execute_tool(tc.name, tc.arguments)  # your implementation
            messages.append(Message(
                role="tool",
                content=[ToolResultBlock(tool_use_id=tc.id, content=result)],
            ))
```

Every provider returns the same `LLMResponse` with the same `ToolCall` objects and the same `stop_reason` values. Your agentic loop works across all 13 providers without modification.

---

## Core Types

ArcLLM's type system is the contract between your agent and any LLM provider.

| Type | Purpose |
|------|---------|
| `Message` | Input message with `role` and `content` |
| `Tool` | Tool definition sent to the LLM |
| `LLMResponse` | Normalized response: `content`, `tool_calls`, `usage`, `stop_reason` |
| `ToolCall` | Parsed tool call: `id`, `name`, `arguments` (always a dict) |
| `Usage` | Token accounting: input, output, total, cache, reasoning |
| `ContentBlock` | Union of `TextBlock`, `ImageBlock`, `ToolUseBlock`, `ToolResultBlock` |

All types are Pydantic v2 models with full validation and serialization.

---

## Architecture

```
Agent Code
    |
load_model() ---- Public API
    |
Modules ---------- opt-in: retry, fallback, telemetry, audit, security, routing, otel
    |
Adapter ---------- provider-specific translation (one .py per provider)
    |
Types ------------ pydantic models (the universal contract)
    |
Config ----------- TOML files (global defaults + per-provider metadata)
```

**Design principles:**

1. Library, not a framework — import what you need, nothing more
2. No state in the LLM layer — model objects hold config, agents hold conversation
3. Provider quirks stay in adapters — your code sees clean, normalized types
4. Fail fast, fail loud — errors carry raw data, nothing is silently swallowed
5. Config-driven — add a provider by dropping in a TOML file, not writing code

---

## Simplicity by the Numbers

ArcLLM is radically smaller than alternatives. This is a design choice, not a limitation.

| Metric | ArcLLM | pi-ai | LiteLLM |
|--------|--------|-------|---------|
| **Source LOC** | ~3,500 | ~22,600 | ~475,000 |
| **Source files** | 35 | 38 | 1,558 |
| **Runtime deps** | 3 | 13 | 12+ |
| **Providers** | 13 | 9 | 100+ |

**LOC per provider**: ArcLLM averages ~60 lines per provider adapter. Most are 11-line thin aliases over the OpenAI-compatible base. pi-ai averages ~630 lines per provider. LiteLLM averages ~1,300 lines per provider.

Why this matters:

- **Auditable** — A security reviewer can read the entire LLM layer in an afternoon. Try that with 475K lines.
- **Debuggable** — When something breaks, the call stack is shallow. No framework magic, no middleware chains you can't trace.
- **Maintainable** — Fewer lines means fewer bugs. Every line in ArcLLM exists because it has to, not because a feature flag needed a feature flag.
- **Fast** — 3 runtime dependencies means fast installs, small container images, and minimal attack surface. pi-ai requires `@anthropic-ai/sdk`, `openai`, `@google/genai`, `@aws-sdk/client-bedrock-runtime`, and 9 more packages. LiteLLM requires `openai`, `tiktoken`, `tokenizers`, `aiohttp`, `jinja2`, `jsonschema`, and more.

The tradeoff is provider count: LiteLLM supports 100+ providers because it wraps provider SDKs. pi-ai wraps 4 provider SDKs (Anthropic, OpenAI, Google, AWS) to cover 9 API backends. ArcLLM supports 13 providers via direct HTTP — no SDKs, no transitive dependency trees. Adding a new OpenAI-compatible provider is an 11-line file and a TOML config.

*LOC measured with `find src -name "*.py" | xargs wc -l` (ArcLLM) and `find src -name "*.ts" | xargs wc -l` (pi-ai), excluding tests. Competitor numbers from public GitHub repos as of February 2026.*

---

## Configuration

**Global defaults** (`src/arcllm/config.toml`):

```toml
[defaults]
provider = "anthropic"
temperature = 0.7
max_tokens = 4096

[vault]
backend = ""
cache_ttl_seconds = 300

[modules.retry]
enabled = false
max_retries = 3
backoff_base_seconds = 1.0

[modules.fallback]
enabled = false
chain = ["anthropic", "openai"]

[modules.routing]
enabled = false
enforcement = "warn"
default_classification = "unclassified"

[modules.telemetry]
enabled = false
log_level = "INFO"
# monthly_limit_usd = 500.00
# daily_limit_usd = 50.00
# per_call_max_usd = 5.00
# alert_threshold_pct = 80
# enforcement = "block"

[modules.security]
enabled = false
pii_enabled = true
signing_enabled = true
signing_algorithm = "hmac-sha256"
signing_key_env = "ARCLLM_SIGNING_KEY"
```

**Provider config** (`src/arcllm/providers/anthropic.toml`):

```toml
[provider]
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
default_model = "claude-sonnet-4-20250514"
vault_path = ""

[models.claude-sonnet-4-20250514]
context_window = 200000
max_output_tokens = 8192
supports_tools = true
supports_vision = true
cost_input_per_1m = 3.00
cost_output_per_1m = 15.00
```

Model metadata (context windows, capabilities, pricing) lives in config, not code. Update a model's pricing or add a new model variant without touching a single line of Python.

---

## Running Tests

```bash
pytest -v                       # Unit + adapter tests (mocked)
pytest --cov=arcllm             # With coverage
pytest tests/security/          # Security-specific tests
pytest tests/test_agentic_loop.py  # Live API test (requires ANTHROPIC_API_KEY)
```

---

## Security

ArcLLM is built security-first for federal production environments. See **[docs/security.md](docs/security.md)** for the full security reference including NIST 800-53 and OWASP mapping.

Key capabilities:

- **API key isolation** — Keys from env vars or vault only. Never in config, logs, or responses.
- **PII redaction** — Automatic detection and redaction of SSN, credit cards, emails, phone numbers, and IPs on both inbound and outbound messages.
- **Request signing** — HMAC-SHA256 signatures on every request payload for tamper detection and non-repudiation.
- **Vault integration** — Pluggable secrets backend with TTL caching. Supports any vault (HashiCorp, AWS SM, Azure KV).
- **Audit trails** — Structured compliance logging. PII-safe metadata by default, raw content opt-in at DEBUG.
- **Budget enforcement** — Per-scope spend limits with negative cost injection prevention, Unicode homoglyph attack resistance, and thread-safe accumulation.
- **Classification routing** — Data classification-aware provider selection prevents CUI from reaching unauthorized providers.
- **TLS enforced** — All provider communication over HTTPS via httpx defaults.
- **OpenTelemetry** — Distributed tracing with mTLS support for secure telemetry export.

---

## Project Status

ArcLLM is in active development. Core foundation, all provider adapters, the module system, budget enforcement, and classification routing are complete and tested.

| Phase | Status |
|-------|--------|
| Core Foundation (types, config, adapters, registry) | Complete |
| Module System (retry, fallback, rate limit, telemetry, audit, security, otel) | Complete |
| Enterprise (vault integration, request signing, PII redaction) | Complete |
| Budget enforcement (per-scope, daily, monthly, per-call) | Complete |
| Classification-aware routing | Complete |

---

## License

This project is licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

Copyright (c) 2025-2026 BlackArc Systems.

---

## Acknowledgments

ArcLLM was inspired by [pi-ai](https://github.com/badlogic/pi-mono/tree/main/packages/ai) (from [pi-mono](https://github.com/badlogic/pi-mono) by Mario Zechner) and [LiteLLM](https://github.com/BerriAI/litellm). We studied their architectures and built something purpose-fit for Python, federal environments, and autonomous agent fleets.