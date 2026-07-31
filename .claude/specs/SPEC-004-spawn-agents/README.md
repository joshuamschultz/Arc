# SPEC-004: Recursive Agent Spawning

| Field | Value |
|-------|-------|
| **ID** | SPEC-004 |
| **Feature** | Recursive Agent Spawning |
| **Type** | Integration |
| **Status** | PENDING |
| **Confidence** | 90% |
| **Route** | Fast-track |
| **Package** | `arcrun` |
| **Created** | 2026-02-16 |

## Prior Work

- **Brainstorm**: `.claude/brainstorms/2026-02-16-recursive-agent-spawning.md`
- **Build Decisions**: `.claude/decisions-log.md` (14 decisions, Feature: Recursive Agent Spawning)

## Key Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Decomposition mechanism | Spawn as a tool (Claude Code pattern) |
| 2 | Where spawn lives | ArcRun built-in + overridable by ArcAgent |
| 3 | Spawn tool API | `task` + `system_prompt` + `tools` |
| 4 | State model | Flat fields on RunState |
| 5 | Shared memory | Strictly nothing |
| 6 | Cross-child comms | Parent only |
| 7 | Budget enforcement | None in v1 (depth only) |
| 8 | Parallelism | Parallel via multiple tool calls |
| 9 | Transport | In-process asyncio (v1) |
| 10 | Identity | Minimal: run_id lineage only |
| 11 | Error handling | Error string as tool result |
| 12 | Event propagation | Bubble up with child prefix |
| 13 | Testing | Mock model + real spawn |
| 14 | Scope | Confirmed MVP |

## Learnings

(Captured during implementation)
