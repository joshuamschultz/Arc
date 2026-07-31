# SPEC-038 — SDD

**Status:** PENDING
Traces: PRD REQ-001..031. Pillars: Simplicity → Modularity → Security → Scalability.
**Engines under change:** `arcrun` (budget accounting + circuit-breaker), `arcagent` (provider_usage fill, clearance binding, delegation propagation, egress classification, comms tagging), `arctrust` (Classification ladder + `ClassificationLayer` + provider-label fail-closed), `arcteam` (message classification + recipient-clearance gate), `arcllm` (confirm trusted usage/label shape).

---

## 1. Current state (verified at file:line)

| Element | Location | State |
|---------|----------|-------|
| `RunState.tokens_used` (dict input/output/total) | `arcrun/state.py:49-51` | **Accounted, NOT enforced** — accumulated every turn, surfaced on result, never compared to a ceiling. |
| `RunState.cost_usd` | `arcrun/state.py:52` | Accounted **and** the only enforced metric. |
| `RunState.token_budget`, `RunState.cost_budget` | `arcrun/state.py:58-59` | **DEAD** — zero readers tree-wide. `cost_budget` duplicates `max_cost_usd`. |
| `RunState.max_cost_usd` | `arcrun/state.py:85` | Enforced at `react.py:193` **but** never set by `_build_state` (`loop.py:66-76`) — unreachable via public `run()`. |
| Cost/token accounting | `arcrun/strategies/react.py:337-344` | `_accumulate_usage` reads `response.usage.{input,output,total}_tokens` + `response.cost_usd`. |
| Top-of-turn cost guard | `arcrun/strategies/react.py:187-203` | Cost breach hand-rolls `completion_payload` inline; **no token check**; does not use `make_budget_breach_args`. |
| `make_budget_breach_args(reason)` | `arcrun/builtins/task_complete.py:106-116` | Real terminator, **never called** (dead). |
| `LLMResponse.usage` / `.cost_usd` / `.model` | `arcllm/types.py:85-92, 160-173` | `usage.{input,output,total}_tokens` always present; `cost_usd` top-level float, `None` unless TelemetryModule + per-model pricing; `model` copied verbatim from HTTP body (untrusted). |
| Trusted provider label | `arcllm/adapters/base.py:60-61` (`model.model_name`), telemetry `_inner.name` | Config-sourced; pricing already keys off it (`registry.py:516`). |
| Streaming usage vs cost | `arcllm/types.py:122-135`, `arcrun/streams.py:396-406` | Final `Delta.usage` carries tokens; **no cost on the streaming path**. |
| `PolicyContext.provider_usage` | `arctrust/policy.py:210` (`ProviderUsage` at `:145`) | Frozen field; **always `None` at dispatch** (`tool_registry.py:381-389`). |
| `ProviderLayer.evaluate` | `arctrust/policy.py:527-599` | Real predicate. `if not limits: ALLOW`; `usage is None & configured: DENY provider.state_missing`; **unknown label (`limit is None`): ALLOW** (`:570-572` — fail-open). |
| Dispatch `PolicyContext` build | `arcagent/core/tool_registry.py:362-389` | Sets only `session_capabilities`; `provider_usage`/`team_scope`/`tool_runtime` `None` (comment names SPEC-038). `ToolCall.classification` hardcoded `"unclassified"` (`:371`). |
| arcrun bridge `arcrun_execute` | `arcagent/core/tool_registry.py:274-280` | Receives `ctx: ToolContext` (`.parent_state` = live `RunState`) but **does not pass it into `wrapped_execute`**. |
| `Classification` ladder + no-read-up | `arcteam/memory/types.py:10` (IntEnum UNCLASSIFIED..TOP_SECRET), `arcteam/memory/classification.py` (`ClassificationChecker`) | Real, tested — **arcteam-memory only.** `parse_classification` defaults UNCLASSIFIED, warns on unknown (fail-open). |
| `AgentIdentity` / `ChildIdentity` / `derive_child_identity` | `arctrust/identity.py:166, 415, 435` | **No clearance field.** HKDF-derived child DID; tools narrow (intersection) in `spawn.py`; classification not carried. |
| `arctrust.ToolCall.classification: str` | `arctrust/policy.py:104` | Opaque `str`; feeds cache key + audit only; **no layer enforces it.** |
| Isolation ladder + comparator | `arctrust/policy.py:690-705` (`_ISOLATION_LADDER`, `_isolation_satisfies`) | The structural template a classification ladder follows. |
| `Message` / `Channel` | `arcteam/types.py:88-112, 164-170` | **No classification field.** `messenger.send` (`messenger.py:167-255`) checks channel *membership* only — no clearance. |
| SPEC-035 legs + map + gate | `arcagent/.../capability_ledger.py:23-52`, `arctrust/policy.py:486-514` | Legs `{private_data, external_comms, untrusted_input}`; `TAG_TO_LEGS`; `GlobalLayer` subset gate — **live but dormant** (no built-in emits `external_comms`). |
| `EgressProxy` | `arcagent/tools/_egress.py:49-112`; built `agent_lifecycle.py:201-226` | `.request(url, method, **kwargs)`; origin allowlist; `egress.allowed` auto-records `external_comms` leg (`:220-223`). |
| Untagged outbound tools | `modules/messaging/capabilities.py:274` (`messaging_send`), `modules/telegram/capabilities.py:60` (`notify_user`) | **No `capability_tags`** → produce zero legs. `browser` tag `browser_navigate` not in `TAG_TO_LEGS`. Already tagged: `slack_notify_user`, voice, web. |

---

## 2. Module boundaries (the collision-prevention contract)

These lines are why the spec is scoped as it is. Draw them; do not cross them.

### 2.1 arcrun (accounting + breaker) vs arcllm (reporting) vs arctrust (deciding)
- **arcrun owns:** per-run token+cost accounting (already on `RunState`) and the **circuit-breaker** that halts the loop. It consumes arcllm usage; it does **not** call the provider directly, decrement any budget store, or contain agent/policy logic. `tier`/budget params arrive as parameters to `run()`.
- **arcllm owns:** *reporting* usage per call — `LLMResponse.usage` (tokens, both paths) + `cost_usd` (non-streaming, priced) — and exposing the **trusted** provider label (`model.model_name`). SPEC-038 only *confirms* the shape; the sole optional arcllm change is surfacing cost on the streaming `Delta` (Could).
- **arctrust owns:** the *decision* — `ProviderLayer` compares injected `provider_usage` to configured limits. It never imports arcllm/arcrun.
- **The line:** arcrun measures, arcagent bridges the measurement onto `PolicyContext`, arctrust decides. If `provider_usage` is `None`, arctrust fails closed above personal (SPEC-034 REQ-005) — it does not compute the number.

### 2.2 SPEC-038 FILLS the SPEC-034 `ProviderLayer` seam (does not duplicate it)
- **SPEC-034 owns** the `ProviderLayer` predicate + the `ProviderUsage`/`ProviderLimit` schema + the `provider_usage=None` fail-closed rule.
- **SPEC-038 owns** *filling* `PolicyContext.provider_usage` at dispatch and *fixing the one fail-open branch* (unknown label, REQ-011). SPEC-038 adds **no** new budget-decision logic. The unknown-label fix (`policy.py:570-572`) is the minimal correction of a defect the SPEC-034 SDD did not cover (it is net-new territory, confirmed in the SPEC-034 SDD which never raises the trusted-label concern).

### 2.3 SPEC-038 ACTIVATES the SPEC-035 trifecta (does not rebuild it)
- **SPEC-035 owns** `GlobalLayer.forbidden_composition`, the `TAG_TO_LEGS` map, the `SessionCapabilityLedger`, `EgressProxy` internals, and `HumanGate`.
- **SPEC-038 owns** supplying the missing **leg producers** — tagging `messaging_send`/`notify_user` with `external_comms`, fixing the `browser` map entry, and routing outbound comms through the already-injected `EgressProxy`. This is exactly the activation the SPEC-035 SDD §9.5 names ("activates the moment network/egress tools are added and tagged … SPEC-038"). No gate logic changes.

### 2.4 arctrust owns classification TYPES + the policy decision; boundaries own the flows the pipeline can't see
The determining fact: Bell-LaPadula needs **two labels + a direction**; a single `ToolCall` carries one opaque label + `agent_did` — it has neither the caller's clearance nor the destination's classification. So:
- **arctrust owns** the canonical `Classification` ladder + `dominates()` (dependency-free type system, mirroring `_ISOLATION_LADDER`) and a thin `ClassificationLayer` — a **pure predicate** enforcing **no-read-up** at the tool surface once arcagent injects the caller clearance + resource classification.
- **arcagent owns** *binding* clearance to identity and *propagating* it (delegation narrowing), and *populating* the clearance/resource fields on `PolicyContext`.
- **arcteam owns** *no-write-down* across the messenger (it alone knows sender+recipient clearance and the send direction), using the shared arctrust `dominates()`.
- **arcagent egress owns** *no-exfil* at `EgressProxy` (it alone knows the data label + external destination).
- **The line:** arctrust defines the ladder + the tool-surface predicate; the messenger and egress are the *producers* that populate labels and call the shared comparator for the peer-to-peer and external-destination directions a single `ToolCall` cannot represent. This mirrors SPEC-034's "policy layer is a pure predicate over state a specialized producer owns."

### 2.5 Tier stringency, not gates (ADR-019)
Budgets and classification checks evaluate at **every** tier. Federal is not more layers — it is **non-relaxable floors** on the same checks: budget ceilings cannot be raised/disabled; missing clearance or an unknown classification/provider label is **deny** (not the permissive default). Personal relaxes (budgets advisory/off; classification defaults `UNCLASSIFIED`, unchanged behavior for single-dev).

---

## 3. Component design

### 3.1 Budget circuit-breaker (arcrun, REQ-001..003, 005)

`RunState` (`state.py`): **delete** `token_budget`/`cost_budget`; keep `max_cost_usd`; **add** `max_tokens: int | None = None`. `_build_state` (`loop.py:24-78`) and `run()`/`run_async()` (`loop.py:93-158`) gain `max_tokens`/`max_cost_usd` parameters, threaded onto `RunState` (REQ-002).

Top-of-turn guard (`react.py:187-203`) — extend the existing cost check with a token check in the same block:
```python
over_cost = state.max_cost_usd is not None and state.cost_usd >= state.max_cost_usd
over_tok  = state.max_tokens   is not None and state.tokens_used["total"] >= state.max_tokens
if over_cost or over_tok:
    reason = "max_cost" if over_cost else "max_tokens"
    args = make_budget_breach_args(reason=reason)     # REQ-003 — use the real terminator
    # terminate via task_complete breach path; emit loop.completed{reason, cost_usd, tokens}
    return _build_result(state, ...)
```
Token is the **robust primary** ceiling — `tokens_used["total"]` is populated on both streaming and non-streaming paths; cost is secondary (non-streaming, priced models only). Both breach sites (`max_turns`, budget) use `make_budget_breach_args`, deleting the inline dicts (no-legacy). O(1) integer/float compare per turn (Scalability). Tier stringency (REQ-005) is expressed by whether the caller supplies a non-relaxable ceiling — arcagent passes the config-resolved floor; arcrun just enforces the number.

### 3.2 `provider_usage` fill (arcagent, REQ-004, 010)

**Thread the run state into dispatch.** `arcrun_execute` (`tool_registry.py:274-280`) already holds `ctx: ToolContext` with `ctx.parent_state` (the live `RunState`). Pass it into the wrapped executor so `wrapped_execute` (`:362-389`) can read it. Build `ProviderUsage` before `pipeline.evaluate`:
```python
rs = ctx.parent_state                       # arcrun.state.RunState | None
if rs is not None and provider_label is not None:
    provider_usage = ProviderUsage(
        provider=provider_label,            # TRUSTED — model.model_name (REQ-010), NOT response.model
        tokens_used=rs.tokens_used["total"],
        cost_used=rs.cost_usd,
        requests_in_window=rs.tool_calls_made,   # MVP window proxy (Could: real sliding window)
    )
ctx_pol = PolicyContext(..., provider_usage=provider_usage)
```
`provider_label` is resolved from the agent's configured model (`model.model_name`), carried on the agent, **not** parsed from any response. This lights up `ProviderLayer` (REQ-004 AC1). arcagent reads `RunState`; it does not import arcrun's loop internals beyond the state attribute. arctrust still decides.

### 3.3 Provider-label fail-closed (arctrust, REQ-011)

`ProviderLayer.evaluate`, the unknown-label branch (`policy.py:570-572`):
```python
limit = self._limits.get(usage.provider)
if limit is None:
    if self._relaxable:            # personal only
        return Decision.allow(...)
    return Decision.deny(layer="provider", rule_id="provider.unknown_label",
                         reason=f"provider {usage.provider!r} not in configured budget map")
```
Reuses the layer's existing `relaxable` flag (already set True only for personal in `build_pipeline`). This is the entire change — ALLOW→conditional-DENY. Combined with REQ-010's trusted label, budget-dodge-by-label is closed.

### 3.4 Canonical `Classification` ladder (arctrust, REQ-020)

New in arctrust (owns classification TYPES), mirroring `_ISOLATION_LADDER`:
```python
class Classification(IntEnum):
    UNCLASSIFIED = 0; CUI = 1; CONFIDENTIAL = 2; SECRET = 3; TOP_SECRET = 4

def dominates(clearance: Classification, resource: Classification) -> bool:
    return clearance >= resource            # total-order compare; no-read-up predicate

def parse_classification(value: str, *, strict: bool) -> Classification:
    # strict=True (federal): unknown/empty -> raise (fail closed, REQ-026)
    # strict=False (personal): unknown -> UNCLASSIFIED + warn (today's arcteam behavior)
```
arcteam imports `arctrust.Classification` (arcteam already depends on arctrust) and its `memory/types.py` duplicate ladder is **deleted** in the same edit (REQ-020 AC1). `arctrust.ToolCall.classification: str` is either kept as the opaque audit label or upgraded to reference the ladder — the new enforcement path uses the typed fields below, not the free-form string.

### 3.5 Clearance on identity + delegation propagation (arcagent, REQ-021, 022)

- `AgentIdentity` (`arctrust/identity.py:166`) gains `clearance: Classification = UNCLASSIFIED`, resolved from operator config at construction (REQ-021). (Identity type lives in arctrust; the *value* is operator-config, wired by arcagent.)
- `derive_child_identity` (`identity.py:435`) + `spawn._execute` (`spawn.py:141-200`): the child clearance is `min(requested_clearance, parent.clearance)` — monotone-non-increasing (REQ-022), carried on `ChildIdentity`. A request above the parent's is clamped (or denied at federal) and audited. This is the same narrowing invariant as SPEC-034's `team.delegation_exceeded` and SPEC-035's protected narrowing.

### 3.6 `ClassificationLayer` (arctrust, REQ-023, 026) — deliberately thin

`PolicyContext` gains a typed field:
```python
class ClearanceContext(BaseModel):        # frozen; filled by arcagent at dispatch
    caller_clearance: Classification
    resource_classification: Classification
# PolicyContext.clearance: ClearanceContext | None = None
```
`ClassificationLayer.evaluate`:
1. `cc = ctx.clearance`. If `None`: DENY (`classification.state_missing`) at enterprise/federal, ALLOW at personal (fail-closed-above-personal, matching `ProviderLayer`).
2. `not dominates(cc.caller_clearance, cc.resource_classification)` → DENY `classification.read_up`.
3. else ALLOW.

Slots into the pipeline after `GlobalLayer`: `[identity, global, classification, provider, agent, team, sandbox]`; add `"classification"` to `TierConfig.layer_names` for enterprise/federal (personal may omit, consistent with Provider/Team/Sandbox not wired at personal). < ~30 LOC, pure predicate (REQ-023 AC2). arcagent fills `ClearanceContext` at dispatch: `caller_clearance` from the agent identity, `resource_classification` from the per-tool config label (OQ-4a) and/or the touched memory-entity label (OQ-4b).

### 3.7 Messenger classification — no-write-down (arcteam, REQ-024)

- `Message` (`types.py:88`) gains `classification: str = "UNCLASSIFIED"` (stamped on send).
- `MessagingService.send` (`messenger.py:167`): after membership resolution, for each recipient/channel resolve its clearance and refuse (`message.classification_refused` + audit) unless `dominates(recipient_clearance, parse(message.classification))`. Uses the shared arctrust comparator; recipient clearance comes from the entity/channel registry (Entity already carries `roles`/`capabilities`; add a clearance attribute or resolve from operator config). arcteam adds **no** ladder (imports arctrust). Federal: unknown recipient clearance → deny (REQ-026).

### 3.8 Egress classification — no-exfil (arcagent, REQ-025)

`EgressProxy.request` (`_egress.py:76`) gains a data/session classification + a destination-clearance lookup (origin→clearance map from `egress_allowlist` config, OQ-5). After the origin-allowlist check, refuse (`egress.classification_refused` + audit) unless the destination clearance dominates the session data classification. The session data classification is the max classification read this session (available from the classification-aware ledger / the same session context SPEC-035's ledger uses). Augments the existing gate; no new path.

### 3.9 Trifecta activation (arcagent modules, REQ-030, 031)

- `messaging_send` (`modules/messaging/capabilities.py:274`): add `capability_tags=["network_egress"]` (→ `external_comms`); its inbound readers (`messaging_check_inbox`, `messaging_read_thread`) add a tag mapping to `untrusted_input`.
- Telegram `notify_user` (`modules/telegram/capabilities.py:60`): add `capability_tags=["network_egress"]` (→ `external_comms`).
- Fix the `browser` map: add `browser_navigate` to `TAG_TO_LEGS` (or retag the tool `browser`) so it produces a leg (SPEC-035 owns the map file; this is a one-entry correction).
- Route these tools' outbound through the injected `EgressProxy` (via `_runtime.egress()` for module tools), giving the `external_comms` leg its real producer and applying REQ-025. The SPEC-035 gate now fires on a genuine private-data + untrusted-input + comms session (REQ-030 AC1).

---

## 4. Data flow (dispatch + loop)

```
arcrun loop (react)
  before each turn:  over_cost? over_tokens?  ──► make_budget_breach_args ─► halt + loop.completed   [A: circuit-breaker]
  after model.invoke: _accumulate_usage(response.usage, response.cost_usd) ─► RunState               [existing]

arcagent tool dispatch (wrapped_execute)
  ctx.parent_state (RunState) ─► ProviderUsage(provider=model.model_name TRUSTED, tokens,cost,reqs)   [B/REQ-004,010]
  agent.clearance + resource label ─► ClearanceContext                                                [C/REQ-023]
  PolicyContext(provider_usage, clearance, session_capabilities, …)
     └► pipeline.evaluate:  Identity ─ Global ─ Classification ─ Provider ─ Agent ─ Team ─ Sandbox     [first-DENY-wins]
            Classification: no-read-up (caller ⊒ resource)            [C]
            Provider: budget/rate; unknown label ─► DENY (configured)  [B/REQ-011 fills SPEC-034 seam]
            Global: forbidden_composition (trifecta) now REACHABLE     [D — external_comms leg produced]

arcteam MessagingService.send
  recipient_clearance ⊒ message.classification ? deliver : refuse (message.classification_refused)     [C/REQ-024 no-write-down]

arcagent EgressProxy.request
  origin in allowlist AND destination_clearance ⊒ session_data_class ? send : refuse                   [C/REQ-025 no-exfil]
  on send: record external_comms leg  ─► SessionCapabilityLedger                                       [D activates trifecta]
```

---

## 5. Module / file impact

| Package | File | Change | REQ |
|---------|------|--------|-----|
| arcrun | `state.py` | delete `token_budget`/`cost_budget`; add `max_tokens`; keep `max_cost_usd` | 001, 002 |
| arcrun | `loop.py` | thread `max_tokens`/`max_cost_usd` through `run`/`run_async`/`_build_state` | 002 |
| arcrun | `strategies/react.py` | token check beside cost check; both breaches via `make_budget_breach_args` | 001, 003 |
| arcrun | `builtins/task_complete.py` | use (not delete) `make_budget_breach_args`; remove if truly unused after wiring | 003 |
| arcagent | `core/tool_registry.py` | pass `ctx.parent_state` into `wrapped_execute`; build `ProviderUsage` (trusted label) + `ClearanceContext`; set on `PolicyContext` | 004, 010, 023 |
| arctrust | `policy.py` | unknown-label DENY (`:570-572`); add `ClassificationLayer` + `PolicyContext.clearance`; slot into `build_pipeline`/`TierConfig.layer_names` | 011, 023, 026 |
| arctrust | `classification.py` (new) or in `policy.py` | `Classification` ladder + `dominates()` + `parse_classification(strict=)` | 020, 026 |
| arctrust | `identity.py` | `AgentIdentity.clearance`; `derive_child_identity` clearance narrowing | 021, 022 |
| arcagent | `orchestration/spawn.py` | child clearance = `min(requested, parent)`; audit clamp/deny | 022 |
| arcagent | `core/config.py`, `core/agent_lifecycle.py` | resolve agent clearance + per-tool resource classifications + egress origin→clearance map from config | 021, 023, 025 |
| arcteam | `types.py` | `Message.classification`; import `arctrust.Classification` | 020, 024 |
| arcteam | `memory/types.py` | **delete** duplicate `Classification`; re-import from arctrust | 020 |
| arcteam | `messenger.py` | recipient/channel clearance gate in `send` + refusal audit | 024, 026 |
| arcagent | `tools/_egress.py`, `core/agent_lifecycle.py` | destination-clearance refusal in `request`; origin→clearance wiring | 025 |
| arcagent | `modules/messaging/capabilities.py`, `modules/telegram/capabilities.py` | add `capability_tags` (external_comms / untrusted_input); route through `EgressProxy` | 030, 031 |
| arcagent | `core/session_internal/capability_ledger.py` | add `browser_navigate` → leg (one-entry map fix) | 030 |
| arcllm | `types.py` (Could) | surface `cost_usd` on streaming `Delta` | (Could) |

---

## 6. Failure modes (fail-closed everywhere)

| Condition | Behavior |
|-----------|----------|
| Token or cost ceiling reached | Halt loop via `make_budget_breach_args`; emit `loop.completed` (REQ-001/003) |
| `provider_usage` populated, budget exceeded | `ProviderLayer` DENY `provider.budget_exceeded` (SPEC-034) |
| Unknown provider label, limits configured | DENY `provider.unknown_label` at ent/fed; ALLOW+audit at personal (REQ-011) |
| Response `model` ≠ configured provider | Budget attributed to **configured** provider (REQ-010) — response label ignored |
| `clearance` context missing, configured | `ClassificationLayer` DENY `classification.state_missing` at ent/fed; ALLOW at personal (REQ-023) |
| Caller clearance < resource classification | DENY `classification.read_up` (REQ-023) |
| Delegated child requests higher clearance | Clamp to parent (personal/enterprise) or DENY (federal); audit (REQ-022) |
| Message classification > recipient clearance | Refuse `message.classification_refused` (REQ-024) |
| Egress data classification > destination clearance | Refuse `egress.classification_refused` (REQ-025) |
| Unknown classification label at federal | DENY (strict parse) — never default-permissive (REQ-026) |
| Streaming path (no cost) | Token ceiling still enforced (primary); cost ceiling simply never trips on stream-only runs |

---

## 7. Research Insights (/deepen enrichment)

External patterns consulted to keep budgets + classification federal-friendly and to justify the producer/predicate split. Each check is a **decision gate over injected state**.

### 7.1 Token/cost budget + circuit-breaker patterns
- **Meter/predicate separation (token bucket, fixed/sliding window).** Standard API rate-limiting — AWS API Gateway usage plans, Envoy's global rate-limit service, Stripe per-key limits — separates the *counter* (stateful, owned by the metering service) from the *admission predicate* (stateless compare). SPEC-038 is the counter (arcrun `RunState`); SPEC-034 `ProviderLayer` is the predicate. Envoy's filter→service→OK/OVER_LIMIT is the exact reference: the filter (layer) decides, the service (arcrun) counts. Keeps the layer O(1) and testable with injected numbers.
- **Circuit-breaker for runaway loops (LLM10).** OWASP LLM10 "Unbounded Consumption" prescribes *both* rate limits *and* cost/quota ceilings evaluated **before** the call, with graceful denial — hence a token **and** cost ceiling checked at the top of each turn, not post-hoc. The breaker is the classic Nygard *Circuit Breaker* (Release It!) applied to the agent loop: trip on a threshold, stop calling the expensive dependency (the model). FinOps "hard vs soft budget" maps to the `relaxable` flag — personal = soft (advisory/off), enterprise/federal = hard floor.
- **Token as the robust metric.** Cost depends on a pricing table and is absent on the streaming path; token counts are provider-reported on both paths (`usage.total_tokens`). Enforcing primarily on tokens (and cost as a best-effort secondary) is the resilient choice — the ceiling holds even when a model lacks pricing metadata or streams.
- **Federal fit:** NIST 800-53 SC-6 (resource availability/priority) — a token/cost ceiling with graceful denial is a resource-protection control; recording the numeric limit + usage in the WORM audit (via the existing policy audit path) is SC-6 / AU-2 evidence.

### 7.2 Classification / MAC info-flow models
- **Bell-LaPadula (BLP), 1973 — the canonical MAC confidentiality model.** Two rules: **Simple Security ("no read up")** — a subject may read an object only if its clearance dominates the object's classification; **Star (\*) property ("no write down")** — a subject may write to an object only at or above its own level, preventing high data leaking to a low sink. SPEC-038 realizes BLP across three boundaries where the two labels + direction actually exist: no-read-up at the tool surface (`ClassificationLayer`, REQ-023), no-write-down across the messenger (recipient ⊒ message, REQ-024), and no-exfil at egress (destination ⊒ data, REQ-025). This is textbook BLP decomposed to the enforcement points — the pipeline alone cannot do it because a single `ToolCall` carries only one label and no direction.
- **Lattice-based information-flow (Denning, 1976).** Classifications form a lattice with a dominance relation `⊒`; secure flow is monotone (information may flow only *upward* in the lattice). Our `dominates()` is the lattice `⊒`; delegation clearance narrowing (`min(requested, parent)`, REQ-022) is the monotone-non-increasing flow of *authority* — the dual of Denning's monotone flow of *data*, and the same narrowing invariant as object-capability attenuation (Macaroons/biscuit caveats that only ever shrink scope; SPEC-034 §6.2). A child can never out-clear its parent — no privilege escalation (ASI03).
- **Cross-agent / cross-boundary label propagation.** Distributed IFC systems (Myers & Liskov's decentralized label model; Flume/HiStar OS-level IFC; taint-tracking in data pipelines) all carry the label *with the data* across process/host boundaries and re-check at each sink. SPEC-038's message `classification` field + recipient re-check (REQ-024) is the message-bus analogue: the label travels on the `Message` envelope and is enforced at the delivery sink, exactly as SPEC-035 carries capability legs on the session ledger. arcteam already proved the pattern for memory reads (`ClassificationChecker`, agent-clearance ≥ entity-level) — SPEC-038 lifts that ladder to arctrust and extends the same check to the *send* direction and the *egress* sink.
- **One canonical ladder (no duplicate type systems).** The lattice must be *single* — two ladders (arcteam's IntEnum + a new arctrust one) risk divergent orderings and a confused-deputy across the boundary. Lifting the ladder to arctrust (which owns classification TYPES per CLAUDE.md) and re-importing in arcteam is the DRY realization; deleting the duplicate in the same edit honors the no-legacy rule.
- **Federal fit:** NIST 800-53 **AC-4 (Information Flow Enforcement)** is the controlling control family — BLP/lattice enforcement is the reference implementation of AC-4; **AC-6 (least privilege)** for the delegation narrowing; **SC-7 (boundary protection)** for the egress sink; **IA-4** for clearance-bound identity. This maps cleanly to the DoD/IC classification model the ladder names (CUI per NIST SP 800-171; UNCLASSIFIED→TOP_SECRET per EO 13526), making the enforcement legible to a federal assessor.

### 7.3 Trusted-source labeling & budget-dodge (net-new over SPEC-034)
- **Never authorize on an attacker-controlled attribute.** The response `model` field is data returned by (or through) the provider — untrusted for a security decision (LLM05 improper output handling). Budget/attribution must key on the **configured** identity of the provider (`model.model_name`), the same trusted value the pricing table already uses. This is the same principle as SPEC-035's "provider label must come from a trusted non-agent source" and SPEC-053's operator-key independence: the subject of an authorization decision cannot also be its author.
- **Fail-closed on unknown category.** An unrecognized label under an active limit regime must deny, not pass — otherwise the enforcement is trivially bypassed by naming an unknown category (the classic "default-allow" allowlist bug). REQ-011 flips the one fail-open branch; REQ-026 applies the identical discipline to unknown *classification* labels at federal. Fail-closed-on-unknown is the consistent posture across budget and classification.

### 7.4 Net design consequence
All threads converge on the shape the codebase already commits to: **the policy layer is a pure predicate over state a specialized producer owns**, and **the label travels with the data, re-checked at each sink.** Budgets → arcrun counts, `ProviderLayer` decides. Classification → arctrust owns the lattice, arcagent binds clearance to identity, the messenger and egress enforce the write-down/exfil directions the pipeline can't see. This is why the layers stay small, arctrust needs no sibling imports, and both the SPEC-034 seam and the SPEC-035 gate light up by *wiring*, not building.

---

## 8. Test strategy (maps to PLAN)

- **Unit (arcrun):** token breach halts before the next turn; cost breach halts; both use `make_budget_breach_args`; `run(max_tokens=…, max_cost_usd=…)` reaches enforcement; dead fields gone.
- **Unit (arctrust):** `dominates()` total order; `ClassificationLayer` ALLOW/DENY/state-missing per tier; `parse_classification(strict=True)` raises on unknown; `ProviderLayer` unknown-label DENY (ent/fed) vs ALLOW (personal).
- **Unit (arcagent):** `ProviderUsage` built from `RunState` with the trusted label; `response.model` cannot redirect attribution; child clearance = `min(requested, parent)`; egress refuses above-clearance destination.
- **Unit (arcteam):** `send` refuses message whose classification exceeds recipient/channel clearance; imports arctrust ladder (no duplicate).
- **Integration:** (a) budget-exceeded run → next dispatch DENIED by `ProviderLayer` (seam live); (b) full trifecta E2E — read private file → ingest untrusted → `messaging_send` trips the SPEC-035 gate (activation proven, replacing SPEC-035's dormant state).
- **Boundary:** import test — `arctrust` imports none of arcagent/arcllm/arcrun/arcteam/arcskill; `arcrun` imports no arcagent/arctrust.

---

## 9. Traceability (REQ → component)

| REQ | Component(s) |
|-----|-------------|
| 001, 003 | react.py top-of-turn token+cost breaker; `make_budget_breach_args` |
| 002 | `RunState` field replacement; `run`/`_build_state` threading |
| 004 | `wrapped_execute` `ProviderUsage` fill from `ctx.parent_state` |
| 005 | config-resolved ceiling (relaxable flag) → arcrun param |
| 010 | trusted `model.model_name` label at the fill site |
| 011 | `ProviderLayer` unknown-label DENY branch |
| 020 | arctrust `Classification` ladder + `dominates()`; arcteam duplicate deleted |
| 021 | `AgentIdentity.clearance` + config resolution |
| 022 | `derive_child_identity` + `spawn` clearance narrowing |
| 023 | `ClassificationLayer` + `PolicyContext.clearance`; arcagent fill |
| 024 | `Message.classification` + `MessagingService.send` gate |
| 025 | `EgressProxy.request` destination-clearance refusal |
| 026 | `parse_classification(strict)`; federal fail-closed branches |
| 030 | `messaging_send`/`notify_user` tags; `browser_navigate` map fix |
| 031 | outbound routing through `EgressProxy` |

## 10. Boundary guardrails (do not cross)

- **arcrun** adds accounting + the breaker only — `tier`/budget are **parameters**; no `PolicyContext`, no arctrust import.
- **arcllm** only *reports* usage + the trusted label — no budget logic.
- **arctrust** adds the ladder + `ClassificationLayer` + the one provider-label branch — pure predicates over injected state; **no sibling imports**.
- **arcagent** *wires*: `provider_usage` fill, clearance binding + delegation narrowing, egress classification, comms tagging. No budget-decision or ladder logic.
- **arcteam** *propagates* classification on messages using the arctrust ladder — no ladder of its own; no policy-layer logic.
- Every added seam deletes its dead predecessor in the same edit (dead budget fields; arcteam duplicate ladder; inline breach dicts).
