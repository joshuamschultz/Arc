# Spec 001: Core Loop + ReAct

## Metadata

| Field | Value |
|-------|-------|
| ID | 001 |
| Name | core-loop-react |
| Phase | 1 |
| Status | PENDING |
| Type | Core Library |
| Created | 2026-02-11 |
| Decisions | 001-025 (see `.claude/decision-log.md`) |

## Summary

Phase 1 of arcrun: the async execution engine foundation. `await run(model, tools, prompt, task)` works end-to-end with ReAct strategy, full event emission, sandbox enforcement, dynamic tool registry, steering (steer + followUp), and context transform.

## Key Decisions

| # | Decision | Choice |
|---|----------|--------|
| 008 | Tool type | Dataclass + factories |
| 009 | Tool.execute | Async only |
| 010 | Cancel signal | Via ToolContext |
| 011 | Tool context | Typed ToolContext dataclass |
| 012 | Return type | str |
| 013 | Event data | Generic Event + dict |
| 014 | Sandbox mechanism | Caller-provided checker |
| 015 | Phase 1 scope | Tool-level + caller checker |
| 016 | Security model | Allowlist |
| 017 | Max turns | Strategy enforces |
| 018 | Text + tools | Preserved in message |
| 019 | Strategy prompt | Prepend to system prompt |
| 020 | Entry points | run() + run_async() |
| 021 | Steering modes | Steer + followUp |
| 022 | Param validation | jsonschema |
| 023 | Dynamic + sandbox | Denied by default |
| 024 | Strategy return | LoopResult directly |
| 025 | Error handling | Exceptions bubble up |

## Learnings

(Updated during implementation)

## Open Questions

- Streaming pass-through (DECISION-004): Requires arcllm streaming support (not yet built). Phase 1 uses full responses only.
- Phase 1 line estimate (~610) exceeds original 500-line budget. Accepted — well under 1,000 total.
