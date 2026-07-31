# SPEC-038 — PRD

**Status:** PENDING
**Owner:** Josh (product owner)
**Format:** EARS acceptance criteria · monotonic `REQ-NNN` · MoSCoW · every requirement tied to a principled-coder pillar and an OWASP/NIST threat.

Steering refs: CLAUDE.md Four Pillars (Identity/Sign/Authorize/Audit), ADR-019 (tier = stringency, not gates), OWASP LLM10 (unbounded consumption), LLM02 (sensitive-info disclosure), ASI06 (memory/context poisoning), ASI03 (identity & privilege abuse); NIST 800-53 AC-4 (information-flow enforcement), SC-6 (resource availability), SC-7 (boundary protection), AU-2 (auditable events), IA-4/AC-6 (identity, least privilege).

---

## Problem statement

Two CLAUDE.md security claims are asserted but unenforced, and two prior-spec seams are built but unlit. (1) The arcrun cost/token **budget is dead**: `token_budget`/`cost_budget` have zero readers, `max_cost_usd` is unreachable via the public `run()` API, there is no token enforcement, and no circuit-breaker is wired — while SPEC-034's `ProviderLayer` sits inert because `PolicyContext.provider_usage` is never populated (LLM10). (2) That same `ProviderLayer` **fails open** on an unknown provider label, and the response-derived label is attacker-suppliable — a budget-dodge-by-label (SPEC-034 review). (3) **Classification** is bound to nothing: a `Classification` ladder + no-read-up checker exist only inside arcteam memory; identity carries no clearance; delegation drops classification; the messenger has no classification field and no recipient-clearance check (LLM02, ASI06). (4) The SPEC-035 **trifecta gate is dormant** because the real outbound comms tools produce no `external_comms` leg (ASI03). This PRD specifies the enforcement floors that make the claims true and light the seams, split across four sub-scopes (A budgets, B provider-label, C classification, D trifecta-activation).

---

## Sub-scope A — Budgets enforced + circuit-breaker (LLM10 unbounded consumption)

The arcrun loop must track per-run token + cost consumption from arcllm usage, halt on a configured ceiling, and feed the SPEC-034 `ProviderLayer`.

### REQ-001 — Per-run token + cost accounting is enforced (Must)
**Pillar:** Security · Simplicity **Threat:** LLM10 · NIST SC-6
The loop shall enforce a configured per-run token ceiling and cost ceiling, not merely account for them.
- **EARS:** While a run executes, before each model invocation the loop shall compare accumulated `tokens_used["total"]` against the configured token ceiling and accumulated `cost_usd` against the configured cost ceiling, and when either is met or exceeded shall stop the loop without starting another turn.
- **AC1 (Security):** A run configured with a token ceiling that the second turn would cross halts after the first, with a `max_tokens` completion reason; a run with a cost ceiling halts with `max_cost`.
- **AC2 (Simplicity):** The token check lives in the existing top-of-turn guard beside the current cost check (`react.py:187-203`); no second loop, no new strategy.

### REQ-002 — Budget is reachable through the public API (Must)
**Pillar:** Modularity **Threat:** LLM10
The dead budget fields shall be replaced by a real, threaded-through pair.
- **EARS:** When a caller supplies a token and/or cost budget to `run()`/`run_async()`, the value shall be threaded through `_build_state` onto `RunState` and read by the enforcement check; the dead `token_budget` and `cost_budget` fields shall be removed in the same edit.
- **AC1 (Modularity):** `run(..., max_tokens=N, max_cost_usd=X)` produces a `RunState` whose enforcement fires; `grep token_budget|cost_budget` returns zero after the edit (no dead field, no duplicate of `max_cost_usd`).
- **AC2:** The unused `make_budget_breach_args` helper is either used by both breach sites or deleted — no dead terminator.

### REQ-003 — Circuit-breaker halt is a clean, audited termination (Must)
**Pillar:** Security (Audit) **Threat:** LLM10 · NIST AU-2
- **EARS:** When the breaker halts a run, the system shall terminate via the `task_complete` breach path (`make_budget_breach_args`) and emit a `loop.completed` event carrying the reason (`max_tokens`/`max_cost`) and the observed tokens/cost.
- **AC1 (Audit):** A halted run emits exactly one terminal event naming the breached metric and the numeric usage at halt.

### REQ-004 — `PolicyContext.provider_usage` is populated at dispatch (Must)
**Pillar:** Modularity · Security **Threat:** LLM10 (fills SPEC-034 `ProviderLayer`)
The SPEC-034 provider budget layer shall receive live usage so it stops being inert.
- **EARS:** When a tool is dispatched during a run, the system shall read the live per-run usage (tokens, cost, request count) from the run state and construct a `ProviderUsage` on `PolicyContext.provider_usage` before policy evaluation.
- **AC1 (Security):** With a configured provider budget already exceeded, the next tool dispatch is DENIED by `ProviderLayer` (`provider.budget_exceeded`) — proving the seam is live; with `provider_usage` populated and under budget it ALLOWs.
- **AC2 (Modularity):** arcagent reads `ToolContext.parent_state` and builds `ProviderUsage`; arcrun exposes the state, arctrust decides. arcrun does not build `PolicyContext`; arctrust does not read `RunState`.

### REQ-005 — Budgets enforced at every tier; federal ceilings non-relaxable (Must)
**Pillar:** Security **Threat:** LLM10 **(ADR-019)**
- **EARS:** The budget check shall run at personal, enterprise, and federal tier; personal may relax the ceiling (advisory/off via config), while enterprise/federal ceilings shall be non-relaxable floors that fail closed.
- **AC1:** Personal with budgets unset runs unbounded (advisory); federal cannot disable or raise its ceiling above the configured floor; the choice is audited.

---

## Sub-scope B — Provider-label fail-closed (LLM10; from the SPEC-034 review)

The label used for budget enforcement must come from trusted config, and an unknown label with limits configured must deny.

### REQ-010 — Provider label comes from trusted config, not the response (Must)
**Pillar:** Security **Threat:** LLM10, LLM05 · NIST SC-6
- **EARS:** When the system constructs `ProviderUsage.provider`, the label shall be sourced from the trusted provider configuration (`model.model_name` / provider `.name`), never from the attacker-suppliable `LLMResponse.model` field.
- **AC1 (Security):** A response whose body reports a different `model` than the configured provider is attributed to the **configured** provider for budgeting; a test asserts the response label cannot redirect attribution.

### REQ-011 — Unknown provider label fails closed when limits are configured (Must)
**Pillar:** Security **Threat:** LLM10 (budget-dodge-by-label)
`ProviderLayer` shall not ALLOW an unrecognized provider when a budget regime is active.
- **EARS:** When `ProviderLayer` has limits configured and `ctx.provider_usage.provider` is not present in the limits map, the layer shall DENY (`provider.unknown_label`) at enterprise/federal, relaxable to ALLOW only at personal.
- **AC1 (Security):** With limits configured for `{anthropic}` and a call reporting `provider="bogus"`, enterprise/federal DENY; personal (relaxable) ALLOWs and audits. (Changes the current fail-open at `policy.py:570-572`.)
- **AC2 (Simplicity):** The change is the single branch at `policy.py:570-572` plus a `relaxable` guard already present on the layer; no new state.

---

## Sub-scope C — Classification binding + propagation (LLM02 sensitive-info disclosure, ASI06 context poisoning, ASI03 privilege abuse)

Classification must be bound to identity, propagate monotone-non-increasing across delegation and the messenger, and enforce no-read-up / no-write-down / no-exfil.

### REQ-020 — Canonical classification ladder lives in arctrust (Must)
**Pillar:** Modularity · Simplicity **Threat:** LLM02 · NIST AC-4
There shall be exactly one ordered classification ladder, owned by arctrust (which owns classification TYPES per CLAUDE.md).
- **EARS:** The system shall define a single ordered `Classification` ladder (`UNCLASSIFIED < CUI < CONFIDENTIAL < SECRET < TOP_SECRET`) with a `dominates()` comparator in arctrust; arcteam shall import it and its duplicate ladder shall be deleted in the same edit.
- **AC1 (Modularity):** `arcteam.memory` imports the arctrust ladder; `grep` shows no second `Classification` enum. arctrust imports no sibling.
- **AC2 (Simplicity):** The ladder mirrors the existing isolation-ladder pattern (`_ISOLATION_LADDER` + `_isolation_satisfies`); `dominates(a,b)` is a total-order integer compare.

### REQ-021 — Identity carries a clearance (Must)
**Pillar:** Security **Threat:** ASI03 · NIST IA-4, AC-6
An agent shall have a maximum classification (clearance) bound to its identity.
- **EARS:** The system shall add a `clearance: Classification` to the agent identity, defaulting to `UNCLASSIFIED`, resolved from operator config at agent construction and immutable for the session.
- **AC1 (Security):** An agent constructed with `clearance=SECRET` reports it; the agent has no tool that can raise its own clearance (config is operator-authored, outside agent-writable paths).

### REQ-022 — Delegation propagates clearance monotone-non-increasing (Must)
**Pillar:** Security **Threat:** ASI03 (privilege abuse), ASI06 · NIST AC-6
A delegated/sub-agent's clearance shall never exceed the delegator's (no privilege escalation).
- **EARS:** When an agent spawns or delegates to a child, the child's effective clearance shall be `min(requested, parent.clearance)`; a request for a higher clearance shall be clamped down (or denied) and audited, never granted.
- **AC1 (Security):** A `SECRET` parent delegating with a requested `TOP_SECRET` child yields a child clamped to `SECRET`; a `CUI` parent cannot mint a `SECRET` child. Mirrors the delegation-grant narrowing invariant (SPEC-034 `team.delegation_exceeded`).
- **AC2:** The child clearance is carried on the derived child identity (`derive_child_identity`), not re-supplied by the child.

### REQ-023 — No-read-up at the tool surface (ClassificationLayer) (Must)
**Pillar:** Security · Simplicity **Threat:** LLM02, ASI06 · NIST AC-4
A tool call shall be denied when the resource/tool classification exceeds the caller's clearance.
- **EARS:** When `PolicyContext` carries a caller clearance and a resource classification, a new `ClassificationLayer` shall DENY (`classification.read_up`) if the resource classification is not dominated by the caller clearance; it shall no-op ALLOW when unconfigured and fail closed (DENY) on missing state when configured at enterprise/federal.
- **AC1 (Security):** A `CUI`-clearance caller invoking a tool bound to a `SECRET` resource is DENIED; a `SECRET` caller is ALLOWED. The layer slots after `GlobalLayer` in the pipeline.
- **AC2 (Simplicity):** The layer is a pure predicate (< ~30 LOC) over injected state, mirroring `ProviderLayer`/`SandboxLayer`; it computes nothing and reads no I/O.

### REQ-024 — No-write-down across the messenger (Must)
**Pillar:** Security **Threat:** LLM02, ASI06 · NIST AC-4
An arcteam message/channel shall carry a classification, and a recipient below that classification shall be refused delivery.
- **EARS:** When a message is sent, the system shall stamp it with a classification (defaulting `UNCLASSIFIED`) and refuse the send/delivery to any recipient or channel whose clearance does not dominate the message classification, returning a structured refusal and an audit event.
- **AC1 (Security):** A `SECRET` message to a `CUI`-clearance recipient (or a `CUI` channel) is refused (`message.classification_refused`); an equal-or-higher-clearance recipient receives it.
- **AC2 (Modularity):** The gate lives in `MessagingService.send` using the shared arctrust `dominates()`; the `Message` model gains a `classification` field. arcteam adds no ladder of its own.

### REQ-025 — No-exfil at the egress boundary (Must)
**Pillar:** Security **Threat:** LLM02 (exfiltration), ASI09 · NIST AC-4, SC-7
Below-clearance destinations shall not receive above-clearance data.
- **EARS:** When a tool egresses through `EgressProxy`, the proxy shall refuse (`egress.classification_refused`) when the data/session classification exceeds the destination's declared clearance, in addition to its existing origin-allowlist check.
- **AC1 (Security):** An egress of `SECRET`-labeled session data to an `UNCLASSIFIED` destination is refused and audited; an allowlisted, cleared destination proceeds.
- **AC2 (Modularity):** The check augments the existing `EgressProxy.request` gate; no new egress path.

### REQ-026 — Classification enforced at every tier; federal mandatory + fail-closed (Should)
**Pillar:** Security **Threat:** LLM02 **(ADR-019)**
- **EARS:** The classification checks (REQ-023/024/025) shall run at every tier; personal shall default to `UNCLASSIFIED` (permissive, no behavior change unless the operator classifies entities), while federal shall treat missing clearance or an unknown classification label as **deny** (fail closed), never defaulting to permissive.
- **AC1:** Federal denies a call/message/egress whose clearance or label cannot be resolved; personal defaults to `UNCLASSIFIED` and proceeds. (Resolves the arcteam `parse_classification` fail-open default for federal — see OQ-2.)

---

## Sub-scope D — Trifecta activation (ASI03; activates SPEC-035)

Real outbound comms tools must produce the `external_comms` leg and route through `EgressProxy` so the armed-but-dormant trifecta gate fires.

### REQ-030 — Messenger/comms tools tag `external_comms` (Must)
**Pillar:** Security **Threat:** ASI03, LLM06 (activates SPEC-035 trifecta)
Every agent-invoked outbound-comms tool shall declare the `external_comms` capability leg (and `untrusted_input` where it ingests inbound content).
- **EARS:** The system shall add `capability_tags` producing `external_comms` to `messaging_send` and Telegram `notify_user`, add `untrusted_input` to their inbound-reading counterparts (`messaging_check_inbox`/`read_thread`), and fix the `browser` tag so it maps to a leg; a test shall assert each tool's derived legs.
- **AC1 (Security):** After the change, a session that reads a private file, ingests untrusted input, then calls `messaging_send` trips the SPEC-035 forbidden-composition gate (previously dormant).
- **AC2 (Modularity):** Tags are declared on the tools (deployment knowledge in arcagent modules); the tag→leg map and the gate are unchanged (SPEC-035 owns them).

### REQ-031 — Outbound comms route through `EgressProxy` (Must)
**Pillar:** Security · Modularity **Threat:** LLM02, ASI09 · NIST SC-7
- **EARS:** When an outbound-comms tool sends externally, the request shall pass through the injected `EgressProxy` (recording the `external_comms` leg and applying the origin allowlist + REQ-025 classification check), rather than opening its own transport.
- **AC1 (Security):** A `messaging_send` / Telegram `notify_user` to a non-allowlisted destination is denied (`egress.denied`); an allowlisted send records the `external_comms` leg into the session ledger.
- **AC2:** No comms tool opens a raw socket; `EgressProxy` is the single mediation point (reuses SPEC-035/SPEC-017 `_egress`).

---

## MoSCoW summary

| Priority | Requirements |
|----------|--------------|
| **Must** | REQ-001, 002, 003, 004, 005, 010, 011, 020, 021, 022, 023, 024, 025, 030, 031 |
| **Should** | REQ-026 |
| **Could** | Per-request rate-window enforcement (fill `requests_in_window` with a real sliding window — MVP uses `tool_calls_made`); classification *auto-detection* of data (PII/CUI content scanning) feeding the labels; a cost estimate on the streaming path (arcllm surfaces cost on `Delta`). |
| **Won't (this spec)** | The `ProviderLayer`/`GlobalLayer` decision logic (SPEC-034/035); `EgressProxy` internals + `HumanGate` (SPEC-035); full untrusted-input taint tracking (SPEC-035 OQ-1); mTLS/NATS transport (SPEC-045). |

## Threat-mapping table

| Sub-scope | Requirements | OWASP / compliance |
|-----------|-------------|--------------------|
| A budgets | REQ-001..005 | LLM10 (unbounded consumption); NIST SC-6, AU-2 |
| B provider-label | REQ-010..011 | LLM10 (budget dodge), LLM05 (untrusted response field); NIST SC-6 |
| C classification | REQ-020..026 | LLM02 (sensitive-info disclosure), ASI06 (context poisoning), ASI03 (privilege abuse); NIST AC-4, AC-6, IA-4, SC-7 |
| D trifecta activation | REQ-030..031 | ASI03, LLM06 (excessive agency), LLM02; NIST AC-4, SC-7 |

## Traceability (REQ → pillar → threat)

| REQ | Pillar (primary) | Threat |
|-----|------------------|--------|
| 001 | Security | LLM10 |
| 002 | Modularity | LLM10 |
| 003 | Security (Audit) | LLM10 / AU-2 |
| 004 | Modularity | LLM10 (fills SPEC-034) |
| 005 | Security | LLM10 / ADR-019 |
| 010 | Security | LLM10 / LLM05 |
| 011 | Security | LLM10 |
| 020 | Modularity | LLM02 / AC-4 |
| 021 | Security | ASI03 / IA-4 |
| 022 | Security | ASI03 / AC-6 |
| 023 | Security | LLM02 / ASI06 / AC-4 |
| 024 | Security | LLM02 / AC-4 |
| 025 | Security | LLM02 / SC-7 |
| 026 | Security | LLM02 / ADR-019 |
| 030 | Security | ASI03 (activates SPEC-035) |
| 031 | Security | LLM02 / SC-7 |

## Open questions (product owner)

- **OQ-1 (classification ladder):** Confirm the canonical ladder is `UNCLASSIFIED < CUI < CONFIDENTIAL < SECRET < TOP_SECRET` (lifting arcteam's existing `IntEnum` verbatim into arctrust as the single owner). Any additional compartments/caveats (e.g. NOFORN, SCI) are **out of scope** for this spec — flag if they must be modeled now.
- **OQ-2 (mandatory-at-federal + default clearance):** Is classification **mandatory** at federal (missing clearance / unknown label → deny, per REQ-026), and what is the **default clearance** for an agent with none declared? Recommendation: default `UNCLASSIFIED` at personal (matches arcteam memory today, permissive, no behavior change for single-dev); at federal, **no default** — a missing clearance is fail-closed deny, and `parse_classification`'s current warn-and-default-UNCLASSIFIED behavior is overridden to deny. Confirm.
- **OQ-3 (budget primary metric + defaults):** Token ceiling is the robust primary (present on both streaming and non-streaming paths); cost is secondary (non-streaming only, and only when the model has pricing metadata). Confirm token-as-primary, cost-as-best-effort-secondary, and whether default ceilings should ship per tier (e.g. federal hard cap) or be operator-set only.
- **OQ-4 (resource classification source):** For REQ-023 no-read-up at the tool surface, where does a *resource* classification come from for a given tool call? Options: (a) a per-tool declared classification in config (`tools.policy.classifications`), (b) the classification of the workspace file/memory entity the tool touches (arcagent knows the path; arcteam memory already labels entities), (c) both. MVP recommendation: (a) declared per-tool + (b) memory-entity labels already enforced by arcteam's `ClassificationChecker`. Confirm scope.
- **OQ-5 (egress destination clearance):** For REQ-025 no-exfil, how is a *destination's* clearance declared — per allowlisted origin in `egress_allowlist` config (origin→clearance map), or a single deployment-wide external-egress ceiling (e.g. "nothing above CUI leaves")? Recommendation: an origin→clearance map with a conservative default of `UNCLASSIFIED` (external = lowest). Confirm.
