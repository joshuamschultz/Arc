# SPEC-034 — Complete the arctrust PolicyPipeline

**Feature:** Make the three stub `PolicyLayer`s real (Provider / Team / Sandbox) and thread the pipeline's single audit emission point into a WORM sink, so **every** policy decision — ALLOW and DENY — lands in a tamper-evident record.
**Status:** PENDING
**Branch:** `feat/SPEC-034-complete-policy-pipeline`
**Type:** Generic (policy-layer completion + audit wiring)
**Confidence:** High — the engine, ordering, fail-closed semantics, cache, `_emit_audit` single point, and two real layers (Identity/Global/Agent) already exist and are tested. This spec fills three `return Decision.allow(...)` stubs and connects an already-present `audit_sink` seam to WORM. No engine redesign.
**Depends on:** nothing hard. **Coordinates with** SPEC-038 (fills the Provider budget/rate fields on `PolicyContext`), SPEC-033 (supplies each tool's verification status the SandboxLayer reads), SPEC-036 (advertises available isolation the SandboxLayer reads).

---

## One-liner

`packages/arctrust/src/arctrust/policy.py` ships a first-DENY-wins, fail-closed `PolicyPipeline` with **seven** layers — but `ProviderLayer` (`:309`), `TeamLayer` (`:351`), and `SandboxLayer` (`:360`) each unconditionally `return Decision.allow(...)`, and production `build_pipeline` (agent.py:162) passes **no** `audit_sink`, so the policy trail never reaches the WORM chain. SPEC-034 makes the three layers real **as pure decision gates** — they read state handed to them via `PolicyContext`, they do not compute it — and wires the pipeline's existing single emission point to `arctrust.audit.WormSink`.

## Why (the problem)

Three of the security claims in the Four Pillars are asserted by a stub:

- **ProviderLayer is a no-op.** `policy.py:318` — `async def evaluate(...)` returns ALLOW unconditionally. Nothing enforces LLM10 (unbounded consumption) at the policy boundary: a runaway agent's token/cost/rate limits are never *decided* on, even when the numbers are known.
- **TeamLayer is a no-op.** `policy.py:356` — returns ALLOW unconditionally. Federal is the only tier that even wires it (`build_pipeline`, `:674-682`), and it decides nothing: an agent can invoke a tool its team role never authorized, or ride a delegated grant past its scope (ASI03 / ASI07).
- **SandboxLayer is a no-op.** `policy.py:365` — returns ALLOW unconditionally. A dynamically-created, not-yet-verified tool passes the policy gate untouched, and a call whose tier-required isolation cannot be satisfied is never denied at dispatch (ASI04 / ASI05).
- **The audit trail is off in production.** `_emit_audit` (`policy.py:572`) already fires once per evaluation for ALLOW **and** DENY — the single emission point exists — but `build_pipeline` is called at `agent.py:162` **without** `audit_sink=`, so decisions are logged nowhere durable. AU-9 / AU-10 (tamper-evident, signed audit) is unmet for the authoritative deny path.

Net: the pipeline advertises five decision boundaries and a tamper-evident trail; three boundaries wave everything through and the trail is disconnected.

## Decision

**Fill the three stubs as pure policy decisions, and connect the existing emission seam to WORM — no new engine, no accounting, no isolation, no re-verification.**

- **ProviderLayer** compares configured limits (layer construction, deployment policy) against current usage (`PolicyContext`, filled by SPEC-038) and DENYs on budget/rate breach. It never calls arcllm, never decrements a budget, never owns the budget store.
- **TeamLayer** holds a capability-scoped team-role→authorized-scope map (layer config) and reads the caller's team/role + delegation lineage (`PolicyContext` + `ToolCall.parent_call_id`) to DENY out-of-scope or over-delegated calls.
- **SandboxLayer** reads each tool's verification status (from SPEC-033's load path) and the isolation available for it (from SPEC-036's backend), both carried on `PolicyContext`, and DENYs an unverified/dynamic tool or one whose tier-required isolation is unsatisfiable. It re-verifies nothing and isolates nothing — those belong to 033/036.
- **audit_sink → WORM** — extend nothing in the emission logic; add a small arctrust adapter that turns the pipeline's `(event_type, payload)` callback into an `AuditEvent` written to `arctrust.audit.WormSink`, and pass it from arcagent's `build_pipeline` call.

Rationale: the stubs are stubs precisely because the *state* they need is produced elsewhere; the smallest correct change is to make each layer a comparator over context the sibling specs already own (Simplicity). arctrust owns the layers, the context schema, and the WORM adapter; arcagent only constructs and wires (Modularity). Fail-closed is preserved — a missing context field at enterprise/federal is a DENY, not an ALLOW (Security). Every gate is O(1) over injected state, no I/O in the hot path (Scalability).

## Scope (this spec)

1. **ProviderLayer** — real budget + rate-limit **decision** (reads usage from context, holds limits as config).
2. **TeamLayer** — real team-scoped delegation **decision** (capability-scoped role map + delegation lineage).
3. **SandboxLayer** — real dynamic-tool / isolation **decision** (reads verification + isolation status from context).
4. **PolicyContext extension** — typed, optional fields for the three layers' inputs; existing 3-field callers unaffected.
5. **WORM audit wiring** — arctrust adapter `(event_type, payload) → AuditEvent → WormSink`; arcagent passes it into `build_pipeline`.
6. **Tier stringency** — personal may relax provider/team limits via config; enterprise/federal floors are non-relaxable (ADR-019).

**Out of scope (owned elsewhere — referenced, not duplicated):**
- Budget/token/cost **accounting** and its arcrun/arcllm API surface — **SPEC-038**. This spec only *reads* the numbers.
- The code-execution **isolation backend** — **SPEC-036**. This spec only *reads* what isolation is available.
- Load-time signature **verification / TOFU** — **SPEC-033**. This spec only *reads* the resulting verification status.
- The WormSink implementation itself (exists, `arctrust/audit.py:169`).

## Principled-coder pillars

1. **Simplicity** — three `return allow` stubs become three comparators over injected state; the audit path is one adapter + one constructor argument. No new engine, no accounting, no isolation.
2. **Modularity** — arctrust owns policy types, the `PolicyContext` schema, and the WORM adapter; arcagent constructs/wires; SPEC-036/038/033 own the state producers. arctrust imports no arcagent/arcllm/arcrun.
3. **Security** — every layer decides (no silent ALLOW); fail-closed on missing state above personal; every decision (ALLOW+DENY) reaches a signed WORM chain (AU-9/AU-10). Closes LLM10, ASI03, ASI04/05, ASI07 at the policy boundary.
4. **Scalability** — O(1) gates over injected context, no I/O in `evaluate`; audit sink failure is fail-open-on-audit (logged), never blocks the decision.

## Files

- `PRD.md` — requirements (EARS, pillar-tagged, MoSCoW)
- `SDD.md` — module boundaries + current-state file:line + component design + Research Insights
- `PLAN.md` — phased TDD tasks (one module per task; REQ→component→task traceable)

## Learnings

_(captured during /deepen, /implement, /review)_
