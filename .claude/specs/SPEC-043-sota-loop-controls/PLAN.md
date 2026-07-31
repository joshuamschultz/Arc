# SPEC-043 — PLAN

**Status:** PENDING
TDD throughout: write the failing test first, watch it fail for the right reason, implement the minimal
change, verify green, refactor. Each task is scoped to **one module**. Every task cites its REQ →
component. Concurrency-race tests are **interleaving-forced** with an `asyncio.Barrier`/`Event`
(per `feedback_concurrency_tests_must_interleave`) — never an instant mock (an instant mock makes
`asyncio.gather` run tasks sequentially and the race never manifests).

**Quality gates per task:** `ruff check` 0 · `mypy --strict` 0 · coverage ≥ 80% (core ≥ 90%) ·
boundary import test green · audit event emitted for every new op.

**Phase order rationale:** wire the shared concurrency mechanism (D) and its safety guards (the hard
part) before the consumers that depend on them (F). The streaming cleanup (E) is a standalone deletion
(streaming is cut — not a feature). Each phase is an approval boundary.

---

## Phase 0 — Foundations & guardrails

- **T-000** Boundary import test (arcagent test): assert `arcrun` imports none of `arcagent`/`arctrust`/`arcteam`; assert the SPEC-040 `Plan` model has no new fields beyond `reserved_*` after this spec. *(AC-M1/M2)*
- **T-001** Confirm-red baseline: a test that dispatches two tool calls concurrently through a real `wrapped_execute` and asserts the trifecta union is respected — **must fail today** (proves the race exists before we fix it). *(REQ-032, red baseline)*

## Phase A — Checkpoint / resume (crash recovery) + versioned provenance emission (arcrun emits, arcagent persists)

- **T-A1** *(arcrun/checkpoint.py, REQ-001)* Test: `to_checkpoint(state)` captures turn/tokens/cost/messages/completion/tool_names; round-trips through `from_checkpoint` to an equal `RunState`. Implement `LoopCheckpoint` + both functions.
- **T-A2** *(arcrun/checkpoint.py, REQ-004)* Test: `from_checkpoint` with a registry whose tool-name set ≠ `cp.tool_names` raises/refuses (fail closed). Implement the equality guard.
- **T-A3** *(arcrun/state.py + strategies/react.py, REQ-001/002)* Test: with an `on_checkpoint` hook set, exactly one checkpoint is emitted per turn boundary; with no hook, zero overhead (hook never called). Implement the field + one call site in `_end_turn`.
- **T-A4** *(arcrun/loop.py, REQ-003)* Test: `run(..., resume_from=cp)` re-enters at `cp.turn_count`, does not re-execute completed turns, and produces a `LoopResult` continuing the run. Implement `resume_from` branch in `_build_state`/`run`.
- **T-A5** *(arcagent/core/session_internal/manager.py, REQ-005)* Test: `persist_checkpoint(cp)` appends one JSONL line under the existing lock; re-reads to an equal checkpoint. Implement.
- **T-A6** *(arcagent wiring, REQ-006/061)* Test: at ent/fed the checkpoint record is emitted to the arcstore WORM spool + an audit event fires; at personal it stays local JSONL. Implement the tier-conditional sink + `on_checkpoint` wiring in agent build.
- **T-A7** *(arcrun executor/events, REQ-007)* Test: `tool.start`/`tool.end`/`llm.call` events carry the tool version and skill version that produced the step (assert both present on the emitted event); raw in/out still ride only under `store_raw_bodies`. Implement version stamping from the registered `Tool`/skill metadata. (Replay *experience* is SPEC-044 — not in this plan; this task only makes the trace replay-ready.)

## Phase B — HITL approval pause (arcrun pauses, SPEC-035 decides)

- **T-B1** *(arcrun/state.py + strategies/react.py, REQ-010/010a/013)* Test: a **proposed** tool call whose `name`/`capability_tags`/`classification` matches the config approval set (tool-based trigger, per LangGraph/MS-Agent-Framework) causes the loop to `await` the provider before dispatch; a non-matching call dispatches without pause; reuses existing suspension (no new queue). Implement `approval_provider` + the `needs_approval(tc)` predicate over the config set + the await point in `_execute_tool_calls`.
- **T-B2** *(arcrun/strategies/react.py, REQ-011)* Test: provider returns a grant → call dispatched with grant attached; provider returns `None` → call NOT dispatched, structured "approval required" tool_result. Implement both branches (fail closed).
- **T-B3** *(arcrun boundary, REQ-012)* Test: arcrun references no token type and never verifies a grant (grant is opaque passthrough). Assert via import test + type check.
- **T-B4** *(arcagent wiring, REQ-012)* Test: the injected provider delegates to SPEC-035 `HumanGate.request` and returns its `ApprovalGrant | None`; agent cannot self-approve (operator-signed). Implement the thin adapter.
- **T-B4a** *(arcagent tier resolution, REQ-010b/010c/060)* Test: tier→approval-set resolution — **personal** yields an empty set (no pause on any tool unless config opts in); **enterprise** marks every tool approval-required; **federal** marks every tool AND every skill-backed capability approval-required. Assert a skill-backed `Tool` pauses at federal but not at enterprise; assert arcrun's `needs_approval` stays a pure membership test (no tier logic in the loop). Implement the resolver + skill-backed marking in `to_arcrun_tools()`.
- **T-B5** *(arcagent integration, REQ-014)* Test: a run paused for approval is checkpointed; after simulated restart + resume, the approved call proceeds. Compose A+B.

## Phase C — Generalized circuit breaker (one hook point)

- **T-C1** *(arcrun/builtins/task_complete.py, REQ-023)* Test: `make_budget_breach_args` accepts `runaway_loop` and `error_cascade` with summaries; `BudgetBreachReason` widened. Implement.
- **T-C2** *(arcrun/strategies/react.py, REQ-022)* Test: `max_turns` trips via the top-of-turn `check_breaker` (not the tail); the tail branch is deleted; existing max_turns behavior preserved. Refactor into `check_breaker`.
- **T-C3** *(arcrun/strategies/react.py, REQ-020/025)* Test: same tool-call signature repeated ≥ `max_repeat` trips `runaway_loop`; a `parallel_safe` batch of *distinct* signatures does NOT trip (counts as progress). Implement the signature ring + detector.
- **T-C4** *(arcrun/strategies/react.py, REQ-021)* Test: `max_consecutive_errors` consecutive tool failures trip `error_cascade`; a success resets the counter. Implement.
- **T-C5** *(arcrun/state.py + arcagent config, REQ-024/060)* Test: federal breaker thresholds are non-relaxable floors — an agent-supplied looser value cannot exceed the config floor; personal may relax/disable. Implement threshold resolution.

## Phase D — Wire `parallel_dispatch` + the ledger admission lock (THE hard part)

- **T-D1** *(arcrun/types.py + parallel_dispatch.py, REQ-034)* Test: `Tool.classification` defaults `state_modifying`; `BatchClassifier` reads it via the registry shim; unknown/unset → sequential. Implement the field + shim.
- **T-D2** *(arcrun/strategies/react.py, REQ-030/033/035)* Test: a turn's tool calls dispatch through `dispatch_batch` (submission order preserved, partial failure isolated, semaphore-bounded); the ad-hoc `parallel_safe`/gather path is **gone** (grep: `parallel_dispatch` now has a production importer; `_execute_tool_calls` no longer calls `asyncio.gather` directly). Replace the path.
- **T-D3** *(arcagent capability_ledger.py + tool_registry.py, REQ-032 — INTERLEAVING-FORCED)* Test: two tool calls, each individually allowed, whose capability-leg **union** completes the lethal trifecta, are dispatched concurrently; an `asyncio.Barrier(2)` forces both into `wrapped_execute` before either records; assert the **second is DENIED** (union seen) — i.e. they are NOT both allowed. Verify the test FAILS without the lock (ties back to T-001) and PASSES with it. Implement `SessionCapabilityLedger.admission_lock` + wrap `snapshot→evaluate→record` in `tool_registry`; keep the HumanGate approval await *outside* the lock.
- **T-D4** *(arcagent tool_registry.py, REQ-031)* Test: every call in a concurrent batch still passes the full PolicyPipeline (a DENY on one does not leak an ALLOW to another); set `Tool.classification` in `to_arcrun_tools()`.
- **T-D5** *(arcagent, REQ-032 regression)* Test: admission lock is held only for the O(1) decision — a slow `tool.execute` in one branch does NOT block another branch's execution (assert overlap via timestamps/Event). Guards against over-locking.

## Phase E — Streaming cleanup (streaming CUT — this is deletion, not a feature)

- **T-E1** *(arcrun/streams.py, REQ-040/041/042)* Test: `run_stream` no longer emits fabricated per-word `TokenEvent`s (assert the synthetic word-split at `streams.py:339-346` is gone via a test that a multi-word final content yields the block/structured events, not N word tokens); `TurnEndEvent` + `collect()` `RunResult` are unchanged (existing consumer tests stay green); the react loop still calls `model.invoke` (grep: no `invoke_stream` in the loop). Delete the word-split.
- **T-E2** *(arcrun/streams.py, REQ-043)* Test: `stream_llm_response` is unchanged and still streams real deltas for out-of-loop UX (its existing tests stay green); it drives no loop and touches no gate.

## Phase F — Plan-Execute strategy + concurrent branches (behind the SPEC-040 seam)

- **T-F1** *(arcrun/strategies/plan_execute.py, REQ-050/051/056)* Test: `PlanExecuteStrategy` runs a list of independent items concurrently via `dispatch_batch` and returns per-item outcomes; it never receives/reads `depends_on`. Implement + register in `STRATEGIES`.
- **T-F2** *(arcagent/modules/planning/models.py, REQ-053/054)* Test: `available_budget()` = remaining − outstanding reservations; adding a reservation shrinks it; `Plan` DAG methods unchanged (`validate_dag`/`ready_steps` identical). Implement `reserved_tokens`/`reserved_cost` + `available_budget`.
- **T-F3** *(arcagent/modules/planning/orchestrator.py, REQ-053 — INTERLEAVING-FORCED)* Test: a plan with N independent ready branches and a `Plan.budget` that fits only < N full caps; an `asyncio.Barrier(N)` forces all branches to reserve concurrently; assert `Σ(spend) ≤ Plan.budget` and the over-budget branch(es) get `None`/deferred (no N-way overspend). Verify it FAILS without reserve-then-settle. Implement `_reserve`/`_settle` under `_budget_lock` + concurrent frontier dispatch.
- **T-F4** *(arcagent/modules/planning/executor.py, REQ-054/055)* Test: `ConcurrentStepExecutor` satisfies `StepExecutor`; one failing branch yields a `FAILED` StepOutcome and does NOT crash siblings or the orchestrator; the interim `ArcRunStepExecutor` is swapped by injection with **zero `Plan` model change**. Implement.
- **T-F5** *(arcagent integration, REQ-052/055)* Test: end-to-end — a two-branch plan executes both branches concurrently, each branch's tools pass SPEC-034 policy + SPEC-038 budget; a policy DENY on one branch triggers replan, the other completes.

## Phase G — Cross-cutting verification

- **T-G1** *(all, REQ-061)* Test: every new op (checkpoint write, approval pause/grant/deny, each breaker reason, concurrent dispatch, branch outcome) emits an audit event through the existing sink. Assert presence + shape.
- **T-G2** *(boundary/regression)* Re-run T-000 + the full arcrun/arcagent suites; assert the three deletions landed (ad-hoc gather, synthetic word-split, tail max_turns) via grep-in-test; assert `parallel_dispatch.py` has exactly one production importer (AC-S1).
- **T-G3** *(perf smoke, REQ-Sc2)* Assert checkpoint is O(1)/turn and no-hook runs carry zero checkpoint overhead; concurrent dispatch + branches are semaphore-bounded.

---

## Traceability matrix (REQ → task)

| REQ | Task(s) |
|---|---|
| 001 | T-A1, T-A3 | 002 | T-A3, T-A4 | 003 | T-A4 | 004 | T-A2 | 005 | T-A5 | 006 | T-A6 | 007 | T-A7 |
| 010 | T-B1 | 010a | T-B1 | 010b | T-B4a | 010c | T-B4a | 011 | T-B2 | 012 | T-B3, T-B4 | 013 | T-B1 | 014 | T-B5 |
| 020 | T-C3 | 021 | T-C4 | 022 | T-C2 | 023 | T-C1 | 024 | T-C5 | 025 | T-C3 |
| 030 | T-D2 | 031 | T-D4 | 032 | T-001, T-D3, T-D5 | 033 | T-D2 | 034 | T-D1, T-D4 | 035 | T-D2 |
| 040 | T-E1 | 041 | T-E1 | 042 | T-E1 | 043 | T-E2 |
| 050 | T-F1 | 051 | T-F1 | 052 | T-F5 | 053 | T-F2, T-F3 | 054 | T-F2, T-F4 | 055 | T-F4, T-F5 | 056 | T-F1 |
| 060 | T-C5 | 061 | T-A6, T-G1 |

## Definition of done

- All tasks green; `ruff`/`mypy --strict` clean across arcrun + arcagent; coverage gates met.
- The two interleaving-forced tests (T-D3, T-F3) fail on the pre-fix code and pass after — proving the guard, not the mock.
- The three dead-code deletions landed; `parallel_dispatch.py` has one production caller; the `Plan` model diff is only `reserved_*`.
- Boundary import test green (arcrun imports no sibling; no token type in arcrun).
- **All four OQs resolved:** OQ-1 (HITL) — tool-based trigger, tier ladder (personal free-run / enterprise all-tools / federal all-skills+tools); OQ-2 — Option A (orchestrator gathers over unchanged `run_step`; matches Claude Code's partition model); OQ-3 (streaming) — cut (Phase E deletion-only); OQ-4 — resume = crash recovery only, and **replay is split into its own SPEC-044** (SPEC-043 emits versioned provenance via T-A7; the step-through/re-run experience is SPEC-044 over arcstore/arcui/arctrust).
