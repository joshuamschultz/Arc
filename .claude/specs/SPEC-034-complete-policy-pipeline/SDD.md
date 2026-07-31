# SPEC-034 — SDD: Complete the arctrust PolicyPipeline

**Status:** PENDING
**Engine under change:** `packages/arctrust/src/arctrust/policy.py`
**Wiring under change:** `packages/arcagent/src/arcagent/core/agent.py` (construction), `packages/arctrust/src/arctrust/audit.py` (WORM adapter home)

---

## 1. Current state (verified at file:line)

| Element | Location | State |
|---------|----------|-------|
| `PolicyPipeline.evaluate` — first-DENY-wins, exception→DENY, cache, shadow, restricted | `policy.py:476-516` | Real, tested. **Unchanged** by this spec. |
| `_emit_audit` — single emission point, fires per-eval for ALLOW+DENY | `policy.py:572-603` | Real. Emits only when `audit_sink` is set; swallows sink exceptions. **Behavior kept; production wiring added.** |
| `IdentityLayer` / `GlobalLayer` / `AgentLayer` | `policy.py:227,281,322` | Real, tested. Reference implementations for the three stubs. |
| `ProviderLayer.evaluate` | `policy.py:318-319` | **Stub** — `return Decision.allow(...)`. |
| `TeamLayer.evaluate` | `policy.py:356-357` | **Stub** — `return Decision.allow(...)`. |
| `SandboxLayer.evaluate` | `policy.py:365-366` | **Stub** — `return Decision.allow(...)`. |
| `PolicyContext` | `policy.py:107-114` | Frozen; only `tier`, `policy_version`, `bundle_age_seconds`. **Extended here.** |
| `build_pipeline(... audit_sink=None)` | `policy.py:611-691` | Threads `audit_sink` into the pipeline. Layers constructed with no config for the three stubs. **Layer construction + call site updated.** |
| Production construction | `agent.py:162-165` | `build_pipeline(tier=..., agent_registry=...)` — **no `audit_sink`**. This is the WORM gap. |
| Dispatch call site | `tool_registry.py:295-332` | Builds `PolicyContext(tier, policy_version, bundle_age_seconds=0.0)` and evaluates. **Populates new context fields as they become available (SPEC-038/036 fill later).** |
| `AuditEvent` / `AuditSink` Protocol / `WormSink` / `emit` | `audit.py:58,114,169,484` | Real. `AuditSink.write(AuditEvent)`. WORM adapter is added alongside. |

**Type mismatch to bridge:** the pipeline's audit seam is `AuditSink = Callable[[str, dict[str, Any]], None]` (`policy.py:49`) — a `(event_type, payload)` callback. The durable sink is `audit.AuditSink` = `.write(AuditEvent)` with `WormSink` as the WORM implementation. One adapter closes this.

---

## 2. Module boundaries (the collision-prevention contract)

These lines are the reason this spec is scoped the way it is. Draw them explicitly; do not cross them.

### 2.1 Provider vs **SPEC-038** (accounting)

- **SPEC-034 owns:** the *decision*. `ProviderLayer` holds configured **limits** (budget, rate) at construction, reads **current usage** from `PolicyContext.provider_usage`, and returns ALLOW/DENY. Pure comparator.
- **SPEC-038 owns:** the *accounting*. It measures token/cost consumption against the arcrun/arcllm call surface, maintains the budget store, and **populates** `PolicyContext.provider_usage` at dispatch time. It does not decide.
- **The line:** SPEC-034 never imports arcllm, never decrements, never persists usage. If `provider_usage` is `None`, SPEC-034 fails closed above personal (REQ-005) — it does not compute the number itself. SPEC-034 ships the field *schema*; SPEC-038 fills it.

### 2.2 Sandbox vs **SPEC-033** (verify-at-load) & **SPEC-036** (isolation backend)

- **SPEC-033 (done)** — sign/verify + TOFU + restricted-builtins at **load** time. Produces a per-tool *verification status*.
- **SPEC-036 (done)** — the real code-exec isolation backend at **run** time. Advertises what *isolation* is available for a tool on this host/tier.
- **SPEC-034 owns:** the *dispatch-time policy decision only* — DENY an unverified/dynamic tool (reads status from `PolicyContext.tool_verification`), DENY when tier-required isolation is unsatisfiable (reads availability from `PolicyContext.isolation_available`).
- **The line:** SPEC-034 re-runs no verification (that is 033) and starts no sandbox (that is 036). **This layer is deliberately thin.** The "is this tool signed/approved" question is *answered* by 033; SandboxLayer only *reads the answer and gates on it*. The "can we isolate this" question is *answered* by 036; SandboxLayer only *reads and gates*. Sub-scopes fully covered elsewhere are **not** re-checked here — no AST scan, no builtin restriction, no microVM launch. If both statuses are "verified" and "isolation satisfiable," the layer ALLOWs in ~2 comparisons.

### 2.3 arctrust owns policy types; arcagent wires

- **arctrust:** `PolicyLayer` implementations, the `PolicyContext` schema, the WORM adapter (arctrust owns *both* `policy` and `audit`, so the adapter has no cross-package dependency). **arctrust imports no arcagent / arcllm / arcrun / arcteam / arcskill.**
- **arcagent:** constructs the `WormSink` (owns the audit file path + config), builds the adapter, and passes it into `build_pipeline`; decides which layers are active per tier (already in `build_pipeline`); populates `PolicyContext` at the dispatch site with whatever state the sibling specs have wired.

### 2.4 Tier stringency, not gates (ADR-019)

All active layers evaluate at every tier. Federal is not "more layers with more checks"; it is **stricter limits and non-relaxable floors** on the same layers. `personal` may relax provider/team limits via config (and, per the tier matrix, does not even wire Provider/Team/Sandbox — `policy.py:664-682`); `enterprise`/`federal` floors are non-relaxable and fail closed on missing state.

---

## 3. Component design

### 3.1 `PolicyContext` extension (arctrust, REQ-014/015)

Add optional, frozen, Pydantic-typed fields. Each names its producing spec. Existing 3-field constructions stay valid because every new field defaults.

```
class ProviderUsage(BaseModel):        # filled by SPEC-038
    frozen. provider: str; tokens_used: int; cost_used: float; requests_in_window: int

class TeamScope(BaseModel):            # filled by arcteam/arcagent
    frozen. role: str; authorized_tools: frozenset[str]
    delegation_grant: frozenset[str] | None = None   # scope of a delegated call, if any

class ToolRuntimeStatus(BaseModel):    # filled by SPEC-033 (verified) + SPEC-036 (isolation)
    frozen. verified: bool; required_isolation: str; available_isolation: str

# PolicyContext gains:
provider_usage: ProviderUsage | None = None
team_scope: TeamScope | None = None
tool_runtime: ToolRuntimeStatus | None = None
```

Concrete models (not `dict[str, Any]`) so the producing specs get a typed contract and mypy checks the fill sites. `required_isolation`/`available_isolation` are compared by an ordered isolation ladder (host < container < vm) owned by SPEC-036's vocabulary; SPEC-034 references the ordering, does not define isolation mechanics.

### 3.2 `ProviderLayer` (arctrust, REQ-001..005)

Construction gains `limits_by_provider: dict[str, ProviderLimit]` and a `relaxable: bool` (True only when built for personal). `evaluate`:
1. `usage = ctx.provider_usage`. If `None`: DENY (`provider.state_missing`) when not `relaxable`, else ALLOW.
2. Look up `limit = limits[usage.provider]` (absent limit → ALLOW; unconstrained provider).
3. `usage.tokens_used >= limit.max_tokens or usage.cost_used >= limit.max_cost` → DENY `provider.budget_exceeded` (reason names limit + observed).
4. `usage.requests_in_window >= limit.max_requests` → DENY `provider.rate_exceeded`.
5. else ALLOW. No clock read, no I/O — all inputs injected.

### 3.3 `TeamLayer` (arctrust, REQ-006..009)

Construction gains `roles: dict[str, frozenset[str]]` (role → authorized tool scope) — capability-scoping. `evaluate`:
1. `scope = ctx.team_scope`. If `None`: ALLOW (no team scoping applies; admission is IdentityLayer's job — REQ-009).
2. Authorized set = `roles.get(scope.role, scope.authorized_tools)` (context may carry the role's scope directly for dynamic teams; construction map is the static floor).
3. `call.tool_name not in authorized` → DENY `team.scope_violation` (reason names role + scope).
4. If `call.parent_call_id is not None` (delegated) and `scope.delegation_grant is not None` and `call.tool_name not in scope.delegation_grant` → DENY `team.delegation_exceeded`.
5. else ALLOW.

### 3.4 `SandboxLayer` (arctrust, REQ-010..013) — deliberately thin

`evaluate`:
1. `rt = ctx.tool_runtime`. If `None`: DENY (`sandbox.state_missing`) at enterprise/federal, ALLOW at personal (consistent fail-closed-above-personal).
2. `not rt.verified` → DENY `sandbox.unverified_tool`.
3. `not _isolation_satisfies(rt.available_isolation, rt.required_isolation)` → DENY `sandbox.isolation_unsatisfiable`.
4. else ALLOW.

`_isolation_satisfies` is a total order comparison over the SPEC-036 isolation ladder — a ~3-line helper, no isolation logic. Target layer size < ~30 LOC (REQ-013 acceptance).

### 3.5 WORM audit adapter (arctrust `audit.py`, REQ-016..019)

```
def worm_policy_sink(sink: AuditSink) -> policy.AuditSink:
    """Adapt the pipeline's (event_type, payload) callback to a durable AuditSink.

    Maps payload → AuditEvent(action=event_type, actor_did, target=tool_name,
    outcome=decision, classification, tier, payload_hash=input_hash,
    extra={layer, rule_id, reason, cache_hit, shadow, evaluation_time_us})
    and calls emit(event, sink). Raw arguments are never copied (REQ-019).
    """
```

- Lives in `arctrust.audit` (arctrust owns both sides — no cross-package import). Returns a `Callable[[str, dict], None]` matching `policy.AuditSink`.
- `emit(event, sink)` already exists (`audit.py:484`) and is the write path into `WormSink`.
- The pipeline's `_emit_audit` already swallows sink exceptions (`policy.py:600-603`) → REQ-018 holds; add a test to lock it.

### 3.6 Wiring (arcagent `agent.py`, REQ-017)

At `agent.py:162`, construct a `WormSink` (path from config, `0600`, owned by arcagent) and pass `audit_sink=worm_policy_sink(worm)` into `build_pipeline`. `build_pipeline` gains the provider/team limit configs (default empty → today's behavior) so the layers receive their limit maps; the config source is the security/policy section of `arcagent.toml`. The dispatch site (`tool_registry.py:325`) populates the new `PolicyContext` fields where producers exist; until SPEC-038/036 fill them, they stay `None` and the fail-closed/ALLOW-personal rules apply.

---

## 4. Data flow (dispatch)

```
tool_registry.wrapped_execute
  → ToolCall(signed)                                   [existing]
  → PolicyContext(tier, policy_version, bundle_age,
                  provider_usage?, team_scope?, tool_runtime?)   [fields NEW, fills later]
  → pipeline.evaluate
      Identity → Global → Provider → Agent → Team → Sandbox      [first-DENY-wins, unchanged]
      _emit_audit(event_type="policy.evaluate", payload)          [existing single point]
         → worm_policy_sink → AuditEvent → emit → WormSink        [NEW wiring, tamper-evident chain]
  → DENY → raise PolicyDenied ; ALLOW → execute                  [existing]
```

---

## 5. Test strategy (maps to PLAN)

- **Unit (arctrust):** each layer — ALLOW and DENY sides, per-tier relaxation, missing-state fail-closed. Adapter — payload→AuditEvent mapping, no raw args, WORM `verify_chain()` passes. Context — existing 3-field construction still valid.
- **Integration (arctrust):** full `build_pipeline` per tier with a `WormSink`; assert first-DENY-wins ordering across the three now-real layers and one chain entry per evaluation for mixed outcomes.
- **Integration (arcagent):** agent construction wires a WORM sink; a denied dispatch produces a verifiable chain record; a failing sink does not break dispatch.
- **Boundary:** grep/import test — `arctrust` imports none of arcagent/arcllm/arcrun/arcteam/arcskill.

---

## 6. Research Insights (/deepen enrichment)

External patterns consulted to keep the three layers federal-friendly and to justify the "decision, not accounting/isolation/verify" split. Each is a **decision gate over injected state**, matching the pillars.

### 6.1 Provider budget & rate-limit policy patterns

- **Token-bucket / fixed-window as *state*, decision as *predicate*.** Standard API rate-limiting (token bucket, sliding/fixed window — the model behind AWS API Gateway usage plans, Envoy's global rate-limit service, Stripe's per-key limits) cleanly separates the *counter* (stateful, hot path, owned by the metering service) from the *admission predicate* (stateless comparison). This is exactly the SPEC-034/038 split: 038 is the counter service, 034 is the predicate. Envoy's rate-limit design — a filter that calls out to a service holding the counts and returns OK/OVER_LIMIT — is the reference: the filter (our layer) decides, the service (SPEC-038) counts. Keeps the layer O(1) and testable with injected numbers (REQ-002 acceptance).
- **Budget ceilings + OWASP LLM10.** The OWASP LLM10 (Unbounded Consumption) guidance recommends *both* rate limits *and* cost/quota ceilings, evaluated before the call, with graceful denial — which is why REQ-001 gates on token **and** cost, not just requests. FinOps "hard budget vs soft budget" maps to our `relaxable` flag: personal = soft (advisory/off), enterprise/federal = hard floor (REQ-004).
- **Federal fit:** NIST 800-53 SC-6 (resource availability) / AU-2 events — denying and auditing an over-budget call is a resource-protection control; recording the numeric limit + usage in the WORM event supports SC-6 evidence.

### 6.2 Team / delegation authorization models

- **Capability-scoping over ACL identity checks.** The literature on least-privilege delegation (object-capability model; Google's Macaroons — bearer tokens with *caveats* that only ever narrow scope; biscuit-auth's attenuable tokens) all share one rule: **a delegated grant can only be a subset of the delegator's authority, never a superset.** REQ-007's `delegation_exceeded` is the enforcement of exactly that monotonic-narrowing invariant, read from `scope.delegation_grant`. This is stronger and simpler than role-inheritance ACLs, which tend to leak privilege upward.
- **RBAC role→scope as the static floor, context as the dynamic grant.** NIST RBAC (INCITS 359) role-permission assignment is the `roles: dict[str, frozenset[str]]` construction map; the per-call `TeamScope` in context is the *activated* session grant. Splitting static role config (deployment) from activated grant (runtime) matches REQ-008's boundary and keeps arctrust free of arcteam.
- **Federal fit:** NIST 800-53 AC-6 (least privilege), AC-3 (access enforcement), AC-4-adjacent for delegated flows. Denying an over-delegated call is an AC-6 enhancement; the WORM record is the AC-6(9) "audit use of privileged functions" evidence.
- **Boundary confirmation:** membership/role *derivation* (who is on which team, what a role means) is arcteam's job; SPEC-034 only *reads and gates*. This mirrors the Provider split and prevents arctrust→arcteam coupling.

### 6.3 Tamper-evident audit sink patterns

- **Hash-chained + signed append-only log.** The WORM sink already implements the canonical tamper-evident pattern (per-record `prev_hash` link + monotonic `seq` + Ed25519 signature + genesis anchor — `audit.py:151-185`), which is the same construction as Certificate Transparency's Merkle log, AWS QLDB's journal, and Trillian: any mutation, reorder, or truncation is detectable by `verify_chain()`. SPEC-034 adds **no** new crypto — it routes the policy decisions *into* this existing chain. The single-emission-point discipline (`_emit_audit`, one call per evaluation) is what guarantees the chain has exactly one entry per decision (REQ-016), which is the property auditors need for completeness.
- **Fail-open on the audit path, fail-closed on the decision.** Best practice (and the existing `policy.py:600-603` behavior) is that an audit-write failure must never suppress or alter the security decision — otherwise a DoS on the log becomes a DoS on enforcement. REQ-018 locks this: the sink exception is logged and swallowed; the decision stands. (The compliance trade-off — "should a failed audit *block*?" — is deliberately resolved toward availability here because the decision itself is still made and the failure is itself logged; a federal deployment that requires block-on-audit-failure would layer that on the sink, not the pipeline.)
- **No raw payload in the record.** REQ-019 follows AU-9/PII-minimization: store `payload_hash`/`input_hash`, never raw `arguments`. The chain proves *what was decided* without becoming a data-exfiltration surface (LLM02).
- **Federal fit:** NIST 800-53 AU-9 (protection of audit information — the signed chain), AU-10 (non-repudiation — the Ed25519 signature binds the record to the writer), AU-2 (auditable events — one event per policy decision).

### 6.4 Net design consequence

All three research threads converge on the same shape SPEC-034 already commits to: **the policy layer is a pure predicate over state that a specialized producer owns.** Provider→SPEC-038, Team→arcteam, Sandbox→SPEC-033/036. This is why the layers are small, why arctrust needs no sibling imports, and why the audit story is "wire the existing chain," not "build a log."
