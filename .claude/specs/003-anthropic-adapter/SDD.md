# SDD: Anthropic Adapter + Tool Support

> System design for ArcLLM Step 3.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 3 creates the adapter layer — the translation engine between ArcLLM's universal types and Anthropic's Messages API. It introduces two new files (`base.py` for shared plumbing, `anthropic.py` for Anthropic-specific translation) and one new exception (`ArcLLMAPIError`).

Design priorities:
1. **Clean translation** — Provider quirks stay in the adapter, core types stay universal
2. **Fail-fast** — API key resolved at init, HTTP errors wrapped immediately
3. **DRY** — Shared adapter plumbing in `BaseAdapter`, not duplicated per provider
4. **Testable** — Private methods per concern, all independently testable with mocked HTTP

---

## Directory Map

```
src/arcllm/
├── exceptions.py                      # MODIFY: Add ArcLLMAPIError
├── __init__.py                        # MODIFY: Export ArcLLMAPIError + AnthropicAdapter
├── adapters/
│   ├── __init__.py                    # NEW: Package init with adapter imports
│   ├── base.py                        # NEW: BaseAdapter (shared plumbing)
│   └── anthropic.py                   # NEW: AnthropicAdapter (translation logic)
tests/
└── test_anthropic.py                  # NEW: Adapter tests with mocked HTTP
```

---

## Component Design

### 1. ArcLLMAPIError (`exceptions.py`)

New exception for provider API errors. Extends `ArcLLMError`.

| Field | Type | Notes |
|-------|------|-------|
| `status_code` | `int` | HTTP status (429, 401, 500, etc.) |
| `body` | `str` | Raw response body from provider |
| `provider` | `str` | Provider name (e.g., "anthropic") |

Message format: `"{provider} API error (HTTP {status_code}): {body}"`

Agents and the retry module (Step 7) can inspect `status_code` to decide action:
- 429 → retry after backoff
- 401 → bad key, don't retry
- 500 → server error, maybe retry

### 2. BaseAdapter (`adapters/base.py`)

Concrete base class that all adapters inherit from. Sits between `LLMProvider` (ABC in types.py) and provider-specific adapters.

**Inheritance**: `LLMProvider` (ABC) → `BaseAdapter` (concrete base) → `AnthropicAdapter`

| Attribute/Method | Type | Notes |
|-----------------|------|-------|
| `_config` | `ProviderConfig` | Injected provider config |
| `_model_name` | `str` | Which model to use |
| `_model_meta` | `ModelMetadata | None` | Metadata for the selected model (None if model not in config) |
| `_api_key` | `str` | Resolved from env var at init |
| `_client` | `httpx.AsyncClient` | Owned by adapter, connection reuse |
| `name` | `str` (property) | Returns provider name from config's api_format or subclass |
| `__init__(config, model_name)` | — | Resolves API key, creates httpx client, looks up model metadata |
| `close()` | `async` | Closes httpx client. Safe to call multiple times. |
| `__aenter__()` | `async` | Returns `self` |
| `__aexit__()` | `async` | Calls `close()` |
| `validate_config()` | `bool` | Checks API key is non-empty, model exists in config |
| `invoke()` | abstract | Still abstract — each adapter implements |

**API key resolution** (`__init__`):
1. Read `config.provider.api_key_env` to get env var name
2. Look up `os.environ[env_var_name]`
3. If missing → raise `ArcLLMConfigError(f"Missing environment variable '{env_var_name}' for provider...")`

**httpx client** (`__init__`):
- `httpx.AsyncClient(timeout=httpx.Timeout(60.0))` — 60s default, override via config later
- Created once, reused across calls

### 3. AnthropicAdapter (`adapters/anthropic.py`)

Implements the Anthropic Messages API translation. Inherits `BaseAdapter`.

| Method | Purpose | Notes |
|--------|---------|-------|
| `name` | Property returning `"anthropic"` | Provider identity |
| `complete(messages, tools, **kwargs)` | Orchestrates the full request/response cycle | Calls private methods below |
| `_build_headers()` | Constructs HTTP headers | `x-api-key`, `anthropic-version`, `content-type` |
| `_build_request_body(messages, tools, **kwargs)` | Translates ArcLLM types → API JSON | Extracts system, formats messages, formats tools |
| `_extract_system(messages)` | Pulls system messages out of the message list | Returns `(system_content, non_system_messages)` |
| `_format_message(message)` | Translates one `Message` → Anthropic format | Handles str and list[ContentBlock] content |
| `_format_content_block(block)` | Translates one `ContentBlock` → Anthropic dict | text, image, tool_use, tool_result |
| `_format_tool(tool)` | Translates one `Tool` → Anthropic dict | Maps `parameters` → `input_schema` |
| `_parse_response(data)` | Translates API response dict → `LLMResponse` | Handles text, tool_use, thinking blocks |
| `_parse_tool_call(block)` | Parses one tool_use block → `ToolCall` | Type-check then json.loads for arguments |
| `_parse_usage(usage_data)` | Translates usage dict → `Usage` | Maps cache tokens, calculates total |

#### Request Translation Details

**System message extraction** (`_extract_system`):
- Anthropic's API takes `system` as a top-level parameter, NOT in the `messages` array
- Scan `messages` for any with `role="system"`, extract their content
- If multiple system messages: concatenate with newlines
- Return `(system_text_or_None, remaining_messages)`

**Message formatting** (`_format_message`):
- Anthropic supports `role: "user" | "assistant"` only in messages
- `role="tool"` in ArcLLM → Anthropic expects tool results as `user` messages with `tool_result` content blocks
- Content: if `str`, pass as-is. If `list[ContentBlock]`, format each block.

**Content block formatting** (`_format_content_block`):
| ArcLLM Type | Anthropic Format |
|-------------|-----------------|
| `TextBlock` | `{"type": "text", "text": "..."}` |
| `ImageBlock` | `{"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}` |
| `ToolUseBlock` | `{"type": "tool_use", "id": "...", "name": "...", "input": {...}}` |
| `ToolResultBlock` | `{"type": "tool_result", "tool_use_id": "...", "content": "..."}` |

**Tool formatting** (`_format_tool`):
- ArcLLM `Tool.parameters` → Anthropic `input_schema`
- ArcLLM `Tool.name` → Anthropic `name`
- ArcLLM `Tool.description` → Anthropic `description`

**Request body** (`_build_request_body`):
```python
{
    "model": self._model_name,
    "max_tokens": kwargs.get("max_tokens", config_default),
    "messages": [...],  # formatted, no system messages
    "system": "...",    # only if system messages exist
    "temperature": kwargs.get("temperature", config_default),
    "tools": [...],     # only if tools provided
}
```

#### Response Parsing Details

**Content blocks** → iterate `response["content"]`:
- `type: "text"` → collect text, concatenate for `LLMResponse.content`
- `type: "tool_use"` → parse into `ToolCall`, add to `LLMResponse.tool_calls`
- `type: "thinking"` → collect text for `LLMResponse.thinking`

**Tool call argument parsing** (`_parse_tool_call`):
- Anthropic returns `input` as a dict (already parsed JSON)
- Our locked decision: type-check first (dict → pass through), string → `json.loads`, failure → `ArcLLMParseError`
- In practice, Anthropic always returns a dict, but we handle the string case for robustness

**Usage parsing** (`_parse_usage`):
| Anthropic Field | ArcLLM Field |
|----------------|-------------|
| `input_tokens` | `input_tokens` |
| `output_tokens` | `output_tokens` |
| (calculated) | `total_tokens` = input + output |
| `cache_read_input_tokens` | `cache_read_tokens` |
| `cache_creation_input_tokens` | `cache_write_tokens` |

**Stop reason mapping**:
| Anthropic | ArcLLM |
|-----------|--------|
| `end_turn` | `end_turn` |
| `tool_use` | `tool_use` |
| `max_tokens` | `max_tokens` |
| `stop_sequence` | `stop_sequence` |

#### HTTP Request

```
POST {base_url}/v1/messages
Headers:
  x-api-key: {resolved_api_key}
  anthropic-version: 2023-06-01
  content-type: application/json
Body: {request_body}
```

**Error handling**:
- Check `response.status_code != 200` → raise `ArcLLMAPIError(status_code, body, "anthropic")`
- Parse JSON response → if JSON parse fails → raise `ArcLLMParseError`

---

## ADRs

### ADR-007: Config Object Injection for Adapters

**Context**: Adapters need config (base URL, API key env var, model name, defaults). Three approaches: inject config object, take individual params, or self-load from TOML.

**Decision**: Constructor takes `ProviderConfig` + model name. Adapter extracts what it needs.

**Rationale**: Clean separation — adapter doesn't know how config was loaded. `load_model()` (Step 6) does the loading, adapter does the translating. Testable with fake configs. Future-proof for vault-loaded configs (Step 14).

**Alternatives rejected**:
- Individual parameters — couples constructor to config structure
- Self-loading — adapter depends on config layer, harder to test

### ADR-008: API Key Resolution at Init

**Context**: Adapter needs API key for HTTP headers. Key stored as env var name in TOML.

**Decision**: Resolve `os.environ[api_key_env]` in `__init__`. Raise `ArcLLMConfigError` if missing.

**Rationale**: Fail-fast. Consistent with our "validate on load" philosophy from Step 2. Agent creating 50 model objects discovers missing key at startup, not on first call 5 minutes later.

**Alternatives rejected**:
- Per-request resolution — delays error discovery, adds overhead
- Lazy on first call — still delays error discovery

### ADR-009: BaseAdapter Concrete Base Class

**Context**: Three adapters (Anthropic, OpenAI, Ollama) share httpx client setup, config storage, API key resolution, and cleanup logic.

**Decision**: `BaseAdapter(LLMProvider)` provides shared plumbing. Adapters inherit and implement only translation methods.

**Rationale**: DRY. Three adapters means three copies of client setup otherwise. BaseAdapter handles the boilerplate, provider-specific adapters focus on translation.

**Alternatives rejected**:
- Each adapter inherits LLMProvider directly — copy-paste, inconsistent cleanup patterns

### ADR-010: Private Methods Per Concern

**Context**: Anthropic adapter needs to build headers, construct request body, parse response, handle errors.

**Decision**: Separate private methods: `_build_headers()`, `_build_request_body()`, `_parse_response()`, etc.

**Rationale**: Each method independently testable. When Anthropic changes their API (they do regularly), update one method, not a 200-line monster. `invoke()` orchestrates.

**Alternatives rejected**:
- All inline in invoke() — long, hard to test, hard to maintain
- External helper functions — loses cohesion

### ADR-011: ArcLLMAPIError Exception

**Context**: Provider APIs return errors (rate limits, auth failures, server errors). Need to surface these to agents.

**Decision**: New `ArcLLMAPIError(ArcLLMError)` with `status_code`, `body`, `provider` attributes.

**Rationale**: Agents catch ArcLLM exceptions, never httpx exceptions. Clean abstraction. `status_code` enables intelligent retry (429 vs 401 vs 500) in Step 7.

**Alternatives rejected**:
- Re-raise httpx exceptions — leaky abstraction, changing HTTP client breaks all error handling
- Return error in LLMResponse — easy to forget checking, no exception flow

---

## Edge Cases

| Case | Handling |
|------|----------|
| No system messages in list | `system` param omitted from request |
| Multiple system messages | Concatenate content with newlines |
| Mixed content (text + tool_use blocks in response) | Text concatenated, tool_use parsed separately |
| Tool call arguments as dict | Pass through (type-check first) |
| Tool call arguments as string | `json.loads()`, raise `ArcLLMParseError` on failure |
| Empty tool_calls in response | `tool_calls` = empty list |
| `stop_reason` we don't recognize | Pass through as-is (future-proof) |
| Missing cache tokens in usage | `None` (optional fields on Usage model) |
| Response with thinking blocks | Concatenate thinking text into `LLMResponse.thinking` |
| API returns non-JSON error body | `body` stored as raw string in `ArcLLMAPIError` |
| `role="tool"` in ArcLLM messages | Translate to `role="user"` with tool_result content blocks |
| Model name not in config's models dict | `_model_meta` is `None`, adapter still works (metadata is informational) |
| API key env var exists but empty | Raise `ArcLLMConfigError` (empty string is not a valid key) |

---

## Test Strategy

Tests in `tests/test_anthropic.py`. All tests use mocked httpx responses (no real API calls).

| Area | Tests | Priority |
|------|-------|----------|
| ArcLLMAPIError creation | Attributes stored, inheritance, message format | P0 |
| BaseAdapter init | Config stored, API key resolved, client created | P0 |
| BaseAdapter missing API key | `ArcLLMConfigError` raised at init | P0 |
| BaseAdapter context manager | `async with` opens/closes properly | P0 |
| Request headers | x-api-key, anthropic-version, content-type present | P0 |
| Simple text request/response | Messages in → text content out | P0 |
| System message extraction | System pulled from list, sent as param | P0 |
| Tool definitions in request | Tool parameters → input_schema mapping | P0 |
| Tool use response parsing | tool_use blocks → ToolCall list | P0 |
| Tool call argument parsing | Dict pass-through, string parsed | P0 |
| Usage parsing | All token fields mapped correctly | P0 |
| Stop reason mapping | end_turn, tool_use, max_tokens | P0 |
| HTTP error handling | 429/401/500 → ArcLLMAPIError | P0 |
| Raw response stored | `LLMResponse.raw` has full response | P1 |
| Thinking blocks | Thinking text parsed into LLMResponse.thinking | P1 |
| Multiple system messages | Concatenated correctly | P1 |
| Tool role translation | role="tool" → role="user" with tool_result | P1 |

---

## __init__.py Changes

Add exports:
```python
# Exception
ArcLLMAPIError

# Adapter (direct import, not through adapters package)
AnthropicAdapter
```

---

## Anthropic API Version

Using `anthropic-version: 2023-06-01` (current stable version).

The version is defined as a constant in the adapter, easy to update when Anthropic releases a new API version.
