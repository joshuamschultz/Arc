# SPEC-043 — PRD

**Status:** PENDING
**Pillars (ranked):** Simplicity → Modularity → Security → Scalability.
**Steering:** `product.md` (premier federal agentic harness), `roadmap.md` Phase 2 line
*"SOTA loop controls — checkpoint/resume+replay; HITL approval primitive; circuit breaker; wire
parallel_dispatch; true streaming; Plan-Execute Strategy. [arcrun]"*.

Five loop-control capabilities for `arcrun`, each **wiring an existing seam**, unified by one
non-negotiable invariant: **concurrency must never bypass or race a gate.** Every requirement is
EARS-formatted, MoSCoW-prioritized, and tied to the pillar it serves.

> **Streaming cut (2026-07).** The roadmap's sixth item — "true streaming" — is descoped. For agentic
> work the deliverable is the *output*, not progressive tokens; streaming is UX-only, and it is the one
> item that would rewrite the loop's model-call path for zero functional gain. Section E is reduced to a
> one-requirement **cleanup** (delete the fake word-split; keep the output contract). Serves the
> Simplicity/Security pillars: smaller surface, no fake-vs-real dual path.

---

## Goals

- G1 — A crashed run resumes mid-loop from a durable checkpoint without redoing completed work (CP-10, crash recovery); and every step emits versioned provenance (tool/skill version + in/out) as the substrate a future replay spec (SPEC-044) consumes.
- G2 — The loop can pause for human approval at a designated point and resume on an authenticated grant, reusing SPEC-035 authority (LLM06/ASI09).
- G3 — The loop halts on runaway, error-cascade, and turn/budget exhaustion — one breaker, structured terminators (LLM10/ASI08).
- G4 — Independent tool calls and independent DAG branches execute concurrently, each still fully gated, with **zero** lost-update or overspend (LLM06/LLM10, ledger/budget integrity).
- G5 — Output is the final answer via the unchanged `RunResult`/`TurnEndEvent` contract; the loop carries **no** token streaming (UX-only, out of scope) and the misleading fake word-split is removed.
- G6 — A Plan-Execute strategy runs SPEC-040 plans with concurrent DAG-branch execution, behind the existing `StepExecutor` seam, with no plan-model change.

## Non-goals

- No new persistence subsystem (reuse `SessionManager` JSONL + `arcstore` WORM).
- No new approval-token type (reuse SPEC-035 `ApprovalGrant`).
- **No in-loop token streaming** — cut as UX-only; `stream_llm_response` remains available outside the loop.
- No re-execution of side effects on resume (resume = deterministic continue, not re-run — OQ-4).
- **No trace-replay experience** (step-through review, single-step re-execution, playground). That is reframed as its own **SPEC-044 — trace replay / time-travel governance** (spans arcstore/arcui/arctrust). SPEC-043 only *emits* the versioned provenance (REQ-007).
- No change to arctrust policy-layer algorithms or the SPEC-040 `Plan` model.
- Full data-provenance taint tracking for the untrusted-input leg stays out of scope (inherited from SPEC-035 OQ-1).

## Users

Operators running long or expensive federal agent workloads (crash recovery, hard budget floors,
approval gates); the planner module (concurrent plan execution); UI/gateway consumers (live streaming).

---

## Functional requirements (EARS)

### A. Checkpoint / resume (crash recovery) + versioned trace-provenance emission — arcrun emits, arcagent persists (G1)

*Scope note: "resume" here is **crash recovery** — deterministic continue from the last checkpoint, no
re-execution of past side effects. The observability/governance **replay** (trace step-through +
single-step re-execution) is reframed as its own **SPEC-044** (OQ-4); SPEC-043's only replay contribution
is emitting the versioned provenance that SPEC-044 will consume — REQ-007.*

- **REQ-001 (Must, Security/Scalability):** WHEN a turn boundary is reached, THE arcrun loop SHALL produce a serializable checkpoint of resumable state (turn count, token/cost totals, message list, `completion_payload`/`completion_tool`, strategy name, frozen tool-name set, run/parent id) via an injected checkpoint hook.
- **REQ-002 (Must, Modularity):** WHERE a checkpoint hook is supplied, THE arcrun loop SHALL invoke it at each turn boundary and SHALL NOT itself write to any filesystem or database (persistence is the caller's).
- **REQ-003 (Must, Security):** WHEN `run(..., resume_from=<checkpoint>)` is called, THE arcrun loop SHALL reconstruct `RunState` from the checkpoint and re-enter the loop at the saved turn without re-executing completed turns (deterministic resume — NOT side-effect replay).
- **REQ-004 (Must, Correctness):** WHILE resuming, THE loop SHALL enforce that the reconstructed frozen tool-name set equals the checkpoint's set, and SHALL fail closed (refuse resume) on mismatch (a changed tool surface is a poisoned resume — ASI06).
- **REQ-005 (Should, Modularity):** WHERE arcagent drives the run, THE `SessionManager` SHALL persist each checkpoint through its existing append-only JSONL path and THE emitted-event prefix SHALL be reconstructable from the tamper-evident audit chain (no inline per-event duplication — OQ-4).
- **REQ-006 (Should, Security):** WHEN a checkpoint is persisted at enterprise/federal, THE record SHALL ride the `arcstore` WORM spool so resume state is tamper-evident (AU-9, CP-10 evidence).
- **REQ-007 (Should, Security — replay substrate):** WHEN the loop emits a step event (LLM call, tool start/end), THE event SHALL carry the provenance a future replay needs: the LLM input/output and tool input/output (under the existing raw-capture flag) **plus the tool version and skill version** that produced the step (supply-chain provenance — LLM03/ASI04). arcrun SHALL only *emit* this; the durable versioned trace store + the step-through/re-execution *experience* are **out of scope** (SPEC-044).

### B. HITL approval pause — arcrun pauses, SPEC-035 decides (G2)

- **REQ-010 (Must, Security):** WHEN the model proposes a tool call whose identity matches the deployment's **approval-required** policy — a config-declared set of tool names / `capability_tags` / `classification` (SOTA tool-based trigger: side-effecting, irreversible, or sensitive tools) — and an `approval_provider` is injected, THE arcrun loop SHALL suspend *before* dispatch and await the provider's decision.
- **REQ-010a (Should, Simplicity):** THE approval-required trigger SHALL be evaluated on the *proposed* tool call (post model-intent, pre-execution), matching the LangGraph/Microsoft-Agent-Framework tool-approval pattern; no separate approval taxonomy is introduced beyond tool identity/tags/classification already carried on the call.
- **REQ-010b (Must, Security, ADR-019):** THE default approval set SHALL follow the tier stringency ladder, resolved by arcagent: **personal** → empty (free run; config MAY opt tools/tags in); **enterprise** → **every tool** call requires approval; **federal** → **every skill and every tool** invocation requires approval. arcagent SHALL resolve tier → set; arcrun SHALL enforce the resulting predicate (same split as the SPEC-038 budget floor).
- **REQ-010c (Must, Security):** WHERE tier is federal, THE approval-required set SHALL include skill-backed capabilities (not only plain tools); arcagent SHALL mark skill-backed `Tool`s so the federal set is the full effecting-capability surface. WHERE tier is enterprise, THE default set SHALL be all tools; personal MAY be empty.
- **REQ-011 (Must, Security):** IF the `approval_provider` returns a grant, THEN THE loop SHALL proceed with the single approved call; IF it returns `None` (deny/timeout/no channel), THEN THE loop SHALL fail closed and not dispatch the call.
- **REQ-012 (Must, Modularity):** THE arcrun loop SHALL NOT mint, sign, or verify any approval token; approval authority SHALL be supplied by arcagent binding the provider to SPEC-035 `HumanGate` (operator-signed one-shot `ApprovalGrant`).
- **REQ-013 (Should, Simplicity):** THE pause SHALL reuse the existing `RunHandle` interrupt machinery (cancel/steer/wait), NOT introduce a second queue/await subsystem.
- **REQ-014 (Should, Security):** WHILE paused for approval, THE loop SHALL remain checkpointable so an approval that arrives after a restart still resumes the correct call (composes A+B).

### C. Generalized circuit breaker — reuse the SPEC-038 hook point (G3)

- **REQ-020 (Must, Security/Scalability):** WHERE the SPEC-038 top-of-turn breaker evaluates, THE loop SHALL additionally detect a **runaway loop** — the same tool-call signature (name + canonical args hash) repeated beyond a configured no-progress threshold — and halt via a structured terminator (`reason="runaway_loop"`).
- **REQ-021 (Must, Security):** THE loop SHALL detect an **error cascade** — consecutive tool failures beyond a configured threshold — and halt (`reason="error_cascade"`), preventing a failing dependency from burning the budget (ASI08).
- **REQ-022 (Must, Simplicity):** THE loop SHALL enforce `max_turns` at the same breaker hook point, using the same `make_budget_breach_args` terminator vocabulary (delete the separate tail check — no parallel breaker).
- **REQ-023 (Must, Modularity):** ALL breaker trips (token, cost, turns, runaway, error-cascade) SHALL emit `loop.completed` with a structured `completion_payload` carrying the reason; consumers SHALL distinguish reasons without re-scanning the event chain.
- **REQ-024 (Must, Security, ADR-019):** WHERE tier is federal, THE breaker thresholds (max_turns cap, runaway/cascade limits) SHALL be non-relaxable floors supplied by config; personal MAY relax or disable them.
- **REQ-025 (Should, Correctness):** THE runaway detector SHALL treat a `parallel_safe` batch of distinct signatures as progress (no false trip on legitimate fan-out).

### D. Wire `parallel_dispatch` — one dispatch path, fully gated (G4)

- **REQ-030 (Must, Simplicity):** THE react loop SHALL dispatch a turn's tool calls through the existing `arcrun.parallel_dispatch` (`BatchClassifier` + `dispatch_batch`), and THE ad-hoc `parallel_safe`/`asyncio.gather` path in `_execute_tool_calls` SHALL be deleted in the same change (no two implementations).
- **REQ-031 (Must, Security):** WHEN a batch is dispatched concurrently, EACH tool call SHALL still pass the full arcagent `wrapped_execute` pipeline — SPEC-034 PolicyPipeline (first-DENY-wins), SPEC-038 budget/provider layer, SPEC-035 trifecta ledger — with no bypass and no reordering of the deny decision.
- **REQ-032 (Must, Security — the hard one):** WHILE multiple calls in one batch run concurrently and share a session, THE trifecta ledger's `snapshot → policy-evaluate → record` critical section SHALL be atomic per session, such that two calls whose *union* completes a forbidden composition are NOT both allowed (no lost update — ASI06/LLM06).
- **REQ-033 (Must, Correctness):** THE dispatcher SHALL return results in submission order regardless of completion order, and a partial failure in the batch SHALL NOT abort sibling calls (already true of `dispatch_batch`; the wiring SHALL preserve it).
- **REQ-034 (Must, Security):** THE `BatchClassifier` SHALL fail closed — any state-modifying or unknown-classification tool, or an implicit shared-resource dependency, forces sequential execution.
- **REQ-035 (Should, Scalability):** Concurrent in-flight calls SHALL be bounded by a configured semaphore ceiling so fan-out cannot exhaust resources (LLM10).

### E. Streaming — CUT to a cleanup (G5)

Real in-loop token streaming is **out of scope** (UX-only; see the scope note). The loop keeps calling
`model.invoke` (one clean call), and its output is the final answer. The only work here is removing the
misleading fake path.

- **REQ-040 (Must, Simplicity):** THE synthetic post-hoc word-splitting in `run_stream` (`streams.py:339-346`) SHALL be deleted; the loop SHALL surface final content as a single block (or the existing structured tool/turn events), NOT fabricated per-word `TokenEvent`s.
- **REQ-041 (Must, Modularity):** THE `TurnEndEvent` and `collect()` `RunResult` contract SHALL be unchanged, so every existing one-shot consumer keeps working; `run_stream` remains the run/event entry, minus the fake tokens.
- **REQ-042 (Must, Simplicity):** THE react loop SHALL NOT be modified to call `model.invoke_stream`; the model-call path stays a single non-streaming `invoke` (no loop-path rewrite).
- **REQ-043 (Should, Modularity):** THE out-of-loop `stream_llm_response` primitive SHALL remain available and unchanged for UX (chat typing effect); it is not a loop control and touches no gate.

### F. Plan-Execute strategy — concurrent DAG branches behind the SPEC-040 seam (G6)

- **REQ-050 (Must, Modularity):** THE arcrun `strategies` registry SHALL gain a `plan_execute` `Strategy` (alongside `react`/`code`) that runs a batch of *independent* ready items concurrently and returns per-item outcomes.
- **REQ-051 (Must, Modularity):** THE `plan_execute` strategy SHALL receive only independent, ready items from the caller and SHALL NOT know `Plan`, `depends_on`, or replan (the DAG stays in arcagent's `PlanOrchestrator`).
- **REQ-052 (Must, Security):** EACH branch's step run SHALL pass SPEC-034 policy + SPEC-038 budget + SPEC-035 classification exactly as a normal `arcrun.run` does (a branch is a bounded run; no bypass).
- **REQ-053 (Must, Security — the hard one):** WHEN N branches run concurrently under one plan, THE plan aggregate budget SHALL be enforced atomically by **reserve-then-settle**: each branch reserves its cap from the shared remaining budget before launch and settles actual spend on completion, such that the sum of concurrent spend cannot exceed `Plan.budget` (no N-way overspend — LLM10).
- **REQ-054 (Must, Modularity):** `plan_execute` SHALL satisfy SPEC-040's `StepExecutor` seam so the planner swaps `ArcRunStepExecutor` for the concurrent executor **without changing the `Plan` model** (REQ-020/025 of SPEC-040).
- **REQ-055 (Must, Security):** IF any branch fails (policy DENY, budget breach, tool error), THEN its outcome SHALL be captured as a `FAILED` `StepOutcome` and SHALL NOT crash sibling branches or the orchestrator (blast-radius containment — ASI08).
- **REQ-056 (Should, Scalability):** Concurrent branch count SHALL be bounded by config; the concurrency mechanism SHALL reuse the wired `parallel_dispatch` primitive rather than a second gather path.

### G. Cross-cutting: tier & audit

- **REQ-060 (Must, Security, ADR-019):** Every new control SHALL evaluate at every tier; tier changes only stringency (federal: non-relaxable breaker floors, HITL may be mandatory, checkpoint on WORM).
- **REQ-061 (Must, Security):** Every new operation (checkpoint write, approval pause/grant/deny, breaker trip, concurrent dispatch, branch outcome) SHALL emit an audit event through the existing sink path (AU-2).

---

## MoSCoW summary

| Priority | Requirements |
|---|---|
| **Must** | 001–004, 010, 010b, 010c, 011, 012, 020–024, 030–034, 040, 041, 042, 050–055, 060, 061 |
| **Should** | 005, 006, 007, 010a, 013, 014, 025, 035, 043, 056 |
| **Could** | real sliding-window rate limit for runaway; per-tool-call (sub-turn) checkpoint granularity |
| **Won't (cut)** | in-loop token streaming (UX-only — `stream_llm_response` covers it outside the loop) |
| **Won't (now)** | new persistence subsystem; new token type; side-effect replay; full taint tracking |

## Pillar-tied acceptance criteria

- **Simplicity** — AC-S1: `parallel_dispatch.py` has exactly one production caller after REQ-030 (grep); the ad-hoc gather path is gone. AC-S2: all five breaker reasons flow through one hook point and one terminator factory.
- **Modularity** — AC-M1: `arcrun` imports no `arcagent`/`arctrust`/`arcteam` (boundary import test stays green). AC-M2: the `Plan` model diff for SPEC-043 is empty; only the injected executor changes. AC-M3: arcrun never references a token type.
- **Security** — AC-Sec1: an interleaving-forced test (asyncio `Barrier`) proves two concurrent calls whose union completes the trifecta are NOT both allowed (REQ-032). AC-Sec2: an interleaving-forced test proves N concurrent branches cannot overspend `Plan.budget` (REQ-053). AC-Sec3: resume with a mutated tool set is refused (REQ-004). AC-Sec4: approval-required call with `None` provider result is not dispatched (REQ-011). AC-Sec5: federal breaker floors cannot be raised by agent-supplied params (REQ-024).
- **Scalability** — AC-Sc1: concurrent dispatch and concurrent branches are semaphore-bounded (REQ-035/056). AC-Sc2: checkpoint is O(1) per turn and off the hot path when no hook is supplied (cold-start budget preserved).

## Threat mapping

| Threat | Requirement(s) | Mitigation |
|---|---|---|
| **LLM10 Unbounded / runaway consumption** | 020, 022, 035, 053 | Runaway + max-turns breaker at one hook; semaphore ceilings; atomic aggregate budget reserve-then-settle prevents N-way overspend. |
| **ASI08 Cascading failures** | 021, 055 | Error-cascade breaker; per-branch failure isolation — one bad branch/step never aborts siblings or the plan. |
| **LLM06 Excessive agency** | 010, 010a, 011, 012, 031, 052 | Tool-based HITL approval pause on flagged actions (operator authority, not the agent); every concurrent call still policy-gated. |
| **ASI06 Memory/context poisoning** | 004, 032 | Resume refuses a changed tool surface; atomic ledger admission prevents a poisoned partial-union bypass. |
| **ASI09 Human-agent trust exploitation** | 012 | Approval token is operator-signed; the agent has no path to mint it; requests are labeled agent-originated. |
| **Concurrency-race integrity (SPEC-038 ledger/budget)** | 032, 053 | Per-session admission lock (ledger) + reserve-then-settle (budget); interleaving-forced tests are acceptance gates. |
| **CP-10 (resume) / AU-9 (tamper-evident)** | 003, 005, 006, 061 | Deterministic resume from durable checkpoint; WORM-backed at federal; audit on every control op. |
