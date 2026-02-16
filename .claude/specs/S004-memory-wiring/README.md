# S004: Memory Wiring

## Metadata

| Field | Value |
|-------|-------|
| **Spec ID** | S004 |
| **Feature** | Wire up ArcAgent's memory system — connect 5 disconnected gaps to make memory functional |
| **Type** | Integration (wiring existing components + architectural refinements) |
| **Status** | PENDING |
| **Created** | 2026-02-15 |
| **Author** | Claude (spec-driven workflow) |
| **Confidence** | 92% (fast-track, brainstorm + build 16 decisions + deepen all resolved) |
| **Prior Work** | `.claude/brainstorms/2026-02-14-memory-module.md`, `.claude/decisions-log.md` (MW-001 through MW-016, deepened) |

## Scope

Wire up ArcAgent's fully-built memory system (423 tests, 93.67% coverage) by connecting 5 gaps that prevent it from functioning:

1. **Eval model never instantiated** — `markdown_memory.py:296` returns early when `eval_model is None`
2. **memory_search not registered as tool** — HybridSearch exists but isn't a callable tool
3. **identity.md has no memory guidance** — Agent doesn't know it has memory capabilities
4. **Compaction never triggered** — Session never checks compact_threshold after turns
5. **Config doesn't enable memory module** — `Basic_Agent/arcagent.toml` missing `[modules.memory]`

Plus 4 architectural refinements from MW build decisions:
- **ModuleContext** (MW-004) — Proper DI for module startup
- **Convention loader** (MW-007) — Replace hardcoded `_register_modules()`
- **Session-owns-context** (MW-008) — SessionManager holds ContextManager
- **Module-injected guidance** (MW-009) — OpenClaw pattern for self-describing modules

## NOT in Scope

- Building new memory module code (already built in S002)
- New tools, skills, or channels
- NATS inter-agent memory sharing
- Module signing/verification
- Neo4j/Graphiti backend

## Key Decisions

All 16 decisions from MW build session, enriched by deepening:

| # | Decision | Summary |
|---|----------|---------|
| MW-001 | Eval model lazy init | Memory module creates eval model from EvalConfig on first use |
| MW-002 | Convention-based module loading | Auto-discover modules, config allowlist controls loading |
| MW-003 | Memory module owns memory_search | Module registers tool via ModuleContext.tool_registry |
| MW-004 | ModuleContext dataclass | Frozen DI container replaces `startup(bus)` with `startup(ctx)` |
| MW-005 | System prompt injection only | No proactive search at session start |
| MW-006 | Session self-manages compaction | Check ratio after each turn, pre-flush with MAGMA pattern |
| MW-007 | Convention loader in core | New module_loader.py replaces _register_modules() |
| MW-008 | Session owns context | SessionManager holds ContextManager instance |
| MW-009 | Module injects guidance | OpenClaw pattern, identity.md `## Memory` overrides |
| MW-010 | Memory enabled by default | New agents get memory automatically |
| MW-011 | memory_search tool interface | query + scope + date_from + date_to (all optional beyond query) |
| MW-012 | Eval model fallback | Falls back to agent's LLM config when EvalConfig is empty |
| MW-013 | Workspace-scoped search | No per-result ACL needed |
| MW-014 | PII handled in ArcLLM | No PII filtering in memory module |
| MW-015 | Semaphore-limited background tasks | asyncio.Semaphore(max_concurrent=2) |
| MW-016 | Unit tests + one integration test | Test wiring, not memory internals |

## Learnings

_(Updated during implementation)_

## Files

- [PRD.md](./PRD.md) — Product Requirements Document
- [SDD.md](./SDD.md) — System Design Document
- [PLAN.md](./PLAN.md) — Implementation Plan
