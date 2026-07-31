# PLAN: Anthropic Adapter + Tool Support

> Implementation tasks for ArcLLM Step 3.
> Status: COMPLETE

---

## Progress

**Completed**: 5/5 tasks
**Remaining**: 0 tasks

---

## Phase 1: Exception + Base Adapter (Tasks 1-2)

### T3.1 Add ArcLLMAPIError to exceptions.py `[activity: backend-development]`

- [x] Add `ArcLLMAPIError(ArcLLMError)` class
  - [x] `__init__(self, status_code: int, body: str, provider: str)`
  - [x] Store `status_code`, `body`, `provider` as instance attributes
  - [x] `super().__init__(f"{provider} API error (HTTP {status_code}): {body}")`
- [x] Write tests FIRST (TDD):
  - [x] `test_api_error_attributes` — status_code, body, provider stored
  - [x] `test_api_error_inherits_arcllm_error` — isinstance check
  - [x] `test_api_error_message_format` — str contains all three values

**Verify**: `pytest tests/test_anthropic.py -v -k "api_error"` — all pass

---

### T3.2 Create BaseAdapter in adapters/base.py `[activity: backend-development]`

- [x] Create `src/arcllm/adapters/__init__.py` (empty or with imports)
- [x] Create `src/arcllm/adapters/base.py` with `BaseAdapter(LLMProvider)`:
  - [x] `__init__(self, config: ProviderConfig, model_name: str)`
    - [x] Store `_config`, `_model_name`
    - [x] Look up `_model_meta` from `config.models.get(model_name)` (None if not found)
    - [x] Resolve API key: `os.environ[config.provider.api_key_env]`
    - [x] If key missing or empty → raise `ArcLLMConfigError`
    - [x] Store as `_api_key`
    - [x] Create `_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))`
  - [x] `name` property (returns string, subclass overrides)
  - [x] `async def close(self)` — closes `_client`, safe to call twice
  - [x] `async def __aenter__(self)` — returns `self`
  - [x] `async def __aexit__(self, *args)` — calls `close()`
  - [x] `def validate_config(self) -> bool` — checks key non-empty
  - [x] `invoke()` remains abstract (inherited from LLMProvider)
- [x] Write tests FIRST (TDD):
  - [x] `test_base_adapter_stores_config` — config and model_name accessible
  - [x] `test_base_adapter_resolves_api_key` — key read from env
  - [x] `test_base_adapter_missing_api_key_raises` — ArcLLMConfigError
  - [x] `test_base_adapter_empty_api_key_raises` — empty string rejected
  - [x] `test_base_adapter_context_manager` — async with works
  - [x] `test_base_adapter_model_meta_found` — looks up model in config
  - [x] `test_base_adapter_model_meta_not_found` — None when model not in config

**Verify**: `pytest tests/test_anthropic.py -v -k "base_adapter"` — all pass

---

## Phase 2: Anthropic Adapter (Task 3)

### T3.3 Create AnthropicAdapter in adapters/anthropic.py `[activity: backend-development]`

- [x] Create `src/arcllm/adapters/anthropic.py` with `AnthropicAdapter(BaseAdapter)`:
  - [x] `ANTHROPIC_API_VERSION = "2023-06-01"` constant
  - [x] `name` property → returns `"anthropic"`
  - [x] `_build_headers() -> dict[str, str]`
    - [x] `x-api-key: {self._api_key}`
    - [x] `anthropic-version: {ANTHROPIC_API_VERSION}`
    - [x] `content-type: application/json`
  - [x] `_extract_system(messages) -> tuple[str | None, list[Message]]`
    - [x] Separate system-role messages from non-system
    - [x] Concatenate system message contents (newline separator)
    - [x] Return (system_text_or_None, remaining_messages)
  - [x] `_format_content_block(block: ContentBlock variant) -> dict`
    - [x] TextBlock → `{"type": "text", "text": ...}`
    - [x] ImageBlock → `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}`
    - [x] ToolUseBlock → `{"type": "tool_use", "id": ..., "name": ..., "input": ...}`
    - [x] ToolResultBlock → `{"type": "tool_result", "tool_use_id": ..., "content": ...}`
  - [x] `_format_message(message: Message) -> dict`
    - [x] Map `role="tool"` → `role="user"` (Anthropic uses user role for tool results)
    - [x] Content: str → pass as-is, list → format each block
  - [x] `_format_tool(tool: Tool) -> dict`
    - [x] Map `parameters` → `input_schema`
  - [x] `_build_request_body(messages, tools, **kwargs) -> dict`
    - [x] Extract system messages
    - [x] Format remaining messages
    - [x] Include: model, max_tokens, temperature, messages
    - [x] Conditionally include: system (if present), tools (if provided)
  - [x] `_parse_tool_call(block: dict) -> ToolCall`
    - [x] Type-check `input`: dict → pass through, str → json.loads, fail → ArcLLMParseError
    - [x] Return `ToolCall(id=..., name=..., arguments=...)`
  - [x] `_parse_usage(usage_data: dict) -> Usage`
    - [x] Map all token fields
    - [x] Calculate total_tokens = input + output
    - [x] Handle optional cache fields (None if missing)
  - [x] `_parse_response(data: dict) -> LLMResponse`
    - [x] Iterate content blocks: collect text, tool_use, thinking
    - [x] Build LLMResponse with all fields
    - [x] Store raw response in `raw` field
  - [x] `async def invoke(messages, tools, **kwargs) -> LLMResponse`
    - [x] Build headers + request body
    - [x] POST to `{base_url}/v1/messages`
    - [x] Check status code → raise `ArcLLMAPIError` on non-200
    - [x] Parse JSON response
    - [x] Return `_parse_response(data)`

**Verify**: `pytest tests/test_anthropic.py -v -k "anthropic"` — all pass

---

## Phase 3: Tests + Integration (Tasks 4-5)

### T3.4 Write comprehensive test_anthropic.py `[activity: unit-testing]`

Tests use mocked httpx responses. No real API calls.

**Mocking approach**: Used `AsyncMock` to monkeypatch `adapter._client.post` with controlled `httpx.Response` objects.

- [x] **Exception tests**:
  - [x] `test_api_error_attributes` — PASSED
  - [x] `test_api_error_inherits_arcllm_error` — PASSED
  - [x] `test_api_error_message_format` — PASSED
- [x] **BaseAdapter tests**:
  - [x] `test_base_adapter_stores_config` — PASSED
  - [x] `test_base_adapter_resolves_api_key` — PASSED
  - [x] `test_base_adapter_missing_api_key_raises` — PASSED
  - [x] `test_base_adapter_empty_api_key_raises` — PASSED
  - [x] `test_base_adapter_context_manager` — PASSED
  - [x] `test_base_adapter_model_meta_found` — PASSED
  - [x] `test_base_adapter_model_meta_not_found` — PASSED
- [x] **Request building tests**:
  - [x] `test_anthropic_headers` — correct headers built
  - [x] `test_anthropic_simple_text_request` — messages formatted correctly
  - [x] `test_anthropic_system_message_extraction` — system separated from messages
  - [x] `test_anthropic_multiple_system_messages` — concatenated
  - [x] `test_anthropic_tool_formatting` — parameters → input_schema
  - [x] `test_anthropic_tool_role_translation` — tool → user with tool_result
  - [x] `test_anthropic_content_block_formatting` — all 4 block types
- [x] **Response parsing tests**:
  - [x] `test_anthropic_text_response` — text parsed into content
  - [x] `test_anthropic_tool_use_response` — tool_calls parsed with correct arguments
  - [x] `test_anthropic_mixed_response` — text + tool_use blocks together
  - [x] `test_anthropic_usage_parsing` — all token fields mapped
  - [x] `test_anthropic_usage_cache_tokens` — cache fields mapped
  - [x] `test_anthropic_stop_reason_mapping` — end_turn, tool_use, max_tokens
  - [x] `test_anthropic_raw_response_stored` — raw field has full dict
  - [x] `test_anthropic_thinking_response` — thinking text parsed
- [x] **Error handling tests**:
  - [x] `test_anthropic_http_429_error` — ArcLLMAPIError with status 429
  - [x] `test_anthropic_http_401_error` — ArcLLMAPIError with status 401
  - [x] `test_anthropic_http_500_error` — ArcLLMAPIError with status 500
  - [x] `test_anthropic_tool_call_string_arguments` — json.loads parses correctly
  - [x] `test_anthropic_tool_call_bad_arguments` — ArcLLMParseError raised
- [x] **Full cycle test**:
  - [x] `test_anthropic_complete_text_cycle` — invoke() returns LLMResponse
  - [x] `test_anthropic_complete_tool_cycle` — invoke() with tools returns tool_calls

**Verify**: `pytest tests/test_anthropic.py -v` — 38 tests pass

---

### T3.5 Update __init__.py with new exports `[activity: backend-development]`

- [x] Import `ArcLLMAPIError` from `arcllm.exceptions`
- [x] Import `AnthropicAdapter` from `arcllm.adapters.anthropic`
- [x] Add both to `__all__`
- [x] Import `BaseAdapter` from `arcllm.adapters.base` (for subclassing)
- [x] Add to `__all__`

**Verify**: `python -c "from arcllm import AnthropicAdapter, ArcLLMAPIError, BaseAdapter"` imports cleanly

---

## Acceptance Criteria

- [x] `ArcLLMAPIError` has `status_code`, `body`, `provider` attributes and inherits `ArcLLMError`
- [x] `BaseAdapter` resolves API key at init, creates httpx client, works as async context manager
- [x] `BaseAdapter` raises `ArcLLMConfigError` for missing/empty API key
- [x] `AnthropicAdapter` extracts system messages from message list
- [x] `AnthropicAdapter` translates `Tool.parameters` → `input_schema`
- [x] `AnthropicAdapter` translates `role="tool"` → `role="user"` with tool_result blocks
- [x] `AnthropicAdapter` parses text responses into `LLMResponse.content`
- [x] `AnthropicAdapter` parses tool_use blocks into `LLMResponse.tool_calls`
- [x] `AnthropicAdapter` parses usage (input, output, cache tokens)
- [x] `AnthropicAdapter` maps stop_reason correctly
- [x] Non-200 HTTP responses raise `ArcLLMAPIError` with correct attributes
- [x] All tests pass: `pytest tests/test_anthropic.py -v`
- [x] Full test suite still passes: `pytest -v` (no regressions)
- [x] No new dependencies added
- [x] `__init__.py` exports `AnthropicAdapter`, `ArcLLMAPIError`, `BaseAdapter`

---

## Implementation Notes

- **TDD approach**: Tests written first (RED), then adapter implemented (GREEN)
- **Mocking**: Used `AsyncMock` + `httpx.Response` for clean HTTP mocking
- **Anthropic API version**: `2023-06-01` as constant (easy to update)
- **Content block formatting**: Handles all 4 ContentBlock variants
- **Tool result content**: ToolResultBlock.content handles both `str` and `list[ContentBlock]`
- **Additional tests**: `test_base_adapter_validate_config`, `test_base_adapter_close_idempotent`, `test_anthropic_name`, `test_image_block_formatting`, `test_kwargs_override_defaults` (beyond spec)
