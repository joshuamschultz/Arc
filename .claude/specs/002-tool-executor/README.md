# Spec 002: Tool Executor Extraction

## Metadata

| Field | Value |
|-------|-------|
| ID | 002 |
| Name | tool-executor |
| Phase | 1 (post-review refactor) |
| Status | PENDING |
| Type | Internal Refactor |
| Created | 2026-02-14 |
| Decisions | 026-031 (see `.claude/decision-log.md`) |

## Summary

Extract the tool execution pipeline from `react.py` into a shared `executor.py` module. All current and future strategies call `execute_tool_call()` instead of reimplementing the 10-step pipeline. Pure refactor — zero behavior change.

## Key Decisions

| # | Decision | Choice |
|---|----------|--------|
| 026 | Extract shared executor | Yes, to executor.py |
| 027 | Granularity | Single tool call function |
| 028 | Return type | tuple[Message, bool] |
| 029 | Cancel/steer checks | Strategy owns (not executor) |
| 030 | Counter increment | Executor increments state.tool_calls_made |
| 031 | Public API | Internal only (not exported from __init__.py) |

## Learnings

(Updated during implementation)

## Open Questions

None — all decisions made during /build-arcrun session.
