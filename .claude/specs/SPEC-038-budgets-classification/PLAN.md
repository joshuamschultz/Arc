# SPEC-038 — PLAN

**Status:** PENDING
**Method:** TDD (RED → GREEN → REFACTOR). Each task is scoped to one module and traces REQ → component → task. Tasks within a phase are ordered by dependency. `[P]` = parallelizable with siblings (no shared file).

Legend: **owner package** in brackets. Every task: write the failing test first, implement minimal, verify `ruff check` + `mypy --strict` + `pytest` clean before check-off. Every added seam deletes its dead predecessor in the same edit (no-legacy rule).

---

## Phase A — Budget circuit-breaker + provider_usage fill (REQ-001..005, 010) · [arcrun + arcagent]

- [ ] **A1 — `RunState` budget fields** `[arcrun.state]`
  RED: constructing `RunState(max_tokens=100, max_cost_usd=1.0)` holds both; `grep token_budget|cost_budget` returns nothing.
  GREEN: delete dead `token_budget`/`cost_budget`; add `max_tokens: int | None = None`; keep `max_cost_usd`.
  Trace: REQ-002.

- [ ] **A2 — Thread budget through the public API** `[arcrun.loop]`
  RED: `run(..., max_tokens=N, max_cost_usd=X)` and `run_async(...)` produce a `RunState` carrying the values via `_build_state`.
  GREEN: add params to `run`/`run_async`/`_build_state`; set on `RunState`.
  Trace: REQ-002, REQ-005.

- [ ] **A3 — Token+cost circuit-breaker in the loop** `[arcrun.strategies.react]`
  RED: a run whose 2nd turn would cross `max_tokens` halts after turn 1 with reason `max_tokens`; a `max_cost` run halts with `max_cost`; a halt emits one `loop.completed` carrying reason + observed tokens/cost; both breach sites call `make_budget_breach_args`.
  GREEN: extend the top-of-turn guard (`react.py:187-203`) with the token check; route both breaches through `make_budget_breach_args`; delete the inline payload dicts.
  Trace: REQ-001, REQ-003.

- [ ] **A4 — `make_budget_breach_args` wired or removed** `[arcrun.builtins.task_complete]`
  RED: the breach path produces `TaskCompleteArgs(status="failed", error="max_tokens"|"max_cost")`.
  GREEN: use the helper from A3; if any breach site still inlines, fix it; if the helper ends unused, delete it.
  Trace: REQ-003.

- [ ] **A5 — `provider_usage` fill at dispatch (trusted label)** `[arcagent.core.tool_registry]`
  RED: with a configured provider budget already exceeded, the next dispatch is DENIED by `ProviderLayer` (`provider.budget_exceeded`); `ProviderUsage.provider` equals the configured `model.model_name` even when `response.model` differs; with no run state, `provider_usage` is `None` and personal ALLOWs / ent-fed fails closed per SPEC-034.
  GREEN: pass `ctx.parent_state` from `arcrun_execute` into `wrapped_execute`; build `ProviderUsage(provider=trusted_label, tokens_used=rs.tokens_used["total"], cost_used=rs.cost_usd, requests_in_window=rs.tool_calls_made)`; set on `PolicyContext`.
  Trace: REQ-004, REQ-010.

---

## Phase B — Provider-label fail-closed (REQ-011) · [arctrust]

- [ ] **B1 — Unknown provider label fails closed** `[arctrust.policy]`
  RED: `ProviderLayer` with limits `{anthropic}` and `ctx.provider_usage.provider="bogus"` DENYs (`provider.unknown_label`) when not relaxable (ent/fed); ALLOWs + is auditable when relaxable (personal); a known-provider call is unaffected.
  GREEN: change the `limit is None` branch (`policy.py:570-572`) from ALLOW to relaxable-guarded DENY.
  Trace: REQ-011.

---

## Phase C — Classification binding + propagation (REQ-020..026) · [arctrust + arcagent + arcteam]

- [ ] **C1 — Canonical `Classification` ladder + `dominates()`** `[arctrust]`
  RED: `dominates(SECRET, CUI)` True, `dominates(CUI, SECRET)` False; `parse_classification("SECERT", strict=True)` raises; `strict=False` returns `UNCLASSIFIED` + warns; ordering `UNCLASSIFIED<CUI<CONFIDENTIAL<SECRET<TOP_SECRET`.
  GREEN: add the `IntEnum` + `dominates` + `parse_classification` (new `arctrust/classification.py` or in `policy.py`), mirroring `_ISOLATION_LADDER`.
  Trace: REQ-020, REQ-026.

- [ ] **C2 — arcteam imports the arctrust ladder; delete duplicate** `[arcteam.memory.types]`
  RED: `arcteam.memory` uses `arctrust.Classification`; `grep "class Classification"` in arcteam returns nothing; existing memory-classification tests still pass.
  GREEN: re-import from arctrust; delete `arcteam/memory/types.py` duplicate in the same edit.
  Trace: REQ-020.

- [ ] **C3 — `AgentIdentity.clearance`** `[arctrust.identity]`
  RED: `AgentIdentity(..., clearance=SECRET)` reports it; default is `UNCLASSIFIED`; existing constructions still valid (default).
  GREEN: add `clearance: Classification = UNCLASSIFIED`.
  Trace: REQ-021.

- [ ] **C4 — Clearance resolved from operator config** `[arcagent.core.config, agent_lifecycle]`
  RED: an agent built from config with `security.clearance="SECRET"` has `identity.clearance == SECRET`; no agent tool can raise it (config outside agent-writable paths).
  GREEN: add the config field; resolve at construction; bind onto identity.
  Trace: REQ-021.

- [ ] **C5 — Delegation clearance narrowing** `[arctrust.identity, arcagent.orchestration.spawn]`
  RED: a `SECRET` parent delegating a requested `TOP_SECRET` child yields child clearance `SECRET` (clamped, audited); a `CUI` parent cannot mint `SECRET`; at federal an over-request DENYs.
  GREEN: `derive_child_identity` takes `min(requested, parent.clearance)`; `spawn._execute` passes it; audit the clamp/deny. [P] with C6.
  Trace: REQ-022.

- [ ] **C6 — `ClassificationLayer` + `PolicyContext.clearance`** `[arctrust.policy]`
  RED: with `ClearanceContext(caller=CUI, resource=SECRET)` the layer DENYs (`classification.read_up`); `caller=SECRET` ALLOWs; `clearance=None` DENYs at ent/fed (`classification.state_missing`), ALLOWs at personal; unconfigured pipeline no-ops.
  GREEN: add `ClearanceContext` + `PolicyContext.clearance`; add `ClassificationLayer` (pure predicate); slot after `GlobalLayer` in `build_pipeline`; add `"classification"` to ent/fed `TierConfig.layer_names`. [P] with C5.
  Trace: REQ-023, REQ-026.

- [ ] **C7 — arcagent fills `ClearanceContext` at dispatch** `[arcagent.core.tool_registry, agent_lifecycle]`
  RED: dispatch sets `PolicyContext.clearance` with `caller_clearance` from identity and `resource_classification` from the per-tool config label (and/or touched memory entity); a `CUI` agent calling a `SECRET`-labeled tool is DENIED.
  GREEN: resolve per-tool resource classifications from config; build `ClearanceContext`; set on `PolicyContext`.
  Trace: REQ-023 (fill side).

- [ ] **C8 — Messenger no-write-down** `[arcteam.types, messenger]`
  RED: `Message` carries `classification`; `send` of a `SECRET` message to a `CUI` recipient/channel is refused (`message.classification_refused` + audit); equal-or-higher recipient receives; federal unknown recipient clearance → deny.
  GREEN: add `Message.classification`; add recipient/channel clearance resolution + `dominates()` gate in `MessagingService.send`.
  Trace: REQ-024, REQ-026.

- [ ] **C9 — Egress no-exfil** `[arcagent.tools._egress, agent_lifecycle]`
  RED: an egress of `SECRET` session data to an `UNCLASSIFIED` destination is refused (`egress.classification_refused` + audit); an allowlisted cleared destination proceeds; origin→clearance map read from config.
  GREEN: add destination-clearance lookup + session-data-classification param to `EgressProxy.request`; refuse on violation after the allowlist check.
  Trace: REQ-025.

---

## Phase D — Trifecta activation (REQ-030..031) · [arcagent modules]

- [ ] **D1 — Tag outbound comms tools** `[arcagent.modules.messaging, modules.telegram]`
  RED: `messaging_send` and Telegram `notify_user` derive the `external_comms` leg; inbound readers (`messaging_check_inbox`/`read_thread`) derive `untrusted_input`; a test asserts each tool's legs.
  GREEN: add `capability_tags` to the tool definitions. [P] across the two module files.
  Trace: REQ-030.

- [ ] **D2 — Fix the `browser` leg mapping** `[arcagent.core.session_internal.capability_ledger]`
  RED: the `browser` tool's tag derives a leg (previously `browser_navigate` produced none).
  GREEN: add the one map entry to `TAG_TO_LEGS` (or retag the tool).
  Trace: REQ-030.

- [ ] **D3 — Route outbound comms through `EgressProxy`** `[arcagent.modules.messaging, modules.telegram]`
  RED: a `messaging_send`/`notify_user` to a non-allowlisted destination is denied (`egress.denied`); an allowlisted send records the `external_comms` leg into the session ledger.
  GREEN: route the tools' outbound via the injected `EgressProxy` (`_runtime.egress()`); no raw socket.
  Trace: REQ-031.

---

## Phase E — Cross-cutting verification

- [ ] **E1 — Budget seam live (integration)** `[tests/integration]`
  A run that exceeds a configured provider token budget has its next tool dispatch DENIED by `ProviderLayer` — proving arcrun accounting → `provider_usage` → SPEC-034 decision end-to-end.

- [ ] **E2 — Trifecta activation E2E** `[tests/e2e]`
  Read a private file (turn 1) → ingest untrusted input (turn 2) → `messaging_send` (turn 3): the third call trips the SPEC-035 forbidden-composition gate and engages the human-gate — replacing SPEC-035's dormant state (no built-in emitted `external_comms`).

- [ ] **E3 — Bell-LaPadula E2E** `[tests/security]`
  A `SECRET` agent delegates a child clamped to `SECRET` (not higher); a `CUI` sub-agent is denied a `SECRET` tool (no-read-up); a `SECRET` message to a `CUI` channel is refused (no-write-down); `SECRET` data to an `UNCLASSIFIED` egress is refused (no-exfil). All decisions audited.

- [ ] **E4 — Budget-dodge negative test** `[tests/security]`
  A response reporting `model="bogus"` cannot redirect budget attribution (trusted label wins); an unknown provider label under configured limits fails closed at ent/fed.

- [ ] **E5 — Boundary + gates** `[tests/architecture + gates]`
  Import test: `arctrust` imports none of arcagent/arcllm/arcrun/arcteam/arcskill; `arcrun` imports no arcagent/arctrust. `ruff check`, `mypy --strict` (all five packages), full `pytest` with coverage ≥ thresholds; no new `# type: ignore` without justification; LOC budgets respected (move code, don't raise ceilings); no dead budget fields / duplicate ladder remain.

---

## Traceability matrix (REQ → task)

| REQ | Tasks |
|-----|-------|
| 001 | A3 |
| 002 | A1, A2 |
| 003 | A3, A4 |
| 004 | A5, E1 |
| 005 | A2 |
| 010 | A5, E4 |
| 011 | B1, E4 |
| 020 | C1, C2 |
| 021 | C3, C4 |
| 022 | C5, E3 |
| 023 | C6, C7, E3 |
| 024 | C8, E3 |
| 025 | C9, E3 |
| 026 | C1, C6, C8 |
| 030 | D1, D2, E2 |
| 031 | D3, E2 |

## Boundary guardrails (do not cross)

- arcrun tasks (A1–A4) add accounting + the breaker only — `tier`/budget are **parameters**; no `PolicyContext`, no arctrust import.
- arctrust tasks (B1, C1, C3, C6) add pure predicates + the ladder — **no sibling imports**; every layer reads injected state.
- arcagent tasks **wire**: `provider_usage`/`clearance` fill, delegation narrowing, egress classification, comms tagging. No budget-decision or ladder logic.
- arcteam tasks (C2, C8) *propagate* classification using the arctrust ladder — no ladder of its own.
- Every added/duplicated seam deletes its dead predecessor in the same edit (dead budget fields A1; arcteam duplicate ladder C2; inline breach dicts A3).
