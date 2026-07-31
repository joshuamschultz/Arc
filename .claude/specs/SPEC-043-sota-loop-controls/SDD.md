# SPEC-043 — SDD

**Status:** PENDING
Traces PRD REQ-001..061. Pillars: Simplicity → Modularity → Security → Scalability.
**Engines under change:** `arcrun` (checkpoint emission, generalized breaker, wired parallel dispatch,
`plan_execute` strategy, tool-based HITL pause point, fake-streaming cleanup) · `arcagent` (checkpoint persistence via
`SessionManager`; per-session admission lock; approval-provider binding to `HumanGate`; plan aggregate
budget reserve-then-settle + concurrent frontier dispatch) · `arcstore` (WORM checkpoint sink — reuse) ·
**no change** to `arctrust` policy algorithms or `arcllm`.

---

## 1. Current state (verified at file:line)

| Element | Location | State |
|---|---|---|
| `Strategy` ABC | `arcrun/strategies/__init__.py:14-39` | `__call__(model, state, sandbox, max_turns) -> LoopResult`; registry `STRATEGIES` holds `react`+`code` (`:45-50`). Clean plug point for `plan_execute`. |
| `RunState` | `arcrun/state.py:41-86` | In-memory `@dataclass`. Carries `messages`, `turn_count`, `tokens_used`, `cost_usd`, queues, `cancel_event`, `completion_payload`, `max_tokens`/`max_cost_usd`. **No serialization.** |
| SPEC-038 breaker | `arcrun/strategies/react.py:192-210` | Top-of-turn token+cost check → `make_budget_breach_args` → `_build_result`. `max_turns` handled separately at the tail (`:295-309`). |
| Ad-hoc parallel path | `arcrun/strategies/react.py:106-168` (`_execute_tool_calls`) | Uses `tool.parallel_safe` + raw `asyncio.gather`. **Does not import `parallel_dispatch`.** |
| `parallel_dispatch` | `arcrun/parallel_dispatch.py` | Full `BatchClassifier`/`ParallelDispatcher`/`SequentialDispatcher`/`dispatch_batch`. Imported **only** by `tests/test_parallel_dispatch.py`. Dead in prod. |
| `run_stream` | `arcrun/streams.py:164-388` | Awaits blocking `run()`, then word-splits `content` into synthetic `TokenEvent`s (`:339-346`) — fake streaming. **Cut:** delete the word-split; keep as run/event entry (§3.5). |
| `stream_llm_response` | `arcrun/streams.py:399-446` | Real `model.invoke_stream` deltas — single call, no loop, ignores tool calls. **Kept unchanged** as out-of-loop UX primitive. |
| `RunHandle` | `arcrun/loop.py:198-239` | `steer`/`follow_up`/`cancel`/`result`; `cancel_event` + two `asyncio.Queue`s. Interrupt primitives, no approval pause. |
| SPEC-035 `HumanGate` | `arcagent/tools/human_gate.py:63-179` | Reactive: `request(call, *, legs) -> ApprovalGrant | None`; operator-signed one-shot token; fail-closed; federal never auto-approves. |
| Trifecta ledger | `arcagent/core/session_internal/capability_ledger.py:93-134` | `snapshot`/`record`/`record_read`; keyed by session id; **plain dict, no lock**. |
| Ledger critical section | `arcagent/core/tool_registry.py:385-435` | `snapshot()` → build `PolicyContext` → `await pipeline.evaluate()` → `record()`. **Await between read and write = the race window.** |
| SPEC-040 `StepExecutor` | `arcagent/modules/planning/executor.py:48-113` | `Protocol.run_step(step, *, plan) -> StepOutcome`; interim `ArcRunStepExecutor` = one bounded `arcrun.run` per step; caps at `plan.remaining_budget()`. |
| `PlanOrchestrator` | `arcagent/modules/planning/orchestrator.py:55-71` | `while` loop picks `ready[0]` — **one step at a time**; `orchestrator.py:63` comment: "parallel dispatch is SPEC-043". Accrues `plan.tokens_spent`/`cost_spent` in `_run_one`. |
| `Plan` aggregate budget | `arcagent/modules/planning/models.py:200-218` | `budget_exhausted()`, `remaining_budget()` — computed from `tokens_spent`/`cost_spent`; **no reservation** (safe only because dispatch is sequential today). |
| Durable persistence | `arcagent/core/session_internal/manager.py` | `SessionManager`: append-only JSONL, `asyncio.Lock`, compaction. The checkpoint sink. |

---

## 2. Module boundaries (the collision-prevention contract)

### 2.1 arcrun executes concurrently; arcagent keeps its own accounting atomic
The single most important line. arcrun introduces concurrency (parallel tool dispatch, concurrent
branches). The **shared mutable state that concurrency endangers lives in arcagent** — the trifecta
ledger (`capability_ledger.py`) and the plan aggregate budget (`models.py`). Therefore the atomicity
guards (a per-session admission lock; a budget reservation) live **in arcagent, next to the state they
protect** — never in the loop. arcrun stays a "dumb but identified" executor; it does not know what a
trifecta or a plan budget is. This mirrors the existing rule that arcrun is a dumb-but-identified queue
for injections (`state.py:16-27`).

### 2.2 arcrun produces checkpoint state; arcagent persists it; arcstore is the sink
arcrun serializes *resumable loop state* and hands it to an injected hook — exactly like `on_event`
and `transform_context` today. It never opens a file. `SessionManager` persists via its existing JSONL
path; at ent/fed the record rides the `arcstore` WORM spool. No new persistence subsystem (reuses
SPEC-029/040 infra).

### 2.3 arcrun pauses; SPEC-035 HumanGate decides
The loop-level HITL is a *pause primitive* (an await-point) plus an injected `approval_provider`
callback. arcrun owns *when to pause and how to resume*; arcagent owns *who approves and with what
authority* by binding the provider to `HumanGate.request` (operator-signed `ApprovalGrant`). arcrun
never imports `arctrust.policy`, never sees a token. This is the same producer/decider split as
SPEC-035's own gate (decision = arctrust, orchestration = arcagent) pushed one level out to the loop.

### 2.4 arcagent owns the DAG; arcrun's `plan_execute` only runs independent items
`Plan.ready_steps()` (the frontier derivation), `depends_on`, and replan stay in
`PlanOrchestrator`. The new arcrun strategy receives a *flat list of independent items* and runs them
concurrently — it never learns the graph. The `Plan` model is untouched (SPEC-040 REQ-020/025 honored).

### 2.5 Tier = stringency, not gates (ADR-019)
Every control runs at every tier. Federal = non-relaxable breaker floors, mandatory HITL where
configured, WORM checkpoints. Personal relaxes (breaker advisory/off, checkpoint local JSONL). The code
path is identical; only config values differ.

---

## 3. Component design

### 3.1 Checkpoint / resume (crash recovery) + versioned provenance emission — REQ-001..007

*Resume vs replay — two different things (OQ-4).* **Resume** = crash recovery: reload the last checkpoint
and *continue*, never re-running past side effects (their results are in the transcript). **Replay** (the
observability/governance workflow — step through a captured trace, re-execute a single step from frozen
upstream inputs) is a *distinct, larger* capability, reframed as its own **SPEC-044** (spans
arcstore/arcui/arctrust). SPEC-043's only replay contribution is REQ-007 below: emit the versioned
provenance SPEC-044 will consume. This keeps the loop-controls spec simple.

**arcrun.** Add a pure serialization pair on a new `arcrun/checkpoint.py` (not on `RunState`, to keep the
dataclass dumb):

```python
@dataclass(frozen=True)
class LoopCheckpoint:
    run_id: str; parent_run_id: str; strategy_name: str
    turn_count: int; tokens_used: dict[str, int]; cost_usd: float
    messages: list[Any]                 # the transcript (already the durable session content)
    tool_names: list[str]               # frozen registry surface — verified on resume (REQ-004)
    completion_payload: dict | None; completion_tool: str | None
    max_turns: int; max_tokens: int | None; max_cost_usd: float | None

def to_checkpoint(state: RunState, *, max_turns: int) -> LoopCheckpoint: ...
def from_checkpoint(cp: LoopCheckpoint, registry: ToolRegistry, ...) -> RunState: ...
```

`RunState` gains one optional field: `on_checkpoint: Callable[[LoopCheckpoint], None] | None = None`. The
react loop calls it inside `_end_turn` (one line) so every turn boundary emits a checkpoint (REQ-001/002).
`run(..., resume_from: LoopCheckpoint | None = None)` short-circuits `_build_state`: when present,
`from_checkpoint` rebuilds `RunState`, **asserting `set(cp.tool_names) == set(registry.names())` and
failing closed on mismatch (REQ-004)**, and the loop re-enters at `cp.turn_count` (REQ-003). Because the
message list already carries all completed work, resume redoes nothing — "replay" is *deterministic
resume*, not side-effect re-execution (OQ-4).

**arcagent.** `SessionManager` gains `persist_checkpoint(cp)` writing one JSONL line through its existing
`asyncio.Lock`-guarded append (REQ-005); at ent/fed the same record is emitted to the `arcstore` WORM
spool (REQ-006). The agent wires `on_checkpoint=session_manager.persist_checkpoint` when it builds the
run. The emitted-event prefix is **not** duplicated inline — it is reconstructable from the tamper-evident
audit chain already recorded (`events.verify_chain`), so a checkpoint is small (OQ-4).

*Why not persist inside arcrun?* Boundary 2.2 — and it keeps cold-start/hot-path free when no hook is set
(REQ-Sc2).

**Versioned provenance emission (REQ-007 — the replay substrate).** arcrun already emits `llm.call` and
`tool.start`/`tool.end` events with arg/result digests (and full bodies under `store_raw_bodies`). SPEC-043
adds two fields to those events: the **tool version** and **skill version** that produced the step (arcrun
reads them off the registered `Tool`/skill metadata arcagent stamps). That is the *entire* arcrun change —
it makes the durable trace carry LLM in/out + tool in/out + which *version* of each capability ran, which
is exactly what a governance replay ("re-run this step from its stored inputs; prove which tool/skill
version produced this output") needs. **arcrun only emits**; the durable versioned trace store (arcstore,
already the arcui data source), the step-through UI, and single-step re-execution (LLM steps re-run freely;
side-effecting tool re-runs sandboxed/gated) are **SPEC-044**, not here. This respects boundary 2.2 (arcrun
emits, doesn't persist) and keeps SPEC-043 to loop controls.

### 3.2 HITL approval pause (arcrun pauses, HumanGate decides) — REQ-010..014

**Trigger (researched — SOTA is tool-based).** LangGraph's HITL middleware and Microsoft Agent
Framework's Tool-Approval policies both gate *between model intent and tool execution*: the model
proposes a call, a policy checks whether *that call* needs review, and if so the runtime interrupts
before executing. The named triggers are properties of the proposed call — side-effecting/irreversible
tools (delete, send email, purchase), sensitive-data/credential tools, config-declared risk. Arc already
carries exactly these discriminants on the call: `tool.name`, `capability_tags`, and `classification`
(`state_modifying` vs `read_only`). So `needs_approval(tc)` is a pure predicate over a **config-declared
approval set** (tool names / tags / `classification`), evaluated on the *proposed* call before dispatch —
no new taxonomy. **The default set is the tier stringency ladder (ADR-019), resolved by arcagent and
enforced by arcrun** (same split as the SPEC-038 budget floor — arcagent passes the policy, arcrun
enforces the predicate):

| Tier | `needs_approval(tc)` default |
|---|---|
| **personal** | `False` for all — free run; config MAY add specific tool names/tags. |
| **enterprise** | `True` for every **tool** call. |
| **federal** | `True` for every **skill and tool** invocation (the full effecting-capability surface). |

*Skills vs tools:* arcrun's loop sees every model-callable capability as a `Tool`; "skill-backed" is
arcagent metadata (a `capability_kind`/tag arcagent stamps in `to_arcrun_tools()`). The federal "skills
too" rule is therefore a one-line widening of arcagent's resolved set — **arcrun's predicate stays a dumb
membership test** and tier logic never enters the loop.

**arcrun.** `RunState` gains `approval_provider: Callable[[Any], Awaitable[Any]] | None` and the
config-sourced `needs_approval(tc) -> bool` predicate above. In `_execute_tool_calls`, before dispatching
a flagged call:

```python
if state.approval_provider is not None and needs_approval(tc):
    grant = await state.approval_provider(tc)      # arcrun awaits; never inspects the token
    if grant is None:
        tool_results_map[idx] = tool_result(tc.id, "operation denied: approval required"); continue
    tc = attach_grant(tc, grant)                    # opaque passthrough onto the call
```

The pause is just an `await` on the injected callback — it reuses the loop's existing suspension model
(no new queue, REQ-013). While awaited, the loop is checkpointable at the turn boundary, so an approval
that arrives post-restart still resumes the right call (REQ-014).

**arcagent.** Binds `approval_provider` to a thin adapter over SPEC-035 `HumanGate`:
`async def provider(tc): return await human_gate.request(to_toolcall(tc), legs=...)`. The grant is the
operator-signed one-shot `ApprovalGrant`; it flows back through dispatch where arctrust honors it exactly
once (existing SPEC-035 path). **arcrun mints/verifies nothing (REQ-012).**

*Relationship to SPEC-035 (explicit):* SPEC-035's gate is *reactive* — it fires only when `GlobalLayer`
DENYs a forbidden composition. SPEC-043's pause is *proactive* — it fires on a tool-based approval policy
before any deny. They **share the token and authority** (one `HumanGate`, one `ApprovalGrant` type); they
differ only in *trigger*. Net rule: **do not build a second approval type; add only the loop-level
tool-based trigger that calls the same gate.** (OQ-1 resolved: tool-based trigger with the tier ladder
above — personal free-run, enterprise all-tools, federal all-skills+tools.)

### 3.3 Generalized circuit breaker (reuse the SPEC-038 hook point) — REQ-020..025

All breaker logic moves into one `check_breaker(state) -> BudgetBreachReason | None` called at the top of
each turn (the SPEC-038 site, `react.py:192`). It folds in:

- **token/cost** (existing).
- **max_turns** — moved from the tail into the same check (REQ-022); the tail branch is deleted.
- **runaway_loop** (REQ-020/025) — `RunState` tracks a small ring of recent tool-call *signatures*
  (`sha256(name + canonical_json(args))`, reusing `executor._digest_and_size`'s canonicalization). A single
  signature repeated ≥ `max_repeat` (config) with no interleaving progress → trip. A `parallel_safe` batch
  of *distinct* signatures counts as progress (REQ-025).
- **error_cascade** (REQ-021) — `RunState.consecutive_tool_errors` incremented on each failed
  `execute_tool_call`, reset on any success; ≥ `max_consecutive_errors` → trip.

```python
class LoopBreach(StrEnum): MAX_TOKENS; MAX_COST; MAX_TURNS; RUNAWAY_LOOP; ERROR_CASCADE
# BudgetBreachReason (task_complete.py) extends to include runaway_loop, error_cascade
```

`make_budget_breach_args` gains the two new reasons + summaries (REQ-023). Thresholds arrive as config
onto `RunState` (like `max_tokens` today); federal supplies non-relaxable floors (REQ-024). O(1) per turn
— a hash compare + two int compares (Scalability). **One hook point, one terminator factory (AC-S2).**

### 3.4 Wire `parallel_dispatch` — one gated path — REQ-030..035

Delete `_execute_tool_calls`'s ad-hoc gather. Replace with `dispatch_batch`:

```python
from arcrun.parallel_dispatch import BatchClassifier, dispatch_batch
classifier = BatchClassifier(state.registry)       # registry must expose get_classification (below)
results = await dispatch_batch(
    response.tool_calls,
    runner=lambda tc: execute_tool_call(tc, state, sandbox),
    classifier=classifier,
    max_parallel=state.max_parallel,               # semaphore ceiling (REQ-035)
)
```

**Registry gap (REQ-034):** `parallel_dispatch.BatchClassifier` needs `registry.get_classification(name)`.
arcrun's `ToolRegistry` has no such method today (grep clean). Two options — pick the boundary-clean one:
the *classification is arcagent deployment knowledge* (already: `capability_ledger.TAG_TO_LEGS`, and
arcagent's `ToolRegistry.get_classification` referenced in the `parallel_dispatch` Protocol docstring).
So arcrun's `Tool` gains an optional `classification: str = "state_modifying"` (fail-closed default), set
by arcagent when it builds `arcrun.Tool`s in `to_arcrun_tools()`. `BatchClassifier` reads it via a tiny
arcrun-side registry shim. Unknown/unset → `state_modifying` → sequential (REQ-034 fail-closed).

**The gate is preserved (REQ-031):** `dispatch_batch`'s `runner` is `execute_tool_call`, which calls the
tool's `execute` — i.e. arcagent's `arcrun_execute` → `wrapped_execute` → **full PolicyPipeline**. So
concurrency does not skip policy; it runs each call's `wrapped_execute` concurrently. That is exactly why
§3.6 (atomic admission) is mandatory. Submission-order + partial-failure semantics come free from
`dispatch_batch` (REQ-033).

### 3.5 Streaming — CUT to a cleanup — REQ-040..043

**Decision: no in-loop token streaming.** For an agentic harness the deliverable is the *output*, not
progressive tokens — streaming is a UX affordance with no functional, performance, or correctness value,
and it is the only item that would edit the loop's shared model-call path (every strategy). Building it
trades surface area and a fake-vs-real dual path for pure UX. Against the Simplicity/Security pillars,
it loses. So:

- The react loop keeps calling `model.invoke(messages, tools=...)` — one clean, non-streaming call
  (REQ-042). No `invoke_stream` in the loop.
- `run_stream` **deletes** the synthetic word-split (`streams.py:339-346`) that fabricated per-word
  `TokenEvent`s from already-complete content (REQ-040). It remains the run/event entry — emitting the
  real structured events (`ToolStart`/`ToolEnd`, turn events) and the final content as one block — minus
  the misleading fake tokens.
- `TurnEndEvent` and `collect()` `RunResult` are byte-for-byte unchanged (REQ-041); every one-shot
  consumer (CLI, scheduler, gateways) keeps working — they already drain to `RunResult`.
- `stream_llm_response` (`streams.py:399-446`) — the real single-call `invoke_stream` primitive — stays
  as-is for out-of-loop UX (chat typing effect). It is isolated, drives no loop, and touches no gate
  (REQ-043). If a UX surface ever *needs* progressive tokens, it already has this primitive; the loop
  does not carry the cost.

*Net:* this section shrinks arcrun rather than growing it — the one place SPEC-043 removes code without
adding a control. (OQ-3 asks the PO only to confirm the cut.)

### 3.6 Concurrency-safe accounting — THE hard part — REQ-032, REQ-053

Two independent races, two guards, both **in arcagent next to the state** (boundary 2.1).

**(a) Trifecta ledger admission lock (REQ-032).** The window is `snapshot → await evaluate → record`
in `wrapped_execute` (`tool_registry.py:385-430`). Because asyncio is single-threaded there is no torn
write, but the `await` *interleaves* two concurrent calls so both read the pre-write union. Guard the
critical section with a **per-session `asyncio.Lock`** owned by the ledger:

```python
# capability_ledger.py — SessionCapabilityLedger gains a per-session lock map
def admission_lock(self, session_id: str) -> asyncio.Lock:
    return self._locks.setdefault(session_id, asyncio.Lock())

# tool_registry.wrapped_execute — the critical section only
async with ledger.admission_lock(session_id):
    accumulated = ledger.snapshot(session_id)
    call = ToolCall(..., session_capabilities=accumulated)
    decision = await pipeline.evaluate(call, ctx_pol)          # inside the lock
    if decision.is_deny(): call = await self._resolve_forbidden_composition(...)
    ledger.record(session_id, tool_legs)                       # atomic read-modify-write
    ledger.record_read(session_id, clearance_ctx.resource_classification)
# tool EXECUTION happens OUTSIDE the lock — concurrency preserved where it matters
```

The lock serializes only the O(1) admission decision + record; the slow part (`tool.execute`, network,
subprocess) still runs concurrently after the lock releases. So two calls whose union completes the
trifecta are evaluated in sequence: the first records its legs, the second's `snapshot` now sees the
completed forbidden union and the `GlobalLayer` denies (→ HumanGate). No lost update (AC-Sec1). The
`HumanGate.request` (which itself awaits a human) must run **outside** the admission lock to avoid
holding it for the human timeout — restructure so a composition-deny releases the lock, obtains approval,
then re-acquires to record. (Design note: keep the lock hold to microseconds; the approval await is not
under it.)

**(b) Plan aggregate budget reserve-then-settle (REQ-053).** Today `ArcRunStepExecutor.run_step` caps a
step at `plan.remaining_budget()` — safe only because steps are sequential. With concurrent branches, two
branches both read the *full* remaining budget and each may spend it → 2× overspend. Fix: a
`BudgetReservation` on the plan, guarded by a plan-level `asyncio.Lock`:

```python
# PlanOrchestrator (arcagent) — concurrent frontier dispatch
async def _reserve(self, plan, cap) -> tuple[int|None, float|None] | None:
    async with self._budget_lock:
        rem_tok, rem_cost = plan.available_budget()          # remaining minus outstanding reservations
        if (rem_tok is not None and rem_tok <= 0) or (rem_cost is not None and rem_cost <= 0):
            return None
        grant = (min(cap_tok, rem_tok), min(cap_cost, rem_cost))
        plan.reserved_tokens += grant[0] or 0; plan.reserved_cost += grant[1] or 0
        return grant
async def _settle(self, plan, grant, actual):
    async with self._budget_lock:
        plan.tokens_spent += actual.tokens; plan.cost_spent += actual.cost
        plan.reserved_tokens -= grant[0] or 0; plan.reserved_cost -= grant[1] or 0
        self._store.save(plan, action="plan.step.settled")   # checkpoint the aggregate (REQ-053 + 3.1)
```

Each branch reserves *before* launch under the lock, so the **sum of outstanding reservations + spend can
never exceed `Plan.budget`** — the (N+1)-th branch that would breach gets `None` and is deferred/failed.
On completion each branch settles actual spend and frees its unused reservation. `models.py` gains
`reserved_tokens`/`reserved_cost` + `available_budget()` (remaining − reservations); `Plan.budget` and the
DAG semantics are otherwise unchanged. The reservation is a small, additive model change — **not** a new
budget authority (arcrun still enforces each branch-run's own ceiling via its SPEC-038 breaker).

**Why a lock, not lock-free?** The critical sections are O(1) and rare (once per admission / per branch
launch); an `asyncio.Lock` is the simplest correct construct (Simplicity pillar) and, being single-loop,
carries no kernel-thread cost. Reserve-then-settle is the standard admission-control pattern (see §7).

### 3.7 Plan-Execute strategy (concurrent branches behind the seam) — REQ-050..056

**arcrun.** New `strategies/plan_execute.py`:

```python
class PlanExecuteStrategy(Strategy):
    name = "plan_execute"
    async def __call__(self, model, state, sandbox, max_turns) -> LoopResult:
        # state carries a list of INDEPENDENT ready items (opaque tasks), NOT a DAG.
        # Dispatch them concurrently through the WIRED parallel_dispatch primitive
        # (REQ-056), each item a bounded sub-run; collect per-item outcomes.
```

It reuses the §3.4 `dispatch_batch` machinery for the concurrency mechanism (one gather path, REQ-056).
Each item runs as a bounded execution gated exactly like `react` (REQ-052). It never sees `depends_on`
(REQ-051).

**arcagent.** `PlanOrchestrator.execute` changes its `while` from `ready[0]` to *dispatch the whole
`ready_steps()` frontier concurrently* via the new concurrent `StepExecutor` (backed by the strategy),
using §3.6(b) reserve-then-settle per branch. Failures are captured per-branch as `FAILED` `StepOutcome`
(REQ-055) and feed the existing replan loop. **OQ-2 resolved (Option A — caller fans out):** the
orchestrator gathers over the *unchanged* `StepExecutor.run_step` (no batch method added), so **the `Plan`
model and the Protocol are untouched (REQ-054, AC-M2)** and the planner swaps executors by injection only.
This mirrors how Claude Code drives concurrency — a partition of concurrency-safe calls into parallel
batches with per-call safety and independent per-call results (§7.1) — which is exactly Arc's
`parallel_dispatch` substrate.

---

## 4. Data flow

```
── Checkpoint ───────────────────────────────────────────────
arcrun loop  ── each turn boundary ──► on_checkpoint(LoopCheckpoint)
                                          └► SessionManager.persist_checkpoint (JSONL, lock)
                                               └► arcstore WORM spool (ent/fed)      [REQ-001..006]
run(resume_from=cp) ─► from_checkpoint (verify tool_names) ─► re-enter at cp.turn    [REQ-003/004]

── Circuit breaker (top of each turn) ───────────────────────
check_breaker(state): token? cost? turns? runaway(sig ring)? error_cascade?
    └► make_budget_breach_args(reason) ─► halt + loop.completed{reason}             [REQ-020..025]

── Concurrent tool dispatch (one turn) ──────────────────────
response.tool_calls ─► BatchClassifier(registry.get_classification): read_only & no shared path?
   parallel ─► dispatch_batch(runner=execute_tool_call, semaphore)                  [REQ-030..035]
                     each runner ─► arcagent wrapped_execute:
                        async with ledger.admission_lock(session):                  [REQ-032]
                            snapshot ─► pipeline.evaluate (Policy/Provider/Global) ─► record
                        (execute OUTSIDE the lock — concurrent)

── Plan-Execute (concurrent DAG branches) ───────────────────
PlanOrchestrator.execute: frontier = plan.ready_steps()          (DAG stays in arcagent)
   for each branch:  grant = reserve(plan, cap)  ── under budget_lock ──            [REQ-053]
   gather:  ConcurrentStepExecutor.run_step ─► arcrun plan_execute / bounded run    [REQ-050..052]
                                                   (each gated; failure isolated)   [REQ-055]
   settle(plan, grant, actual) ── under budget_lock ─► checkpoint plan aggregate
   any FAILED ─► existing replan loop (bounded by max_replans)

── HITL pause ───────────────────────────────────────────────
before flagged tool: await state.approval_provider(tc)                             [REQ-010..013]
        └► arcagent: HumanGate.request ─► operator-signed ApprovalGrant | None
   grant ─► attach to call ─► dispatch (arctrust honors once) ; None ─► fail closed [REQ-011/012]
```

---

## 5. Module / file impact

| Package | File | Change | REQ |
|---|---|---|---|
| arcrun | `checkpoint.py` (new) | `LoopCheckpoint` + `to_checkpoint`/`from_checkpoint` (tool-set verify) | 001, 003, 004 |
| arcrun | `executor.py` / `strategies/react.py` (events) | stamp **tool version + skill version** on `tool.start`/`tool.end`/`llm.call` events (raw in/out already ride `store_raw_bodies`) — replay-provenance emission only | 007 |
| arcrun | `state.py` | add `on_checkpoint`, `approval_provider`, `max_parallel`, breaker config (runaway/cascade thresholds), signature ring, `consecutive_tool_errors` | 001, 010, 020, 021, 035 |
| arcrun | `loop.py` | `run`/`run_async` gain `resume_from`; thread new params; wire `on_checkpoint` | 002, 003 |
| arcrun | `strategies/react.py` | one `check_breaker` at the hook (delete tail max_turns); replace ad-hoc gather with `dispatch_batch`; add tool-based HITL await; **loop keeps `model.invoke` (no streaming)** | 020-025, 030-035, 042, 001 |
| arcrun | `parallel_dispatch.py` | add a registry shim reading `Tool.classification`; no logic change | 030, 034 |
| arcrun | `types.py` | `Tool.classification: str = "state_modifying"` (fail-closed default) | 034 |
| arcrun | `builtins/task_complete.py` | extend `BudgetBreachReason` + summaries: `runaway_loop`, `error_cascade` | 020, 021, 023 |
| arcrun | `streams.py` | **delete** synthetic word-split; keep `run_stream` as run/event entry + final content; `TurnEndEvent`/`collect` unchanged; `stream_llm_response` untouched | 040, 041, 043 |
| arcrun | `strategies/plan_execute.py` (new) | `PlanExecuteStrategy` — concurrent independent-item batch via `dispatch_batch`; register in `STRATEGIES` | 050-052, 056 |
| arcagent | `core/session_internal/capability_ledger.py` | per-session `admission_lock`; keep dict record atomic under it | 032 |
| arcagent | `core/tool_registry.py` | wrap `snapshot→evaluate→record` in `admission_lock`; approval await outside it; set `Tool.classification` in `to_arcrun_tools()` | 032, 034 |
| arcagent | `core/session_internal/manager.py` | `persist_checkpoint` (JSONL + WORM at ent/fed) | 005, 006 |
| arcagent | agent wiring (`agent.py`/lifecycle) | inject `on_checkpoint`, `approval_provider`→HumanGate; resolve tier→approval set (personal empty / enterprise all-tools / federal all-skills+tools); mark skill-backed `Tool`s; breaker floors from config | 005, 010b, 010c, 012, 024 |
| arcagent | `modules/planning/models.py` | add `reserved_tokens`/`reserved_cost` + `available_budget()`; no DAG change | 053, 054 |
| arcagent | `modules/planning/executor.py` | `ConcurrentStepExecutor` (Protocol-compatible) | 054, 055 |
| arcagent | `modules/planning/orchestrator.py` | dispatch the whole frontier concurrently w/ reserve-then-settle; checkpoint aggregate; replan on any FAILED | 053, 055 |
| arcstore | (reuse) | checkpoint records via existing WORM spool | 006 |

**Zero change:** `arctrust` policy algorithms, `arcllm` (streaming already exposed via `invoke_stream`).

---

## 6. Failure modes (fail-closed everywhere)

| Condition | Behavior |
|---|---|
| Resume tool-set ≠ checkpoint set | Refuse resume (REQ-004) — poisoned surface (ASI06) |
| Checkpoint hook raises | Swallow + log; run continues (persistence never breaks the loop) — but at federal, a WORM write failure fails the run (AU-9) |
| `approval_provider` returns None / times out | Call not dispatched; structured tool_result "approval required" (REQ-011) |
| Runaway signature ≥ threshold | Halt `runaway_loop` (REQ-020) |
| Consecutive tool errors ≥ threshold | Halt `error_cascade` (REQ-021) |
| Two concurrent calls complete trifecta union | Second sees completed union under admission lock → `GlobalLayer` DENY → HumanGate (REQ-032) |
| N branches would overspend `Plan.budget` | (N+1)-th reservation returns None → branch deferred/failed; ceiling holds (REQ-053) |
| One branch fails | Captured `FAILED` StepOutcome; siblings unaffected; replan (REQ-055) |
| Unknown tool classification in a batch | Treated `state_modifying` → sequential (REQ-034) |
| Streaming cut | Loop calls `model.invoke`; `run_stream` emits final content as a block (no fake tokens); `stream_llm_response` unchanged for out-of-loop UX (REQ-040..043) |

---

## 7. Research Insights (/deepen enrichment)

External patterns consulted, each mapped to the design and cited. Convergent theme: **the loop stays a
dumb concurrent executor; correctness under concurrency comes from admission control over state a
specialized owner holds.**

### 7.1 LLMCompiler — parallel DAG execution (Kim et al., 2023, *An LLM Compiler for Parallel Function Calling*, arXiv 2312.04511)
LLMCompiler's three pieces — a **Planner** emitting a DAG of tasks with inter-task dependencies, a
**Task-Fetching Unit** that dispatches tasks the moment their dependencies resolve (streaming the
frontier), and an **Executor** running ready tasks in parallel — map **exactly** onto our split: SPEC-040's
`Plan`/`ready_steps` is the Planner + Task-Fetching Unit (arcagent), and SPEC-043's `plan_execute` strategy
is the parallel Executor (arcrun). Their headline result (up to ~3.7× latency and cost reduction from
parallelizing independent calls) is the payoff this spec unlocks by dispatching the whole `ready_steps()`
frontier instead of `ready[0]`. Critically, LLMCompiler keeps *dependency resolution* in the fetching unit,
not the executor — validating boundary 2.4 (the executor never learns `depends_on`). Our reserve-then-settle
budget guard is the piece LLMCompiler doesn't need (it has no shared cost ceiling) but a federal harness
does (LLM10). **Claude Code corroborates the same architecture in a shipping harness:** a *partition
algorithm* groups consecutive concurrency-safe tool calls into parallel batches and isolates unsafe ones
into serial batches, on the explicit principle that *"concurrency is a property of a specific tool
invocation with specific inputs — safety is per-call, not per-tool-type"*; and (June 2026) a failed call
returns its own result without cancelling siblings. Arc's `parallel_dispatch.BatchClassifier`
(read-only/state-modifying partition + per-call shared-path check) and
`dispatch_batch(return_exceptions=True)` already implement exactly this — so wiring it (REQ-030) adopts a
validated production design, and the Plan-Execute frontier fan-out (Option A) is the same "caller partitions
and dispatches" pattern Claude Code uses for both tool calls and sub-agent Tasks.

### 7.2 Agent-loop checkpoint / durable resume (LangGraph checkpointers; Temporal/durable-execution; NIST CP-10)
The dominant pattern for resumable agent loops (LangGraph's `Checkpointer`/thread state; durable-workflow
engines like Temporal) is **persist state at each super-step boundary; on restart, reload the last
checkpoint and continue — never re-run committed side effects.** Two lessons shape §3.1: (a) checkpoint at a
*deterministic boundary* (the turn) so resume is unambiguous — LangGraph checkpoints per node/super-step;
we checkpoint per turn. (b) The transcript *is* the state — durable-execution replays from an event/command
log rather than re-invoking effects, which is why our "replay" is deterministic resume from the persisted
message list + audit chain, not effect re-execution (OQ-4). Temporal's separation of *workflow state*
(durable) from *activity execution* (at-least-once, idempotent) mirrors our arcrun-emits / arcagent-persists
split — the loop is the workflow, tool calls are the activities. Federal fit: NIST 800-53 **CP-10**
(system recovery/reconstitution) and **AU-9** (protection of audit info) — a WORM-backed, tamper-evident
checkpoint is CP-10 + AU-9 evidence.

### 7.3 Circuit-breaker & runaway-loop detection (Nygard *Release It!*; OWASP LLM10; loop-detection heuristics)
The circuit breaker is Nygard's classic pattern applied to the agent loop: trip on a threshold, stop
calling the expensive dependency (the model/tools), degrade gracefully. OWASP **LLM10 (Unbounded
Consumption)** prescribes evaluating cost/quota ceilings **before** the expensive call with graceful denial
— hence a single top-of-turn `check_breaker`, not post-hoc cleanup. For **runaway detection**, the standard
agentic heuristic (echoed across AutoGPT-era "stuck-loop" fixes and ReAct-trajectory guards) is a
*no-progress* signal: the agent repeats the same action/observation with no state change. Hashing the
tool-call signature and tripping on repetition is the cheapest robust realization — and treating a
*distinct-signature* parallel batch as progress (REQ-025) avoids the false-positive that naive
"repeated-tool-name" detectors hit. **Error-cascade** breaking is the bulkhead/circuit-breaker guard against
ASI08 cascading failures: a dependency failing N times in a row trips the breaker before it drains the
budget. FinOps "hard vs. soft budget" maps to the `relaxable` tier flag (federal = hard floor). Federal fit:
NIST **SC-5** (denial-of-service protection) and **SC-6** (resource priority/availability).

### 7.4 Concurrency-safe admission control (token-bucket admission; TOCTOU; two-phase reservation)
The ledger race is a textbook **TOCTOU** (time-of-check-to-time-of-use): the check (`snapshot`) and the use
(`record`) straddle an `await`, so concurrent tasks interleave. The canonical fix is to make check-and-act
*atomic* under a mutex — here an `asyncio.Lock` scoped to the shared key (the session), held only for the
O(1) decision, not the slow side effect. This is the same discipline as database *admission control* and
Envoy's global rate-limit filter: the *decision* is serialized and cheap; the *work* runs concurrently. The
budget guard is **two-phase reservation** (reserve → settle), the standard pattern for bounded shared
resources under concurrency (connection pools, airline-seat/inventory holds, semaphore permits): you cannot
prevent N-way overspend by checking a *remaining* balance N times — each check must *decrement a
reservation* so later checks see less. Reserving before launch and settling actual on completion guarantees
`Σ reservations + spend ≤ ceiling`. The literature on **interleaving-forced testing** (deterministic
schedulers; the project's own `feedback_concurrency_tests_must_interleave`) warns that an instant mock makes
`asyncio.gather` run tasks *sequentially*, so the race never manifests and a broken guard passes green — the
acceptance tests therefore use an `asyncio.Barrier`/`Event` to force both tasks into the critical section
simultaneously (AC-Sec1/2, PLAN T-D3/T-F3). Federal fit: NIST **AC-4** (information-flow enforcement — the
trifecta is an AC-4 control, and a raced ledger is an AC-4 *bypass*) and **SC-5**.

### 7.5 Human-in-the-loop: tool-based trigger + durable interrupt (LangGraph HITL middleware; Microsoft Agent Framework Tool-Approval; ASI09)
Two questions: *when* to pause and *how*. On **when**, the 2025-26 SOTA is unambiguously **tool-based**:
the gate sits *between model intent and tool execution* — the model proposes a tool call, a policy checks
whether *that specific call* needs review, and only then does the runtime interrupt. LangGraph's HITL
middleware states it plainly ("HITL sits between model intent and tool execution: the model proposes a tool
call, a policy checks whether that call needs review, if yes the runtime interrupts and stores state").
Microsoft Agent Framework's **Tool-Approval policies** are "evaluated after code generation, allowing
inspection of the actual tools the agent intends to invoke," and the field's guidance on *which* tools:
side effects (modify data, send comms, purchases), data sensitivity (PII, credentials), and reversibility
(irreversible deletes/sends). This is exactly why SPEC-043's trigger is a pure predicate over the proposed
call's `name`/`capability_tags`/`classification` (§3.2) — Arc already carries those discriminants, so no
new taxonomy is invented. On **how**, the SOTA primitive is a **durable pause**: persist loop state, surface
a labeled request, resume only on an authenticated approval the agent itself cannot issue (LangGraph
`interrupt` + checkpoint). SPEC-035 already implements the *authority* half (operator-signed one-shot token,
approval ≠ audited subject); SPEC-043 adds the *durable-pause* half at the loop level and makes it
**checkpointable** (REQ-014) so an approval can arrive across a restart. The federal property preserved from
SPEC-035: approval authority is cryptographically distinct from the agent (ASI09; NIST **AC-4** human-review
flow-control point). The design *reuses* the grant rather than inventing a loop-level one — the research
confirms the hard part is the authenticated, non-self-issuable token, which already exists.

*(On streaming: the research + product judgment converge on descoping it — it is a UX affordance orthogonal
to loop correctness. No agent framework treats token streaming as a control-plane requirement; it is a
presentation-layer concern. Cutting it keeps the loop's model-call path a single `invoke`.)*

### 7.6 Observability "replay" — a distinct capability (Langfuse, LangSmith, Laminar) → reframed as SPEC-044
The PO's "replay" is not crash recovery — it is the **observability/governance** workflow the leading
platforms ship as a first-class feature. Langfuse captures "the exact prompt sent, the model's response,
token usage, latency, and any tools or retrieval steps," and its sandbox lets a team "identify a problematic
generation and **replay the chain from that point with prior inputs and context frozen, then re-run** to see
how a change propagates." LangSmith's Playground "opens LLM runs directly from traces" preserving original
context; Laminar makes "span replay a first-class workflow," opening a span with its "original model, tool,
and prompt configuration" and offering synchronized session replay. Three consistent properties: (1) **full,
structured trace capture** — every step's input/output, timing, cost, and config; (2) **frozen upstream
context** — you re-run *one* step against its stored inputs, you do not redo the earlier chain; (3)
**versioned provenance** — which model/tool/prompt produced the step (federal adds: which *tool version* and
*skill version* — supply-chain, LLM03/ASI04). Property (2) is exactly the PO's insight: because the full
history is recorded, a single-step re-execution needs no earlier steps. This is **semantically distinct from
resume** (continue-after-crash) and materially larger — it is a durable trace store + a step-through/re-run
surface, not a loop control. Hence the split: **SPEC-043 emits the versioned provenance** (REQ-007, the
substrate); **SPEC-044 builds the replay experience** over arcstore (durable versioned trace — already the
arcui data source), arcui (step-through + re-run), and arctrust (audit/provenance). Federal fit: NIST
**AU-2/AU-3** (auditable content — the trace *is* the audit record), **AU-10** (non-repudiation — versioned,
tamper-evident provenance), **CM-8** (component/version inventory — tool/skill versions on every step).

### 7.7 Net design consequence
Every thread converges on the codebase's existing commitments: **arcrun stays a dumb, identified,
concurrent executor; the guards live with the state their owner holds; the label/authority travels with the
data and is re-checked at each sink.** Parallelism → wire the one classified dispatch path and serialize
only the O(1) admission. Budget → reserve-then-settle in the plan owner. Checkpoint → arcrun emits, arcagent
persists, arcstore seals. HITL → arcrun pauses on a tool-based trigger, SPEC-035 decides. This is why
arcrun needs no sibling imports and the `Plan` model is untouched: **all five controls land by *wiring*,
not building** — and the would-be sixth (true streaming) is *removed*, not added, shrinking the loop rather
than rewriting its model-call path.

---

## 8. Test strategy (maps to PLAN)

- **Unit (arcrun):** `to/from_checkpoint` round-trip; resume refuses mutated tool set; `check_breaker` trips on token/cost/turns/runaway/error-cascade with the right reason; runaway ignores distinct parallel batch; `dispatch_batch` wired (ad-hoc path gone); `run_stream` fake word-split deleted + output contract unchanged; `plan_execute` runs independent items concurrently.
- **Unit (arcagent):** ledger `admission_lock` serializes snapshot/record; `persist_checkpoint` writes JSONL + WORM; `available_budget` = remaining − reservations; `ConcurrentStepExecutor` satisfies the Protocol.
- **Concurrency (interleaving-forced, `asyncio.Barrier`/`Event` — NOT an instant mock):** two calls whose union completes the trifecta are NOT both allowed (REQ-032); N concurrent branches cannot overspend `Plan.budget` (REQ-053); a slow policy-evaluate does not let a second call slip its record.
- **Integration:** crash-mid-plan → resume skips completed work (REQ-003 + SPEC-040 resume); approval-required tool pauses then proceeds on grant / fails closed on None; a plan with two independent branches executes them concurrently and both stay gated.
- **Boundary:** import test — `arcrun` imports no `arcagent`/`arctrust`/`arcteam`; the `Plan` model diff is empty; arcrun references no token type.

---

## 9. Traceability (REQ → component)

| REQ | Component(s) |
|---|---|
| 001-004 | `arcrun/checkpoint.py`; `state.on_checkpoint`; `loop.resume_from`; tool-set verify |
| 005, 006 | `SessionManager.persist_checkpoint`; arcstore WORM |
| 007 | tool/skill version on step events (replay substrate; SPEC-044 consumes) |
| 010-014 | `state.approval_provider` + `_execute_tool_calls` pause; arcagent HumanGate binding |
| 010b, 010c | arcagent tier→approval-set resolution (personal/enterprise/federal); skill-backed `Tool` marking |
| 020-025 | `check_breaker`; signature ring; `consecutive_tool_errors`; `make_budget_breach_args` reasons |
| 030-035 | `dispatch_batch` wiring; `Tool.classification`; `BatchClassifier` shim; semaphore |
| 032 | `capability_ledger.admission_lock`; `tool_registry` critical section |
| 040-043 | `streams.py` — delete fake word-split; loop keeps `model.invoke`; `stream_llm_response` kept for out-of-loop UX |
| 050-056 | `strategies/plan_execute.py`; `ConcurrentStepExecutor`; orchestrator frontier dispatch |
| 053 | `models.reserved_*`/`available_budget`; orchestrator reserve-then-settle |
| 060, 061 | tier config floors; audit emission on every control op |

## 10. Boundary guardrails (do not cross)

- **arcrun** adds the five controls as *mechanism* — checkpoint *emission* (never persistence), a pause *await* (never a token), a breaker, a wired dispatch, a concurrent strategy — and *removes* the fake streaming. No `Plan`, no `arctrust`/`arcagent` import, no token streaming in the loop.
- **arcagent** *wires + guards*: persists checkpoints, binds the approval provider to HumanGate, holds the admission lock and the budget reservation, dispatches the DAG frontier. No turn-loop; no new token/persistence subsystem.
- **arctrust** unchanged — pure predicates over injected state.
- **arcllm** unchanged — `invoke_stream` already exists.
- Every wired seam deletes its dead predecessor in the same edit: the ad-hoc gather path (§3.4), the synthetic word-split (§3.5), the tail max_turns check (§3.3). No parallel implementations left behind.
