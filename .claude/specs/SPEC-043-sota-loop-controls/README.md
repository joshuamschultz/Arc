# SPEC-043 — SOTA loop controls

**Feature:** Bring `arcrun`'s execution loop to state-of-the-art on five control dimensions —
**checkpoint/resume** (crash recovery; plus versioned step-provenance *emission* that a future
**SPEC-044 trace-replay** spec consumes — see OQ-4), a **HITL approval pause**, a **generalized circuit
breaker**, **wired concurrent tool dispatch**, and a **Plan-Execute strategy** that closes the concurrent-DAG
parallelism SPEC-040 deferred. Every control reuses an existing seam; none builds a parallel
subsystem. The load-bearing engineering is **concurrency-safe accounting**: concurrent tool calls
and concurrent DAG branches must each still pass the SPEC-034 PolicyPipeline (first-DENY-wins), the
SPEC-038 budget breaker, and the SPEC-035 trifecta ledger — with no lost update and no N-way
overspend.

> **Scope decision (2026-07): true token streaming is CUT.** The roadmap listed it, but for an
> agentic harness streaming is a UX affordance, not a functional/performance/correctness need — we
> want the *output*, not the tokens. It is the one item that would rewrite the loop's model-call
> path (shared by every strategy), enlarging the surface for zero functional gain. Cutting it serves
> "simple, clean, robust, secure." The only residual work is a *cleanup*: delete the misleading
> **fake** word-split in `run_stream` and emit final content as one block. Real per-token streaming,
> if UX ever needs it, already exists in the isolated `stream_llm_response` primitive **outside** the
> loop — it touches no loop control. See §E (PRD) / §3.5 (SDD).

**Status:** PENDING
**Branch:** `feat/SPEC-043-sota-loop-controls` (planning only — no branch/commit created; `.claude/` is gitignored)
**Type:** Generic (arcrun loop-control upgrade + one arcagent orchestration wiring seam; zero new persistence subsystem)
**Phase:** Phase 2 — SOTA + mission control
**Confidence:** High on substrate — every reused seam confirmed at file:line (Strategy ABC, react top-of-turn breaker, the dead `parallel_dispatch.py`, the batch `run_stream`, the SPEC-035 ledger + HumanGate, SPEC-040 `StepExecutor`). Two scope calls now resolved: **streaming is cut** (UX-only — OQ-3) and the **HITL trigger is tool-based** (researched: LangGraph / MS Agent Framework — OQ-1), reusing the SPEC-035 grant. Remaining medium-confidence item is only the `StepExecutor` batch-Protocol shape (OQ-2).

---

## Investigation — built vs. unwired (the recurring pattern, checked at file:line)

| # | Capability | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | **Checkpoint / resume** (crash recovery) | **ABSENT.** No durable loop state at all. | `arcrun/state.py` — `RunState` is an in-memory `@dataclass`; no serialize/`to_dict`/`from_dict` anywhere in `arcrun/src` (grep clean). A mid-loop crash loses everything. SPEC-040 added *plan-level* checkpoint (`arcagent` `PlanStore`), but that is the DAG, not the arcrun turn-loop. (*Replay* — trace step-through + single-step re-exec — is reframed as its own SPEC-044; see OQ-4.) |
| 2 | **HITL approval (loop-level)** | **Interrupt primitives exist; no approval pause.** | `arcrun/loop.py` `RunHandle` has `steer`/`follow_up`/`cancel` + `cancel_event`; there is **no** "pause here and await approval." SPEC-035 `HumanGate` (`arcagent/tools/human_gate.py`) is a *reactive*, policy-DENY-triggered, operator-signed one-shot token — not a proactive loop pause. |
| 3 | **Circuit breaker** | **Partial.** Token+cost only. | `arcrun/strategies/react.py:192-210` — SPEC-038 top-of-turn breaker on `max_tokens`/`max_cost_usd` via `make_budget_breach_args`. No runaway-loop (repeated identical calls / no-progress), no error-cascade, and `max_turns` is a separate tail check. |
| 4 | **Wire `parallel_dispatch`** | **BUILT BUT 100% UNWIRED.** | `arcrun/parallel_dispatch.py` (`BatchClassifier`, `ParallelDispatcher`, `SequentialDispatcher`, `dispatch_batch`) is imported **only by its own test file** (grep). react's `_execute_tool_calls` (`react.py:106-168`) uses a *separate* ad-hoc `tool.parallel_safe` + raw `asyncio.gather` path. Two implementations; the richer classified one is dead. |
| 5 | **True streaming** | **CUT (was FAKE batch).** | `arcrun/streams.py` `run_stream` awaits the *blocking* `run()`, then splits final `content` on spaces into synthetic `TokenEvent`s (`:339-346`) — misleading fake streaming. Decision: **do not** build real streaming; **delete** the fake word-split (cleanup) and emit final content as one block. Real token streaming stays available in the out-of-loop `stream_llm_response` primitive, unchanged, for UX only. |
| 6 | **Plan-Execute Strategy** | **ABSENT in arcrun; seam ready.** | `arcrun/strategies/__init__.py` registers only `react`+`code`. SPEC-040 built the `StepExecutor` Protocol + interim `ArcRunStepExecutor` (one bounded run per step, **sequential**) + `PlanOrchestrator` (one ready step at a time; parallel *explicitly* deferred: `orchestrator.py:63` "parallel dispatch is SPEC-043"). |

**The race nobody has hit yet (because nothing dispatches concurrently through policy):** in `arcagent`
`tool_registry.wrapped_execute` the sequence is `ledger.snapshot(session_id)` → `await pipeline.evaluate(...)`
→ `ledger.record(...)` (`tool_registry.py:385-430`). The `await` is a suspension point. The moment two
tool calls (or two DAG branches sharing a session) run concurrently, both read the pre-write snapshot,
both pass, and a trifecta that only *completes* in the union is never seen — a classic lost-update TOCTOU.
The same shape lets N concurrent Plan-Execute branches each reserve the full `plan.remaining_budget()`
and collectively overspend the ceiling. **This is the spec's hard problem, and it is arcagent's to solve
(it owns the shared state), not arcrun's.**

## Concern-boundary split (the load-bearing decision)

| Concern | Owner | Does | Must NOT do |
|---|---|---|---|
| The loop + all five controls | **arcrun** | turn loop; checkpoint *emission*; the circuit breaker; concurrent tool dispatch; the Plan-Execute concurrent-batch strategy | know about `Plan`/`depends_on`/replan; mint/verify approval tokens; persist to disk; decide policy; stream tokens |
| DAG walk + replan + plan aggregate budget | **arcagent** (`modules/planning`) | compute the ready frontier; hand it to the strategy; apply outcomes; checkpoint the plan; **reserve/settle** the plan aggregate budget atomically | run a turn-loop; dispatch tools directly |
| Trifecta ledger + budget bridge + **admission atomicity** | **arcagent** (`core/tool_registry`, `session_internal`) | make its own `snapshot→evaluate→record` critical section atomic under concurrency (per-session admission lock) | move that lock into arcrun |
| Approval decision + one-shot token | **arcagent** SPEC-035 `HumanGate` | decide "who approves, with what authority"; mint the operator-signed grant | live in arcrun |
| Policy decision (pure predicate) | **arctrust** | first-DENY-wins over injected state | import a sibling; count anything |
| Durable persistence | **existing infra** — `SessionManager` JSONL + `arcstore` WORM spool | store the checkpoint record + resume it | a new parallel persistence subsystem |

**The subtle lines.** (a) *arcrun executes concurrently; arcagent's shared accounting stays atomic* — the
producer of the shared state protects its own invariant, so the admission lock and the budget reservation
live where the state lives (arcagent), not in the loop. (b) *Checkpoint = arcrun produces the resumable
state; arcagent/SessionManager persists it; arcstore WORM is the sink* — arcrun exposes a checkpoint hook
exactly like `on_event`/`transform_context`, never touching the filesystem. (c) *Loop-level HITL = arcrun
owns "when to pause / how to resume"; arcagent's HumanGate owns "who approves"* — arcrun awaits an injected
approval callback and never sees a token. (d) *Plan-Execute = arcagent still owns the DAG (`ready_steps`);
arcrun only ever receives "run these N independent items concurrently and safely"* — the strategy never
learns `depends_on`.

## Open questions for the product owner

1. **OQ-1 — HITL trigger (RESOLVED by the product owner).** Trigger is **tool-based** (SOTA: LangGraph
   HITL middleware, Microsoft Agent Framework Tool-Approval policies — the gate sits *between model intent
   and tool execution*; a policy checks whether the *proposed* call needs review before dispatch). The
   **default approval set is a tier stringency ladder** (ADR-019), set by the PO:

   | Tier | Default HITL behavior |
   |---|---|
   | **personal** | **Free run** — approval set empty; config MAY opt specific tools/tags in. |
   | **enterprise** | HITL before **every tool** call. |
   | **federal** | Full HITL before **every skill and every tool** invocation (every effecting capability). |

   arcrun owns the *pause* (await an injected `approval_provider(call) -> ApprovalGrant | None`, resume via
   `RunHandle`); **arcagent resolves tier → approval set** (the same "arcagent passes the floor, arcrun
   enforces" split as the SPEC-038 budget breaker) and binds the provider to SPEC-035's `HumanGate`
   (operator-signed one-shot token). Proactive, complements SPEC-035's *reactive* trifecta gate — same
   token/authority, different trigger; **no second approval type.** *(Skills vs tools: arcrun's loop sees
   every model-callable capability as a `Tool`; whether one is skill-backed is arcagent metadata, so the
   federal "skills too" rule is a one-line widening of arcagent's resolved set, not an arcrun change.)*
   All four OQs are now resolved (below); nothing is left blocking for the PO.
2. **OQ-2 — concurrent branches (RESOLVED: Option A — caller fans out).** `PlanOrchestrator` dispatches the
   whole `ready_steps()` frontier concurrently over the unchanged `StepExecutor.run_step` (no batch method,
   no `Plan` model change); the arcrun `plan_execute` strategy just reuses the wired `parallel_dispatch`.
   **This is exactly how Claude Code does it** (researched): a *partition algorithm* groups consecutive
   concurrency-safe tool calls into parallel batches and isolates unsafe ones into serial batches, and its
   driving insight is *"concurrency is a property of a specific tool invocation with specific inputs —
   safety is per-call, not per-tool-type."* Arc's existing `parallel_dispatch.BatchClassifier` already
   implements exactly this (read-only vs state-modifying partition + a per-call shared-path check), and
   `dispatch_batch(return_exceptions=True)` already gives the June-2026 Claude-Code fix where a failed
   call returns its own result without cancelling siblings. So Option A is not just simplest — it's the
   SOTA-aligned design, and Arc's substrate already matches it. The only work is *wiring* it (REQ-030).
3. **OQ-3 — true streaming (RESOLVED: CUT).** Streaming is a UX affordance, not a
   functional/performance/correctness need for agentic output — we want the answer, not the tokens, and
   it is the only item that would rewrite the loop's model-call path (every strategy) for zero functional
   gain. Cut real streaming; delete the misleading fake word-split (cleanup only); the loop emits final
   content as one block behind the unchanged `TurnEndEvent`/`collect()` contract; `stream_llm_response`
   stays for out-of-loop UX. *Confirm the cut, or name a UX surface that needs in-loop token streaming
   badly enough to justify the loop-path rewrite.*
4. **OQ-4 — RESOLVED: split "resume" from "replay" (they are two different things).** The roadmap bundled
   "checkpoint/resume+replay," but the PO clarified that **replay** means an *observability/governance*
   capability — step through a captured trace (LLM in/out, tool in/out, **tool version, skill version**,
   timing, cost, policy decision), and re-execute a *single* step from its stored inputs to see its output,
   *without* redoing earlier steps (the full history is already recorded). That is the SOTA
   observability-platform workflow: Langfuse "identify a problematic generation and replay the chain from
   that point with prior inputs and context frozen, then re-run"; LangSmith "open LLM runs from traces in
   the Playground"; Laminar makes span replay first-class. It is **semantically distinct from crash
   recovery** and much larger than a loop control.
   - **SPEC-043 keeps *resume* only** = crash recovery: checkpoint at **turn boundaries**, deterministic
     continue from the last checkpoint, **no re-execution of past side effects** (their results are already
     in the transcript). This is what LangGraph/Temporal-style durable resume does.
   - **SPEC-043's contribution *toward* replay** = ensure arcrun **emits fully versioned step provenance**
     on every step event (LLM input/output + tool input/output under raw-capture, plus **tool version and
     skill version** — supply-chain provenance, LLM03/ASI04). That is the durable substrate replay needs,
     and it is a natural extension of the events arcrun already emits.
   - **The replay *experience* is its own spec** (recommend **SPEC-044 — trace replay / time-travel
     governance**): the step-through UI + single-step re-execution (LLM steps re-run freely; side-effecting
     tool re-runs sandboxed/gated) + the "open in playground" workflow. It spans **arcstore** (durable
     versioned trace store — already the arcui data source), **arcui** (the step-through + re-run surface),
     and **arctrust** (audit/provenance), with arcrun only as the *emitter*. Keeping it out of SPEC-043
     holds the loop-controls spec simple, clean, and robust. **Confirm the split + spinning up SPEC-044.**

## Deliverables

- `PRD.md` — EARS requirements, MoSCoW, pillar-tied acceptance criteria, threat mapping (LLM10 unbounded/runaway; ASI08 cascading failures; LLM06 excessive agency / HITL; concurrency-race integrity of the SPEC-038 ledger + budget; CP-10 resume).
- `SDD.md` — components, boundaries, the **concurrency-safe accounting** design, how the Plan-Execute strategy closes SPEC-040's deferred parallelism behind the `StepExecutor` seam, and **Research Insights** (LLMCompiler + Claude Code partition-dispatch, agent-loop checkpoint/resume, circuit-breaker/runaway detection, tool-based HITL, observability trace-replay → SPEC-044 — cited).
- `PLAN.md` — TDD tasks, each scoped to one module, REQ→component→task traceable, with explicit interleaving-forced concurrency-race tests (asyncio `Barrier`/`Event`, not an instant mock).
