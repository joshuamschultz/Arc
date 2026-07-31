# PRD: OpenAI Adapter + StopReason Normalization

> Feature-specific requirements for ArcLLM Step 5.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

ArcLLM has one working adapter (Anthropic) but the abstraction is unvalidated. Without a second adapter, we don't know if the types, base adapter, and contract actually generalize. The OpenAI adapter proves the architecture works and exposes where Anthropic-specific assumptions leaked into the core. Additionally, `stop_reason` is currently a raw `str` — agents checking `stop_reason == "end_turn"` will silently fail on OpenAI responses (which use `"stop"` instead). The type must be normalized.

### Goal

Create the OpenAI Chat Completions adapter that:
1. Translates `Message[]` + `Tool[]` into OpenAI Chat Completions API request format
2. Sends HTTP requests via httpx (reusing `BaseAdapter` plumbing from Step 3)
3. Parses OpenAI responses back into `LLMResponse` (content, tool_calls, usage, stop_reason)
4. Handles OpenAI-specific quirks (system messages in-line, `function` wrapper for tools, `choices[0].message` nesting, JSON string tool arguments)
5. Normalizes `finish_reason` to canonical `StopReason` values
6. Flattens `ToolResultBlock` messages into OpenAI's one-message-per-tool-result format
7. Validates the abstraction — same agent code works with both providers

### Success Criteria

- `StopReason` type added to `types.py` as `Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]`
- `LLMResponse.stop_reason` uses `StopReason` type (not `str`)
- All existing 84 tests still pass (Anthropic already uses canonical values)
- `OpenAIAdapter` takes `ProviderConfig` + model name (same as Anthropic)
- `await adapter.invoke(messages)` returns typed `LLMResponse` with correct content and usage
- `await adapter.invoke(messages, tools=tools)` returns `LLMResponse` with `tool_calls` parsed
- System messages pass through in-line (NOT extracted like Anthropic)
- OpenAI `finish_reason` values mapped to canonical `StopReason`
- Tool result messages flattened from ArcLLM format to OpenAI format
- API errors (429, 401, 500) raise `ArcLLMAPIError` with status code + body
- `pytest tests/test_openai.py -v` shows all tests passing
- Zero new dependencies

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `StopReason` type defined as `Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]` | P0 | Type exists in types.py, exported from __init__.py |
| FR-2 | `LLMResponse.stop_reason` uses `StopReason` type | P0 | Pydantic validates values at construction |
| FR-3 | `OpenAIAdapter` inherits `BaseAdapter` | P0 | Config, API key, httpx client inherited |
| FR-4 | `OpenAIAdapter` uses Bearer token auth | P0 | `Authorization: Bearer {key}` header |
| FR-5 | `OpenAIAdapter` keeps system messages in-line | P0 | No extraction — OpenAI handles system as regular message |
| FR-6 | `OpenAIAdapter` wraps tools in `{"type": "function", "function": {...}}` format | P0 | Our `parameters` maps to OpenAI's nested function format |
| FR-7 | `OpenAIAdapter` parses response from `choices[0].message` | P0 | Handles OpenAI response nesting |
| FR-8 | `OpenAIAdapter` parses `tool_calls` from message-level array | P0 | OpenAI puts tool_calls on message, not in content blocks |
| FR-9 | `OpenAIAdapter` maps `finish_reason` to canonical `StopReason` | P0 | `stop`->`end_turn`, `tool_calls`->`tool_use`, `length`->`max_tokens` |
| FR-10 | `OpenAIAdapter` parses usage from `prompt_tokens`/`completion_tokens` | P0 | Maps to our `input_tokens`/`output_tokens` |
| FR-11 | `OpenAIAdapter` flattens tool result messages | P0 | One ArcLLM message with N ToolResultBlocks -> N OpenAI messages |
| FR-12 | `OpenAIAdapter` parses tool call arguments from JSON string | P0 | OpenAI always returns arguments as JSON string |
| FR-13 | Non-200 HTTP responses raise `ArcLLMAPIError` | P0 | Status code, response body, provider name in error |
| FR-14 | `OpenAIAdapter` stores raw response in `LLMResponse.raw` | P0 | Full response dict for debugging |
| FR-15 | `OpenAIAdapter` uses config temperature and max_tokens as defaults | P1 | Overridable via `**kwargs` |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Adapter overhead (excluding HTTP) | <1ms per call |
| NFR-2 | Connection reuse across calls | Inherited from BaseAdapter |
| NFR-3 | Zero new dependencies | Uses httpx (already in deps) |
| NFR-4 | All adapter tests run without real API calls | Mocked httpx responses |
| NFR-5 | Existing tests unaffected | 84 tests still pass |

---

## User Stories

### Agent Developer

> As an agent developer, I want to switch from Anthropic to OpenAI by changing one config value, and have my agentic tool-calling loop work identically — same `invoke()` call, same `LLMResponse` shape, same `stop_reason` checks.

### Platform Engineer

> As a platform engineer, I want `stop_reason` to be type-safe so typos in stop reason checks are caught at validation time, not at runtime during a live agent loop.

---

## Out of Scope (Step 5)

- Streaming responses
- OpenAI-specific features (function calling legacy format, structured outputs)
- Retry logic (Step 7)
- Rate limiting (Step 8)
- `load_model()` integration (Step 6)
- Sync wrapper
- OpenAI reasoning_tokens (future enhancement — o1/o3 models)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1 (types + exceptions) | Prerequisite | COMPLETE |
| Step 2 (config loading) | Prerequisite | COMPLETE |
| Step 3 (BaseAdapter + AnthropicAdapter) | Prerequisite | COMPLETE |
| Step 4 (agentic loop test) | Prerequisite | COMPLETE |
| httpx | Library | Already installed |
| Pydantic v2 | Library | Already installed |
| OpenAI Chat Completions API format | External | Documented |
