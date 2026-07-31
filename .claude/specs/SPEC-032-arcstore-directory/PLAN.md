# PLAN — SPEC-032 ArcStore Team Directory + arcui Integration

**Status:** PENDING
**Method:** TDD. Each task scoped to **one module** (`[pkg]` tag). Cross-module use via SDD contracts only.

**Progress:** 0 / 16 complete

## Research Enrichment (from /deepen)
Five research streams enriched the SDD (§ Research Insights). Load-bearing refinements now baked into the tasks below:
- **Directory = authz-of-record** → every membership mutation flows through `PolicyPipeline` (requester's DID) + `arctrust.audit.emit`; **agents never write their own membership**.
- **Re-subscribe (D3)** = reconciliation loop: arcteam maintains a **derived NATS-KV mirror** of membership (SQLite = source of truth), daemon **KV-watches + local-diffs**; per-channel durable `agent-{did}-{channel}`, `fetch()`-loop task per channel, delete-on-leave, client-side dedup lock. Grants lazy / revocations eager.
- **arcstore (A1)** = mutable table + **trigger-fed companion mutation log** (not event-sourcing), soft-delete, WAL+`busy_timeout`+`BEGIN IMMEDIATE`, `user_version` migrations. Single-write-owner is the documented scaling path (not built now).
- **Migration (B3)** = shadow-write→verify→**revoke old NATS-KV write path**+audit marker; no dual-write; delete migration code same change.
- **Human signing (E2)** = **NOT UI-holds-key**: CLI signs locally (fix `_signer_for` as a kind-based `Signer` Protocol, fail-closed); arcui = **delegated forwarder (own DID, on_behalf_of=human)** or client-side signing, backend derives identity from token.
- **arcui (C1/C2)** = roster = directory ⊕ presence overlay (never persisted); reuse existing `TeamStreamHub` (drop-oldest+replay); server-side operator/viewer scope check per mutating call.

---

## Phase A — arcstore mutable plane `[arcstore]` (foundation)
- [ ] **A1** `[arcstore]` `MutableStore` (SQLite `mutable_records`, `ON CONFLICT DO UPDATE` + delete + get + list); separate from the immutable telemetry backend — REQ-001, REQ-002
- [ ] **A2** `[arcstore]` export `MutableStore` + open helper via `arcstore.__init__`; audit hook on write/delete — REQ-003

## Phase B — arcteam directory + composite backend `[arcteam]`
- [ ] **B1** `[arcteam]` `ArcStoreDirectoryBackend` implements the record-half of `StorageBackend` over `MutableStore` — REQ-010
- [ ] **B2** `[arcteam]` `CompositeBackend` (records→arcstore, streams→NATS); satisfies full Protocol, messenger unchanged — REQ-011
- [ ] **B3** `[arcteam]` route entity/team/channel storage through the directory; retire the NATS KV path for them — REQ-012, REQ-050

## Phase C — wire arcui to the directory + bus `[arccli]`/`[arcui]`
- [ ] **C1** `[arccli]` `arc ui start` builds `MessagingService` over the composite backend; passes `messaging_service` to `create_app` (channels + `/ws/team` live) — REQ-020
- [ ] **C2** `[arcui]` roster provider reads `EntityRegistry.list_entities` (directory) + liveness overlay; disk-scan fallback — REQ-021
- [ ] **C3** `[arcui]` boundary test: arcui still holds no directory-write logic (view+forward) — REQ-022

## Phase D — channel membership control
- [ ] **D1** `[arccli]` `arc team channel create|list|join|leave` over the directory; membership writes go through `PolicyPipeline` (requester DID) + audit — REQ-030, REQ-033
- [ ] **D2** `[arcui]` operator create-channel + add/remove membership via **operator-scope-checked** route → arcteam (not direct) — REQ-031
- [ ] **D3** `[arcteam]`/`[arcagent]` arcteam maintains a derived NATS-KV membership mirror; daemon **KV-watches + reconciles** (per-channel durable `agent-{did}-{channel}`, fetch-loop task, delete-on-leave, dedup lock) — no restart — REQ-032

## Phase E — human interaction
- [ ] **E1** `[arctrust]`/`[arcteam]` `Signer` Protocol resolved by identity **kind** (human backend reuses arctrust 0600/0700 enforcement); fix `_signer_for` (no-workspace users sign, **fail-closed** on missing key); CLI human-send signs locally — REQ-040
- [ ] **E2** `[arcui]`/`[arccli]` arcui composer via **delegated forwarder** (own DID, `on_behalf_of=human`, policy-authorized+audited) or client-side signing — **UI never holds the human's raw key**; backend derives identity from token — REQ-041
- [ ] **E3** `[arcui]`/`[arccli]` human DM + channel post from composer and CLI, rendered with handles — REQ-042

## Phase F — acceptance & gates
- [ ] **F1** Integration test: register agents → directory persists across a NATS restart; `arc ui start` shows roster + channels; human posts to a channel; agent (real daemon) receives + replies; membership add/remove changes delivery live
- [ ] **F2** Gates: `ruff` 0, `mypy --strict` 0 (all touched pkgs), suites green (arcui excl. pre-existing `test_chat_ws` hang); no NATS-KV dual-write for directory; LOC checked

---

## Sequencing
- **A before B** (backend needs the store); **B before C** (arcui needs the composite service); **C before D/E** (control + human build on the wired UI). D3 (arcagent re-subscribe) depends on B (directory as membership source).
- Cleanup (REQ-050) rides inline with B3/C2 — delete superseded NATS-KV/disk-only paths in the same edit.

## Definition of Done
Directory durable in arcstore (survives NATS restart); arcui shows agents/channels/fleet; human chats via channel+DM; agents monitor their channel subset with live membership changes; tests + `mypy --strict` + `ruff` green; audit on directory mutations; no dual source of truth.
