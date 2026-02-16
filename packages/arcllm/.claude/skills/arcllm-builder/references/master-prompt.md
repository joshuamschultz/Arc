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

- [x] Step 1: Project setup + pydantic types
- [x] Step 2: Config loading (global + provider TOMLs)
- [ ] Step 3: Core — single provider adapter (Anthropic) with tool support
- [ ] Step 4: Test harness — verify core works in an agentic loop
- [ ] Step 5: Add second provider (OpenAI) — validate the abstraction
- [ ] Step 6: Provider registry + load_model() interface
- [ ] Step 7: Module: Fallback + retry logic
- [ ] Step 8: Module: Rate limiter
- [ ] Step 9: Module: Router — model selection, routing rules
- [ ] Step 10: Module: Telemetry (timing, tokens, cost)
- [ ] Step 11: Module: Audit trail (call logging, reasoning capture)
- [ ] Step 12: Module: Budget manager
- [ ] Step 13: Module: Observability (OpenTelemetry export)
- [ ] Step 14: Security layer — API key vault, request signing, PII redaction hooks
- [ ] Step 15: Local/open-source provider support (Ollama, vLLM)
- [ ] Step 16: Integration test — full agentic loop with all modules

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
