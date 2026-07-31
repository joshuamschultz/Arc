# PLAN — SPEC-040 Real planner (Plan-Execute)

**Status:** PENDING
**Method:** TDD (Red → Green → Refactor). Each task writes the failing test first, then the minimal implementation. Every task is scoped to **one module** and traces REQ → component → task.
**Scope guard:** all work under `packages/arcagent/src/arcagent/modules/planning/` and `packages/arcagent/tests/unit/modules/planning/` (+ one integration file). **Zero** `arcagent/core` edits. No arcrun/arcllm edits (SPEC-040 consumes their existing seams; the arcrun strategy is SPEC-043).

Legend: **[REQ-…]** requirement covered · **(comp)** component in SDD §3.

---

## Phase 0 — Scaffolding & teardown

- [ ] **T-001** Delete the to-do notebook (no-legacy). Remove `tasks.json` handling, the 4 CRUD tools, and `modules/planning/tools.py`; strip the legacy `PlanningModule` task methods from `__init__.py`. **Test:** existing planning tests that assert the CRUD tools are updated/removed; a test asserts `task_create`/`task_list`/`task_update`/`task_complete` are no longer registered. **[REQ-042]**
- [ ] **T-002** Repurpose `_runtime.py`: `_State` gains `plans_dir`, `model`, `step_executor`, `audit_sink`; drop `tasks_path`. **Test:** `configure()` sets `plans_dir = workspace/plans`; `state()` raises if unconfigured. **(_runtime)** **[REQ-051]**

## Phase 1 — Plan data model (`models.py`)

- [ ] **T-010** `PlanStep` + `Plan` Pydantic models with the fields in SDD §3.1. **Test:** round-trip serialize/deserialize; defaults correct. **(models)** **[REQ-003]**
- [ ] **T-011** `Plan.validate_dag()` rejects cycles and dangling `depends_on`. **Test:** cyclic plan raises; dangling edge raises; valid DAG passes. **(models)** **[REQ-001]**
- [ ] **T-012** `Plan.ready_steps()` returns `PENDING` steps whose `depends_on` are all `SUCCEEDED`, in a valid topological order. **Test:** diamond DAG yields correct frontier as steps complete. **(models)** **[REQ-012, REQ-024]**
- [ ] **T-013** `Plan.is_terminal()` / status transitions helpers. **Test:** all-succeeded → completed-eligible; any-failed-and-no-ready → failed-eligible. **(models)** **[REQ-030]**

## Phase 2 — Durable state (`store.py`)

- [ ] **T-020** Atomic `save(plan)` / `load(plan_id)` to `<workspace>/plans/<id>.json` (temp+rename), mirroring SessionManager. **Test:** save then load returns an equal `Plan`; write is atomic (no partial file on simulated interrupt). **(store)** **[REQ-010]**
- [ ] **T-021** `active_plan(session_key)` resolves the current `ACTIVE` plan; integrity-check rejects malformed/truncated JSON. **Test:** corrupt file → raises (not silent execute); no active plan → None. **(store)** **[REQ-012, REQ-014]**
- [ ] **T-022** Every `save` emits an `AuditEvent` via the injected arctrust sink (single emission point). **Test:** a fake sink captures `plan.created`/`plan.step.*`/`plan.replanned`/`plan.completed` with correct fields; chain verifies with SignedChainSink. **(store)** **[REQ-013]**

## Phase 3 — Decomposition & replan (`decomposer.py`)

- [ ] **T-030** `decompose(goal, model)` calls **arcllm** with a structured-output schema and validates the result into a `Plan` (DAG). **Test:** a fake model returning a fixed plan JSON yields a valid `Plan`; no provider adapter is imported (asserted by import check). **(decomposer)** **[REQ-001, REQ-002]**
- [ ] **T-031** Grounding gate: reject a plan referencing no known capability, or targeting `identity.md`/`policy.md`, or contradicting `parent_goal_hash`. **Test:** ungrounded/protected-path/goal-drift plans raise and are **not** persisted. **(decomposer)** **[REQ-005, REQ-040]**
- [ ] **T-032** `replan(plan, failure, model)` preserves the `SUCCEEDED` prefix, feeds real results + failure reason to arcllm, bumps `version`, emits `plan.replanned`. **Test:** completed steps survive; version += 1; remaining steps replaced. **(decomposer)** **[REQ-030, REQ-032]**

## Phase 4 — Execution seam (`executor.py`)

- [ ] **T-040** Define `StepExecutor` Protocol + `StepOutcome`. **Test:** a fake executor satisfies the Protocol; type-checks under mypy strict. **(executor)** **[REQ-020]**
- [ ] **T-041** `ArcRunStepExecutor.run_step` drives **one bounded arcrun run** per step through the existing agent run seam; the step gets its slice of `Plan.budget` as run `max_tokens`/`max_cost`. **Test:** step description becomes the run task; run ceiling set from budget; the planner never calls a tool directly (fake seam records calls). **(executor)** **[REQ-022, REQ-024]**
- [ ] **T-042** Map `LoopResult`/`completion_payload` → `StepOutcome`: policy DENY, budget breach, or tool error → `FAILED` with `failure_reason`; clean end → `SUCCEEDED` with `result`. **Test:** each terminator maps to the right outcome. **(executor)** **[REQ-023]**

## Phase 5 — Orchestrator (`orchestrator.py`)

- [ ] **T-050** DAG-walk control loop: decompose → checkpoint → walk ready steps → checkpoint each transition → finalize. **Test:** a 3-step linear plan runs to `COMPLETED` with checkpoints after every transition. **(orchestrator)** **[REQ-011]**
- [ ] **T-051** Failure path: a `FAILED` step triggers a bounded replan; `max_replans` exhaustion terminates the plan `FAILED` with a structured terminator (arcrun `make_budget_breach_args` shape). **Test:** N consecutive failures → exactly N replans then `FAILED`; no infinite loop. **(orchestrator)** **[REQ-031]**
- [ ] **T-052** Resume: on startup with an `ACTIVE` plan, re-enter the walk and skip `SUCCEEDED` steps (frontier re-derived from `depends_on`). **Test:** kill mid-plan, reload, resume completes without re-running succeeded steps. **(orchestrator)** **[REQ-012]**
- [ ] **T-053** (optional, gated by OQ approval) Divergence trigger: a `SUCCEEDED` step whose result fails a re-evaluation predicate triggers a replan. **Test:** diverged result → replan of remainder. **(orchestrator)** **[REQ-033]**

## Phase 6 — LLM surface & hooks (`capabilities.py`)

- [ ] **T-060** Tools: `plan_create(goal)`, `plan_status()`, `plan_replan(reason)`, `plan_abandon(reason)` with correct classifications. **Test:** `plan_create` produces + starts a plan; `plan_status` is `read_only` and returns current step statuses. **(capabilities)** **[REQ-042]**
- [ ] **T-061** `agent:assemble_prompt` hook (prio 60) injects the active plan; honors append-only `transform_context`. **Test:** active plan appears in `sections["planning"]`; no plan → no section. **(capabilities)** **[REQ-041]**
- [ ] **T-062** `agent:shutdown` hook checkpoints the active plan. **Test:** shutdown flushes plan to disk. **(capabilities)** **[REQ-042]**

## Phase 7 — Integration & security (one integration file)

- [ ] **T-070** **AC-4** — planner + **real** `ToolRegistry`+`PolicyPipeline`: a step whose tool the policy DENYs → `FAILED` with DENY reason → bounded replan. End-to-end, no policy mock. **[REQ-021, REQ-023]**
- [ ] **T-071** **AC-2** — fake arcllm (fixed decompositions) + fake `StepExecutor`: prove the planner performs no direct tool dispatch and makes no provider call. **[REQ-002, REQ-020]**
- [ ] **T-072** **AC-6** — kill-and-resume across a simulated restart skips `SUCCEEDED` steps; plan file is the sole resume record. **[REQ-012]**
- [ ] **T-073** **AC-5** — budget-breach step marked `FAILED` (not retried); `max_replans` plan terminates `FAILED`. **[REQ-022, REQ-031]**
- [ ] **T-074** **AC-3** — decomposition targeting `identity.md`/`policy.md` or contradicting `parent_goal_hash` rejected with audited reason; no protected file written. **[REQ-040]**
- [ ] **T-075** **AC-7 / AC-8** — audit events present + chain verifies; same plan executes at personal/enterprise/federal with no planner-specific gate; **core LOC unchanged**. **[REQ-013, REQ-050, REQ-051]**

## Phase 8 — Gates

- [ ] **T-080** `ruff check` + `ruff format` clean; `mypy --strict` clean on the module (Protocol + Pydantic types). Fix any pre-existing planning-module lint/type debt seen in-flight (leave-it-correct rule).
- [ ] **T-081** Coverage ≥ 80% line / ≥ 75% branch on the module; verify `arcagent/core` NCLOC unchanged (no core growth); update `modules/planning` docstrings/README to reality (no aspirational claims).

---

## Traceability matrix (REQ → task)

| REQ | Tasks |
|---|---|
| REQ-001 | T-011, T-030 |
| REQ-002 | T-030, T-071 |
| REQ-003 | T-010 |
| REQ-004 | T-041 (tool_hint advisory) |
| REQ-005 | T-031 |
| REQ-010 | T-020 |
| REQ-011 | T-050 |
| REQ-012 | T-012, T-021, T-052, T-072 |
| REQ-013 | T-022, T-075 |
| REQ-014 | T-021 |
| REQ-020 | T-040, T-071 |
| REQ-021 | T-070 |
| REQ-022 | T-041, T-073 |
| REQ-023 | T-042, T-070 |
| REQ-024 | T-012, T-041 |
| REQ-025 | *deferred → SPEC-043* |
| REQ-030 | T-013, T-032 |
| REQ-031 | T-051, T-073 |
| REQ-032 | T-032 |
| REQ-033 | T-053 (gated by OQ) |
| REQ-040 | T-031, T-074 |
| REQ-041 | T-061 |
| REQ-042 | T-001, T-060, T-062 |
| REQ-043 | *could — via existing audit fan-out, no task unless requested* |
| REQ-050 | T-075 |
| REQ-051 | T-002, T-075, T-081 |

## Notes for the implementer
- **Do not** add an arcrun strategy or wire `parallel_dispatch` here — that is SPEC-043. Keep the `StepExecutor` Protocol stable so SPEC-043 swaps the implementation only.
- **Do not** import a provider adapter in the module; decomposition/replan go through the injected arcllm handle.
- **Do not** invent a store; use the workspace-JSONL/atomic-write pattern from `SessionManager` and the arctrust audit sink already handed to audit-bearing modules.
- Resolve OQ-1/OQ-2/OQ-3/OQ-4 with the product owner before T-050 (control pattern) and T-053 (divergence trigger); T-001/T-004 assume OQ-4 = full replacement.
