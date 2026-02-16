# PLAN: Project Setup + Pydantic Types

> Implementation tasks for ArcLLM Step 1.
> Status: COMPLETE

---

## Progress

**Completed**: 7/7 tasks
**Remaining**: 0 tasks

---

## Phase 1: Project Skeleton (Tasks 1-2)

### T1.1 Create Directory Structure `[activity: backend-development]`

- [x] Create project root: `arcllm/`
- [x] Create source layout: `src/arcllm/`
- [x] Create provider stub: `src/arcllm/providers/__init__.py`
- [x] Create test directory: `tests/__init__.py`

**Verify**: Directory structure matches SDD directory map. **PASSED**

---

### T1.2 Create pyproject.toml `[activity: backend-development]`

- [x] Configure build system (setuptools)
- [x] Set project metadata (name, version, description, requires-python)
- [x] Add core dependencies (pydantic>=2.0, httpx>=0.25)
- [x] Add dev dependencies (pytest>=7.0, pytest-asyncio>=0.21)
- [x] Configure package discovery (`[tool.setuptools.packages.find]` where=["src"])
- [x] Configure pytest (`asyncio_mode = "auto"`, testpaths = ["tests"])

**Note**: Build backend corrected from `setuptools.backends._legacy:_Backend` to `setuptools.build_meta` (the original step-01-plan had incorrect path).

**Verify**: `pip install -e ".[dev]"` succeeds. **PASSED**

---

## Phase 2: Core Types (Tasks 3-5)

### T1.3 Create exceptions.py `[activity: backend-development]`

- [x] Define `ArcLLMError(Exception)` — base exception
- [x] Define `ArcLLMParseError(ArcLLMError)` — stores `raw_string: str` and `original_error: Exception`
- [x] Define `ArcLLMConfigError(ArcLLMError)` — message-based

**Verify**: Can raise and catch each exception, access `raw_string` and `original_error`. **PASSED**

---

### T1.4 Create types.py `[activity: type-design]`

- [x] **ContentBlock variants** (4 classes)
  - [x] `TextBlock`: `type: Literal["text"]`, `text: str`
  - [x] `ImageBlock`: `type: Literal["image"]`, `source: str`, `media_type: str`
  - [x] `ToolUseBlock`: `type: Literal["tool_use"]`, `id: str`, `name: str`, `arguments: dict[str, Any]`
  - [x] `ToolResultBlock`: `type: Literal["tool_result"]`, `tool_use_id: str`, `content: str | list[ContentBlock]`

- [x] **ContentBlock union type**
  - [x] `Annotated[Union[...], Field(discriminator="type")]`

- [x] **Forward reference resolution**
  - [x] Call `ToolResultBlock.model_rebuild()` after ContentBlock is defined

- [x] **Message**: `role: Literal[...]`, `content: str | list[ContentBlock]`
- [x] **Tool**: `name: str`, `description: str`, `parameters: dict[str, Any]`
- [x] **ToolCall**: `id: str`, `name: str`, `arguments: dict[str, Any]`
- [x] **Usage**: 3 required int fields + 3 optional `int | None` fields
- [x] **LLMResponse**: `content`, `tool_calls`, `usage`, `model`, `stop_reason`, `thinking`, `raw`
- [x] **LLMProvider**: ABC with `name`, abstract `complete()`, abstract `validate_config()`

**Verify**: All types instantiate correctly. **PASSED**

---

### T1.5 Create __init__.py `[activity: backend-development]`

- [x] Export all ContentBlock variants (TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock)
- [x] Export ContentBlock union type
- [x] Export Message, Tool, ToolCall, Usage, LLMResponse, LLMProvider
- [x] Export ArcLLMError, ArcLLMParseError, ArcLLMConfigError
- [x] Add `load_model` placeholder (raises NotImplementedError)

**Verify**: `python -c "from arcllm import Message, LLMResponse, LLMProvider"` works. **PASSED**

---

## Phase 3: Tests + Validation (Tasks 6-7)

### T1.6 Create test_types.py `[activity: unit-testing]`

- [x] **test_message_string_content** — PASSED
- [x] **test_message_contentblock_list** — PASSED
- [x] **test_each_contentblock_variant** — PASSED
- [x] **test_discriminated_union** — PASSED
- [x] **test_invalid_role_rejected** — PASSED
- [x] **test_toolcall_creation** — PASSED
- [x] **test_llmresponse_with_toolcalls** — PASSED
- [x] **test_llmresponse_no_content** — PASSED
- [x] **test_usage_optional_fields** — PASSED
- [x] **test_tool_definition** — PASSED
- [x] **test_toolresultblock_nested** — PASSED
- [x] **test_parse_error** — PASSED

**Verify**: `pytest tests/test_types.py -v` — 12/12 pass in 0.09s. **PASSED**

---

### T1.7 Create .env.example `[activity: backend-development]`

- [x] Add `ANTHROPIC_API_KEY=your-key-here`
- [x] Add `OPENAI_API_KEY=your-key-here`

**Verify**: File exists, no real keys. **PASSED**

---

## Acceptance Criteria

- [x] `pip install -e ".[dev]"` succeeds with no errors
- [x] `pytest tests/test_types.py -v` shows all 12 tests passing
- [x] `python -c "from arcllm import Message, LLMResponse"` imports cleanly
- [x] Pydantic validates correct data and rejects bad data
- [x] No runtime dependencies beyond pydantic and httpx
- [x] All types match SDD specifications

---

## Implementation Notes

- **pyproject.toml build backend**: Original step-01-plan specified `setuptools.backends._legacy:_Backend` which doesn't exist. Corrected to `setuptools.build_meta`.
- **Python version**: Built on Python 3.13.9 (exceeds 3.11+ requirement).
- **Pydantic version**: 2.12.5 installed (exceeds 2.0 requirement).
- **Test speed**: All 12 tests run in 0.09s.
