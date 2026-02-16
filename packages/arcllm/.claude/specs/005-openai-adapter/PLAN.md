# PLAN: OpenAI Adapter + StopReason Normalization

> Implementation tasks for ArcLLM Step 5.
> Status: COMPLETE

---

## Progress

**Completed**: 5/5 tasks
**Remaining**: 0 tasks

---

## Phase 1: StopReason Type (Task 1)

### T5.1 Add StopReason type to types.py `[activity: type-design]`

- [x] Add `StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]` above `LLMResponse` class
- [x] Update `LLMResponse.stop_reason` from `str` to `StopReason`
- [x] Add `StopReason` to imports in `__init__.py`
- [x] Add `StopReason` to `__all__` in `__init__.py`
- [x] Add StopReason tests to `tests/test_types.py`:
  - [x] `test_stop_reason_valid_values` — all 4 values accepted in LLMResponse — PASSED
  - [x] `test_stop_reason_invalid_value` — invalid string rejected by pydantic — PASSED
- [x] Verify existing 84 tests still pass (Anthropic already uses these exact strings)

**Verify**: `pytest -v` — 86 tests pass (84 existing + 2 new StopReason tests), zero failures

---

## Phase 2: OpenAI Adapter Tests (Task 2)

### T5.2 Write test_openai.py — tests first (TDD) `[activity: unit-testing]`

Mirror the structure of `test_anthropic.py`. All tests use mocked httpx responses.

- [x] Create fixtures: `FAKE_OPENAI_SETTINGS`, `FAKE_OPENAI_CONFIG` with OpenAI provider config
- [x] Create helpers: `_make_openai_text_response()`, `_make_openai_tool_response()`
- [x] **TestOpenAIHeaders**:
  - [x] `test_openai_bearer_auth` — Authorization header has Bearer prefix — PASSED
  - [x] `test_openai_content_type` — content-type is application/json — PASSED
  - [x] `test_openai_name` — adapter.name returns "openai" — PASSED
- [x] **TestOpenAIRequestBuilding**:
  - [x] `test_simple_text_request` — messages formatted correctly — PASSED
  - [x] `test_system_message_inline` — system messages stay in messages array (not extracted) — PASSED
  - [x] `test_tool_formatting` — wrapped in {"type": "function", "function": {...}} — PASSED
  - [x] `test_tool_result_flattening` — one message with 2 ToolResultBlocks -> 2 OpenAI messages — PASSED
  - [x] `test_tool_result_single` — one ToolResultBlock -> one OpenAI message with tool_call_id — PASSED
  - [x] `test_assistant_tool_use_formatting` — ToolUseBlocks formatted as tool_calls array — PASSED
  - [x] `test_kwargs_override_defaults` — temperature, max_tokens overridable — PASSED
- [x] **TestOpenAIResponseParsing**:
  - [x] `test_text_response` — content extracted from choices[0].message — PASSED
  - [x] `test_tool_use_response` — tool_calls parsed from message level — PASSED
  - [x] `test_mixed_response` — text content + tool_calls together — PASSED
  - [x] `test_null_content_response` — content is None when tool_calls present — PASSED
  - [x] `test_usage_parsing` — prompt_tokens/completion_tokens mapped — PASSED
  - [x] `test_usage_reasoning_tokens` — reasoning_tokens mapped from completion_tokens_details — PASSED
  - [x] `test_raw_response_stored` — raw field has full response dict — PASSED
- [x] **TestOpenAIStopReasonMapping**:
  - [x] `test_stop_maps_to_end_turn` — "stop" -> "end_turn" — PASSED
  - [x] `test_tool_calls_maps_to_tool_use` — "tool_calls" -> "tool_use" — PASSED
  - [x] `test_length_maps_to_max_tokens` — "length" -> "max_tokens" — PASSED
  - [x] `test_content_filter_maps_to_end_turn` — "content_filter" -> "end_turn" — PASSED
- [x] **TestOpenAIToolCallParsing**:
  - [x] `test_tool_call_json_string_arguments` — json.loads parses correctly — PASSED
  - [x] `test_tool_call_dict_arguments` — dict pass-through (defensive) — PASSED
  - [x] `test_tool_call_bad_arguments` — ArcLLMParseError raised — PASSED
- [x] **TestOpenAIErrorHandling**:
  - [x] `test_http_429_error` — ArcLLMAPIError with status 429 — PASSED
  - [x] `test_http_401_error` — ArcLLMAPIError with status 401 — PASSED
  - [x] `test_http_500_error` — ArcLLMAPIError with status 500 — PASSED
- [x] **TestOpenAIFullCycle**:
  - [x] `test_complete_text_cycle` — invoke() returns LLMResponse — PASSED
  - [x] `test_complete_tool_cycle` — invoke() with tools returns tool_calls — PASSED

---

## Phase 3: OpenAI Adapter Implementation (Task 3)

### T5.3 Create OpenAIAdapter in adapters/openai.py `[activity: backend-development]`

- [x] Create `src/arcllm/adapters/openai.py` with `OpenAIAdapter(BaseAdapter)`:
  - [x] `name` property -> returns `"openai"`
  - [x] `_build_headers() -> dict[str, str]`
    - [x] `Authorization: Bearer {self._api_key}`
    - [x] `Content-Type: application/json`
  - [x] `_format_tool(tool: Tool) -> dict`
    - [x] Wrap in `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}`
  - [x] `_format_message(message: Message) -> dict`
    - [x] Handle str content -> pass as-is
    - [x] Handle assistant with ToolUseBlocks -> format as `tool_calls` array with JSON string arguments
    - [x] Handle text blocks -> join text
  - [x] `_format_messages(messages: list[Message]) -> list[dict]`
    - [x] Iterate messages, call `_format_message()` for most
    - [x] For `role="tool"` with ToolResultBlocks: flatten to N individual messages
    - [x] Each tool result message: `{"role": "tool", "tool_call_id": ..., "content": ...}`
  - [x] `_build_request_body(messages, tools, **kwargs) -> dict`
    - [x] Format all messages (including system in-line)
    - [x] Include: model, messages, temperature, max_tokens
    - [x] Conditionally include: tools (if provided)
  - [x] `_map_stop_reason(finish_reason: str) -> StopReason`
    - [x] `"stop"` -> `"end_turn"`
    - [x] `"tool_calls"` -> `"tool_use"`
    - [x] `"length"` -> `"max_tokens"`
    - [x] `"content_filter"` -> `"end_turn"`
    - [x] Unknown -> `"end_turn"` (safe default)
  - [x] `_parse_tool_call(tc: dict) -> ToolCall`
    - [x] Extract from `tc["function"]["name"]` and `tc["function"]["arguments"]`
    - [x] Parse arguments: type-check first (dict pass-through), string json.loads, fail -> ArcLLMParseError
    - [x] Return `ToolCall(id=tc["id"], name=..., arguments=...)`
  - [x] `_parse_usage(usage_data: dict) -> Usage`
    - [x] Map `prompt_tokens` -> `input_tokens`
    - [x] Map `completion_tokens` -> `output_tokens`
    - [x] Map `total_tokens` -> `total_tokens`
    - [x] Map `completion_tokens_details.reasoning_tokens` -> `reasoning_tokens` (if present)
  - [x] `_parse_response(data: dict) -> LLMResponse`
    - [x] Extract `choice = data["choices"][0]`
    - [x] Content from `choice["message"]["content"]` (can be None)
    - [x] Tool calls from `choice["message"].get("tool_calls", [])` -> parse each
    - [x] Stop reason from `choice["finish_reason"]` -> `_map_stop_reason()`
    - [x] Usage from `data["usage"]`
    - [x] Store raw response
  - [x] `async def invoke(messages, tools, **kwargs) -> LLMResponse`
    - [x] Build headers + request body
    - [x] POST to `{base_url}/v1/chat/completions`
    - [x] Check status code -> raise `ArcLLMAPIError` on non-200
    - [x] Parse JSON response
    - [x] Return `_parse_response(data)`

**Verify**: `pytest tests/test_openai.py -v` — all 29 tests pass (GREEN)

---

## Phase 4: Exports + Final Verification (Tasks 4-5)

### T5.4 Update __init__.py with new exports `[activity: backend-development]`

- [x] Import `OpenAIAdapter` from `arcllm.adapters.openai`
- [x] Import `StopReason` from `arcllm.types`
- [x] Add `OpenAIAdapter` to `__all__`
- [x] Add `StopReason` to `__all__`

**Verify**: `python -c "from arcllm import OpenAIAdapter, StopReason"` imports cleanly

---

### T5.5 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL 115 tests pass (84 existing + 2 StopReason + 29 OpenAI)
- [x] Verify zero regressions in test_types.py, test_config.py, test_anthropic.py
- [x] Verify StopReason type works with both adapters
- [x] Count total tests: 115

**Verify**: `pytest -v` — 115 passed in 1.60s, zero failures

---

## Acceptance Criteria

- [x] `StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]` exists in types.py
- [x] `LLMResponse.stop_reason` uses `StopReason` type (not `str`)
- [x] All existing 84 tests still pass unchanged
- [x] `OpenAIAdapter` inherits `BaseAdapter`
- [x] Bearer token auth header
- [x] System messages stay in-line (not extracted)
- [x] Tools wrapped in `{"type": "function", "function": {...}}` format
- [x] Response parsed from `choices[0].message`
- [x] Tool calls parsed from message-level `tool_calls` array
- [x] Tool call arguments parsed from JSON string
- [x] `finish_reason` mapped to canonical `StopReason`
- [x] Tool result messages flattened (one-to-many)
- [x] Usage mapped: `prompt_tokens`/`completion_tokens` -> `input_tokens`/`output_tokens`
- [x] HTTP errors raise `ArcLLMAPIError`
- [x] Raw response stored in `LLMResponse.raw`
- [x] `__init__.py` exports `OpenAIAdapter`, `StopReason`
- [x] Full test suite passes with zero failures
- [x] Zero new dependencies

---

## Implementation Notes

- **Code written directly**: Josh requested direct code implementation (not teaching mode)
- **Mocking**: Same `AsyncMock` + `httpx.Response` pattern from Step 3
- **Mirroring**: Structure mirrors `test_anthropic.py` for consistency
- **Extra test**: `test_usage_reasoning_tokens` — validates o1/o3 reasoning token mapping
- **Stop reason map**: Module-level constant `_STOP_REASON_MAP` dict for clean lookup
- **Tool result flattening**: `_format_messages()` handles expansion, `_format_message()` handles single messages — clean separation
