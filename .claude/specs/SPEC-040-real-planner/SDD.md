# SDD — SPEC-040 Real planner (Plan-Execute)

## 1. Design overview

The planner is a **module** (`arcagent/modules/planning/`) that owns a plan artifact and its lifecycle, while **borrowing** every heavy capability from existing infrastructure:

```
                    goal
                      │
        ┌─────────────▼──────────────┐
        │  PlanningModule (arcagent)  │   ← owns Plan model, persistence, replan
        │                            │
        │  1. decompose(goal) ───────┼──► arcllm  (structured plan inference)
        │  2. persist(Plan) ─────────┼──► workspace JSONL + arctrust audit
        │  3. for each ready step:   │
        │       StepExecutor.run ────┼──► arcrun.run  (loop; tools → policy+budget)
        │  4. on failure → replan ───┼──► arcllm  (revise remaining steps)
        │  5. checkpoint + audit ────┼──► workspace JSONL + arctrust audit
        └────────────────────────────┘
```

Nothing in the diagram is new machinery except the `PlanningModule` orchestration and the `Plan`/`PlanStep` models. Inference is arcllm, the loop is arcrun, persistence and audit are existing infra. Zero `arcagent/core` LOC.

## 2. Current state (what SPEC-040 replaces)

- `modules/planning/__init__.py` — legacy `PlanningModule` (Module-Bus class form).
- `modules/planning/capabilities.py` — SPEC-021 decorator form: 4 tools (`task_create/list/update/complete`) + 2 hooks (`agent:assemble_prompt` prio 60 injects pending tasks; `agent:shutdown`).
- `modules/planning/tools.py` — the older native-tool form of the same 4 tools.
- `modules/planning/_runtime.py` — module-level `_State(workspace, tasks_path, agent_name)` configured once at startup.
- **Data model today:** `tasks.json` = flat `list[{id, description, status, result?}]`; `status` a free-text string; **no** dependencies, **no** execution, **no** audit, **no** resume semantics (a plain `write_text`).

Per the no-legacy rule and OQ-4, SPEC-040 **replaces** this: `tasks.json`, the 4 CRUD tools, and `tools.py` (redundant native form) are deleted; `_runtime.py` is repurposed to point at the plans directory.

## 3. Components (all in `arcagent/modules/planning/`)

### 3.1 `models.py` — Plan data model (new)
Pydantic 2.x models (data boundary → Pydantic per CLAUDE.md).

```python
class StepStatus(StrEnum): PENDING, READY, RUNNING, SUCCEEDED, FAILED, SKIPPED

class PlanStep(BaseModel):
    step_id: str
    description: str
    depends_on: list[str] = []          # DAG edges (step_ids)
    tool_hint: str | None = None        # ADVISORY only (REQ-004)
    status: StepStatus = PENDING
    result: str | None = None           # observation captured on success
    failure_reason: str | None = None   # DENY / budget / tool error (REQ-023)
    attempts: int = 0
    checkpoint: dict[str, Any] = {}      # opaque resume hint (REQ-011)

class Plan(BaseModel):
    plan_id: str
    goal: str
    goal_source_did: str                 # provenance (REQ-003)
    parent_goal_hash: str                # binds to immutable identity goals (ASI01)
    status: PlanStatus                    # DRAFT|ACTIVE|COMPLETED|FAILED|ABANDONED
    version: int = 1                      # ++ on replan (REQ-032)
    steps: list[PlanStep]
    max_replans: int
    replans_used: int = 0
    budget: PlanBudget                    # aggregate → maps to run ceilings (REQ-022)
    created_at / updated_at: datetime
```
- `validate_dag()` — reject cycles / dangling `depends_on` (REQ-001).
- `ready_steps()` — steps whose `depends_on` are all `SUCCEEDED` and self `PENDING` (REQ-012/024).
- `is_terminal()` — all steps `SUCCEEDED|SKIPPED`, or `FAILED`.

### 3.2 `decomposer.py` — goal → Plan via arcllm (new)
- `async decompose(goal, *, context, model) -> Plan` and `async replan(plan, failure, *, model) -> Plan`.
- Uses **arcllm** with a structured-output schema (the model returns a plan JSON; validated into `Plan`). **No provider adapter import, no turn-loop.**
- Grounding gate (REQ-005): validates each step against the live capability set (tool names known to the registry) and rejects a plan that references nothing runnable or that targets a protected path (REQ-040). Ungrounded → raise, do not persist.
- Replan preserves the `SUCCEEDED` prefix, feeds the model the real results + failure reason, bumps `version`.

### 3.3 `store.py` — durable plan state (new; reuses existing pattern)
- `async save(plan)` / `async load(plan_id)` / `async active_plan(session_key)` — atomic write to `<workspace>/plans/<plan_id>.json`, mirroring `SessionManager`'s JSONL/atomic-write + `open_or_resume` pattern (REQ-010/012). **No new store.**
- Integrity check on read (REQ-014): reject malformed/truncated JSON rather than execute a corrupt plan.
- Each `save` is paired with an `AuditEvent` emission (REQ-013) via the injected arctrust sink (`arctrust.audit.emit`) — JsonlSink + SignedChainSink fan-out, single emission point.

### 3.4 `executor.py` — the `StepExecutor` seam (new; interim driver)
```python
class StepExecutor(Protocol):
    async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome: ...
```
- **Interim implementation** (`ArcRunStepExecutor`) drives one bounded `arcrun` run per ready step through the **existing agent run seam** (`agent.run_collected` / an arcrun sub-run) — the step description becomes the run task, the run's `max_tokens`/`max_cost` come from the step's slice of `Plan.budget` (REQ-022). Tool calls inside the run pass through `ToolRegistry`→`PolicyPipeline` automatically (REQ-021).
- `StepOutcome` maps the run's `LoopResult`/`completion_payload` to `SUCCEEDED` (with `result`) or `FAILED` (with `failure_reason` = DENY / budget breach / tool error) (REQ-023).
- **SPEC-043 swap point:** the same Protocol is later satisfied by a native arcrun *Plan-Execute strategy* that walks the DAG with `parallel_dispatch`. The plan model and replan logic do not change — only which `StepExecutor` is injected (REQ-020/025).

### 3.5 `orchestrator.py` — Plan-Execute control loop (new)
Deterministic DAG walk (NOT an agentic loop — see boundary §5):
```
plan = decompose(goal); store.save(plan)              # REQ-001/010/013
while not plan.is_terminal():
    for step in plan.ready_steps():                   # topological (REQ-024)
        mark RUNNING; store.save(plan)                # checkpoint (REQ-011)
        outcome = await step_executor.run_step(step, plan=plan)
        apply outcome; store.save(plan)               # checkpoint + audit
        if outcome failed:
            if plan.replans_used >= plan.max_replans:  # circuit-breaker (REQ-031)
                mark plan FAILED (structured terminator); break
            plan = await decomposer.replan(plan, failure)  # REQ-030/032
            plan.replans_used += 1; store.save(plan)
    if reflection_predicate(step) diverged: replan     # REQ-033 (optional)
finalize(plan); store.save(plan)                       # plan.completed/failed audit
```
Resume (REQ-012): on startup, `store.active_plan(session_key)`; if an `ACTIVE` plan exists, re-enter the loop — `ready_steps()` naturally skips `SUCCEEDED` steps.

### 3.6 `capabilities.py` — LLM surface + hooks (rewritten)
- Tools (REQ-042): `plan_create(goal)` (decompose + start), `plan_status()` (report current plan/steps), `plan_replan(reason)` (force a bounded replan), `plan_abandon(reason)`. Classifications: `plan_status` = `read_only`; the rest = `state_modifying`.
- Hook (REQ-041): `agent:assemble_prompt` (prio 60) injects the **active plan** (goal + step statuses) — replaces the pending-tasks injection; respects the append-only `transform_context` contract.
- Hook: `agent:shutdown` — flush/checkpoint the active plan.

### 3.7 `_runtime.py` — module state (edited)
- Repurpose `_State`: `plans_dir` (replaces `tasks_path`); hold the injected `model` (arcllm handle), `step_executor`, and `audit_sink`, configured once at startup by the agent lifecycle (mirrors the policy module's `_runtime`). No new wiring in core — configured through the existing `configure_module_runtimes` signature dispatch in `agent_lifecycle.py`.

## 4. Data & control flow

1. `plan_create(goal)` tool fires in the main agent loop → orchestrator.
2. Decomposer calls arcllm → validated `Plan` (DAG, grounded) → `store.save` + `plan.created` audit.
3. Orchestrator walks ready steps; each step → `StepExecutor.run_step` → arcrun run → tools gated by policy+budget → `StepOutcome`.
4. Checkpoint after every transition; audit every transition.
5. On failure → bounded replan (arcllm) → new version persisted → resume the walk.
6. Terminal → `plan.completed`/`plan.failed` audit; final status reported through `plan_status` / prompt injection / (optional) arcui via the audit fan-out.

## 5. Concern boundary (explicit)

- **arcagent owns the plan.** `Plan`/`PlanStep` schema, decomposition **orchestration**, durable state, resume, replan logic, the plan LLM tools and prompt hook. It performs a **deterministic DAG walk** — this is plan orchestration, not an agentic loop.
- **arcrun owns execution.** The reason-act-observe loop that runs a single step to completion, per-run checkpoint primitives, the budget circuit-breaker, tool dispatch. arcrun has **no knowledge** of `Plan`, `depends_on`, or replan.
- **arcllm owns inference.** The structured model call that produces/revises a plan. The planner module never imports a provider adapter.
- **Existing infra owns persistence + audit.** Workspace JSONL (SessionManager pattern) for operational resume; arctrust sinks for the tamper-evident transition trail. No parallel store.
- **The seam:** `StepExecutor` Protocol. Today → `ArcRunStepExecutor` (one bounded arcrun run per step). SPEC-043 → native arcrun Plan-Execute strategy (parallel DAG). Same Protocol, no plan-model change. **SPEC-040 defines the seam; SPEC-043 crosses to the other side of it.**
- **The subtle line restated:** iterating a fixed DAG of steps = arcagent (deterministic, no LLM turn per iteration); the open-ended reason-act-observe *inside* one step = arcrun. Replan (an LLM revision of the remaining DAG) = arcagent orchestration calling arcllm, **not** an arcrun turn.

## 6. Durable-state design (detail)

- **Location:** `<workspace>/plans/<plan_id>.json` — one file per plan, atomic write (temp + rename), same discipline as `SessionManager._session_jsonl_path`.
- **What survives:** full `Plan` (steps, statuses, results, `version`, `replans_used`, budget consumed). A resume needs only this file — `ready_steps()` reconstructs the frontier from `depends_on` + `SUCCEEDED` set (no separate cursor).
- **Compaction (SPEC-029):** the plan lives outside the message list, so a compaction boundary cannot lose it; the prompt injection re-derives from the file each turn, honoring append-only `transform_context`.
- **Audit vs. operational:** the JSON file is the **operational** resume record (fast, mutable). The arctrust audit chain is the **compliance** record (append-only, tamper-evident) — plan transitions emit `AuditEvent`s; durability of the compliance trail is the WORM's job, not the JSON file's (matches arcstore's spool-vs-WORM split). Two records, one emission point per transition.
- **Integrity (ASI06):** malformed/truncated plan JSON is rejected on load; a plan whose `parent_goal_hash` no longer matches the identity goals is refused (goal drift = abandon, not execute).

## 7. Testing strategy (summary; full tasks in PLAN.md)

- **Unit:** DAG validation (cycles/dangling), `ready_steps` topological correctness, decomposer grounding-rejection, replan preserves `SUCCEEDED` prefix + bumps version, `max_replans` terminator, store round-trip + integrity rejection.
- **Integration:** planner + **real** `ToolRegistry`+`PolicyPipeline` (a DENY step → `FAILED` → bounded replan, AC-4); planner + arcrun run seam via a fake model (fixed decompositions) proving no direct tool dispatch (AC-2); kill-and-resume skips `SUCCEEDED` steps (AC-6); audit events present + chain verifies (AC-7).
- **Security:** decomposition targeting `identity.md`/`policy.md` rejected (AC-3); budget-breach step marked failed, not retried (AC-5); tier parity (AC-8).

---

## Research Insights (`/deepen`)

> Comparative study of the four candidate control patterns and how each maps onto Arc's module (arcagent) + loop (arcrun) + inference (arcllm) split, weighted for federal auditability. Citations are to the canonical sources.

### R-1. Plan-and-Execute (Wang et al., "Plan-and-Solve Prompting," ACL 2023, arXiv:2305.04091; LangChain `plan-and-execute` agent, 2023)
- **Pattern:** a Planner LLM emits a multi-step plan; an Executor runs steps one at a time; after execution (or failure) the plan is revised. Decouples *planning* (one heavier reasoning call) from *acting* (cheaper per-step calls).
- **Fit for Arc:** the **cleanest** map. Planner = arcagent decomposer (arcllm); Executor = arcrun run per step; "revise" = replan. Replan is native, not bolted on. Each step is an independent bounded run → each is independently policy/budget-gated and independently auditable — ideal for federal (an auditor reads a linear, versioned plan + per-step outcomes).
- **Cost:** more planner tokens than a monolithic ReAct; but predictable and cap-able. **Chosen as the control pattern.**

### R-2. LLMCompiler (Kim et al., "An LLM Compiler for Parallel Function Calling," ICML 2024, arXiv:2312.04511)
- **Pattern:** a Planner emits a **DAG** of tasks; a Task-Fetching Unit dispatches independent tasks **in parallel** with variable substitution across dependencies; a Joiner decides done-or-replan. ~3.7x latency / ~6.7x cost wins on parallelizable workloads vs. sequential ReAct.
- **Fit for Arc:** the **DAG representation** is worth adopting now (our `depends_on` edges are exactly LLMCompiler's dependency graph). But the **parallel dispatcher + joiner** are an *execution* concern — that is arcrun's `parallel_dispatch` (currently unwired) and belongs in **SPEC-043's arcrun strategy**, not the planner. Building the parallel joiner in arcagent would duplicate arcrun and blur the boundary.
- **Decision:** adopt the DAG data model (LLMCompiler-upgradeable); **defer** parallel execution to SPEC-043 (OQ-2). The plan already expresses the parallelism SPEC-043 will exploit — zero rework.

### R-3. ReWOO (Xu et al., "ReWOO: Decoupling Reasoning from Observations," arXiv:2305.18323, 2023)
- **Pattern:** Planner writes the **entire** plan upfront with `#E` variable placeholders; Worker executes all tool calls (no LLM in the loop); Solver composes the final answer from evidence. Removes per-step LLM re-invocation → large token savings.
- **Fit for Arc:** attractive for cost, but **brittle to reality-divergence** — it assumes the upfront plan holds because it never observes mid-flight. Our core requirement is *replan when reality diverges*, which is exactly what ReWOO trades away. Federal auditability also prefers observed intermediate results over blind variable substitution.
- **Decision:** adopt **only** ReWOO's *single upfront decomposition* (one planner call, not per-step planning) — but keep **real observations** between steps and a replan trigger. This is the Plan-Execute + ReWOO hybrid: plan-once, execute-with-observation, replan-on-divergence.

### R-4. Reflexion / self-refine replanning (Shinn et al., "Reflexion," NeurIPS 2023, arXiv:2303.11366)
- **Pattern:** an agent reflects on a failed trajectory in natural language and stores the reflection to improve the next attempt.
- **Fit for Arc:** informs the **replan trigger** (REQ-030/033). On step failure we feed the model the failure reason + prior results (a lightweight reflection) to revise the remainder. Full episodic reflection memory is **SPEC-041** (close the learning loop) — SPEC-040 keeps replan stateless-per-plan (the reflection is the replan input, not a persisted lesson).
- **Decision:** minimal reflection = the failure/divergence reason handed to `replan()`; deeper reflective memory deferred to SPEC-041.

### R-5. Durable execution / checkpointed workflows (Temporal / LangGraph checkpointers, 2023–2024)
- **Pattern:** persist workflow state at each step so a crash resumes from the last checkpoint, not the start.
- **Fit for Arc:** validates REQ-010/011/012 — but we deliberately **reuse** the SessionManager JSONL pattern + arctrust audit rather than adopt an external durable-execution engine (Simplicity, no new subsystem). Our plan file is the checkpoint; `ready_steps()` is the resume cursor. NIST CP-10 (resume from checkpoint) is satisfied without new machinery.

### Synthesis for Arc
**Plan-and-Execute control + LLMCompiler DAG data model + ReWOO single-upfront-decomposition + Reflexion-lite replan input + reuse-existing durable state.** Parallel execution and native strategy live in arcrun (SPEC-043) behind the `StepExecutor` seam; reflective learning lives in bio-memory (SPEC-041). SPEC-040 stays a small, auditable arcagent module that plans, checkpoints, and replans — and drives, never re-implements, the loop.
