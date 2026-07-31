# SPEC-002: Scheduling / Heartbeat / Cron

| Field | Value |
|-------|-------|
| **ID** | SPEC-002 |
| **Feature** | scheduling-heartbeat |
| **Status** | COMPLETE |
| **Created** | 2026-02-16 |
| **Type** | Generic (Module + Config + CLI + Tests) |
| **Confidence** | 95% (fast-track) |
| **Source Design** | `packages/arcagent/docs/arcagent-design-v3.md` Section 4 |

## Prior Work

| Stage | Output | Key Content |
|-------|--------|-------------|
| `/brainstorm` | (interactive, not saved) | Scope: full scheduling system for autonomous agent work |
| `/build` | `.claude/decisions-log.md` | 12 design decisions with rationale |
| `/deepen` | Research insights in decisions-log.md | 3 parallel agents: asyncio patterns, codebase analysis, security edge cases |

## Decision Summary

12 decisions logged. Key choices:
1. Module via Module Bus (not core)
2. All 3 schedule types (cron + interval + once)
3. JSON file storage with atomic writes
4. `agent.run(prompt)` per schedule fire
5. Full CRUD tools (create/list/update/cancel)
6. Active hours + timeout, no schedule-level retries
7. Sequential FIFO queue
8. Standalone daemon (`arc agent serve`)
9. Runtime-only config (schedules.json)
10. Module Bus events + metadata for audit
11. croniter for cron parsing
12. Unit (frozen time) + integration (mock LLM) tests

## Files

- [PRD.md](./PRD.md) — Product Requirements
- [SDD.md](./SDD.md) — System Design
- [PLAN.md](./PLAN.md) — Implementation Plan

## Learnings

- **croniter timezone handling**: `croniter.get_next(datetime)` returns naive datetimes even when the base is tz-aware. Must explicitly add `tzinfo=UTC` after each `get_next()` call.
- **asyncio.Queue drain pattern**: Use `queue.join()` with `asyncio.wait_for()` timeout to drain remaining items before shutdown. Worker must call `task_done()` in a `finally` block.
- **Atomic file writes**: `tempfile.mkstemp()` in the same directory as target + `os.fsync()` + `os.replace()` guarantees no partial writes. The temp file must be in the same directory for `os.replace()` atomicity.
- **Prompt injection detection**: Regex patterns for 8 common injection vectors (ignore previous, system:, disregard, etc.) catch the majority of simple injection attempts. URL detection catches data exfiltration attempts.
- **RegisteredTool pattern**: Handler functions receive kwargs matching the JSON schema property names. Parameter names must match the schema (even if they shadow builtins like `id` and `type`).
- **Daemon pattern**: `asyncio.new_event_loop()` + `add_signal_handler()` is required for long-running daemons. Cannot use `asyncio.run()` because it doesn't support signal handler registration before the loop starts.

## Implementation Summary

| Metric | Value |
|--------|-------|
| Tests | 90 (86 unit + 4 integration) |
| Coverage | 89% line coverage |
| New files | 10 |
| Modified files | 2 (config.py, agent.py) |
| Ruff errors | 0 |
| Phases | 6/6 complete |
