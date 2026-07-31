# Brainstorm: Recursive Agent Spawning in ArcRun

**Date**: 2026-02-16
**Topic**: Recursive spawning of agents as execution strategies in ArcRun
**Status**: Brainstorm complete. Ready for `/build`.

---

## Inspiration

Two driving forces:

1. **Multi-step decomposition** — Complex tasks need to be broken into sub-tasks and delegated to focused child agents.
2. **Parallel fan-out** — N agents running concurrently, each solving a niche sub-problem, results aggregated by parent.

**Core insight**: A niche agent solving a specific sub-problem outperforms a generalist trying to solve everything. Decompose, specialize, aggregate = better results, faster.

---

## Vision

**ArcRun is a self-organizing stateless execution engine.** It receives a job (task + tools + model) and executes it in the best possible way. Today that means choosing between `react` and `code` strategies. This brainstorm expands the strategy space:

| Strategy | Pattern |
|----------|---------|
| `react` | Single-agent iterative loop (existing) |
| `code` | Code execution strategy (existing) |
| `spawn` | Decompose task into sub-tasks, execute child loops |
| `team` | Coordinate multiple agents with roles working in parallel |
| `hybrid` | Team where some members spawn sub-tasks of their own |

**ArcAgent provides the WHAT** (tools, model, prompt, identity, permissions).
**ArcRun decides the HOW** (strategy selection, execution mechanics, security).

The model has full autonomy to decide WHEN to decompose and HOW to split work. Strategy selection includes spawn/team as first-class options.

---

## Audience

- **Framework developers (us)**: Building the spawn/team primitives and strategies.
- **Agent builders (developers using Arc)**: Configuring spawn behavior, tool subsets for children, depth limits.

---

## Desired Outcomes

**Self-organizing execution.** A single `run()` call handles tasks that today require manual orchestration. ArcRun becomes intelligent about HOW to execute — it reasons about decomposition, not just tool calls.

Specific outcomes:
- **Time efficiency** — Parallel execution means faster results
- **Quality improvement** — Focused agents solve specific problems better than one generalist
- **Better outcomes faster** — Decomposition + specialization = higher quality in less time

---

## Guiding Principles

All four are non-negotiable. No tradeoffs between them.

### 1. Blast Radius Containment
A rogue child must never compromise the parent or siblings. Isolation is non-negotiable. Shared-nothing per child agent. Failure of one child doesn't cascade.

### 2. Total Observability
Parent must see everything children do. Full audit trail across the tree. Every spawn, every tool call, every result. No blind spots.

### 3. Resource Budgets Cascade
Parent's token/cost/time budget splits across children. No unbounded consumption. If budget exhausted, no more spawning. Maps to LLM10 (Unbounded Consumption) and ASI08 (Cascading Failures).

### 4. Simplicity of the Primitive
The spawn/team mechanism must be as simple as the current `run()` API. Complexity lives in strategies, not in the API surface. The public interface stays clean.

---

## Constraints

### Depth
- Configurable recursion depth via parameter
- Default: 3 levels (parent -> child -> grandchild)
- Prevents runaway recursion

### Concurrency Model
- **NATS message bus** (distributed)
- Children can run on different nodes
- Fits the existing scalability architecture
- NATS is already a core dependency

### Result Flow
- **LoopResult returned directly**
- Child returns LoopResult to parent
- Parent strategy processes it like any other tool result
- Simple, consistent, no special handling

### Strategy Selection
- **Model decides everything**
- Model picks strategy AND decides task decomposition
- Full autonomy within the primitives ArcRun provides
- Same pattern as existing strategy selection, just expanded scope

### Exclusions
- **No persistent agents** — Children are ephemeral. Spawn, execute, return, die.
- Long-lived agent management is an ArcAgent concern, not execution.

---

## Open Questions for /build

These are design decisions, not brainstorm questions. Defer to `/build`.

1. **Shared memory between children?** — Children are isolated today. Should siblings share a read-only context? Or strictly nothing?
2. **Cross-child communication?** — Should siblings talk to each other, or only through the parent?
3. **How does the model express decomposition intent?** — Special tool call? Structured output? Strategy-internal prompting?
4. **How do resource budgets split?** — Equal division? Priority-weighted? Dynamic reallocation?
5. **Strategy composition** — Can a `team` strategy have members that use `spawn` strategy internally? How deep does composition go?
6. **NATS protocol design** — What do spawn/result/cancel messages look like on the wire?
7. **Identity inheritance** — Does a child inherit parent's DID? Get its own? Scoped delegation?
8. **Tool subsetting** — Does the parent decide which tools children get, or does the model decide?
9. **Error aggregation** — When 3 of 5 children fail, how does the parent strategy handle partial results?
10. **Steering through the tree** — Can a user steer a grandchild? Or only the immediate parent?

---

## Layer Mapping

```
ArcAgent (WHAT)                    ArcRun (HOW)
-----------------                  -----------------
Tools to provide                   Strategy selection (react/code/spawn/team)
System prompt                      Execution mechanics
Model selection                    Depth enforcement
Identity & permissions             Resource budget tracking
                                   NATS spawn/result protocol
                                   Cancellation cascading
                                   Event propagation (parent <-> child)
                                   Sandbox enforcement per child
```

---

## Next Steps

- `/build` — Walk through each open question as a design decision
- `/specify` — After decisions are made, formal spec for implementation
