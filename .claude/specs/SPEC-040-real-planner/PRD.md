# PRD — SPEC-040 Real planner (Plan-Execute)

## Product context

- **Steering:** `.claude/specs/ROADMAP-PROGRAM.md` → Phase 2 → *SPEC-040 Real planner — Plan-Execute/LLMCompiler; durable/checkpointed plan state; replan. [arcagent]*.
- **Problem:** The planning module is a to-do notebook, not a planner. It cannot decompose a goal, cannot order dependent work, cannot execute a step, cannot survive a restart with progress intact, and cannot recover from a failed step except by leaving it stuck. A federal agentic harness needs a planner that produces an auditable plan artifact, executes it under the existing security pillars, and adapts when reality diverges.
- **Outcome:** Given a goal, the agent produces a **durable, checkpointed, dependency-aware plan**, executes it step-by-step through arcrun (every step policy- and budget-gated), resumes cleanly after interruption, and **replans** the remainder on failure — bounded so it can never run away.
- **Non-goals:** (a) the arcrun Plan-Execute *strategy* and parallel DAG dispatch — that is **SPEC-043**; (b) any new LLM-calling path — decomposition/replan inference goes through **arcllm**; (c) any change to `arcagent/core` — the planner lives entirely in `modules/planning/` and adds **zero** core LOC; (d) a new persistence subsystem — durable state reuses workspace JSONL + arctrust audit sinks.

## Pillars (priority order per CLAUDE.md)

1. **Simplicity** — one `Plan` model, one durable file per plan, one replan function, one `StepExecutor` seam. No parallel dispatch, no new store.
2. **Modularity** — planner is a module; execution is arcrun behind a Protocol seam; inference is arcllm; persistence is existing infra. Swapping in SPEC-043's strategy is a seam replacement, not a rewrite.
3. **Security** — every step's tools stay behind SPEC-034 policy + SPEC-038 budget; the plan is subordinate to immutable identity goals (ASI01); every plan/step transition is audited and tamper-evident (AU-9/10).
4. **Scalability** — plan state is O(steps) and shared-nothing per agent; replan and resume are bounded; step budgets roll up to the run ceiling.

---

## Requirements (EARS + MoSCoW)

Priorities: **M**ust / **S**hould / **C**ould / **W**on't (this cycle).

### A. Plan decomposition & data model

- **REQ-001 (M).** When the planner receives a goal, the system **shall** produce a `Plan` containing an ordered set of `PlanStep`s with explicit `depends_on` edges forming a DAG (no cycles). *(Pillar: Simplicity, Modularity)*
- **REQ-002 (M).** The system **shall** obtain the decomposition from an LLM **through arcllm** (the planner module shall not call a provider adapter directly and shall not implement a turn-loop). *(Pillar: Modularity)*
- **REQ-003 (M).** The `Plan` **shall** record `goal`, `goal_source_did` (who set the goal), a `parent_goal_hash` binding it to the agent's immutable identity goals, `version` (incremented on replan), and per-step `status ∈ {pending, ready, running, succeeded, failed, skipped}`. *(Pillar: Security — ASI01, ASI06)*
- **REQ-004 (S).** Where a step names a tool, the system **shall** treat that as an advisory `tool_hint` only; actual dispatch authority remains the policy pipeline's. *(Pillar: Security — LLM06)*
- **REQ-005 (S).** When decomposition returns a plan that cannot be grounded (references no known capability, or contradicts the goal), the system **shall** reject it and surface the reason rather than persist an ungrounded plan. *(Pillar: Security — LLM09)*

### B. Durable / checkpointed plan state

- **REQ-010 (M).** The system **shall** persist each `Plan` durably to the workspace (`plans/<plan_id>.json`), reusing the SessionManager JSONL/atomic-write persistence pattern — **not** a new store. *(Pillar: Simplicity, Modularity)*
- **REQ-011 (M).** When a step transitions state (ready→running→succeeded/failed) or a plan is created/replanned/completed/abandoned, the system **shall** checkpoint the plan to disk **before** proceeding to the next step. *(Pillar: Security — CP-10; Scalability)*
- **REQ-012 (M).** After a restart or a compaction boundary, when an active plan exists for the session, the system **shall** resume it by reloading plan state and skipping already-`succeeded` steps (re-deriving the ready set from `depends_on`). *(Pillar: Scalability — CP-10)*
- **REQ-013 (M).** For every plan/step transition, the system **shall** emit an `AuditEvent` through the existing arctrust sink (JsonlSink for compliance, SignedChainSink where configured) — one emission point, sinks fan out. *(Pillar: Security — AU-2/AU-9/AU-10)*
- **REQ-014 (S).** Plan-state writes **shall** be integrity-checked on read (reject a malformed/truncated plan file rather than execute a corrupt plan). *(Pillar: Security — ASI06)*

### C. Execution through the existing loop

- **REQ-020 (M).** The system **shall** execute a ready step by driving the **existing arcrun run seam** (one bounded run per step) via an injected `StepExecutor` Protocol — the planner shall not dispatch tools itself. *(Pillar: Modularity)*
- **REQ-021 (M).** Every tool a step invokes during execution **shall** pass through `ToolRegistry` → `PolicyPipeline` (first-DENY-wins, fail-closed) unchanged; the planner **shall** add no bypass. *(Pillar: Security — LLM06, ASI02, AC-3)*
- **REQ-022 (M).** Each step run **shall** be bounded by the arcrun token/cost circuit-breaker (SPEC-038); the plan **shall** carry an aggregate budget that maps onto per-step run ceilings so a plan cannot exceed the run's budget. *(Pillar: Security — LLM10; Scalability)*
- **REQ-023 (M).** When a step run reports a policy DENY, a budget breach, or a tool error, the system **shall** mark the step `failed` with the reason captured (not silently retried). *(Pillar: Security — LLM05; Simplicity)*
- **REQ-024 (S).** The system **shall** execute steps in a valid topological order of the DAG; independent branches **may** be ordered arbitrarily but **shall not** run before their `depends_on` prerequisites `succeeded`. *(Pillar: Modularity)*
- **REQ-025 (W — deferred to SPEC-043).** The system **shall** dispatch independent DAG branches concurrently. *(Owner: arcrun `parallel_dispatch`; out of scope this cycle — see OQ-2.)*

### D. Replan loop

- **REQ-030 (M).** When a step fails, the system **shall** replan the **remaining** steps — re-invoking decomposition (via arcllm) with the goal, the completed steps + their results, and the failure reason — and **shall not** discard already-`succeeded` work. *(Pillar: Simplicity, Scalability)*
- **REQ-031 (M).** Replan **shall** be bounded by a configurable `max_replans` ceiling; on exhaustion the system **shall** mark the plan `failed` with a structured terminator (mirroring arcrun's `make_budget_breach_args` pattern) rather than loop indefinitely. *(Pillar: Security — LLM10/ASI08)*
- **REQ-032 (M).** Each replan **shall** increment `Plan.version`, preserve the completed-step prefix, and emit a `plan.replanned` audit event carrying the trigger and the version delta. *(Pillar: Security — AU-2)*
- **REQ-033 (S).** When a step **succeeds** but its result diverges from the plan's expectation (a re-evaluation predicate the planner supplies), the system **shall** be able to trigger a replan of the remainder (not only on hard failure). *(Pillar: Modularity — reflection seam)*

### E. Goal integrity, grounding, surface

- **REQ-040 (M).** The plan **shall** be subordinate to the agent's immutable goals: the planner **shall not** write `identity.md` or `policy.md` (already enforced by the SPEC-035 protected-path denylist) and **shall** reject a decomposition whose steps target those protected paths. *(Pillar: Security — ASI01)*
- **REQ-041 (S).** The active plan **shall** be injected into the system prompt (via the `agent:assemble_prompt` hook, matching the current module) respecting SPEC-029's append-only `transform_context` contract. *(Pillar: Simplicity)*
- **REQ-042 (M).** The planner **shall** expose plan-oriented LLM tools (create/decompose a plan, report plan status, advance/replan) and **shall** remove the four to-do CRUD tools and `tasks.json` in the same change (no-legacy). *(Pillar: Simplicity)*
- **REQ-043 (C).** The planner **could** surface plan/step state to arcui via the existing UIBridgeSink audit fan-out (no new push wire). *(Pillar: Modularity)*

### F. Tier & compatibility

- **REQ-050 (M).** Planning **shall** function at every tier; federal **shall** add no new planner-specific gate (steps are gated by the existing pillars). Tier remains stringency metadata (ADR-019). *(Pillar: Security)*
- **REQ-051 (M).** The planner **shall** add **zero** LOC to `arcagent/core` and live entirely under `arcagent/modules/planning/`. *(Pillar: Simplicity — core budget)*

---

## Threat mapping

| Threat | Requirement(s) | Mitigation in this spec |
|---|---|---|
| **ASI01 — Agent goal hijack** | REQ-003, REQ-040 | Plan binds to `parent_goal_hash` of immutable identity goals; planner cannot write `identity.md`/`policy.md` (SPEC-035 denylist); decomposition targeting protected paths is rejected. The plan is subordinate, never overriding. |
| **LLM09 — Misinformation** | REQ-005, REQ-030, REQ-033 | Steps must be grounded in known capabilities; replan is fed **real** executed results (observations), not hallucinated expectations; ungrounded plans are rejected, not persisted. |
| **LLM06 / ASI02 — Excessive agency / tool misuse** | REQ-004, REQ-021, REQ-023 | `tool_hint` is advisory; every step tool call still goes through the SPEC-034 policy pipeline (first-DENY-wins, fail-closed); a DENY is a captured step failure, not a bypass. |
| **LLM10 / ASI08 — Unbounded consumption / cascade** | REQ-022, REQ-031 | Step runs bounded by the SPEC-038 budget breaker; replan bounded by `max_replans` with a structured terminator; plan budget rolls up to the run ceiling. |
| **ASI06 — Plan/context poisoning** | REQ-011, REQ-013, REQ-014 | Plan state is checkpointed, integrity-checked on read, and every mutation is audited to a tamper-evident chain. |
| **AU / CP (NIST)** | REQ-012, REQ-013 | Audited transition trail (AU-2/9/10); resume-from-checkpoint (CP-10). |

## Acceptance criteria (pillar-tied)

- **AC-1 (Simplicity, REQ-001/010/042):** A goal yields one `Plan` (DAG, no cycles) persisted to one `plans/<id>.json`; the old `tasks.json` + 4 CRUD tools are gone; no `arcagent/core` file changed.
- **AC-2 (Modularity, REQ-002/020):** Decomposition/replan inference happens only through arcllm; step execution happens only through the injected `StepExecutor` seam (arcrun `run`); a fake `StepExecutor` in tests proves the planner never dispatches a tool itself.
- **AC-3 (Security — goal integrity, REQ-003/040):** A decomposition whose step targets `identity.md`/`policy.md`, or contradicts `parent_goal_hash`, is rejected with an audited reason; no protected file is written.
- **AC-4 (Security — gated steps, REQ-021/023):** A step whose tool call the policy pipeline DENYs is marked `failed` with the DENY reason and triggers a bounded replan — proven end-to-end through the real `ToolRegistry`+`PolicyPipeline`, not a mock.
- **AC-5 (Security — bounded, REQ-022/031):** A plan configured with `max_replans=N` that keeps failing terminates as `failed` with a structured terminator after exactly N replans; a step run that hits the token/cost ceiling is marked `failed` (budget breach), not retried forever.
- **AC-6 (Scalability — durable/resume, REQ-011/012):** Killing the process mid-plan and re-opening the session resumes the plan, skips `succeeded` steps, and re-derives the ready set from `depends_on` — verified by reloading `plans/<id>.json` and replaying.
- **AC-7 (Security — audit, REQ-013):** `plan.created`, `plan.step.started/succeeded/failed`, `plan.replanned`, and `plan.completed/failed` events appear on the arctrust sink with a verifiable chain (SignedChainSink where configured).
- **AC-8 (Tier, REQ-050/051):** The same plan executes at personal/enterprise/federal with no planner-specific gate difference; core LOC unchanged (federal fail-closed comes only from the existing pillars).
