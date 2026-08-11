# Decisions Log

Every design decision made across Arc, grouped by **concern** and then by **package**.

## How to read this

- **Sections 1–14** are the decisions themselves, one concern per section, packages inside.
- **Appendix A** is the ID lookup — find any `D-NNN` and jump to its section.
- **Appendix B** points to the `/build` and `/deepen` output — research, diagrams, risk registers,
  open questions — which lives with each build in `.claude/builds/<feature>/research.md`.

## ID convention

- IDs are global, monotonic, and **never renumbered**. A superseded decision keeps its ID and gains
  a note; it is not deleted and not reused.
- Range in use: `D-001` – `D-667`.
- `D-403` was allocated but never used. It stays empty.
- Five early decisions used off-pattern IDs (`T-1`, `T-001`, `TS-001`, `DP-001`, `U-001`) and now carry
  `D-581`–`D-585`. Each one shows its former ID inline.
- arcrun's separate `DECISION-NNN` log was folded in here as `D-586`–`D-619`; each shows its former
  ID inline, so the `DECISION-NNN` references in `.claude/specs/001-core-loop-react/` still resolve.
- New decisions append to the end of the matching category and package. `/build` allocates the next
  free ID with `allocate_ids.py`.

---

## Contents

| # | Category | Decisions | Packages |
|---|----------|-----------|----------|
| 1 | [Architecture](#1-architecture) | 197 | arcllm, arcrun, arcagent, arcmemory, arcprompt, arcteam, arcui, arctui, capabilities, cross-cutting |
| 2 | [Data Model](#2-data-model) | 67 | arcllm, arcrun, arcagent, arcmemory, arcprompt, arcteam, arcstore, arcui, capabilities, cross-cutting |
| 3 | [API Design](#3-api-design) | 51 | arcllm, arcrun, arcagent, arcprompt, arcui, capabilities, cross-cutting |
| 4 | [Identity & Trust](#4-identity--trust) | 15 | arcllm, arcagent, arctrust, arcprompt, arcteam, arcui, cross-cutting |
| 5 | [Security](#5-security) | 114 | arcllm, arcrun, arcagent, arcmemory, arcprompt, arcteam, arctrust, arcui, arctui, capabilities, cross-cutting |
| 6 | [Audit & Compliance](#6-audit--compliance) | 28 | arcllm, arcrun, arcagent, arcprompt, arcteam, arcui, capabilities, cross-cutting |
| 7 | [Observability](#7-observability) | 23 | arcllm, arcrun, arcagent, arcprompt, arcteam, arcui |
| 8 | [Integration](#8-integration) | 29 | arcllm, arcrun, arcagent, arcmemory, arcprompt, arcteam, arcui, cross-cutting |
| 9 | [Performance](#9-performance) | 29 | arcllm, arcrun, arcagent, arcprompt, arcteam, arcui |
| 10 | [Extensibility](#10-extensibility) | 35 | arcllm, arcrun, arcagent, arcprompt, arcteam, arcui, arctui, cross-cutting |
| 11 | [Testing](#11-testing) | 23 | arcllm, arcrun, arcagent, arcmemory, arcprompt, arcteam, arcui |
| 12 | [Deployment](#12-deployment) | 14 | arcllm, arcrun, arcagent, arcprompt, arcui, capabilities |
| 13 | [UI/UX](#13-uiux) | 24 | arcagent, arcprompt, arcui, arctui, capabilities, cross-cutting |
| 14 | [CLI](#14-cli) | 2 | arcagent |

**Total: 625 decisions across 14 categories.**

---

## 1. Architecture

### arcllm

#### D-188 — Budget-Telemetry Integration Pattern

`arcllm` · Architecture · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Extend TelemetryModule with budget tracking
- **Alternatives**: Sibling module with shared state (rejected: shared mutable state complexity); Budget wraps Telemetry (rejected: two modules for one concern)
- **Rationale**: Simplest approach — one module, one cost concern area. Budget can't be bypassed without bypassing telemetry. Stack order unchanged: `Otel > Telemetry(+Budget) > Audit > Security > Retry > Fallback > RateLimit > Adapter`. Cost IS telemetry.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-189 — Budget Scope Isolation

`arcllm` · Architecture · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Per-agent ID scope
- **Alternatives**: Hierarchical agent+tenant (rejected: shared state across agents); Per-provider (rejected: no per-agent attribution, contention); Configurable scope key (rejected: no structure enforcement)
- **Rationale**: Flat lookup, shared-nothing. Maps 1:1 to ArcAgent DID identity. Per-agent spend attribution for NIST AU-3. `budget_scope="agent:agent-007"` passed at `load_model()` time — mandatory, no default.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-190 — Routing Module Stack Position

`arcllm` · Architecture · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Router replaces adapter at innermost position
- **Alternatives**: Outermost/before all modules (rejected: duplicates entire module stack per route, fragments budget/telemetry); Inside Security/outside Retry (rejected: tighter coupling)
- **Rationale**: Router IS the provider. One module stack, multiple backends. All security/observability modules apply uniformly regardless of which provider handles the request. Clean abstraction.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-191 — Router Adapter Lifecycle

`arcllm` · Architecture · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Eager — load all adapters at init
- **Alternatives**: Lazy/load on first use (rejected: deferred config errors, lazy init complexity)
- **Rationale**: Validates all configs upfront (fail fast). Predictable cold start and memory footprint. All connections established and auditable at startup.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-192 — Budget-Routing Interaction

`arcllm` · Architecture · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Budget tracks total agent spend only — not per-provider
- **Alternatives**: Budget tracks per-provider too (rejected: more accumulators, couples budget to routing)
- **Rationale**: OTel spans already contain per-call provider info (`gen_ai.system`, `gen_ai.request.model`). Grafana can aggregate spend per provider via OTel queries. Budget accumulator stays simple — one counter per agent per period.

#### D-232 — Inheritance strategy

`arcllm` · Architecture · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Subclass `OpenaiAdapter` — override `name`, `_build_headers()`, `invoke()` only
- **Priority**: Simplicity
- **Tier Notes**: All tiers: same adapter, no tier-specific behavior

#### D-233 — Class name

`arcllm` · Architecture · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: `Azure_openaiAdapter` with `# noqa: N801` (follows `Huggingface_TgiAdapter` precedent)
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-234 — Provider name

`arcllm` · Architecture · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: `azure_openai` (TOML filename + adapter module path convention)
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-280 — Stack Position

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: QueueModule sits just inside OtelModule (between Otel and Telemetry)
- **Priority**: Simplicity — Otel captures full picture including queue wait; telemetry only counts actual call
- **Alternatives**: Outermost (rejected — Otel wouldn't see queue wait), Between Retry and RateLimit (rejected — more complex interaction, retries don't re-enqueue)
- **Rationale**: Otel span wraps queue wait + call, giving complete timing. Telemetry/Audit/Security/Retry all operate within the queue slot.
- **Stack**: `Otel → QueueModule → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter`
- **Tiers**: Federal: queue enabled by default, audit events mandatory | Enterprise: queue enabled by default | Personal: queue enabled by default (simplicity benefit is universal)

#### D-281 — Queue Scope

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Per adapter instance (each `load_model()` gets its own queue)
- **Priority**: Simplicity — zero shared state, matches arcllm's existing stateless pattern
- **Alternatives**: Shared per provider endpoint (rejected — introduces global mutable state, thread-safety complexity, breaks arcllm's pattern)
- **Rationale**: Agents already have separate model instances for main vs eval. Different models have different concurrency needs. No shared state means no coordination bugs.
- **Tiers**: Same across all tiers

#### D-282 — Concurrency Primitive

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: `asyncio.Semaphore` — stdlib, FIFO, 5 lines of core logic
- **Priority**: Simplicity — proven pattern, zero deps, already used in arcagent's spawn_background
- **Alternatives**: PriorityQueue + Semaphore (rejected — worker loop lifecycle, Future-based indirection, ~30 lines vs ~5)
- **Rationale**: FIFO is sufficient. If priority becomes a real need, the module interface stays the same — only internals change.
- **Tiers**: Same across all tiers
- **Deepen notes**: [`builds/arcllm-call-queue/research.md`](builds/arcllm-call-queue/research.md)

#### D-283 — Backpressure

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Max waiters limit — track waiter count, reject with `QueueFullError` when exceeded
- **Priority**: Security — bounded resource usage prevents unbounded consumption (LLM10, ASI-08)
- **Alternatives**: No limit / rely on caller timeouts (rejected — unbounded accumulation flagged as medium-severity in scheduler hardening review), Oldest-out eviction (rejected — canceling in-progress work is surprising)
- **Rationale**: Clear failure mode. Caller decides how to handle rejection. Consistent with scheduler hardening findings.
- **Tiers**: Federal: max_queued enforced, rejection audited | Enterprise: max_queued enforced | Personal: max_queued enforced (simplicity benefit)
- **Deepen notes**: [`builds/arcllm-call-queue/research.md`](builds/arcllm-call-queue/research.md)

#### D-284 — Timeout Semantics

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Send-time timeout only — timeout starts when semaphore is acquired (call actually fires), not when enqueued
- **Priority**: Simplicity — this IS the core fix. Timeouts mean what they say.
- **Alternatives**: Dual timeout / queue wait + send (rejected — two timeout configs to manage, more complex)
- **Rationale**: Root cause of the bio_memory bug was timeouts that included invisible queue wait. Queue wait is bounded by backpressure (max_queued). Call timeout measures actual LLM response time.
- **Tiers**: Same across all tiers

#### D-285 — Configuration

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Standard `load_model()` kwarg pattern — `queue=True|False|dict`, config.toml under `[modules.queue]`
- **Priority**: Simplicity — identical to every other arcllm module, zero new patterns
- **Alternatives**: Always-on with no config (rejected — some callers may need to disable or customize)
- **Rationale**: Consistency with existing retry, rate_limit, telemetry modules. Config.toml provides defaults, per-model overrides via kwarg.
- **Tiers**: Same across all tiers

#### D-286 — Exception Hierarchy

`arcllm` · Architecture · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Two exceptions under `ArcLLMError` — `QueueFullError` (backpressure rejection) and `QueueTimeoutError` (call exceeded send-time timeout)
- **Priority**: Simplicity — minimal, precise, existing `except ArcLLMError` blocks catch both
- **Alternatives**: Single QueueError with reason enum (rejected — less precise except blocks)
- **Rationale**: Callers can handle each failure mode differently. Bio_memory can skip on QueueFullError, retry on QueueTimeoutError.
- **Tiers**: Same across all tiers

#### D-393 — Cache breakpoint location

`arcllm` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Breakpoint is set in arcllm (`_build_request_body`), never in arcrun. arcrun's contract to arcllm is "stable ordered list," nothing more.
- **Priority**: modularity
- **Rationale / Tier Notes**: No caching concept leaks into the loop nucleus.

#### D-449 — Scope is intra-provider: spread across N endpoints/keys of the *same* provider

`arcllm` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Distinct from FallbackModule (inter-provider) and RoutingModule (classification); raises aggregate throughput, never changes which provider/model answers.

#### D-450 — Default strategy: weighted round-robin

`arcllm` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Deterministic, stateless-per-call, zero deps; weights bias larger replicas / higher-quota keys.

#### D-451 — Health-aware strategy skips endpoints via a per-endpoint circuit mechanism owned by the pool

`arcllm` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Reuses circuit_breaker failure-count/cooldown per endpoint; a tripped replica is skipped until cooldown.

#### D-452 — Sticky routing (session/agent key) optional, off by default

`arcllm` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Pins a caller for prompt-cache locality; trades even distribution for cache warmth — explicit operator choice.

#### D-454 — Cursor + per-endpoint health in a shared per-pool registry guarded by `asyncio.Lock`

`arcllm` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Mirrors rate_limit `_bucket_registry`; thousands of agents share one cursor/health view — no singleton bottleneck, no per-agent drift.

#### D-458 — LB sits at the innermost stack position, holding a pool of endpoint adapters (Router-like)

`arcllm` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Selection happens closest to the wire; RateLimit/Retry/CircuitBreaker wrap the pool; replaces the single adapter like RoutingModule.

### arcrun

#### D-131 — How does the model express decomposition intent?

`arcrun` · Architecture · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Spawn as a tool (Claude Code pattern)**
- **Rationale**: No new strategies needed. Model calls `spawn_task` like any tool within the react loop. Simplest extension of existing architecture.
- **Category**: Architecture

#### D-132 — Where does the spawn tool live?

`arcrun` · Architecture · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **ArcRun built-in + overridable**
- **Rationale**: Lives in `arcrun/builtins/spawn.py`. ArcRun is standalone — spawn works without ArcAgent. ArcAgent can override with a richer version (identity, permissions). Same pattern as `execute_python`.
- **Category**: Architecture

#### D-133 — Spawn tool API (model arguments)

`arcrun` · Architecture · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **task + system_prompt + tools**
- **Rationale**: `spawn_task(task, system_prompt, tools)`. Model specializes child role and restricts capabilities. `max_turns` inherits from parent. Strategy not exposed — child uses same selection logic.
- **Category**: Architecture

#### D-141 — Child failure behavior

`arcrun` · Architecture · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Error string as tool result**
- **Rationale**: Same as any failed tool. Parent gets `"Error: child failed — {reason}"` and decides how to proceed. Consistent, no special handling.
- **Category**: Error Handling

#### D-144 — V1 scope

`arcrun` · Architecture · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Confirmed MVP**
- **Rationale**: See scope section below.
- **Category**: Scope

#### D-175 — What does the container sandbox wrap?

`arcrun` · Architecture · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **CodeExec only**
- **Rationale**: Only `make_execute_tool()` (model-generated code) runs in containers. User-provided `Tool.execute` stays in-process — caller trusts their own tools. Model-generated code is the RCE threat vector.
- **Category**: Architecture

#### D-176 — How does container sandbox integrate?

`arcrun` · Architecture · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **New factory: `make_contained_execute_tool()`**
- **Rationale**: Separate factory function. Existing `make_execute_tool()` unchanged. Caller explicitly opts into container isolation. Zero changes to existing code. Same pattern as execute vs spawn.
- **Category**: Architecture

#### D-179 — Event verification API

`arcrun` · Architecture · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Method on LoopResult**
- **Rationale**: `result.verify_integrity()` returns bool. Optional `detailed=True` for VerifyResult with chain metadata. Natural home — caller already has the result. ~5 LOC public API.
- **Category**: Architecture

#### D-183 — NIST 800-53 documentation format

`arcrun` · Architecture · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Standalone `docs/security/` directory**
- **Rationale**: `nist-800-53-mapping.md` + `threat-model.md` + `adversarial-tests.md`. Each control entry includes: control ID, title, arcrun feature, code reference, test evidence. Separate from README.
- **Category**: Architecture

#### D-185 — Docker SDK dependency strategy

`arcrun` · Architecture · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Optional: `pip install arcrun[container]`**
- **Rationale**: Lazy import with helpful error message if not installed. Zero impact on existing users. `docker>=7.0` in `[project.optional-dependencies]`.
- **Category**: Dependencies

#### D-186 — LOC budget revision

`arcrun` · Architecture · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **1,400 LOC for Phase 4**
- **Rationale**: Current: ~1,221 LOC (post-spawn). Phase 4 adds ~120-160 LOC (container factory + hash chain). Spawn was bigger than estimated (+421 LOC vs Phase 3 budget). Accept and adjust. Log in ADR.
- **Category**: Architecture

#### D-391 — Tool-set stability owner

`arcrun` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: arcrun owns an **immutable, deterministically-ordered per-run tool set**, NOT caching. Freeze `ToolRegistry` after construction (reject add/remove on the per-run object); memoize `list_schemas()`.
- **Priority**: modularity > security
- **Rationale / Tier Notes**: Resolves "don't mix concerns": cache hit is emergent at arcllm boundary. Freezing also closes ASI04/LLM06 mid-run tool-injection surface.

#### D-455 — Cross-AGENT scheduling / fairness / global prioritization OUT OF SCOPE

`arcrun` · Architecture · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Loop/runtime concern → arcrun. arcllm balances one caller's invoke across endpoints, never between agents.

#### D-586 — Package Name

`arcrun` · Architecture · was `DECISION-001` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: `arcrun`
- **Context**: Original name "arcloop" described the shape (a loop) but not the purpose (agent execution). Needed a name that communicates what the layer does.
- **Options**:
- `arcloop` — Describes mechanism, not purpose
- `arcexec` — Direct: "arc execute". Clear execution layer signal.
- `arcrun` — Action-oriented: "arc run". Short, verb-based, implies execution.
- `arcengine` — Engine metaphor from PRD. Heavier word but clear.
- `arcagent` — Most direct for domain but confusable with agent definitions (which live above).
- **Reasoning**: Short, action-oriented, verb-based. "Arc run" communicates exactly what it does — it runs agent tasks. Clean import: `from arcrun import run`. No confusion with the agent definition layer above.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-587 — Adopt Steering (Mid-Execution Interrupt)

`arcrun` · Architecture · was `DECISION-002` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Adopt steering capability
- **Context**: pi-agent-core has `steer()` — ability to inject new instructions while tools are executing. Important for human-in-the-loop and course correction.
- **Options**:
- Skip — caller can abort and restart
- Adopt — add `steer(message)` method to inject messages mid-loop
- **Reasoning**: Enables human-in-the-loop patterns. When a tool is running and new context arrives (user correction, priority change), the loop can incorporate it instead of completing a potentially wrong path. Critical for enterprise deployments where humans supervise agents.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-589 — Adopt Streaming Response Deltas

`arcrun` · Architecture · was `DECISION-004` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Adopt streaming support
- **Context**: Currently arcrun waits for full `model.invoke()` response. Streaming would let callers show progress in real-time.
- **Options**:
- Skip — full response only
- Adopt — support streaming when arcllm model supports it
- **Reasoning**: Better UX for interactive agents. Federal/enterprise dashboards need real-time visibility into what agents are doing. Streaming also enables early cancellation (ties into steering). Implementation: arcllm would need to support streaming invoke, arcrun passes through.
- **Status**: Accepted — requires arcllm streaming support (not yet built)
- **Date**: 2026-02-11

#### D-591 — arcrun Owns Run-Level State

`arcrun` · Architecture · was `DECISION-006` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: arcrun owns run-level state
- **Context**: Need to decide where state lives during a single run() call. State includes: message history, turn count, token/cost accumulators, tool registry, event log, spawn budget.
- **Options**:
- arcrun owns run state — internal RunState during execution, caller gets read access via handle. State dies when run() returns.
- Agent owns all state — arcrun is pure function, messages in, result out. No internal state.
- Shared — arcrun manages but accepts initial state and returns final state.
- **Reasoning**: arcrun already manages messages internally. Steering requires knowing current state to interrupt. Streaming requires state to emit deltas. Context transform needs access to the message list. RunState is internal to the execution — it dies when run() returns. Cross-session state (memory, user profiles) stays with the agent above. Clean separation: arcrun owns "what's happening right now", agent owns "what happened before and what to do next".
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-602 — Strategy Enforces Max Turns

`arcrun` · Architecture · was `DECISION-017` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Strategy enforces
- **Context**: Who counts turns and enforces the max_turns limit?
- **Options**:
- run() enforces — counts model.invoke() calls, stops strategy at limit. Consistent but rigid.
- Strategy enforces — each strategy manages its own turn count and defines what "turn" means.
- **Reasoning**: Different strategies have different semantics. CodeExec might count "code executions" not LLM calls. Recursive might count "spawn completions." Strategy owns the loop and knows when to stop. run() is pure orchestration — picks strategy, hands off, gets result. Clean separation of concerns.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-604 — Strategy Prepends to System Prompt

`arcrun` · Architecture · was `DECISION-019` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Prepend to system prompt
- **Context**: CodeExec and Recursive strategies need to steer model behavior. How do they inject instructions?
- **Options**:
- Prepend to system prompt — strategy instructions + caller's prompt concatenated
- Inject as first user message — keeps prompt clean but adds context token
- Separate system message — clean but not all providers support multiple system messages
- **Reasoning**: Simple concatenation: strategy instructions first, then caller's system prompt. One string, no extra messages, works with every provider. Model sees unified instructions. ReAct (default) adds nothing — caller's prompt used as-is.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-611 — Extract Shared Tool Executor

`arcrun` · Architecture · was `DECISION-026` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Extract to `src/arcrun/executor.py`
- **Context**: Tool execution pipeline (sandbox check, registry lookup, schema validation, execute, events, error handling) is inline in react.py. Future strategies (CodeExec, Recursive) need the same pipeline. Extracting prevents copy-paste across strategies.
- **Options**:
- Keep inline — each strategy implements its own tool execution (duplication)
- Extract to shared module — single function all strategies call
- **Reasoning**: DRY. Every strategy needs the same 10-step pipeline. Extraction means one place to add timeout, truncation, rate limiting later. Strategies only own their loop/control flow — tool execution is convention.
- **Status**: Accepted
- **Date**: 2026-02-14

#### D-612 — Single Tool Call Granularity

`arcrun` · Architecture · was `DECISION-027` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Single tool call only
- **Context**: Should executor expose a single-call function or a batch function?
- **Options**:
- Single: `execute_tool_call(tc, state, sandbox)` — strategy loops over calls
- Batch: `execute_tool_calls(tool_calls, state, sandbox)` — executor loops
- Both: single as primitive, batch as convenience
- **Reasoning**: Strategy owns the loop. A strategy may call tools in parallel (asyncio.gather over single calls) or sequentially — that's strategy logic. Executor is just "run this one tool call through the pipeline." Most composable.
- **Status**: Accepted
- **Date**: 2026-02-14

#### D-614 — Strategy Owns Cancel/Steer Checks

`arcrun` · Architecture · was `DECISION-029` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Strategy checks before calling executor
- **Context**: Should executor check cancel_event and steer_queue before executing, or should strategies handle that?
- **Options**:
- Strategy checks — executor is pure execution
- Executor checks — couples executor to control flow
- **Reasoning**: Clean separation. Executor = "run this tool." Strategy = "should I run this tool?" Cancel and steering are control flow concerns that vary by strategy. Executor doesn't know about steering, cancel events, or loop state.
- **Status**: Accepted
- **Date**: 2026-02-14

#### D-615 — Executor Increments tool_calls_made

`arcrun` · Architecture · was `DECISION-030` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Executor increments
- **Context**: Should executor increment state.tool_calls_made on success, or leave it to strategies?
- **Options**:
- Executor increments — consistent counting across all strategies
- Strategy increments — executor stays pure
- **Reasoning**: Executor owns the full pipeline: validate → execute → track. Strategies don't need to remember to increment. Prevents counting bugs in future strategies. The counter is part of "executing a tool" not "running a loop."
- **Status**: Accepted
- **Date**: 2026-02-14

#### D-617 — CodeExec Wraps react_loop

`arcrun` · Architecture · was `DECISION-032` · from *Build Decisions: Phase 2 — CodeExec*

- **Decision**: Wrapper around react_loop
- **Context**: CodeExec is "ReAct with an augmented system prompt." Three options for how code.py relates to react.py.
- **Options**:
1. Wrapper around react_loop — code.py prepends prompt, delegates to react (~15 lines)
2. Copy + modify react.py — full duplication allowing future divergence (~135 lines)
3. Parameterized react_loop — add system_prompt_prefix param to react_loop itself
- **Reasoning**: Zero duplication. CodeExec is literally: modify the system message in state.messages, then call react_loop. If CodeExec needs to diverge later (different error retry logic, code-specific turn handling), it can be unwrapped into its own loop. YAGNI until then.
- **Implication**: `_build_result()` stays in react.py — no extraction needed. CodeExecStrategy.__call__ modifies state.messages[0] (system prompt), then delegates.

#### D-618 — ABC Base Class for Strategy

`arcrun` · Architecture · was `DECISION-033` · from *Build Decisions: Phase 2 — CodeExec*

- **Decision**: ABC base class
- **Context**: With two strategies, the implicit function-signature convention needs formalization.
- **Options**:
1. `typing.Protocol` — structural typing, strategies just need the right shape
2. Keep implicit — functions in a dict, metadata stored separately
3. `ABC` base class — explicit inheritance, clear contract
- **Reasoning**:
Clearer contract than Protocol. Forces every strategy to declare name + description + implement __call__. Good for documentation. Both strategies become classes:
- `ReactStrategy` wraps the existing `react_loop` function
- `CodeExecStrategy` inherits nothing from ReactStrategy — it's its own class that calls react_loop
- **Shape**:
```python
from abc import ABC, abstractmethod
class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    @property
    @abstractmethod
    def description(self) -> str: ...
    @abstractmethod
    async def __call__(self, model, state, sandbox, max_turns) -> LoopResult: ...
```

#### D-622 — RunState remains internal to ArcRun

`arcrun` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Do not promote mutable `RunState` into ArcRun's public facade merely to satisfy ArcAgent's current deep imports. Add a narrow immutable parent/run context contract containing only the run metadata and event operations an upper layer legitimately consumes.
- **Alternatives**: Root-export `RunState` (rejected: fossilizes mutable loop internals); retain `arcrun.state` imports (rejected: makes file topology a cross-package contract).
- **Rationale**: ArcRun owns run-level state. ArcAgent needs selected context, not ownership of the engine's mutable implementation object.

#### D-623 — Parallel dispatch is a public mechanism, not a concrete strategy dependency

`arcrun` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Expose a small public parallel-ready dispatch mechanism or protocol for callers that need it; ArcAgent must not instantiate `PlanExecuteStrategy` solely to call its helper method.
- **Alternatives**: Root-export `PlanExecuteStrategy` (rejected: couples the caller to a strategy implementation); duplicate `asyncio.gather` logic in ArcAgent (rejected: creates a second concurrency primitive).
- **Rationale**: Mechanisms are stable seam material; named strategies remain ArcRun implementation/composition choices.

### arcagent

#### D-073 — Tool catalog formatter location

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: `format_for_prompt()` on ToolRegistry
- **Options Considered**: ToolRegistry method / Standalone class / Mirror SkillRegistry
- **Rationale**: Registry knows its own data. Same pattern as SkillRegistry but lives on ToolRegistry directly.

#### D-074 — Prompt format

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: XML tags for both catalogs
- **Options Considered**: Markdown / XML / Mixed
- **Rationale**: Consistent with SkillRegistry's XML format. Structured, parseable.

#### D-075 — RegisteredTool new fields

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: Add `when_to_use`, `example`, `category` to both RegisteredTool and @native_tool decorator
- **Options Considered**: RegisteredTool only / Add to decorator too
- **Rationale**: Ergonomic — module authors set metadata inline at registration time.

#### D-076 — Tool catalog cache strategy

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: Invalidate on `register()`
- **Options Considered**: Invalidate on register / Bus events / No cache
- **Rationale**: `register()` is the single entry point for all tools (startup, reload, mid-session). One line: `self._prompt_cache = None`. Always fresh, zero overhead.

#### D-077 — Team roster cache strategy

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: TTL-based refresh (60s default)
- **Options Considered**: TTL / Rebuild every turn / File watcher
- **Rationale**: Entity data lives on disk, written by other processes. No hook into their writes. TTL bounds staleness. Different from tools because different data ownership.

#### D-078 — Section ordering

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: Alphabetical
- **Options Considered**: Alphabetical / Explicit priority
- **Rationale**: messaging → skills → tools is reasonable. Identity first, context last. Don't over-engineer the middle.

#### D-079 — Built-in tools in catalog

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: All tools, no exceptions
- **Options Considered**: All / Exclude builtins / Minimal builtins
- **Rationale**: Everything through `register()` appears. Consistent. Built-in tools can have `when_to_use` too.

#### D-080 — Section key for team context

`arcagent` · Architecture · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: `sections['teams']`
- **Options Considered**: `sections['messaging']` / `sections['teams']`
- **Rationale**: Will grow to include more team-based injections beyond messaging. Future-proof name.

#### D-119 — Where does the scheduler live?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Module (via Module Bus)**
- **Rationale**: Keeps core under LOC budget. Opt-in via config. Lives in `arcagent/modules/scheduler/`.
- **Category**: Architecture

#### D-120 — Which schedule types ship?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **All three: interval + cron + once**
- **Rationale**: Full ScheduleEntry from design doc. Complete from day one.
- **Category**: Scope

#### D-122 — What happens when a schedule fires?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **agent.run(prompt) with new session**
- **Rationale**: Each fire creates a fresh session, runs prompt through full agent loop (tools, memory, context).
- **Category**: Execution

#### D-123 — Schedule management tools?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Full CRUD: create/list/update/cancel**
- **Rationale**: Agent fully owns its schedule lifecycle. 4 tools exposed to LLM.
- **Category**: Agent Tools

#### D-124 — Active hours and limits?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Active hours + timeout. No max_retries.**
- **Rationale**: Active hours critical for heartbeats. arcllm handles LLM retries. Schedule-level retries deferred.
- **Category**: Constraints

#### D-125 — Overlapping executions?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Queue and run sequentially**
- **Rationale**: FIFO queue. Nothing missed, no concurrency issues.
- **Category**: Concurrency

#### D-126 — When does the scheduler run?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Standalone daemon (`arc agent serve`)**
- **Rationale**: Agent stays warm in-process. Schedules fire even when nobody is chatting. Fresh session per execution.
- **Category**: Lifecycle

#### D-127 — Where are schedules defined?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Runtime only (schedules.json)**
- **Rationale**: No TOML seeds. Agent creates via tools or operator edits JSON. Single source of truth.
- **Category**: Config

#### D-129 — Cron expression parser?

`arcagent` · Architecture · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **croniter**
- **Rationale**: Proven library. Handles DST, leap years, edge cases. Used by Airflow, Celery.
- **Category**: Dependencies

#### D-145 — CDP client library

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **cdp-use (browser-use's CDP layer)**
- **Rationale**: Type-safe Python CDP bindings auto-generated from Chrome's protocol spec. No agent/LLM baggage from browser-use. Direct CDP over WebSocket.
- **Category**: Architecture

#### D-146 — Module internal structure

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **Tool-per-file**
- **Rationale**: Each tool (navigate, click, type, screenshot, etc.) in its own file. BrowserModule wires them together. Most granular, easiest to test individually.
- **Category**: Architecture

#### D-147 — Tool API surface

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **Fine-grained tools**
- **Rationale**: Individual tools: browser_navigate, browser_click, browser_type, browser_screenshot, browser_read_page, browser_fill_form, browser_execute_js, browser_handle_dialog. LLM picks exactly what it needs.
- **Category**: Architecture

#### D-148 — Element selection strategy

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **Hybrid: accessibility-first, CSS fallback**
- **Rationale**: Primary: accessibility tree snapshot (role + name). Fallback: CSS selectors when accessibility labels are missing. Semantic and robust.
- **Category**: Architecture

#### D-149 — Page state representation

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **Accessibility snapshot (structured)**
- **Rationale**: Return accessibility tree as structured text with role, name, value, and numeric ref IDs for targeting. Compact, LLM-friendly.
- **Category**: Architecture

#### D-158 — Config schema

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **Flat under [modules.browser]**
- **Rationale**: All config under [modules.browser] with sub-tables for security, timeouts, connection. Matches existing module config pattern.
- **Category**: Configuration

#### D-159 — Module bus events

`arcagent` · Architecture · from *Feature: CDP Browser Module*

- **Choice**: **Full action events**
- **Rationale**: Emit for every action: browser.navigated, browser.clicked, browser.typed, browser.screenshot_taken, browser.js_executed, browser.dialog_handled, browser.connected, browser.disconnected, browser.error. Rich audit stream.
- **Category**: Events

#### D-160 — Where does Telegram integration live?

`arcagent` · Architecture · from *Feature: Telegram Messaging Module*

- **Choice**: **ArcAgent module** (`arcagent/modules/telegram/`)
- **Rationale**: Follows existing module convention (MODULE.yaml, Module protocol, ModuleLoader). Same pattern as memory, policy, scheduler. Removable without touching core.
- **Category**: Architecture

#### D-161 — Inbound message transport?

`arcagent` · Architecture · from *Feature: Telegram Messaging Module*

- **Choice**: **Long polling only**
- **Rationale**: No HTTPS, no domain, no reverse proxy needed. Works on Mac and AWS. Proactive outbound via `send_message()` works regardless. OpenClaw also defaults to polling.
- **Category**: Architecture

#### D-162 — Module lifecycle?

`arcagent` · Architecture · from *Feature: Telegram Messaging Module*

- **Choice**: **Module owns the polling loop**
- **Rationale**: `TelegramModule.startup()` starts polling as background asyncio task. `shutdown()` stops it. Same pattern as scheduler's `_timer_loop()`. Self-contained.
- **Category**: Architecture

#### D-163 — How does module access agent.chat()?

`arcagent` · Architecture · from *Feature: Telegram Messaging Module*

- **Choice**: **Deferred binding via callback**
- **Rationale**: `set_agent_chat_fn(agent.chat)` wired by agent.py after startup. Same pattern as scheduler's `set_agent_run_fn()`. Zero coupling to agent internals. Module removable without core changes.
- **Category**: Architecture

#### D-209 — Scope of this build

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Bio-memory is default module; markdown-memory becomes simpler alternative
- **Rationale**: New default, existing kept as opt-in alternative
- **Category**: Architecture

#### D-210 — Module mutual exclusivity

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Mutually exclusive via config. `[modules.memory]` = bio-memory, `[modules.markdown-memory]` = alternative. Both enabled = ConfigError.
- **Rationale**: Priority: simplicity. Clear, no ambiguity.
- **Category**: Architecture

#### D-211 — Codebase location

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Bio-memory replaces `modules/memory/`. Existing markdown-memory moves to `modules/markdown_memory/`.
- **Rationale**: Priority: simplicity. Bio-memory IS the default memory.
- **Category**: Architecture

#### D-212 — Agent-team relationship

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Agent memory fully standalone. Team is optional overlay discovered via Module Bus events. No hard dependency.
- **Rationale**: Priority: simplicity. Works solo or with team.
- **Category**: Architecture

#### D-213 — Internal structure

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Facade (BioMemoryModule) + internal helpers: WorkingMemory, IdentityManager, EpisodeStore, Retriever, Consolidator.
- **Rationale**: Priority: simplicity. Matches existing pattern.
- **Category**: Architecture

#### D-214 — Disk layout

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: `{workspace}/memory/` containing working.md, how-i-work.md, episodes/.
- **Rationale**: Priority: simplicity. Under existing workspace convention.
- **Category**: Architecture

#### D-215 — LLM access

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Use existing eval model pattern from `model_helpers.py`. Same [eval] config.
- **Rationale**: Priority: simplicity. Zero new infrastructure.
- **Category**: Architecture

#### D-216 — Module Bus events

`arcagent` · Architecture · from *Bio-Memory (ArcAgent)*

- **Decision**: Map to existing events: assemble_prompt (inject), post_respond (working.md), shutdown (consolidate). No new core events.
- **Rationale**: Priority: simplicity. No core changes.
- **Category**: Architecture

#### D-252 — Module structure

`arcagent` · Architecture · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Mirror Telegram: `__init__.py`, `bot.py`, `config.py`, `MODULE.yaml`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-253 — Socket Mode lifecycle

`arcagent` · Architecture · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `connect_async()` (non-blocking), `close_async()` (clean shutdown)
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: deepened research

#### D-254 — Event handler registration

`arcagent` · Architecture · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `@app.event("message")` not `@app.message()` — catches all subtypes
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: deepened research

#### D-255 — Message processing

`arcagent` · Architecture · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Inline with `asyncio.Lock` — no queue, no background task. Lock serializes overlapping messages
- **Priority**: Simplicity
- **Tier Notes**: Simpler than Telegram's queue pattern

#### D-291 — Parallel tool execution

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Validate sequentially, execute in parallel via `asyncio.gather()`
- **Priority**: simplicity + performance
- **Tier Variation**: None

#### D-292 — Self-modification level

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Skills + tools + extensions (full self-modification)
- **Priority**: extensibility
- **Tier Variation**: Federal: extensions DISABLED. Enterprise: require approval. Personal: all enabled

#### D-293 — Hot-reload mechanism

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Immediate reload on create (no watcher)
- **Priority**: simplicity
- **Tier Variation**: None

#### D-294 — Tool policy pipeline

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: 5-layer execution-time (Global->Provider->Agent->Team->Sandbox)
- **Priority**: security + compliance
- **Tier Variation**: Federal: all 5. Enterprise: 4 (no team). Personal: global only

#### D-295 — Heartbeat / proactive execution

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Unified ProactiveEngine (merge pulse + scheduler)
- **Priority**: simplicity
- **Tier Variation**: Federal: can be disabled. Enterprise/Personal: default enabled

#### D-296 — Session history model

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Keep linear JSONL (no tree branching)
- **Priority**: simplicity
- **Tier Variation**: None

#### D-297 — Loop termination signal

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Structured `task_complete` tool with status/summary/artifacts
- **Priority**: observability + integration
- **Tier Variation**: None

#### D-298 — Turn/step limits

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Configurable defaults (max_turns=100, max_cost=5.00)
- **Priority**: security + simplicity
- **Tier Variation**: Federal: hard caps. Enterprise: auto-approve 2x. Personal: always approve

#### D-299 — ArcLLM bridge wiring

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Wire `on_event` through `load_eval_model()` helper
- **Priority**: simplicity
- **Tier Variation**: None

#### D-300 — httpx client lifecycle

`arcagent` · Architecture · from *Feature: arc-core-hardening*

- **Choice**: Explicit `model.close()` in `ArcAgent.shutdown()`
- **Priority**: simplicity
- **Tier Variation**: None

#### D-338 — Modules are optional shipped bundles, not a runtime concept

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: A module is a folder of capabilities (`.py` + skill folders). Runtime sees only capabilities; `arc module enable/disable/install/uninstall` manages bundles. `MODULE.yaml` is read by the CLI for marketplace metadata, never by the runtime.
- **Priority**: simplicity (one runtime concept), modularity (CLI vs runtime separation)
- **Tiers**: Federal — install requires Sigstore signature. Enterprise — warns on unsigned. Personal — accepts unsigned with info log.

#### D-339 — Tools may declare an optional `requires_skill` field

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Tool frontmatter has `requires_skill: Optional[str]`. When set, runtime auto-attaches the named skill body when the tool is called. Most tools omit this field.
- **Priority**: simplicity (tight tool↔skill couplings made explicit)

#### D-341 — Last-wins on name collisions with audit

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Scan order builtins → global → agent → workspace; later-loaded capability replaces earlier with same name. Audit emitted on every shadow. No special protection for builtins.
- **Priority**: simplicity (consistent rule), modularity (extensibility — user can override builtins)

#### D-342 — Skills are folders, one tier — no quick-vs-full distinction

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Every skill is a folder with `SKILL.md` + optional `references/`, `scripts/`, `templates/`, `assets/`. Required frontmatter and required sections enforced uniformly. Short sections OK; missing sections rejected; filler ("N/A", "none", empty body) flagged.
- **Priority**: simplicity (one mental model), modularity (consistent shape eases discovery)

#### D-343 — `triggers` is a semantic hint, not a runtime matcher

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Frontmatter `triggers: [list of phrases]` is concrete examples for the LLM to recognize when a skill applies. Never matched literally at runtime. Routing remains LLM-driven via description + triggers in the manifest.
- **Priority**: simplicity (no regex/match maintenance), scalability (LLM scales semantically; regex doesn't)

#### D-346 — Skills/tools manifest in system prompt at session start; bodies lazy via `read`

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Manifest XML (extending D-073/D-074) injected into system prompt at session start. LLM picks a skill from the manifest and uses `read` on the listed SKILL.md path. Body is one-shot in conversation memory (not pinned). References pulled lazily by `read` if cited.
- **Priority**: simplicity (existing bus injection pattern), scalability (no per-turn rebuild)

#### D-347 — Skill-usage instruction injected via bus, not hardcoded in prompt builder

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: A short (~3-line) instruction ("scan available-skills, pick the most specific, read its SKILL.md, never read more than one up front") is injected at priority 91 alongside the skill manifest. Lives as a constant in the bus subscriber.
- **Priority**: modularity (identity.md stays user-owned)

#### D-349 — Two file types only — `.py` (decorated) and `.md` (skill folder)

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Capability surface is exactly: Python files with `@tool` / `@hook` / `@background_task` / `@capability`-class decorators; or skill folders containing SKILL.md. Nothing else (no JSON manifests, no YAML configs at runtime). `MODULE.yaml` exists only for `arc module` CLI metadata.
- **Priority**: simplicity (minimum viable type set), modularity (clean kind boundary)

#### D-356 — Lifecycle abstraction — three decorators + one class form

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Four primitives:
- **Priority**: simplicity (each decorator does one thing) > modularity (clean axis: LLM-call vs runtime-call vs lifecycle)
- **Alternatives**: One unified `@capability(kind=...)` decorator (rejected — fields-by-kind is implicit schema, type-checker can't help). Two decorators `@tool` + `@reactive` (rejected — `@reactive` becomes a kitchen sink).

- `@tool(name, description, when_to_use, classification, capability_tags, requires_skill?, version)` — LLM-callable function
- `@hook(event, priority)` — bus subscriber
- `@background_task(name, interval)` — periodic runtime-callable
- `@capability` class with optional `setup()` / `teardown()` — heavy-resource case (browser, memory pool, websocket)
Tools-on-class are written as `@tool` on bound methods. Each primitive maps 1:1 to a distinct runtime behavior.

#### D-366 — state.json is not framework-enshrined

`arcagent` · Architecture · from *Unified Capability System — Build Decisions (2026-04-28)*


Framework writes runtime metadata (version history, last validated, last reload) to `.skill-meta.json` (hidden, framework-owned). Skills create their own state files with descriptive names if they need persistence. No reserved-keys-in-shared-file race.

#### D-396 — Compaction model (arcagent)

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Replace per-turn sliding-window prune with **discrete, persisted, debounced checkpoint compaction**: append-only between boundaries; on threshold cross, compact ONCE, write back a new baseline (`[system][tools][summary][protected tail]`), compact deep (~50%) for hysteresis; emergency truncation stays as rare valve. Trigger off reported tokens where available. **Compaction METHOD (truncate vs summary turn vs structured extraction vs hybrid) is OPEN — under web research.**
- **Priority**: scalability > simplicity
- **Rationale / Tier Notes**: Current `ContextManager.transform_context` (context.py 209-258) rewrites a sliding prefix every turn >70% → busts cache every turn on long-running agents (O(new)→O(full) re-encode). Discrete + persisted + debounced = one cache miss per boundary, then re-warm.

#### D-398 — Compaction method

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: **Hybrid: observation-masking + structured running summary, applied TOGETHER at discrete debounced boundaries, persisted (write-back), append-only between boundaries.** Not continuous, not freeform prose, not recursive.
- **Priority**: scalability > simplicity
- **Rationale**: Masking does cheap bulk token reduction; structured summary supplies cumulative task-awareness masking alone loses (premature-termination failure in the D365 study). Doing both at a boundary keeps the loop fully append-only between boundaries → one cache miss per boundary, not per turn.

#### D-400 — Observation masking policy

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Keep the existing `prune_observations` mechanism but fire it **at the compaction boundary and persist the masked result** (write-back), NOT as a per-turn sliding view. Mask tool outputs older than a fixed recency window (~N=5–10 tool calls); keep tool-call name/args visible.
- **Priority**: scalability
- **Rationale**: This is the direct fix to the current per-turn cache-buster (context.py 209-258). Persisting + batching at a boundary makes the masked prefix stable and cacheable; keeping call metadata preserves provenance (JetBrains finding).

#### D-401 — Large/durable content offload

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Large tool outputs and any cross-session state go to workspace files / memory (restorable compression); keep only pointers (path/id) in live context. Route "must survive across resets" through the memory/file layer, never through in-context summarization.
- **Priority**: simplicity > scalability
- **Rationale**: Manus restorable-vs-irreversible compression; Anthropic's own long-running-agent study: compaction alone is insufficient, durable file artifacts carry correctness. Cache-neutral (append pointers, never mutate prefix).

#### D-402 — Emergency truncation

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Keep `_emergency_truncate` as a rare last-resort floor only (hard ceiling breach), also a discrete persisted boundary event. Never the primary mechanism.
- **Priority**: security (availability) > simplicity
- **Rationale**: Truncation is semantically blind; acceptable only to prevent a crash after masking+summary+offload have run.

#### D-405 — Compaction trigger = estimate, not reported tokens

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: `maybe_compact` uses `session.context_ratio()` = `ContextManager.message_fill_ratio(live messages)`.
- **Rationale**: `update_reported_usage` has zero production callers → `token_ratio()` always 0.0 → compaction never fired. The reported accumulator is also cumulative-and-never-reset (would thrash). The estimate over current messages is the honest signal and drops after a boundary (natural debounce). `token_ratio`/`update_reported_usage` kept as a telemetry surface.

#### D-407 — Emergency valve keeps newest

`arcagent` · Architecture · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: `_emergency_truncate` always keeps >=1 (the newest) message.
- **Rationale**: Prior loop returned `[]` when a single newest message exceeded budget → zero messages to provider.

#### D-408 — Notes trigger

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: Decouple from compaction entirely; remove `memory_pre_compaction`.
- **Rationale**: MemGPT coupled memory to context pressure; Letta sleep-time compute exists to undo exactly that. Memory quality must not be hostage to token pressure.

#### D-409 — Capture cadence

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: Per-turn RAW append, no LLM, on `post_respond` (before background extract).
- **Rationale**: "ASSUME INTERRUPTION" (Anthropic memory tool) — sessions don't end cleanly; cheap append is the crash-safety floor. LLM-per-turn is the rejected anti-pattern (LangMem hot-path).

#### D-410 — Enrichment

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: Keep existing `entity_extraction_loop` (interval background).
- **Rationale**: Off-critical-path LLM work = Letta sleep-time latency isolation; already present.

#### D-411 — Session consolidation

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: One eval-model dedupe/tidy pass on `agent:shutdown`, fail-open.
- **Rationale**: The guaranteed floor for one-shot runs (Reflexion per-episode; Anthropic harness update-at-session-end).

#### D-412 — Day rollup

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: LAZY on new-day file creation → `spawn_background(rollup(prev))`, `.rolled` marker for idempotency.
- **Rationale**: Dual-mode (daemon crossing midnight OR next-day one-shot) with no live scheduler — the gap the research flagged as unsolved. Generative Agents daily reflection + practitioner nightly rollup.

#### D-413 — Deferred

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: No importance-scoring, no wall-clock heartbeat (v1).
- **Rationale**: YAGNI: session-end + new-day boundaries suffice; 1.0s loop bounds intra-turn loss.

#### D-415 — Drop `rollup_compacts_source`

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: Never rewrite the raw source day. Removes the crash-retry summary-of-summary ordering hazard AND the identical yesterday double-count AND a YAGNI flag in one deletion.

#### D-417 — Drain the full backlog

`arcagent` · Architecture · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: `_unrolled_prior_days` rolls ALL un-rolled prior days (oldest-first), not just the newest — no starvation.

#### D-489 — Home vs working dir

`arcagent` · Architecture · from *Coding-agent working directory — Build Decisions (2026-07-26)*

- **Choice**: Split them: a `working_dir` = where the LLM's file/exec tools operate (bash cwd + relative-path root), distinct from the `workspace` = the agent's home. Defaults to workspace (every existing agent unchanged).
- **Priority**: modularity > simplicity
- **Rationale**: Lets one agent work across many projects without moving its brain. The OpenCode model. (ADR-029)

#### D-506 — Engine home

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: arcteam owns the workflow engine (models, validator, runner, run coordination); arcagent gets only a thin tool module exposing workflow_* tools that call into arcteam.
- **Priority**: modularity
- **Alternatives**: arcagent module owns everything; new arcflow package
- **Rationale**: Josh: workflows ARE multi-agent coordination and arcteam owns the coordination layer; arcflow is a narrowed multi-agent coordination. Layering stays clean: the runner needs only arcstore + messaging, never arcagent.

#### D-507 — Execution substrate

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: A run instantiates onto the existing tasks module + arcstore task DAG (blocked_by/deps_met/claim/retry/review). No third DAG engine.
- **Priority**: simplicity
- **Alternatives**: extend modules/planning; new bespoke executor
- **Rationale**: The task layer already owns atomic claims, dependency gating, retries, dead-letter, review gates, and cross-agent handoff; planning stays for ad-hoc self-decomposed goals.

#### D-508 — Run progression

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: A dedicated fleet runner: deterministic arcteam code hosted in the existing arc service process. No orchestrator agent; models never sequence.
- **Priority**: security
- **Alternatives**: owning agent hosts the runner; any participant advances runs
- **Rationale**: One authoritative progressor for cross-agent graphs; deterministic control flow with LLM calls confined to journaled nodes (durable-execution research consensus; app-store D-004/D-022/D-024).

#### D-509 — Vocabulary and node kinds

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Graph units are nodes (not stages). Kinds: agent (LLM step w/ optional skill + strategy), tool (API calls are tools), script, router, gate.
- **Priority**: simplicity
- **Alternatives**: stage vocabulary per app-store docs
- **Rationale**: Josh's call; app-store docs adopt [[node]] when next touched.

#### D-510 — Multi-agent scope

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Multi-agent in v1: every node names its executing agent.
- **Priority**: modularity
- **Alternatives**: single-owner v1, multi-agent v2
- **Rationale**: The point of the feature is coordinated multi-agent processes; the task substrate already supports cross-agent assignment with signed DMs.

#### D-511 — Handoff transport

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Dispatch stays task + signed DM (atomic, no per-member triage cost); every transition is narrated to the workflow's group channel; narration never wakes a run.
- **Priority**: security
- **Alternatives**: handoff by channel broadcast
- **Rationale**: Broadcast dispatch burns a classification call per member and gives up atomicity; narration gives the public visibility Josh wants without the tax (app-store D-026).
- **Note**: the phrase "task + signed DM" was ambiguous about which one *is* the handoff — [clarified by D-538](#d-538-handoff-is-a-task-write-never-a-message).

#### D-512 — Authoring surfaces

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Three surfaces from day one — IDE (hand-edit TOML), conversation (validated builder tools guided by a shipped workflow-builder skill), arcui editor — all converging on one artifact and one validator; every surface produces unsigned drafts.
- **Priority**: modularity
- **Alternatives**: conversation-only v1; LLM emits raw TOML
- **Rationale**: n8n lesson: constrain generation to validated builder tools emitting the canonical schema so agent-authored and human-authored converge on the same code path.

#### D-538 — Handoff is a task write, never a message

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: The handoff between workflow nodes IS the task-row write that sets the next node's owner. A signed DM may follow only as a wake signal — it never contains the work, and a lost DM costs latency only because the owning agent's dispatch loop finds the row regardless. No run's progress may depend on a message being delivered, read, or acted upon. Messaging in a workflow is narration and signalling only.
- **Priority**: security
- **Alternatives**: handoff carried in message bodies; handoff by channel post
- **Rationale**: Four properties a task row has and a message does not — (1) it **forces action**: a task must be claimed and driven to a terminal state, while a message can be read and ignored with no trace; (2) it **retries**: attempts, backoff, timeout and dead-letter already exist per row, whereas delivery is fire-and-forget; (3) it **has history**: a durable queryable audited row records what was handed off and what came back, where a chat line is a story, not a state machine; (4) it **directs ownership**: `owner_did` plus the atomic claim guarantees exactly one named executor, where in a channel anyone may answer, several may, or none. Clarifies D-511 rather than reversing it — dispatch was always task-first; this removes any reading in which the DM carries the work.

#### D-539 — A run is an office, not a data bus — the shared run workspace

`arcagent` · Architecture · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Every run gets a shared workspace at `<team_root>/shared/runs/<run_id>/`, alongside the team shared-files area that already exists. Each node's agent works there for its node and keeps its own private workspace for its own state. Work product lives as files in that shared folder; a node's typed `output` carries only the facts the graph needs (router inputs, the did-you-do-it contract); an agent's memory and identity stay home (ADR-029). `artifacts` are paths relative to the run workspace.
- **Priority**: simplicity
- **Alternatives**: marshal work product through task-row metadata and message payloads; per-node output blobs in the store
- **Rationale**: Josh's framing — people working a process either collaborate on a shared folder or keep their own files, agents already do work, and a workflow only controls order and assignment. Inventing a transport for work product is the part that would have made this feature big. Three consequences fall out: no size or encoding decisions for bulk output; the "trust the filesystem, not the report" check is just looking in the shared folder; and path confinement stops being a guard someone must remember, because the workspace IS the boundary. That last point retires a whole defect class — an untested confinement guard on agent-supplied file paths was the most serious bug found during implementation, and this design removes the need for the guard rather than testing it harder.

#### D-546 — What a connector extension is

`arcagent` · Architecture · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: A signed bundle per connector: extension.toml (server command, pinned version and hash, tool allowlist, required secrets, tier floor) plus skills, plus a SIGNATURE. A small adapter.py ships ONLY for services where no acceptable MCP server exists. `arc ext add <name>` probes the server and verifies the signature before saving anything.
- **Priority**: simplicity
- **Alternatives**: Hermes model: skill folder whose SKILL.md tells the agent to paste an mcp_servers config block (nothing signed, nothing probed, secrets land in config); OpenClaw model: two separate registries, code plugins vs config-only MCP servers (proven, but two install stories to maintain); Fold connectors into blueprints (no new concept, but cannot add Jira to a running agent without editing its blueprint)
- **Rationale**: Keeps the common case non-technical configuration while still handling services with awkward auth. Signing and probe-before-save close the two holes visible in the Hermes pattern.

#### D-547 — Where the shared MCP client lives

`arcagent` · Architecture · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: An optional arcagent MODULE at arcagent/modules/mcp/, gated by [modules.mcp] in the agent's toml. Removable without breaking the agent; when present, extensions register their servers into it. One DRY MCP path, per-extension adapters.
- **Priority**: modularity
- **Alternatives**: New leaf package arcmcp (standalone-installable, but a package the agent cannot simply drop); arcagent/tools/mcp/ (buries a protocol client in the nucleus and pushes the core LOC budget); In arcrun beside the loop (breaks concern purity — arcrun must not own agent integration); Vendor the official MCP Python SDK (violates CON-7, no vendor SDKs, and moves protocol handling outside the audit envelope)
- **Rationale**: Josh's requirement was that MCP be ignorable and removable without the agent breaking. Matches CLAUDE.md §Simplicity: complexity lives in modules, never the nucleus.

#### D-548 — How connector tools surface to agent and policy

`arcagent` · Architecture · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: At module load the server's tools are listed, filtered to the manifest allowlist, and each registered as a first-class named capability (jira.create_issue, gmail_work.send). Policy, audit, and approval gates all see the real verb.
- **Priority**: security
- **Alternatives**: One generic mcp_call(server, tool, args) tool (tiny surface, but policy and audit see only 'mcp_call' so no per-verb deny is possible); Register names from the manifest without contacting the server at boot (fast cold start, but manifest can drift from reality)
- **Rationale**: OWASP ASI02/LLM06. A generic dispatch tool makes the policy pipeline blind; named tools make `DENY jira.delete_*` expressible and make every audit record self-describing.

#### D-566 — Extensions live outside Arc and plug into hooks

`arcagent` · Architecture · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: An extension is entirely outside Arc — a self-contained, pluggable unit carrying its own implementation and its own dependencies. Arc provides hooks it attaches to and nothing else. arcagent contains ONLY what every extension reuses: the hooks. Nothing service-specific enters arcagent, with one standing exception — the connector/adapter class itself, as the Slack and Telegram gateway adapters already are. The 1Password SDK client and the Jira/Confluence REST client live inside their own extension packages, never in arcagent. INVARIANT, both directions: remove any or all modules and Arc still runs, minus those features; remove any or all extensions and Arc still runs; a new module attaches the same way; an outside party can build an extension against the same hooks with no change to core.
- **Priority**: modularity
- **Alternatives**: MCP only, dropping anything that does not fit (purest spine thesis, but drops 1Password entirely and forces Atlassian onto a rejected upstream); Finish the PROCESS transport at the same time so a vetted CLI like gogcli is first-class (most flexible, but finishes two dead transports at once); Per-service code inside arcagent behind a shared interface (fewer packages, but every new service edits core and removability dies)
- **Rationale**: Josh's ruling, in his words: extensions need to be extensions, entirely outside our system and able to be plugged in, and we just allow certain hooks or ways to interact with the agent. THE MECHANISM IS THE DELIVERABLE, NOT THE CONNECTORS — "I don't want to tailor it to those connections, I want to build it in a way where we can add most/any other ones in the same manner." The ten named services are test cases chosen to cover the real shapes (hosted and local, MCP and direct API, OAuth and service account, read-mostly and write-heavy), not the design target. GOVERNING TEST: the eleventh connector must be addable without touching Arc. If adding one requires a core change, the mechanism is wrong and the fix belongs in the hooks, never in a special case for that service. Core never learns a vendor's name. Satisfies CLAUDE.md §Composability — dependencies point one way — and keeps the core LOC budget flat as connectors are added, because connectors add packages rather than core code.

#### D-567 — What goes where — the three-way placement rule

`arcagent` · Architecture · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Placement is decided by what the thing IS, not by which connector it belongs to. (1) Something the MACHINE needs — a runtime, a binary, a system package — the extension DIRECTS THE INSTALL on the host running Arc (the DGX or server). (2) SKILLS AND TOOLS the agent uses are loaded as agent tools and skills in the agent's CAPABILITY FOLDER, through the paths that already load capabilities. (3) EVERYTHING ELSE — implementation code, SDK clients, vendored dependencies — stays in the EXTENSION FOLDER and is referenced from the agent's tools. Nothing in category 3 is ever copied into core or into the agent's capability folder.
- **Priority**: modularity
- **Alternatives**: One location for everything an extension ships (simplest to explain, but a system runtime cannot live in a capability folder and implementation code in the capability folder would be loaded as agent capabilities); Core installs system prerequisites on the extension's behalf (one installer to audit, but core would then know about specific connectors, breaking D-566)
- **Rationale**: Josh's ruling. Each category has a different owner and a different lifetime: the host owns machine-level installs, the agent owns its capabilities, the extension owns its own code. Splitting on that boundary is what makes removal clean — deleting an extension deletes category 3 entirely, leaves category 2 to the capability loader's normal unload, and leaves category 1 as an explicit host-level action the operator can see. Recorded in ADR-030.

#### D-568 — ADR-018's MCP exclusion is reversed

`arcagent` · Architecture · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: ADR-030 supersedes the MCP-client exclusion in ADR-018. Agents get MCP capability. ADR-018 remains Accepted for migration tooling and the ACP adapter, whose reasoning is unchanged. ADR-018's status line and its MCP "Reconsider when" trigger are updated in place to point at ADR-030.
- **Priority**: simplicity
- **Alternatives**: Leave ADR-018 untouched and let ADR-030 contradict it silently (less editing, but two accepted ADRs would disagree and a future reader could not tell which governs); Mark all of ADR-018 superseded (cleaner status line, but wrongly reopens migration tooling and ACP, which nobody has asked for)
- **Rationale**: The trigger ADR-018 named for MCP was explicit customer demand. What actually fired was different and should be recorded honestly: research across all ten target services found that the maintained integrations ARE MCP servers, so ADR-018's own escape hatch — "the community can write an adapter" — resolves in practice to "adopt MCP". Two further facts postdate ADR-018: the 2026-07-28 spec revision made MCP stateless, cutting a correct client to roughly 150-400 LOC, and the registry already models the transport, so this finishes a half-wired path rather than opening a new one.

#### D-632 — Every module is optional; none ship in the wheel

`arcagent` · Architecture · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: All eighteen modules are optional. The `arc-agent` wheel ships the
  nucleus and builtin tools and no modules at all; every module is delivered as a
  signed bundle and materialized into `${ARC_CONFIG_DIR:-~/.arc}/modules/<name>/` —
  the deployment module root, installed once per box and shared by every agent on it.
  Not installed means the folder does not exist. There is no second category of
  always-present module and therefore no line to argue about.
- **Alternatives**: Split modules into always-bundled and optional (a working agent
  straight out of `pip install`, but it creates a permanent argument about which side
  each module sits on, and the bundled set is never exercised as absent); one
  distribution per module (real absence, but eighteen packages to version, sign, and
  release in lockstep with a weekly-changing core); post-install prune of
  site-packages (no new packages, but mutates an installed wheel, breaks RECORD
  hashes, and is undone by the next upgrade); extras only, files always present
  (smallest change, but the source is on disk in the enclave and an auditor scanning
  for capability finds it).
- **Rationale**: `pip` writes every file in a wheel; a flag cannot install a subset.
  Real absence therefore requires the code to arrive from somewhere other than the
  arcagent wheel. Materializing a signed tree into the workspace reuses the road
  `capabilities/capability_loader.py` and blueprint v2 materialize already run, so
  this is an existing mechanism applied to modules rather than a new one. Making
  *every* module optional was checked against the code before being adopted: nothing
  in `core/` imports a module or requires one to exist. The single hardcoded module
  name in core (`core/tool_policy_bridge.py`) is a tool-name prefix list for
  caller-DID binding, which simply never matches when the module is absent.

#### D-633 — Modules live at the deployment root; the in-tree directory is a source catalog

`arcagent` · Architecture · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `_MODULES_DIR` moves from the installed package to
  `${ARC_CONFIG_DIR:-~/.arc}/modules/`. That is a deployment-level directory: outside
  every agent workspace, outside every agent config dir, one copy per box.
  `packages/arcagent/src/arcagent/modules/` stays exactly where it is in git but
  becomes a *source catalog* that the release build packages into bundles and the
  wheel excludes; it is never a scan root. Folder-presence remains the only discovery
  signal and config remains the only activation signal.
- **Alternatives**: Materialize into each agent's workspace (per-agent module sets
  fall out for free, but ADR-029 reserves the workspace for agent *state*, and a
  module tree inside a directory the agent can write to is a self-modification path
  no signature closes); materialize the whole module into each agent's config
  dir (out of the workspace, but N agents means N copies of the background loops and
  hook handlers, where divergence is a defect rather than the feature it is for tools
  and skills — see D-648); scan both the in-wheel directory
  and the deployment root (handles a source checkout with no install step, but the
  in-wheel root is empty in every real deployment, so the materialize path would be
  the one nobody exercises locally).
- **Rationale**: Folder-presence discovery already gives absence for free — a module
  with no folder is not discovered and cannot load. Moving the root rather than adding
  one preserves that property, keeps `active_modules()` the single seam both the load
  path and `arc module list` agree on, and leaves exactly one discovery path that dev,
  personal, and federal all run. Deployment-level placement costs nothing in
  per-agent control: which modules an agent loads has always been decided by its
  `[modules.NAME]` config entries, never by which files sit near it.

#### D-634 — Module runtimes load by path, not by import name

`arcagent` · Architecture · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `configure_module_runtimes` stops calling
  `importlib.import_module(f"arcagent.modules.{name}._runtime")` for materialized
  modules and loads them through `spec_from_file_location` against the resolved
  folder. Bundled modules take the same path, resolved to the in-wheel directory,
  so there is one loader rather than two.
- **Alternatives**: Add the workspace module root to `sys.path` and keep import-by-
  name (a two-line change, but puts an agent-writable directory on the import path
  for the whole process, which shadows arbitrary modules); branch on origin and keep
  both loaders (no new mechanism, but the bundled path becomes the only one anyone
  exercises locally and the materialized path rots).
- **Rationale**: Import-by-name requires the code to live under the installed package,
  which is the thing D-632 removes. Path loading is what `capability_loader.py`
  already does, and one code path everywhere is the same rule D-564 applied to
  sandbox policy.

#### D-630 — A nonterminal plan iteration must make monotonic progress

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: If a ready planning frontier produces zero executable outcomes,
  the orchestrator records a typed step failure and enters its bounded failure
  path; it never resets the same frontier and retries indefinitely.
- **Rationale**: Budget reservation and aggregate exhaustion can disagree at
  dimensional edge cases. Progress must be an explicit loop invariant rather
  than an assumption shared by separate budget predicates.

#### D-643 — ArcAgent lifecycle transitions are serialized states

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Each ArcAgent instance moves through explicit
  STOPPED/STARTING/STARTED/STOPPING states under one lifecycle lock. Repeated
  startup is idempotent and failed startup returns to a restartable STOPPED state.
- **Rationale**: A boolean allows duplicate initialization and makes partially
  constructed states indistinguishable from a usable agent.

#### D-644 — Shutdown owns and bounds every agent resource

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Shutdown rejects new work first, cancels active runs, drains
  finalizers, and tears down each owned service under an independent deadline;
  one failing stage never suppresses later cleanup.
- **Rationale**: Linear teardown leaks everything after the first exception and
  can orphan execution loops that continue mutating state after shutdown.

#### D-647 — One coordinator serializes complete turns per session

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: A per-agent SessionRunCoordinator owns both identity-guarded live
  handle registration and a lock keyed by canonical session id. A turn holds the
  lock from history read through assistant commit; different sessions remain
  concurrent and steering reads the active registry without taking the lock.
- **Rationale**: Protecting individual appends does not prevent two full turns
  from reading the same prefix or committing responses out of order.

#### D-656 — Multi-spawn scheduling reserves only work that can run

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: `spawn_many` uses a bounded worker queue with a fail-fast stop
  boundary, caps batch/concurrency inputs, drains every worker, and atomically
  settles each reservation to actual usage or zero.
- **Rationale**: Pre-creating one task per child allows semaphore waiters to
  start after failure and strands reservations when cancellation arrives.
  Scheduling ownership and budget ownership must share the same lifetime.

#### D-657 — Capability reload publishes only complete candidates

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Reload scans and validates an isolated candidate, then commits
  registry tools and token-owned bus subscriptions as replaceable batches with
  rollback. No live state is removed during preparation.
- **Rationale**: Unregister-first reload turns a recoverable syntax/import error
  into an outage, while identity-only hook deduplication keeps stale code and
  priority alive after a successful source change.

#### D-658 — One structured primitive owns child-run semantics

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Programmatic and tool-driven child runs both execute through
  `spawn() -> SpawnResult`; the tool adapter only resolves user-facing inputs,
  applies its shared envelope, and formats the structured result.
- **Rationale**: Two implementations had drifted on identity, depth, budgets,
  audit, timeout, strategy, and error behavior. One policy-bearing primitive
  makes those outcomes equivalent and testable.

#### D-660 — Every detached task has one draining owner

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: An agent-owned background supervisor creates, observes, reports,
  cancels, and drains detached capability loops, event bridges, and run
  finalizers; local paired tasks follow the same cancel-and-gather rule.
- **Rationale**: A done callback that only discards a task loses exceptions, and
  shutdown that does not await owned work permits post-shutdown mutation and
  destroyed-pending-task warnings.

#### D-663 — Periodic services share one stoppable cadence contract

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Owned periodic services use `PeriodicRunner` with event-based
  stopping, explicit first-tick/cadence configuration, and a declared failure
  threshold plus bounded backoff.
- **Rationale**: Independent boolean/sleep/while-true loops drift on shutdown,
  cancellation, retries, and timing. One runner makes immediate stop and error
  policy deterministic without forcing genuinely one-shot polling into it.

#### D-664 — Module runtimes declare typed dependency contracts

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Each supported runtime has an explicit spec over a closed set of
  typed dependencies and produces named `RuntimeBinding` objects; required and
  optional failure behavior is declared by that spec.
- **Rationale**: Filtering a service dictionary by parameter names makes a
  rename silently remove a dependency and lets arbitrary modules request
  authority by spelling a name. Explicit contracts make drift and signer access
  reviewable and fail required startup predictably.

#### D-666 — Large facades split only at established domain seams

`arcagent` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Responsibility concentration is reduced by extracting named
  domain owners—configuration loading, reload state, task routing, security
  custody, workflow tools, connector policy/attachments/credentials/catalog,
  and spawn observability—behind stable existing facades.
- **Rationale**: Line-count-only splitting creates generic utility buckets and
  circular dependencies. Established behavioral seams preserve discoverability,
  public compatibility, and one-way ownership while enabling later local work.

### arcmemory

#### D-494 — Home: an evaluations/ folder, not a package

`arcmemory` · Architecture · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: evaluations/ at the arc repo root, with evaluations/longmemeval/ beneath it. Plain scripts. No pyproject, no package, no install, no entry in the uv workspace. Nothing under packages/ imports it, so the dependency DAG is untouched and the framework never learns it exists.
- **Priority**: Simplicity
- **Alternatives**: A standalone sibling repo (rejected: re-solves installing 5+ fast-moving arc packages by hand for no benefit). A packages/arcbench/ package (rejected: puts a data-ingestion tool inside the framework's dependency DAG and buys quality gates the scripts do not need).
- **Rationale**: The scripts are consumers of an already-built system. Living in-repo means they import arcmemory and arcagent from the workspace that is already installed, with zero install ceremony. 'The framework must not know about it' is satisfied by direction of dependency, not by physical distance.

#### D-495 — Ingest granularity: session as one call, chunked on turn boundaries

`arcmemory` · Architecture · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: One haystack session is fed as one logical unit. When the transcript exceeds MemoryConfig.max_event_chars (default 2000), it is split on turn boundaries into successive chunks and fed in order. Never split mid-turn.
- **Priority**: Simplicity
- **Alternatives**: Turn-by-turn replay (rejected: 5-10x the calls for granularity the session unit already preserves). Raising max_event_chars via backend.dynamics (rejected: chunking needs no config override at all). Leaving the 2000 cap with unchunked sessions (rejected: sanitize() silently truncates, so most evidence turns would be cut and the run would measure the cap, not the memory).
- **Rationale**: Verified: FastCapture calls sanitize(text, max_length=cfg.max_event_chars) with a 2000-char default, so an unchunked session is silently truncated. Chunking on turn boundaries keeps every evidence turn intact with no framework or config change. Feeding the transcript as text also means the dataset's real assistant turns land in memory as content, so single-session-assistant evidence is preserved rather than replaced by anything the agent invents.

#### D-496 — Ingest path: a real ArcAgent turn per chunk

`arcmemory` · Architecture · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: Each chunk is fed via a real ArcAgent.run(). Full turn machinery: module bus, hook dispatch, memory recall at assemble_prompt, capture_user, the model call, and capture_respond. The agent's own reply is captured into the store as it would be in production. No framework change.
- **Priority**: Modularity
- **Alternatives**: Calling brain.capture() directly (rejected by the operator despite being byte-identical to the hook at capabilities.py:231, and free of both LLM cost and synthetic events). Adding a capture-only ingest entry to arcagent (withdrawn: framework change). Adding a capture_respond=false config flag (withdrawn: framework change).
- **Rationale**: Operator's explicit call: the system must be exercised through the same surface a live agent uses, and the agent path is the product. Accepted costs, stated plainly: roughly one LLM call per chunk (~4,000 on the sampled S run, ~40,000 on full S), a memory recall pass per chunk, and one model-commentary event per chunk sitting in the store alongside real evidence where it can compete at recall time.

#### D-497 — Source adapter seam: one read() contract, longmemeval first

`arcmemory` · Architecture · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: Each source is one module exposing a read() that yields sessions of ordered turns with a source timestamp and a conversation id. The chunker, the agent driver, and the consolidation waiter are shared and source-agnostic. Only the longmemeval adapter is built now; email and Slack attach to the same seam later without touching the pipeline.
- **Priority**: Modularity
- **Alternatives**: Building email and Slack adapters now (rejected: YAGNI, and the seam is unproven until a second source exists). Emitting arcmemory Event objects directly (rejected: couples adapters to an internal type and skips the FastCapture security boundary).
- **Rationale**: The operator's stated goal is one pathway into memory with many attachments. The pathway is the shared chunk-drive-consolidate loop; the attachment point is read(). Keeping the seam to a single method means a new source is one file and no pipeline edit.

### arcprompt

#### D-459 — Prompt load model

`arcprompt` · Architecture · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: arcprompt is a leaf package exposing load(pkg, name). Each package ships stock prompts as markdown at src/<pkg>/context/*.md. Resolution walks overlay then stock.
- **Priority**: simplicity
- **Alternatives**: central registry with import-time registration; arcstore DB-backed prompts
- **Rationale**: Keeps prompts in git, so version control is inherited rather than built. Mirrors how SKILL.md already loads from disk. Leaf position is forced by the dependency DAG: arcrun, arcskill, arcagent and arcmemory all consume prompts, so arcprompt must sit at arctrust's level and never import upward. No DB dependency on a core prompt path.

#### D-460 — Overlay scope

`arcprompt` · Architecture · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Two layers only: packaged stock, then a per-agent overlay. No fleet-wide layer.
- **Priority**: simplicity
- **Alternatives**: stock → fleet → agent (three layers); fleet-wide overlay only
- **Rationale**: Shared-nothing per agent, consistent with the architecture's isolation model and with the cross-agent memory-bleed lesson. Resolution stays a single conditional. Cost accepted: a fleet-wide prompt change is N edits; revisit only if that friction proves real.

#### D-461 — Reload semantics

`arcprompt` · Architecture · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Resolve and snapshot the prompt set at run start; the snapshot is fixed for every turn of that run. Edits take effect on the next run.
- **Priority**: security
- **Alternatives**: re-read per call like identity.md; explicit operator-triggered reload only
- **Rationale**: Per-call re-reads let a prompt shift between turns of one run, so no single version is attributable as the cause of a behavior. Pinning makes every run attributable to exact prompt bytes, which is what makes the audit record meaningful. Mirrors the existing frozen-tool-registry-per-run precedent. Hot-reload is preserved from the operator's seat — no redeploy, just next run.

#### D-463 — Resolution failure mode

`arcprompt` · Architecture · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Absent overlay resolves to stock silently (the normal path). Overlay present but unparseable, empty, or unsigned raises. Missing stock prompt raises as a packaging bug.
- **Priority**: security
- **Alternatives**: always fall back to stock with a warning; fail closed on any resolution gap including absent overlay
- **Rationale**: Mirrors the configured-gate policy rule: unconfigured is a no-op, configured-but-broken is a hard failure. Silent fallback is the embedder silent-degrade shape — a warning nobody reads while the fleet runs the wrong prompts. An operator who wrote an overlay intended to change behavior and must learn immediately that they did not.

### arcteam

#### D-093 — Team Memory Core Concept

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Shared knowledge base — team-level persistent store with shared context, notes, entity index. Members contribute, coordinator curates. Independent from per-agent memory.
- **Priority**: Simplicity — cleanest separation of concerns, no coupling to arcagent memory internals.
- **Alternatives**: Message-derived memory (rejected: implicit, hard to debug), Federated agent memory (rejected: couples to agent internals), Hybrid (rejected: too complex for Phase 1).
- **Tiers**: All tiers same.

#### D-094 — Storage Location

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Reuse existing `StorageBackend` for decisions JSONL. New `MemoryStorage` layer for entity markdown files.
- **Priority**: Simplicity — consistent with messaging pattern for structured data, proper markdown handling for entities.
- **Alternatives**: All through StorageBackend (rejected: muddies JSON protocol with markdown), All new storage (rejected: duplicates existing patterns).
- **Tiers**: All tiers identical. Encryption at rest handled by policy layer.

#### D-095 — Storage Format

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Entity files are actual `.md` files on disk with YAML frontmatter. New `MemoryStorage` class handles markdown+YAML I/O. `StorageBackend` stays for messaging/JSON only.
- **Priority**: Simplicity — matches design doc exactly, human-readable, git-diffable.
- **Alternatives**: Adapt StorageBackend for markdown (rejected: muddies protocol), JSON with markdown rendering (rejected: breaks "read the file" philosophy).
- **Tiers**: Federal adds encryption-at-rest wrapper.

#### D-096 — Service Shape

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Single `TeamMemoryService` class parallel to `MessagingService`. Methods for entities, playbooks, decisions, search. Consolidation is a separate `ConsolidationEngine` class.
- **Priority**: Simplicity — discoverable API, one place to look.
- **Alternatives**: Split into EntityGraph + PlaybookStore + DecisionLog (rejected: too many classes), Plugin architecture (rejected: over-abstracted for a file store).
- **Tiers**: Same class all tiers. Policy layer gates writes.

#### D-097 — Consolidation Trigger

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: First agent to start a session checks `.last_consolidated` timestamp file. If stale (>24h configurable), runs consolidation with file lock to prevent concurrent runs. Zero daemon, zero cron.
- **Priority**: Simplicity — no background processes, no scheduling infrastructure, self-healing.
- **Alternatives**: Post-session hook (rejected: "last agent" detection is fragile), Activity-based escalation (rejected: more complex for marginal benefit).
- **Tiers**: Federal — consolidation mandatory, blocks session start until complete. Enterprise — runs async, agent proceeds. Personal — optional via config.

#### D-098 — LLM Integration

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Direct arcllm dependency. arcllm handles provider routing, budgets, model selection. arcteam config has optional `consolidation_model` — if set, passed to arcllm; otherwise arcllm default.
- **Priority**: Simplicity — no protocol abstraction layer. arcllm IS the abstraction.
- **Alternatives**: Callable protocol (rejected: unnecessary when arcllm already handles routing), Event-based (rejected: over-engineered).
- **Tiers**: All tiers use arcllm. Federal gets FIPS-compliant providers via arcllm's tier config.

#### D-099 — Promotion Gate Location

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: `TeamMemoryService.promote()` method in arcteam. Agent decides to promote, calls the method, arcteam validates/audits/writes.
- **Priority**: Simplicity — one method call, clear ownership, audit trail in arcteam.
- **Alternatives**: Message-based (rejected: async, harder to confirm), Bridge in arcagent (rejected: arcagent shouldn't understand team storage format).
- **Tiers**: Federal — requires classification label, blocks without it. Enterprise — warns if missing. Personal — no enforcement.

#### D-100 — Wiki-Link Resolution

`arcteam` · Architecture · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: `_index.json` manifest maps entity_id to relative path. O(1) lookup. Rebuilt on write/delete.
- **Priority**: Simplicity — fast, deterministic, already in design doc.
- **Alternatives**: Flat namespace (rejected: loses organizational structure), Glob search (rejected: O(n) per resolve).
- **Tiers**: Federal adds integrity checksum on index load.

### arcui

#### D-004 — Agent discovery/connection

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Explicit TOML config `[ui]` section with url and token
- **Priority**: simplicity
- **Tier Notes**: Federal: validates wss:// for non-localhost. Same otherwise.

#### D-005 — Disconnect resilience

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Reconnect + buffer. Exponential backoff (1s→60s cap), bounded deque (1000), flush on reconnect
- **Priority**: simplicity
- **Tier Notes**: Federal: disconnect/reconnect audited.

#### D-006 — UI ↔ arcteam messaging

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Agents relay their own team messages through UI WebSocket
- **Priority**: simplicity
- **Tier Notes**: No direct arcteam dependency in arcui.

#### D-007 — Event layer taxonomy

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: 4 layers: llm, run, agent, team
- **Priority**: simplicity
- **Tier Notes**: Maps 1:1 to packages.

#### D-008 — UIReporter ownership

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: arcagent module at `arcagent/modules/ui_reporter/`
- **Priority**: simplicity
- **Tier Notes**: Opt-in via config. Follows existing module pattern.

#### D-009 — UI process launch

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: New `arc ui start` CLI command. Standalone process.
- **Priority**: simplicity
- **Tier Notes**: `--ui` on agent means "connect to UI". Old embedded mode removed.

#### D-010 — Control plane transport

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Bidirectional WebSocket. Same connection for events and control.
- **Priority**: simplicity
- **Tier Notes**: Federal: control commands signed.

#### D-011 — UI agent registry

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: In-memory. Agents reconnect on UI restart. No persistence.
- **Priority**: simplicity
- **Tier Notes**: Federal: connection events in separate audit log.

#### D-012 — Browser event filtering

`arcui` · Architecture · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Server-side subscription filters. Browser sends subscribe message with agents/layers/teams.
- **Priority**: scalability
- **Tier Notes**: Federal: subscription changes audited.

#### D-037 — TraceStore location

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: ArcLLM
- **Options Considered**: ArcLLM / ArcUI / Shared utils
- **Rationale**: Data belongs closest to where it's generated. Traces persist without arcUI running.

#### D-038 — TraceStore backend design

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Protocol + implementations (JSONLStore, SQLiteStore)
- **Options Considered**: Protocol + impls / Single class / JSONL-only
- **Rationale**: Follows ArcLLM's adapter pattern. We know SQLite is coming. 4-method Protocol is minimal.

#### D-039 — Real-time event hook

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: `on_event` callback on `load_model()`
- **Options Considered**: on_event callback / Internal EventBus / OTel SpanProcessor
- **Rationale**: Matches ArcRun's pattern. Optional, zero overhead when unused. ArcAgent bridge forwards to ModuleBus.

#### D-040 — ConfigController location

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: ArcLLM
- **Options Considered**: ArcLLM / ArcUI / ArcAgent
- **Rationale**: Owner provides API. Standalone ArcLLM users also benefit.

#### D-041 — Attach API

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: `attach_llm(instance)` explicit API
- **Options Considered**: attach_llm(instance) / Auto-discover / Config-driven
- **Rationale**: Explicit, typed, debuggable. Works standalone and inside ArcAgent. One-liner shortcut: `serve(llm=model)`.

#### D-042 — WebSocket protocol

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: JSON messages with `type` field
- **Options Considered**: JSON type field / MessagePack / JSON-RPC 2.0
- **Rationale**: Matches ArcRun Event structure. Debuggable in devtools. No client deps.

#### D-043 — Multi-LLM handling

`arcui` · Architecture · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Multiple `attach_llm()` calls with labels
- **Options Considered**: Multiple attach_llm() / Agent registry / NATS discovery
- **Rationale**: Explicit, typed, scales linearly. Agent auto-discovery layered later.

### arctui

#### D-482 — arctui is a thin terminal interface over existing arc

`arctui` · Architecture · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Decision**: arctui adds NO new extension/agent system. It is a terminal interface that surfaces and drives arc's existing systems — agents, capabilities (tools/skills/modules), memory, MCPs, arcrun loops, arcllm models, and hooks. Extensibility lives in arc; the TUI renders it. Modeled on pi's tui package (a rendering/component framework) sitting separate from the agent.
- **Priority**: Simplicity + Modularity
- **Alternatives**: A new arctui-specific plugin layer wrapping arc packages (parallel system, weaker modularity); extending arc's CORE extension system for UI contributions across web+tui (larger blast radius, more scope now).
- **Rationale**: The owner's framing: 'arctui is just an interface to what already exists, a new interface through terminal.' Reusing arc's package/hook machinery dogfoods the existing API and avoids a second extension system to maintain.

#### D-483 — Coding capability = a tuned arcagent, not TUI logic

`arctui` · Architecture · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Decision**: The coding ability is a normal arcagent (bash/tools already built in) whose system prompt + identity are tuned for coding. arctui is the control surface that runs whichever agent you point it at; the coding smarts live in the agent/preset, not the TUI. 'General, then tune for coding' = a preset.
- **Priority**: Simplicity + Modularity
- **Alternatives**: Bake file-edit/diff/shell/approval logic into arctui as core features (couples the interface to one use case, bypasses the preset/dogfood path); a hybrid where coding VIEWS are first-party but smarts stay in the preset (revisit post-v1).
- **Rationale**: Owner: 'I create an arcagent, it comes with bash tools already, I tune the system prompt and identity to work on coding, and control it from arctui in various folders.' Keeps the interface generic and dogfoods the agent/preset path.

#### D-484 — Folder scoping via an explicit trust gate that edits policy

`arctui` · Architecture · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Decision**: Launching arctui in a folder prompts 'is this folder safe?'. On confirm, arctui persists the grant by adding that folder to the agent's [tools.policy] allowed_paths in arcagent.toml (wherever arc controls those policies). The folder becomes the agent's working + tool-confinement root for the session — the Claude Code trust model wired to arc's policy surface. Untrusted until confirmed; the grant should emit an audit event.
- **Priority**: Security + Simplicity
- **Alternatives**: Repo-local .arc agent per project (more modular per-project memory/identity, but two-tier discovery + more setup); a global agent whose allowed-paths is silently extended at launch (weakest isolation).
- **Rationale**: Owner: 'tui should ask if this folder is safe; if it is, it modifies the toml to allow that area for reading/writing.' Preserves arc's workspace-confinement invariant while enabling Claude-Code-style multi-folder use; secure by default.

### capabilities

#### D-372 — Extension architecture & install location

`capabilities` · Architecture · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: `~/.arc/capabilities/scap/` (global discovery root, precedence #2)
- **Source**: Source doc §2; arcagent capability precedence list

#### D-378 — In-memory ingest cache lifetime

`capabilities` · Architecture · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Module-level dict in `capabilities/scap/`, agent-runtime scoped (lost on restart, fine for 9-min demo)
- **Source**: Convention — matches `_runtime` pattern in builtins

### cross-cutting

#### D-321 — Package boundaries for new Hermes-parity capabilities (FINAL)

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Exactly **ONE** new sibling package: `arcgateway` (long-running platform daemon). Voice/web/browser as `arcagent.modules.*`. Skills Hub extends `arcskill` (TOML+CLI gated, see D-322). **Out of scope** (explicitly cut by user): `arcmcp` (no MCP client), `arcmigrate` (no migration tooling), `arcacp` (no IDE adapter). Centralized command registry lives in `arccli` (D-323). TUI completion in `arctui` (D-324).
- **Priority**: simplicity (one new package vs many); modularity (arcllm/arcrun/arcagent boundaries preserved); security (smallest possible new attack surface)
- **Alternatives**: Original proposal had 5 new packages (arcgateway, arcmcp, arcskillhub, arcacp, arcmigrate); progressively cut by user across the walk.
- **Rationale**: User chose to absorb only the Hermes capabilities that fit Arc's stance. MCP, IDE protocol, and migration tooling can ship later as community packages or follow-up roadmap items if demand emerges.
- **Tiers**: Same package layout across tiers; tier behavior differs in policy layer.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-325 — arcgateway process model

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Long-running separate daemon, one ArcAgent instance per chat (not bundled with agent process)
- **Priority**: simplicity (single ops unit, restart independently); scalability (horizontal scale via N gateways)
- **Alternatives**: Same-process multi-tenant pool; per-chat OS subprocess; per-chat asyncio task in shared daemon
- **Rationale**: Crash isolation; horizontal scale; matches existing arcui pattern.
- **Tiers**: Same model; tier flips agent-spawn isolation (D-326).
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-326 — Agent dispatch model inside arcgateway

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: In-process asyncio task per active chat; tier flips executor — federal adds subprocess isolation per chat (own DID, own tool sandbox, own audit boundary). Same code path; policy layer chooses executor.
- **Priority**: simplicity (asyncio default); security (federal pays subprocess overhead for safety)
- **Alternatives**: NATS-routed (forces NATS dep on personal); subprocess-always (cold start kills UX); single shared agent (breaks DID model)
- **Rationale**: Lets Arc match Hermes' "$5 VPS" ergonomics at personal while honoring SCIF requirements at federal.
- **Tiers**: Federal: subprocess per chat. Enterprise: asyncio with strict resource limits + per-chat audit. Personal: asyncio.

#### D-327 — Session storage engine

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: JSONL primary (append-only, audit-friendly, human-readable) + SQLite FTS5 derived index (background-built). Search reads SQLite; truth lives in JSONL. Crash-safe rebuild from JSONL.
- **Priority**: simplicity (no breaking change); modularity (search can be swapped without touching primary store); security (JSONL audit posture preserved)
- **Alternatives**: Full SQLite migration; pluggable session store; non-SQLite full-text (whoosh/tantivy)
- **Rationale**: Adds Hermes' search capability without losing Arc's existing audit posture. Index lag acceptable for search use case.
- **Tiers**: Same engine all tiers. Federal: SQLite file at AES-256 (D-319); JSONL append + checksum chain.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-328 — Session API ownership

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Sessions live in `arcagent.modules.session`; arcgateway depends on arcagent for the API.
- **Priority**: simplicity (no new package); modularity (arcgateway = "the daemon that runs ArcAgents," dep is honest)
- **Alternatives**: New `arcsession` package; NATS RPC; split-brain per-package
- **Rationale**: arcgateway will never run without arcagent; an extracted session package would be ceremony without payoff.
- **Tiers**: Same.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-329 — Session identity model (revised)

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Session = `(user, agent)` pair. Same user across multiple platforms (Slack + Telegram + …) = same session. Different user = new session. Agent owns multiple sessions and shares its own memory across them; can read across its own sessions for context per the ACL model in D-330.
- **Priority**: simplicity (matches "agent = person" mental model); security (per-user isolation by default)
- **Alternatives**: Per-platform isolation (Hermes default); always-unified by user (info-flow violation at federal); per-chat isolation (loses memory accumulation)
- **Rationale**: User explicitly framed agent as a person who may speak with many users across many channels — sessions belong to the agent but are bounded by the user they're with.
- **Tiers**: Same model; tier governs cross-session reads (D-330) and cross-user data flow.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-331 — Multi-message concurrency

`cross-cutting` · Architecture · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: One session = one in-flight turn. Different sessions (different users) run concurrently. No interrupt; no parallel turns on the same session. Per-user-session FIFO is the natural consequence of D-329.
- **Priority**: simplicity; security (no race conditions on session memory); modularity
- **Alternatives**: Interrupt-and-redirect; parallel turns; gateway-level block
- **Rationale**: Direct consequence of D-329.
- **Tiers**: Same.

#### D-620 — One-way execution-stack dependency graph

`cross-cutting` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Enforce `arcllm <- arcrun <- arcagent <- arcgateway / arcui`. ArcLLM knows nothing above it; ArcRun may know ArcLLM but not ArcAgent; ArcAgent knows ArcRun but not ArcLLM, ArcGateway, or ArcUI; ArcAgent must run headlessly.
- **Alternatives**: Allow convenient cross-layer imports (rejected: standalone packages cease to be standalone); enforce only runtime imports (rejected: metadata can reintroduce the same forbidden dependency).
- **Rationale**: One-way seams preserve independent installation, replaceability, headless operation, and a comprehensible security boundary.

#### D-624 — Do not turn the ArcRun facade into a cross-layer junk drawer

`cross-cutting` · Architecture · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Classify each current ArcAgent-to-ArcLLM dependency before moving it. Execution/model contracts may become ArcRun-owned; generic PII and secret-detection services belong in a neutral security contract; ArcAgent-specific optional behavior belongs behind injected module/extension interfaces.
- **Alternatives**: Re-export every ArcLLM helper from ArcRun (rejected: hides rather than removes coupling); keep direct private imports (rejected: violates the dependency graph).
- **Rationale**: A facade should expose one layer's stable contract, not proxy unrelated internals from every lower package.

#### D-648 — A module's tools and skills are copied to each agent; its runtime is not

`arcagent` · Architecture · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `arc module install` copies the module's tools and skills into
  `<agent_dir>/capabilities/<name>/` for each agent that enables it, and leaves
  `_runtime.py` and its hook handlers at the deployment module root. The agent runs
  the copies out of its own capability dir and works out of its workspace. Extensions
  install the same way. The capability loader's scan-root list stays the fixed set it
  is today and never grows with the number of installed modules or extensions.
- **Alternatives**: Load tools and skills in place from the deployment root as a
  trusted `module:<name>` scan root, which is what happens today (no duplication, one
  copy to update, and first-party signed code keeps its trusted classification — but
  every installed module and every extension adds a scan root, so the root list grows
  without bound and its shape is dictated by artifacts that do not exist yet);
  per-agent overlay files layered over an in-place base (customization without
  duplication, but a two-file merge for every skill and a base that can move under an
  overlay that no longer applies).
- **Rationale**: Josh's call, on two grounds. First, per-agent drift is a product
  goal, not a defect: an agent's skills are supposed to improve for that agent, which
  is the whole premise of the skill improver, and a shared read-only original cannot
  improve for one agent without changing every agent. Second, and decisively for
  security: an unbounded scan-root list is an unbounded threat surface. Copying into
  one known directory means the loader's roots are fixed and enumerable no matter how
  many modules or extensions arrive, so no future artifact gets to add a load path.

#### D-641 — The development path signs with a dev key rather than skipping verification

`arcagent` · Architecture · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `arc module install --from-source <name>` bundles one module out of
  the repo's source catalog, signs it with a locally generated development key, and
  installs it through the identical verify-then-materialize path. Nothing is
  symlinked, nothing skips verification, and no code is placed in a workspace. The dev
  key is trusted only at personal tier; enterprise and federal accept release-key
  issuers alone, so a dev-signed module simply will not load there.
- **Alternatives**: Symlink the source catalog into the module root and skip
  verification behind a tier gate (fastest inner loop, but it introduces an
  unverified load path that exists solely for convenience, and the tier gate is then
  the only thing keeping it out of production); keep the in-tree directory as a live
  scan root for development only (zero friction, but a second discovery path that
  only dev exercises, which is what D-633 rejects); require a release-signed bundle
  for every edit (no dev machinery at all, but nobody can iterate without CI).
- **Rationale**: Moving modules out of the wheel makes iteration the thing most likely
  to break, and a slow inner loop is how a good boundary gets quietly bypassed. But an
  exception that skips verification is a second path by definition, and the second
  path is always the one that rots or gets shipped by accident. Signing with a
  different key instead of skipping the check keeps exactly one code path — bundle,
  verify, materialize — and moves the trust decision to where it already lives, the
  issuer allowlist that `arcrun/backends/_verifier.py` and D-636 both use. Bundling a
  single module is fast enough that this costs seconds per edit.

#### D-635 — One core artifact; the module bundle is the only thing that varies by deployment

`cross-cutting` · Architecture · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: Every deployment installs the identical `arc-agent` wheel. What
  differs between personal, enterprise, and federal is which signed module bundle
  is applied on top. No tier-specific build, no tier-specific wheel, no build flag
  that changes what is compiled.
- **Alternatives**: Build a custom wheel per deployment containing only approved
  modules (one artifact to carry, but federal then runs code that was never built or
  tested elsewhere, every module combination becomes its own hash and its own
  approval, and the wheel under test is not the wheel shipped).
- **Rationale**: CLAUDE.md line 129 — personal to federal is a stringency dial, not a
  rewrite. A per-deployment wheel is a rewrite wearing a version number. Splitting
  the artifact means the core is approved once and the auditable delta is a short
  list of module names rather than a diff of two binaries, and adding a module later
  is a new bundle rather than a rebuild of everything.

---

## 2. Data Model

### arcllm

#### D-193 — Budget Storage Backend

`arcllm` · Data Model · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: In-memory accumulator for enforcement + OTel spans/structured logs for durable persistence
- **Alternatives**: SQLite local file (rejected: breaks pattern — no other module persists locally); External Redis/NATS KV (rejected: adds infrastructure dependency)
- **Rationale**: Follows existing telemetry pattern exactly. TelemetryModule, AuditModule, and OtelModule all persist via OTel spans and structured logging to external collectors. Budget data flows through the same pipeline. External observability stack (Grafana, Jaeger, etc.) is the query/audit layer. In-memory accumulator resets on restart — acceptable because OTel has the durable record.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-194 — Budget Period & Reset

`arcllm` · Data Model · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Calendar periods — monthly + daily + per-call max
- **Alternatives**: Rolling window (rejected: requires storing all transactions, higher memory, harder to audit); Monthly only (rejected: no daily protection against runaway agents)
- **Rationale**: Federal compliance (Anti-Deficiency Act 31 U.S.C. 1341). Monthly aligns with procurement/billing/fiscal reporting cycles. Daily acts as circuit breaker — prevents a runaway agent from burning an entire monthly allocation in hours. Per-call max prevents single expensive calls. Three enforcement layers. NIST 800-53 SA-2 (resource allocation) and OWASP LLM10 (unbounded consumption).
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-195 — Per-Call Max Estimation

`arcllm` · Data Model · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Pre-flight estimate via `max_tokens * cost_output_per_1m / 1_000_000`
- **Alternatives**: Post-call enforcement only (rejected: reactive — money already spent, Anti-Deficiency violation already occurred)
- **Rationale**: Conservative upper bound. One multiplication, zero overhead, no tokenizer dependency. Prevents obviously excessive calls before they happen. Underestimates (ignores input cost) but that's acceptable — it's a safety net, not a billing system.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-235 — TOML config structure

`arcllm` · Data Model · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Standard `[provider]` + `[models.*]` sections. `api_key_env = "AZURE_OPENAI_API_KEY"`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-236 — Model/deployment name mapping

`arcllm` · Data Model · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Deployment name as model — user passes deployment name to `load_model()`. TOML model metadata is reference-only for pricing/capabilities
- **Priority**: Simplicity
- **Tier Notes**: All tiers: deployment name is the model identifier

### arcrun

#### D-134 — How do depth and budgets live on RunState?

`arcrun` · Data Model · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Flat fields**
- **Rationale**: `depth`, `max_depth`, `parent_run_id`, `token_budget`, `cost_budget` as flat fields on RunState. No special classes. A child is just another `run()` call with depth + 1.
- **Category**: Data Model

#### D-598 — Generic Event with Dict Data

`arcrun` · Data Model · was `DECISION-013` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Generic Event with dict data
- **Context**: ~12 event types need data. Choice between typed dataclass per event (~200 lines) vs generic Event with dict data (~10 lines).
- **Options**:
- Generic Event + dict data — one Event dataclass, flexible, ~10 lines
- Typed dataclass per event — full autocomplete but ~200 lines (40% of Phase 1 budget)
- Generic Event + TypedDict helpers — middle ground (~80 lines) but callers still get dict
- **Reasoning**: 1,000 line budget demands efficiency. One Event dataclass with type:str + data:dict saves ~190 lines. Expected keys documented in docstrings. Flexible enough for custom events from tool authors. Autocomplete loss is acceptable — event consumers typically switch on event.type anyway.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-603 — Text + Tool Calls Preserved in Message

`arcrun` · Data Model · was `DECISION-018` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Include in assistant message
- **Context**: Models sometimes return both text content and tool_calls in one response. What happens to the text?
- **Options**:
- Include in assistant message alongside ToolUseBlocks (preserves reasoning chain)
- Emit as event only, don't store (saves context but loses reasoning)
- **Reasoning**: This is what arcllm's message format expects (TextBlock + ToolUseBlock in content array). The model's reasoning chain is valuable for the next turn. Events also capture it for observability. Discarding would break context that the model might reference in subsequent turns.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-609 — Strategies Return LoopResult

`arcrun` · Data Model · was `DECISION-024` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Return LoopResult
- **Context**: Should strategies return the final result or mutate shared state?
- **Options**:
- Return LoopResult — strategy is self-contained, builds and returns result
- Mutate RunState — strategy modifies state, run() reads final state
- **Reasoning**: Strategies own the loop, they own the result. Self-contained, independently testable. run() is pure orchestration — picks strategy, passes config, returns whatever the strategy returns. No shared mutable state between run() and strategy.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-619 — Strategy Metadata is name + description

`arcrun` · Data Model · was `DECISION-034` · from *Build Decisions: Phase 2 — CodeExec*

- **Decision**: name + description
- **Context**: What metadata does each strategy expose for model-based selection?
- **Options**:
1. name + description — two strings, minimal
2. name + description + when_to_use — three strings, more guidance
3. name + description + capabilities — structured tags
- **Reasoning**: Sufficient for model routing. The description already explains when to use ("iterative tool-calling loop for multi-step problems" vs "write and execute Python code to solve tasks"). Adding when_to_use is redundant. Capability tags add boilerplate that only matters with many strategies — premature with two.

### arcagent

#### D-081 — Entity fields in roster

`arcagent` · Data Model · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: All non-empty fields
- **Options Considered**: name+id+roles+caps+status / name+id only / all fields
- **Rationale**: Convention-driven: whatever's on the model appears in the prompt. Add a field to Entity, it auto-renders.

#### D-082 — Specific new fields on Entity

`arcagent` · Data Model · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: None needed
- **Options Considered**: description / when_to_contact / none
- **Rationale**: Convention handles it. Any field set on Entity renders automatically. Design fields as needed, not upfront.

#### D-083 — Renderer approach

`arcagent` · Data Model · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: Dynamic field iteration
- **Options Considered**: Dynamic (model_dump) / Static (known fields)
- **Rationale**: `model_dump(exclude_defaults=True)` for Pydantic, `dataclasses.fields()` for dataclasses. New fields appear in prompt without code changes.

#### D-121 — Schedule storage format?

`arcagent` · Data Model · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Pydantic models + JSON file**
- **Rationale**: `workspace/schedules.json`. Atomic writes (tmp+rename). Git-diffable, auditable, zero infrastructure.
- **Category**: Data Model

#### D-164 — Session management?

`arcagent` · Data Model · from *Feature: Telegram Messaging Module*

- **Choice**: **Resume last session, /new for fresh start**
- **Rationale**: Following OpenClaw's model. Cron-triggered runs get isolated sessions. Current session_id persisted in `{workspace}/telegram/state.json`.
- **Category**: Data Model

#### D-165 — Chat model?

`arcagent` · Data Model · from *Feature: Telegram Messaging Module*

- **Choice**: **1 bot = 1 user = 1 chat**
- **Rationale**: Single-user day-1. `allowed_chat_ids` allowlist in config. No mapping store needed. Module tracks current active session_id.
- **Category**: Data Model

#### D-217 — working.md format

`arcagent` · Data Model · from *Bio-Memory (ArcAgent)*

- **Decision**: YAML frontmatter (topics, semantic tags, entity refs, importance, turn number, timestamp) + LLM-written markdown body
- **Rationale**: Frontmatter aids search/graph traversal. Body is turn state.
- **Category**: Data Model

#### D-218 — Episode file format

`arcagent` · Data Model · from *Bio-Memory (ArcAgent)*

- **Decision**: Rich YAML frontmatter (date, type, significance, participants, emotional_signal, entities_touched, source_agent, tags, links_to) + LLM narrative body
- **Rationale**: Follows PRD. Auditable metadata.
- **Category**: Data Model

#### D-219 — how-i-work.md format

`arcagent` · Data Model · from *Bio-Memory (ArcAgent)*

- **Decision**: Minimal frontmatter (last_updated, token_count, version) + LLM-written body. 500 token budget.
- **Rationale**: LLM decides structure. Budget is the constraint.
- **Category**: Data Model

#### D-220 — Episode naming

`arcagent` · Data Model · from *Bio-Memory (ArcAgent)*

- **Decision**: `YYYY-MM-DD-{llm-slug}.md`
- **Rationale**: Human-readable, date-sortable.
- **Category**: Data Model

#### D-256 — Session storage

`arcagent` · Data Model · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Single-user, mirror Telegram: `{workspace}/slack/state.json` with `{user_id, session_id}`. 1 agent = 1 user = 1 session
- **Priority**: Simplicity
- **Tier Notes**: Each agent has its own Slack connection

#### D-257 — Config fields

`arcagent` · Data Model · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `enabled`, `allowed_user_ids: list[str]`, `max_message_length: int = 4000`, `bot_token_env_var`, `app_token_env_var`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-301 — Tool policy types location

`arcagent` · Data Model · from *Feature: arc-core-hardening*

- **Choice**: New `arcagent/core/tool_policy.py`
- **Priority**: simplicity
- **Tier Variation**: None

#### D-302 — Proactive schedule definition

`arcagent` · Data Model · from *Feature: arc-core-hardening*

- **Choice**: TOML config + runtime API (tools)
- **Priority**: simplicity + extensibility
- **Tier Variation**: None

#### D-303 — Agent-created tool format

`arcagent` · Data Model · from *Feature: arc-core-hardening*

- **Choice**: Single `.py` file with `@tool` decorator
- **Priority**: simplicity
- **Tier Variation**: None

#### D-304 — Agent-created extension format

`arcagent` · Data Model · from *Feature: arc-core-hardening*

- **Choice**: Python file + MODULE.yaml (match convention)
- **Priority**: simplicity
- **Tier Variation**: None

#### D-344 — Title-case section headings standardized

`arcagent` · Data Model · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: SKILL.md sections are exactly `## Resources`, `## Contract`, `## Knowledge`, `## Steps`, `## Anti Patterns`, `## Examples`, `## Validation`. Validator hardcodes these.
- **Priority**: simplicity (one canonical form, no drift)

#### D-357 — Versioning format — semver, LLM picks bump

`arcagent` · Data Model · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: All capabilities carry semver (`1.0.0`) in frontmatter. Create starts at `1.0.0`. LLM decides major/minor/patch as a step in the `update-skill` / `update-tool` procedure. No hardcoded auto-bump rule.
- **Priority**: simplicity (don't over-engineer the bump rule; let the skill teach it)
- **Alternatives**: simple int (rejected — marketplace will want semver later); always-bump-patch rule (rejected per user — the update skill is the right place to teach judgment).
- **Tiers**: Federal — `version` required, audit logged on every bump. Enterprise — required, warns on missing. Personal — required (cheap to populate).

#### D-399 — Summary structure

`arcagent` · Data Model · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Schema-anchored fields, single-shot regenerate (or delta-merge), NOT prose: `goal` (verbatim, never overwritten), `constraints` (security/user, verbatim), `progress` (quantified), `key_facts[]` (w/ provenance), `files_modified[]`, `decisions[]` (w/ rationale), `rejected_approaches[]`, `open_questions[]`, `next_step`.
- **Priority**: simplicity > scalability
- **Rationale**: "Structure forces preservation" (Factory). The two most-omitted-yet-critical fields per the research: `rejected_approaches` (what masking/prose loses → repeated dead ends) and quantified `progress` (fixes pruning's premature termination). Verbatim goal + constraints = Claude Code's hard-preserve rule (LLM07/ASI01).

#### D-404 — Compaction summary must reassemble

`arcagent` · Data Model · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: `compaction_summary` entry now carries `role="user"` + rendered `content` (type/counts are ignored-extra keys).
- **Rationale**: `agent_dispatch` builds history via `Message(**record)`; the old entry had no role/content → pydantic ValidationError on the first dispatch after ANY compaction. Was never hit only because the trigger was dead (D-405).

#### D-416 — Long-term recall correctness

`arcagent` · Data Model · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: `_longterm.md` truncation keeps the NEWEST sections (was `text[:max]` = oldest); exclude today/yesterday sections from Long-term injection (already shown as ### Today/### Yesterday). 3-way-consensus bug.

#### D-513 — Canonical artifact and storage

`arcagent` · Data Model · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: workflow.toml + schemas/ + prompts/ + scripts/ in the owning agent's workspace with detached .arcsig sidecars; prior versions retained as versions/<n>.toml; arcstore indexes (metadata, version, content hash) but never owns; written via direct filesystem I/O (ADR-029).
- **Priority**: security
- **Alternatives**: definitions in arcstore rows; git-only history
- **Rationale**: One canonical serialized form is simultaneously the executable, the UI render source, and the diffable audit record (Step Functions/n8n single-artifact discipline); workspace-is-truth is the app-store rule.

#### D-514 — Node materialization

`arcagent` · Data Model · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Lazy frontier materialization: the runner creates a task row only when a node becomes reachable. Untaken branches never become tasks and need no status. The Run row records the path taken (ordered nodes, router choices, loop iterations) as the authoritative trace.
- **Priority**: simplicity
- **Alternatives**: instantiate all nodes upfront + new skipped status; done+resolution=skipped
- **Rationale**: Josh: an untaken branch needs no status — only the final path taken must be recorded. Also unifies with loops, which must materialize iterations lazily anyway.

#### D-515 — Typed handoff

`arcagent` · Data Model · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Every producing node declares output_schema (JSON Schema), validated on completion — schema failure is a retryable node failure. Nodes may declare required artifacts (files that must exist on disk for completion to count). Downstream wiring via $nodes.<id>.output.<field> and $input.<field>, resolved by deterministic runner code from journaled outputs.
- **Priority**: security
- **Alternatives**: free-form output dicts + prose handoff (status quo)
- **Rationale**: ARC-3: a schema is a contract, a prompt asking the model to be careful is not. Artifact checks are the BlastForge pipeline.py lesson: trust the filesystem, not the report.

#### D-516 — Run aggregate

`arcagent` · Data Model · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: New arcstore runs mutable collection (ARC-1): run_id, workflow id+version+hash, status (pending/running/waiting_gate/done/failed/cancelled), initiator DID, budget, path taken, per-node rollup.
- **Priority**: modularity
- **Alternatives**: extend the spool tables; no Run row (status quo GROUP BY)
- **Rationale**: SpoolKind is a closed Literal with no run_id; every app UI asks is-it-done/what-stage/what-cost and nothing can answer that today.

#### D-517 — Loops

`arcagent` · Data Model · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Declared bounded back-edges only: loop_back_to + mandatory max_iterations, statically validated to target an ancestor within the same branch; exhaustion fails the node; every iteration is a fresh audited task row.
- **Priority**: security
- **Alternatives**: free cycles (LangGraph-style); no loops (pure DAG)
- **Rationale**: Production graphs need revise loops (LangGraph retrospective) but unbounded model-driven cycles forfeit auditability and budget control.

#### D-518 — Routers and conditional edges

`arcagent` · Data Model · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Routers choose only among pre-declared routes. mode=rules evaluates when-predicates (equality/comparison/boolean over $nodes.*.output.* and $input.* only); mode=llm is an Infer step whose output schema is an enum of the declared route ids, choice recorded and audited.
- **Priority**: security
- **Alternatives**: LLM picks any next node dynamically
- **Rationale**: D-004: models fill in nodes, deterministic code sequences; an enum-constrained choice among declared branches preserves that while allowing judgment-based routing.

#### D-549 — Where an installed connector lives

`arcagent` · Data Model · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: A per-agent copy inside the agent's workspace: ~/arc/team/<agent>/extensions/<name>/. Duplication across agents is accepted.
- **Priority**: security
- **Alternatives**: Shared machine-level bundle store plus per-agent enablement flags (one copy to verify and update, no duplication); Pip/uv packages (versioning comes free, but makes every connector an engineering artifact); Checked into the fleet repo (atomic deploys, but adding a connector needs a code change)
- **Rationale**: Full per-agent isolation, consistent with ADR-029 workspace ownership. Accepts N copies of a bundle as the price of one agent never reaching another agent's connector state.

#### D-550 — Multiple accounts on one service

`arcagent` · Data Model · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: Named instances. A connector may be installed more than once under distinct names (gmail_work, gmail_personal), each bound to its own account. Tools carry the instance name, so policy can allow one and deny the other.
- **Priority**: security
- **Alternatives**: One account per connector per agent (simplest, but breaks on Josh's existing two Google accounts on day one); One connector holding several accounts with the account chosen per call (fewest tools, but the agent can pick the wrong identity and policy cannot separate work from personal)
- **Rationale**: Josh runs hello@joshuaschultz.com and joshuamschultz@gmail.com today. Identity must be fixed at registration time, not chosen by the model at call time.

#### D-629 — Consumed session bytes advance independently of indexable rows

`arcagent` · Data Model · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: The session search index commits its byte offset whenever it
  consumes complete JSONL records, even when every record is malformed or
  intentionally excluded. Row inserts and the new offset share one transaction.
- **Rationale**: The offset represents durable consumption, not the count of
  searchable messages. Coupling it to successful row creation causes malformed
  tails to be reparsed and relogged forever.

#### D-650 — Compaction commits an explicit replay baseline by revision

`arcagent` · Data Model · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Compaction summarizes a versioned snapshot outside the session
  lock, commits only when the revision is unchanged, and appends a boundary
  record containing the exact summary-plus-tail baseline replay must restore.
- **Rationale**: Appending only a summary resurrects all pre-compaction messages
  after restart, while holding the lock across a model call blocks normal turns.

### arcmemory

#### D-498 — Source timestamps are carried in the chunk text, not the event ts

`arcmemory` · Data Model · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: Each chunk is prefixed with its session date, e.g. '[Session date: 2023-05-14]', so the date enters memory as content. The stored Event.ts remains datetime.now(UTC). Logged as a known limitation.
- **Priority**: Simplicity
- **Alternatives**: Threading a ts parameter through Brain.capture and FastCapture.capture (rejected: framework change). Skipping the temporal-reasoning question type (rejected: discards a whole capability the benchmark exists to measure).
- **Rationale**: Verified: FastCapture hard-codes Event ts to now(), and no public capture signature accepts a source timestamp. The text prefix makes the date reachable by both keyword and semantic recall with zero framework change, and matches what LongMemEval's own reference implementations do. Known limitation to carry into results: all 40 sessions in a haystack share a wall-clock ts, so recency ranking has no signal to work with, and any future email or Slack backfill inherits the same gap.

### arcprompt

#### D-462 — Frontmatter schema

`arcprompt` · Data Model · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Authored fields: name, description, tunable. Version identity is derived — content sha256 computed at load, change history from git. No hand-maintained version field.
- **Priority**: simplicity
- **Alternatives**: name + description only; explicit version plus owner/tier/eval_suite
- **Rationale**: A hand-bumped version drifts from the body the first time someone edits and forgets, and then the audit trail lies — the same failure as spec-status drift. A content hash cannot drift by construction. `tunable` is the one forward-looking field kept, because the v2 optimizer needs a declared way to mark a prompt off-limits; tier and eval_suite are deferred as YAGNI with no consumer.

#### D-465 — No seed-on-install

`arcprompt` · Data Model · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Agent context/ starts empty. Stock is never copied into agents at install or agent-create time; an overlay file exists only where someone deliberately overrode a prompt.
- **Priority**: modularity
- **Alternatives**: arccli seeds all stock prompts into each agent at install; seed with recorded stock hash and auto-update unmodified copies
- **Rationale**: Seeded copies make a stale copy indistinguishable from a deliberate override, so upstream prompt improvements never reach deployed agents and every upgrade becomes an N-agent manual patch — the drift already seen when the arcmemory distiller fix landed in the scaffold but not in deployed tomls. Overlay-only keeps stock authoritative and upgradeable, and makes diff-vs-stock pure signal.

#### D-480 — An override is permanent — upgrade divergence accepted

`arcprompt` · Data Model · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Once a prompt is overridden, upstream changes to that prompt never reach that agent, by design. Arc ships no stock-hash tracking, no three-way merge, and no "stock has changed" staleness indicator. The overlay is the answer until a human deletes it (reset-to-stock, D-464).
- **Priority**: simplicity
- **Alternatives**: record the stock hash an overlay forked from and surface divergence in arcui; three-way merge on upgrade (the dpkg/ucf model)
- **Rationale**: Operator ruling, accepting the risk the deepening pass raised. An override exists precisely because stock was wrong for this deployment; silently pulling upstream wording back in would defeat it, and a staleness banner on every upgrade is noise for a signal the operator has already decided to ignore. This bounds the blast radius of D-465: un-overridden prompts (the overwhelming majority) still upgrade automatically, and the few deliberate overrides are frozen on purpose. Revisit only if an upstream prompt change is ever security-relevant, in which case the fix is a release note, not merge machinery.

### arcteam

#### D-101 — Search Strategy

`arcteam` · Data Model · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Grep + adaptive link traversal. Max hops configurable (default 3). Each hop evaluated — if BM25 score drops below threshold, traversal stops for that branch. Prevents flooding context with irrelevant traversals.
- **Priority**: Simplicity — grep is zero deps, adaptive stopping prevents wasted tokens.
- **Alternatives**: FTS5 (deferred to later phase), Pluggable backend protocol (rejected: premature abstraction).
- **Tiers**: Federal audit-logs every search query + results returned.

#### D-102 — Hop Relevance Scoring

`arcteam` · Data Model · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: BM25 (Okapi BM25) scoring for traversal relevance. Lightweight index of entity files, scored per hop. Below threshold stops that branch. Handles document length variation well.
- **Priority**: Simplicity — well-understood algorithm, ~50 lines or rank-bm25 lib, better than raw grep without LLM cost.
- **Alternatives**: TF-IDF (rejected: BM25 handles doc length better), LLM per hop (rejected: too expensive), Jaccard (rejected: weaker at term importance).
- **Tiers**: All tiers same scoring.

#### D-103 — Search Response Shape

`arcteam` · Data Model · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: `list[SearchResult]` Pydantic models with entity_id, path, snippet, score, hops, entity_type, tags. Caller formats for injection.
- **Priority**: Simplicity — clean data boundary, caller has full control.
- **Tiers**: Federal adds `classification` field to SearchResult.

#### D-104 — Index Schema

`arcteam` · Data Model · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Rich _index.json — includes id, path, type, tags, links_to, linked_from, summary snippet, last_updated, status. Enables graph traversal and consolidation cluster selection from index alone.
- **Priority**: Scalability — fewer file reads during search and consolidation.
- **Tiers**: Federal adds `classification` field per entry.

#### D-105 — Entity Type to Directory Mapping

`arcteam` · Data Model · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: entity_type maps to subdirectory. Generic defaults (person, organization, project, domain, process). Custom types configurable via TOML.
- **Priority**: Simplicity — small generic set, extensible.
- **Alternatives**: Flat directory (rejected: loses organization), User-defined freeform (rejected: unpredictable).
- **Tiers**: All tiers same.

#### D-106 — Playbooks & Decisions Storage

`arcteam` · Data Model · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Playbooks are entities (entity_type='playbook'), stored as markdown, searchable, consolidatable. Decisions are append-only JSONL via existing StorageBackend.
- **Priority**: Simplicity — two storage modes matching two write patterns.
- **Tiers**: Federal — decisions JSONL gets chained HMAC. Enterprise/Personal — no chain enforcement.

### arcstore

#### D-541 — Connector runtime state store

`arcstore` · Data Model · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Mandated Answer**: Health, probe results, and token state live in arcstore, not in config files.
- **Citation**: SPEC-026 FR-5 — arcui pulls from the arcstore DB
- **Category**: Data Model

### arcui

#### D-013 — UIEvent schema

`arcui` · Data Model · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Flat Pydantic envelope: layer, event_type, agent_id, agent_name, source_id, timestamp, data, sequence
- **Priority**: simplicity
- **Tier Notes**: Federal: may add classification field.

#### D-014 — Agent registration schema

`arcui` · Data Model · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Identity + capabilities: DID, name, model, provider, team, tools, modules, workspace, meta dict
- **Priority**: simplicity
- **Tier Notes**: Federal: workspace path redacted.

#### D-015 — Control message schema

`arcui` · Data Model · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Core set + extensible: action, target, data, request_id. Actions: steer, cancel, config, ping, shutdown. Response correlation via request_id.
- **Priority**: simplicity
- **Tier Notes**: Federal: includes operator identity.

#### D-044 — TraceRecord fields

`arcui` · Data Model · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: **Full telemetry + raw bodies**
- **Options Considered**: Full telemetry / Minimal / Full + raw bodies
- **Rationale**: Everything TelemetryModule computes PLUS serialized request (messages, tools, params) and response (content, usage, stop_reason). Federal requires full visibility into every LLM call. Raw bodies enable trace detail view in arcUI. Configurable: personal tier can omit raw bodies to save disk.

#### D-045 — JSONL rotation

`arcui` · Data Model · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Daily rotation
- **Options Considered**: Daily / Size-based / No rotation
- **Rationale**: Aligns with budget reset. Date-based retention trivial. chain-state.json for continuity.

#### D-046 — Trace file location

`arcui` · Data Model · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Workspace-relative `{workspace}/traces/` with configurable override
- **Options Considered**: Workspace-relative / XDG / Configurable
- **Rationale**: Co-locates with sessions. Configurable for shared fleet directories.

#### D-047 — Hash chain across rotations

`arcui` · Data Model · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: chain-state.json carries last hash
- **Options Considered**: chain-state.json / Self-referencing records / Separate chain log
- **Rationale**: Simple pointer file. Unbroken chain across file boundaries.

#### D-069 — Raw body storage

`arcui` · Data Model · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Configurable per tier, default ON
- **Options Considered**: Always store / Configurable / Never store
- **Rationale**: Federal: always store (full audit trail, NIST AU-3). Enterprise: default on, can disable. Personal: default off (saves disk), opt-in. TraceRecord gains `request_body: dict | None` and `response_body: dict | None`. Serialized as JSON within JSONL record.

#### D-070 — Filter/export on trace table

`arcui` · Data Model · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Provider filter + Agent filter + Status filter + Export CSV/JSON
- **Options Considered**: No filters / Provider + Agent filters / Full filter set
- **Rationale**: Matches demo. Dropdowns for provider and agent. Status filter (all/success/error/timeout). Export button for compliance reporting (NIST AU-6 review support).

### capabilities

#### D-370 — Hostname/IP/MAC sanitization mapping persistence

`capabilities` · Data Model · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Persisted TOML at `~/.arc/capabilities/scap/data/sanitize_map.toml`
- **Priority**: security > simplicity
- **Rationale**: Federal-grade answer to "how do we trust the rebranding" (source doc §9): file on disk + audit-event emission on first ingest. Reproducible across runs. Auditor can eyeball mapping.
- **Tier Notes**: Federal: mapping write also signed via `SignedChainSink`. Enterprise/Personal: JSONL audit only.

#### D-380 — ATT&CK → 800-53 mapping source

`capabilities` · Data Model · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: CTID (Center for Threat-Informed Defense) published JSON, bundled in `data/attack_to_800_53.json`
- **Source**: Source doc §5; NIST IR 8473 references CTID as canonical implementation

#### D-381 — POA&M output format

`capabilities` · Data Model · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: CSV with columns: control, finding, milestones, owner, remediation language, due date
- **Source**: Source doc §3, §6 Act 3

#### D-382 — 800-53 catalog source

`capabilities` · Data Model · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: NIST OSCAL JSON Rev 5 bundled in `data/nist_800_53_rev5.json`
- **Source**: Source doc §5

#### D-383 — FedRAMP baseline membership source

`capabilities` · Data Model · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Official FedRAMP Low/Mod/High definitions bundled in `data/fedramp_baselines.json`
- **Source**: Source doc §5

### cross-cutting

#### D-334 — Memory architecture under per-(user, agent) sessions

`cross-cutting` · Data Model · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Two-tier. `bio_memory.identity` and `bio_memory.episodic` stay agent-wide (the agent's own self/history). NEW `user_profile/{user_id}.md` per user (preferences, communication style, history with this agent). Read order on turn: agent_memory + relevant user_profile.
- **Priority**: modularity (two clean surfaces); simplicity (one new file type); security (per-user reads gated by D-330 ACL)
- **Alternatives**: Single shared agent memory (multi-tenant leakage); per-user only (loses agent compounding); three-tier (complexity)
- **Rationale**: Mirrors Hermes' MEMORY/USER split adapted to Arc's per-user session model and existing bio_memory.
- **Tiers**: Federal: cross-user profile reads blocked per D-330. Enterprise: warn on cross-user. Personal: free read.

---

## 3. API Design

### arcllm

#### D-196 — Budget API Surface in load_model()

`arcllm` · API Design · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Extend telemetry kwarg + mandatory `budget_scope` top-level kwarg
- **Alternatives**: Separate budget kwarg (rejected: contradicts D-188, budget is part of telemetry)
- **Rationale**: Budget config fields live in the telemetry dict (monthly_limit_usd, daily_limit_usd, per_call_max_usd, alert_threshold_pct, enforcement). `budget_scope` is a required top-level kwarg — forces caller to identify the agent. Config.toml sets org-wide defaults, `load_model()` overrides per-agent.

```python
load_model(
    "anthropic",
    telemetry={
        "monthly_limit_usd": 500.00,
        "daily_limit_usd": 50.00,
        "per_call_max_usd": 5.00,
        "alert_threshold_pct": 80,
        "enforcement": "block",
    },
    budget_scope="agent:agent-007",
)
```

#### D-197 — Routing Classification Source

`arcllm` · API Design · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Caller-declared via `classification` kwarg at invoke() time
- **Alternatives**: Content scanner detects (rejected: adds latency, false positives, duplicates Step 18); Both caller + verify (rejected: more complex, overlaps Content Scanner)
- **Rationale**: Classification is a policy decision, not a detection problem. Caller (ArcAgent) knows its data context. Zero overhead — no content scanning. Auditable. Content Scanner (Step 18) can verify separately if needed later.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-198 — Routing Rules Structure

`arcllm` · API Design · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Classification -> provider + model mapping
- **Alternatives**: Simple classification -> provider only (rejected: FedRAMP authorizes specific models, not just providers); Classification -> priority list (rejected: duplicates FallbackModule)
- **Rationale**: FedRAMP authorizes specific models. Routing rules specify both provider and model. Cost optimization per classification tier (e.g., CUI on Sonnet, unclassified on GPT-4o-mini). Auditors can verify exact model authorized per classification.

```toml
[modules.routing.rules.cui]
provider = "anthropic"
model = "claude-sonnet-4-6"
[modules.routing.rules.unclassified]
provider = "openai"
model = "gpt-4o-mini"
```

#### D-237 — URL construction

`arcllm` · API Design · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: `{base_url}/openai/v1/chat/completions` — no query params. v1 API hard-fails (400) on `?api-version=`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: deepened research

#### D-238 — Auth header

`arcllm` · API Design · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: `api-key: {key}` (lowercase, case-sensitive). NOT `Authorization: Bearer`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: deepened research

#### D-239 — URL normalization

`arcllm` · API Design · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: `base_url.rstrip('/')` before URL construction — defensive one-liner
- **Priority**: Simplicity
- **Tier Notes**: All tiers: prevents user config mistakes

#### D-240 — Content filter handling

`arcllm` · API Design · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Inherit base behavior — `_parse_response()` already handles `null` content via `.get('content')` and maps `content_filter` stop reason
- **Priority**: Simplicity
- **Tier Notes**: All tiers: no additional code

#### D-384 — Canonical `tool_choice` type ownership

`arcllm` · API Design · from *tool_choice passthrough — review follow-ups (2026-06-18)*

- **Choice**: Export `ToolChoice = Literal["auto","none","required"] \
- **Priority**: dict[str, Any] \
- **Rationale**: None` from `arcllm/types.py`; promote to a typed keyword-only param on `LLMProvider.invoke` (parity with `ResponseFormat`); arcrun (`state.py`, `streams.py`, `loop.py`) and arcagent (`core/agent.py`, `core/agent_dispatch.py`) `import` it instead of redefining `dict[str, Any]` per layer
- **Tier Notes**: modularity > simplicity | arcllm owns the concept (defines values, translates `"required"`→`"any"` for Mistral) but exports no type, so 6 sites hand-redefined it too narrowly — excludes the string forms OpenAI/Mistral accept and arcllm's own tests exercise (`test_mistral.py:131-162`); `mypy --strict` rejects a legitimate string caller at the agent surface. Architect recommends an ADR. | Same all tiers (type contract, not posture).

#### D-428 — GuardrailsModule validates the RESPONSE per call via a `guardrails={...}` kwarg

`arcllm` · API Design · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Structural output validation is a transport concern once bytes are in hand; per-call kwarg mirrors routing's `classification`.

#### D-434 — Both modules use `enforcement="block"\

`arcllm` · API Design · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: "warn"` (warn = flag + continue, block = raise) | Same vocabulary as RoutingModule; warn enables observe-only rollout before enforcing.

#### D-456 — Per-call opt-in via `load_model(..., load_balance=True\

`arcllm` · API Design · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: {dict})` | Consistent with every optional module; single-endpoint behavior unchanged when absent.

### arcrun

#### D-385 — `tool_choice` input validation at the loop boundary

`arcrun` · API Design · from *tool_choice passthrough — review follow-ups (2026-06-18)*

- **Choice**: Validate once where it enters the arcrun loop: reject empty `{}` and malformed shapes with a clear `ValueError` + audit warn before the provider call; normalize the no-tools case to one behavior across adapters (recommend: drop + warn, matching Anthropic)
- **Priority**: security > simplicity
- **Rationale**: Today empty `{}` passes the `is not None` guard and 400s at the provider; Anthropic silently drops on no-tools while OpenAI sends unconditionally → inconsistent. Conflicts with CLAUDE.md "validate all inputs" / LLM05. Validate at the single owning boundary, not per-layer.
- **Tier Notes**: Federal: validation failure is an audit event.

#### D-593 — Tool Type Implementation

`arcrun` · API Design · was `DECISION-008` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Dataclass + factories
- **Context**: Need to decide how the Tool type supports both simple (pass a function) and complex (stateful, configurable) usage patterns. This is the first thing every caller touches.
- **Options**:
- Dataclass + factories — Tool is a dataclass. Complex tools use factory functions returning Tool instances with closures. One pattern to learn.
- Dataclass + subclassing — Tool is a dataclass that can be subclassed (override execute). Two patterns, some devs prefer class-based for complex state.
- Protocol only — Structural typing, anything with the right shape works. Maximum flexibility but no validation at construction time.
- **Reasoning**: Follows pi-agent-core's proven pattern. One pattern to learn, not two. Factory functions (`make_search_tool(db)`) handle stateful/configurable tools via closures — achieves everything subclassing does without class inheritance overhead. Simpler DX, simpler internals.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-594 — Tool.execute Async Requirement

`arcrun` · API Design · was `DECISION-009` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Async only
- **Context**: Should Tool.execute accept both sync and async functions, or require async only?
- **Options**:
- Async only — execute must be async. Callers wrap sync themselves (trivial).
- Accept both — detect sync/async at construction, auto-wrap sync in asyncio.to_thread(). Friendlier but adds detection logic and threading footgun.
- **Reasoning**: arcllm is async-native. run() is async. Everything in the execution path is async. Adding sync auto-detection adds complexity for a one-liner wrapper the caller can do themselves. Matches arcllm's design.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-595 — Tool.execute Receives Cancellation Signal

`arcrun` · API Design · was `DECISION-010` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Pass cancel signal via ToolContext
- **Context**: Long-running tools (HTTP requests, subprocess, file operations) need a way to know when to stop if the loop is cancelled or steered.
- **Options**:
- Pass cancel signal via ToolContext — tools check ctx.cancelled or await ctx.cancel_event
- No signal — tools run to completion, loop ignores result if cancelled. Wastes compute, can't stop runaway subprocesses.
- **Reasoning**: Critical for steering (DECISION-002) and clean shutdown. Without it, a steered loop has no way to tell an in-flight HTTP request or subprocess to stop. Signal is opt-in for tool authors — they can ignore it for simple tools.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-596 — Typed ToolContext Object

`arcrun` · API Design · was `DECISION-011` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Typed ToolContext dataclass
- **Context**: What context does Tool.execute receive about its environment?
- **Options**:
- Typed ToolContext dataclass with: run_id, tool_call_id, cancel signal, event_bus, turn_number
- Plain dict with string keys (flexible but no type safety)
- Minimal — just params and cancel signal (simplest but tools can't emit events)
- **Reasoning**: IDE-friendly (autocomplete, type checking). Tools know exactly what's available. Includes run_id and tool_call_id for correlation, cancel signal for shutdown, event_bus for tools that need to emit custom events, turn_number for context awareness. Typed > dict for a public API surface.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-597 — Tool.execute Returns str

`arcrun` · API Design · was `DECISION-012` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: str
- **Context**: Should execute return a simple string or a richer ToolResult type?
- **Options**:
- str — simple, arcrun wraps into message format. Exceptions for errors. Events for observability.
- ToolResult(content, metadata, is_error) — richer but adds ceremony for the 90% case.
- str | ToolResult union — flexible but two code paths internally.
- **Reasoning**: Simplest thing that works. Errors handled via exceptions (arcrun catches, emits tool.error, returns error string to model). Observability handled via event bus (captures tool name, args, duration, result length). State/rendering metadata belongs to agent layer, not engine. pi needs {content, details} for TUI rendering and session state reconstruction — arcrun does neither. Can extend to str | ToolResult later without breaking existing tools.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-605 — Two Entry Points — run() + run_async()

`arcrun` · API Design · was `DECISION-020` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Two functions — run() + run_async()
- **Context**: How callers start the execution loop. Need to support both simple (fire and forget) and interactive (steering) usage.
- **Options**:
- Two functions: run() blocking, run_async() returns RunHandle
- One function always returning RunHandle (simple case needs .result())
- run() with optional on_handle callback
- **Reasoning**: 80% of callers use run() and never think about handles. Clean one-liner: `result = await run(model, tools, prompt, task)`. Steering callers use `handle = await run_async(...)` and get steer/followUp/cancel/result/state. Two patterns but each is clean for its use case.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-606 — Steer + FollowUp Delivery Modes

`arcrun` · API Design · was `DECISION-021` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Steer + followUp
- **Context**: For mid-execution steering, should there be one or two message delivery modes?
- **Options**:
- Steer only — inject message, skip remaining tools, continue loop
- Steer + followUp — steer interrupts, followUp waits for end_turn then injects
- Defer steering to Phase 4
- **Reasoning**: Follows pi-agent-core's proven model. Steer handles "stop what you're doing, do this instead." FollowUp handles "when you're done, also do this." Both are needed for human-in-the-loop patterns. Steer checks between tool executions within a turn. FollowUp checks at end_turn before returning LoopResult. Cancel is a third mode — hard stop with partial result.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-610 — Exceptions Bubble Up for Model Errors

`arcrun` · API Design · was `DECISION-025` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Exceptions bubble up
- **Context**: How does run() handle errors? Tool errors vs model API errors vs budget exceeded.
- **Options**:
- Exceptions bubble up — tool errors caught and returned to model; model/network errors raise to caller
- All errors in LoopResult — never raise, caller checks result.error
- Custom exception types — LoopError, ToolError, BudgetExceededError
- **Reasoning**: Tool errors are caught by arcrun and returned to the model as error tool results (model can retry or adjust). Model API errors (network, auth, rate limit) bubble up as exceptions — arcllm handles retries at the invoke level, so anything that reaches arcrun is a real failure. Caller's standard try/except pattern works. Clean separation: recoverable errors stay in the loop, unrecoverable errors exit to caller.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-613 — Executor Returns tuple[Message, bool]

`arcrun` · API Design · was `DECISION-028` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: `tuple[Message, bool]`
- **Context**: What does execute_tool_call return?
- **Options**:
- `tuple[Message, bool]` — message for conversation, bool for success
- `ToolCallResult` dataclass — richer but adds a new type
- Just `Message` — infer success from content (fragile)
- **Reasoning**: Minimal. Message goes into conversation history. Bool lets strategies track errors (for future circuit breaker) without parsing strings. Duration/name already emitted via events. No new types needed.
- **Status**: Accepted
- **Date**: 2026-02-14

#### D-616 — Executor Is Internal (Not Public API)

`arcrun` · API Design · was `DECISION-031` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Internal only
- **Context**: Should execute_tool_call be exported from __init__.py?
- **Options**:
- Internal — strategies import directly, not in public surface
- Public — exported for custom strategy builders
- **Reasoning**: Same pattern as _messages.py. Strategies import from arcrun.executor directly. Not part of the stable public surface — no stability commitment. Can promote to public later if custom strategies become a first-class use case.
- **Status**: Accepted
- **Date**: 2026-02-14

#### D-621 — ArcAgent consumes ArcRun through one qualified facade import

`arcrun` · API Design · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Each ArcAgent module uses `import arcrun` and qualified public names such as `arcrun.run_stream` and `arcrun.Tool`. `from arcrun ...` and ArcRun submodule imports are forbidden in ArcAgent production code.
- **Alternatives**: Individual root imports (rejected: obscures ownership and creates alias churn); deep imports (rejected: couples ArcAgent to ArcRun's internal layout).
- **Rationale**: One visible namespace makes the package boundary obvious in every call site and gives ArcRun one enforceable compatibility surface.

### arcagent

#### D-258 — Response delivery

`arcagent` · API Design · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Regular DM reply (no threads). Flat conversation like texting. Threading unnecessary for 1:1
- **Priority**: Simplicity
- **Tier Notes**: Differs from original SDD (thread replies)

#### D-259 — Processing indicators

`arcagent` · API Design · from *Slack Messaging Module (SPEC-011)*

- **Choice**: No emoji reactions. Just process and reply. Response appearing IS the feedback
- **Priority**: Simplicity
- **Tier Notes**: Differs from original SDD (:thinking_face: reactions)

#### D-260 — Proactive DMs

`arcagent` · API Design · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `conversations.open(users=user_id)` → cache channel ID → `chat_postMessage`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: deepened research

#### D-261 — Commands

`arcagent` · API Design · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Regular text commands ('start', 'new', 'status') — no slash commands. No manifest changes needed
- **Priority**: Simplicity
- **Tier Notes**: Eliminates 3-second ack complexity

#### D-262 — Message splitting

`arcagent` · API Design · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Copy `split_message()` to slack/bot.py with `max_length=4000`. Module stays self-contained
- **Priority**: Simplicity
- **Tier Notes**: DRY extraction at N=3, not N=2

#### D-305 — Self-modification tool surface

`arcagent` · API Design · from *Feature: arc-core-hardening*

- **Choice**: 6 focused tools: create_skill, improve_skill, create_tool, create_extension, list_artifacts, reload_artifacts
- **Priority**: simplicity
- **Tier Variation**: create_extension: Federal DENIED, Enterprise approval

#### D-306 — Schedule management tools

`arcagent` · API Design · from *Feature: arc-core-hardening*

- **Choice**: 5 tools: create/list/pause/resume/delete + bus-based wake events
- **Priority**: simplicity + extensibility
- **Tier Variation**: None

#### D-307 — `task_complete` schema

`arcagent` · API Design · from *Feature: arc-core-hardening*

- **Choice**: Minimal: status + summary (required), artifacts + next_steps + error (optional)
- **Priority**: simplicity
- **Tier Variation**: None

#### D-345 — `update_*` is a separate skill+tool from `create_*`

`arcagent` · API Design · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: `create_tool` / `create_skill` start fresh at version `1.0.0`. `update_tool` / `update_skill` modify existing capabilities and bump version. Bump direction (major/minor/patch) is decided by the LLM as a step in the update skill — see D-357.
- **Priority**: simplicity (single responsibility per tool), modularity (clean tool boundaries)

#### D-358 — `reload()` returns a plain string diff

`arcagent` · API Design · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: `reload()` returns a single human-readable string. One line nominal:
- **Priority**: simplicity (LLM reads it like a sentence; no JSON parsing)
- **Alternatives**: structured newline format (rejected — more tokens, no real benefit); JSON (rejected — parse overhead with no gain for the LLM consumer).
- **Tiers**: Federal additionally writes the diff to audit log per D-350.

```
reload: +3 added (create-tool, format-date, write-blog), ~2 replaced (read 1.0.0→1.0.1, write 1.0.0→1.0.1), -1 removed (legacy-grep), 0 errors
```
Multi-line only when errors exist (each error appended on its own line with skill/tool name + reason).

#### D-519 — Tool and CLI surface

`arcagent` · API Design · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Agent tools: workflow_create/add_node/edit_node/remove_node/set_trigger/set_channel (drafts), workflow_run, workflow_list/inspect/runs/run_status (read-only), workflow_cancel_run. CLI: arc workflow list/show/run/sign/verify/approve. arcui routes mirror the tasks route patterns (operator gate + emit_mutation_audit).
- **Priority**: simplicity
- **Alternatives**: single workflow_edit mega-tool; CLI-only authoring
- **Rationale**: Targeted tools give the model small validated moves with repairable errors; CLI keeps the signing key out of the agent process.

#### D-520 — Versioning contract

`arcagent` · API Design · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Edits bump a monotonic version and require expected_version (optimistic concurrency, stale edits refused); in-flight runs stay pinned to the version+hash they started from; edits never touch a running instantiation.
- **Priority**: security
- **Alternatives**: mutable definition, runs read latest; git-commit-per-edit as the only history
- **Rationale**: Temporal's versioning discipline + n8n's edit-concurrency pattern; prevents mid-run definition drift.

#### D-551 — Install and setup flow

`arcagent` · API Design · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: One command: `arc ext add <name> --agent <agent>` reads the manifest, prompts for each required secret with hidden input, probes the server, and writes nothing unless the probe succeeds. Re-authorization later is `arc ext auth <name>`.
- **Priority**: simplicity
- **Alternatives**: Two steps, add then auth, as OpenClaw does (keeps browser OAuth out of the install path, but the second step is forgettable); Agent walks the operator through it in chat (most approachable, but secrets would enter the model's context — LLM07); Hand-edit the toml then `arc ext sync` (scriptable for fleet deploys, least approachable, secrets end up in a file)
- **Rationale**: Matches the CLI-install experience Josh described. Probe-before-save means a half-installed connector is never persisted.

#### D-582 — Agent tools

`arcagent` · API Design · was `T-001` · from *Bio-Memory (ArcAgent)*

- **Decision**: Four tools: `memory_search`, `memory_note`, `memory_recall`, `memory_reflect`. Full agent control over memory.
- **Rationale**: Agent needs tools to create, update, use, manage memory.
- **Category**: API/Tools

#### D-655 — Capability registry state is exposed only through snapshots

`arcagent` · API Design · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: `CapabilityRegistry` owns all collection and lock state and
  exposes immutable snapshot/query operations for tools, skills, hooks,
  lifecycles, authored names, and counts.
- **Rationale**: Consumers that acquire the registry's lock or traverse its
  dictionaries duplicate precedence rules and make transactional reload
  impossible to reason about. Snapshots keep one owner for mutation semantics.

#### D-662 — Tool dispatch is an ordered typed pipeline

`arcagent` · API Design · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: One dispatch envelope passes a typed context through explicit
  normalize, authorize, approve, execute, and record stages in that order.
- **Rationale**: A 185-line nested closure that mutates captured arguments and
  policy state makes security ordering implicit. Named stage contracts expose
  inputs/outputs while retaining one fail-closed public envelope.

### arcprompt

#### D-464 — Dedicated prompt endpoints

`arcprompt` · API Design · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: GET /api/agents/{id}/prompts (all prompts, all packages, with overridden state); GET .../{pkg}/{name} (stock + effective + diff); PUT (write overlay); DELETE (reset to stock). Stock is read server-side from the installed package; the client never supplies a filesystem path.
- **Priority**: security
- **Alternatives**: extend the generic files API with a stock read root and ?stock=true; both a curated prompts API and raw file access
- **Rationale**: Prompts are (package, name)-shaped with a stock/overlay duality; the files API is path-shaped and assumes one root per request. Teaching it a read-from-package/write-to-workspace redirect would require building enumerate-all, diff, and reset anyway. Reading stock server-side with no client-supplied path is tighter than widening file-API confinement into installed package code, and keeps the files API's single-root invariant intact. Handlers reuse the existing confinement and secret-scan helpers rather than reimplementing them.

### arcui

#### D-016 — Rate limiting

`arcui` · API Design · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: All endpoints rate-limited
- **Priority**: security
- **Tier Notes**: `auto-applied: federal-mandate` (NIST SC-5)

#### D-017 — Agent WS path

`arcui` · API Design · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: `/api/agent/connect`. Browser stays at `/ws`.
- **Priority**: simplicity

#### D-018 — Browser REST for agents

`arcui` · API Design · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Thin proxy: POST to UI REST → UI forwards to agent WS → returns response. Endpoints: /api/agents, /api/agents/{id}, /api/agents/{id}/control
- **Priority**: simplicity
- **Tier Notes**: Federal: operator role for control.

#### D-048 — Endpoint structure

`arcui` · API Design · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Single WS `/ws` + static `/` + REST `/api/*`
- **Options Considered**: Single WS + static + REST / Multiple WS / Pure WS
- **Rationale**: Minimal routing surface. WS for real-time, REST for queries.

#### D-049 — Historical query API

`arcui` · API Design · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: GET with query params + cursor pagination
- **Options Considered**: Filter params / GraphQL / POST body
- **Rationale**: Cacheable, shareable, curl-friendly. Inline aggregates.

#### D-050 — Day 1 authentication

`arcui` · API Design · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Bearer token from TOML
- **Options Considered**: Bearer token / No auth / mTLS
- **Rationale**: Day 1 auth without certificate infra. Auto-generated if blank. Federal upgrades to mTLS.

### capabilities

#### D-373 — Tool declaration

`capabilities` · API Design · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: `@tool` decorator from `arcagent.tools._decorator` with frozen `ToolMetadata`
- **Source**: Convention — every built-in uses this; AI-authored tools never write JSON Schema by hand

#### D-374 — Tool return shape

`capabilities` · API Design · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Plain strings (or JSON-serializable strings) — no Pydantic models exposed to LLM
- **Source**: Convention — `builtins/read.py`, `find.py` return `str`; errors as `"Error: ..."` strings

#### D-375 — Tool classification

`capabilities` · API Design · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: All 6 tools `read_only`; remediation/tailoring/live-scan deferred
- **Source**: Source doc §3, deliberate scope cut

### cross-cutting

#### D-626 — Core packages have one clean cross-package import

`cross-cutting` · API Design · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: A cross-package consumer uses only `import arcllm`, `import arcrun`, or `import arcagent`, followed by qualified public names. It does not use `from <core-package> ...` or import a core package's implementation submodule.
- **Scope**: This governs cross-package seams. A package's own implementation may import its internal modules. Deep cross-package imports are reserved for genuine separately installable extras, extensions, modules, or plugins with an intentionally public submodule API.
- **Alternatives**: Force package internals through their own root facade (rejected: circular imports and a service-locator `__init__`); allow convenient deep imports everywhere (rejected: internal file layouts become public contracts).
- **Rationale**: One qualified namespace makes ownership visible, keeps compatibility surfaces curated, and permits internal refactors without change amplification.

#### D-631 — Package metadata declares the public-facade compatibility floor

`cross-cutting` · API Design · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: A consumer that relies on the 0.9 root facades declares
  `>=0.9,<1` for those dependencies and type-checks the real installed seam
  without missing-import suppression.
- **Rationale**: A permissive `>=0.1` range claims compatibility with releases
  that cannot provide required symbols, while suppressed imports hide drift.

---

## 4. Identity & Trust

### arcllm

#### D-278 — Tamper-evident queue event log

`arcllm` · Identity & Trust · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Mandated Answer**: Append-only audit events
- **Citation**: NIST 800-53 AU-9

#### D-437 — Hash chain covers raw bodies + encryption envelope, no new integrity machinery

`arcllm` · Identity & Trust · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: `compute_hash()` already digests every field but `record_hash`; raw capture is tamper-evident for free (AU-10).

### arcagent

#### D-203 — Audit logs tamper-evident (append-only JSONL + OTel)

`arcagent` · Identity & Trust · from *Bio-Memory (ArcAgent)*

- **Mandate**: NIST 800-53 AU-9
- **Tag**: `auto-applied: federal-mandate`

#### D-352 — Marketplace install signing

`arcagent` · Identity & Trust · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Mandated Answer**: Sigstore signature verification required for federal install
- **Citation**: EO 14028 §4 (SBOM)
- **Category**: Security

#### D-353 — AST validator on agent-authored Python

`arcagent` · Identity & Trust · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Mandated Answer**: Required — blocked imports, blocked attrs, blocked calls; restricted-builtins compile
- **Citation**: NIST 800-53 SI-7(15), CM-5
- **Category**: Security

#### D-354 — Federal tier blocks agent-authored `.py` reload

`arcagent` · Identity & Trust · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Mandated Answer**: Reload only registers signature-verified capabilities at federal tier
- **Citation**: NIST 800-53 CM-5, CM-7
- **Category**: Security

### arctrust

#### D-502 — Artifact signing

`arctrust` · Identity & Trust · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Mandated Answer**: Definitions verified via arctrust .arcsig sidecars pinned to the operator key before execution; unsigned refused above personal tier.
- **Citation**: CLAUDE.md Pillar 2 (Sign); LLM03/ASI04; SPEC-047 pinned-not-TOFU fix
- **Category**: Security

#### D-503 — Per-call authorization

`arctrust` · Identity & Trust · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Mandated Answer**: Every node tool call passes PolicyPipeline with caller DID; first-DENY-wins; fail-closed.
- **Citation**: CLAUDE.md Pillars 1+3; NIST 800-53 AC/IA
- **Category**: Security

### arcprompt

#### D-474 — Loaded artifacts verified before use

`arcprompt` · Identity & Trust · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Mandated Answer**: Overlay signature verification is mandatory at every tier, with no tier-conditional bypass flag.
- **Citation**: CLAUDE.md Four Pillars — Sign (ADR-019); implemented by the overlay-signing decision in this section
- **Category**: Security

### arcteam

#### D-089 — Audit log integrity

`arcteam` · Identity & Trust · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Mandated Answer**: Chained HMAC (existing AuditLogger)
- **Citation**: NIST 800-53 AU-9

### arcui

#### D-003 — Agent identity verification

`arcui` · Identity & Trust · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Verifiable identity on connect
- **Priority**: security
- **Tier Notes**: Federal: DID + signed challenge. Enterprise/Personal: token. `auto-applied: federal-mandate` (NIST IA-2/IA-8)

#### D-019 — Auth model

`arcui` · Identity & Trust · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Three token types: viewer_token, operator_token, agent_token. Clear privilege matrix.
- **Priority**: security
- **Tier Notes**: Federal: DID challenge for agents.

#### D-030 — Agent token scope

`arcui` · Identity & Trust · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Shared agent_token with server-side binding. Server stamps agent_id on events.
- **Priority**: simplicity
- **Tier Notes**: Federal: upgrades to DID.

### cross-cutting

#### D-317 — Audit log integrity

`cross-cutting` · Identity & Trust · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: Tamper-evident, append-only, hash-chained
- **Citation**: NIST AU-9

#### D-318 — New package extras release artifacts

`cross-cutting` · Identity & Trust · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: SBOM required
- **Citation**: EO 14028

---

## 5. Security

### arcllm

#### D-199 — Enforcement Behavior

`arcllm` · Security · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Configurable — warn or block, default block
- **Alternatives**: Hard block only (rejected: too rigid for dev/staging); Three-tier warn/soft/hard (rejected: three thresholds, more complex)
- **Rationale**: Secure by default (block). `enforcement = "block"` raises `BudgetExceededError`. `enforcement = "warn"` logs + emits OTel event but allows the call, sets `response.metadata["budget_warning"] = True`. Alert threshold (default 80%) warns before the hard stop. Warn mode exists for dev/staging and non-federal deployments.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-200 — Unknown Classification Behavior

`arcllm` · Security · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Follows enforcement config — enterprise (warn) routes to default, federal (block) raises error
- **Alternatives**: Always fail closed (rejected: too strict for enterprise); Always fall back (rejected: security risk for federal)
- **Rationale**: Same enforcement toggle governs both budget and routing behavior. `enforcement = "warn"` (enterprise default): unknown classification logs a warning and routes to `default_classification`. `enforcement = "block"` (federal): unknown classification raises `ArcLLMConfigError`, fail closed. No data sent to wrong provider in federal mode.
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-243 — Token handling

`arcllm` · Security · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Env var only, vault-backed in production. Never filesystem, never logs
- **Priority**: Security
- **Tier Notes**: auto-applied: federal-mandate (NIST 800-53 IA-5)

#### D-244 — HTTPS enforcement

`arcllm` · Security · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Existing `_validate_https` validator. Both `.azure.com` and `.azure.us` are HTTPS
- **Priority**: Security
- **Tier Notes**: auto-applied: existing validator

#### D-245 — Startup validation

`arcllm` · Security · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: None — no key prefix validation, no URL domain validation. Let Azure API return auth errors
- **Priority**: Simplicity
- **Tier Notes**: All tiers: avoid false positives from over-validation

#### D-419 — InjectionModule ships OFF by default, opt-in per call

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Heuristic scanning can false-positive; flagging user intent is a policy concern the agent owns; keeps default path zero-cost.

#### D-420 — Pattern-corpus tier is the zero-dep default; semantic tier is `arcllm[injection-semantic]`

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Regex/substring corpus catches common attacks dep-free; embedding-cosine detection belongs behind an extra.

#### D-421 — InjectionModule flags/blocks only — never interprets, rewrites, or executes content

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Respects the transport boundary; semantic "is this an attack" judgement lives in arcagent/arcrun.

#### D-422 — Scans INBOUND user + tool-result content pre-provider, pre-redaction

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Tool results = ASI06 vector, user turns = LLM01; both untrusted-adjacent, scanned while text is original.

#### D-423 — Secret scanner folded in as a togglable `SECRETS` category → `[SECRET:TYPE]`

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Secrets are a leak class, not a subsystem; reuses the detect→redact path (D-094 tag format).

#### D-424 — Checksum validators (Luhn / mod-97 / ABA) gate entity matches

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Raw regexes false-positive on order numbers/IDs; cheap arithmetic validator cuts noise dep-free.

#### D-425 — Add gov/CUI entities: US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN, IPV6

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Federal deployments handle CUI beyond SSN/CC/email (SI-10 / SC-28).

#### D-429 — Semantic guardrails (grounding, correctness, toxicity) OUT OF SCOPE

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Require model reasoning + agent context → arcagent/arcrun. arcllm validates structure, not meaning.

#### D-430 — Stack: Injection above Security (sees original text); Guardrails just inside Audit (validates final resolve...

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Injection needs pre-redaction text; Guardrails must run post-Retry/Fallback and be recorded by Audit.
- **Full title**: Stack: Injection above Security (sees original text); Guardrails just inside Audit (validates final resolved response)

#### D-431 — New `ArcLLMInjectionError`, `ArcLLMGuardrailError` (subclass `ArcLLMError`)

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Distinct types let callers catch block-mode failures precisely without string-matching.

#### D-438 — Federal tier: AES-256-GCM envelope, DEK wrapped by vault/KMS key; plaintext never on disk when on

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Raw prompts/responses are an LLM02/LLM07 exfil target; envelope encryption (SC-28, AU-9) is the primary compensating control.

#### D-439 — Add per-record `classification` tag (default from config floor)

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Classification-aware handling (SI-12); drives retention/access filtering; aligns with routing classification.

#### D-441 — Right-to-erasure DROPPED (no crypto-shred)

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: GDPR/CCPA feature that conflicts with federal audit immutability/retention (AU-9/10/11, Federal Records Act); not an enterprise need; was the sole source of the impossible per-record shred. Retention (D-440) covers lifecycle, encryption (D-438) confidentiality.

#### D-446 — Tier behavior: personal plaintext chmod-locked; enterprise same (encryption recommended); federal encryptio...

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Stringency is metadata not a gate (ADR-019); every tier captures, hash-chains, chmod-locks, audits.
- **Full title**: Tier behavior: personal plaintext chmod-locked; enterprise same (encryption recommended); federal encryption+classification+retention required

#### D-447 — Encryption-key resolution reuses `vault.py` VaultResolver (allowlisted, TTL cache, KMS-wrapped)

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: DRY; wrapping key fetched exactly like an API key; credentials never touch the filesystem.

#### D-448 — GCM AAD binds ciphertext to `trace_id` + `timestamp`

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Anti-transplant: decryption fails if the record identity was altered; strengthens tamper-evidence beyond the SHA-256 chain.

#### D-457 — Each endpoint's key resolves through the same vault/env path as the base provider

`arcllm` · Security · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: No plaintext keys in TOML/pool state; health data holds counters/timestamps only, never message content.

### arcrun

#### D-135 — Shared memory between children?

`arcrun` · Security · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Strictly nothing**
- **Rationale**: Complete isolation. Fresh state per child. Results flow up via LoopResult only. Blast radius containment.
- **Category**: Security

#### D-136 — Cross-child communication?

`arcrun` · Security · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Parent only**
- **Rationale**: No sibling-to-sibling. Clean tree structure: parent spawns, children return results, parent aggregates.
- **Category**: Security

#### D-140 — Identity inheritance

`arcrun` · Security · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Minimal: run_id lineage only**
- **Rationale**: Unique `run_id` per child with `parent_run_id` for correlation. DID/auth is ArcAgent's concern when it overrides the spawn tool.
- **Category**: Security

#### D-177 — Default container constraints

`arcrun` · Security · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Maximum lockdown**
- **Rationale**: No network, read-only root FS, tmpfs /tmp (64MB), 256MB memory cap, 50% CPU, drop ALL capabilities, no-new-privileges, 64 PID limit. Model code can compute and write to /tmp. Cannot call APIs, write to FS, fork-bomb, OOM, or escape. Caller can relax.
- **Category**: Security

#### D-178 — Event integrity mechanism

`arcrun` · Security · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **SHA-256 hash chain**
- **Rationale**: Each event includes sequence number + hash of (previous_hash + event_data). Blockchain-like tamper-evidence. If any event modified or deleted, chain breaks. ~20-30 LOC. No crypto keys needed (integrity, not authentication). Maps to NIST AU-9, AU-10.
- **Category**: Security

#### D-184 — NIST control mapping scope

`arcrun` · Security · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Full audit (all applicable controls)**
- **Rationale**: Map every NIST 800-53 control arcrun touches (estimated 30-40+). Most thorough for FedRAMP authorization. Includes controls enabled by Phase 4 additions (SC-4, SC-39, AU-9, AU-10, SC-7, AC-25, SA-8, CA-8).
- **Category**: Security

#### D-187 — Container image management

`arcrun` · Security · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Caller specifies, no auto-pull**
- **Rationale**: Image name is required param. arcrun never pulls images or makes network calls. Operator pre-stages approved images. Air-gap safe for SCIF/disconnected environments.
- **Category**: Security

#### D-599 — Sandbox Uses Caller-Provided Checker

`arcrun` · Security · was `DECISION-014` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Caller-provided checker
- **Context**: Sandbox needs to determine what a tool is doing (path access, network, etc.) but tools are caller-defined — arcrun doesn't know tool internals.
- **Options**:
- Caller-provided checker — SandboxConfig takes optional async check(tool_name, params) callback
- Tool name allow/deny list only — binary, no param inspection
- Tool-declared capabilities — Tools declare what resources they access
- Convention-based param inspection — scan params for 'path', 'url', etc.
- **Reasoning**: Caller knows their tools best. arcrun makes zero assumptions about tool parameter shapes. Default is no-op (allow all when no sandbox). arcrun ships utility functions (path_checker, etc.) that callers compose into their checker. Maximum flexibility, zero coupling to tool internals.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-600 — Phase 1 Sandbox = Tool-Level + Caller Checker

`arcrun` · Security · was `DECISION-015` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Tool-level + caller checker
- **Context**: How much sandbox logic should Phase 1 include?
- **Options**:
- Tool-level + caller checker — tool name allowlist + optional check function. Caller implements path checking via their checker using arcrun's path_checker utility.
- Full path checking in Phase 1 — tools declare path_params, sandbox validates against allowed/denied paths
- Just tool-level — only allow/deny by name, no path checking until Phase 4
- **Reasoning**: Tool name allowlist provides the security gate. Caller-provided check function enables granular control (path checking, network checking, etc.) without arcrun needing to understand tool internals. arcrun ships path_checker utility for callers who want it. Phase 4 adds container isolation and deeper analysis.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-601 — Allowlist Security Model

`arcrun` · Security · was `DECISION-016` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Allowlist model
- **Context**: What does "deny-by-default" mean when a sandbox is configured?
- **Options**:
- Allowlist — only tools in allowed_tools can run, everything else denied
- Denylist — all tools allowed unless in denied_tools list
- Configurable — caller picks default policy
- **Reasoning**: Federal/enterprise safe. Callers explicitly opt in to what runs. Critical property: when dynamic tool registry adds a new tool mid-execution, it's automatically denied (not in allowlist). Prevents privilege escalation via self-extending agents. If no sandbox configured, all tools are allowed (opt-in security).
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-607 — jsonschema for Param Validation

`arcrun` · Security · was `DECISION-022` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: jsonschema library
- **Context**: Tool params need validation against input_schema before execute(). Runs on every tool call.
- **Options**:
- jsonschema library — standard, well-tested, clear errors. Adds one dependency.
- Manual validation — zero deps, covers required fields + basic types (~30 lines). No $ref/oneOf/pattern support.
- Pydantic (via arcllm) — available but doesn't validate arbitrary JSON Schema, only generates it.
- **Reasoning**: Standard, well-tested, handles edge cases. One-liner validation. Worth the dependency for correctness on every tool call.
- **Status**: Accepted
- **Date**: 2026-02-11
- **Note**: This conflicts with PRD constraint "zero dependencies beyond arcllm." Accepted as necessary tradeoff — incorrect param validation is a security/reliability risk. jsonschema is small and widely used.

#### D-608 — Dynamic Tools Denied by Default

`arcrun` · Security · was `DECISION-023` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Denied by default
- **Context**: When a tool is added to the dynamic registry mid-execution, should it be automatically allowed or denied?
- **Options**:
- Denied by default — not in allowlist, automatically denied. Caller must also update sandbox.
- Auto-allow — tools added via registry.add() are automatically allowed.
- Registry notifies sandbox — event-based coupling.
- **Reasoning**: Prevents privilege escalation. A self-extending agent can't grant itself dangerous capabilities just by adding tools. Caller must explicitly update both registry and sandbox for new tools. Two-step process is the security tax — worth it for enterprise/federal deployments.
- **Status**: Accepted
- **Date**: 2026-02-11

### arcagent

#### D-084 — Metadata sanitization

`arcagent` · Security · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: XML-escape all string values
- **Options Considered**: XML-escape / Trust registered data
- **Rationale**: Defense in depth. Matches SkillRegistry pattern. Prevents prompt injection via tool descriptions from extensions.

#### D-152 — URL access control

`arcagent` · Security · from *Feature: CDP Browser Module*

- **Choice**: **Dual-mode (configurable)**
- **Rationale**: Config setting: mode = 'allowlist' or 'denylist'. Allowlist for restricted/federal environments, denylist for open ones. Clear security posture per deployment.
- **Category**: Security

#### D-153 — JavaScript execution control

`arcagent` · Security · from *Feature: CDP Browser Module*

- **Choice**: **Enabled by default, toggle in config**
- **Rationale**: JS execution available as a tool. Config toggle: security.allow_js_execution = true/false. Simple on/off.
- **Category**: Security

#### D-154 — Credential/cookie handling

`arcagent` · Security · from *Feature: CDP Browser Module*

- **Choice**: **Configurable persistence**
- **Rationale**: Option to persist cookies/sessions to encrypted store for multi-step workflows. Ephemeral by default, opt-in persistence.
- **Category**: Security

#### D-169 — Bot token storage?

`arcagent` · Security · from *Feature: Telegram Messaging Module*

- **Choice**: **Environment variable**
- **Rationale**: `ARCAGENT_TELEGRAM_BOT_TOKEN`. Doesn't touch filesystem. Production can use vault-injected env vars.
- **Category**: Security

#### D-170 — Unauthorized user handling?

`arcagent` · Security · from *Feature: Telegram Messaging Module*

- **Choice**: **chat_id allowlist in config**
- **Rationale**: `allowed_chat_ids` list in arcagent.toml. Unauthorized messages silently ignored. Rejection logged to telemetry for audit.
- **Category**: Security

#### D-204 — Memory content encrypted at rest

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Mandate**: FIPS 140-2/3
- **Tag**: `auto-applied: federal-mandate`

#### D-205 — Memory content validated on read (integrity check)

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Mandate**: OWASP ASI-06
- **Tag**: `auto-applied: federal-mandate`

#### D-206 — PII/CUI filtered before storage

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Mandate**: NIST 800-53 SI-12
- **Tag**: `auto-applied: federal-mandate`

#### D-207 — Per-agent memory isolation

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Mandate**: NIST 800-53 AC-3
- **Tag**: `auto-applied: federal-mandate`

#### D-208 — Entity file classification tracking in frontmatter

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Mandate**: NIST 800-53 AC-16
- **Tag**: `auto-applied: federal-mandate`

#### D-222 — Memory poisoning defense

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Decision**: Sanitize on write (NFKC, strip zero-width/control chars, length limits) + boundary markers on retrieval. Reuses existing patterns.
- **Rationale**: Priority: security (ASI-06) + simplicity.
- **Category**: Security

#### D-223 — File access protection

`arcagent` · Security · from *Bio-Memory (ArcAgent)*

- **Decision**: Bash veto pattern for memory paths. Episodes append-only, how-i-work.md consolidation-only. **Sanitizer in `arcagent/utils/`** (shared, not in either memory module).
- **Rationale**: Priority: security. Self-contained at package level.
- **Category**: Security

#### D-265 — Token handling

`arcagent` · Security · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Two env vars: `ARCAGENT_SLACK_BOT_TOKEN`, `ARCAGENT_SLACK_APP_TOKEN`. Never filesystem, never logs
- **Priority**: Security
- **Tier Notes**: auto-applied: federal-mandate (NIST 800-53 IA-5)

#### D-266 — Token prefix validation

`arcagent` · Security · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Validate `xoxb-` and `xapp-` prefixes at startup. Log clear error if wrong
- **Priority**: Simplicity
- **Tier Notes**: 4 lines, saves debugging time

#### D-267 — Authorization

`arcagent` · Security · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Check `event.user` against `allowed_user_ids`. Empty = deny all (fail-closed). Silent ignore + audit
- **Priority**: Security
- **Tier Notes**: auto-applied: pattern-following

#### D-268 — Bot loop prevention

`arcagent` · Security · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Check `event.get("bot_id")` to skip bot messages (not `subtype == "bot_message"`)
- **Priority**: Security
- **Tier Notes**: auto-applied: deepened research

#### D-309 — Dynamic tool sandboxing

`arcagent` · Security · from *Feature: arc-core-hardening*

- **Choice**: Policy pipeline + restricted imports (AST validation, blocked imports, sandboxed ToolContext)
- **Priority**: security
- **Tier Variation**: Federal: agent-created tools DENIED. Enterprise: restricted imports enforced. Personal: restricted + warning

#### D-310 — Config architecture

`arcagent` · Security · from *Feature: arc-core-hardening*

- **Choice**: Contained TOML per package (each package owns its config, no cross-cutting)
- **Priority**: simplicity
- **Tier Variation**: Tier set independently per package config

#### D-340 — Workspace boundary stays — agent writes only inside workspace

`arcagent` · Security · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Agent-authored capabilities land in `<agent_root>/workspace/.capabilities/` (the only path the agent can write to). User-curated capabilities live in `<agent_root>/capabilities/` (read-only to agent).
- **Priority**: security (agent can't escalate by writing outside workspace)

#### D-348 — Policy is authoritative; skill `tools` field is descriptive

`arcagent` · Security · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: A skill's `tools: [...]` declares intent (which tools the skill expects to use). Runtime policy decides what actually executes. Mismatch (skill lists denied tool) registers the skill, emits `skill.tool_dependency_policy_denied` audit, surfaces at call-time refusal.
- **Priority**: security (no policy override via metadata)

#### D-359 — Validation script trust model — tier-specific TOFU

`arcagent` · Security · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: Same trust model applies to all self-executing agent-authored code (validators, `@tool`, `@hook`, `@background_task` files). Three tiers:
- **Priority**: security (TOFU is industry-standard for self-executing code) > modularity (one trust model across all agent-code paths)
- **Alternatives**: same AST gate as `@tool` files (rejected — auto-run-on-reload has different risk profile from LLM-invoked tool); separate stricter sandbox for validators (rejected — second sandbox flavor adds complexity for marginal gain).
- **Personal**: auto-run any agent-or-user-authored script. Toml toggle (`[security] auto_run_agent_code = true`) lets user disable.
- **Enterprise**: default-allow trusted (builtins + signed modules); default-deny new agent-authored code. First sight prompts user approval. Approved scripts persisted to toml policy with hash + timestamp + approver. Subsequent loads of unchanged scripts auto-approved by hash match.
- **Federal**: default-deny everything. Only Sigstore-verified bundles run. Agent-authored code never executes (denied at AST-load time per D-354). Approval flow goes through external compliance tooling, not in-band prompt.

#### D-363 — Denied capabilities not in manifest

`arcagent` · Security · from *Unified Capability System — Build Decisions (2026-04-28)*


Hidden from prompt manifest. LLM doesn't see what it can't use; no temptation to suggest unavailable actions to the user.

#### D-406 — In-band summary sanitized

`arcagent` · Security · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: `summary_text` routed through `_sanitize_context_output` before it re-enters the baseline (symmetry with the context.md flush).
- **Rationale**: ASI-06/LLM-01: prevents injection laundering into the persisted session baseline.

#### D-414 — Sanitizer: one shared, hardened impl

`arcagent` · Security · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: Converge the 4th inline copy onto `utils/sanitizer.py:sanitize_text`; harden IT to strip Unicode Tag chars (U+E0000–E007F), variation selectors, soft hyphen, U+180E, DEL — fixes invisible-instruction smuggling (M2) across every caller incl. SPEC-029's compaction flush. Add `truncation_suffix`.

#### D-418 — Harden + audit

`arcagent` · Security · from *Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)*

- **Choice**: Sanitize Tier-1 raw excerpts + fail-open; neutralize markdown headings in rollup body (anti section-spoofing); UTC clock throughout; audit events on lossy consolidation/rollup rewrites.

#### D-490 — Opt-in + sandbox floor

`arcagent` · Security · from *Coding-agent working directory — Build Decisions (2026-07-26)*

- **Choice**: `working_dir` = the launch cwd only when the agent opts in (`[tools] operate_in_launch_dir`, set by the coding blueprint) AND the dir is already inside `workspace + allowed_paths` (the folder-trust prompt puts it there). Boundary check unchanged.
- **Priority**: security > simplicity
- **Rationale**: Secure by default (off for every other agent); working_dir moves the root, never the fence — it can never widen the sandbox.

#### D-491 — State-persistence invariant

`arcagent` · Security · from *Coding-agent working directory — Build Decisions (2026-07-26)*

- **Choice**: Framework/module state (memory, sessions, context.md, identity, audit chain) persists via DIRECT workspace I/O, never by calling the LLM's file tools.
- **Priority**: security > simplicity
- **Rationale**: This is what makes the split safe: because agent state is written straight to the workspace (not through the cwd-rooted tools), moving the tools to your project never drags the agent's memory into your repo. A skill writing PROJECT files via the tools is correct; one saving AGENT state must use a workspace path. (ADR-029, CLAUDE.md)

#### D-504 — Input validation at trust boundaries

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Mandated Answer**: Run input schema-validated against [input].schema; definition free-text fields pass the same sanitizer discipline as Task fields.
- **Citation**: OWASP baseline; LLM01
- **Category**: Security

#### D-505 — Secret management

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Mandated Answer**: No secrets in workflow files, prompts, or schemas; vault-backed short-lived credentials only.
- **Citation**: OWASP baseline; LLM07; CLAUDE.md Security
- **Category**: Security

#### D-523 — Draft/sign split

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Agents and UIs author unsigned drafts only; signing is out-of-band via arc workflow sign with the operator key, which never enters the agent process. Any edit drops the definition back to draft (content hash changes).
- **Priority**: security
- **Alternatives**: agent-process signing on edit; no signing for workflows
- **Rationale**: If the authoring process could sign, prompt injection could author an exfiltration pipeline and bless it (LLM06/ASI04); mirrors arc approve and arc blueprint sign.
- **Deployment**: personal: unsigned drafts may run with an audit warning | enterprise: unsigned/agent-signed definitions refused, fail-closed | federal: unsigned/agent-signed definitions refused, fail-closed

#### D-524 — Trifecta accumulation scope

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Lethal-trifecta capability legs accumulate per RUN: the runner threads accumulated legs into every node's PolicyContext.session_capabilities. Hard v1 requirement.
- **Priority**: security
- **Alternatives**: per-node sessions reset legs (status quo behavior)
- **Rationale**: Fresh per-node sessions would let a workflow complete a forbidden composition across nodes that no single session could — a gate bypass by construction.

#### D-525 — Activation approval

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: At definition/edit time the validator computes the union of capability legs the graph can touch; a trifecta-spanning definition requires an operator-signed ApprovalGrant at first activation and on any widening edit — not per run.
- **Priority**: security
- **Alternatives**: approve every run; no activation gate
- **Rationale**: Reuses SPEC-035 HumanGate/arc approve machinery; per-run approval would train operators to rubber-stamp.
- **Deployment**: personal: auto-approvable for named compositions per existing policy | enterprise: operator grant required | federal: operator grant required; never auto-approved

#### D-526 — Gate reject semantics

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: The reviewer chooses per decision: reject-fail (terminate the run) or reject-revise (re-materialize the gated node's upstream with reviewer notes injected). Both audited.
- **Priority**: simplicity
- **Alternatives**: always fail; always send back
- **Rationale**: Matches BlastForge's fix-approval flow and real review behavior; some rejections mean stop, some mean fix.

#### D-527 — Side-effect idempotency

`arcagent` · Security · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Node attempt ids carry into tool dispatch as idempotency keys so a retried node cannot double-send or double-write (ARC-6).
- **Priority**: security
- **Alternatives**: rely on at-least-once and hope
- **Rationale**: Retry + external side effects without dedup turns 'at least once' into 'twice invoiced'.

#### D-542 — Spawn environment safety filter

`arcagent` · Security · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Mandated Answer**: LD_*, DYLD_*, PYTHONSTARTUP, and NODE_OPTIONS are stripped before any connector server process is spawned, so a manifest cannot hijack the interpreter.
- **Citation**: OWASP ASI05 (unexpected code execution); pattern taken from OpenClaw's stdio env safety filter
- **Category**: Security

#### D-554 — Secret storage ladder

`arcagent` · Security · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: A tier-selected SecretStore seam. Personal and self-hosted default to a readable per-agent env file; an optional local vault sits above it; enterprise and federal use their own cloud vault (AWS Secrets Manager, Azure Key Vault). Once a vault is configured it also holds the agent's other keys and its DID.
- **Priority**: simplicity
- **Alternatives**: Encrypted rows in arcstore with the key from the OS keyring (no plaintext anywhere, one story on Mac and headless); OS keyring holding each secret directly (strongest platform protection, but headless Linux has no session bus); External vault only, everywhere (strongest federal answer, but nothing ships until that infrastructure exists)
- **Rationale**: Josh's ruling: enterprise and federal bring their own vault, personal stays simple and readable. Keeps the turnkey case unconfigured while leaving the hardened path a configuration change rather than a rewrite.

#### D-555 — Env file location

`arcagent` · Security · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: Per-agent file at ~/arc/team/<agent>/arc.env, mode 0600. The gateway's Telegram bot token moves there in the same change and the machine-level ~/.arc/arc.env path is deleted.
- **Priority**: security
- **Alternatives**: Keep the single machine-level ~/.arc/arc.env with per-agent variable prefixes (nothing shipped has to change, but every agent process can read every other agent's credentials); Per-agent for new connector secrets only, leaving the gateway on the old path (smallest change, but two secret locations)
- **Rationale**: Consistent with the per-agent install decision (D-549) and stops one agent reading another's tokens. CON-13 forbids leaving the superseded path behind; the existing _upsert_env writer in arcgateway/connect.py:59 is repointed, not duplicated.

#### D-556 — Third-party server supply chain

`arcagent` · Security · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: The manifest pins an exact version and a sha256. `arc ext add` fetches once and hashes; every spawn re-verifies and refuses plus audits on mismatch. No fetch-at-launch is ever permitted, which kills the `npx -y` pattern. At enterprise and federal the server process additionally runs sandboxed.
- **Priority**: security
- **Alternatives**: Pin and verify with no sandbox at any tier (closes the real hole, one execution path everywhere, but no containment if a vetted dependency turns bad); Sandbox at every tier (strongest containment, but blocks shipping on sandbox infrastructure working on both Mac and DGX); Trust on first use with a fingerprint warning (cheapest, but trusts whatever arrived first and only warns after the fact)
- **Rationale**: OWASP ASI04/LLM03. Hermes's own AgentMail skill spawns `npx -y agentmail-mcp`, fetching whatever is newest at launch and handing it a live API key; that is the concrete hole this closes.

#### D-557 — Trifecta approval policy

`arcagent` · Security · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: A per-instance `approval` setting inside the extension block of the agent's toml. DEFAULT: incoming reads are free, ALL outbound calls are gated and require human approval via CLI, TUI, or UI. The operator may loosen it per connected account.
- **Priority**: security
- **Alternatives**: Reads free, outbound gated only for recipients outside a known-contacts list (fewer prompts, catches exfiltration, but lets routine outbound through unreviewed); Any outbound after an untrusted read gates (strictest literal reading, but an inbox triage would prompt dozens of times an hour); Manifest declares each connector's trust level (fewest prompts for internal work, but lets a bundle author declare their own source trustworthy); Gate purely by verb, ignoring what was read (predictable, but misses that the instruction came from a poisoned email)
- **Rationale**: Josh: default strict, operator opens it per account. Routes through the existing SPEC-035 mechanical approval path, where an operator grant is signed and pinned to the operator DID rather than accepted over chat.

#### D-563 — Tool-contract hashing (rug-pull defense)

`arcagent` · Security · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: At approval, hash each tool's name, description, and inputSchema. Re-check on every tools/list. A changed contract SUSPENDS the tool, emits an audit event, and requires explicit re-approval before it can be called again.
- **Priority**: security
- **Alternatives**: Hash and warn but keep the tool callable (nothing breaks unattended, but a rug-pull succeeds and is only visible afterwards); Package version + sha256 pinning as the whole defense (simplest, and adequate for local stdio servers, but no defense at all for hosted servers)
- **Rationale**: CVE-2025-54136 (CVSS 8.8) confirmed a server can serve a benign tools/list at approval time and swap in malicious tool descriptions later; a CSA benchmark across 45+ real servers measured >60% attack success. The MCP spec has no continuous re-verification mechanism. Package pinning cannot cover hosted servers because there is no package. Also per spec: tool `annotations` from untrusted servers are treated as untrusted and ignored in favor of Arc's own manifest.

#### D-564 — Sandbox policy and code path

`arcagent` · Security · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: ONE launcher and one code path at every tier. The SandboxPolicy object is the only thing that varies. Personal ships SandboxPolicy.none as the default — a no-op — so a laptop needs no AppArmor or KVM setup, and a single config setting turns real confinement on. Enterprise uses container-level policy, federal adds microVM isolation.
- **Priority**: modularity
- **Alternatives**: Sandbox unconditionally at every tier including personal (strongest, and nearly free at single-digit ms, but adds host setup to a laptop install); Sandboxing only at enterprise and above with a separate direct-spawn path for personal (simplest laptop story, but forks the code path by tier so the federal path is never exercised locally)
- **Rationale**: CLAUDE.md line 78 — tier is stringency metadata, not a gate — and line 119 — never ship a path that would need pillars retrofitted for federal. A no-op policy keeps the personal experience simple while the code path stays identical everywhere, so the federal path is the same path. Mechanisms available: sandbox-exec on macOS, bubblewrap on Linux, rootless Podman, Firecracker on Linux only.

#### D-565 — Bundle load root

`arcagent` · Security · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Connector bundles load through an UNTRUSTED root so the existing capability trust gate runs on every load. A new untrusted root kind is added for extensions. Bundles never load from a module scan root.
- **Priority**: security
- **Alternatives**: Load from inside the MCP module directory (simplest wiring, but module scan roots are trusted so every bundle would inherit trust it must not have); Load from the agent workspace root alongside skills (already untrusted with the gate wired, nothing new to build, but mixes third-party connector definitions into the agent's own home)
- **Rationale**: capability_loader.py:84-94 — _UNTRUSTED_ROOTS covers only workspace, global, and agent roots; each enabled module is appended as a TRUSTED scan root at agent_lifecycle.py:152-154. Third-party connector code must not inherit module trust. The gate re-verifies independently of any install-time check and denies on any exception, and per capability_loader.py:334-337 requiring a signature implies pinning a key — an unpinned floor is no floor.

#### D-628 — Host shell execution owns a bounded process group

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Personal-tier Bash runs each command in a new process session,
  incrementally retains bounded stdout/stderr, and treats timeout or caller
  cancellation as whole-process-group teardown with bounded drain and reap.
- **Rationale**: Killing only the shell leaves descendants alive, while
  `communicate()` can exhaust host memory before output truncation. Resource
  ownership must cover the command tree and every cancellation path.

#### D-642 — Secret files are authorized and read through one bounded fd

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Every standalone secret file is opened with `O_NOFOLLOW`, then
  type, owner, exact mode, and size are verified with `fstat` on that same file
  descriptor before a bounded read.
- **Rationale**: `exists`/`stat` followed by `read_text` authorizes a pathname
  that an attacker can swap. One descriptor binds authorization to the bytes
  actually read and rejects devices, FIFOs, symlinks, and oversized secrets.

#### D-645 — Secret-store replacement locks the full cross-process transaction

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Env-file reads and mutations coordinate through a private 0600
  advisory lock; mutations hold it from snapshot read through atomic replace.
  Store size, entry count, key shape, and value length are bounded.
- **Rationale**: Atomic rename prevents torn files but cannot prevent two
  processes from replacing each other's independently derived snapshots.

#### D-646 — Session journals replay through bounded typed streaming

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Session resume streams records off the event loop under explicit
  file, line, and record-count ceilings and admits only validated public message
  and checkpoint shapes. Incomplete tails are ignored until completed.
- **Rationale**: Whole-file parsing lets a local/crafted journal block or exhaust
  the agent, while syntactically valid scalars and malformed message dictionaries
  otherwise fail later at a less controlled boundary.

#### D-654 — Outbound URL authorization includes DNS destinations

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Browser and web egress authorize canonical HTTP(S) URLs before
  use and again after redirects/provider resolution, resolving DNS off the event
  loop and rejecting every non-global answer. JavaScript and downloads default
  off; tests inject a resolver through a context-local seam.
- **Rationale**: String allowlists do not stop localhost, metadata services,
  suffix confusion, redirects, or DNS answers that cross the network boundary.
  Test determinism must come from injection, not a production fail-open path.

#### D-659 — Authored Python crosses an isolated JSON execution seam

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Untrusted authored Python is never imported by ArcAgent. Literal
  tool metadata is extracted statically and calls cross a bounded JSON-only
  proxy into ArcRun's configured Docker/VM backend; unsupported resident hooks
  and lifecycle code fail closed.
- **Rationale**: Signatures and AST restrictions establish provenance and lint
  intent, not containment. Host `exec` exposes secrets, file descriptors,
  process state, memory, and availability to any approved or escaped artifact.

#### D-665 — Extension entrypoints are not authored capability files

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: A manifest-declared attachment entrypoint is excluded from the
  extension capability scan. Authored `@tool` files still cross the isolated
  JSON seam; the separately declared plugin entrypoint follows the extension's
  signature, attachment, credential, and egress contract.
- **Rationale**: Treating every top-level Python file as an authored tool both
  rejects valid attachment plugins and conflates two trust mechanisms. The
  exception is manifest-exact, not a general fallback for decorator-free code.

#### D-667 — Isolation backends are acquired only for executable artifacts

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Capability scanning constructs a tier isolation runner lazily,
  at the first isolated authored tool, rather than when inspecting a root.
- **Rationale**: Eager VM resolution makes manifest-only extensions and skills
  uninstallable on a non-execution host and confuses the ability to verify an
  artifact with authority to execute it. Actual execution still fails closed if
  the tier backend is unavailable.

#### D-661 — Workspace file authorization is descriptor-relative

`arcagent` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Built-in file operations walk the selected authorized root with
  no-follow directory descriptors, authorize and read the final descriptor, and
  commit writes through a fsynced same-directory temporary plus identity-checked
  dirfd replacement.
- **Rationale**: Resolving a safe pathname before a later open leaves every
  component vulnerable to rename/symlink substitution. Authorization must bind
  to the descriptors and inode identity that actually supply or receive bytes.

#### D-636 — Bundle signature is verified before materialize, never after

`arcagent` · Security · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `arc module install` verifies the bundle's Ed25519 signature and each
  member file's content hash before a single byte is written to the workspace. A
  failed verification writes nothing and leaves no partial tree. Verification is
  required at every tier; the tier knob selects which issuer keys are trusted, not
  whether to verify. Every attempt emits `module.bundle.verified`,
  `module.signature_invalid`, or `module.content_hash_mismatch`.
- **Alternatives**: Extract then verify then roll back on failure (simpler streaming
  extract, but unverified code sits on disk in the window between, and a crash leaves
  it there); verify at load rather than install (catches tampering after install too,
  but pays the cost every startup and still lets unverified bytes land).
- **Rationale**: This is the same shape `arcrun/backends/loader.py` already enforces
  for executor backends and D-556 enforces for connector bundles. Writing first and
  checking second is the pattern that produced ASI04 findings elsewhere. Absence of a
  partial tree is also what makes uninstall and reinstall idempotent.

#### D-637 — Module runtime code never lands anywhere an agent can write

`arcagent` · Security · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: Three locations with three jobs.
  `${ARC_CONFIG_DIR:-~/.arc}/modules/` holds module *runtime* code — `_runtime.py`,
  hook handlers, background loops: deployment-level, written `0444` in `0555`
  directories, outside the tool fence (`workspace + allowed_paths`), writable only by
  `arc module install` / `arc module remove` running as the operator. A module's tools
  and skills are copied to the agent's capability dir instead (D-648), where they are
  writable on purpose. The workspace stays agent state and working data — never module
  code, never a symlink to module code.
- **Alternatives**: Materialize inside the workspace behind a sandbox exclusion and
  a read-only mode (keeps everything an agent uses in one tree, but the exclusion is
  now the only thing standing between an agent and its own capability source, and one
  `allowed_paths` mistake reopens it); materialize inside the workspace and
  re-verify signatures at every load instead of a write barrier (detects tampering,
  but detection after the fact is not containment, and it pays verification cost on
  every start).
- **Rationale**: ADR-029 reserves the workspace for agent state, and the reasoning
  applies with more force to executable code than to memory: a module tree inside a
  directory the agent writes to is a way for the agent to rewrite its own capabilities
  between runs — ASI05 and ASI06 in one move. A signature checked at install says
  nothing about a file edited afterwards. Putting the code somewhere the agent has no
  path to at all is a stronger control than any permission bit or sandbox rule,
  because it removes the reach instead of guarding it. The line falls between runtime
  and capability because they differ in kind: a tool is a leaf the agent invokes and
  may improve, while `_runtime.py` owns background loops, hook registration, and
  shared state for every agent on the box. Editing the first changes what one agent
  can do; editing the second changes the harness.

### arctrust

#### D-627 — Neutral security primitives live below model and agent layers

`arctrust` · Security · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: ArcTrust owns reusable PII detection, redaction mechanics, and
  canonical secret-pattern detection. ArcLLM and ArcAgent consume that public
  neutral API; ArcAgent owns policy composition and ArcRun does not re-export
  these unrelated security utilities.
- **Rationale**: Both the model adapter and agent layer need identical mechanics,
  but placing them in either consumer creates a reverse dependency or turns the
  execution facade into a junk drawer. A neutral owner preserves the one-way
  package graph and one canonical security implementation.

### arcmemory

#### D-492 — Dataset, workspaces, and credentials never committed or written in plaintext

`arcmemory` · Security · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Mandated Answer**: LongMemEval JSON files, the 500 throwaway agent workspaces, and all run artifacts are gitignored. Judge and provider API keys are read from the environment only, never from a config file in the repo.
- **Citation**: OWASP baseline (no compliance-mandates.json present; tech.md declares regime fedramp/nist but ships no mandate table)
- **Category**: Security

#### D-493 — Benchmark content is untrusted input at the memory boundary

`arcmemory` · Security · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Mandated Answer**: No separate validation layer is added. Haystack text is untrusted third-party content and already crosses the existing trust boundary: FastCapture runs sanitize() then privacy_filter() then windowed dedup before anything is stored. Bypassing that path to ingest faster would both violate the boundary and invalidate the measurement.
- **Citation**: OWASP baseline (input validation at trust boundaries); LLM01 prompt injection; arcmemory/capture.py
- **Category**: Security

### arcprompt

#### D-652 — arcui signs from the capability inventory, and the agent has no path to it

`arcui` · Security · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: The arcui capability inventory grows an Approve action on any gated
  row, calling the same code path as `arc trust approve` and signing with the same
  operator key arcui already uses for prompt overlays. The endpoint is operator-
  authenticated, is not reachable from `/ws/chat`, and is not exposed as a tool on any
  registry. Every approval emits an audit event carrying the operator DID, the
  artifact path, and the source hash signed.
- **Alternatives**: Approve by chatting with the agent (zero new UI, but it makes the
  agent the channel for its own privilege escalation, which is ASI09 exactly, and the
  same reason connector tokens never transit arcui chat); CLI only, no UI (smallest
  surface, and the CLI is the auditable path anyway — but the person who reviews a
  drifted skill is looking at a diff in the browser, and forcing a terminal switch is
  how review turns into rubber-stamping).
- **Rationale**: The inventory already renders every gated capability with its status
  and source, so the review surface exists and only the action is missing. Keeping the
  endpoint off the chat socket and off every tool registry is the whole security
  content of this decision: an agent that could reach it could improve its own skill
  and then approve its own improvement, which converts the trust gate into a
  formality.

#### D-466 — Overlays outside agent tool reach

`arcprompt` · Security · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Overlays live in the agent's config root (team/<agent>/context/), structurally outside the workspace subtree the agent's file tools are confined to. Written only by arcui and arccli acting as operator.
- **Priority**: security
- **Alternatives**: extend DEFAULT_PROTECTED_NAMES to denylist the context/ tree; allow agent self-writes with signed audit events
- **Rationale**: An agent able to rewrite its own system prompt is ASI01 goal-hijack and ASI06 context-poisoning in one, and would bypass the operator gate entirely. Placement puts the file out of the tool's addressable range with no denylist to maintain and no path-normalization slip to exploit — secure by default rather than by configuration.

#### D-467 — Overlays are signed and verified before injection

`arcprompt` · Security · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Every overlay carries an Ed25519 operator signature, verified by arcprompt before the text is injected. Verification failure refuses the load. Same rule at every tier, no bypass flag. The run-start audit event records source (stock|overlay) and sha256 per prompt.
- **Priority**: security
- **Alternatives**: hash and audit only, following the identity.md precedent; hash now, signing deferred until prompts become distributable
- **Rationale**: Hash-and-audit only records tampering after the model has consumed the text; verification refuses to load it. Against Arc's assumed threat model — a potentially hostile host — that difference is decisive. The key infrastructure is already ambient (DIDs, arctrust.keypair, an operator DID on the arcui write path), so the key-management cost is largely imaginary. CLAUDE.md's Sign pillar admits no carve-out for locally authored artifacts. That identity.md is unsigned is a gap to flag, not a precedent to extend.
- **Deployment**: federal: identical code path; FIPS-validated crypto per tier requirements | enterprise: identical code path | personal: identical code path; self-signed operator key acceptable

#### D-468 — Sign-on-write editing flow

`arcprompt` · Security · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: arcui signs with the operator key as part of the save. On the box, `arc prompt edit <pkg>/<name>` opens $EDITOR and signs on close. A raw editor save produces an unsigned file that fails to load with a message naming the fix (`arc prompt sign ...`).
- **Priority**: simplicity
- **Alternatives**: detached .md.sig sidecar; signature stored in frontmatter
- **Rationale**: Signing is only sustainable if the ergonomics match what people already do; friction is what drives operators to disable verification. Signing as a side effect of the normal write path means no separate step to remember, and the failure message converts a raw vim save from a mystery into a one-command fix. Where the signature bytes physically live (sidecar vs frontmatter) is left open — see Open Questions.

#### D-472 — No secrets in prompt text

`arcprompt` · Security · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Mandated Answer**: Prompt writes run the existing arcui secret scanner (_find_secret) before persisting; a detected credential rejects the write. Prompts are treated as exfiltrable.
- **Citation**: OWASP LLM07 System Prompt Leakage; OWASP baseline (no declared compliance regime)
- **Category**: Security

#### D-473 — Input validation at trust boundary

`arcprompt` · Security · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Mandated Answer**: Frontmatter parses through a Pydantic model at the arcprompt load boundary; unparseable frontmatter raises rather than degrading (see D-463).
- **Citation**: OWASP baseline; CLAUDE.md "Pydantic models for all data boundaries"
- **Category**: Security

#### D-481 — Signing authority resolved per-request, never hardcoded

`arcprompt` · Security · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: The write path never names a key. It calls a `SigningAuthority` seam that resolves the signing identity from the authenticated principal on the request. Today that resolves to the single deployment operator key at `~/.arc/` for any operator-role caller; when arcui moves to per-user login, the same seam resolves to the logged-in user's own key with no call-site change. The audit event records the **resolved** signer DID, never a constant.
- **Priority**: modularity
- **Alternatives**: arcui holds one process-level operator key referenced directly at the call site; arcui writes unsigned and the CLI signs separately; browser-side WebCrypto signing
- **Rationale**: Per-user login is a stated near-term direction, so the variable to design around is *which principal signs*, not *which key the server holds*. Binding the call site to a single process key would have to be unpicked in every handler later; a resolver seam makes that migration a one-implementation change. The audit half is not cosmetic — SPEC-017 (`tier-must-flow-through-construction`) is the in-repo precedent where enforcement was correct while the audit event recorded a hardcoded fallback, so federal audit trails lied. Recording a constant `signer_did` while the real signer varies would reproduce that defect exactly. Accepted interim exposure: until per-user login lands, holding the shared operator token confers signing authority — which still strictly improves on today's baseline, where `identity.md` is writable through that same token and carries no signature at all.

### arcteam

#### D-090 — Data classification labeling on stored memory

`arcteam` · Security · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Mandated Answer**: Required on all entities
- **Citation**: NIST 800-53 RA-2

#### D-091 — Encryption at rest for memory store

`arcteam` · Security · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Mandated Answer**: AES-256 (tier-gated)
- **Citation**: NIST 800-53 SC-28, FIPS 140-3

#### D-092 — Memory content in transit between agents

`arcteam` · Security · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Mandated Answer**: mTLS required
- **Citation**: NIST 800-53 SC-8

#### D-109 — Classification Access Control

`arcteam` · Security · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Agent has max_classification in config. TeamMemoryService checks entity classification against agent clearance on every read/search. Entities above clearance are invisible.
- **Priority**: Security — zero-trust, least-privilege. NIST 800-53 AC-3.
- **Tiers**: Federal — hard block. Enterprise — warn + block. Personal — classification ignored.

#### D-110 — Promotion Validation

`arcteam` · Security · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: promote() validates schema (Pydantic), classification label presence. UNCLASSIFIED auto-approved. CUI+ queued for human approval (async). All promotions audit-logged.
- **Priority**: Security — human-in-the-loop for sensitive data entering shared knowledge graph (OWASP ASI-09).
- **Tiers**: Federal — human approval for CUI+ required. Enterprise — configurable. Personal — all auto-approved.

#### D-111 — Approval Queue

`arcteam` · Security · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: CUI+ promotion requests sent as messages to `memory-approval` channel via existing MessagingService. Approval/rejection sent as reply messages.
- **Priority**: Simplicity — reuses existing messaging infrastructure, no new storage mechanism.
- **Tiers**: Federal — channel always exists, promotions blocked until approved. Enterprise — configurable. Personal — no channel.

### arcui

#### D-001 — Agent-to-UI encryption in transit

`arcui` · Security · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: TLS 1.2+ minimum
- **Priority**: security
- **Tier Notes**: Federal: mTLS. Enterprise: TLS. Personal: optional (localhost). `auto-applied: federal-mandate` (NIST 800-52r2, SC-8)

#### D-027 — No secrets in events

`arcui` · Security · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Sanitized payloads
- **Priority**: security
- **Tier Notes**: `auto-applied: federal-mandate` (NIST IA-5)

#### D-028 — Control requires operator role

`arcui` · Security · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Role-gated
- **Priority**: security
- **Tier Notes**: `auto-applied: federal-mandate` (NIST AC-3)

#### D-029 — WS idle timeout

`arcui` · Security · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: 5-min timeout with heartbeat keepalive
- **Priority**: security
- **Tier Notes**: `auto-applied: federal-mandate` (NIST SC-10)

#### D-053 — Authorization model

`arcui` · Security · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Two roles: viewer (read-only) + operator (read + write)
- **Options Considered**: Two roles / Single role / RBAC
- **Rationale**: Least-privilege. Dashboard on wall = viewer. Person at keyboard = operator.

#### D-054 — Data redaction

`arcui` · Security · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: No redaction — trust the viewer role
- **Options Considered**: No redaction / Configurable / Classification-aware
- **Rationale**: TraceRecords are operational metadata, not secrets. Access control is the right gate.

### arctui

#### D-487 — No secrets in code or plaintext on disk

`arctui` · Security · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Mandated Answer**: arctui stores no credentials; model/provider keys are resolved through arcllm/vault, never written to arctui code or config.
- **Citation**: OWASP baseline (no declared regime)
- **Category**: Security

#### D-488 — Input validation at trust boundaries

`arctui` · Security · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Mandated Answer**: The folder path, selected agent id, and model id are validated before use; folder access is granted only through the explicit trust gate (D-484).
- **Citation**: OWASP baseline (no declared regime)
- **Category**: Security

### capabilities

#### D-377 — Sanitization determinism

`capabilities` · Security · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Code-driven, deterministic — `sanitize.py` runs once at ingest, mapping reviewable
- **Source**: Source doc §4, §9 honesty-gap answer

### cross-cutting

#### D-313 — Inter-package transport encryption

`cross-cutting` · Security · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: TLS 1.2+ minimum; mTLS at federal
- **Citation**: NIST 800-52r2, SC-8

#### D-316 — Platform credential storage at federal

`cross-cutting` · Security · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: Vault-backed only; never on disk
- **Citation**: NIST IA-5

#### D-319 — Session/state files at rest (federal)

`cross-cutting` · Security · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: AES-256 / FIPS 140-3
- **Citation**: NIST SC-28

#### D-320 — Voice transcript handling

`cross-cutting` · Security · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: PII; bidirectional redaction at federal/enterprise
- **Citation**: NIST 800-53 SI-12

---

## 6. Audit & Compliance

### arcllm

#### D-242 — Audit trail

`arcllm` · Audit & Compliance · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Inherited from AuditModule in stack. Every invoke() logged. API key excluded
- **Priority**: Security
- **Tier Notes**: auto-applied: federal-mandate (NIST 800-53 AU-2)

#### D-277 — Audit trail on queue operations

`arcllm` · Audit & Compliance · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Mandated Answer**: All state changes (enqueue, dequeue, send, timeout, reject) logged
- **Citation**: NIST 800-53 AU-2

#### D-440 — Retention: `max_age_days` + `max_bytes` drive rotation + whole-file purge

`arcllm` · Audit & Compliance · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Raw-by-default grows unbounded; AU-11/SI-12 require defined lifecycle; purge never rewrites live chain lines.

#### D-444 — Turning raw capture off emits an audited `config_change` record

`arcllm` · Audit & Compliance · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Capture is ON by default; disabling it is a logged security-relevant action (AU-2) — no silent blinding.

### arcrun

#### D-386 — Audit the turn-0 force-pin

`arcrun` · Audit & Compliance · from *tool_choice passthrough — review follow-ups (2026-06-18)*

- **Choice**: Add `forced_tool_choice` to the `turn.start` payload in `arcrun/strategies/react.py` when `turn_count == 0 and state.tool_choice is not None` (~2 lines)
- **Priority**: security (audit) > simplicity
- **Rationale**: A forced first-turn tool call is an authority-shaping decision invisible in the current audit trail — NIST AU gap; post-hoc review can't distinguish model-elected vs orchestrator-forced first calls. Closes at the single emission point.
- **Tier Notes**: Federal: also captured in `SignedChainSink`.

### arcagent

#### D-072 — Audit prompt catalog rebuilds

`arcagent` · Audit & Compliance · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Mandated Answer**: Log `prompt.tools_catalog_rebuilt` and `prompt.roster_rebuilt` events
- **Citation**: NIST 800-53 AU-2

#### D-128 — What audit trail?

`arcagent` · Audit & Compliance · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Module Bus events + metadata update**
- **Rationale**: Emit schedule:fired/completed/failed/skipped. Update ScheduleEntry metadata. Flows to existing telemetry.
- **Category**: Audit

#### D-202 — All memory writes emit audit events

`arcagent` · Audit & Compliance · from *Bio-Memory (ArcAgent)*

- **Mandate**: NIST 800-53 AU-2
- **Tag**: `auto-applied: federal-mandate`

#### D-264 — Audit trail

`arcagent` · Audit & Compliance · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Every message and auth rejection is a telemetry event. No tokens in events
- **Priority**: Security
- **Tier Notes**: auto-applied: federal-mandate (NIST 800-53 AU-2)

#### D-350 — Capability lifecycle audit

`arcagent` · Audit & Compliance · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Mandated Answer**: Emit on register / unregister / replace / registration_failed
- **Citation**: NIST 800-53 AU-2
- **Category**: Audit

#### D-351 — Audit log integrity

`arcagent` · Audit & Compliance · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Mandated Answer**: Tamper-evident, append-only for capability lifecycle
- **Citation**: NIST 800-53 AU-9
- **Category**: Audit

#### D-355 — Capability source in audit content

`arcagent` · Audit & Compliance · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Mandated Answer**: Track scan root and source classification (builtin / module / agent / global) on every capability event
- **Citation**: NIST 800-53 AU-3
- **Category**: Audit

#### D-501 — Audit trail

`arcagent` · Audit & Compliance · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Mandated Answer**: All workflow lifecycle events (created/edited/signed/activated, run state changes, router choices, loop iterations, gate resolutions) emit AuditEvents to the operator-signed WORM chain with actor DID.
- **Citation**: fedramp/nist regime — steering tech.md ## Compliance; NIST 800-53 AU; CLAUDE.md Pillar 4
- **Category**: Audit & Compliance

#### D-522 — Gate resolution identity

`arcagent` · Audit & Compliance · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Real human DID on gate resolutions is a named prerequisite (SPEC-057 control-plane identity); until it lands, resolutions record the operator role DID and the run view flags them as role-attributed.
- **Priority**: security
- **Alternatives**: ship with role-DID silently (status quo)
- **Rationale**: arcui routes/tasks.py hardcodes did:arc:ui:operator — 'who approved this' is unanswerable per-person today; the workflow audit story requires the person.

#### D-552 — What lands in the audit trail

`arcagent` · Audit & Compliance · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: FULL capture. Every connector call and response, inputs and outputs, is recorded and encrypted at rest with classification labels.
- **Priority**: security
- **Alternatives**: Metadata only, never content (zero leak risk and small logs, but the work cannot be reconstructed); Metadata always with content gated by classification (middle ground, but leaves reconstruction gaps)
- **Rationale**: Josh: federal requires knowing everything that goes and comes. NIST 800-53 AU-3 demands records sufficient to reconstruct the event. Accepted cost: the audit store becomes high-value and must be access-controlled accordingly.

#### D-553 — Credential values carve-out

`arcagent` · Audit & Compliance · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: The single exception to full capture. A credential read records vault, item, field, caller, and outcome, but never the secret value.
- **Priority**: security
- **Alternatives**: No exception, record secret values too (maximum reconstruction, but the audit chain becomes a credential database); Record values encrypted under a separate auditor-only key (full reconstruction with day-to-day readers blind, at the cost of a second key to manage and rotate)
- **Rationale**: Prevents the tamper-evident audit chain from becoming the richest credential target on the box, and preserves the standing rule that credentials never sit in plaintext. Redaction is implemented with the existing arcllm._pii detector (SECRETS category), applied in the MCP module BEFORE the event reaches arctrust.audit.emit — arctrust is a leaf and must not import arcllm.

### arcprompt

#### D-476 — Audit inherited from arctrust sinks

`arcprompt` · Audit & Compliance · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: No separate decision. Audit schema, storage, and retention are inherited from the existing arctrust audit sinks; the prompt-specific content is the per-run source+sha record.
- **Priority**: simplicity
- **Rationale**: No declared compliance regime in .claude/steering/compliance-mandates.json, so no retention or classification mandate auto-applies. Change-management tracking for federal is handled by D-471's policy freeze.

### arcteam

#### D-088 — Audit trail on team memory operations

`arcteam` · Audit & Compliance · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Mandated Answer**: Every read/write/search logged
- **Citation**: NIST 800-53 AU-2

### arcui

#### D-002 — Connection lifecycle audit events

`arcui` · Audit & Compliance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: All state changes audited
- **Priority**: compliance
- **Tier Notes**: Same across tiers. `auto-applied: federal-mandate` (NIST AU-2)

#### D-022 — Registration audited

`arcui` · Audit & Compliance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: AU-2
- **Mandate**: `auto-applied: federal-mandate`

#### D-023 — Control commands audited

`arcui` · Audit & Compliance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: AU-2
- **Mandate**: `auto-applied: federal-mandate`

#### D-024 — Auth failures audited

`arcui` · Audit & Compliance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: AU-2
- **Mandate**: `auto-applied: federal-mandate`

#### D-025 — Subscription changes audited (federal)

`arcui` · Audit & Compliance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: AU-6
- **Mandate**: `auto-applied: federal-mandate`

#### D-026 — Tamper-evident UI audit log

`arcui` · Audit & Compliance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: AU-9
- **Mandate**: `auto-applied: federal-mandate`

#### D-052 — Audit target for config mutations

`arcui` · Audit & Compliance · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Same TraceStore, different event type
- **Options Considered**: Same TraceStore / Separate audit log / ArcAgent audit
- **Rationale**: One store, one chain, one verify_chain(). Type field distinguishes LLM calls from admin actions.

### capabilities

#### D-376 — Audit emission per tool call

`capabilities` · Audit & Compliance · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Framework `audit_event()` on every invocation, sensitive-key redaction on
- **Source**: `auto-applied: federal-mandate` (NIST 800-53 AU-2, AU-9) + Arc four-pillars (CLAUDE.md ADR-019)

### cross-cutting

#### D-314 — Audit scope (gateway msgs, cron runs, skill installs)

`cross-cutting` · Audit & Compliance · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: Every state-changing op audited
- **Citation**: NIST AU-2

#### D-315 — Audit log retention

`cross-cutting` · Audit & Compliance · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Mandated Answer**: ≥1 year (3 years recommended)
- **Citation**: NIST AU-11

---

## 7. Observability

### arcllm

#### D-241 — Telemetry

`arcllm` · Observability · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Inherit all OTel tracing/audit from module stack. `provider` = `"azure_openai"` in spans
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-279 — Queue metrics export

`arcllm` · Observability · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Mandated Answer**: OTLP-compatible metrics
- **Citation**: FedRAMP CA-7

#### D-287 — Queue Telemetry

`arcllm` · Observability · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Otel span attributes on existing span — `queue.wait_ms`, `queue.depth_at_entry`, `queue.rejected`, `queue.call_timeout`
- **Priority**: Simplicity — zero new spans, piggyback on OtelModule's `llm.invoke` span
- **Alternatives**: Dedicated metrics counters/histograms (rejected — more code, less essential), Both spans + metrics (rejected — over-engineering for v1)
- **Rationale**: Immediately answers "was it slow because of queue or provider?" in any OTLP backend. Audit events (auto-applied) cover the compliance mandate independently.
- **Tiers**: Federal: audit events always emitted regardless of Otel | Enterprise/Personal: Otel attributes when enabled
- **Deepen notes**: [`builds/arcllm-call-queue/research.md`](builds/arcllm-call-queue/research.md)

#### D-435 — Flip default: `store_raw_bodies=True` — capture full request + response

`arcllm` · Observability · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Metadata-only can't answer "what was actually sent/received"; full forensic replay is a hard requirement (ASI10, LLM05/09). Compensating controls keep it federal-safe.

#### D-436 — Reuse existing `request_body`/`response_body` fields; no parallel `*_raw`, no linked record

`arcllm` · Observability · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Fields already exist; adding parallel ones duplicates; inline keeps one record = one atomically-hashed unit.

#### D-442 — Replay READ path (`load_for_replay`) in arcllm; EXECUTION in arcrun

`arcllm` · Observability · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: arcllm reconstructs the request object only; re-invoking a model would put loop logic in the wrong package.

#### D-443 — Lineage token persisted VERBATIM, never constructed by arcllm

`arcllm` · Observability · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Lineage (template/RAG provenance) is built by arcrun/arcagent; arcllm has no visibility and must not fabricate it.

### arcrun

#### D-142 — Event propagation

`arcrun` · Observability · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Bubble up with prefix**
- **Rationale**: Child events propagate to parent bus with `child.{run_id}.` prefix. Full observability across the tree.
- **Category**: Observability

### arcagent

#### D-221 — Telemetry

`arcagent` · Observability · from *Bio-Memory (ArcAgent)*

- **Decision**: Follow existing OTel pattern via `AgentTelemetry.audit_event()`. 7 event types: retrieval, consolidation, note_created, working_updated, identity_updated, episode_created, reflect.
- **Rationale**: Priority: compliance (NIST AU-2) + simplicity.
- **Category**: Observability

#### D-263 — Telemetry events

`arcagent` · Observability · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `slack:message_received/sent`, `slack:notification_sent`, `slack:auth_rejected`, `slack:connected/disconnected`, `slack:error`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-308 — Telemetry for new components

`arcagent` · Observability · from *Feature: arc-core-hardening*

- **Choice**: Bus events + OTel spans (both internal and external)
- **Priority**: observability
- **Tier Variation**: None

#### D-521 — Telemetry reuse

`arcagent` · Observability · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: No new telemetry system: per-node execution reuses the hash-chained arcrun EventBus + arcstore spool; the run view joins on pinned run_id exactly as the existing timeline does; the group channel is the human-readable story.
- **Priority**: simplicity
- **Alternatives**: dedicated workflow event stream
- **Rationale**: The tamper-evident chain and the timeline join already exist and are tested; a second stream would drift.

#### D-540 — MCP call telemetry

`arcagent` · Observability · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Mandated Answer**: Every MCP call emits an OTel span carrying server, instance, tool, latency, and outcome. Tool-invocation telemetry rides the path arcrun already uses; correlation threads through run_id.
- **Citation**: Arc CLAUDE.md §Security — full observability on every action; existing arcrun tool telemetry
- **Category**: Observability

### arcprompt

#### D-475 — Prompt telemetry rides existing audit emission

`arcprompt` · Observability · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: No separate decision. The run-start snapshot (D-461) emits one audit event listing every prompt used with its source and sha256; prompt resolution failures raise and are audited (D-463).
- **Priority**: simplicity
- **Rationale**: Prompt telemetry rides the existing arctrust.audit.emit single emission point and the run span; no new export target, metric, or sampling decision is introduced.

### arcteam

#### D-107 — Event Flow

`arcteam` · Observability · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Audit-only for Phase 1. Memory operations log to AuditLogger. No messaging events. Agents discover updates on next retrieval. Messaging events deferred to Phase 3/NATS.
- **Priority**: Simplicity — zero coupling between memory and messaging systems.
- **Tiers**: Federal requires audit. Enterprise/Personal same.

#### D-108 — Telemetry Scope

`arcteam` · Observability · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Full telemetry from start — AuditLogger for compliance, structured logging for operations, OpenTelemetry spans for distributed tracing. arcteam gets OTEL dependency.
- **Priority**: Security — full observability required for federal. "Can this be audited?"
- **Tiers**: Federal — all required, 100% sampling. Enterprise — configurable sampling. Personal — audit optional, OTEL opt-in.

### arcui

#### D-020 — UI operations traced

`arcui` · Observability · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: OpenTelemetry spans on all UI operations
- **Priority**: compliance
- **Tier Notes**: `auto-applied: federal-mandate` (NIST AU-12)

#### D-021 — Multi-agent aggregation

`arcui` · Observability · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Per-agent sub-aggregators + global rollup. Reuses RollingAggregator class.
- **Priority**: simplicity

#### D-051 — arcUI self-telemetry

`arcui` · Observability · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Minimal OTel spans
- **Options Considered**: Minimal OTel / None / Full OTel + Prometheus
- **Rationale**: Dog-fooding. HTTP requests, WS connections, queries, config mutations. Same tracer pattern.

#### D-065 — Span timeline sub-phases

`arcui` · Observability · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Sub-phase timing in TraceRecord + OTel child spans
- **Options Considered**: Total duration only / Sub-phase timing / Full OTel child spans
- **Rationale**: TelemetryModule needs to emit timing for: prompt assembly, token estimation, LLM API call, tool execution, post-processing. TraceRecord stores `phase_timings: dict[str, float]` (ms). arcUI renders as horizontal span bar matching demo. OTel child spans for collector integration.

#### D-066 — Circuit breaker module

`arcui` · Observability · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: New CircuitBreakerModule in ArcLLM module stack
- **Options Considered**: No circuit breaker / Per-provider state machine / External service
- **Rationale**: RetryModule handles per-call retries but doesn't track provider health state. CircuitBreakerModule wraps adapter: tracks consecutive failures, transitions CLOSED→OPEN (trip after N failures)→HALF_OPEN (probe after cooldown)→CLOSED (on success). Per-provider state. Emits state transitions as events. arcUI displays provider circuit state table.

#### D-067 — Budget state visibility

`arcui` · Observability · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Queryable via ConfigController + OTel spans
- **Options Considered**: OTel only / Queryable API / Both
- **Rationale**: BudgetAccumulator already tracks monthly_spend, daily_spend, limits, enforcement. ConfigController exposes `GET /api/budget` returning per-scope spend vs limits. arcUI displays budget gauges (spent/limit) per scope. Alerts at threshold_pct.

#### D-068 — Per-agent cost breakdown

`arcui` · Observability · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Server-side rolling window keyed by agent label
- **Options Considered**: Client-side compute / Server-side aggregate / Pre-computed in TraceStore
- **Rationale**: BudgetAccumulator scopes map to agents. Rolling window aggregation (decision #22) adds agent dimension. arcUI cost tab shows per-agent cost bar chart matching demo layout.

---

## 8. Integration

### arcllm

#### D-246 — Error mapping

`arcllm` · Integration · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Raw body passthrough via `ArcLLMAPIError`. No Azure-specific error code parsing
- **Priority**: Simplicity
- **Tier Notes**: All tiers: caller sees full Azure error JSON

### arcrun

#### D-139 — NATS for distributed execution?

`arcrun` · Integration · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **In-process asyncio (v1)**
- **Rationale**: Children run as asyncio tasks in same process. Design interfaces so NATS is a drop-in later.
- **Category**: Integration

#### D-174 — Container runtime for isolation sandbox

`arcrun` · Integration · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Docker SDK with configurable socket**
- **Rationale**: Works with both Docker and Podman via socket config. Docker SDK (docker-py) is mature. Podman implements Docker's API. Zero extra LOC for dual runtime. Podman is superior for fed/enterprise (rootless, SELinux native, FIPS mode) but Docker SDK is the portable abstraction.
- **Category**: Integration

### arcagent

#### D-087 — Overlap with API tool schemas

`arcagent` · Integration · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: Complementary
- **Options Considered**: Complementary / Enriched-only / Replace
- **Rationale**: API schemas = WHAT (parameters). Prompt catalog = WHEN/WHY (guidance). Different concerns, both valuable.

#### D-150 — CDP connection management

`arcagent` · Integration · from *Feature: CDP Browser Module*

- **Choice**: **Launch + connect**
- **Rationale**: Module can optionally launch a headless Chrome process and connect via CDP WebSocket. Self-contained.
- **Category**: Integration

#### D-151 — Tool registration

`arcagent` · Integration · from *Feature: CDP Browser Module*

- **Choice**: **Auto-register on module load**
- **Rationale**: BrowserModule subscribes to agent:startup event, registers all browser tools via ToolRegistry. Tools appear automatically when module is enabled.
- **Category**: Integration

#### D-166 — Long message handling?

`arcagent` · Integration · from *Feature: Telegram Messaging Module*

- **Choice**: **Smart split at paragraph boundaries**
- **Rationale**: Double-newline first, sentence boundaries second, hard-split at 4096 as fallback. Sequential messages.
- **Category**: Integration

#### D-167 — Response formatting?

`arcagent` · Integration · from *Feature: Telegram Messaging Module*

- **Choice**: **Plain text only**
- **Rationale**: No parse mode. Agent output sent as-is. Zero formatting bugs. Can add HTML later.
- **Category**: Integration

#### D-168 — Processing acknowledgment?

`arcagent` · Integration · from *Feature: Telegram Messaging Module*

- **Choice**: **Typing indicator only**
- **Rationale**: Send TYPING chat action before processing. Expires after ~5s. No "Processing..." messages cluttering chat.
- **Category**: Integration

#### D-224 — Retrieval trigger

`arcagent` · Integration · from *Bio-Memory (ArcAgent)*

- **Decision**: Inject how-i-work.md + working.md via assemble_prompt. Agent uses memory tools for on-demand retrieval. No automatic retrieval decision logic.
- **Rationale**: "LLM is the intelligence layer."
- **Category**: Integration

#### D-225 — Light consolidation

`arcagent` · Integration · from *Bio-Memory (ArcAgent)*

- **Decision**: On agent:shutdown via spawn_background. Non-blocking. Failure-tolerant.
- **Rationale**: Priority: simplicity + scalability.
- **Category**: Integration

#### D-226 — Deep consolidation

`arcagent` · Integration · from *Bio-Memory (ArcAgent)*

- **Decision**: Scheduler module + CLI trigger. No internal timer.
- **Rationale**: Separation of concerns.
- **Category**: Integration

#### D-269 — Import strategy

`arcagent` · Integration · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Lazy import in `start()`. If slack-bolt not installed, log warning and stay dormant
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-270 — DM channel caching

`arcagent` · Integration · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Cache channel ID after first `conversations.open` call. Reuse for all notifications
- **Priority**: Simplicity + Performance
- **Tier Notes**: DM channel IDs don't change

#### D-528 — Trigger seam

`arcagent` · Integration · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: ScheduleEntry gains a typed action (kind=prompt | workflow_run): a workflow [trigger] materializes as a schedule whose firing calls the run entry directly — deterministic dispatch, no model deciding to start. Event triggers (message/webhook) deferred; the action.kind seam accommodates them.
- **Priority**: security
- **Alternatives**: free-text prompt asking the agent to run it (status quo)
- **Rationale**: The scheduler's only mechanism today is English-and-hope into agent_run_fn; a workflow trigger must be a hard structured dispatch.

#### D-529 — MCP scope

`arcagent` · Integration · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: MCP tools are out of scope for workflow nodes until the MCP transport is actually built (enum + dependency exist, zero dispatch).
- **Priority**: simplicity
- **Alternatives**: build MCP dispatch inside arcflow
- **Rationale**: Smuggling a transport into a workflow feature would mix concerns; MCP is its own spine work.

#### D-543 — External dependency failure handling

`arcagent` · Integration · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Mandated Answer**: Retry with exponential backoff, circuit breaker after repeated failures, and a hard timeout on every call. A failing connector's tools stay registered and return a structured error the agent can reason about rather than silently vanishing from its toolset.
- **Citation**: SPEC-056 Phase 1 task reliability engine; Arc CLAUDE.md §Scalability — circuit breakers, backoff, timeouts on everything external
- **Category**: Integration

#### D-558 — Server process lifecycle

`arcagent` · Integration · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: Lazy spawn on first use, reaped after an idle period. Nothing starts at boot; an agent that never calls Jira never pays for Jira. Both stdio and HTTP/SSE transports are supported because real upstreams use both.
- **Priority**: scalability
- **Alternatives**: Spawn every enabled connector at agent start (every call fast and breakage is loud at boot, but idle agents hold dozens of processes); Fresh process per call (cleanest isolation and no leaks, but roughly a second of startup tax on every call); One shared process per connector across the whole fleet (fewest processes, but agents would share one credential context, breaking per-agent identity)
- **Rationale**: Ten agents times several connectors is a hundred potential processes. Lazy spawn preserves the < 500ms cold start and < 50MB baseline budgets at the cost of one slow first call per connector.

#### D-562 — Atlassian upstream

`arcagent` · Integration · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Write our own adapter against the public Jira and Confluence Cloud REST APIs, packaged self-contained inside the extension. Seeded by the existing ~/.claude/skills/atlassian-jira (v1.2.0, REST API v3, issue CRUD, JQL, boards, workflows) and ~/.claude/skills/atlassian-confluence (v1.0.0, page CRUD, spaces, templates, permissions). No third-party Atlassian server is adopted.
- **Priority**: security
- **Alternatives**: Adopt sooperset/mcp-atlassian pinned to 0.23.0 with READ_ONLY_MODE and ENABLED_TOOLS (every mechanic wanted — PyPI pinning, headless PAT auth, per-tool allowlists, Cloud and Data Center — but 35 security advisories including CVE-2026-27825, a critical arbitrary-file-write to RCE, and a critical auth bypass, all patched only on 2026-07-10); Use Atlassian's hosted endpoint at mcp.atlassian.com (vendor-operated and vendor-audited, but nothing to pin or hash, Cloud only, no allowlist mechanism, browser OAuth, and Atlassian states it does not meet FedRAMP or HIPAA); Defer Atlassian entirely until the ecosystem settles
- **Rationale**: The know-how is already written; only the transport was missing. Headless API-token auth, Data Center support, no inherited CVE history, and the only Atlassian option that survives a federal review. Cost accepted: we own the maintenance.

### arcmemory

#### D-499 — Consolidation fires per session, and the harness waits for it

`arcmemory` · Integration · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: The eval agent's toml lowers the consolidation triggers so a pass fires at each session boundary and/or every 60 seconds. Critically, BOTH rate limits are lowered: the agent module's consolidate_event_threshold / consolidate_idle_seconds / consolidate_interval_seconds, AND arcmemory's own consolidate_interval_minutes (default 60) via backend.dynamics. The harness then waits for quiescence before asking the question rather than assuming the pass completed.
- **Priority**: Security
- **Alternatives**: Production cadence plus one final flush (rejected: operator wants per-session distillation). One consolidation at the end (rejected: a single window over 40 sessions is far outside the incremental regime the distill prompts were written for).
- **Rationale**: Consolidation is where arcmemory's distill prompts run, so it is the thing under test; if it does not fire, the run measures raw episodic recall and silently reports it as memory quality. Verified failure mode: brain.consolidate() gates on consolidator.due(now, interval_minutes=cfg.consolidate_interval_minutes), so polling every 60 seconds while that inner limit sits at 60 minutes returns an empty result every time. Lowering one limit without the other is a silent no-op.

### arcprompt

#### D-477 — Not applicable

`arcprompt` · Integration · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Not applicable. arcprompt is a leaf library with no external service connections, no message-bus participation, no protocol choice, and no inter-agent communication. Prompt resolution is local filesystem reads.
- **Priority**: simplicity
- **Rationale**: Recorded explicitly rather than skipped silently. If prompts ever become distributable (a hub, or optimizer-proposed variants arriving off-box), this category reopens — and so does the signing-scope question.

### arcteam

#### D-112 — Agent Wiring

`arcteam` · Integration · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: `TeamMemoryService` in arcteam is fully standalone — any framework calls it directly. For arcagent, a thin `TeamMemoryBridge` module hooks Module Bus events and delegates to TeamMemoryService. arcteam has zero knowledge of arcagent.
- **Priority**: Simplicity + Scalability — clean separation, future-proof for langchain/crewai.
- **Tiers**: All tiers same wiring.

#### D-113 — Concurrency

`arcteam` · Integration · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Reads are lock-free. Writes use `fcntl.flock` per entity file. Consolidation uses global `.consolidation.lock`. Same pattern as existing StorageBackend.
- **Priority**: Simplicity — zero new deps, proven pattern, self-cleaning.
- **Tiers**: All tiers same.

### arcui

#### D-031 — UIReporter ↔ arcllm

`arcui` · Integration · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Chain into TelemetryModule.on_event. Same pattern as agent.py:1273.
- **Priority**: simplicity
- **Tier Notes**: No arcllm changes.

#### D-032 — UIReporter ↔ arcrun

`arcui` · Integration · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Subscribe to ModuleBus events (existing arcrun bridge).
- **Priority**: simplicity
- **Tier Notes**: No arcrun changes.

#### D-055 — ArcAgent integration

`arcui` · Integration · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Optional arcui_bridge module
- **Options Considered**: Optional module / Direct bus sub / Shared queue
- **Rationale**: Decoupled — works LLM-only or full-stack. attach_agent() wires everything.

#### D-056 — CLI mode

`arcui` · Integration · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Not day 1, design for it
- **Options Considered**: Not day 1 / Day 1 / No CLI
- **Rationale**: REST API is the CLI (curl + jq). Thin wrapper trivial to add later.

### cross-cutting

#### D-335 — Natural-language cron parser

`cross-cutting` · Integration · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Deterministic parser first (regex/grammar for cron exprs, ISO timestamps, `every Nh`, `9am daily`, `30m`); arcllm fallback only on parse failure with strict JSON-schema response.
- **Priority**: simplicity (90% free); security (deterministic = federal auditable); scalability (no per-schedule LLM cost in common case)
- **Alternatives**: Always-LLM (cost + non-determinism); deterministic-only (loses NL UX); two-stage user-confirm (friction)
- **Rationale**: Best of both — predictable for the common case, NL flexibility on the long tail.
- **Tiers**: Federal: LLM fallback disabled by default; deterministic-only mode. Enterprise: fallback enabled with audit event per LLM-resolved schedule. Personal: full fallback.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-336 — Subagent delegation primitive

`cross-cutting` · Integration · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: arcrun gains a `spawn()` primitive (child execution context with own tool budget, identity, sandbox). The agent-facing tool lives in arcagent (`arcagent.tools.delegate`) and calls `arcrun.spawn(...)`. NOT routed through arcteam (which is fleet-level).
- **Priority**: modularity (arcagent decides WHAT, arcrun does HOW — matches CLAUDE.md split); simplicity (one new primitive, no NATS dep)
- **Alternatives**: New tool only in arcagent (loses arcrun integration); arcteam routing (overkill for ephemeral); both tools (UX confusion); skip
- **Rationale**: User explicitly said "arc run should handle the delegation through its spawn in the execution run." Spawning is execution, not coordination.
- **Tiers**: Federal: spawn requires explicit allowlist + own DID per child + delegation audit chain. Enterprise: warn on deep recursion. Personal: depth limit only.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

---

## 9. Performance

### arcllm

#### D-247 — Connection pooling

`arcllm` · Performance · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Inherit `httpx.AsyncClient` pool from `BaseAdapter`. One pool per adapter instance
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-288 — Default Concurrency

`arcllm` · Performance · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: `max_concurrent=2` per model instance
- **Priority**: Simplicity — matches existing `eval_config.max_concurrent` default, safe on all Azure deployments including GCC
- **Alternatives**: 1 (too restrictive — bio_memory + policy can't eval simultaneously), 5 (may hit rate limits on smaller deployments)
- **Rationale**: Conservative default. Override in config for specific needs.
- **Tiers**: Same across all tiers

#### D-289 — Default Call Timeout

`arcllm` · Performance · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: `call_timeout=60.0` seconds (from send time)
- **Priority**: Simplicity — covers all non-reasoning models. Reasoning models override in config.
- **Alternatives**: 120s (too long for fast models — stuck call holds slot for 2 min), No default (friction)
- **Rationale**: gpt-4-1-mini responds in 5-15s, gpt-4-1 in 10-30s. Reasoning models (o4-mini, o1) need explicit override to 120-180s.
- **Tiers**: Same across all tiers

#### D-387 — Anthropic cache_control placement

`arcllm` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Auto-place ≤3 breakpoints (last tool, system-as-block, rolling tail message) inside `AnthropicAdapter._build_request_body`, behind one config flag `enable_prompt_caching` (default on). NO type-system change; system string → 1-block list only when caching on.
- **Priority**: modularity > simplicity
- **Rationale / Tier Notes**: Keeps `cache_control` (Anthropic wire specific) inside the adapter; arcrun/arcagent stay provider-agnostic. Cascade + 4-breakpoint cap → ≤3 fixed is correct. Read side already wired (`_parse_usage` 156-165).

#### D-388 — Cache TTL default

`arcllm` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: 5-minute ephemeral default; 1-hour opt-in via config.
- **Priority**: security > scalability
- **Rationale / Tier Notes**: Smaller exfil window (LLM07) + half the write cost (1.25× vs 2×). Ceiling: agents whose turn cadence >5min re-pay writes — expose ttl for long-lived agents.

#### D-389 — OpenAI/Gemini cache telemetry read-back

`arcllm` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Map `usage.prompt_tokens_details.cached_tokens` → `Usage.cache_read_tokens` in `OpenaiAdapter._parse_usage`; mirror in SSE parser. Keep `google.py` inheriting `openai.py` (Gemini compat returns identical field). Leave `cache_write_tokens=None`.
- **Priority**: simplicity > modularity
- **Rationale / Tier Notes**: ~3 lines/site; billing (`telemetry_cost.py` 28-31) already consumes it. Forking Gemini parsing = boundary violation, zero payoff.

#### D-390 — prompt_cache_key / cachedContent

`arcllm` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Do NOT adopt (YAGNI).
- **Priority**: scalability
- **Rationale / Tier Notes**: A shared cache key overflows OpenAI's ~15 req/min-per-prefix ceiling at fleet scale and *reduces* hit rate; `cachedContent` is a heavyweight managed resource. Defer until a measured miss problem.

#### D-395 — Ollama model residency

`arcllm` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Warm KV cache via server env `OLLAMA_KEEP_ALIVE` (finite 30m–24h on shared boxes; `-1` only dedicated single-model hosts). Zero arcllm code.
- **Priority**: simplicity > scalability
- **Rationale / Tier Notes**: `keep_alive` is silently ignored on the OpenAI-compat `/v1` path (Ollama #11458); no body passthrough exists. `-1` disables idle eviction → OOM/503 risk on multi-model boxes. Native `/api/chat` override only if per-request control becomes a hard requirement.

### arcrun

#### D-137 — Resource budget splitting

`arcrun` · Performance · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **No enforcement in v1**
- **Rationale**: Depth limit only. Budget fields exist on RunState for observability but aren't enforced. Add enforcement later with real usage data.
- **Category**: Performance

#### D-138 — Parallel vs sequential spawning

`arcrun` · Performance · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Parallel via multiple tool calls**
- **Rationale**: When model emits multiple `spawn_task` calls in one turn, run concurrently via `asyncio.gather`. Model naturally expresses parallelism. Requires react loop change for concurrent tool execution.
- **Category**: Performance

#### D-182 — Concurrent spawn testing approach

`arcrun` · Performance · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Stress tests in `tests/security/test_timing_attacks.py`**
- **Rationale**: 10+ parallel spawns, nested parallel spawns, spawn+cancel race, spawn+steer race. Uses asyncio.gather + mock models with controlled delays. No new production code.
- **Category**: Performance

#### D-392 — Dynamic capability mid-run

`arcrun` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Surface new tools/skills via deferred menu meta-tool (name only, body into message tail) OR subagent (own registry + cache). Do NOT append to the live tools block as primary mechanism.
- **Priority**: scalability > security
- **Rationale / Tier Notes**: `use_skill` already does this. Deferred loading is O(1) in the tools block — the only pattern with no ceiling. Append-only grows the block unboundedly + forces a write.

#### D-394 — transform_context contract (arcrun side)

`arcrun` · Performance · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Document append-only contract on the seam (loop.py/streams.py/react.py/state.py docstrings); optional debug-flag-gated assertion returned-prefix==input-prefix. Never in the default hot path.
- **Priority**: modularity > simplicity
- **Rationale / Tier Notes**: Invariant's owner is the caller (arcagent). arcrun documents + optionally asserts; does not enforce caching semantics in the loop (<500ms cold start, no per-turn O(context) diff).

### arcagent

#### D-155 — Timeouts

`arcagent` · Performance · from *Feature: CDP Browser Module*

- **Choice**: **Per-tool defaults at registration (ArcRun enforces)**
- **Rationale**: Set appropriate timeout_seconds per RegisteredTool (navigate=30s, click=5s, screenshot=10s). ArcRun's existing asyncio.wait_for handles enforcement. No duplicate timeout logic.
- **Category**: Performance

#### D-156 — Screenshot format

`arcagent` · Performance · from *Feature: CDP Browser Module*

- **Choice**: **PNG base64 inline**
- **Rationale**: Return as base64-encoded PNG directly in tool result. Vision-capable LLMs process inline. No file management.
- **Category**: Performance

#### D-171 — Concurrent message handling?

`arcagent` · Performance · from *Feature: Telegram Messaging Module*

- **Choice**: **Sequential (asyncio.Queue)**
- **Rationale**: Queue inbound messages, process one at a time. Prevents session state race conditions. Same approach as OpenClaw's per-chat sequencing.
- **Category**: Performance

#### D-227 — Token budget enforcement

`arcagent` · Performance · from *Bio-Memory (ArcAgent)*

- **Decision**: Use existing `CHARS_PER_TOKEN` from `arcagent/utils/io.py`. Character-based estimation.
- **Rationale**: Priority: simplicity. No new dependencies.
- **Category**: Performance

#### D-228 — Working.md I/O

`arcagent` · Performance · from *Bio-Memory (ArcAgent)*

- **Decision**: Synchronous write in post_respond. Sub-ms for ~2KB.
- **Rationale**: Priority: simplicity. No benefit to async.
- **Category**: Performance

#### D-229 — Retrieval search

`arcagent` · Performance · from *Bio-Memory (ArcAgent)*

- **Decision**: Grep-based with wiki-link following (one hop). No database, no index.
- **Rationale**: Follows design doc primary path. Priority: simplicity.
- **Category**: Performance

#### D-271 — Connection model

`arcagent` · Performance · from *Slack Messaging Module (SPEC-011)*

- **Choice**: One WebSocket per bot via Socket Mode. slack-bolt handles reconnection
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: per-SDK

#### D-530 — Per-agent node concurrency

`arcagent` · Performance · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: The existing one-in-progress-task-per-agent cap holds for workflow nodes; parallelism comes from assigning parallel branches to different agents.
- **Priority**: simplicity
- **Alternatives**: lift the cap for workflow tasks; per-workflow concurrency setting
- **Rationale**: Zero changes to the battle-tested claim/dispatch race guards; a same-agent fan-out serializing is acceptable v1 behavior.

#### D-531 — Run budget

`arcagent` · Performance · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: RootTokenBudget lifted to the Run; concurrent node accounting reuses planning's reserve-then-settle grants verbatim; runner enforces run-level wall-clock, stall detection, and cancel fan-out (ARC-5).
- **Priority**: scalability
- **Alternatives**: per-node caps only; new budget implementation
- **Rationale**: The concurrency-safe accounting exists and is tested; per-node caps alone cannot stop a livelocked fleet.

#### D-544 — Connection and resource discipline

`arcagent` · Performance · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Mandated Answer**: A warm server process reuses its connection for the life of that process. Per-call timeouts and an idle reaper bound resource use; concurrency and backpressure ride the existing arcrun loop rather than a new mechanism.
- **Citation**: Arc CLAUDE.md §Scalability — cold start < 500ms, baseline memory < 50MB per agent, connection pooling and resource limits on everything external
- **Category**: Performance

### arcprompt

#### D-478 — Prompt I/O bounded by run-start pinning

`arcprompt` · Performance · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: No separate decision. Run-start pinning (D-461) is the caching strategy: prompts are read and hashed once per run, not per call, so per-turn prompt I/O is zero.
- **Priority**: simplicity
- **Rationale**: Reading roughly 25 small markdown files once per run is negligible against a single LLM call and preserves the sub-500ms cold-start budget. No connection pooling, backpressure, or batching concerns arise for local file reads.

### arcteam

#### D-114 — Index Freshness

`arcteam` · Performance · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Lazy rebuild with dirty flag. Writes touch `.dirty` marker. Next read checks and rebuilds if dirty. Amortizes cost to readers.
- **Priority**: Simplicity + Performance — clean separation of write and index concerns.
- **Tiers**: All tiers same.

#### D-115 — Budget Enforcement

`arcteam` · Performance · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: arcteam enforces per-entity file budget (800 tokens) during writes and consolidation. Retrieval budget (3000 tokens) is caller's responsibility. Clean ownership split.
- **Priority**: Simplicity — each package enforces what it owns.
- **Note**: Aligned with arcagent brainstorm RT-5 (retrieval budget enforcement).
- **Tiers**: All tiers same.

### arcui

#### D-033 — Max agent connections

`arcui` · Performance · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: 100 default, configurable. ~60KB per agent. 429 on exceed.
- **Priority**: scalability

#### D-057 — Event stream backpressure

`arcui` · Performance · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Server-side buffering with 100ms batched flush
- **Options Considered**: Server buffer + batch / Client filter / Sampling
- **Rationale**: Bounded deque, batched sends. All events still stored. Bandwidth-efficient.

#### D-058 — Aggregation strategy

`arcui` · Performance · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Server-side rolling windows (1h/24h/7d)
- **Options Considered**: Server rolling windows / Client compute / Pre-computed in store
- **Rationale**: O(1) per event. Client gets pre-computed stats. In-memory counters.

---

## 10. Extensibility

### arcllm

#### D-248 — Future auth extensibility

`arcllm` · Extensibility · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: API-key only, no preparation for Managed Identity. `_build_headers()` is the natural extension point
- **Priority**: Simplicity (YAGNI)
- **Tier Notes**: Future: subclass + override `_build_headers()` for Entra ID

#### D-426 — Entity categories individually toggleable via `pii_entities` allow/deny

`arcllm` · Extensibility · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Feature toggles via config, not code branches.

#### D-427 — Implement the allowlisted `pii_detector_class` loader spec-012 D-093/FR-13 specced but never built, mirrori...

`arcllm` · Extensibility · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Removes the `_VALID_DETECTORS` hard-reject; makes bring-your-own spaCy/Presidio real with the same ASI04 allowlist guard.
- **Full title**: Implement the allowlisted `pii_detector_class` loader spec-012 D-093/FR-13 specced but never built, mirroring `vault.py`

#### D-432 — Config: new `[modules.injection]`, `[modules.guardrails]`; extend `[modules.security]` with `pii_entities`...

`arcllm` · Extensibility · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Each module owns its TOML section; security enrichment stays in the security section.
- **Full title**: Config: new `[modules.injection]`, `[modules.guardrails]`; extend `[modules.security]` with `pii_entities` + `pii_detector_class`

#### D-433 — New optional extra `arcllm[injection-semantic]`

`arcllm` · Extensibility · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Semantic deps must never load when injection is pattern-only or disabled — zero-dep-when-disabled.

#### D-445 — New optional extra `arcllm[trace-encryption]` (cryptography), helper in `_trace_crypto.py`

`arcllm` · Extensibility · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Zero crypto deps in core; only federal encryption pulls it. Mirrors `arcllm[signing]`.

#### D-453 — Pool topology `[[endpoints]]` in provider TOML; strategy in `[modules.load_balance]`

`arcllm` · Extensibility · from *ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)*

- **Rationale**: Endpoints are provider connection settings; behavior is module config. Mirrors D-098.

### arcrun

#### D-588 — Adopt Context Transform Hook

`arcrun` · Extensibility · was `DECISION-003` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Adopt context transform hook
- **Context**: Long-running loops accumulate messages and hit context limits. pi-agent-core has `transformContext()` — a caller-provided hook to prune/compact messages before each LLM call.
- **Options**:
- Skip — caller manages context externally
- Adopt — `transform_context` callback in run() options
- **Reasoning**: arcrun manages the message list inside the loop. Without this hook, the caller has no way to prevent context overflow in long-running tasks. The hook keeps arcrun minimal (just calls the function) while giving callers full control over context management strategy.
- **Status**: Accepted
- **Date**: 2026-02-11

#### D-590 — Adopt Dynamic Tool Registry

`arcrun` · Extensibility · was `DECISION-005` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Adopt dynamic tool registry
- **Context**: pi-agent-core supports hot-reloading tools mid-session. Original arcrun design passes tools at run() and they're fixed.
- **Options**:
- Fixed tools only — passed at run(), immutable
- Dynamic registry — tools can be added/removed/replaced during execution
- **Reasoning**: Agents that self-extend (writing their own tools, loading MCP servers mid-task) need to modify available tools during execution. The registry is simple: a mutable dict of Tool objects that the loop reads each turn. Add/remove is just dict operations. Keeps the core simple while enabling powerful patterns.
- **Status**: Accepted
- **Date**: 2026-02-11

### arcagent

#### D-085 — Preamble configurability

`arcagent` · Extensibility · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: Configurable via TOML
- **Options Considered**: Hardcoded / Workspace file / TOML
- **Rationale**: Default hardcoded, overridable in `[tools]` config. Lets operators customize the guidance text per deployment.

#### D-086 — Roster TTL configuration

`arcagent` · Extensibility · from *Convention-Driven Prompt Injection — Build Decisions (2026-02-27)*

- **Choice**: `roster_ttl_seconds` in MessagingConfig
- **Options Considered**: MessagingConfig / Hardcoded
- **Rationale**: Configured via `[modules.messaging.config]` in TOML. Keeps config with the owning module.

#### D-230 — Config structure

`arcagent` · Extensibility · from *Bio-Memory (ArcAgent)*

- **Decision**: Follow design doc. Paths, budgets, retrieval, consolidation settings. All with sensible defaults. Zero-config works.
- **Rationale**: Priority: simplicity.
- **Category**: Extensibility

#### D-231 — Team discovery

`arcagent` · Extensibility · from *Bio-Memory (ArcAgent)*

- **Decision**: Module Bus event (`team:memory_available`). Zero coupling to team module. Retrieval scope expands when team is present.
- **Rationale**: Priority: simplicity + scalability.
- **Category**: Extensibility

#### D-272 — notify tool

`arcagent` · Extensibility · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Register `slack_notify_user` tool during startup. Agent decides when to send
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-273 — Tool name

`arcagent` · Extensibility · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `slack_notify_user` (prefixed to avoid collision with Telegram's `notify_user`)
- **Priority**: Simplicity
- **Tier Notes**: Explicit channel identification

#### D-361 — Conflict UX

`arcagent` · Extensibility · from *Unified Capability System — Build Decisions (2026-04-28)*


Last-wins everywhere with audit. No special shielding for core builtins. User explicitly wanted extensibility (better-implementation override).

#### D-362 — Hot reload trigger

`arcagent` · Extensibility · from *Unified Capability System — Build Decisions (2026-04-28)*


Explicit `reload()` call only. No file watcher, no auto-reload at session boundaries. Predictable + auditable.

#### D-364 — Malformed capability — skip + audit, agent keeps starting

`arcagent` · Extensibility · from *Unified Capability System — Build Decisions (2026-04-28)*


Frontmatter validation failure or AST rejection causes the single capability to be skipped with audit; agent continues startup. Errors surface in next `reload()` diff so the LLM can fix.

#### D-365 — Skill-usage instruction text location

`arcagent` · Extensibility · from *Unified Capability System — Build Decisions (2026-04-28)*


Hardcoded constant in the bus subscriber that injects the skills manifest. ~3 lines. Operator override deferred (would be a small TOML field if needed later).

#### D-367 — Bus event names for capability lifecycle

`arcagent` · Extensibility · from *Unified Capability System — Build Decisions (2026-04-28)*


`capability:added`, `capability:removed`, `capability:replaced`, `capability:registration_failed`. Parallels existing `agent:*` taxonomy.

#### D-532 — Node strategy field

`arcagent` · Extensibility · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: strategy on agent nodes threads into arcrun allowed_strategies: omitted = pinned react (deterministic default, no meta-selection call); single entry = forced; list = arcrun's existing select_strategy picks its best among them, audited. Registry-level worker entities (arcteam entity kind) deferred to Phase 4.
- **Priority**: modularity
- **Alternatives**: always let arcrun pick; always pin react
- **Rationale**: Gives both of Josh's modes with machinery that already exists; workflows stay predictable by default.

#### D-533 — First companion arcrun strategy

`arcagent` · Extensibility · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: subagents first: decompose, spawn bounded children that can message each other, synthesize — the strategy shape lives in arcrun with spawn/messaging execution bound in from arcagent (build_arcrun_run_fn pattern). reflect and map follow.
- **Priority**: modularity
- **Alternatives**: reflect first (cheapest); map first
- **Rationale**: Josh's call: closest to the graph-engineering vision of child agents that talk to each other. Noted as the hardest layering of the three; the arcrun boundary (strategies never see the graph) must hold.

#### D-559 — Bundle distribution

`arcagent` · Extensibility · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: Bundles ship in the arc repo and install from a bundled catalog. The manifest carries everything a hub would need (name, version, signature, source) so publishing to SkillVault later is a distribution change, not a redesign.
- **Priority**: simplicity
- **Alternatives**: Publish to SkillVault from day one (one distribution story and third-party publishing, but couples shipping ten connectors to hub work in another repo); Install from arbitrary git URLs (no catalog to build, but no curation, so 'only vetted upstreams' becomes hand-enforced); Connectors ship only inside blueprints (nothing new to distribute, but no way to add one to a running agent)
- **Rationale**: Nothing blocks on the separate SkillVault repo, while the manifest shape keeps the hub path open.

### arcprompt

#### D-471 — Tier variance via policy content, not code

`arcprompt` · Extensibility · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: arcprompt has one code path at all tiers. Tier stringency is expressed as PolicyPipeline content: a federal deployment can DENY prompt:write to freeze prompts after authorization; personal declares no rule, so the gate is a no-op ALLOW.
- **Priority**: modularity
- **Alternatives**: no tier variance at all; federal requires a countersignature (two-person integrity)
- **Rationale**: Consistent with ADR-019 — tier is stringency metadata, not a gate, and the pipeline runs identically everywhere. Reusing the existing policy layer gives federal a CM-3/CM-5 change-control freeze with zero tier branching in arcprompt. Two-person countersigning has no existing consumer; if it is wanted later, the mechanical operator-approval work (SPEC-035) is its natural home.

### arcteam

#### D-116 — Disabled Behavior

`arcteam` · Extensibility · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Null Object pattern. When `enabled = false`, service returns empty results, no-op on writes. Callers never need conditionals.
- **Priority**: Simplicity — zero error handling needed in callers.
- **Tiers**: All tiers same.

### arcui

#### D-034 — Transport abstraction

`arcui` · Extensibility · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: UITransport Protocol now. WebSocketTransport implementation. NATSTransport later.
- **Priority**: simplicity

#### D-059 — Frontend framework

`arcui` · Extensibility · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Vanilla HTML/CSS/JS
- **Options Considered**: Vanilla HTML/CSS/JS / Lit / HTMX
- **Rationale**: Matches demo. Zero build step. Ships in wheel. No node in a Python project.

#### D-060 — Page extensibility

`arcui` · Extensibility · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Convention-ready (HTML file + PAGES entry)
- **Options Considered**: Convention-ready / Plugin API / Fixed
- **Rationale**: Convention is the plugin system. API when modules prove they need custom UI.

### arctui

#### D-486 — Extensions are arc units the TUI surfaces; one real extension proves the seam

`arctui` · Extensibility · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Decision**: An extension IS an arc package/capability/hook unit (tools, skills, modules, memory, MCPs — already contributed through arc). arctui surfaces them; the terminal-specific contribution points (custom views/components, slash-commands, autocomplete providers, themes — pi-style) are the minimal additions, wired in a later phase. v1 proves the end-to-end seam by installing ONE real external extension. Extensions remain signed/policy-gated/audited (secure by default) and built-ins use the same public API (dogfood).
- **Priority**: Modularity (dogfood the extension API)
- **Alternatives**: Build a full public registry/marketplace + sandboxed third-party views up front (deferred — that's the ecosystem phase, not v1).
- **Rationale**: The wedge is the extensible ecosystem, but v1 is a vertical slice: prove the hook surface works with one real extension rather than shipping a marketplace.

### cross-cutting

#### D-322 — Skills Hub gating

`cross-cutting` · Extensibility · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: TOML toggle (`[skills.hub] enabled = false` default) **plus** CLI-only install path (`arc skill hub install <name>`). No agent-driven auto-install at federal.
- **Priority**: security (defense-in-depth: must enable AND must run CLI); simplicity (one toggle for the off case)
- **Alternatives**: Optional pip extra; toggle-only; signature-required-always
- **Rationale**: Two-step gate prevents accidental enablement leading to silent skill installs. User explicitly requested both layers.
- **Tiers**: Federal: hub blocked OR allowlisted source list only; signed skills only; install requires admin role. Enterprise: hub on, signature verify required, admin approval per install. Personal: hub off by default; once enabled, agent can request install with user confirmation.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-323 — Centralized slash command registry

`cross-cutting` · Extensibility · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Single `arccli.commands.registry` (CommandDef list) consumed by arccli, arcgateway, arctui, telegram/slack/discord platforms. One source of truth for dispatch + help + autocomplete + platform menus.
- **Priority**: simplicity (one file change to add/alias/category); modularity (each surface picks how to render but shares the catalog)
- **Alternatives**: Per-surface registries (drift); registry in arcagent (UX in agent layer)
- **Rationale**: Hermes' biggest single maintenance leverage; worth the cross-package read dep.
- **Tiers**: Same. Federal: command catalog can be filtered by config (hide commands not allowed by tier).
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-330 — Cross-session context reads (per-session ACL)

`cross-cutting` · Extensibility · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Each session carries an ACL: `private` | `shared-with-agent` | `shared-with-other-users-via-agent`. Tier sets defaults.
- **Priority**: security (information flow control); modularity (ACL surface is one field, enforcement is one gate)
- **Alternatives**: Always shared (info-flow violation); always isolated (loses value); memory-shared/history-isolated
- **Rationale**: Enables D-329's "agent can read across own sessions for context" while preserving multi-tenant safety.
- **Tiers**: Federal: default `private`; cross-session reads blocked unless user marks shared. Enterprise: default `shared-with-agent` within team; warn on cross-org. Personal: default `shared-with-agent`.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-332 — Pluggable terminal backends in arcrun

`cross-cutting` · Extensibility · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: arcrun exposes `ExecutorBackend` protocol; ships `local` and `docker` in core. `ssh`/`modal`/`daytona`/`singularity` ship as separate `arcrun-backend-{name}` packages or extras.
- **Priority**: modularity (arcrun = execution); simplicity (core stays tiny); security (tier can allowlist which backends are even loadable)
- **Alternatives**: Backends in arcagent (violates CLAUDE.md split); all in core (deps explosion); local-only
- **Rationale**: Honors Arc's package boundaries; matches existing sandbox.py location.
- **Tiers**: Federal: backend allowlist required; remote backends require approved network reachability. Enterprise: warns on unsigned backend plugins. Personal: any backend installed is usable.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-333 — Platform credential storage

`cross-cutting` · Extensibility · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Federal/enterprise must resolve via vault backend (extends existing `arcagent.modules.vault_azure` pattern; pluggable for HashiCorp/AWS/etc.). Personal uses `~/.arc/gateway.toml` with 0600 perms.
- **Priority**: security (credentials never on disk at federal — IA-5); simplicity (personal stays one-file)
- **Alternatives**: Vault always (personal friction); env+file (federal violation); OS keyring (no FIPS)
- **Rationale**: Builds on existing Arc secret hygiene.
- **Tiers**: Federal: vault required, hard error otherwise. Enterprise: vault preferred, env fallback warns. Personal: file or env, file 0600.
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

#### D-337 — Skill auto-creation nudge location

`cross-cutting` · Extensibility · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: New `arcagent.modules.skill_improver.nudge` submodule. Subscribes to module bus events (`tool.success`, `tool.error`, `user.correction`); counts per turn; injects a system message at threshold, calling existing skill_improver create/patch path.
- **Priority**: simplicity (uses existing event bus + skill_improver substrate); modularity (nudge is a thin trigger over existing reflector/evaluator/Pareto)
- **Alternatives**: New `compounding` module (yet another module); arcrun loop hook (violates CLAUDE.md); manual-only (loses self-improvement)
- **Rationale**: skill_improver already has the substrate (reflector, evaluator, Pareto, candidate_store); only the trigger logic was missing.
- **Tiers**: Same trigger; auto-created skills audited and human-confirmable at federal/enterprise (D-314 applies).
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

---

## 11. Testing

### arcllm

#### D-201 — Testing Strategy

`arcllm` · Testing · from *ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)*

- **Decision**: Standard TDD + security-specific tests
- **Alternatives**: Standard TDD only (rejected: budget/routing are security-critical modules)
- **Rationale**: Standard TDD (unit + integration, >=80% coverage) plus dedicated security tests. Budget security: bypass attempts, scope isolation, negative cost injection, overflow/underflow, config injection via scope string. Routing security: classification downgrade attempts, provider config injection, adapter isolation, audit trail completeness.

```
tests/
  unit/
    test_budget.py           # Accumulator, limits, periods, enforcement
    test_routing.py          # Selection, classification, adapter lifecycle
  integration/
    test_budget_telemetry.py # Budget inside TelemetryModule end-to-end
    test_routing_stack.py    # Router with full module stack
  security/
    test_budget_security.py  # Bypass, isolation, injection, overflow
    test_routing_security.py # Downgrade, injection, isolation, audit
```
- **Deepen notes**: [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md)

#### D-249 — Test strategy

`arcllm` · Testing · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Add to parametrized `CLOUD_PROVIDERS` tests + Azure-specific test file
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-250 — URL regression test

`arcllm` · Testing · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Explicit URL assertion with `'?' not in url` guard against api-version regression
- **Priority**: Security
- **Tier Notes**: Guards most likely regression path

#### D-290 — Test Strategy

`arcllm` · Testing · from *ArcLLM Call Queue — Build Decisions (2026-02-27)*

- **Decision**: Unit tests with mock inner adapter — 6 core test cases covering concurrency limiting, backpressure, send-time timeout, queue wait excluded from timeout, Otel attributes, and config loading
- **Priority**: Simplicity — pure async tests, no real LLM calls, fast CI
- **Alternatives**: Unit + integration with real provider (rejected — unnecessary for a concurrency primitive, integration tests exist for the adapter layer)
- **Rationale**: The queue is a pure asyncio wrapper. Its behavior is fully testable with mocks.
- **Tiers**: Same across all tiers

### arcrun

#### D-143 — Testing strategy

`arcrun` · Testing · from *Feature: Recursive Agent Spawning (ArcRun v1)*

- **Choice**: **Mock model + real spawn**
- **Rationale**: Mock model that emits `spawn_task` tool calls, with real nested `run()` calls. Tests the full pipeline.
- **Category**: Testing

#### D-180 — Adversarial test scope

`arcrun` · Testing · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Comprehensive (8 categories)**
- **Rationale**: Prompt injection, path traversal, resource exhaustion, event chain tampering, tool parameter injection, spawn depth bomb, steering injection, timing attacks. Covers OWASP LLM01, ASI02, ASI05, AU-9.
- **Category**: Testing

#### D-181 — Adversarial test location

`arcrun` · Testing · from *Feature: ArcRun Phase 4 — Hardening*

- **Choice**: **Dedicated `tests/security/` directory**
- **Rationale**: One file per attack category (8 files). Matches project test structure (unit/, integration/, security/, performance/). Run independently: `pytest tests/security/`.
- **Category**: Testing

### arcagent

#### D-130 — Testing strategy?

`arcagent` · Testing · from *Feature: Scheduling / Heartbeat / Cron MVP*

- **Choice**: **Unit (frozen time) + integration (mock LLM)**
- **Rationale**: Full scheduling logic coverage plus end-to-end with actual agent loop and mock provider.
- **Category**: Testing

#### D-157 — Testing strategy

`arcagent` · Testing · from *Feature: CDP Browser Module*

- **Choice**: **Mock CDP at WebSocket level**
- **Rationale**: Unit tests mock CDP WebSocket with canned responses. Integration tests use real headless Chrome. Standard test pyramid.
- **Category**: Testing

#### D-172 — Testing strategy?

`arcagent` · Testing · from *Feature: Telegram Messaging Module*

- **Choice**: **Mock bot API + real agent**
- **Rationale**: Unit tests mock python-telegram-bot's Bot class. Integration tests use real ArcAgent with mocked Telegram. Separate test bot for manual E2E.
- **Category**: Testing

#### D-274 — Mock strategy

`arcagent` · Testing · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Mock `AsyncApp`, `WebClient`, `AsyncSocketModeHandler`. Test handler logic
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-275 — split_message

`arcagent` · Testing · from *Slack Messaging Module (SPEC-011)*

- **Choice**: Duplicate in Slack (not shared utility). Module stays self-contained. Extract at N=3
- **Priority**: Simplicity
- **Tier Notes**: Module independence over DRY for N=2

#### D-311 — Test coverage target

`arcagent` · Testing · from *Feature: arc-core-hardening*

- **Choice**: 90% + adversarial security suite (~20 new test files)
- **Priority**: security
- **Tier Variation**: None

#### D-534 — Test strategy

`arcagent` · Testing · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: TDD per standing rules; coverage gates apply. Four mandatory E2E tests through real paths (producers-unwired guard): schedule trigger fires a real run; output_schema validation actually rejects a bad completion; unsigned definition actually refused at enterprise/federal; accumulated legs actually reach PolicyContext on node N>1. Full package matrix before merge.
- **Priority**: security
- **Alternatives**: unit coverage only
- **Rationale**: The repo's recurring failure is correct predicates with dead activating wiring (SPEC-034/035/037/038/040/043/044/056); these four seams are the likely victims.

#### D-560 — Test strategy

`arcagent` · Testing · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: CI runs a real MCP server we control, driven through the ACTUAL registry, policy, and audit path with no patched internals, asserting tool registration, allowlist denial, approval gating, and audit emission. Separately, vetting a new upstream requires a live conformance run against the real service, recorded as an evidence file in the bundle.
- **Priority**: security
- **Alternatives**: Mock the MCP client boundary (fast and tiny, but exactly the shape that let unwired producers pass green before); Live accounts in CI (true signal, but needs ten funded accounts and makes CI depend on other people's uptime); Recorded transcripts replayed in CI (realistic payloads with no credentials, but recordings go stale silently)
- **Rationale**: Arc has repeatedly shipped correct-looking predicates with dead wiring. Driving the real dispatch path is the only test shape that cannot hide it.

#### D-583 — Strategy

`arcagent` · Testing · was `TS-001` · from *Bio-Memory (ArcAgent)*

- **Decision**: 70/20/10 split. Unit per helper class (mock eval model), integration through bus, e2e with real model.
- **Rationale**: Follows project test pyramid.
- **Category**: Testing

#### D-640 — Absent-module safety is proven by removing, not by asserting

`arcagent` · Testing · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: A parametrized suite removes each optional module's materialized tree,
  starts a real agent, and runs a turn end to end. Green means absent-safe. The suite
  is parametrized over `discover_modules()` output, so a module added later is covered
  without anyone remembering to add a case.
- **Alternatives**: Assert that each module's `configure()` is skipped when absent
  (fast and unit-shaped, but proves only that the loader skipped it, not that nothing
  downstream needed it); a single test with all optional modules removed (one case
  instead of a dozen, but a failure names no module and passing all-off does not prove
  any individual combination works).
- **Rationale**: "Removable with no loss of function" is a claim the core can silently
  break the moment anything treats a module's contribution as guaranteed rather than
  nullable — a prompt section that assumes a memory block, a listing that assumes a
  tasks table. Only running without it finds that. This is the producers-unwired
  lesson: drive the real path or the test proves nothing.

### arcmemory

#### D-500 — Run scale, isolation, and what is measured

`arcmemory` · Testing · from *Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)*

- **Decision**: One clean throwaway workspace per question, never shared. Phased: LongMemEval-Oracle across all 500 questions first, then a stratified sample of LongMemEval-S (~50 questions covering all six types), then full S only once the harness is clean. Both benchmark metrics are recorded per question: QA accuracy judged by GPT-4o via the benchmark's evaluate_qa.py, and turn-level plus session-level memory recall accuracy from the dataset's has_answer flags and answer_session_ids. Results are JSONL keyed by question_id, resumable so an interrupted run continues. A preflight hard-fails if the embedder or distiller seam is unwired.
- **Priority**: Security
- **Alternatives**: A single shared workspace across all 500 haystacks (rejected: each instance is a different persona, so merging them makes consolidation fuse 500 contradictory identities and directly poisons the knowledge-update question type). Straight to full S (rejected: ~40,000 ingest calls before the first answer, paid twice on any harness bug). QA accuracy alone or recall alone (rejected: the benchmark defines both, and either alone turns a failure into a single bit that cannot be attributed).
- **Rationale**: Per-question isolation is what LongMemEval defines and what keeps a miss attributable. Oracle first buys a near-free end-to-end proof of ingest, consolidation, recall, answer, and judge before any real spend. Recording both metrics splits a failure into 'memory never surfaced it' versus 'memory surfaced it and the answer was still wrong', which are different bugs with different fixes. The preflight exists because both seams degrade silently today: embed_backend='none' drops recall to BM25 plus graph, and an empty distill_provider makes consolidation a no-op, either of which would produce a real-looking number for a crippled system.

### arcprompt

#### D-469 — Byte-identical migration assertion

`arcprompt` · Testing · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: Before any Python prompt constant is deleted, a test asserts the loaded markdown equals the original string byte-for-byte. Migrate one package at a time; the temporary test and the constant are deleted together once it passes.
- **Priority**: security
- **Alternatives**: golden-file snapshot of the fully assembled system prompt; both per-prompt bytes and an assembled snapshot
- **Rationale**: Prompts break invisibly — a lost trailing newline or collapsed blank line changes model behavior without failing anything. Byte equality against the exact string being replaced is the tightest possible proof of a faithful move, and this repo's history of shipping correct predicates with dead wiring makes a real-path assertion non-optional. Deleting the scaffold with the constant honors the no-vestigial-code standard.

#### D-625 — Dependency boundaries are checked in source and metadata

`arcagent` · Testing · from *ArcAgent Refactor and Hardening (2026-08-11)*

- **Decision**: Use non-vacuous AST tests for import direction/style, parse `pyproject.toml` for matching dependency direction, verify ArcRun qualified names belong to its declared `__all__`, and prove ArcAgent has no upper-layer import requirement.
- **Alternatives**: Grep-only checks (rejected: comments and aliases create false results); code review convention (rejected: does not prevent regression); metadata-only checks (rejected: undeclared workspace imports still couple packages).
- **Rationale**: Source and packaging are separate dependency graphs and both must enforce the architecture.

### arcteam

#### D-117 — LLM Testing Approach

`arcteam` · Testing · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: Fixture-based pre-recorded LLM responses per test scenario. Mock LLM matches prompts to fixtures. Small set of integration tests hit real LLM (marked slow, CI-optional).
- **Priority**: Simplicity — deterministic, fast, reproducible, zero LLM cost.
- **Tiers**: All tiers same.

#### D-118 — Test Strategy

`arcteam` · Testing · from *ArcTeam Memory — Build Decisions (2026-02-21)*

- **Decision**: 70% unit / 20% integration / 10% e2e. Security tests in dedicated directory. Matches CLAUDE.md quality gates.
- **Priority**: Compliance — matches established project standards (>=80% line, >=75% branch, >=90% core).
- **Tiers**: All tiers same.

### arcui

#### D-061 — Test strategy

`arcui` · Testing · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: pytest (unit/integration) + Playwright (E2E)
- **Options Considered**: Python + Playwright / Python only / Python + Jest
- **Rationale**: Both Python tools. Playwright already in arcagent deps. Full stack confidence.

#### D-581 — Test strategy

`arcui` · Testing · was `T-1` · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Unit (70%) + integration with InMemoryTransport (20%) + E2E WebSocket (10%)
- **Priority**: simplicity

---

## 12. Deployment

### arcllm

#### D-251 — Dependencies

`arcllm` · Deployment · from *Azure OpenAI Provider (SPEC-010)*

- **Choice**: Zero new dependencies. Uses existing httpx, pydantic, arcllm internals
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

### arcrun

#### D-592 — Build System

`arcrun` · Deployment · was `DECISION-007` · from *arcrun Decision Log (2026-02-11 – 2026-02-14)*

- **Decision**: Hatchling
- **Context**: Need a build backend for `pyproject.toml`. arcrun is a library with one dependency (arcllm).
- **Options**:
- Hatchling — Modern, minimal config. Official PyPA build backend. Zero extra files.
- Setuptools — Most familiar but more boilerplate. Feels legacy for 2026.
- Poetry — Rich dependency management but heavier. Brings its own lock file. Overkill for a single-dependency library.
- **Reasoning**: Lightest config, modern standard, no extra files needed. ~25 lines in pyproject.toml covers everything. Matches the "simple and clear" design priority.
- **Status**: Accepted
- **Date**: 2026-02-11

### arcagent

#### D-173 — Module activation?

`arcagent` · Deployment · from *Feature: Telegram Messaging Module*

- **Choice**: **Auto-start in serve mode**
- **Rationale**: Module detects serve mode at startup. If telegram.enabled=true and agent is serving, starts polling. Dormant in run/chat modes.
- **Category**: Deployment

#### D-276 — Dependencies

`arcagent` · Deployment · from *Slack Messaging Module (SPEC-011)*

- **Choice**: `slack-bolt >= 1.20.0` + `aiohttp` as optional: `pip install 'arcagent[slack]'`
- **Priority**: Simplicity
- **Tier Notes**: auto-applied: pattern-following

#### D-360 — Migration approach — big bang, single spec

`arcagent` · Deployment · from *Unified Capability System — Build Decisions (2026-04-28)*

- **Decision**: New `CapabilityLoader` replaces all four old paths in one PR/spec. Every existing module (memory, browser, scheduler, voice, telegram, slack, etc., ~15 modules) rewritten to the new decorator form in the same edit. `ExtensionLoader`, `_load_modules_by_convention`, `register_native_tools`, `MODULE.yaml` runtime parsing, `[tools.native]` config block all deleted in the same edit.
- **Priority**: simplicity (one mental model after merge, no parallel paths) > codebase mandate (CLAUDE.md "no legacy/backward-compat" — local-only repo, no users to break)
- **Alternatives**: phased migration with parallel paths (rejected — directly contradicts CLAUDE.md, parallel paths tend to become permanent); brand-new only with old modules untouched (rejected — worst-of-both, permanent dual-path).
- **Spec impact**: produces one large spec with ~15 module-migration tasks plus the loader/registry rewrite.

#### D-368 — `arc module install` source scope (v1)

`arcagent` · Deployment · from *Unified Capability System — Build Decisions (2026-04-28)*


Local file (`.tgz`, `.zip`, directory) only. Git URL and marketplace registry deferred to a future spec. Sigstore verification on install at federal tier per D-352.

#### D-397 — Doc correction

`arcagent` · Deployment · from *Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)*

- **Choice**: Fix CLAUDE.md structure diagram: `core/context_manager.py` → `core/session_internal/context.py` (class `ContextManager`; logger still `arcagent.context_manager`).
- **Priority**: simplicity
- **Rationale / Tier Notes**: File named in the standards doc does not exist; stale pointer.

#### D-535 — Default enablement

`arcagent` · Deployment · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: arc agent build scaffold declares [modules.workflows] enabled=true for the tool module; the arcteam runner ships inside the existing arc service process — no new deployment unit.
- **Priority**: simplicity
- **Alternatives**: scaffold declares disabled; no scaffold declaration
- **Rationale**: SPEC-056 lesson: tasks shipped without scaffold declaration and sat dead fleet-wide; removal stays one config line.

#### D-545 — Rollout default

`arcagent` · Deployment · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Mandated Answer**: The MCP module ships OFF by default. Nothing changes for the five DGX agents until each one opts in via [modules.mcp]. Rollout follows the existing deploy runbook; connectors install per agent after deploy because secrets never ride in git.
- **Citation**: Arc CLAUDE.md §Composability — unbreakable defaults; reference_dgx_deploy_runbook
- **Category**: Deployment

#### D-584 — Migration

`arcagent` · Deployment · was `DP-001` · from *Bio-Memory (ArcAgent)*

- **Decision**: Clean start. No migration. Bio-memory creates fresh state at `memory/`. Existing markdown-memory data untouched.
- **Rationale**: Priority: simplicity. Agent learns organically.
- **Category**: Deployment

#### D-651 — One verb signs and pins; `arc trust approve` produces a real signature

`arcagent` · Security · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `arc trust approve <name>` writes a detached Ed25519 signature over
  the capability's `.py` or the skill's `SKILL.md` using the operator key, pins that
  key as the trusted key for the agent, and records the TOFU pin — all three, in one
  command. The current hash-only pin is replaced, not kept alongside. `arc trust
  disapprove` is its exact inverse: revoke the signature, drop the pin. Signing is an
  operator action; no agent tool, chat message, or module can invoke it.
- **Alternatives**: Add a separate `arc trust sign` beside the existing `approve`
  (each verb does one thing, but an operator then has to know that the signature floor
  and the TOFU layer are different mechanisms evaluated in that order, and approving
  without signing silently does nothing above personal tier — which is today's
  behaviour and today's bug); make `approve` sign only above personal tier (fewer
  files written on a laptop, but the federal path stops being the path exercised
  locally).
- **Rationale**: `capability_loader.py:_passes_trust_gate` runs the signature check
  *before* it consults TOFU, and above personal a missing signature denies outright.
  `arc trust approve` today pins a hash only, and prints a note conceding the pin
  changes nothing at personal tier. The combination means an agent-improved skill can
  never load above personal tier and no command in the product can make it load —
  while `README.md` already advertises signed agent-authored capabilities. This is the
  missing half, and merging the verbs means an operator never has to reason about two
  gates to answer one question: do I trust this file.

#### D-649 — Copied capabilities are untrusted by design; federal drift needs an operator signature

`arcagent` · Security · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: Tools and skills copied into `<agent_dir>/capabilities/` land in the
  existing `agent` scan root and keep its untrusted classification — AST validator,
  restricted builtins, Sign/TOFU gate — exactly as an agent-authored capability does.
  They arrive Arc-signed, so they load clean on first install at every tier. Once an
  agent edits one, the signature no longer matches: at personal tier TOFU adjudicates
  it, and at enterprise and federal it does not load until an operator reviews and
  re-signs it with `arc trust approve` (D-651), which does not exist in signing form
  today.
- **Alternatives**: Give copied capabilities a trusted `module-copy:` root so they
  load unchallenged (no re-signing friction, and first-party code keeps the trust it
  shipped with, but a writable directory whose contents are trusted is exactly the
  plant-a-`.py`-via-bash hole `_UNTRUSTED_ROOTS` was drawn to close); re-verify the
  original signature and refuse any drifted file at every tier (unambiguous, but it
  forbids the skill improvement the copy exists to enable).
- **Rationale**: The untrusted classification is not a cost of copying here, it is the
  correct reading of what the directory now is: a place whose contents are *meant* to
  change. `capability_loader.py:_UNTRUSTED_ROOTS` already treats every agent-writable
  root this way, so this adds no mechanism. The tier split falls out of the existing
  signature floor rather than a new rule, and it lands where it should: an agent may
  improve its own skills on a laptop, while a federal box improves nothing without a
  human signing for it.

#### D-653 — The signing procedure ships as an operator runbook, README claim, and code together

`arcagent` · Deployment · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: Signing a new tool or skill is documented in three places, and all
  three land in the same change as the code: `docs/runbooks/signing-capabilities.md`
  (the operator procedure, CLI and arcui, including what a drifted skill looks like
  and how to read a denial), `docs/walkthrough/10-security-model.md` (where the
  signature floor and the TOFU layer sit relative to each other), and `README.md`
  (the supply-chain row corrected to describe what the commands actually do).
- **Alternatives**: `--help` text and docstrings only (lives next to the code and
  cannot go stale, but an operator facing a denied capability at 2am needs a
  procedure, not a flag list); a runbook written after the code lands (normal
  sequencing, but this is the exact gap being fixed — the README has been claiming
  signed agent-authored capabilities while no command could produce a signature).
- **Rationale**: The defect being closed is not only a missing command; it is a
  documented capability with no implementation behind it. Shipping the procedure in
  the same change is what stops the claim and the code drifting apart a second time.
  `mkdocs --strict` already gates the links, so a runbook that references a command
  that does not exist fails the build.

#### D-638 — Federal bundles are built outside the enclave and carried in

`arcagent` · Deployment · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: `arc module bundle <names...> -o <file>.arcbundle` runs on the low
  side, resolves the named modules and their wheels, and emits one signed file.
  `arc module install --from <file>.arcbundle` runs inside the enclave against a
  standard `arc-agent` wheel. The air-gapped install is a pre-staged file, never a
  network fetch, and the same two commands work online where `--from` is omitted.
- **Alternatives**: Ship a private index mirror inside the enclave (familiar to ops,
  but that is a service to run, patch, and accredit); require the enclave to have
  the source repo and build in place (no artifact transfer, but puts a toolchain
  and the full module catalog inside the boundary).
- **Rationale**: Nobody installs from the internet in a SCIF, so "offline" was never
  a capability constraint — it was a timing constraint, and pre-staging resolves it.
  The bundle is the thing that gets scanned and approved for transfer, and its
  contents are a readable list of module names rather than an opaque binary diff.

### arcprompt

#### D-479 — Package-by-package rollout, no feature flag

`arcprompt` · Deployment · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: No separate rollout decision beyond D-469. Migrate package by package (arcrun, then arcagent, then arcmemory, then arcskill), each package's constants deleted only once its byte-identical tests pass. No feature flag: behavior is provably unchanged at each step, so there is nothing to toggle between.
- **Priority**: simplicity
- **Alternatives**: big-bang migration of all packages; feature-flagged dual-path loading
- **Rationale**: A flag implies two live code paths and the option of running the old one, which is exactly the backward-compat shim the repo standard forbids. Byte-identical proof per package makes each step a no-op in behavior, so rollback is plain git revert.

### arcui

#### D-035 — Migration from embedded

`arcui` · Deployment · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Clean break. Remove embedded code. `--ui` becomes "connect to UI".
- **Priority**: simplicity

#### D-062 — Packaging

`arcui` · Deployment · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Subpackage in Arc monorepo (`packages/arcui/`)
- **Options Considered**: Monorepo subpackage / Separate repo / Built into arcagent
- **Rationale**: Same pattern as siblings. Cross-package testing. Static files in wheel.

### capabilities

#### D-379 — Packaging for demo

`capabilities` · Deployment · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Skip — dev-mode install (files copied directly), no bundle, no signing, no `arc ext install`
- **Source**: Source doc §10, §11 (post-NLIT roadmap)

---

## 13. UI/UX

### arcagent

#### D-536 — Workflow as group chat

`arcagent` · UI/UX · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Every workflow has a group channel whose members are all participating agents and the involved people (owner, gate approvers, coordinator). Handoffs are narrated there; gates surface and are answerable there (the answer routes through the control-plane action with the person's identity — never an agent tool); humans steer agents in-channel via mention-gated activation.
- **Priority**: modularity
- **Alternatives**: narration-only feed; no channel binding
- **Rationale**: Josh: workflows are group chats with all agents and people involved. This is app-store ARC-7 (gates as conversation) + ARC-11 pulled into v1; D-025 gate invariant preserved.

#### D-537 — Agents as chat citizens + graph rendering

`arcagent` · UI/UX · from *ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)*

- **Decision**: Agents are first-class chat participants in arcui alongside people (group and DM). The workflow definition and live-run views render with React Flow (dagre layout), matching the control-plane design language.
- **Priority**: modularity
- **Alternatives**: custom SVG renderer; text/list first
- **Rationale**: The graph is the product story; React Flow ships editor + live view fastest. Agents-in-chat makes the group-chat model real in the UI.

#### D-561 — Management surface

`arcagent` · UI/UX · from *Connector Extensions — Build Decisions (2026-08-04)*

- **Decision**: Full connector management is available in arcui: install, authorize, set allowlists, view health, and approve outbound calls. CONSTRAINT: CLI and TUI are the complete and primary surface — arcui is a convenience layer over the same commands and is required for nothing. Secret entry uses a dedicated form field that posts straight to the secret store and never enters the model's context; arcui must sit behind authentication before this ships.
- **Priority**: simplicity
- **Alternatives**: Read-only status in arcui with approvals at the terminal only (smallest surface, but an approval waiting while away from the terminal just sits there); Connections page plus approvals routed to the existing approval surface (one new read-only page and no new plumbing); No UI at all for now (least work, but no single place showing what is connected across five agents)
- **Rationale**: Josh wants a non-technical buyer to never need a terminal, while every technical operator keeps a full terminal path. The Telegram prohibition was about a token entering the LLM's context via chat, not about a browser form; a dedicated field that bypasses the model preserves that rule.

#### D-569 — CLI-first — a vetted CLI is the default attachment, MCP is an option

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Where a service has an acceptable command-line tool, that is the default way to attach it. MCP remains fully supported as an option for services with no decent CLI. The PROCESS transport is finished FIRST and MCP second; the hook contract (COMP-004) is unchanged because it names no transport, so this is a priority change rather than a redesign.
- **Priority**: simplicity
- **Alternatives**: MCP-first as originally specced (ADR-030 is written around it, but that ordering was chosen to prove the spine rather than because MCP was the better artifact); both transports built together in one phase (proves transport-agnosticism immediately, more work at once)
- **Rationale**: Josh's ruling. A single hashed binary is easier to pin and verify than a package with a transitive dependency tree, and it removes a Node or Python runtime from the trust surface. Good CLIs also ship real safety controls of their own. The hook was designed transport-agnostic, so nothing in COMP-004, COMP-003, COMP-001 or COMP-012 changes.

#### D-570 — A CLI extension is three parts, not two

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: An extension wrapping a CLI ships (1) an INSTALL DIRECTIVE naming what the host must install, which the operator runs and Arc never runs silently; (2) a MANIFEST declaring each CLI command as a named tool with its read-only/state-modifying classification and capability tags; and (3) a SKILL carrying the know-how of when to use which command. The skill teaches judgment; the manifest grants authority.
- **Priority**: security
- **Alternatives**: install script plus skill only, as Hermes and OpenClaw do it (fewer moving parts and matches the reference implementations, but the agent then invokes the CLI through the generic bash tool)
- **Rationale**: Josh proposed install-script-plus-skill and asked to be checked. The shape is right but skill-only would route every CLI call through `bash`, so the policy pipeline would see `bash` rather than `readwise.export`, audit would record a shell string rather than a structured call, the trifecta gate could not distinguish a read from a send, and the tool allowlist would have nothing to bound. That is the generic-dispatcher failure already rejected in D-548, and worse, because `bash` reaches the whole machine. Hermes and OpenClaw can ship skill-only because neither has per-tool-call authorization; that authorization is Arc's differentiator. Declaring commands as named tools is what makes `gh.pr_view` grantable without `gh.pr_merge`.

#### D-571 — Google switches to gogcli; Readwise Reader and GitHub CLI added

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Google Workspace attaches via `openclaw/gogcli` rather than `taylorwilsdon/google_workspace_mcp`, reversing the earlier choice now that CLI-first is the default. Two connectors are added to the set: Readwise Reader (`npm install -g @readwise/cli`, then `readwise login`) and GitHub (`gh`).
- **Priority**: security
- **Alternatives**: keep taylorwilsdon/google_workspace_mcp (actively maintained, PyPI with SHA256, per-service --tools scoping — chosen originally so the flagship connector would prove the MCP spine, an argument that inverts under CLI-first)
- **Rationale**: gogcli was the stronger artifact on every axis except proving MCP: one MIT Go binary distributed with release artifacts, native multi-account routing via `--account`, tokens in the platform keyring with an encrypted file backend for headless Linux, and safety controls no MCP server offers — `--readonly`, `--gmail-no-send`, `--enable-commands-exact`, and `--wrap-untrusted` for untrusted-content wrapping. Under D-569 the one argument against it no longer applies. Readwise and GitHub are both CLI-native and reinforce the direction.

#### D-572 — arcllm owns PII policy; arcagent consumes the result

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: `human_gate` STOPS importing `arcllm._pii` and stops re-redacting. It displays the arguments exactly as it received them. `arcllm`'s `SecurityModule` already applies `pii_enabled` to both message text and TOOL-CALL ARGUMENTS on the inbound response (`_redact_response`, "redacts PII from response content and tool-call arguments"), so by the time the gate sees an argument the policy has already run.
- **Priority**: modularity
- **Alternatives**: make the gate surface-aware and tier-aware itself (new machinery, second policy to drift); construct both redacted and full request objects (safe by separation, two objects to keep in sync); keep construction-time redaction plus an unredacted side channel (smallest change, two paths to the same fact)
- **Rationale**: Josh's ruling — "human gate is in arcagent, it should show what is available, not control, or direct import arcllm; it just uses the response as arcllm has shown it." Reaching past `SecurityModule` into the private `_pii` module let arcagent override a decision arcllm owns, which is a concern-purity violation (CLAUDE.md §1). It also produced the defect: Arc was STRICTER WITH THE OPERATOR THAN WITH A THIRD-PARTY MODEL API — at personal tier the recipient was sent to the provider in the clear and then hidden from the human asked to approve it. The fix is a deletion, and it is automatically tier-correct.

#### D-573 — calls that never transited arcllm still get the tier's PII policy

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Scheduler firings, task dispatch, and workflow stages build tool calls inside Arc and never pass through `arcllm`, so no redaction has been applied to them. After D-572's deletion those must have the tier's policy applied where the call is BUILT, not in the gate.
- **Priority**: security
- **Alternatives**: leave the gate redacting as a catch-all (defeats D-572 and keeps the wrong layer in charge); accept the gap (a scheduled send would present an unredacted recipient at federal)
- **Rationale**: Today the gate catches these by accident. Removing it without this guard would open a real hole at exactly the tier that cares.

#### D-574 — federal reads; it does not send to a PII-identified target [refined by D-580]

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: ACCEPTED LIMITATION. With `pii_enabled` on, `arcllm` redacts outbound, so the model never sees the address and returns a tool call containing the literal placeholder. The send cannot execute and the approval is moot. Federal agents read, search and summarize, and send only to operator-configured aliases resolved locally. No reversible-token mechanism is built.
- **Priority**: security
- **Alternatives**: reversible indexed tokens restored before tool execution (the only option where federal sends work; what production systems do; costs a per-turn map and a restore path); exempt tool arguments from outbound redaction (smallest change, but sends the address to the provider anyway and defeats the control)
- **Rationale**: Josh's ruling. Verified in code: `redact_text` writes a TYPE-ONLY placeholder (`[PII:EMAIL]`), so two different addresses collapse to the same string, `PiiMatch.matched_text` is discarded, and no detokenize path exists anywhere in arcllm, arcrun or arcagent. Redaction is one-way and lossy. An agent that cannot email arbitrary strangers is a defensible federal posture rather than a regression. NOTE: this is an arcllm-level limitation latent since PII redaction shipped; connectors are simply the first feature that sends to people.

#### D-575 — build the credential broker

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: BUILD IT, before real credentials are configured. The connector never receives the credential: a broker holds it and attaches it on egress, so the connector only ever sees a localhost URL.
- **Priority**: security
- **Alternatives**: defer, since COMP-010 is already shaped as its seam
- **Rationale**: Josh: "yeah, lets build it, we need it." Far cheaper now than after connectors depend on the current shape, and the central supply-chain risk in this spec is third-party code holding live tokens.

#### D-576 — build all connectors; order does not matter

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: All identified connectors ship. No sequencing constraint.
- **Priority**: simplicity
- **Alternatives**: prove one end to end first (Jira needs only an API token; Google exercises named instances across two accounts)
- **Rationale**: Josh: "we need to do all of them, order doesn't matter."

#### D-577 — audit encrypted at rest now; access control and retention next spec

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: Encryption at rest lands with this work. Access control and retention policy for the audit store move to their own spec folder, with notes captured now.
- **Priority**: security
- **Alternatives**: design all three here (buries a consequence of the full-capture ruling inside connector work); defer all three (leaves a log reader seeing everything)
- **Rationale**: Josh's split. Full capture (D-552) made the audit store the highest-value target on the box; that is a consequence of the connector decision but not connector work.

#### D-578 — blueprints do not declare connectors

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: A blueprint never names or bundles a connector. Connections are added by the user AFTER the agent exists.
- **Priority**: simplicity
- **Alternatives**: blueprint names required connectors and install prompts for them; blueprint bundles them (breaks the config-only rule)
- **Rationale**: Josh: "blueprints don't declare connectors, this is after agents created, the user can add." Keeps the blueprint contract untouched and matches how an operator actually works — the agent exists first, then gets access.

#### D-579 — a manifest's tier floor refuses below, and cannot raise

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: A tier floor in an extension manifest refuses to load below that tier. It CANNOT raise the effective stringency of the deployment for that connection.
- **Priority**: security
- **Alternatives**: also allow raising (sounds safer, but hands a third-party bundle author control over operator policy)
- **Rationale**: Letting a bundle author raise stringency puts the wrong person in charge of policy. The operator sets the tier; a manifest may decline to run, never redefine the rules.

#### D-580 — egress is gated by tier and tool ORIGIN, refused at install

`arcagent` · UI/UX · from *Connector Extensions — Follow-Up — Build Decisions (2026-08-04)*

- **Decision**: One predicate governs whether a tool may send data out, using two signals that already exist — the `external_comms` trifecta leg (already computed by `tools/_egress.py`) and the tool's origin (`RegisteredTool.source` plus the capability root it loaded from).
- **Priority**: security
- **Alternatives**: approve per extension (fewer lines, but approving one send verb approves every sending verb it ships now or after an upgrade); approve per origin class (almost no config, but cannot express "this connector may send, that one may not"); enforce at call time (no install-time refusal, but produces exactly the silent mid-turn failures this decision exists to prevent)
- **Rationale**: Josh's ruling, and it replaces the accidental version recorded in D-574. That decision described federal as unable to send because PII redaction destroyed the address before the model saw it — a silent failure discovered by accident. This is the intentional rule: federal CAN send, through a tool the operator wrote and signed, which needs no model-supplied address because it resolves the recipient from config. The redaction problem never arises, because the generic-connector egress path does not exist at federal.

| Tier | May a tool egress? |
|---|---|
| Personal | Yes, any origin |
| Enterprise | Only if the tool is named in the operator's per-tool `egress_allow` list |
| Federal | Only if it is an operator-signed capability. NEVER an extension. |
At federal an extension may read freely and may not egress at all. Sending out at federal means writing a custom tool, signing it, and adding it to the agent's capabilities.
**Enforced at INSTALL and LOAD, never at call time.** A bundle declaring an egress tool the tier forbids is REFUSED at install with a message naming the tool and the tier. The agent never receives a tool it cannot use, so nothing fails silently mid-turn.
Concern purity is untouched: arcllm still only calls, arcrun still only loops, arcagent still manages, tools still arrive via modules and extensions. Nothing new owns anything, and the enterprise list composes with the existing `ToolConfig.allow`/`deny` filter rather than duplicating it.

### arcprompt

#### D-470 — Prompts tab with drawer editor

`arcprompt` · UI/UX · from *Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)*

- **Decision**: A Prompts tab in agent detail lists prompts grouped by package, each row chipped [stock] or [overridden]. Clicking opens a drawer with a stock / effective / diff toggle, edit, save, and reset-to-stock.
- **Priority**: simplicity
- **Alternatives**: a section inside the existing Files page; a fleet-wide prompts page plus a per-agent tab
- **Rationale**: Reuses the established tools-skills.tsx plus skill-drawer.tsx pattern, which already implements list → drawer → diff → rollback, so the feature inherits the existing design language instead of inventing one. The file tree cannot express the stock/overlay duality. A fleet-wide view serves the auditor persona but is v2-shaped; the v1 driver is tuning one agent.

### arcui

#### D-036 — Dashboard layout

`arcui` · UI/UX · from *Multi-Agent UI Architecture — Build Decisions (2026-03-03)*

- **Choice**: Agent sidebar + layer tabs (All/LLM/Run/Agent/Team). Stat cards + event stream.
- **Priority**: simplicity

#### D-063 — Telemetry default view

`arcui` · UI/UX · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Live stream + stats
- **Options Considered**: Live stream + stats / Historical analytics / Per-agent split
- **Rationale**: Shows the pulse immediately. Errors visible at a glance. Historical/per-agent are tabs.

#### D-064 — Telemetry tab structure

`arcui` · UI/UX · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Four pill-nav tabs: Overview, Traces, Replay, Cost
- **Options Considered**: Single page / Tabbed views / Separate pages
- **Rationale**: Matches demo. Overview = live stream + stats. Traces = filterable table + detail expand. Replay = session-level (ArcAgent concern, not day 1). Cost = per-agent + per-provider breakdowns.

#### D-071 — Model cost efficiency insights

`arcui` · UI/UX · from *ArcUI LLM Telemetry — Build Decisions (2026-03-01)*

- **Choice**: Cost optimization section in Cost tab
- **Options Considered**: No insights / Simple cheapest-model display / Full optimization analysis
- **Rationale**: Calculate per-model $/token efficiency from TraceStore records. Show: (1) most cost-efficient model (lowest $/token), (2) most-utilized model (highest request volume), (3) potential savings if migrating heavy-use models to cheapest viable alternative. ArcLLM already has split input/output pricing per provider config — use actual rates, not flat estimates. Alert when optimization opportunity exceeds 20% potential savings. Export-friendly for budget justification. Inspired by Mission Control's optimizer, but ours uses real split pricing instead of their flat-rate approximation.

### arctui

#### D-485 — v1 = the reliable working agentic loop through arcrun

`arctui` · UI/UX · from *arctui — Terminal Agent Interface — Build Decisions (2026-07-24)*

- **Decision**: v1's must-have is start-up → immediately use/code/talk agentically through arcrun, reliably, on a trusted folder — built on the existing transcript/activity/input panes plus tool-approval prompts (needed for safe agentic action). The richer view catalog (diff viewer, model/agent pickers, slash-command palette, @file autocomplete) is DEFERRED to post-v1.
- **Priority**: Reliable loops over features
- **Alternatives**: Lead with a polished view catalog (diff viewer, pickers, palette) before the loop is solid — rejected: contradicts the 'reliable loops over features' principle and the 'usable like Claude Code' bar.
- **Rationale**: Owner: 'start up, and start agentically using/coding/talking through arcrun.' The v1 success test is that the agentic loop actually works end-to-end in the terminal, not a breadth of views.

### capabilities

#### D-369 — PDF rendering library for ATO narrative + POA&M

`capabilities` · UI/UX · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: WeasyPrint (HTML/CSS → PDF)
- **Priority**: simplicity (visual) > modularity
- **Rationale**: Output must look like a real federal ATO document for the pitch to land. macOS + brew handles cairo/pango deps; backup laptop pre-installed for risk mitigation. ReportLab rejected for visual gap.
- **Tier Notes**: Same all tiers (rendering is offline).

#### D-371 — T-30 drift artifact production for Act 4

`capabilities` · UI/UX · from *NLIT SCAP Demo — Build Decisions (2026-05-04)*

- **Choice**: Programmatic drift generator script
- **Priority**: modularity > simplicity
- **Rationale**: Reproducible, auditable, lives in repo. Source doc §4 framing ("real data + small fake delta") preserved — script flips ~5–10 sshd-hardening rules pass→fail and adjusts timestamps deterministically. Hand-edit rejected for re-run reproducibility.
- **Tier Notes**: Same all tiers (build-time artifact, not runtime).

### cross-cutting

#### D-324 — TUI tech stack

`cross-cutting` · UI/UX · from *Hermes-Parity Roadmap — Build Decisions (2026-04-18)*

- **Decision**: Textual (Python-only). No Node/Ink dependency.
- **Priority**: simplicity (one runtime); modularity (no polyglot bridge); security (no Node toolchain in air-gapped/SCIF)
- **Alternatives**: Hermes Ink + Python RPC; prompt_toolkit only; web TUI via xterm.js
- **Rationale**: Arc is Python-only. Air-gapped deployments shouldn't need Node. Textual is mature enough.
- **Tiers**: Same. Federal: tier filter applied to slash command catalog (D-323).
- **Deepen notes**: [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md)

---

## 14. CLI

### arcagent

#### D-312 — CLI surface for new features

`arcagent` · CLI · from *Feature: arc-core-hardening*

- **Choice**: Full mirror -- all features get CLI commands for scriptability/CI/CD
- **Priority**: extensibility
- **Tier Variation**: None

#### D-585 — Commands

`arcagent` · CLI · was `U-001` · from *Bio-Memory (ArcAgent)*

- **Decision**: Full design doc CLI: status, identity, episodes, working, search, consolidate (light/deep/dry-run).
- **Rationale**: Priority: simplicity. Follows PRD.
- **Category**: CLI

#### D-639 — One install command; `--all` is the personal path, explicit names the federal path

`arcagent` · CLI · from *Optional Module Bundles — Build Decisions (2026-08-11)*

- **Decision**: One command set at every tier: `arc module list`, `arc module install
  <names...>`, `arc module install --all`, `arc module install --from <bundle>`,
  `arc module install --link <names...>` (development, D-641), `arc module remove
  <name>`, `arc module bundle <names...> -o <file>`. Personal uses `--all`; federal
  names modules explicitly and `--all` resolves to whatever the signed manifest
  permits rather than erroring. Install writes the config entry as well as the files,
  so install and enable are one step. `remove` is the exact inverse: delete the
  materialized tree, drop the config entry, and fail loudly with a non-zero exit if
  either half does not complete — a partially removed module is a defect, never a
  warning.
- **Alternatives**: Separate `install` and `enable` steps (mirrors the four states
  exactly, but makes the common case two commands and leaves installed-but-forgotten
  modules on disk); a federal-only `--manifest` flag (explicit about the gate, but
  forks the command by tier, which is the thing D-635 refuses).
- **Rationale**: The same command with the same flags everywhere is what keeps the
  federal path exercised locally. `--all` meaning "everything permitted here" rather
  than "everything that exists" makes the manifest the authority without needing the
  operator to know it is there.

---

# Appendix A — Decision ID Lookup

| ID | Category | Package | Decision |
|----|----------|---------|----------|
| D-001 | Security | arcui | Agent-to-UI encryption in transit |
| D-002 | Audit & Compliance | arcui | Connection lifecycle audit events |
| D-003 | Identity & Trust | arcui | Agent identity verification |
| D-004 | Architecture | arcui | Agent discovery/connection |
| D-005 | Architecture | arcui | Disconnect resilience |
| D-006 | Architecture | arcui | UI ↔ arcteam messaging |
| D-007 | Architecture | arcui | Event layer taxonomy |
| D-008 | Architecture | arcui | UIReporter ownership |
| D-009 | Architecture | arcui | UI process launch |
| D-010 | Architecture | arcui | Control plane transport |
| D-011 | Architecture | arcui | UI agent registry |
| D-012 | Architecture | arcui | Browser event filtering |
| D-013 | Data Model | arcui | UIEvent schema |
| D-014 | Data Model | arcui | Agent registration schema |
| D-015 | Data Model | arcui | Control message schema |
| D-016 | API Design | arcui | Rate limiting |
| D-017 | API Design | arcui | Agent WS path |
| D-018 | API Design | arcui | Browser REST for agents |
| D-019 | Identity & Trust | arcui | Auth model |
| D-020 | Observability | arcui | UI operations traced |
| D-021 | Observability | arcui | Multi-agent aggregation |
| D-022 | Audit & Compliance | arcui | Registration audited |
| D-023 | Audit & Compliance | arcui | Control commands audited |
| D-024 | Audit & Compliance | arcui | Auth failures audited |
| D-025 | Audit & Compliance | arcui | Subscription changes audited (federal) |
| D-026 | Audit & Compliance | arcui | Tamper-evident UI audit log |
| D-027 | Security | arcui | No secrets in events |
| D-028 | Security | arcui | Control requires operator role |
| D-029 | Security | arcui | WS idle timeout |
| D-030 | Identity & Trust | arcui | Agent token scope |
| D-031 | Integration | arcui | UIReporter ↔ arcllm |
| D-032 | Integration | arcui | UIReporter ↔ arcrun |
| D-033 | Performance | arcui | Max agent connections |
| D-034 | Extensibility | arcui | Transport abstraction |
| D-035 | Deployment | arcui | Migration from embedded |
| D-036 | UI/UX | arcui | Dashboard layout |
| D-037 | Architecture | arcui | TraceStore location |
| D-038 | Architecture | arcui | TraceStore backend design |
| D-039 | Architecture | arcui | Real-time event hook |
| D-040 | Architecture | arcui | ConfigController location |
| D-041 | Architecture | arcui | Attach API |
| D-042 | Architecture | arcui | WebSocket protocol |
| D-043 | Architecture | arcui | Multi-LLM handling |
| D-044 | Data Model | arcui | TraceRecord fields |
| D-045 | Data Model | arcui | JSONL rotation |
| D-046 | Data Model | arcui | Trace file location |
| D-047 | Data Model | arcui | Hash chain across rotations |
| D-048 | API Design | arcui | Endpoint structure |
| D-049 | API Design | arcui | Historical query API |
| D-050 | API Design | arcui | Day 1 authentication |
| D-051 | Observability | arcui | arcUI self-telemetry |
| D-052 | Audit & Compliance | arcui | Audit target for config mutations |
| D-053 | Security | arcui | Authorization model |
| D-054 | Security | arcui | Data redaction |
| D-055 | Integration | arcui | ArcAgent integration |
| D-056 | Integration | arcui | CLI mode |
| D-057 | Performance | arcui | Event stream backpressure |
| D-058 | Performance | arcui | Aggregation strategy |
| D-059 | Extensibility | arcui | Frontend framework |
| D-060 | Extensibility | arcui | Page extensibility |
| D-061 | Testing | arcui | Test strategy |
| D-062 | Deployment | arcui | Packaging |
| D-063 | UI/UX | arcui | Telemetry default view |
| D-064 | UI/UX | arcui | Telemetry tab structure |
| D-065 | Observability | arcui | Span timeline sub-phases |
| D-066 | Observability | arcui | Circuit breaker module |
| D-067 | Observability | arcui | Budget state visibility |
| D-068 | Observability | arcui | Per-agent cost breakdown |
| D-069 | Data Model | arcui | Raw body storage |
| D-070 | Data Model | arcui | Filter/export on trace table |
| D-071 | UI/UX | arcui | Model cost efficiency insights |
| D-072 | Audit & Compliance | arcagent | Audit prompt catalog rebuilds |
| D-073 | Architecture | arcagent | Tool catalog formatter location |
| D-074 | Architecture | arcagent | Prompt format |
| D-075 | Architecture | arcagent | RegisteredTool new fields |
| D-076 | Architecture | arcagent | Tool catalog cache strategy |
| D-077 | Architecture | arcagent | Team roster cache strategy |
| D-078 | Architecture | arcagent | Section ordering |
| D-079 | Architecture | arcagent | Built-in tools in catalog |
| D-080 | Architecture | arcagent | Section key for team context |
| D-081 | Data Model | arcagent | Entity fields in roster |
| D-082 | Data Model | arcagent | Specific new fields on Entity |
| D-083 | Data Model | arcagent | Renderer approach |
| D-084 | Security | arcagent | Metadata sanitization |
| D-085 | Extensibility | arcagent | Preamble configurability |
| D-086 | Extensibility | arcagent | Roster TTL configuration |
| D-087 | Integration | arcagent | Overlap with API tool schemas |
| D-088 | Audit & Compliance | arcteam | Audit trail on team memory operations |
| D-089 | Identity & Trust | arcteam | Audit log integrity |
| D-090 | Security | arcteam | Data classification labeling on stored memory |
| D-091 | Security | arcteam | Encryption at rest for memory store |
| D-092 | Security | arcteam | Memory content in transit between agents |
| D-093 | Architecture | arcteam | Team Memory Core Concept |
| D-094 | Architecture | arcteam | Storage Location |
| D-095 | Architecture | arcteam | Storage Format |
| D-096 | Architecture | arcteam | Service Shape |
| D-097 | Architecture | arcteam | Consolidation Trigger |
| D-098 | Architecture | arcteam | LLM Integration |
| D-099 | Architecture | arcteam | Promotion Gate Location |
| D-100 | Architecture | arcteam | Wiki-Link Resolution |
| D-101 | Data Model | arcteam | Search Strategy |
| D-102 | Data Model | arcteam | Hop Relevance Scoring |
| D-103 | Data Model | arcteam | Search Response Shape |
| D-104 | Data Model | arcteam | Index Schema |
| D-105 | Data Model | arcteam | Entity Type to Directory Mapping |
| D-106 | Data Model | arcteam | Playbooks & Decisions Storage |
| D-107 | Observability | arcteam | Event Flow |
| D-108 | Observability | arcteam | Telemetry Scope |
| D-109 | Security | arcteam | Classification Access Control |
| D-110 | Security | arcteam | Promotion Validation |
| D-111 | Security | arcteam | Approval Queue |
| D-112 | Integration | arcteam | Agent Wiring |
| D-113 | Integration | arcteam | Concurrency |
| D-114 | Performance | arcteam | Index Freshness |
| D-115 | Performance | arcteam | Budget Enforcement |
| D-116 | Extensibility | arcteam | Disabled Behavior |
| D-117 | Testing | arcteam | LLM Testing Approach |
| D-118 | Testing | arcteam | Test Strategy |
| D-119 | Architecture | arcagent | Where does the scheduler live? |
| D-120 | Architecture | arcagent | Which schedule types ship? |
| D-121 | Data Model | arcagent | Schedule storage format? |
| D-122 | Architecture | arcagent | What happens when a schedule fires? |
| D-123 | Architecture | arcagent | Schedule management tools? |
| D-124 | Architecture | arcagent | Active hours and limits? |
| D-125 | Architecture | arcagent | Overlapping executions? |
| D-126 | Architecture | arcagent | When does the scheduler run? |
| D-127 | Architecture | arcagent | Where are schedules defined? |
| D-128 | Audit & Compliance | arcagent | What audit trail? |
| D-129 | Architecture | arcagent | Cron expression parser? |
| D-130 | Testing | arcagent | Testing strategy? |
| D-131 | Architecture | arcrun | How does the model express decomposition intent? |
| D-132 | Architecture | arcrun | Where does the spawn tool live? |
| D-133 | Architecture | arcrun | Spawn tool API (model arguments) |
| D-134 | Data Model | arcrun | How do depth and budgets live on RunState? |
| D-135 | Security | arcrun | Shared memory between children? |
| D-136 | Security | arcrun | Cross-child communication? |
| D-137 | Performance | arcrun | Resource budget splitting |
| D-138 | Performance | arcrun | Parallel vs sequential spawning |
| D-139 | Integration | arcrun | NATS for distributed execution? |
| D-140 | Security | arcrun | Identity inheritance |
| D-141 | Architecture | arcrun | Child failure behavior |
| D-142 | Observability | arcrun | Event propagation |
| D-143 | Testing | arcrun | Testing strategy |
| D-144 | Architecture | arcrun | V1 scope |
| D-145 | Architecture | arcagent | CDP client library |
| D-146 | Architecture | arcagent | Module internal structure |
| D-147 | Architecture | arcagent | Tool API surface |
| D-148 | Architecture | arcagent | Element selection strategy |
| D-149 | Architecture | arcagent | Page state representation |
| D-150 | Integration | arcagent | CDP connection management |
| D-151 | Integration | arcagent | Tool registration |
| D-152 | Security | arcagent | URL access control |
| D-153 | Security | arcagent | JavaScript execution control |
| D-154 | Security | arcagent | Credential/cookie handling |
| D-155 | Performance | arcagent | Timeouts |
| D-156 | Performance | arcagent | Screenshot format |
| D-157 | Testing | arcagent | Testing strategy |
| D-158 | Architecture | arcagent | Config schema |
| D-159 | Architecture | arcagent | Module bus events |
| D-160 | Architecture | arcagent | Where does Telegram integration live? |
| D-161 | Architecture | arcagent | Inbound message transport? |
| D-162 | Architecture | arcagent | Module lifecycle? |
| D-163 | Architecture | arcagent | How does module access agent.chat()? |
| D-164 | Data Model | arcagent | Session management? |
| D-165 | Data Model | arcagent | Chat model? |
| D-166 | Integration | arcagent | Long message handling? |
| D-167 | Integration | arcagent | Response formatting? |
| D-168 | Integration | arcagent | Processing acknowledgment? |
| D-169 | Security | arcagent | Bot token storage? |
| D-170 | Security | arcagent | Unauthorized user handling? |
| D-171 | Performance | arcagent | Concurrent message handling? |
| D-172 | Testing | arcagent | Testing strategy? |
| D-173 | Deployment | arcagent | Module activation? |
| D-174 | Integration | arcrun | Container runtime for isolation sandbox |
| D-175 | Architecture | arcrun | What does the container sandbox wrap? |
| D-176 | Architecture | arcrun | How does container sandbox integrate? |
| D-177 | Security | arcrun | Default container constraints |
| D-178 | Security | arcrun | Event integrity mechanism |
| D-179 | Architecture | arcrun | Event verification API |
| D-180 | Testing | arcrun | Adversarial test scope |
| D-181 | Testing | arcrun | Adversarial test location |
| D-182 | Performance | arcrun | Concurrent spawn testing approach |
| D-183 | Architecture | arcrun | NIST 800-53 documentation format |
| D-184 | Security | arcrun | NIST control mapping scope |
| D-185 | Architecture | arcrun | Docker SDK dependency strategy |
| D-186 | Architecture | arcrun | LOC budget revision |
| D-187 | Security | arcrun | Container image management |
| D-188 | Architecture | arcllm | Budget-Telemetry Integration Pattern |
| D-189 | Architecture | arcllm | Budget Scope Isolation |
| D-190 | Architecture | arcllm | Routing Module Stack Position |
| D-191 | Architecture | arcllm | Router Adapter Lifecycle |
| D-192 | Architecture | arcllm | Budget-Routing Interaction |
| D-193 | Data Model | arcllm | Budget Storage Backend |
| D-194 | Data Model | arcllm | Budget Period & Reset |
| D-195 | Data Model | arcllm | Per-Call Max Estimation |
| D-196 | API Design | arcllm | Budget API Surface in load_model() |
| D-197 | API Design | arcllm | Routing Classification Source |
| D-198 | API Design | arcllm | Routing Rules Structure |
| D-199 | Security | arcllm | Enforcement Behavior |
| D-200 | Security | arcllm | Unknown Classification Behavior |
| D-201 | Testing | arcllm | Testing Strategy |
| D-202 | Audit & Compliance | arcagent | All memory writes emit audit events |
| D-203 | Identity & Trust | arcagent | Audit logs tamper-evident (append-only JSONL + OTel) |
| D-204 | Security | arcagent | Memory content encrypted at rest |
| D-205 | Security | arcagent | Memory content validated on read (integrity check) |
| D-206 | Security | arcagent | PII/CUI filtered before storage |
| D-207 | Security | arcagent | Per-agent memory isolation |
| D-208 | Security | arcagent | Entity file classification tracking in frontmatter |
| D-209 | Architecture | arcagent | Scope of this build |
| D-210 | Architecture | arcagent | Module mutual exclusivity |
| D-211 | Architecture | arcagent | Codebase location |
| D-212 | Architecture | arcagent | Agent-team relationship |
| D-213 | Architecture | arcagent | Internal structure |
| D-214 | Architecture | arcagent | Disk layout |
| D-215 | Architecture | arcagent | LLM access |
| D-216 | Architecture | arcagent | Module Bus events |
| D-217 | Data Model | arcagent | working.md format |
| D-218 | Data Model | arcagent | Episode file format |
| D-219 | Data Model | arcagent | how-i-work.md format |
| D-220 | Data Model | arcagent | Episode naming |
| D-221 | Observability | arcagent | Telemetry |
| D-222 | Security | arcagent | Memory poisoning defense |
| D-223 | Security | arcagent | File access protection |
| D-224 | Integration | arcagent | Retrieval trigger |
| D-225 | Integration | arcagent | Light consolidation |
| D-226 | Integration | arcagent | Deep consolidation |
| D-227 | Performance | arcagent | Token budget enforcement |
| D-228 | Performance | arcagent | Working.md I/O |
| D-229 | Performance | arcagent | Retrieval search |
| D-230 | Extensibility | arcagent | Config structure |
| D-231 | Extensibility | arcagent | Team discovery |
| D-232 | Architecture | arcllm | Inheritance strategy |
| D-233 | Architecture | arcllm | Class name |
| D-234 | Architecture | arcllm | Provider name |
| D-235 | Data Model | arcllm | TOML config structure |
| D-236 | Data Model | arcllm | Model/deployment name mapping |
| D-237 | API Design | arcllm | URL construction |
| D-238 | API Design | arcllm | Auth header |
| D-239 | API Design | arcllm | URL normalization |
| D-240 | API Design | arcllm | Content filter handling |
| D-241 | Observability | arcllm | Telemetry |
| D-242 | Audit & Compliance | arcllm | Audit trail |
| D-243 | Security | arcllm | Token handling |
| D-244 | Security | arcllm | HTTPS enforcement |
| D-245 | Security | arcllm | Startup validation |
| D-246 | Integration | arcllm | Error mapping |
| D-247 | Performance | arcllm | Connection pooling |
| D-248 | Extensibility | arcllm | Future auth extensibility |
| D-249 | Testing | arcllm | Test strategy |
| D-250 | Testing | arcllm | URL regression test |
| D-251 | Deployment | arcllm | Dependencies |
| D-252 | Architecture | arcagent | Module structure |
| D-253 | Architecture | arcagent | Socket Mode lifecycle |
| D-254 | Architecture | arcagent | Event handler registration |
| D-255 | Architecture | arcagent | Message processing |
| D-256 | Data Model | arcagent | Session storage |
| D-257 | Data Model | arcagent | Config fields |
| D-258 | API Design | arcagent | Response delivery |
| D-259 | API Design | arcagent | Processing indicators |
| D-260 | API Design | arcagent | Proactive DMs |
| D-261 | API Design | arcagent | Commands |
| D-262 | API Design | arcagent | Message splitting |
| D-263 | Observability | arcagent | Telemetry events |
| D-264 | Audit & Compliance | arcagent | Audit trail |
| D-265 | Security | arcagent | Token handling |
| D-266 | Security | arcagent | Token prefix validation |
| D-267 | Security | arcagent | Authorization |
| D-268 | Security | arcagent | Bot loop prevention |
| D-269 | Integration | arcagent | Import strategy |
| D-270 | Integration | arcagent | DM channel caching |
| D-271 | Performance | arcagent | Connection model |
| D-272 | Extensibility | arcagent | notify tool |
| D-273 | Extensibility | arcagent | Tool name |
| D-274 | Testing | arcagent | Mock strategy |
| D-275 | Testing | arcagent | split_message |
| D-276 | Deployment | arcagent | Dependencies |
| D-277 | Audit & Compliance | arcllm | Audit trail on queue operations |
| D-278 | Identity & Trust | arcllm | Tamper-evident queue event log |
| D-279 | Observability | arcllm | Queue metrics export |
| D-280 | Architecture | arcllm | Stack Position |
| D-281 | Architecture | arcllm | Queue Scope |
| D-282 | Architecture | arcllm | Concurrency Primitive |
| D-283 | Architecture | arcllm | Backpressure |
| D-284 | Architecture | arcllm | Timeout Semantics |
| D-285 | Architecture | arcllm | Configuration |
| D-286 | Architecture | arcllm | Exception Hierarchy |
| D-287 | Observability | arcllm | Queue Telemetry |
| D-288 | Performance | arcllm | Default Concurrency |
| D-289 | Performance | arcllm | Default Call Timeout |
| D-290 | Testing | arcllm | Test Strategy |
| D-291 | Architecture | arcagent | Parallel tool execution |
| D-292 | Architecture | arcagent | Self-modification level |
| D-293 | Architecture | arcagent | Hot-reload mechanism |
| D-294 | Architecture | arcagent | Tool policy pipeline |
| D-295 | Architecture | arcagent | Heartbeat / proactive execution |
| D-296 | Architecture | arcagent | Session history model |
| D-297 | Architecture | arcagent | Loop termination signal |
| D-298 | Architecture | arcagent | Turn/step limits |
| D-299 | Architecture | arcagent | ArcLLM bridge wiring |
| D-300 | Architecture | arcagent | httpx client lifecycle |
| D-301 | Data Model | arcagent | Tool policy types location |
| D-302 | Data Model | arcagent | Proactive schedule definition |
| D-303 | Data Model | arcagent | Agent-created tool format |
| D-304 | Data Model | arcagent | Agent-created extension format |
| D-305 | API Design | arcagent | Self-modification tool surface |
| D-306 | API Design | arcagent | Schedule management tools |
| D-307 | API Design | arcagent | `task_complete` schema |
| D-308 | Observability | arcagent | Telemetry for new components |
| D-309 | Security | arcagent | Dynamic tool sandboxing |
| D-310 | Security | arcagent | Config architecture |
| D-311 | Testing | arcagent | Test coverage target |
| D-312 | CLI | arcagent | CLI surface for new features |
| D-313 | Security | cross-cutting | Inter-package transport encryption |
| D-314 | Audit & Compliance | cross-cutting | Audit scope (gateway msgs, cron runs, skill installs) |
| D-315 | Audit & Compliance | cross-cutting | Audit log retention |
| D-316 | Security | cross-cutting | Platform credential storage at federal |
| D-317 | Identity & Trust | cross-cutting | Audit log integrity |
| D-318 | Identity & Trust | cross-cutting | New package extras release artifacts |
| D-319 | Security | cross-cutting | Session/state files at rest (federal) |
| D-320 | Security | cross-cutting | Voice transcript handling |
| D-321 | Architecture | cross-cutting | Package boundaries for new Hermes-parity capabilities (FINAL) |
| D-322 | Extensibility | cross-cutting | Skills Hub gating |
| D-323 | Extensibility | cross-cutting | Centralized slash command registry |
| D-324 | UI/UX | cross-cutting | TUI tech stack |
| D-325 | Architecture | cross-cutting | arcgateway process model |
| D-326 | Architecture | cross-cutting | Agent dispatch model inside arcgateway |
| D-327 | Architecture | cross-cutting | Session storage engine |
| D-328 | Architecture | cross-cutting | Session API ownership |
| D-329 | Architecture | cross-cutting | Session identity model (revised) |
| D-330 | Extensibility | cross-cutting | Cross-session context reads (per-session ACL) |
| D-331 | Architecture | cross-cutting | Multi-message concurrency |
| D-332 | Extensibility | cross-cutting | Pluggable terminal backends in arcrun |
| D-333 | Extensibility | cross-cutting | Platform credential storage |
| D-334 | Data Model | cross-cutting | Memory architecture under per-(user, agent) sessions |
| D-335 | Integration | cross-cutting | Natural-language cron parser |
| D-336 | Integration | cross-cutting | Subagent delegation primitive |
| D-337 | Extensibility | cross-cutting | Skill auto-creation nudge location |
| D-338 | Architecture | arcagent | Modules are optional shipped bundles, not a runtime concept |
| D-339 | Architecture | arcagent | Tools may declare an optional `requires_skill` field |
| D-340 | Security | arcagent | Workspace boundary stays — agent writes only inside workspace |
| D-341 | Architecture | arcagent | Last-wins on name collisions with audit |
| D-342 | Architecture | arcagent | Skills are folders, one tier — no quick-vs-full distinction |
| D-343 | Architecture | arcagent | `triggers` is a semantic hint, not a runtime matcher |
| D-344 | Data Model | arcagent | Title-case section headings standardized |
| D-345 | API Design | arcagent | `update_*` is a separate skill+tool from `create_*` |
| D-346 | Architecture | arcagent | Skills/tools manifest in system prompt at session start; bodies lazy via `read` |
| D-347 | Architecture | arcagent | Skill-usage instruction injected via bus, not hardcoded in prompt builder |
| D-348 | Security | arcagent | Policy is authoritative; skill `tools` field is descriptive |
| D-349 | Architecture | arcagent | Two file types only — `.py` (decorated) and `.md` (skill folder) |
| D-350 | Audit & Compliance | arcagent | Capability lifecycle audit |
| D-351 | Audit & Compliance | arcagent | Audit log integrity |
| D-352 | Identity & Trust | arcagent | Marketplace install signing |
| D-353 | Identity & Trust | arcagent | AST validator on agent-authored Python |
| D-354 | Identity & Trust | arcagent | Federal tier blocks agent-authored `.py` reload |
| D-355 | Audit & Compliance | arcagent | Capability source in audit content |
| D-356 | Architecture | arcagent | Lifecycle abstraction — three decorators + one class form |
| D-357 | Data Model | arcagent | Versioning format — semver, LLM picks bump |
| D-358 | API Design | arcagent | `reload()` returns a plain string diff |
| D-359 | Security | arcagent | Validation script trust model — tier-specific TOFU |
| D-360 | Deployment | arcagent | Migration approach — big bang, single spec |
| D-361 | Extensibility | arcagent | Conflict UX |
| D-362 | Extensibility | arcagent | Hot reload trigger |
| D-363 | Security | arcagent | Denied capabilities not in manifest |
| D-364 | Extensibility | arcagent | Malformed capability — skip + audit, agent keeps starting |
| D-365 | Extensibility | arcagent | Skill-usage instruction text location |
| D-366 | Architecture | arcagent | state.json is not framework-enshrined |
| D-367 | Extensibility | arcagent | Bus event names for capability lifecycle |
| D-368 | Deployment | arcagent | `arc module install` source scope (v1) |
| D-369 | UI/UX | capabilities | PDF rendering library for ATO narrative + POA&M |
| D-370 | Data Model | capabilities | Hostname/IP/MAC sanitization mapping persistence |
| D-371 | UI/UX | capabilities | T-30 drift artifact production for Act 4 |
| D-372 | Architecture | capabilities | Extension architecture & install location |
| D-373 | API Design | capabilities | Tool declaration |
| D-374 | API Design | capabilities | Tool return shape |
| D-375 | API Design | capabilities | Tool classification |
| D-376 | Audit & Compliance | capabilities | Audit emission per tool call |
| D-377 | Security | capabilities | Sanitization determinism |
| D-378 | Architecture | capabilities | In-memory ingest cache lifetime |
| D-379 | Deployment | capabilities | Packaging for demo |
| D-380 | Data Model | capabilities | ATT&CK → 800-53 mapping source |
| D-381 | Data Model | capabilities | POA&M output format |
| D-382 | Data Model | capabilities | 800-53 catalog source |
| D-383 | Data Model | capabilities | FedRAMP baseline membership source |
| D-384 | API Design | arcllm | Canonical `tool_choice` type ownership |
| D-385 | API Design | arcrun | `tool_choice` input validation at the loop boundary |
| D-386 | Audit & Compliance | arcrun | Audit the turn-0 force-pin |
| D-387 | Performance | arcllm | Anthropic cache_control placement |
| D-388 | Performance | arcllm | Cache TTL default |
| D-389 | Performance | arcllm | OpenAI/Gemini cache telemetry read-back |
| D-390 | Performance | arcllm | prompt_cache_key / cachedContent |
| D-391 | Architecture | arcrun | Tool-set stability owner |
| D-392 | Performance | arcrun | Dynamic capability mid-run |
| D-393 | Architecture | arcllm | Cache breakpoint location |
| D-394 | Performance | arcrun | transform_context contract (arcrun side) |
| D-395 | Performance | arcllm | Ollama model residency |
| D-396 | Architecture | arcagent | Compaction model (arcagent) |
| D-397 | Deployment | arcagent | Doc correction |
| D-398 | Architecture | arcagent | Compaction method |
| D-399 | Data Model | arcagent | Summary structure |
| D-400 | Architecture | arcagent | Observation masking policy |
| D-401 | Architecture | arcagent | Large/durable content offload |
| D-402 | Architecture | arcagent | Emergency truncation |
| D-404 | Data Model | arcagent | Compaction summary must reassemble |
| D-405 | Architecture | arcagent | Compaction trigger = estimate, not reported tokens |
| D-406 | Security | arcagent | In-band summary sanitized |
| D-407 | Architecture | arcagent | Emergency valve keeps newest |
| D-408 | Architecture | arcagent | Notes trigger |
| D-409 | Architecture | arcagent | Capture cadence |
| D-410 | Architecture | arcagent | Enrichment |
| D-411 | Architecture | arcagent | Session consolidation |
| D-412 | Architecture | arcagent | Day rollup |
| D-413 | Architecture | arcagent | Deferred |
| D-414 | Security | arcagent | Sanitizer: one shared, hardened impl |
| D-415 | Architecture | arcagent | Drop `rollup_compacts_source` |
| D-416 | Data Model | arcagent | Long-term recall correctness |
| D-417 | Architecture | arcagent | Drain the full backlog |
| D-418 | Security | arcagent | Harden + audit |
| D-419 | Security | arcllm | InjectionModule ships OFF by default, opt-in per call |
| D-420 | Security | arcllm | Pattern-corpus tier is the zero-dep default; semantic tier is `arcllm[injection-semantic]` |
| D-421 | Security | arcllm | InjectionModule flags/blocks only — never interprets, rewrites, or executes content |
| D-422 | Security | arcllm | Scans INBOUND user + tool-result content pre-provider, pre-redaction |
| D-423 | Security | arcllm | Secret scanner folded in as a togglable `SECRETS` category → `[SECRET:TYPE]` |
| D-424 | Security | arcllm | Checksum validators (Luhn / mod-97 / ABA) gate entity matches |
| D-425 | Security | arcllm | Add gov/CUI entities: US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT,... |
| D-426 | Extensibility | arcllm | Entity categories individually toggleable via `pii_entities` allow/deny |
| D-427 | Extensibility | arcllm | Implement the allowlisted `pii_detector_class` loader spec-012 D-093/FR-13 specced but ... |
| D-428 | API Design | arcllm | GuardrailsModule validates the RESPONSE per call via a `guardrails={...}` kwarg |
| D-429 | Security | arcllm | Semantic guardrails (grounding, correctness, toxicity) OUT OF SCOPE |
| D-430 | Security | arcllm | Stack: Injection above Security (sees original text); Guardrails just inside Audit (val... |
| D-431 | Security | arcllm | New `ArcLLMInjectionError`, `ArcLLMGuardrailError` (subclass `ArcLLMError`) |
| D-432 | Extensibility | arcllm | Config: new `[modules.injection]`, `[modules.guardrails]`; extend `[modules.security]` ... |
| D-433 | Extensibility | arcllm | New optional extra `arcllm[injection-semantic]` |
| D-434 | API Design | arcllm | Both modules use `enforcement="block"\ |
| D-435 | Observability | arcllm | Flip default: `store_raw_bodies=True` — capture full request + response |
| D-436 | Observability | arcllm | Reuse existing `request_body`/`response_body` fields; no parallel `*_raw`, no linked re... |
| D-437 | Identity & Trust | arcllm | Hash chain covers raw bodies + encryption envelope, no new integrity machinery |
| D-438 | Security | arcllm | Federal tier: AES-256-GCM envelope, DEK wrapped by vault/KMS key; plaintext never on di... |
| D-439 | Security | arcllm | Add per-record `classification` tag (default from config floor) |
| D-440 | Audit & Compliance | arcllm | Retention: `max_age_days` + `max_bytes` drive rotation + whole-file purge |
| D-441 | Security | arcllm | Right-to-erasure DROPPED (no crypto-shred) |
| D-442 | Observability | arcllm | Replay READ path (`load_for_replay`) in arcllm; EXECUTION in arcrun |
| D-443 | Observability | arcllm | Lineage token persisted VERBATIM, never constructed by arcllm |
| D-444 | Audit & Compliance | arcllm | Turning raw capture off emits an audited `config_change` record |
| D-445 | Extensibility | arcllm | New optional extra `arcllm[trace-encryption]` (cryptography), helper in `_trace_crypto.py` |
| D-446 | Security | arcllm | Tier behavior: personal plaintext chmod-locked; enterprise same (encryption recommended... |
| D-447 | Security | arcllm | Encryption-key resolution reuses `vault.py` VaultResolver (allowlisted, TTL cache, KMS-... |
| D-448 | Security | arcllm | GCM AAD binds ciphertext to `trace_id` + `timestamp` |
| D-449 | Architecture | arcllm | Scope is intra-provider: spread across N endpoints/keys of the *same* provider |
| D-450 | Architecture | arcllm | Default strategy: weighted round-robin |
| D-451 | Architecture | arcllm | Health-aware strategy skips endpoints via a per-endpoint circuit mechanism owned by the... |
| D-452 | Architecture | arcllm | Sticky routing (session/agent key) optional, off by default |
| D-453 | Extensibility | arcllm | Pool topology `[[endpoints]]` in provider TOML; strategy in `[modules.load_balance]` |
| D-454 | Architecture | arcllm | Cursor + per-endpoint health in a shared per-pool registry guarded by `asyncio.Lock` |
| D-455 | Architecture | arcrun | Cross-AGENT scheduling / fairness / global prioritization OUT OF SCOPE |
| D-456 | API Design | arcllm | Per-call opt-in via `load_model(..., load_balance=True\ |
| D-457 | Security | arcllm | Each endpoint's key resolves through the same vault/env path as the base provider |
| D-458 | Architecture | arcllm | LB sits at the innermost stack position, holding a pool of endpoint adapters (Router-like) |
| D-459 | Architecture | arcprompt | Prompt load model |
| D-460 | Architecture | arcprompt | Overlay scope |
| D-461 | Architecture | arcprompt | Reload semantics |
| D-462 | Data Model | arcprompt | Frontmatter schema |
| D-463 | Architecture | arcprompt | Resolution failure mode |
| D-464 | API Design | arcprompt | Dedicated prompt endpoints |
| D-465 | Data Model | arcprompt | No seed-on-install |
| D-466 | Security | arcprompt | Overlays outside agent tool reach |
| D-467 | Security | arcprompt | Overlays are signed and verified before injection |
| D-468 | Security | arcprompt | Sign-on-write editing flow |
| D-469 | Testing | arcprompt | Byte-identical migration assertion |
| D-470 | UI/UX | arcprompt | Prompts tab with drawer editor |
| D-471 | Extensibility | arcprompt | Tier variance via policy content, not code |
| D-472 | Security | arcprompt | No secrets in prompt text |
| D-473 | Security | arcprompt | Input validation at trust boundary |
| D-474 | Identity & Trust | arcprompt | Loaded artifacts verified before use |
| D-475 | Observability | arcprompt | Prompt telemetry rides existing audit emission |
| D-476 | Audit & Compliance | arcprompt | Audit inherited from arctrust sinks |
| D-477 | Integration | arcprompt | Not applicable |
| D-478 | Performance | arcprompt | Prompt I/O bounded by run-start pinning |
| D-479 | Deployment | arcprompt | Package-by-package rollout, no feature flag |
| D-480 | Data Model | arcprompt | An override is permanent — upgrade divergence accepted |
| D-481 | Security | arcprompt | Signing authority resolved per-request, never hardcoded |
| D-482 | Architecture | arctui | arctui is a thin terminal interface over existing arc |
| D-483 | Architecture | arctui | Coding capability = a tuned arcagent, not TUI logic |
| D-484 | Architecture | arctui | Folder scoping via an explicit trust gate that edits policy |
| D-485 | UI/UX | arctui | v1 = the reliable working agentic loop through arcrun |
| D-486 | Extensibility | arctui | Extensions are arc units the TUI surfaces; one real extension proves the seam |
| D-487 | Security | arctui | No secrets in code or plaintext on disk |
| D-488 | Security | arctui | Input validation at trust boundaries |
| D-489 | Architecture | arcagent | Home vs working dir |
| D-490 | Security | arcagent | Opt-in + sandbox floor |
| D-491 | Security | arcagent | State-persistence invariant |
| D-492 | Security | arcmemory | Dataset, workspaces, and credentials never committed or written in plaintext |
| D-493 | Security | arcmemory | Benchmark content is untrusted input at the memory boundary |
| D-494 | Architecture | arcmemory | Home: an evaluations/ folder, not a package |
| D-495 | Architecture | arcmemory | Ingest granularity: session as one call, chunked on turn boundaries |
| D-496 | Architecture | arcmemory | Ingest path: a real ArcAgent turn per chunk |
| D-497 | Architecture | arcmemory | Source adapter seam: one read() contract, longmemeval first |
| D-498 | Data Model | arcmemory | Source timestamps are carried in the chunk text, not the event ts |
| D-499 | Integration | arcmemory | Consolidation fires per session, and the harness waits for it |
| D-500 | Testing | arcmemory | Run scale, isolation, and what is measured |
| D-501 | Audit & Compliance | arcagent | Audit trail |
| D-502 | Identity & Trust | arctrust | Artifact signing |
| D-503 | Identity & Trust | arctrust | Per-call authorization |
| D-504 | Security | arcagent | Input validation at trust boundaries |
| D-505 | Security | arcagent | Secret management |
| D-506 | Architecture | arcagent | Engine home |
| D-507 | Architecture | arcagent | Execution substrate |
| D-508 | Architecture | arcagent | Run progression |
| D-509 | Architecture | arcagent | Vocabulary and node kinds |
| D-510 | Architecture | arcagent | Multi-agent scope |
| D-511 | Architecture | arcagent | Handoff transport |
| D-512 | Architecture | arcagent | Authoring surfaces |
| D-513 | Data Model | arcagent | Canonical artifact and storage |
| D-514 | Data Model | arcagent | Node materialization |
| D-515 | Data Model | arcagent | Typed handoff |
| D-516 | Data Model | arcagent | Run aggregate |
| D-517 | Data Model | arcagent | Loops |
| D-518 | Data Model | arcagent | Routers and conditional edges |
| D-519 | API Design | arcagent | Tool and CLI surface |
| D-520 | API Design | arcagent | Versioning contract |
| D-521 | Observability | arcagent | Telemetry reuse |
| D-522 | Audit & Compliance | arcagent | Gate resolution identity |
| D-523 | Security | arcagent | Draft/sign split |
| D-524 | Security | arcagent | Trifecta accumulation scope |
| D-525 | Security | arcagent | Activation approval |
| D-526 | Security | arcagent | Gate reject semantics |
| D-527 | Security | arcagent | Side-effect idempotency |
| D-528 | Integration | arcagent | Trigger seam |
| D-529 | Integration | arcagent | MCP scope |
| D-530 | Performance | arcagent | Per-agent node concurrency |
| D-531 | Performance | arcagent | Run budget |
| D-532 | Extensibility | arcagent | Node strategy field |
| D-533 | Extensibility | arcagent | First companion arcrun strategy |
| D-534 | Testing | arcagent | Test strategy |
| D-535 | Deployment | arcagent | Default enablement |
| D-536 | UI/UX | arcagent | Workflow as group chat |
| D-537 | UI/UX | arcagent | Agents as chat citizens + graph rendering |
| D-538 | Architecture | arcagent | Handoff is a task write, never a message |
| D-539 | Architecture | arcagent | A run is an office, not a data bus — the shared run workspace |
| D-540 | Observability | arcagent | MCP call telemetry |
| D-541 | Data Model | arcstore | Connector runtime state store |
| D-542 | Security | arcagent | Spawn environment safety filter |
| D-543 | Integration | arcagent | External dependency failure handling |
| D-544 | Performance | arcagent | Connection and resource discipline |
| D-545 | Deployment | arcagent | Rollout default |
| D-546 | Architecture | arcagent | What a connector extension is |
| D-547 | Architecture | arcagent | Where the shared MCP client lives |
| D-548 | Architecture | arcagent | How connector tools surface to agent and policy |
| D-549 | Data Model | arcagent | Where an installed connector lives |
| D-550 | Data Model | arcagent | Multiple accounts on one service |
| D-551 | API Design | arcagent | Install and setup flow |
| D-552 | Audit & Compliance | arcagent | What lands in the audit trail |
| D-553 | Audit & Compliance | arcagent | Credential values carve-out |
| D-554 | Security | arcagent | Secret storage ladder |
| D-555 | Security | arcagent | Env file location |
| D-556 | Security | arcagent | Third-party server supply chain |
| D-557 | Security | arcagent | Trifecta approval policy |
| D-558 | Integration | arcagent | Server process lifecycle |
| D-559 | Extensibility | arcagent | Bundle distribution |
| D-560 | Testing | arcagent | Test strategy |
| D-561 | UI/UX | arcagent | Management surface |
| D-562 | Integration | arcagent | Atlassian upstream |
| D-563 | Security | arcagent | Tool-contract hashing (rug-pull defense) |
| D-564 | Security | arcagent | Sandbox policy and code path |
| D-565 | Security | arcagent | Bundle load root |
| D-566 | Architecture | arcagent | Extensions live outside Arc and plug into hooks |
| D-567 | Architecture | arcagent | What goes where — the three-way placement rule |
| D-568 | Architecture | arcagent | ADR-018's MCP exclusion is reversed |
| D-569 | UI/UX | arcagent | CLI-first — a vetted CLI is the default attachment, MCP is an option |
| D-570 | UI/UX | arcagent | A CLI extension is three parts, not two |
| D-571 | UI/UX | arcagent | Google switches to gogcli; Readwise Reader and GitHub CLI added |
| D-572 | UI/UX | arcagent | arcllm owns PII policy; arcagent consumes the result |
| D-573 | UI/UX | arcagent | calls that never transited arcllm still get the tier's PII policy |
| D-574 | UI/UX | arcagent | federal reads; it does not send to a PII-identified target [refined by D-580] |
| D-575 | UI/UX | arcagent | build the credential broker |
| D-576 | UI/UX | arcagent | build all connectors; order does not matter |
| D-577 | UI/UX | arcagent | audit encrypted at rest now; access control and retention next spec |
| D-578 | UI/UX | arcagent | blueprints do not declare connectors |
| D-579 | UI/UX | arcagent | a manifest's tier floor refuses below, and cannot raise |
| D-580 | UI/UX | arcagent | egress is gated by tier and tool ORIGIN, refused at install |
| D-581 | Testing | arcui | Test strategy |
| D-582 | API Design | arcagent | Agent tools |
| D-583 | Testing | arcagent | Strategy |
| D-584 | Deployment | arcagent | Migration |
| D-585 | CLI | arcagent | Commands |
| D-586 | Architecture | arcrun | Package Name |
| D-587 | Architecture | arcrun | Adopt Steering (Mid-Execution Interrupt) |
| D-588 | Extensibility | arcrun | Adopt Context Transform Hook |
| D-589 | Architecture | arcrun | Adopt Streaming Response Deltas |
| D-590 | Extensibility | arcrun | Adopt Dynamic Tool Registry |
| D-591 | Architecture | arcrun | arcrun Owns Run-Level State |
| D-592 | Deployment | arcrun | Build System |
| D-593 | API Design | arcrun | Tool Type Implementation |
| D-594 | API Design | arcrun | Tool.execute Async Requirement |
| D-595 | API Design | arcrun | Tool.execute Receives Cancellation Signal |
| D-596 | API Design | arcrun | Typed ToolContext Object |
| D-597 | API Design | arcrun | Tool.execute Returns str |
| D-598 | Data Model | arcrun | Generic Event with Dict Data |
| D-599 | Security | arcrun | Sandbox Uses Caller-Provided Checker |
| D-600 | Security | arcrun | Phase 1 Sandbox = Tool-Level + Caller Checker |
| D-601 | Security | arcrun | Allowlist Security Model |
| D-602 | Architecture | arcrun | Strategy Enforces Max Turns |
| D-603 | Data Model | arcrun | Text + Tool Calls Preserved in Message |
| D-604 | Architecture | arcrun | Strategy Prepends to System Prompt |
| D-605 | API Design | arcrun | Two Entry Points — run() + run_async() |
| D-606 | API Design | arcrun | Steer + FollowUp Delivery Modes |
| D-607 | Security | arcrun | jsonschema for Param Validation |
| D-608 | Security | arcrun | Dynamic Tools Denied by Default |
| D-609 | Data Model | arcrun | Strategies Return LoopResult |
| D-610 | API Design | arcrun | Exceptions Bubble Up for Model Errors |
| D-611 | Architecture | arcrun | Extract Shared Tool Executor |
| D-612 | Architecture | arcrun | Single Tool Call Granularity |
| D-613 | API Design | arcrun | Executor Returns tuple[Message, bool] |
| D-614 | Architecture | arcrun | Strategy Owns Cancel/Steer Checks |
| D-615 | Architecture | arcrun | Executor Increments tool_calls_made |
| D-616 | API Design | arcrun | Executor Is Internal (Not Public API) |
| D-617 | Architecture | arcrun | CodeExec Wraps react_loop |
| D-618 | Architecture | arcrun | ABC Base Class for Strategy |
| D-619 | Data Model | arcrun | Strategy Metadata is name + description |
| D-620 | Architecture | cross-cutting | One-way execution-stack dependency graph |
| D-621 | API Design | arcrun | ArcAgent consumes ArcRun through one qualified facade import |
| D-622 | Architecture | arcrun | RunState remains internal to ArcRun |
| D-623 | Architecture | arcrun | Parallel dispatch is a public mechanism, not a concrete strategy dependency |
| D-624 | Architecture | cross-cutting | Do not turn the ArcRun facade into a cross-layer junk drawer |
| D-625 | Testing | arcagent | Dependency boundaries are checked in source and metadata |
| D-626 | API Design | cross-cutting | Core packages have one clean cross-package import |
| D-627 | Security | arctrust | Neutral security primitives live below model and agent layers |
| D-628 | Security | arcagent | Host shell execution owns a bounded process group |
| D-629 | Data Model | arcagent | Consumed session bytes advance independently of indexable rows |
| D-630 | Architecture | arcagent | A nonterminal plan iteration must make monotonic progress |
| D-631 | API Design | cross-cutting | Package metadata declares the public-facade compatibility floor |
| D-642 | Security | arcagent | Secret files are authorized and read through one bounded fd |
| D-643 | Architecture | arcagent | ArcAgent lifecycle transitions are serialized states |
| D-644 | Architecture | arcagent | Shutdown owns and bounds every agent resource |
| D-645 | Security | arcagent | Secret-store replacement locks the full cross-process transaction |
| D-646 | Security | arcagent | Session journals replay through bounded typed streaming |
| D-647 | Architecture | arcagent | One coordinator serializes complete turns per session |
| D-648 | Architecture | arcagent | A module's tools and skills are copied to each agent; its runtime is not |
| D-649 | Security | arcagent | Copied capabilities are untrusted by design; federal drift needs an operator signature |
| D-650 | Data Model | arcagent | Compaction commits an explicit replay baseline by revision |
| D-651 | Security | arcagent | One verb signs and pins; `arc trust approve` produces a real signature |
| D-652 | Security | arcui | arcui signs from the capability inventory, and the agent has no path to it |
| D-653 | Deployment | arcagent | The signing procedure ships as an operator runbook, README claim, and code together |
| D-654 | Security | arcagent | Outbound URL authorization includes DNS destinations |
| D-655 | API Design | arcagent | Capability registry state is exposed only through snapshots |
| D-656 | Architecture | arcagent | Multi-spawn scheduling reserves only work that can run |
| D-657 | Architecture | arcagent | Capability reload publishes only complete candidates |
| D-658 | Architecture | arcagent | One structured primitive owns child-run semantics |
| D-659 | Security | arcagent | Authored Python crosses an isolated JSON execution seam |
| D-660 | Architecture | arcagent | Every detached task has one draining owner |
| D-661 | Security | arcagent | Workspace file authorization is descriptor-relative |
| D-662 | API Design | arcagent | Tool dispatch is an ordered typed pipeline |
| D-663 | Architecture | arcagent | Periodic services share one stoppable cadence contract |
| D-664 | Architecture | arcagent | Module runtimes declare typed dependency contracts |
| D-665 | Security | arcagent | Extension entrypoints are not authored capability files |
| D-666 | Architecture | arcagent | Large facades split only at established domain seams |
| D-667 | Security | arcagent | Isolation backends are acquired only for executable artifacts |
| D-632 | Architecture | arcagent | Every module is optional; none ship in the wheel |
| D-633 | Architecture | arcagent | Modules live at the deployment root; the in-tree directory is a source catalog |
| D-634 | Architecture | arcagent | Module runtimes load by path, not by import name |
| D-635 | Architecture | cross-cutting | One core artifact; the module bundle is the only thing that varies by deployment |
| D-636 | Security | arcagent | Bundle signature is verified before materialize, never after |
| D-637 | Security | arcagent | Module runtime code never lands anywhere an agent can write |
| D-638 | Deployment | arcagent | Federal bundles are built outside the enclave and carried in |
| D-639 | CLI | arcagent | One install command; `--all` is the personal path, explicit names the federal path |
| D-640 | Testing | arcagent | Absent-module safety is proven by removing, not by asserting |
| D-641 | Architecture | arcagent | The development path signs with a dev key rather than skipping verification |

---

# Appendix B — Build & Deepen Notes

Research, diagrams, risk registers and open questions from each `/build` and `/deepen` run live
next to that build's state, in `.claude/builds/<feature>/research.md`.

| Feature | Decisions | Notes |
|---------|-----------|-------|
| multi-agent-ui-architecture | D-001–D-581 (37) | [`builds/multi-agent-ui-architecture/research.md`](builds/multi-agent-ui-architecture/research.md) |
| arcui-llm-telemetry | D-037–D-071 (35) | [`builds/arcui-llm-telemetry/research.md`](builds/arcui-llm-telemetry/research.md) |
| convention-prompt-injection | D-072–D-087 (16) | [`builds/convention-prompt-injection/research.md`](builds/convention-prompt-injection/research.md) |
| arcteam-memory | D-088–D-118 (31) | [`builds/arcteam-memory/research.md`](builds/arcteam-memory/research.md) |
| scheduling-heartbeat | D-119–D-130 (12) | [`builds/scheduling-heartbeat/research.md`](builds/scheduling-heartbeat/research.md) |
| recursive-agent-spawning | D-131–D-144 (14) | [`builds/recursive-agent-spawning/research.md`](builds/recursive-agent-spawning/research.md) |
| cdp-browser-module | D-145–D-159 (15) | [`builds/cdp-browser-module/research.md`](builds/cdp-browser-module/research.md) |
| telegram-messaging | D-160–D-173 (14) | [`builds/telegram-messaging/research.md`](builds/telegram-messaging/research.md) |
| arcrun-phase-4 | D-174–D-187 (14) | [`builds/arcrun-phase-4/research.md`](builds/arcrun-phase-4/research.md) |
| arcllm-budget-routing | D-188–D-201 (14) | [`builds/arcllm-budget-routing/research.md`](builds/arcllm-budget-routing/research.md) |
| arcagent-memory | D-202–D-585 (34) | [`builds/arcagent-memory/research.md`](builds/arcagent-memory/research.md) |
| azure-openai-provider | D-232–D-251 (20) | [`builds/azure-openai-provider/research.md`](builds/azure-openai-provider/research.md) |
| slack-messaging | D-252–D-276 (25) | [`builds/slack-messaging/research.md`](builds/slack-messaging/research.md) |
| arcllm-call-queue | D-277–D-290 (14) | [`builds/arcllm-call-queue/research.md`](builds/arcllm-call-queue/research.md) |
| arc-core-hardening | D-291–D-312 (22) | [`builds/arc-core-hardening/research.md`](builds/arc-core-hardening/research.md) |
| hermes-parity-roadmap | D-313–D-337 (25) | [`builds/hermes-parity-roadmap/research.md`](builds/hermes-parity-roadmap/research.md) |
| nlit-demo-local-build | — | [`builds/nlit-demo-local-build/research.md`](builds/nlit-demo-local-build/research.md) |
| unified-capability-system | D-338–D-368 (31) | [`builds/unified-capability-system/research.md`](builds/unified-capability-system/research.md) |
| nlit-scap-demo | D-369–D-383 (15) | [`builds/nlit-scap-demo/research.md`](builds/nlit-scap-demo/research.md) |
| tool-choice-passthrough | D-384–D-386 (3) | [`builds/tool-choice-passthrough/research.md`](builds/tool-choice-passthrough/research.md) |
| provider-prompt-caching | D-387–D-407 (20) | [`builds/provider-prompt-caching/research.md`](builds/provider-prompt-caching/research.md) |
| ongoing-daily-notes | D-408–D-418 (11) | [`builds/ongoing-daily-notes/research.md`](builds/ongoing-daily-notes/research.md) |
| arcllm-gateway-hardening | D-419–D-458 (40) | [`builds/arcllm-gateway-hardening/research.md`](builds/arcllm-gateway-hardening/research.md) |
| editable-system-prompts | D-459–D-481 (23) | [`builds/editable-system-prompts/research.md`](builds/editable-system-prompts/research.md) |
| arctui | D-482–D-488 (7) | [`builds/arctui/research.md`](builds/arctui/research.md) |
| coding-agent-working-dir | D-489–D-491 (3) | [`builds/coding-agent-working-dir/research.md`](builds/coding-agent-working-dir/research.md) |
| memory-ingestion-eval | D-492–D-500 (9) | [`builds/memory-ingestion-eval/research.md`](builds/memory-ingestion-eval/research.md) |
| arcflow | D-501–D-539 (39) | [`builds/arcflow/research.md`](builds/arcflow/research.md) |
| connector-extensions | D-540–D-580 (41) | [`builds/connector-extensions/research.md`](builds/connector-extensions/research.md) |
| arcrun-core-loop | D-586–D-616 (31) | [`builds/arcrun-core-loop/research.md`](builds/arcrun-core-loop/research.md) |
| phase-2-codeexec | D-617–D-619 (3) | [`builds/phase-2-codeexec/research.md`](builds/phase-2-codeexec/research.md) |
| module-bundles | D-632–D-641, D-648–D-649, D-651–D-653 (15) | [`builds/module-bundles/research.md`](builds/module-bundles/research.md) |

---

## Gateway Messaging + Media — Build Decisions (2026-08-11)

**Phase**: build | **Status**: complete | **Total decisions**: 10 (10 user, 0 auto-applied)
**ID range**: D-668 to D-679
**Priority framework**: simplicity → modularity → security → scalability

### Summary
One messaging path for every surface (CLI, arcui, Telegram, Slack), adapters as droppable in-tree modules, and inbound/outbound media stored in the agent workspace and referenced (never inlined) through the session. Driven by three live defects: a photo produces no run at all, every message starts a fresh turn instead of joining the running one, and arcui Messages goes nowhere.

### Auto-Applied (Compliance Mandates)
_(none)_

### Architecture

#### D-668: Inbound envelope shape
**Decision**: One InboundMessage carrying an ordered list of typed parts; text is a part like any other, so media is never a special case.
**Priority**: simplicity
**Alternatives**: keep message: str and add a parallel attachments list; platform-native passthrough normalised in core
**Rationale**: Today InboundEvent.message is a bare str, so media has nowhere to live. A parallel attachments list keeps two code paths forever and cannot represent a caption between two photos. Passthrough would grow per-platform branches in core, the opposite of adapters being droppable.

#### D-669: Where a gateway part becomes an LLM content block
**Decision**: arcgateway defines its own MediaPart carrying a workspace ref and never imports arcllm. arcagent translates at its own boundary, reaching the types through the arcrun facade (import arcrun, qualified names).
**Priority**: modularity
**Alternatives**: gateway emits arcllm blocks directly; a shared MediaRef type in arcllm
**Rationale**: Matches the dependency graph after the ArcAgent decoupling: arcagent uses the arcrun facade only. Putting the type in arcllm would give the LLM layer a workspace-path concept it has no business knowing (ADR-029).

#### D-670: Adapter location and discovery
**Decision**: Adapters are in-tree arcgateway/adapters/<name>/ modules with a module-level PLATFORM descriptor, found by directory scan. A failed import is skipped and logged; a deleted folder is simply gone.
**Priority**: simplicity
**Alternatives**: explicit registry module; config names them and the gateway imports only those
**Rationale**: Replaces the separate arcgateway-telegram/-slack/-mattermost packages. The filesystem is the registry, so 'delete it with no problem' is literally true; a registry file would have to be edited in step, and config-gating would hide a working adapter until config mentioned it.

#### D-671: How an inbound message reaches a running turn
**Decision**: A message always lands in the session and the agent handles it. steer vs follow-up vs new run is internal to arcagent; 'interrupt' leaves the gateway-facing seam entirely.
**Priority**: simplicity
**Alternatives**: gateway checks active_run and branches; queue drain decides
**Rationale**: Root cause of the reported bug: deliver_message is only bus-published for teammates, so every human surface calls agent.run() and gets a fresh turn. No caller should choose — mid-run or new, it joins the same session and is answered, the way Claude Code behaves.

#### D-672: Where inbound media bytes live
**Decision**: The adapter downloads once; the gateway writes the file under the agent workspace with direct filesystem I/O and passes a MediaPart ref. Session history carries the ref; bytes are materialised only for the provider call that needs them.
**Priority**: simplicity
**Alternatives**: inline base64 in the part; gateway-side content-addressed blob store the agent fetches from
**Rationale**: Web research is consistent against inlining: the Messages API is stateless so base64 is re-sent every turn (Anthropic recommends the Files API for anything referenced more than once); Microsoft Agent Framework and the MCP file-handling write-up both land on metadata-plus-reference, with files moving without passing through the LLM. Inlining would put a 5 MB photo in the session jsonl permanently and leave it unreadable next turn. ADR-029 also makes an inbound attachment agent state, written to the workspace.

#### D-673: Gateway simplification scope
**Decision**: Collapse pairing's four files (1,740 LOC) into one module with one entry point, preserving hashed ids, operator approval and throttling. session.py, runner.py and web.py are untouched this pass.
**Priority**: simplicity
**Alternatives**: also collapse session_queue and the session router; change only what the three bugs force
**Rationale**: Pairing is the single largest concentration and it is four files implementing one idea. Keeping the live inbound path out of scope bounds the blast radius on the only thing currently making agents reachable.

#### D-674: Trust posture for inbound media
**Decision**: A paired channel IS the authorization boundary. Media arriving over Slack, Telegram, or arcui with an operator token does not trip the lethal-trifecta human gate per file.
**Priority**: security
**Alternatives**: gate every inbound file; tier-gate the screening
**Rationale**: Operator decision: trust attaches to the approved channel, not to each artefact, so the trifecta rules that govern untrusted content do not apply to a channel a human already approved. Tier-gating was rejected separately because CLAUDE.md makes the pillars universal and tier a stringency dial, never a gate.

#### D-675: Filename, size and audit for stored media
**Decision**: The gateway names the file: workspace/inbox/<date>/<hhmmss>-<sender>-<sanitised-stem>.<ext>, sender derived from the resolved user_did. The sender's original filename is kept as metadata. Size is capped and one media.received audit event is emitted per file.
**Priority**: security
**Alternatives**: take the sender's filename as-is; sanitise the name but no cap and no per-file audit
**Rationale**: D-674 settles trust in the sender's intent; it does not make the bytes well-formed. Generating the name removes path traversal by construction rather than by policy, the cap guards against an accidental huge upload rather than an attack, and audit is a universal AU pillar because a write into agent state is an operation. Timestamp and sender are in the name so a file is identifiable on disk without opening metadata.

### Testing

#### D-676: How media round-tripping is proven
**Decision**: A parametrised contract suite every discovered adapter must pass (inbound text/image/file, outbound long text, outbound media), plus one test per adapter driving a fake platform payload through the real handler, real gateway and real agent to assertions on the workspace file and the session parts.
**Priority**: security
**Alternatives**: per-layer unit tests; live smoke against a real bot
**Rationale**: The defect being fixed is one line — MessageHandler(filters.TEXT, ...) — that every per-layer unit test passes while photos silently do nothing. Only a test through the real registration path fails on it. Live smoke has the highest fidelity but needs credentials and cannot gate a merge.

### Extensibility

#### D-677: The adapter author's contract
**Decision**: Thin adapter plus optional capability declarations: the adapter holds the connection, turns a payload into parts, and sends parts back; AdapterSpec declares optional extras (edit, threads, reactions) and the gateway degrades when absent. Download, naming, caps, audit, session keys, pairing and splitting stay in the gateway, written once.
**Priority**: modularity
**Alternatives**: fat adapter owning its own storage and delivery; thin adapter with no capability negotiation
**Rationale**: A new platform cannot get the security properties wrong because it never implements them — the fourth adapter is otherwise the fourth chance to forget a size cap. Capability declarations are the concession to platforms genuinely differing; the cost is a negotiation surface that has to be kept honest.


#### D-678: Who owns session identity

**Decision**: `arcgateway` owns session identity — `build_session_key` derives it and `router.new_session` rotates it. `arcui` and `arctui` are viewpoints that call those and never derive or hold their own. A session rotates on an explicit `/new` and on nothing else.

- **Priority**: modularity
- **Alternatives considered**: each surface derives its own key; the agent owns session identity
- **Rationale**: This is already how the code behaves — arcui's `agent_sessions.py` calls `router.new_session` and `chat_ws.py` imports `build_session_key` from `arcgateway.session`. Recording it makes the property enforceable instead of incidental, and matches the arctui ruling that a surface is a viewpoint, not an agent owner. It also retires an open question from this build: arcui was never a source of session instability.
- **Corrects**: the reported symptom "it starts a new session after the run is done" is not a session rotation. The session key is stable and the history is intact; each message becomes a fresh *run* because nothing calls `deliver_message` (the delivery decision above), which reads as a new conversation.

#### D-679: What an inbound message may be injected into [refines D-671]

**Decision**: A message is injected only into the **interactive** run for its session. A background run — scheduler, pulse, proactive, memory consolidation — is never an injection target. With no interactive run in flight, the message becomes a new turn in the same session; it does not interrupt background work and it does not start a new session.

- **Priority**: simplicity
- **Alternatives considered**: inject into whatever run is active; queue behind background work until it finishes
- **Rationale**: That decision said the message always lands in the session but did not distinguish which run is in flight. Injecting into a scheduled job would surface a user's answer inside that job's output and let an interactive turn perturb work the user never asked about. Waiting for background work is worse — a consolidation pass can be long, and the user is owed an answer now. The three states are therefore: interactive run in flight → inject; background run or nothing in flight → new turn, same session; explicit `/new` → rotate.

### Open Questions
- session_queue.py becomes vestigial under the delivery decision unless it earns its keep as flood backpressure. Decide before implementing — CLAUDE.md 3 forbids leaving dead code.
- arcui Messages requires a NATS broker; _connect_backend fails open to None and the channel routes report unavailable. Test with a mock backend and assume the broker is up on DGX.
- Deferred: Anthropic Files API (upload once, pass file_id) for media referenced across several turns. An arcllm capability, not a gateway one.
- Outbound media (agent sends a file back) is covered by the adapter contract above, but its workspace-source and size rules are not yet decided.
- Categories not walked (Data Model, API, Observability, Audit, Security, Integration, Performance, Deployment, UI) were judged settled by the decisions above rather than skipped: the envelope fixes the data model, the delivery decision fixes the API seam, and D-674/D-675 settle audit and security for this feature. /specify should challenge that judgement.

### Related Solutions
_(none)_

