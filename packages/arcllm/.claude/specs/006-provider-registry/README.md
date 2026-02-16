# Spec: 006-provider-registry

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 006 |
| **Name** | Provider Registry + load_model() |
| **Type** | Library/Backend |
| **Status** | PENDING |
| **Created** | 2026-02-08 |
| **Confidence** | High (>70%) — All building blocks exist, decisions locked |

## Summary

Implements the public API entry point `load_model()` that agents use to get a configured model object. Uses a convention-based registry where the provider name string drives TOML discovery, module import, and class lookup — the file structure IS the registry, no mapping dict needed. Includes module-level config caching for performance at scale (thousands of agents). Also renames `OpenAIAdapter` to `OpenaiAdapter` to comply with the naming convention.

## Source

ArcLLM Build Step 6. Decisions made interactively via `/build-arcllm 6` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| D-041 Convention-based registry | Provider name drives TOML path, module path, and class name | File structure is the registry. Zero config for discovery. No mapping dict to maintain. | 2026-02-08 |
| D-042 Class name convention | `provider.title() + "Adapter"` | Predictable, no exception maps. Rename OpenAIAdapter to OpenaiAdapter. | 2026-02-08 |
| D-043 Config caching | Module-level cache with `clear_cache()` for testing | Avoids re-parsing TOML per `load_model()` call. Essential at scale. | 2026-02-08 |

## Learnings

(To be filled during implementation)

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 5 Spec: `.claude/specs/005-openai-adapter/`
- Base Adapter: `src/arcllm/adapters/base.py`
- Config Loader: `src/arcllm/config.py`
- Current __init__.py: `src/arcllm/__init__.py` (placeholder load_model)
- Master Prompt: `/Users/joshschultz/AI/arcllm/docs/arcllm-master-prompt.md`
- Product PRD: `/Users/joshschultz/AI/arcllm/docs/arcllm-prd.md`
- Decision Log: `/Users/joshschultz/AI/arcllm/.claude/decision-log.md`
- Steering: `/Users/joshschultz/AI/arcllm/.claude/steering/`
