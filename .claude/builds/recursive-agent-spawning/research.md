# recursive-agent-spawning — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-131–D-144 (14 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Feature: Recursive Agent Spawning (ArcRun v1)

**Date**: 2026-02-16
**Source**: `.claude/brainstorms/2026-02-16-recursive-agent-spawning.md`
**Goal**: Enable ArcRun to recursively spawn child agent loops for task decomposition and parallel execution

#### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|

#### V1 Scope

**In scope:**
- `spawn_task` built-in tool in `arcrun/builtins/spawn.py`
- Flat depth/budget fields on `RunState`
- `depth` and `max_depth` params on `run()` / `run_async()`
- Depth enforcement (reject spawn if `depth >= max_depth`)
- Event bubbling from child to parent with `child.{run_id}.` prefix
- In-process asyncio execution
- Parallel spawn via multiple tool calls in one turn
- `parent_run_id` on `RunState` for lineage tracking

**Deferred to v2+:**
- NATS distributed execution
- Resource budget enforcement (tokens, cost, time)
- Identity/DID inheritance (ArcAgent override)
- Cross-child communication
- Persistent agents
- Team strategy (coordinated multi-agent with roles)

#### Key Design Principles

- **Spawn is a tool, not a strategy** — The react loop doesn't change. It gains a powerful tool.
- **ArcRun is standalone** — Spawn works without ArcAgent. `pip install arcrun` gets you spawning.
- **A child is just another run()** — No special classes. Recursion via flat fields (depth, parent_run_id).
- **Complete isolation** — No shared state between children. Results flow up only.
- **Full observability** — Event bubbling gives parent complete visibility into child execution.

#### Components to Build

1. **`arcrun/builtins/spawn.py`** — Built-in spawn tool
2. **`arcrun/state.py`** — Add flat fields (depth, max_depth, parent_run_id, budgets)
3. **`arcrun/loop.py`** — Add depth/max_depth params to run()/run_async()
4. **`arcrun/strategies/react.py`** — Parallel tool execution for spawn calls
5. **Tests** — Mock model + real spawn, depth limits, event bubbling, parallel, errors

#### Architecture Diagram

```
run(model, tools, prompt, task, depth=0, max_depth=3)
    |
    v
ReactStrategy (react loop)
    |
    +-- model decides to call spawn_task(task="research X")
    |
    v
spawn_task tool (builtins/spawn.py)
    |
    +-- checks depth < max_depth
    +-- creates child EventBus (bubbles to parent)
    +-- calls run(model, tools, child_prompt, child_task, depth=depth+1)
    |
    v
Child ReactStrategy (independent react loop)
    |
    +-- uses inherited model + tools (or subset)
    +-- can itself call spawn_task (if depth allows)
    +-- returns LoopResult
    |
    v
Parent receives LoopResult.content as tool result string
    |
    +-- continues react loop with child's answer
```

---

---
