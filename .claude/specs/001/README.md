# Spec: step-01-types

## Metadata

| Field | Value |
|-------|-------|
| **ID** | step-01-types |
| **Name** | Project Setup + Pydantic Types |
| **Type** | Library/Backend |
| **Status** | COMPLETE |
| **Created** | 2026-02-07 |
| **Confidence** | High (>70%) — fully planned |

## Summary

Create the ArcLLM project skeleton and define all core pydantic types. No logic. Just the shapes that everything else builds on. When complete: working Python package with validated types and passing tests.

## Source

Formalized from existing plan: `/Users/joshschultz/AI/arcllm/arcllm-step-01-plan.md`

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| Discriminated union approach | `Annotated[Union[...], Field(discriminator="type")]` | Pydantic v2 recommended, fast, explicit | Pre-planned |
| Forward reference handling | `model_rebuild()` after all types defined | Explicit over `__future__` annotations, runtime type checking | Pre-planned |
| Exception hierarchy | `ArcLLMError` base, `ArcLLMParseError` + `ArcLLMConfigError` subclasses | Catch-all for library errors, specific subclasses for tool parse + config | Pre-planned |
| Build system | setuptools via `pyproject.toml` | Standard, no extra tooling dependency | Pre-planned |

## Learnings

- **Build backend correction**: Original step-01-plan specified `setuptools.backends._legacy:_Backend` which doesn't exist. Corrected to `setuptools.build_meta`.
- **venv activation in zsh**: `source .venv/bin/activate` can fail with zsh parse errors on the `deactivate()` function. Use direct paths (`.venv/bin/pip`, `.venv/bin/python`) instead.
- **Python version**: Built on Python 3.13.9 (exceeds 3.11+ requirement).
- **Pydantic version**: 2.12.5 installed (exceeds 2.0 requirement).
- **Test speed**: All 12 tests run in 0.09s — type-only specs are fast to validate.

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Master Prompt: `/Users/joshschultz/AI/arcllm/arcllm-master-prompt.md`
- Product PRD: `/Users/joshschultz/AI/arcllm/arcllm-prd.md`
- Steering: `/Users/joshschultz/AI/arcllm/.claude/steering/`
