# SPEC-034 — PLAN: Complete the arctrust PolicyPipeline

**Status:** PENDING
**Method:** TDD (RED → GREEN → REFACTOR). One module per task. Every task cites its REQ(s) and target file. `mypy --strict` + `ruff check` clean at each phase boundary; existing arctrust suite stays green throughout.

Legend: **[NEW]** new code · **[WIRE]** connect existing code · each task = one failing test first.

---

## Phase 0 — Contract: extend `PolicyContext` (foundation for all layers)

- [ ] **T-001 [NEW]** — Add `ProviderUsage`, `TeamScope`, `ToolRuntimeStatus` frozen Pydantic models and the three optional fields (`provider_usage`, `team_scope`, `tool_runtime`) to `PolicyContext` in `policy.py`. *(REQ-014, REQ-015)*
  - RED: test asserting the new models exist, are frozen, and that `PolicyContext(tier=..., policy_version=..., bundle_age_seconds=...)` still constructs with all three new fields defaulting to `None`.
  - GREEN: add models + fields.
  - Guard: run the **existing** `test_policy.py` + `test_identity_layer.py` unmodified — must stay green (REQ-014 acceptance).

---

## Phase 1 — ProviderLayer (LLM10)

- [ ] **T-101 [NEW]** — Add `ProviderLimit` model + `ProviderLayer.__init__(limits_by_provider, relaxable)`. *(REQ-003, REQ-004)*
  - RED: test that a `ProviderLayer` built with limits stores them and imports nothing from arcllm/arcagent.
- [ ] **T-102 [NEW]** — Budget-exceeded DENY. *(REQ-001)*
  - RED: context with `tokens_used`/`cost_used` ≥ limit → expect DENY `provider.budget_exceeded`, reason names limit + observed. GREEN: implement steps 1–3 of §3.2.
- [ ] **T-103 [NEW]** — Rate-exceeded DENY + under-limit ALLOW. *(REQ-002)*
  - RED: rate-breach context → DENY `provider.rate_exceeded`; under-limit → ALLOW. Assert no clock/I/O in `evaluate`.
- [ ] **T-104 [NEW]** — Tier relaxation + missing-state. *(REQ-004, REQ-005)*
  - RED: personal `relaxable=True` over-budget → ALLOW; enterprise/federal same numbers → DENY; `provider_usage=None` → DENY at federal, ALLOW at personal (`provider.state_missing`).

## Phase 2 — TeamLayer (ASI03 / ASI07)

- [ ] **T-201 [NEW]** — `TeamLayer.__init__(roles: dict[str, frozenset[str]])` + no-team ALLOW. *(REQ-008, REQ-009)*
  - RED: `team_scope=None` → ALLOW; layer imports no arcteam.
- [ ] **T-202 [NEW]** — Scope-violation DENY + in-scope ALLOW. *(REQ-006)*
  - RED: tool outside role scope → DENY `team.scope_violation` (reason names role+scope); in-scope → ALLOW. GREEN: §3.3 steps 1–3.
- [ ] **T-203 [NEW]** — Delegation-exceeded DENY + within-grant ALLOW. *(REQ-007)*
  - RED: `parent_call_id` set + tool outside `delegation_grant` → DENY `team.delegation_exceeded`; within grant → ALLOW. GREEN: §3.3 step 4.

## Phase 3 — SandboxLayer (ASI04 / ASI05) — thin

- [ ] **T-301 [NEW]** — `_isolation_satisfies` helper over the SPEC-036 ladder (host<container<vm). *(REQ-011, REQ-012)*
  - RED: table test of ladder comparisons. Assert no verify/exec/import of arcskill/SPEC-036.
- [ ] **T-302 [NEW]** — Unverified-tool DENY + verified ALLOW + missing-state. *(REQ-010, REQ-013)*
  - RED: `tool_runtime.verified=False` → DENY `sandbox.unverified_tool`; verified+satisfiable → ALLOW; `tool_runtime=None` → DENY at federal / ALLOW at personal (`sandbox.state_missing`).
- [ ] **T-303 [NEW]** — Isolation-unsatisfiable DENY. *(REQ-011)*
  - RED: federal required=vm, available=host → DENY `sandbox.isolation_unsatisfiable`; satisfiable → ALLOW. Confirm layer stays < ~30 LOC (REQ-013 acceptance).

## Phase 4 — WORM audit adapter + emission locks

- [ ] **T-401 [NEW]** — `worm_policy_sink(sink)` adapter in `audit.py`. *(REQ-017)*
  - RED: feed a `(event_type, payload)` call → assert an `AuditEvent` with `action="policy.evaluate"`, matching `outcome`, `payload_hash=input_hash`, `extra` carrying `layer/rule_id`, and **no** raw `arguments`; written via `emit` to a `WormSink`; `verify_chain()` passes. GREEN: §3.5.
- [ ] **T-402 [WIRE/test-only]** — Lock the single-emission invariant. *(REQ-016)*
  - RED: pipeline with a counting sink over mixed ALLOW/DENY calls → exactly one write per `evaluate`. (Behavior exists at `policy.py:572`; test pins it.)
- [ ] **T-403 [WIRE/test-only]** — Lock fail-open-on-audit. *(REQ-018)*
  - RED: throwing sink → `evaluate` does not raise and returns the unchanged decision. (Behavior at `policy.py:600-603`.)
- [ ] **T-404 [NEW]** — Payload sufficiency. *(REQ-019)*
  - RED: emitted payload contains `tier`, `layer`, `rule_id`, `input_hash`, `classification`; absent `arguments`.

## Phase 5 — Factory + tier assembly

- [ ] **T-501 [WIRE]** — Extend `build_pipeline` to accept `provider_limits`, `team_roles` (default empty → today's behavior) and pass them into `ProviderLayer`/`TeamLayer`; keep the tier matrix (`policy.py:664-682`) unchanged. *(REQ-004, REQ-008)*
  - RED: `build_pipeline(tier="federal", provider_limits=..., team_roles=...)` → the constructed layers carry the config; personal still gets `[identity, global]` only.
- [ ] **T-502 [NEW]** — Full-pipeline first-DENY-wins across the three now-real layers. *(REQ-001, REQ-006, REQ-010, REQ-016)*
  - RED: enterprise/federal pipeline + `WormSink`; a call breaching Provider stops before Sandbox; one chain entry per evaluation for a mixed batch; `verify_chain()` passes.

## Phase 6 — arcagent wiring + boundary

- [ ] **T-601 [WIRE]** — In `agent.py:162`, construct a `WormSink` (path from `arcagent.toml` security/policy config, `0600`) and pass `audit_sink=worm_policy_sink(worm)` into `build_pipeline`; pass provider/team config from config. *(REQ-017)*
  - RED (arcagent integration): build an agent, deny a dispatch, assert the WORM chain has a verifiable `policy.evaluate` record for it.
- [ ] **T-602 [WIRE]** — At `tool_registry.py:325`, populate the new `PolicyContext` fields from whatever producers exist (all `None` until SPEC-038/036/033 fill them); assert dispatch still works and fail-closed/ALLOW-personal rules hold. *(REQ-005, REQ-015)*
- [ ] **T-603 [test-only]** — Import-boundary test: `arctrust` imports none of `arcagent`/`arcllm`/`arcrun`/`arcteam`/`arcskill`. *(REQ-003, REQ-008, REQ-012)*
  - RED: static import scan over `arctrust/src` → zero sibling imports.

## Phase 7 — Gates

- [ ] **T-701** — `mypy --strict` on arctrust + arcagent: 0 errors. `ruff check`: 0. Full arctrust + affected arcagent suites green. Coverage of `policy.py` new code ≥ 90% (core-component floor). Bump arctrust minor version + CHANGELOG.

---

## Traceability matrix (REQ → component → task)

| REQ | Component | Task(s) |
|-----|-----------|---------|
| 001 | ProviderLayer budget | T-102, T-502 |
| 002 | ProviderLayer rate | T-103 |
| 003 | ProviderLayer boundary | T-101, T-603 |
| 004 | ProviderLayer tiering | T-104, T-501 |
| 005 | ProviderLayer missing-state | T-104, T-602 |
| 006 | TeamLayer scope | T-202, T-502 |
| 007 | TeamLayer delegation | T-203 |
| 008 | TeamLayer boundary/config | T-201, T-501, T-603 |
| 009 | TeamLayer no-team ALLOW | T-201 |
| 010 | SandboxLayer unverified | T-302, T-502 |
| 011 | SandboxLayer isolation | T-301, T-303 |
| 012 | SandboxLayer boundary | T-301, T-603 |
| 013 | SandboxLayer thinness | T-302, T-303 |
| 014 | PolicyContext compat | T-001 |
| 015 | PolicyContext schema | T-001, T-602 |
| 016 | Single emission | T-402, T-502 |
| 017 | WORM adapter + wiring | T-401, T-601 |
| 018 | Fail-open-on-audit | T-403 |
| 019 | Payload sufficiency | T-404 |

## Definition of done

- All 14 Must + 3 Should + 1 Could REQs have a green test.
- Three stub layers contain zero unconditional `return Decision.allow(...)`.
- Production `build_pipeline` (agent.py) passes a WORM-backed `audit_sink`; a denied dispatch yields a `verify_chain()`-passing record.
- arctrust import boundary proven clean (T-603).
- `mypy --strict` + `ruff` clean; arctrust core-component coverage ≥ 90%; CHANGELOG + version bump.
