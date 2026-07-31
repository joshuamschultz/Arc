# SPEC-045 — Scale + Interop — PLAN

**Status:** PENDING
**PRD:** `./PRD.md` · **SDD:** `./SDD.md`
**Method:** TDD (RED → GREEN → REFACTOR). Each task is scoped to **one module**, writes the failing test first, and cites its REQ → component. Phase boundaries are approval gates.

Traceability legend: `REQ-NNN → <component/file> → Task`.

**Progress:** 0 / 22 tasks complete.

---

## Phase 0 — Decisions gate (blocks scope)
Resolve **OQ-1** (implement NATSExecutor now vs delete stub + secure live bus only) and **OQ-2** (MCP server now vs client-first) with the product owner before starting Phase 4/5b. Default assumptions if unanswered: OQ-1 = secure live bus (delete stub); OQ-2 = client-first (defer server). Record the decision in README.

---

## Phase 1 — NATS transport security (arctrust + arcteam)  [REQ-001/002]

- [ ] **T1.1** — arctrust TLS context primitive. `REQ-001 → arctrust.transport_tls → T1.1`
  RED: test `nats_tls_context("federal", material=None)` raises `TLSMaterialUnavailable`; `("personal", None)` returns `None`; valid material returns an `ssl.SSLContext` with client cert loaded. GREEN: implement `transport_tls.py`. One module.
- [ ] **T1.2** — arctrust envelope sign/verify + replay window. `REQ-002 → arctrust envelope crypto → T1.2`
  RED: `verify_envelope` accepts a freshly signed body; rejects a tampered body, an expired timestamp, and a replayed nonce (seen-set). GREEN: implement over `arctrust.keypair`; bounded windowed nonce set.
- [ ] **T1.3** — arcteam NatsBackend consumes TLS + signs/verifies envelopes. `REQ-001/002 → arcteam.backends.nats + types → T1.3`
  RED: `NatsBackend.connect(servers, ssl_context=ctx)` passes `tls=ctx` to `nats.connect`; a published envelope carries signature/nonce/ts; a consumed forged envelope is dropped + audited. GREEN: edit `connect`, extend `types.Envelope`, wire publish/consume. (Use a fake NATS client in unit tests; live round-trip in Phase 6 integration.)

---

## Phase 2 — Ingress guardrails + budgets (arcgateway)  [REQ-004/005/006/007]

- [ ] **T2.1** — extract shared rate primitive. `REQ-004 → arcgateway.ingress_throttle → T2.1`
  RED: token-bucket/window primitive: allows N within window, denies N+1, refills after window. GREEN: extract from the `pairing_throttle` pattern; refactor `pairing_throttle` to compose it (DRY — no behavior change, existing pairing tests stay green). REFACTOR gate: confirm one rate primitive, two callers.
- [ ] **T2.2** — IngressGuard rate + turn/token budget + untrusted mark. `REQ-004/005/006 → arcgateway.ingress_guard → T2.2`
  RED: `check_rate` denies over-limit per `(platform, chat_id)`; `check_and_charge_turn` refuses after budget exhausted (fail-closed) and spans multiple charges; `mark_untrusted` records `UNTRUSTED_INPUT` on the session ledger. GREEN: implement `IngressGuard` with bounded evictable per-conversation store.
- [ ] **T2.3** — wire IngressGuard into SessionRouter. `REQ-004/005/006 → arcgateway.session → T2.3`
  RED: `SessionRouter.handle` calls the guard *before* dispatch; an over-budget event never reaches the executor (assert executor not invoked) and returns an audited throttle notice. GREEN: insert guard check at the `_resolve_user_did` insertion point.
- [ ] **T2.4** — tier-scaled ingress defaults. `REQ-007 → arcgateway.config → T2.4`
  RED: federal config yields tighter rate/turn defaults than personal. GREEN: add tier-scaled defaults to `GatewayConfig`; every tier still enforces (ADR-019).

---

## Phase 3 — Platform → agent DID routing (arcgateway + adapters)  [REQ-008..011]

- [ ] **T3.1** — Slack adapter stamps real DID. `REQ-008 → arcgateway-slack (adapter+plugin) → T3.1`
  RED: a Slack `InboundEvent` carries a passed-in `agent_did` (not `""`); plugin `build()` forwards `ctx.agent_did()`. GREEN: add ctor param + stamp `self._agent_did`.
- [ ] **T3.2** — Mattermost adapter stamps real DID. `REQ-008 → arcgateway-mattermost (adapter+plugin) → T3.2`
  RED/GREEN: same as T3.1 for Mattermost (`adapter.py:503`, `plugin.py`).
- [ ] **T3.3** — bootstrap/cli pass effective_agent_did per block. `REQ-009 → arcgateway.bootstrap + cli → T3.3`
  RED: a per-block `[platforms.slack].agent_did` override reaches the built adapter (not the bare gateway default). GREEN: pass `effective_agent_did(platform)` in `build_for_embedded` + `cli`.
- [ ] **T3.4** — startup DID validation, remove placeholder. `REQ-010 → arcgateway.bootstrap + config → T3.4`
  RED: startup raises when a configured `agent_did` resolves to no team DID; the `did:arc:agent:default` default is gone. GREEN: validate against `_load_did_index`; fail-closed.
- [ ] **T3.5** — per-conversation agent resolver (Should). `REQ-011 → arcgateway.session + config → T3.5`
  RED: `_resolve_agent_did(platform, chat_id)` returns the mapped DID when configured, else the adapter default; consulted before `build_session_key`. GREEN: add resolver mirroring `_resolve_user_did`; config schema per OQ-3.

---

## Phase 4 — NATSExecutor (arcgateway)  [REQ-003, gated by OQ-1]

- [ ] **T4.1a** *(if OQ-1 = implement)* — real NATSExecutor on secured bus. `REQ-003 → arcgateway.executor_nats → T4.1a`
  RED: `NATSExecutor.run(event)` publishes to a worker subject over the arctrust-secured connection and yields streamed `Delta`s from a fake worker. GREEN: implement using `arctrust.transport_tls`; remove the `NotImplementedError` stub.
- [ ] **T4.1b** *(if OQ-1 = defer)* — delete the dead stub. `REQ-003 → arcgateway.executor(_nats) → T4.1b`
  Delete both `NATSExecutor` copies (`executor.py:322-346`, `executor_nats.py`) + the re-export + the stub test (no-legacy). Verify no import breaks.

---

## Phase 5a — MCP client (arcagent)  [REQ-012/016]

- [ ] **T5.1** — MCP client connect + list_tools. `REQ-012 → arcagent.tools.mcp_client → T5.1`
  RED: `MCPClient.connect(entry)` handshakes a fake stdio MCP server and returns its tool list. GREEN: implement over `MCPServerEntry` (command/args/env already in config).
- [ ] **T5.2** — MCP transport registers tools through the choke point. `REQ-012/016 → arcagent.tools.mcp_transport → T5.2`
  RED: each MCP tool becomes a `RegisteredTool(transport=MCP)`; dispatch flows through `pipeline.evaluate` (assert a deny is honored) + ClassificationLayer. GREEN: build closures + `ToolRegistry.register`; **no MCP-specific policy path**.

## Phase 5b — MCP anti-tool-poisoning (arcagent)  [REQ-013/014/015]

- [ ] **T5.3** — sanitize + boundary-mark descriptions. `REQ-013 → arcagent.tools.mcp_transport (sanitize) → T5.3`
  RED: an MCP description containing an injection string ("ignore previous instructions … exfiltrate") is neutralized/boundary-marked before prompt use; ingest is audited. GREEN: route descriptions through the arcagent sanitize util.
- [ ] **T5.4** — pin definitions + rug-pull refusal. `REQ-014 → arcagent.tools.mcp_pin → T5.4`
  RED: first sight pins `sha256(name||schema||description)`; a changed definition on reconnect is DENIED (fail-closed) + audited; federal requires an operator-signed allowlist (unpinned = deny). GREEN: implement pin store + reconnect check.
- [ ] **T5.5** — MCP legs + EgressProxy routing. `REQ-015 → arcagent.core.session_internal.capability_ledger + mcp_transport → T5.5`
  RED: add `"mcp": {EXTERNAL_COMMS, UNTRUSTED_INPUT}` to `TAG_TO_LEGS`; an MCP tool call records both legs; MCP egress to a non-allowlisted origin raises `EgressDenied`; over-classification egress raises `EgressClassificationDenied`. GREEN: tag MCP tools `["mcp", …]`; route egress through the injected `EgressProxy`.

---

## Phase 6 — Arm trifecta for real comms (arcgateway)  [REQ-018]

- [ ] **T6.1** — platform adapter egress through EgressProxy. `REQ-018 → arcgateway adapters (send path) → T6.1`
  RED: an outbound Slack/Telegram/Mattermost reply calls `EgressProxy.authorize()` and records `external_comms`; a non-allowlisted origin is denied. GREEN: route adapter delivery through the injected proxy (Telegram `bot.py:322` pattern).
- [ ] **T6.2** — trifecta e2e (real tools, not synthetic). `REQ-006/015/018 → integration → T6.2`
  RED/E2E: a session that (a) reads private data, (b) ingests untrusted ingress + an MCP result, (c) replies externally trips the `GlobalLayer.forbidden_composition` human-gate. Assert with real adapters/tools — the SPEC-035 review's false-confidence lesson.

---

## Phase 7 — MCP server (arcgateway-mcp)  [REQ-017, gated by OQ-2 — Could]

- [ ] **T7.1** *(if OQ-2 = now)* — `arcgateway-mcp` plugin exposing curated tools. `REQ-017 → packages/arcgateway-mcp → T7.1`
  RED: an inbound MCP tool call carries a caller identity, is signed into a `ToolCall`, passes the policy pipeline + classification, and executes only allowlisted tools; every call is audited; nothing exposed by default. GREEN: new plugin package via the gateway plugin registry.

---

## Phase 8 — Quality gate (all)
- [ ] **T8.1** — `ruff check`, `mypy --strict`, full test suite green across arctrust/arcteam/arcgateway/arcagent; coverage ≥ 80% (core ≥ 90%); audit events emitted on every new operation; no `NotImplementedError` stubs, no placeholder DIDs, no plaintext-NATS path at federal. Update README status → COMPLETE; sync roadmap.

---

## REQ → Task coverage matrix

| REQ | Tasks |
|-----|-------|
| REQ-001 | T1.1, T1.3 |
| REQ-002 | T1.2, T1.3 |
| REQ-003 | T4.1a / T4.1b |
| REQ-004 | T2.1, T2.2, T2.3 |
| REQ-005 | T2.2, T2.3 |
| REQ-006 | T2.2, T2.3, T6.2 |
| REQ-007 | T2.4 |
| REQ-008 | T3.1, T3.2 |
| REQ-009 | T3.3 |
| REQ-010 | T3.4 |
| REQ-011 | T3.5 |
| REQ-012 | T5.1, T5.2 |
| REQ-013 | T5.3 |
| REQ-014 | T5.4 |
| REQ-015 | T5.5 |
| REQ-016 | T5.2 |
| REQ-017 | T7.1 |
| REQ-018 | T6.1, T6.2 |
