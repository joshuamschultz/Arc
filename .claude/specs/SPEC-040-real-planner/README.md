# SPEC-040 — Real planner (Plan-Execute)

**Feature:** Upgrade `arcagent/modules/planning/` from a to-do-list CRUD (create/list/update/complete over a flat `tasks.json`) into a **real planner**: given a goal, decompose it into a structured, dependency-aware **plan** (a DAG of steps); execute steps through Arc's existing loop; **checkpoint** every step so the plan survives restart/compaction and can resume mid-flight; and **replan** the remaining steps when a step fails or reality diverges — without restarting from scratch.

**Status:** REMOVED — 2026-08-16. `arcagent/modules/planning/` was built and tested but never enabled (`PlanningConfig.enabled` defaulted `False`; no blueprint/team config turned it on) and has been deleted in favor of arcrun's `dynamic` strategy, which supersedes it with real control flow. Salvaged pieces (operator-signed integrity sidecar, WORM audit sink, identity-goal-hijack grounding refusal, reserve-then-settle budget accounting) are noted at `packages/arcrun/src/arcrun/dynamic/SALVAGE.md` for the `dynamic` strategy to port. This document is kept as historical record only.
**Branch:** `feat/SPEC-040-real-planner` (planning only — no branch/commit created; `.claude/` is gitignored)
**Type:** Generic (module upgrade — data model + orchestration + durable state + replan loop; zero core LOC)
**Phase:** Phase 2 — SOTA + mission control
**Confidence:** High on substrate (every reused seam confirmed at file:line — arcrun `run` loop, budget breaker, ToolRegistry→PolicyPipeline, SessionManager JSONL persistence, arctrust audit sinks). Medium on pattern choice (Plan-Execute vs LLMCompiler vs ReWOO) — one open question for the product owner (OQ-1) plus a parallel-execution scope call (OQ-2).

**Depends on / references:**
- **arcrun** (execution substrate) — `run()`/`run_collected()` is the loop; the planner drives it through the existing run seam and never re-implements a turn-loop or calls providers. SPEC-040 defines the **`StepExecutor` seam**; **SPEC-043** later provides the native arcrun *Plan-Execute strategy* behind that same seam (checkpoint/resume+replay + wire `parallel_dispatch`).
- **SPEC-034** (complete policy pipeline) — every planned step's tool call still routes through `ToolRegistry` → `PolicyPipeline` (first-DENY-wins, fail-closed). The planner adds **no** bypass; a policy DENY is a step failure that triggers replan.
- **SPEC-038** (budgets + classification) — the arcrun token/cost circuit-breaker (`react.py` top-of-turn guard) bounds each step run; the plan carries an aggregate budget that maps onto the run ceiling. Classification propagates unchanged.
- **SPEC-035** (lock goals + lethal trifecta) — protected-path denylist already blocks the planner from writing `identity.md`/`policy.md`; the plan is **subordinate** to the immutable identity goals (ASI01).
- **SPEC-029** (prompt caching / discrete compaction) — durable plan state is what lets a plan survive a compaction boundary; plan injection into the prompt respects the append-only `transform_context` contract.
- **SPEC-021** (unified capability system) — the planner ships as decorator-form `capabilities.py` (tools + hooks), matching the current module.
- **ADR-019** (tier = stringency) — planning works at every tier; federal adds **no new gate** here (steps are gated by the existing four pillars).
- CLAUDE.md Four Pillars + OWASP **ASI01** (goal integrity), **LLM09** (misinformation / grounded steps), **LLM06** (excessive agency — steps stay policy-gated), **LLM10 / ASI08** (bounded replan / no runaway), **ASI06** (plan-state integrity). NIST **AU-2/AU-9/AU-10** (audited, tamper-evident plan trail), **AC-3/AC-4** (per-step authorization), **CP-10** (resume from checkpoint).

---

## One-liner

Today the "planner" is a **notebook**: four LLM tools push flat rows into `tasks.json` with a free-text `status`, and a prompt hook lists the incomplete ones. There is **no decomposition** (the model writes each row by hand), **no dependencies** (rows are unordered), **no execution** (nothing runs a task), **no checkpoint** (a plain file write, no audit, no resume semantics), and **no replan** (a failed task just sits at `in_progress`). SPEC-040 replaces the notebook with a **Plan-Execute planner**: a Pydantic `Plan` (goal + DAG of `PlanStep`s with `depends_on` edges and per-step status) that the planner **decomposes via arcllm**, **persists durably** (a checkpointed `plans/<id>.json` mirroring SessionManager's JSONL pattern **plus** an audited transition trail through arctrust sinks), **executes step-by-step through the existing arcrun run seam** (so every step's tools pass SPEC-034 policy + SPEC-038 budget), **resumes** by reloading the plan and skipping completed steps, and **replans** the remaining steps — bounded by a `max_replans` circuit-breaker — when a step fails or a step result diverges from the plan's expectation.

## Concern-boundary split (the load-bearing decision)

| Concern | Owner | What it does | What it must NOT do |
|---|---|---|---|
| **Plan** | **arcagent** (`modules/planning/`) | `Plan`/`PlanStep` schema; goal→plan decomposition (orchestrated); durable plan state + resume; replan logic; plan-oriented LLM tools + prompt injection | Implement a turn-loop; call an LLM provider directly; dispatch tools directly |
| **Execute** | **arcrun** | The reason-act-observe loop that runs a single step to completion; per-run checkpoint primitives; budget breaker; tool dispatch | Know about `Plan`, `depends_on`, or replan (that's arcagent) |
| **Infer** | **arcllm** | The model call that produces / revises a structured plan | Persist or sequence anything |
| **Persist** | **existing infra** — workspace JSONL (SessionManager pattern) + arctrust audit sinks (JsonlSink / SignedChainSink) | Durable plan checkpoint + tamper-evident transition trail | A new parallel persistence subsystem |

The subtle line: **walking a fixed DAG of steps is plan orchestration (arcagent); the open-ended reason-act-observe *inside* one step is the loop (arcrun).** SPEC-040 ships an interim in-arcagent sequential **`StepExecutor`** that issues each ready step as one bounded `arcrun` run; **SPEC-043** swaps that seam for a native arcrun Plan-Execute strategy (parallel DAG dispatch) with **no change to the plan model or replan logic**.

## Open questions for the product owner

1. **OQ-1 — planning pattern.** Recommendation: **Plan-and-Execute** as the control pattern, with the plan represented as a **DAG** (so it is LLMCompiler-upgradeable) and executed **topologically/sequentially** for now. Rationale: replan is native to Plan-Execute; a DAG already expresses parallelism for SPEC-043 to exploit; ReWOO's "plan-all-upfront, substitute variables" is token-cheap but brittle to reality-divergence — we adopt only its *single upfront decomposition* (not its no-observation execution). LLMCompiler's parallel joiner is deferred to arcrun (SPEC-043). **Confirm Plan-Execute + DAG, or prefer ReWOO's token-lean variant?**
2. **OQ-2 — parallel step execution scope.** Recommendation: **defer** parallel *execution* to SPEC-043's arcrun strategy. SPEC-040 produces a DAG that *expresses* independent branches but executes them in topological order through the interim sequential `StepExecutor`. **In scope now, or deferred?** (Deferring keeps SPEC-040 small and avoids duplicating `parallel_dispatch`, which is arcrun's to wire.)
3. **OQ-3 — ship a live driver or an inert plan?** Recommendation: **ship the working interim `StepExecutor`** so the planner is live E2E (roadmap lesson: "producers unwired" — an inert plan model would repeat that). SPEC-043 later replaces the driver behind the seam. **Confirm SPEC-040 executes plans now, rather than only producing them for SPEC-043 to run?**
4. **OQ-4 — supersede or coexist?** Recommendation: the plan **supersedes** the to-do notebook (no-legacy rule) — `tasks.json` + the 4 CRUD tools are deleted in the same change, the plan's steps subsume "tasks." **Confirm full replacement** (vs. keeping a lightweight task list alongside plans).

## Deliverables

- `PRD.md` — EARS requirements, MoSCoW, pillar-tied acceptance criteria, threat mapping (ASI01 / LLM09 / LLM06 / LLM10 / ASI06).
- `SDD.md` — components, the arcagent-plans / arcrun-executes boundary + `StepExecutor` seam, durable-state design, **Research Insights** (Plan-and-Execute, LLMCompiler, ReWOO, Reflexion/replanning).
- `PLAN.md` — TDD tasks, each scoped to one module, REQ→component→task traceable.
