# PRD: Project Setup + Pydantic Types

> Feature-specific requirements for ArcLLM Step 1.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

ArcLLM needs a foundation: a Python package with validated types that define the contract between agents, adapters, and modules. Without clean types, every downstream component (config loading, provider adapters, modules) builds on sand.

### Goal

Create a working Python package (`arcllm`) with:
1. All core pydantic types validated and tested
2. Exception hierarchy for error handling
3. Package installable in dev mode
4. Public API surface defined in `__init__.py`

### Success Criteria

- `pip install -e ".[dev]"` succeeds with no errors
- `pytest tests/test_types.py -v` shows all 12 tests passing
- `python -c "from arcllm import Message, LLMResponse"` imports cleanly
- Pydantic validates and rejects bad data correctly
- No runtime dependencies beyond pydantic and httpx

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | Package installs via pip with pydantic + httpx dependencies | P0 | `pip install -e ".[dev]"` exits 0 |
| FR-2 | ContentBlock discriminated union supports 4 block types | P0 | TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock all validate |
| FR-3 | Message accepts string or list of ContentBlocks as content | P0 | Both `content="text"` and `content=[TextBlock(...)]` validate |
| FR-4 | Message rejects invalid roles | P0 | `role="invalid"` raises ValidationError |
| FR-5 | ToolCall stores parsed arguments as dict | P0 | `arguments={"key": "value"}` validates |
| FR-6 | LLMResponse defaults tool_calls to empty list | P0 | No tool_calls provided → `[]` |
| FR-7 | LLMResponse accepts None content (pure tool call response) | P0 | `content=None` validates |
| FR-8 | Usage has required + optional token fields | P0 | Required: input/output/total. Optional: cache_read/cache_write/reasoning default None |
| FR-9 | ToolResultBlock supports nested ContentBlocks | P0 | `content=[TextBlock(type="text", text="result")]` validates |
| FR-10 | ArcLLMParseError stores raw_string and original_error | P0 | Both accessible after catch |
| FR-11 | ArcLLMConfigError provides clear message | P1 | Message accessible |
| FR-12 | LLMProvider abstract base class defines complete() and validate_config() | P0 | Cannot instantiate directly |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Import time | <100ms for `from arcllm import Message` |
| NFR-2 | Zero extra runtime dependencies | Only pydantic + httpx |
| NFR-3 | Python 3.11+ required | Uses stdlib tomllib |
| NFR-4 | Strict typing throughout | All types annotated, no `Any` except where intentional (tool parameters, raw response) |

---

## User Stories

### Agent Developer

> As an agent developer, I want to construct messages with mixed content (text + tool results) and have pydantic validate the structure, so I catch malformed messages before they hit the LLM API.

### Platform Engineer

> As a platform engineer, I want LLMResponse to include usage tracking (including cache and reasoning tokens) so I can build cost monitoring on top of it.

### Adapter Author

> As someone adding a new LLM provider, I want a clear LLMProvider abstract class so I know exactly what methods to implement and what types to return.

---

## Out of Scope (Step 1)

- Config loading (Step 2)
- Provider adapters (Steps 3, 5)
- Any actual LLM API calls
- Module system
- `load_model()` function (placeholder only)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Python 3.11+ | Runtime | Available |
| Pydantic v2 | Library | Install via pip |
| httpx | Library | Install via pip |
| pytest | Dev | Install via pip |
| pytest-asyncio | Dev | Install via pip |
