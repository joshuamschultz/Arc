# SPEC-003: Module Decoupling

**Status**: PENDING
**Type**: Refactoring
**Created**: 2026-02-16
**Confidence**: 90% (fast-track)

## Summary

Decouple memory, policy, and scheduler modules from arcagent core so they are fully removable and swappable. Move module-specific configs, errors, and hardcoded references out of core into their respective module directories.

## Prior Context

- Decisions-log documents module loader DI pattern (D2: constructor signature must match `available` dict)
- SPEC-002 scheduler module follows same convention-based loading pattern
- Research in this session mapped every coupling point between core and modules

## Decisions Made During Spec

| ID | Decision | Rationale |
|----|----------|-----------|
| D1 | Module configs move from root ArcAgentConfig to `[modules.X.config]` TOML section | Modules own their config; core shouldn't know about module internals |
| D2 | Module loader passes `dict[str, Any]` from `ModuleEntry.config`; modules validate internally | OCP-compliant; adding modules never touches core config schema |
| D3 | PolicyEngine becomes its own module (not sub-component of memory) | Independent lifecycle, independent config, can be swapped/disabled |
| D4 | `EvalConfig` stays in core | Used by SessionManager (core) + multiple modules; shared infrastructure |
| D5 | context_manager uses dynamic section ordering (identity first, context last, rest sorted) | No hardcoded module names in core |
| D6 | Error classes move to their modules but extend `ArcAgentError` from core | Structured error contract preserved; module errors travel with the module |
| D7 | Same config class names in new locations (no re-exports from old locations) | Clean break; all imports updated including tests |

## Learnings

(Updated during implementation)

## Related

- `.claude/decisions-log.md` — D2 constructor DI pattern
- `SPEC-002` — scheduler module follows same pattern
