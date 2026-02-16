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
│  11 Providers · 7 Modules · 2 Dependencies · <1ms   │
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
- **Opt-in complexity** — Need just Anthropic with no extras? That's all that loads. Need retry, fallback, telemetry, audit, and rate limiting? Enable them with a flag. Nothing runs that you didn't ask for.
- **Config-driven** — Model metadata, provider settings, and module toggles live in TOML files. Add a provider by dropping in one `.toml` file. Zero code changes.

---

## Supported Providers

| Provider | Type | Adapter |
|----------|------|---------|
| Anthropic | Cloud | `anthropic` |
| OpenAI | Cloud | `openai` |
| DeepSeek | Cloud | `deepseek` |
| Mistral | Cloud | `mistral` |
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
| **Telemetry** | Timing, token counts, and cost-per-call with automatic pricing from model metadata. |
| **Audit** | Structured call logging with metadata for compliance trails. PII-safe by default. |
| **Security** | PII redaction on requests/responses, HMAC request signing, vault-based key resolution. |
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

**Requirements:** Python 3.11+

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
model = load_model("openai")          # OpenAI
model = load_model("groq")            # Groq
model = load_model("ollama")          # Local Ollama
model = load_model("together")        # Together AI
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

Every provider returns the same `LLMResponse` with the same `ToolCall` objects and the same `stop_reason` values. Your agentic loop works across all 11 providers without modification.

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
Modules ---------- opt-in: retry, fallback, telemetry, audit, security, otel
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
- **TLS enforced** — All provider communication over HTTPS via httpx defaults.
- **OpenTelemetry** — Distributed tracing with mTLS support for secure telemetry export.

---

## Project Status

ArcLLM is in active development. Core foundation, all provider adapters, and the module system are complete and tested.

| Phase | Status |
|-------|--------|
| Core Foundation (types, config, adapters, registry) | Complete |
| Module System (retry, fallback, rate limit, telemetry, audit, security, otel) | Complete |
| Enterprise (vault integration, request signing, PII redaction) | Complete |
| Router, budget manager | In progress |

---

## License

This project is licensed under the [Creative Commons Attribution-NoDerivatives 4.0 International License (CC BY-ND 4.0)](https://creativecommons.org/licenses/by-nd/4.0/).

You are free to use and share this software, provided you give appropriate credit. You may not distribute modified versions.

Copyright (c) 2025 BlackArc Systems / CTG Federal.
