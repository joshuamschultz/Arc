# Spec: 002-config-loading

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 002 |
| **Name** | Config Loading System |
| **Type** | Library/Backend |
| **Status** | COMPLETE |
| **Created** | 2026-02-07 |
| **Confidence** | High (>70%) — all decisions made, clear PRD structure |

## Summary

TOML-based configuration loading for ArcLLM. Typed pydantic config models, global config file, per-provider config files, and loader functions with fail-fast validation. Config is package-relative and ships with the library.

## Source

ArcLLM Build Step 2. Decisions made interactively via `/build-arcllm 2` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| Config data model | Pydantic models (typed configs) | Fail-fast validation critical for unattended agents; pydantic already in deps | 2026-02-07 |
| Validation timing | Validate on load | Don't debug config during LLM call; catch errors at startup | 2026-02-07 |
| Merge strategy | Simple override chain (args > provider > global) | Flat TOML structure; deep merge is overkill | 2026-02-07 |
| File discovery | Package-relative (importlib or __file__) | Config ships with the library; part of the unified layer | 2026-02-07 |

## Learnings

- **ModuleConfig extra="allow"** works cleanly — `config.modules["budget"].monthly_limit_usd` accessible as attribute without a separate model per module
- **TOML int→float coercion**: Pydantic handles `3` (TOML int) → `3.0` (Python float) transparently in cost fields
- **Package data required**: setuptools doesn't include non-Python files by default — need `[tool.setuptools.package-data]` in pyproject.toml
- **tomllib.load() requires binary mode**: Must `open(..., "rb")` — easy to forget
- **Test speed**: 10 config tests run in 0.11s alongside 20 type tests — total 30 tests in 0.13s

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 1 Spec: `.claude/specs/001/`
- Master Prompt: `/Users/joshschultz/AI/arcllm/docs/arcllm-master-prompt.md`
- Product PRD: `/Users/joshschultz/AI/arcllm/docs/arcllm-prd.md`
- Decision Log: `/Users/joshschultz/AI/arcllm/.claude/decision-log.md` (D-031 through D-034)
- Steering: `/Users/joshschultz/AI/arcllm/.claude/steering/`
