# SPEC-038 — Budgets + classification binding

**Feature:** Four floors that make two asserted-but-unenforced CLAUDE.md security claims true, and that light up two dormant seams built by prior specs:
(A) **wire the dead cost/token budget** in arcrun to real per-run enforcement with a **circuit-breaker**, and populate `PolicyContext.provider_usage` so the SPEC-034 `ProviderLayer` (inert because nothing fills it) enforces per-provider budget/rate;
(B) **provider-label fail-closed** — the budget label must come from trusted provider config, and an unknown label with limits configured must DENY, not ALLOW (closes a fail-open in SPEC-034's `ProviderLayer`);
(C) **classification binding + propagation** — bind a clearance to identity, propagate it monotone-non-increasing across delegation and across the arcteam messenger, and enforce no-read-up / no-write-down / no-exfil at the tool, message, and egress boundaries;
(D) **tag messenger/comms tools `external_comms` + route them through `EgressProxy`** so the SPEC-035 lethal-trifecta gate — armed but dormant because no tool produces the `external_comms` leg — becomes live for real comms.

**Status:** PENDING
**Branch:** `feat/SPEC-038-budgets-classification` (planning only — no branch/commit created; `.claude/` is gitignored)
**Type:** Generic (budget accounting + policy-seam fill + info-flow binding + capability-leg tagging)
**Confidence:** High — every gap is confirmed at file:line; every fix either wires an existing seam (SPEC-034 `provider_usage`, SPEC-035 `EgressProxy`/trifecta, arcteam `Classification` ladder) or is a one-line fail-closed correction. No new subsystem.
**Depends on / references:** SPEC-034 (policy pipeline — SPEC-038 *fills* `ProviderLayer`'s `provider_usage` seam + fixes its unknown-label fail-open), SPEC-035 (lethal trifecta + `EgressProxy` — SPEC-038 *activates* it by tagging real comms tools), SPEC-006/031 (arcteam messaging — SPEC-038 adds message classification + recipient-clearance gate), SPEC-008 (arcteam memory classification — SPEC-038 lifts its `Classification` ladder to arctrust as canonical), SPEC-037 (asymmetric signing — clearance-bearing identity keeps its signed provenance), ADR-019 (tier = stringency), CLAUDE.md Four Pillars + OWASP LLM10/LLM02/ASI06/ASI03, NIST AC-4/SC-6/SC-7/AU-2.

---

## One-liner

arcrun already *accounts* per-run tokens (`RunState.tokens_used`) and cost (`RunState.cost_usd`) but only enforces cost, and only via a field (`max_cost_usd`) that the public `run()` API never sets — so in practice there is **no budget ceiling at all**, and the declared `token_budget`/`cost_budget` fields are dead. arctrust's `ProviderLayer` is a correct per-provider budget predicate that is **never fed** (`PolicyContext.provider_usage` stays `None` at every dispatch) and, worse, **fails open** on an unknown provider label. Classification exists as an `IntEnum` ladder + a no-read-up checker **inside arcteam memory only** — it is not bound to identity, not propagated across delegation, and the arcteam `Message` envelope has **no classification field and no recipient-clearance check**. And SPEC-035's trifecta gate is armed but **dormant** because the outbound comms tools (`messaging_send`, Telegram `notify_user`) carry no `external_comms` capability leg. SPEC-038 closes all four by **wiring what already exists**: a token+cost circuit-breaker in the arcrun loop, a dispatch-time `provider_usage` fill in arcagent (using the trusted provider label), a canonical `Classification` ladder + thin `ClassificationLayer` in arctrust with clearance bound to identity and narrowed on delegation, a classification field + clearance gate on the arcteam messenger, and `external_comms` tags + `EgressProxy` routing on the real comms tools.

## Why (the problem)

Two CLAUDE.md security claims are asserted but not enforced, and two prior-spec seams are built but unlit:

- **LLM10 "Token budgets, request rate limits, cost ceilings … Circuit breakers on runaway loops."** `arcrun/state.py:58-59` declares `token_budget` and `cost_budget` — both **dead** (zero readers). The only enforced metric is `cost_usd` vs `max_cost_usd` (`react.py:193`), but `loop.py`'s `_build_state` never sets `max_cost_usd`, and `run()` exposes no budget parameter — so the ceiling is unreachable. There is **no token enforcement at all** and **no circuit-breaker** wired to the public API. Meanwhile SPEC-034's `ProviderLayer` (`arctrust/policy.py:527`) is a real budget predicate reading `PolicyContext.provider_usage`, but `tool_registry.py:381` leaves `provider_usage=None` on every call (comment: "stay None … SPEC-038"). The layer is inert.
- **Budget-dodge-by-label (from the SPEC-034 review).** `ProviderLayer` at `policy.py:570-572`: when limits are configured but `usage.provider` is not in the limits map, it **ALLOWs** ("unconstrained provider"). An attacker- or misconfig-supplied label bypasses every budget. Compounding it, the response's `model` field (`LLMResponse.model`) is copied verbatim from the provider HTTP body (`adapters/anthropic.py:235`, `adapters/openai.py:314`) — untrusted — while the trusted label is the provider config property `model.model_name` (`adapters/base.py:60`).
- **LLM02 / ASI06 "classification-aware data flow," "no read-up/write-down."** A `Classification` IntEnum ladder (`arcteam/memory/types.py:10`: UNCLASSIFIED<CUI<CONFIDENTIAL<SECRET<TOP_SECRET) and a `ClassificationChecker` (agent clearance ≥ entity level) exist — but **only for memory reads**. `AgentIdentity` (`arctrust/identity.py:166`) has **no clearance**. Delegation (`spawn.py`) narrows tools but carries **no classification**. `arctrust.ToolCall.classification` is an opaque `str` hardcoded to `"unclassified"` at dispatch (`tool_registry.py:371`) — no layer enforces it. The arcteam `Message` (`types.py:88`) has **no classification field**; `messenger.send` (`messenger.py:167`) checks channel *membership*, never *clearance*. Below-clearance data can flow up (sub-agent) or out (message/egress) unchecked.
- **ASI03 / trifecta dormancy.** SPEC-035 wired `GlobalLayer.forbidden_composition` + `EgressProxy`, but the SPEC-035 review confirmed the gate is **dormant**: no built-in produces `external_comms`, and the real outbound module tools are **untagged** — `messaging_send` (`modules/messaging/capabilities.py:274`) and Telegram `notify_user` (`modules/telegram/capabilities.py:60`) carry no `capability_tags`; `browser`'s tag `browser_navigate` is not in `TAG_TO_LEGS`. So an agent can read private data, ingest untrusted input, and message it out with the trifecta gate never firing.

## Decision

**Fill the seams; add the smallest enforcement that makes each claim true; delete the dead predecessors in the same edit (no-legacy rule).**

- **A — budget circuit-breaker.** arcrun tracks per-run **token and cost** consumption (already accounted) and, in the existing top-of-turn guard (`react.py:187-203`), halts the loop when a configured **token OR cost** ceiling is exceeded — the circuit-breaker. Budget parameters are threaded through `run()`→`_build_state`→`RunState`; the dead `token_budget`/`cost_budget` fields are replaced by the real enforced pair. arcagent, at dispatch, reads the live `RunState` (via `ToolContext.parent_state`) and populates `PolicyContext.provider_usage`, lighting up the SPEC-034 `ProviderLayer`. Token is the **robust primary** ceiling (present on both streaming and non-streaming paths); cost is the secondary (non-streaming, only when the model is priced).
- **B — provider-label fail-closed.** The `provider` label on `ProviderUsage` comes from the **trusted** provider config (`model.model_name`), never `LLMResponse.model`. `ProviderLayer`'s unknown-label branch is changed from ALLOW to **DENY when limits are configured** (`provider.unknown_label`), relaxable only at personal.
- **C — classification binding + propagation.** Lift the `Classification` ladder into **arctrust** as the canonical, dependency-free type (arcteam re-imports it, its duplicate deleted). Bind a `clearance` to `AgentIdentity`; propagate it **monotone-non-increasing** through `derive_child_identity`/`spawn` (a sub-agent's clearance ≤ the delegator's — same narrowing invariant SPEC-034 uses for delegation grants). A thin arctrust `ClassificationLayer` enforces **no-read-up** at the tool surface (caller clearance must dominate the resource/tool classification). The arcteam messenger enforces **no-write-down** (recipient/channel clearance must dominate the message classification). `EgressProxy` enforces **no-exfil** (a below-clearance destination cannot receive above-clearance data). Bell-LaPadula, split across the three boundaries where the two labels + direction actually exist.
- **D — activate the trifecta.** Tag the real outbound tools (`messaging_send`, Telegram `notify_user`) with `external_comms` (and `untrusted_input` where they ingest inbound), fix the `browser` tag mapping, and route their outbound through the injected `EgressProxy`. No change to the gate logic — SPEC-035 already decides; SPEC-038 supplies the missing leg producers.

Rationale: A is a token check beside an existing cost check + a `RunState` field fill (Simplicity); B is a one-line ALLOW→DENY correction + a trusted-source read (Security); C reuses an existing ladder and the isolation-ladder predicate pattern, and mirrors delegation-grant narrowing (Modularity); D tags two tools and routes them through a built proxy (Simplicity). Every gate is O(1) over injected state (Scalability).

## Scope (this spec)

1. **Budget circuit-breaker (arcrun)** — per-run token+cost tracking from arcllm usage; halt in the top-of-turn guard on a configured ceiling; thread budget params through `run()`/`_build_state`/`RunState`; delete dead `token_budget`/`cost_budget`; use the existing `make_budget_breach_args` terminator.
2. **`provider_usage` fill (arcagent)** — thread `ToolContext.parent_state` into `wrapped_execute`; map `RunState`→`ProviderUsage` with the **trusted** provider label; populate `PolicyContext.provider_usage`.
3. **Provider-label fail-closed (arctrust + arcllm)** — `ProviderLayer` unknown-label → DENY when configured; confirm the trusted label source.
4. **Canonical `Classification` ladder (arctrust)** — lift arcteam's IntEnum; add `dominates()`; delete the arcteam duplicate (re-import).
5. **Clearance on identity + delegation propagation (arcagent)** — `AgentIdentity.clearance`; narrow on `derive_child_identity`/`spawn`; populate `PolicyContext` clearance + resource classification at dispatch.
6. **`ClassificationLayer` (arctrust)** — thin no-read-up predicate; slots after `GlobalLayer`; fail-closed on missing state when configured; no-op unconfigured.
7. **Messenger classification (arcteam)** — `Message.classification`; recipient/channel clearance gate in `send` (no-write-down); audited.
8. **Egress classification (arcagent)** — `EgressProxy` refuses above-clearance data to a below-clearance destination (no-exfil).
9. **Trifecta activation (arcagent modules)** — tag `messaging_send` + Telegram `notify_user`; fix `browser` mapping; route outbound through `EgressProxy`.

**Out of scope (owned elsewhere — referenced, not duplicated):**
- The `ProviderLayer` budget/rate *decision* — **SPEC-034** (SPEC-038 only fills `provider_usage` + fixes the unknown-label line).
- The `GlobalLayer` forbidden-composition gate, `EgressProxy` internals, `HumanGate` — **SPEC-035** (SPEC-038 only supplies leg producers + destination-classification checks).
- Full data-provenance taint tracking for the untrusted-input leg — deferred (SPEC-035 OQ-1).
- PII/CUI *content detection* (classifying data automatically) — SPEC-038 enforces declared labels; auto-detection is a later spec.
- mTLS / NATS transport — SPEC-045.

## Principled-coder pillars

1. **Simplicity** — one token check beside an existing cost check; one ALLOW→DENY line; one lifted enum; two tools tagged. No new engine, no parallel ladder.
2. **Modularity** — arcrun owns accounting + the breaker; arcllm reports usage + the trusted label; arcagent wires the fill + clearance binding + tagging; arctrust owns the ladder + the pure predicates; arcteam propagates classification on messages. No package reaches across its boundary; arctrust imports no sibling.
3. **Security** — budgets enforced at every tier (LLM10); unknown provider label fails closed (no budget dodge); no-read-up/no-write-down/no-exfil bind classification to identity and flow (LLM02, ASI06); the trifecta gate goes live for real comms (ASI03). Federal floors non-relaxable.
4. **Scalability** — every check is O(1) over injected state (integer compares, set membership); the breaker short-circuits runaway loops before the next model call.

## Documents

- [PRD.md](./PRD.md) — EARS requirements, MoSCoW, threat mapping, pillar-tied acceptance criteria across A/B/C/D.
- [SDD.md](./SDD.md) — components, concern boundaries, how it FILLS the SPEC-034 `ProviderLayer` seam + ACTIVATES the SPEC-035 trifecta, Research Insights (/deepen).
- [PLAN.md](./PLAN.md) — TDD tasks, one module each, REQ→component→task traceable.
