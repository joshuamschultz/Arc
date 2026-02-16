# SDD: Project Setup + Pydantic Types

> System design for ArcLLM Step 1.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 1 creates the type foundation. Every adapter, module, and consumer of ArcLLM depends on these types. The design prioritizes:

1. **Correctness** — Pydantic validates everything, rejects bad data
2. **Simplicity** — Minimal fields, no extras
3. **Agent-native** — Types map directly to the agentic tool-calling loop

---

## Directory Map

```
arcllm/
├── pyproject.toml                     # NEW: Package config
├── .env.example                       # NEW: API key template
├── src/
│   └── arcllm/
│       ├── __init__.py                # NEW: Public API exports
│       ├── types.py                   # NEW: Core pydantic types
│       ├── exceptions.py              # NEW: Exception hierarchy
│       └── providers/
│           └── __init__.py            # NEW: Empty init
├── tests/
│   ├── __init__.py                    # NEW: Empty init
│   └── test_types.py                  # NEW: 12 type tests
```

---

## Component Design

### 1. Exception Hierarchy (`exceptions.py`)

```
ArcLLMError (base)
├── ArcLLMParseError
│   ├── raw_string: str          # The unparseable input
│   └── original_error: Exception # What caused the failure
└── ArcLLMConfigError
    └── message: str             # What went wrong
```

**Design decisions**:
- Base `ArcLLMError` inherits from `Exception` — users can catch all arcllm errors with one except clause
- `ArcLLMParseError` stores the raw string so agents can log it, retry, or surface it
- Both are simple classes, not pydantic models

### 2. ContentBlock Types (`types.py`)

Four pydantic BaseModel classes, discriminated by `type` field:

| Class | type Literal | Fields |
|-------|-------------|--------|
| `TextBlock` | `"text"` | `text: str` |
| `ImageBlock` | `"image"` | `source: str`, `media_type: str` |
| `ToolUseBlock` | `"tool_use"` | `id: str`, `name: str`, `arguments: dict[str, Any]` |
| `ToolResultBlock` | `"tool_result"` | `tool_use_id: str`, `content: str \| list[ContentBlock]` |

**Union type**:
```python
ContentBlock = Annotated[
    Union[TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock],
    Field(discriminator="type")
]
```

**Forward reference**: `ToolResultBlock.content` references `ContentBlock` before it's fully defined. Resolution: call `ToolResultBlock.model_rebuild()` after all types are defined.

### 3. Message (`types.py`)

| Field | Type | Notes |
|-------|------|-------|
| `role` | `Literal["system", "user", "assistant", "tool"]` | Standard four. Provider-specific roles mapped by adapter. |
| `content` | `str \| list[ContentBlock]` | String for simple text, list for multimodal/tool content |

### 4. Tool (`types.py`)

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | Tool identifier |
| `description` | `str` | Sent to LLM |
| `parameters` | `dict[str, Any]` | Raw JSON Schema — intentionally loose |

### 5. ToolCall (`types.py`)

| Field | Type | Notes |
|-------|------|-------|
| `id` | `str` | Correlates with ToolResultBlock.tool_use_id |
| `name` | `str` | Which tool the LLM wants to call |
| `arguments` | `dict[str, Any]` | Always parsed by adapter before reaching here |

### 6. Usage (`types.py`)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `input_tokens` | `int` | required | |
| `output_tokens` | `int` | required | |
| `total_tokens` | `int` | required | |
| `cache_read_tokens` | `int \| None` | `None` | Prompt caching |
| `cache_write_tokens` | `int \| None` | `None` | Prompt caching |
| `reasoning_tokens` | `int \| None` | `None` | Thinking/reasoning models |

### 7. LLMResponse (`types.py`)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `content` | `str \| None` | `None` | Null if pure tool calls |
| `tool_calls` | `list[ToolCall]` | `[]` | Empty if no tool calls |
| `usage` | `Usage` | required | Token counts |
| `model` | `str` | required | Which model responded |
| `stop_reason` | `str` | required | "end_turn", "tool_use", "max_tokens" |
| `thinking` | `str \| None` | `None` | Reasoning content if available |
| `raw` | `Any` | `None` | Original provider response (debugging) |

### 8. LLMProvider (`types.py` — abstract base class)

**NOT a pydantic model.** Uses `abc.ABC`.

| Member | Type | Notes |
|--------|------|-------|
| `name` | `str` | Provider identifier (class attribute) |
| `complete()` | `async → LLMResponse` | Abstract. Takes `messages: list[Message]`, `tools: list[Tool] \| None`, `**kwargs` |
| `validate_config()` | `→ bool` | Abstract. Checks config validity |

### 9. Public API (`__init__.py`)

Exports all types, exceptions, and a `load_model` placeholder:

```python
# Types
ContentBlock, TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock
Message, Tool, ToolCall, Usage, LLMResponse, LLMProvider

# Exceptions
ArcLLMError, ArcLLMParseError, ArcLLMConfigError
```

`load_model` will be a placeholder that raises `NotImplementedError` until Step 6.

---

## ADRs

### ADR-001: Discriminated Union via Field(discriminator)

**Context**: Need a union type for ContentBlock with four variants.

**Decision**: Use `Annotated[Union[...], Field(discriminator="type")]` (Pydantic v2 pattern).

**Rationale**: Fast validation — pydantic checks the `type` field first to select the right model. Explicit. No custom validators needed.

**Alternatives rejected**:
- `model_validator` — More code, slower, less type-safe
- No union (separate types, `list[Any]`) — Loses validation, silent bugs in agentic loops

### ADR-002: model_rebuild() for Forward References

**Context**: `ToolResultBlock.content` references `ContentBlock` which includes `ToolResultBlock` — circular.

**Decision**: Define all types, then call `ToolResultBlock.model_rebuild()`.

**Rationale**: Explicit. Keeps runtime type checking. `from __future__ import annotations` makes all annotations strings (deferred), which changes runtime behavior.

### ADR-003: LLMProvider as ABC, not Pydantic Model

**Context**: Need an abstract base for provider adapters.

**Decision**: Plain `abc.ABC` class with abstract methods.

**Rationale**: Providers hold HTTP clients, config objects, etc. — not serializable data. Pydantic models are for data, ABCs are for behavior contracts.

---

## Edge Cases

| Case | Handling |
|------|----------|
| ToolResultBlock with nested ToolUseBlock | Valid — pydantic handles recursive ContentBlock |
| Message with empty content list | Valid — `content=[]` passes validation |
| LLMResponse with both content and tool_calls | Valid — some providers return both |
| Usage with 0 tokens | Valid — could happen with cached responses |
| ToolCall with empty arguments dict | Valid — some tools take no args |

---

## Test Strategy

12 unit tests covering all types, validation, rejection, and edge cases. See PLAN.md for test breakdown.
