# ArcLLM — Master Prompt (Reference Copy)

> Original location: docs/arcllm-master-prompt.md

## Project

**ArcLLM** — A modular, security-first unified LLM abstraction layer purpose-built for agentic workflows. Minimal core, everything else is opt-in modules.

## Architecture Principles

1. Core is minimal. It does ONE thing: send messages to an LLM and get a response back in a normalized format, including tool calls.
2. Security first, control second, functionality third.
3. Built FOR agents — not humans chatting. Every design decision assumes this is inside an agentic loop doing tool calling.
4. No SDK. This is imported directly into agent code.
5. Everything beyond core is a pluggable module imported when needed.

## Locked Decisions

- **Language**: Python 3.11+
- **Types/validation**: Pydantic v2 (minimize core code, leverage validation)
- **Testing**: pytest + pytest-asyncio
- **Async**: Async-first with sync wrapper
- **Config format**: TOML (stdlib tomllib, zero dependency)
- **Config structure**: Global `config.toml` + one TOML per provider in `providers/`
- **Model interface**: `load_model()` returns a stateless model object with `.invoke()`. Knows its config, settings, model metadata. Holds NO conversation state — agent manages its own messages.
- **Provider adapters**: One `.py` file per provider in `adapters/`, lazy loaded based on config
- **Model metadata**: Per-provider TOML files, config-driven, overridable
- **API keys**: Environment variables (`.env`), vault integration later
- **Content model**: Union type — `str | list[ContentBlock]` — supports text, image, tool_use, tool_result from the start
- **Message roles**: Standard four internally (system, user, assistant, tool). Provider-specific roles (e.g., OpenAI's "developer") handled by adapter translation layer.
- **Tool parameters**: Loose/flexible — `dict[str, Any]` (raw JSON schema)
- **Tool call argument parsing**: Always parse (type-check + json.loads). Raise `ArcLLMParseError` on failure with raw string attached. No elaborate fallback — let the agent loop handle errors.
- **Usage tracking**: Grab everything available (cache tokens, reasoning tokens) as optional fields for security/audit later
- **LLM Response**: Includes stop_reason and thinking field for observability/audit
- **Provider interface**: Includes `validate_config()` method
- **HTTP client**: httpx (async-native, lightweight)

## Agent Interface (target)

```python
from arcllm import load_model

model = load_model("anthropic")                                  # default model
model = load_model("anthropic", "claude-sonnet-4-20250514")      # specific model
model = load_model("anthropic", telemetry=True)                  # with modules

response = await model.invoke(messages, tools=my_tools)
```

## Config Structure

```
arcllm/
├── config.toml              # global defaults + module toggles
├── providers/
│   ├── anthropic.toml       # provider config + model metadata
│   ├── openai.toml
│   └── ollama.toml
├── adapters/
│   ├── __init__.py
│   ├── anthropic.py         # translates arcllm types <-> Anthropic API
│   ├── openai.py
│   └── ollama.py
```

## Core Types

- **ContentBlock**: Discriminated union — TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock
- **Message**: role (Literal), content (str | list[ContentBlock])
- **Tool**: name, description, parameters (dict)
- **ToolCall**: id, name, arguments (dict, always parsed)
- **Usage**: input_tokens, output_tokens, total_tokens + optional cache/reasoning tokens
- **LLMResponse**: content, tool_calls, usage, model, stop_reason, thinking, raw
- **LLMProvider**: Abstract base — name, invoke(), validate_config()
- **ArcLLMParseError**: Custom exception with raw string + original error

## Module Map (build order)

### Phase 1-2: Core + Providers (Complete)

- [x] Step 1: Project setup + pydantic types
- [x] Step 2: Config loading (global + provider TOMLs)
- [x] Step 3: Anthropic adapter with tool support
- [x] Step 4: Test harness
- [x] Step 5: OpenAI adapter
- [x] Step 6: Provider registry + load_model() interface

### Phase 2-3: Modules + Observability (Mostly Complete)

- [x] Step 7: Module: Fallback + retry logic
- [x] Step 8: Module: Rate limiter
- [ ] ~~Step 9: Module: Router~~ (deferred -- see Step 26)
- [x] Step 10: Module: Telemetry (timing, tokens, cost)
- [x] Step 11: Module: Audit trail (call logging, reasoning capture)
- [ ] Step 12: Module: Budget manager (config exists, no implementation)
- [x] Step 13: Module: Observability (OpenTelemetry export)

### Phase 4: Enterprise (Mostly Complete)

- [x] Step 14: Security layer (PII redaction, HMAC-SHA256 signing, vault, TLS)
- [x] Step 15: Local/open-source provider support (Ollama, vLLM + 10 thin adapters)
- [ ] Step 15.1: ECDSA P-256 signing (stub exists, not implemented)
- [ ] Step 16: Integration test -- full agentic loop with all modules

### Phase 5: Security Hardening

- [ ] Step 17: Module: Tool call validator (allowlist + JSON Schema validation)
- [ ] Step 18: Module: Content scanner (injection detection)

### Phase 6: Streaming & Structured Output (Critical Gap)

- [ ] Step 19: Streaming support (AsyncIterator[StreamChunk] across all adapters + modules)
- [ ] Step 20: Structured output (JSON mode, JSON Schema constrained responses)

### Phase 7: LLM Parity Features

- [ ] Step 21: Prompt caching (send-side cache_control)
- [ ] Step 22: Extended thinking / reasoning params
- [ ] Step 23: Pre-call token counting
- [ ] Step 24: Embeddings API
- [ ] Step 25: Module: Response caching (in-memory + Redis)
- [ ] Step 26: Module: Compliance-aware routing (Step 9 revisited)

### Phase 8: Advanced Capabilities

- [ ] Step 27: Model discovery (list_models)
- [ ] Step 28: Batch API
- [ ] Step 29: Guardrail hooks (pre/post invoke callbacks)
- [ ] Step 30: Multimodal expansion (audio, video, document)

### Phase 9: Compliance Documentation

- [ ] SBOM generation (pip-audit + cyclonedx-bom)
- [ ] Threat model document (data flow, trust boundaries)
- [ ] IR runbook (detection criteria mapped to telemetry)

> Full roadmap with details: `docs/roadmap.md`

## Target Environment

- **Organizations**: BlackArc Systems, CTG Federal
- **Scale**: Thousands of concurrent autonomous agents
- **Compliance**: Federal production environments, FedRAMP pathway
- **Security**: Auditable, traceable, no API keys in config files
- **Performance**: Abstraction adds <1ms overhead on calls that take 500-5000ms

## Dependencies

### Core (required)
- pydantic >= 2.0
- httpx >= 0.25

### Dev
- pytest
- pytest-asyncio

### Runtime (zero additional for config)
- tomllib (stdlib, Python 3.11+)
