# SDD: OpenAI Adapter + StopReason Normalization

> System design for ArcLLM Step 5.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 5 creates the second adapter and introduces stop reason normalization. It validates the adapter abstraction by proving the same `invoke(messages, tools)` contract works for both Anthropic and OpenAI. The design mirrors Step 3's proven pattern (private methods per concern, mocked HTTP tests) while handling OpenAI-specific translation differences.

Design priorities:
1. **Abstraction validation** — Same agent code, different provider, identical behavior
2. **Type safety** — `StopReason` as Literal, not raw string
3. **DRY** — Inherits all BaseAdapter plumbing, only implements translation
4. **Testable** — Private methods per concern, all independently testable

---

## Directory Map

```
src/arcllm/
├── types.py                           # MODIFY: Add StopReason type, update LLMResponse
├── __init__.py                        # MODIFY: Export StopReason + OpenAIAdapter
├── adapters/
│   ├── __init__.py                    # MODIFY: Add OpenAIAdapter import
│   └── openai.py                      # NEW: OpenAIAdapter (translation logic)
tests/
└── test_openai.py                     # NEW: Adapter tests with mocked HTTP
```

---

## Component Design

### 1. StopReason Type (`types.py`)

New type alias for normalized stop reasons across all providers.

```
StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]
```

`LLMResponse.stop_reason` changes from `str` to `StopReason`.

**Why these four values**: They cover all meaningful stop conditions in agentic loops:
- `end_turn` — Model finished responding naturally
- `tool_use` — Model wants to call tools (agent should execute and re-invoke)
- `max_tokens` — Hit output limit (agent may need to continue)
- `stop_sequence` — Hit a custom stop sequence

**Backward compatibility**: Anthropic already returns these exact strings. No Anthropic code changes needed.

### 2. OpenAIAdapter (`adapters/openai.py`)

Implements the OpenAI Chat Completions API translation. Inherits `BaseAdapter`.

| Method | Purpose | Notes |
|--------|---------|-------|
| `name` | Property returning `"openai"` | Provider identity |
| `invoke(messages, tools, **kwargs)` | Full request/response cycle | Calls private methods below |
| `_build_headers()` | HTTP headers | `Authorization: Bearer {key}`, `content-type` |
| `_build_request_body(messages, tools, **kwargs)` | Translates ArcLLM types to API JSON | System messages stay in-line |
| `_format_messages(messages)` | Translates messages list with tool result flattening | One ArcLLM tool message -> N OpenAI messages |
| `_format_message(message)` | Translates one `Message` to OpenAI format | Handles str and list[ContentBlock] |
| `_format_tool(tool)` | Translates one `Tool` to OpenAI format | Wraps in `{"type": "function", "function": {...}}` |
| `_parse_response(data)` | Translates API response dict to `LLMResponse` | Extracts from `choices[0].message` |
| `_parse_tool_call(tc)` | Parses one tool_call to `ToolCall` | Arguments always JSON string in OpenAI |
| `_parse_usage(usage_data)` | Translates usage dict to `Usage` | Maps `prompt_tokens`/`completion_tokens` |
| `_map_stop_reason(finish_reason)` | Maps OpenAI `finish_reason` to `StopReason` | Core normalization logic |

#### Request Translation Details

**Headers** (`_build_headers`):
```
Authorization: Bearer {self._api_key}
Content-Type: application/json
```

No version header needed (OpenAI uses URL versioning).

**System messages**: Unlike Anthropic, OpenAI accepts system messages in-line in the messages array. No extraction needed — simpler than Anthropic.

**Message formatting** (`_format_messages` + `_format_message`):

For most messages:
```
ArcLLM Message(role="user", content="hello")
-> {"role": "user", "content": "hello"}
```

For assistant messages with tool use blocks:
```
ArcLLM Message(role="assistant", content=[ToolUseBlock(id="t1", name="calc", arguments={"x":1})])
-> {"role": "assistant", "content": null, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "calc", "arguments": "{\"x\": 1}"}}]}
```

For tool result messages (**flattening**):
```
ArcLLM Message(role="tool", content=[
    ToolResultBlock(tool_use_id="t1", content="42"),
    ToolResultBlock(tool_use_id="t2", content="hello"),
])
-> TWO messages:
   {"role": "tool", "tool_call_id": "t1", "content": "42"}
   {"role": "tool", "tool_call_id": "t2", "content": "hello"}
```

This is the key one-to-many expansion. `_format_messages()` handles this by iterating messages and potentially emitting multiple OpenAI messages per ArcLLM message.

**Tool formatting** (`_format_tool`):
```
ArcLLM Tool(name="search", description="Search", parameters={...})
-> {"type": "function", "function": {"name": "search", "description": "Search", "parameters": {...}}}
```

Note: OpenAI wraps tools in an extra `{"type": "function", "function": {...}}` layer that Anthropic doesn't have.

**Request body** (`_build_request_body`):
```python
{
    "model": self._model_name,
    "messages": [...],  # including system messages in-line
    "temperature": kwargs.get("temperature", config_default),
    "max_tokens": kwargs.get("max_tokens", config_default),  # Note: OpenAI uses max_tokens too
    "tools": [...],  # only if tools provided
}
```

#### Response Parsing Details

**Response shape**: OpenAI nests the actual message inside `choices[0].message`:
```json
{
    "choices": [{"message": {"role": "assistant", "content": "...", "tool_calls": [...]}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
}
```

**Content**: `choices[0].message.content` — can be `str` or `null` (null when tool_calls present).

**Tool calls**: `choices[0].message.tool_calls` — array of:
```json
{"id": "call_abc", "type": "function", "function": {"name": "search", "arguments": "{\"query\": \"cats\"}"}}
```

Key difference: OpenAI **always** returns `arguments` as a JSON string (not a dict like Anthropic). Our parser must `json.loads()` it.

**Stop reason mapping** (`_map_stop_reason`):

| OpenAI `finish_reason` | ArcLLM `StopReason` |
|------------------------|---------------------|
| `"stop"` | `"end_turn"` |
| `"tool_calls"` | `"tool_use"` |
| `"length"` | `"max_tokens"` |
| `"content_filter"` | `"end_turn"` |

**Usage mapping** (`_parse_usage`):

| OpenAI Field | ArcLLM Field |
|--------------|-------------|
| `prompt_tokens` | `input_tokens` |
| `completion_tokens` | `output_tokens` |
| `total_tokens` | `total_tokens` |
| `completion_tokens_details.reasoning_tokens` | `reasoning_tokens` |

Note: OpenAI may include `reasoning_tokens` for o1/o3 models. Map if present.

#### HTTP Request

```
POST {base_url}/v1/chat/completions
Headers:
  Authorization: Bearer {resolved_api_key}
  Content-Type: application/json
Body: {request_body}
```

**Error handling**: Same pattern as Anthropic — `status_code != 200` raises `ArcLLMAPIError(status_code, body, "openai")`.

---

## ADRs

### ADR-012: StopReason Normalization

**Context**: Anthropic uses `end_turn`/`tool_use`/`max_tokens`, OpenAI uses `stop`/`tool_calls`/`length`. Agents checking stop reasons need provider-agnostic values.

**Decision**: Define `StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]`. Each adapter maps provider-native values to these canonical values.

**Rationale**: The whole point of ArcLLM is a unified interface. If stop reason checks are provider-specific, the abstraction leaks. Anthropic's values are clean and descriptive — adopt them as canonical. Literal type catches typos at validation time.

**Alternatives rejected**:
- Pass through provider-native values — defeats the abstraction
- Dual field (normalized + raw) — over-engineering, raw response is in `LLMResponse.raw`

### ADR-013: Tool Result Message Flattening

**Context**: ArcLLM allows one message with multiple `ToolResultBlock`s. OpenAI requires one message per tool result, each with `tool_call_id` at the message level.

**Decision**: The adapter's `_format_messages()` method handles the one-to-many expansion transparently. A single ArcLLM message with 3 ToolResultBlocks becomes 3 OpenAI messages.

**Rationale**: Agents use the same message-building pattern regardless of provider. The adapter owns translation complexity.

**Alternatives rejected**:
- Require agents to use different message format per provider — defeats the abstraction

### ADR-014: Mirror Anthropic Adapter Structure

**Context**: OpenAI adapter needs the same kind of request building and response parsing as Anthropic.

**Decision**: Same private method pattern: `_build_headers()`, `_build_request_body()`, `_format_message()`, `_format_tool()`, `_parse_response()`, `_parse_tool_call()`, `_parse_usage()`. Plus OpenAI-specific `_map_stop_reason()`.

**Rationale**: Proven in Step 3. Each method independently testable. Easy to update when OpenAI changes their API.

---

## Edge Cases

| Case | Handling |
|------|----------|
| Tool call arguments as JSON string (always for OpenAI) | `json.loads()`, raise `ArcLLMParseError` on failure |
| Tool call arguments as dict (shouldn't happen with OpenAI) | Type-check first, pass through if dict |
| `finish_reason` we don't recognize (future OpenAI values) | Default to `"end_turn"` with the raw in `LLMResponse.raw` |
| `content` is `null` in response (tool_calls present) | `LLMResponse.content = None` |
| Multiple tool calls in single response | All parsed into `LLMResponse.tool_calls` list |
| Tool result with list[ContentBlock] content | Stringify/join for OpenAI (only accepts string content) |
| `content_filter` finish reason | Map to `"end_turn"` — agent treats as normal end |
| No tool_calls in response message | `tool_calls = []` |
| Usage includes `reasoning_tokens` (o1 models) | Map to `Usage.reasoning_tokens` if present |
| Empty messages list | Pass through — let OpenAI return the error |
| Assistant message with text + tool_use blocks | Format with both `content` and `tool_calls` fields |

---

## Test Strategy

Tests in `tests/test_openai.py`. All tests use mocked httpx responses (no real API calls). Structure mirrors `test_anthropic.py` for consistency.

| Area | Tests | Priority |
|------|-------|----------|
| StopReason type (in test_types.py) | Valid values accepted, invalid rejected | P0 |
| OpenAI headers | Bearer auth, content-type | P0 |
| Simple text request/response | Messages formatted, content parsed | P0 |
| System messages in-line | System message stays in messages array | P0 |
| Tool definitions in request | Function wrapper format | P0 |
| Tool use response parsing | tool_calls from message level | P0 |
| Tool call argument parsing | JSON string parsed to dict | P0 |
| Tool result flattening | One ArcLLM message -> N OpenAI messages | P0 |
| Stop reason normalization | All mappings verified | P0 |
| Usage parsing | prompt_tokens/completion_tokens mapped | P0 |
| HTTP error handling | 429/401/500 -> ArcLLMAPIError | P0 |
| Full request/response cycle | invoke() end-to-end with mocked HTTP | P0 |
| Raw response stored | LLMResponse.raw has full dict | P1 |
| Mixed content (text + tool_calls) | Both parsed correctly | P1 |
| Assistant message with ToolUseBlocks | Formatted as OpenAI tool_calls | P1 |
| kwargs override defaults | temperature, max_tokens overridable | P1 |

---

## __init__.py Changes

Add exports:
```python
# Type
StopReason

# Adapter
OpenAIAdapter
```
