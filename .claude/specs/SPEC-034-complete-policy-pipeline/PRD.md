# SPEC-034 — PRD: Complete the arctrust PolicyPipeline

**Status:** PENDING
**Steering:** `.claude/steering/product.md` (federal-first agentic harness), `roadmap.md` (SPEC-034, P0 pipeline completion)
**Threat coverage:** LLM10 (unbounded consumption), ASI03 (identity/privilege abuse), ASI04 (agentic supply chain), ASI05 (unexpected code exec), ASI07 (insecure inter-agent comms). Compliance: NIST 800-53 AU-9, AU-10, AC-6, AU-2.

Each requirement uses EARS phrasing, carries a MoSCoW priority, and states one acceptance criterion tied to a principled-coder pillar (Simplicity / Modularity / Security / Scalability).

---

## Goals

- Make `ProviderLayer`, `TeamLayer`, `SandboxLayer` real **policy decisions** (no stub ALLOW).
- Keep each layer a pure comparator over state injected via `PolicyContext` — no accounting, no isolation, no re-verification (those are SPEC-038 / SPEC-036 / SPEC-033).
- Route every decision (ALLOW and DENY) through the pipeline's existing single emission point into a WORM / tamper-evident sink.
- Preserve first-DENY-wins, fail-closed, and cache semantics unchanged.

## Non-Goals

- Budget/token/cost accounting or the arcrun/arcllm API that produces it (**SPEC-038**).
- The code-execution isolation backend / microVM routing (**SPEC-036**).
- Load-time signature verification, TOFU approval, restricted-builtins (**SPEC-033**).
- Any new engine feature (shadow mode, restricted mode, cache) — those exist and stay as-is.

---

## Requirements

### ProviderLayer — LLM provider budget & rate-limit gate (LLM10)

**REQ-001 (Must)** — WHEN a `ToolCall` is evaluated and the `PolicyContext` reports the provider's consumed token or cost usage at or above the layer's configured budget for that provider, THE ProviderLayer SHALL return DENY with `layer="provider"`, `rule_id="provider.budget_exceeded"`, and a reason naming the provider, the limit, and the observed usage.
- *Acceptance (Security):* a call whose context usage ≥ configured budget is denied; the audit event records the numeric limit and usage. Verified by a unit test asserting DENY + structured reason for a budget-breach context.

**REQ-002 (Must)** — WHEN a `ToolCall` is evaluated and the `PolicyContext` reports the provider's request count in the current window at or above the layer's configured rate limit, THE ProviderLayer SHALL return DENY with `rule_id="provider.rate_exceeded"`.
- *Acceptance (Scalability):* rate decision is O(1) over injected counters with no I/O or clock read in `evaluate`; a rate-breach context yields DENY, an under-limit context yields ALLOW. Verified by unit tests for both sides.

**REQ-003 (Must)** — THE ProviderLayer SHALL derive its **limits** solely from layer construction (deployment policy) and its **current usage** solely from `PolicyContext`; it SHALL NOT call arcllm, decrement any budget, or hold a mutable usage store.
- *Acceptance (Modularity):* `arctrust` has no import of arcllm/arcagent/arcrun (grep-verified); the layer exposes no method that mutates usage. Verified by an import-boundary test and code review.

**REQ-004 (Must)** — WHERE a per-provider limit IS configured, THE ProviderLayer SHALL treat it as a non-relaxable floor and DENY an over-budget call; WHERE no limit is configured, THE ProviderLayer SHALL be a no-op (ALLOW). The ProviderLayer is constructed only for `enterprise`/`federal`; `personal` runs Identity + Global only, so tier no longer parametrizes the layer's behavior.
- *Acceptance (Security):* a pipeline with a configured limit DENYs an over-budget call; a pipeline with no limits ALLOWs. Verified by unit tests. (ADR-019: stringency, not extra gates.)

**REQ-005 (Should)** — **Configured-gate (product-owner decision, 2026-07-06).** WHEN no provider limit is configured, THE ProviderLayer SHALL ALLOW even with no usage state in `PolicyContext` — absence of a policy is not a violation. WHEN a limit IS configured AND `PolicyContext` carries no provider usage state, THE ProviderLayer SHALL fail closed (DENY, `rule_id="provider.state_missing"`) — a real budget with a blind meter cannot be proven within bounds. Rationale: `build_pipeline` defaults `provider_limits={}` and the dispatch site injects `provider_usage=None` until SPEC-038 lands; the prior "DENY on missing state regardless of config" bricked every enterprise/federal tool call. Mirrors the TeamLayer's `scope is None → ALLOW` pattern.
- *Acceptance (Security):* configured-limit + missing usage → DENY `provider.state_missing`; empty-config + missing usage → ALLOW; enterprise/federal default-config pipeline evaluating a signed call with a blind context → ALLOW. Verified by unit + full-pipeline tests.

### TeamLayer — team-scoped delegation gate (ASI03 / ASI07)

**REQ-006 (Must)** — WHEN a `ToolCall`'s `agent_did` maps (via `PolicyContext`) to a team role, AND the requested `tool_name` is outside the capability scope that role authorizes, THE TeamLayer SHALL return DENY with `layer="team"`, `rule_id="team.scope_violation"`, and a reason naming the role and the authorized scope.
- *Acceptance (Security):* an agent in role R calling a tool absent from R's scope is denied; a tool within scope is allowed. Verified by unit tests for in-scope and out-of-scope calls.

**REQ-007 (Must)** — WHEN a `ToolCall` carries a `parent_call_id` (a delegated call) AND the delegated grant recorded in `PolicyContext` does not authorize the requested tool or exceeds the grant's scope, THE TeamLayer SHALL return DENY with `rule_id="team.delegation_exceeded"`.
- *Acceptance (Security):* a delegated call reaching beyond its grant is denied; a delegated call within grant is allowed. Verified by unit tests covering both.

**REQ-008 (Should)** — THE TeamLayer SHALL express authorization as a capability-scoped role→allowed-scope map supplied at construction; it SHALL NOT consult arcteam directly or hold membership state beyond what `PolicyContext` provides.
- *Acceptance (Modularity):* the layer's only inputs are its constructor config and `PolicyContext`; no arcteam import in arctrust (grep-verified). Verified by import-boundary test.

**REQ-009 (Should)** — WHEN `PolicyContext` carries no team/role for an `agent_did` that is otherwise admitted, THE TeamLayer SHALL ALLOW (absence of team scoping is not a team violation); enforcement of unknown agents remains the IdentityLayer's job.
- *Acceptance (Simplicity):* an agent with no team mapping passes the TeamLayer unchanged. Verified by a unit test. (Prevents the TeamLayer from duplicating IdentityLayer admission.)

### SandboxLayer — dynamic-tool / isolation policy gate (ASI04 / ASI05)

**REQ-010 (Must)** — WHEN a `ToolCall` targets a tool whose verification status in `PolicyContext` is unverified/dynamic (not yet approved by the SPEC-033 load path), THE SandboxLayer SHALL return DENY with `layer="sandbox"`, `rule_id="sandbox.unverified_tool"`.
- *Acceptance (Security):* a call to a tool marked unverified is denied; a verified tool is allowed. Verified by unit tests for both statuses.

**REQ-011 (Must)** — WHEN a `ToolCall` targets a tool whose tier-required isolation level cannot be satisfied by the isolation advertised in `PolicyContext` (from the SPEC-036 backend), THE SandboxLayer SHALL return DENY with `rule_id="sandbox.isolation_unsatisfiable"`.
- *Acceptance (Security):* federal-required isolation with only host available is denied; satisfiable isolation is allowed. Verified by a tier-parametrized test.

**REQ-012 (Must)** — THE SandboxLayer SHALL read verification and isolation status exclusively from `PolicyContext`; it SHALL NOT re-run signature verification, invoke a sandbox backend, or import arcskill/SPEC-036 code.
- *Acceptance (Modularity):* the layer contains no verify/exec call; arctrust import boundary intact. Verified by code review + import-boundary test. (Explicitly avoids duplicating SPEC-033 verify and SPEC-036 isolation.)

**REQ-012a (Should)** — **Configured-gate (product-owner decision, 2026-07-06).** WHEN `PolicyContext` carries no tool runtime status, THE SandboxLayer SHALL be a no-op (ALLOW) at every tier — the SPEC-033 load gate already verified any tool that reached the registry, so a blind SandboxLayer has nothing to add. It gates (`sandbox.unverified_tool` / `sandbox.isolation_unsatisfiable`) only when a status IS present. Rationale: the dispatch site injects `tool_runtime=None` until SPEC-033/036 wire the producer; the prior "DENY on missing state above personal" (`sandbox.state_missing`, now removed) bricked every enterprise/federal tool call.
- *Acceptance (Security):* missing runtime status → ALLOW at enterprise/federal; a present-but-unverified status → DENY. Verified by unit + full-pipeline tests.

**REQ-013 (Won't, this spec)** — THE SandboxLayer SHALL NOT implement dynamic-tool loading, restricted builtins, TOFU approval, or microVM isolation. These are delivered by SPEC-033 (load-time verify) and SPEC-036 (isolation backend) and are referenced, not reimplemented.
- *Acceptance (Simplicity):* scope note recorded; SandboxLayer stays a thin comparator. Verified by SDD boundary section + review.

### PolicyContext extension

**REQ-014 (Must)** — THE `PolicyContext` model SHALL be extended with typed, optional fields carrying provider usage, team/role + delegation grant, and tool verification + isolation status, each defaulting such that existing 3-field constructions remain valid.
- *Acceptance (Simplicity):* existing `PolicyContext(tier=..., policy_version=..., bundle_age_seconds=...)` call sites compile and pass unchanged; new fields are optional with safe defaults. Verified by running the existing arctrust suite unmodified plus a schema test.

**REQ-015 (Must)** — THE new `PolicyContext` fields SHALL be Pydantic-typed and frozen (consistent with the existing model), and populated by the owning specs (SPEC-038 provider, arcteam/arcagent team, SPEC-033/036 sandbox); SPEC-034 SHALL define the schema only.
- *Acceptance (Modularity):* the fields have concrete Pydantic types (no bare `dict[str, Any]` where a model is warranted) and a docstring naming the producing spec. Verified by schema review.

### Audit → WORM

**REQ-016 (Must)** — WHEN the pipeline evaluates any call, THE pipeline SHALL emit exactly one audit event per evaluation for both ALLOW and DENY outcomes through its single emission point.
- *Acceptance (Security):* one event per `evaluate` call, outcome recorded; ALLOW and DENY both emit. Verified by a test counting sink writes across mixed outcomes. (Behavior already present at `policy.py:572`; test locks it.)

**REQ-017 (Must)** — THE system SHALL provide an arctrust adapter that converts the pipeline's `(event_type, payload)` audit callback into an `arctrust.audit.AuditEvent` and writes it to an `arctrust.audit.WormSink` (or any `audit.AuditSink`), and arcagent SHALL pass this adapter into `build_pipeline` in production.
- *Acceptance (Security/Modularity):* the adapter lives in arctrust (no arcagent import) and produces a WORM chain entry whose `action="policy.evaluate"` and whose `outcome` matches the decision; `verify_chain()` passes over the resulting chain. Verified by an adapter unit test + an arcagent wiring test.

**REQ-018 (Must)** — WHEN the audit sink raises, THE pipeline SHALL log and continue, returning the policy decision unchanged (audit failure never blocks or alters enforcement).
- *Acceptance (Scalability):* a throwing sink does not raise out of `evaluate` and does not change the outcome. Verified by a test with a failing sink. (Behavior present at `policy.py:600-603`; test locks it.)

**REQ-019 (Could)** — THE emitted audit payload SHOULD carry `tier`, `layer`, `rule_id`, `input_hash`, and `classification` so the WORM record is sufficient for AU-2 event reconstruction without raw arguments.
- *Acceptance (Security):* payload contains those keys and no raw argument values. Verified by asserting payload keys + absence of `arguments`.

---

## Traceability (REQ → threat → pillar)

| REQ | Layer / area | Threat | Primary pillar |
|-----|--------------|--------|----------------|
| 001–002 | ProviderLayer | LLM10 | Security / Scalability |
| 003 | ProviderLayer boundary | LLM03 | Modularity |
| 004–005 | ProviderLayer tiering | LLM10 | Security |
| 006–007 | TeamLayer | ASI03 / ASI07 | Security |
| 008–009 | TeamLayer boundary | ASI03 | Modularity / Simplicity |
| 010–011 | SandboxLayer | ASI04 / ASI05 | Security |
| 012–013 | SandboxLayer boundary | ASI04 | Modularity / Simplicity |
| 014–015 | PolicyContext | — | Simplicity / Modularity |
| 016–019 | Audit → WORM | AU-9 / AU-10 / AU-2 | Security / Scalability |

## MoSCoW summary

- **Must:** 001, 002, 003, 004, 006, 007, 010, 011, 012, 014, 015, 016, 017, 018
- **Should:** 005, 008, 009
- **Could:** 019
- **Won't (this spec):** 013 (and all Non-Goals: accounting/isolation/verify)
