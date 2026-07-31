# PRD: Recursive Agent Spawning

## Problem Statement

ArcRun currently executes a single agent loop per `run()` call. Complex tasks requiring decomposition, parallel research, or multi-step delegation must be orchestrated manually by the caller. This forces complexity upstream into ArcAgent or end-user code instead of letting the execution engine handle it.

A model should be able to decide "this task is better solved by splitting into sub-tasks" and act on that decision within the loop — spawning child runs that execute independently and return results.

## Requirements

### Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | ArcRun provides a built-in `spawn_task` tool in `arcrun/builtins/spawn.py` | Must |
| FR-2 | `spawn_task` accepts `task` (required), `system_prompt` (optional), `tools` (optional list of tool names) | Must |
| FR-3 | Calling `spawn_task` starts a child `run()` with depth + 1, inheriting parent's model | Must |
| FR-4 | Child inherits parent's tools unless `tools` parameter specifies a subset | Must |
| FR-5 | Child returns `LoopResult.content` as the tool result string to parent | Must |
| FR-6 | Child failure returns error string as tool result (same as any failed tool) | Must |
| FR-7 | `RunState` tracks `depth`, `max_depth`, `parent_run_id` as flat fields | Must |
| FR-8 | Spawn is rejected with error when `depth >= max_depth` | Must |
| FR-9 | `run()` and `run_async()` accept optional `depth` and `max_depth` parameters | Must |
| FR-10 | Default `max_depth` is 3 | Must |
| FR-11 | Child events propagate to parent EventBus with `child.{run_id}.` prefix | Must |
| FR-12 | When model emits multiple `spawn_task` calls in one turn, they execute concurrently via `asyncio.gather` | Should |
| FR-13 | `spawn_task` tool is exported from `arcrun.builtins` and `arcrun.__init__` | Must |
| FR-14 | `spawn_task` tool follows same factory pattern as `make_execute_tool` | Must |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-1 | Zero additional dependencies | 0 new packages |
| NFR-2 | Spawn tool LOC | <= 80 lines |
| NFR-3 | RunState changes | <= 5 new fields |
| NFR-4 | All existing tests continue passing | 0 regressions |
| NFR-5 | `mypy --strict` passes | 0 errors |
| NFR-6 | `ruff check` passes | 0 errors |
| NFR-7 | Child isolation | No shared state between children |

## Success Criteria

1. A mock model can call `spawn_task` and receive the child's result as a tool result string
2. Nested spawning works (parent -> child -> grandchild) up to `max_depth`
3. Depth limit enforcement rejects spawns at max depth with clear error
4. Multiple `spawn_task` calls in one turn execute concurrently
5. Child events appear on parent's event bus with correct prefix
6. All existing tests pass with zero regressions
7. `mypy --strict` and `ruff check` clean

## Out of Scope (v2+)

- NATS distributed execution (v1 is in-process asyncio)
- Resource budget enforcement (token/cost/time limits)
- Identity/DID inheritance (ArcAgent concern)
- Cross-child communication (siblings don't talk)
- Persistent child agents (children are ephemeral)
- Team strategy (coordinated multi-agent with roles)
- ArcAgent spawn tool override (ArcAgent implements when ready)
