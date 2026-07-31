# SPEC-013: Convention-Driven Prompt Injection

## Metadata

| Field | Value |
|-------|-------|
| ID | SPEC-013 |
| Feature | convention-prompt-injection |
| Type | integration |
| Status | COMPLETE |
| Created | 2026-02-27 |
| Confidence | 95% (fast-track) |

## Prior Work

| Phase | Artifact | Date |
|-------|----------|------|
| Brainstorm | `.claude/brainstorms/2026-02-27-convention-driven-prompt-injection.md` | 2026-02-27 |
| Build | `.claude/decisions-log.md` (16 decisions, 15 user, 1 auto-applied) | 2026-02-27 |
| Deepen | Research Insights section in decisions-log.md (27 sources) | 2026-02-27 |

## Decision Summary

Key decisions from `/build`:
- D1: `format_for_prompt()` on ToolRegistry (same pattern as SkillRegistry)
- D2: XML tags for both catalogs
- D3: Add `when_to_use`, `example`, `category` to RegisteredTool and `@native_tool` decorator
- D4: Invalidate-on-register caching for tool catalog
- D5: TTL-based refresh (60s) for team roster
- D8: All non-empty Entity fields rendered dynamically
- D9: Section key = `sections['teams']`
- D11: Dynamic field iteration via `model_dump(exclude_defaults=True)`
- D13: XML-escape all string values
- D14: Preamble configurable via TOML
- D15: `roster_ttl_seconds` in MessagingConfig

## Learnings

- SDD assumed `EntityRegistry.list_entities()` was sync — it's async. Made `_build_roster()` async to match.
- Core LOC was already at 3,638 before spec; +85 LOC delta (vs estimated +122) well within plan.
- Existing `_setup_skill_prompt_injection()` pattern in agent.py made tool injection trivially consistent.
