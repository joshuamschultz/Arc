# PRD: Kimi (Moonshot AI) Provider Support

## Problem Statement

ArcLLM currently supports 12 providers but lacks support for Moonshot AI's Kimi models. Kimi K2/K2.5 offer competitive performance with 256K context windows, native tool calling, and vision capabilities at attractive pricing ($0.60/M input). Adding Kimi expands the provider catalog for users who want access to Moonshot's models.

## Requirements

### Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | Users can load Kimi models via `load_model("moonshot")` | Must |
| FR-2 | Provider TOML includes `kimi-k2` and `kimi-k2.5` model metadata | Must |
| FR-3 | Adapter correctly routes to `https://api.moonshot.ai/v1/chat/completions` | Must |
| FR-4 | Auth via `MOONSHOT_API_KEY` environment variable (Bearer token) | Must |
| FR-5 | Tool calling works via inherited OpenAI-compatible format | Must |
| FR-6 | Lazy import registered in `__init__.py` | Must |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-1 | Zero additional dependencies | 0 new packages |
| NFR-2 | Adapter LOC | <= 12 lines (thin alias) |
| NFR-3 | TOML validates against `ProviderConfig` schema | Pass |
| NFR-4 | All existing tests continue passing | 0 regressions |

## Success Criteria

1. `load_model("moonshot")` returns a working `MoonshotAdapter`
2. `load_model("moonshot", "kimi-k2.5")` selects the correct model
3. All parametrized provider tests pass with moonshot included
4. `mypy --strict` passes
5. `ruff check` passes

## Out of Scope

- Streaming support (future enhancement)
- Thinking mode / `reasoning_content` parsing (future enhancement)
- China-region endpoint (`api.moonshot.cn`) support
- Vision message formatting for K2.5 (already handled by inherited `OpenaiAdapter`)
