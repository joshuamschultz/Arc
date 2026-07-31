# PRD: Anthropic Adapter + Tool Support

> Feature-specific requirements for ArcLLM Step 3.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

ArcLLM has types (Step 1) and config (Step 2), but can't talk to any LLM yet. The Anthropic adapter is the first real I/O layer — it translates ArcLLM's clean, universal types into Anthropic's Messages API format, sends HTTP requests via httpx, and parses responses back into normalized `LLMResponse` objects. Without this, agents can't make LLM calls.

### Goal

Create the Anthropic adapter that:
1. Translates `Message[]` + `Tool[]` into Anthropic Messages API request format
2. Sends HTTP requests via httpx (async-first, connection reuse)
3. Parses Anthropic responses back into `LLMResponse` (content, tool_calls, usage, stop_reason)
4. Handles Anthropic-specific quirks (system message extraction, `input_schema` key naming, content block arrays)
5. Wraps API errors in `ArcLLMAPIError` with status code, body, and provider name
6. Resolves API keys from environment variables at init time (fail-fast)
7. Establishes the `BaseAdapter` pattern for future providers (OpenAI, Ollama)

### Success Criteria

- `AnthropicAdapter` takes `ProviderConfig` + model name, resolves API key from env
- `await adapter.invoke(messages)` returns a typed `LLMResponse` with correct content and usage
- `await adapter.invoke(messages, tools=tools)` returns `LLMResponse` with `tool_calls` parsed
- System messages extracted from `messages` list and sent as top-level `system` parameter
- API errors (429, 401, 500) raise `ArcLLMAPIError` with status code + body
- Missing API key env var raises `ArcLLMConfigError` at adapter init (not at first call)
- Adapter works as async context manager (`async with`)
- `BaseAdapter` provides shared plumbing (client, config, close) for all adapters
- `pytest tests/test_anthropic.py -v` shows all tests passing
- Zero new dependencies beyond httpx (already in deps)

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `BaseAdapter` provides shared init (config, model, httpx client, API key resolution) | P0 | All adapters inherit without duplicating client setup |
| FR-2 | `BaseAdapter` supports async context manager (`async with adapter:`) | P0 | `__aenter__` returns self, `__aexit__` closes client |
| FR-3 | `BaseAdapter` exposes `close()` for explicit cleanup | P0 | Closes httpx client, safe to call multiple times |
| FR-4 | `AnthropicAdapter` extracts system messages from `messages` list | P0 | System messages NOT in `messages` array, sent as `system` param |
| FR-5 | `AnthropicAdapter` translates `Message[]` to Anthropic format | P0 | Handles `str` and `list[ContentBlock]` content |
| FR-6 | `AnthropicAdapter` translates `Tool[]` to Anthropic format (`input_schema` key) | P0 | Our `parameters` field maps to Anthropic's `input_schema` |
| FR-7 | `AnthropicAdapter` sends request with correct headers (x-api-key, anthropic-version, content-type) | P0 | Headers verified in tests |
| FR-8 | `AnthropicAdapter` parses text response into `LLMResponse.content` | P0 | Text blocks concatenated into single string |
| FR-9 | `AnthropicAdapter` parses tool_use blocks into `LLMResponse.tool_calls` | P0 | Each `ToolCall` has id, name, parsed arguments dict |
| FR-10 | `AnthropicAdapter` parses `usage` (input_tokens, output_tokens, cache tokens) | P0 | All token fields mapped to `Usage` model |
| FR-11 | `AnthropicAdapter` maps `stop_reason` from Anthropic values | P0 | Maps Anthropic values to our normalized values |
| FR-12 | `AnthropicAdapter` stores raw response in `LLMResponse.raw` | P0 | Full response dict available for debugging |
| FR-13 | `ArcLLMAPIError` added to exception hierarchy | P0 | Has `status_code`, `body`, `provider` attributes |
| FR-14 | Non-200 HTTP responses raise `ArcLLMAPIError` | P0 | Status code, response body, provider name in error |
| FR-15 | Missing API key env var raises `ArcLLMConfigError` at init | P0 | Fail-fast, not lazy |
| FR-16 | `AnthropicAdapter` uses config temperature and max_tokens as defaults | P1 | Can be overridden via `**kwargs` |
| FR-17 | `AnthropicAdapter` passes thinking blocks to `LLMResponse.thinking` | P1 | When response contains thinking content blocks |
| FR-18 | Tool call argument parsing follows locked decision (type-check then json.loads) | P0 | Dict pass-through, string parsed, failure raises `ArcLLMParseError` |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Adapter overhead (excluding HTTP) | <1ms per call |
| NFR-2 | Connection reuse across calls | httpx client persists for adapter lifetime |
| NFR-3 | Zero new dependencies | Uses httpx (already in deps) |
| NFR-4 | Adapter tests run without real API calls | All tests use mocked httpx responses |
| NFR-5 | Clear error messages | API errors include status code, body, and provider |

---

## User Stories

### Agent Developer

> As an agent developer, I want to call `await adapter.invoke(messages, tools=tools)` and get back a normalized `LLMResponse` with parsed `tool_calls`, so I can execute tools and continue my agentic loop without knowing Anthropic's API format.

### Platform Engineer

> As a platform engineer, I want the adapter to resolve API keys from environment variables at init time, so misconfigurations are caught at startup — not during a live agent loop at 2am.

### Future Adapter Author

> As a developer adding a new provider, I want to inherit from `BaseAdapter` and only implement translation methods, so I don't have to duplicate httpx client setup, config storage, and cleanup logic.

---

## Out of Scope (Step 3)

- Streaming responses (future enhancement)
- Extended thinking configuration (just parse if present)
- Image content translation (structure exists but no test with real images)
- Retry logic (Step 7)
- Rate limiting (Step 8)
- `load_model()` integration (Step 6)
- Sync wrapper (later — async-first)

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
| httpx | Library | Already installed |
| Pydantic v2 | Library | Already installed |
| Anthropic Messages API format | External | Documented |
