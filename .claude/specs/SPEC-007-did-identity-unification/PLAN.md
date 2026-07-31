# Implementation Plan: DID Identity Unification

**Spec**: SPEC-007
**Status**: PENDING
**Phases**: 4
**Estimated Tasks**: 16

---

## Phase 1: DID Support in arcteam/types.py [0/4]

Add DID parsing, validation, and utility functions alongside existing URI support.

- [ ] **1.1** Add `_DID_PATTERN`, `_ALIAS_PATTERN` regex patterns for DID and legacy alias detection
- [ ] **1.2** Implement `parse_did(did) -> (org, type, hash)`, `did_hash(did) -> str`, `is_did(value) -> bool`, `is_alias(value) -> bool`
- [ ] **1.3** Update `parse_uri()` to handle channel:// and role:// only; add `parse_uri_legacy()` for agent://user:// backward compat
- [ ] **1.4** Write unit tests: DID parsing (valid/invalid), alias detection, `did_hash` extraction, `parse_uri` still works for channels/roles

**Approval gate**: All types tests pass. No existing test breakage.

---

## Phase 2: Entity Registry DID Support [0/4]

Entity registry uses DID as primary key with name→DID index.

- [ ] **2.1** Add `get_by_name(name) -> Entity | None` to EntityRegistry — reads `names/{name}.json`
- [ ] **2.2** Add `resolve(identifier) -> Entity | None` — handles DID, alias URI, and bare name lookup
- [ ] **2.3** Update `register()` to write name→DID index (`names/{name}.json → {"did": "..."}`) alongside entity record
- [ ] **2.4** Write unit tests: register with DID, get_by_name, resolve DID/alias/name, duplicate name handling

**Approval gate**: Registry tests pass with DID-based entities.

---

## Phase 3: Messenger DID Routing [0/4]

MessagingService uses DIDs for sender/to fields and stream name derivation.

- [ ] **3.1** Replace `_stream_name_from_uri()` with `_stream_name_from_identity()` — DID→`arc.agent.{hash}`, channel/role unchanged
- [ ] **3.2** Update `send()` to validate sender is a DID, derive streams from DID hash for agent/user targets
- [ ] **3.3** Update `poll_all()` and `resolve_subscriptions()` to use DID-based stream names
- [ ] **3.4** Write unit/integration tests: send with DID sender/to, poll with DID entity, channel/role routing unchanged

**Approval gate**: All 165+ arcteam tests pass with DID-based messaging.

---

## Phase 4: arcagent Integration [0/4]

Wire arcagent's DID identity into the messaging module.

- [ ] **4.1** Add `identity: AgentIdentity | None` field to `ModuleContext` dataclass; pass identity during module startup in `Agent.startup()`
- [ ] **4.2** Update `MessagingModule.startup()` to use `ctx.identity.did` as entity_id instead of constructing `agent://{name}`
- [ ] **4.3** Update `_handle_send` in tools.py to resolve aliases (agent://name → DID) before sending; update `_handle_check_inbox` to use DID
- [ ] **4.4** Update arcagent messaging tests: tool tests use DID entities, module startup uses DID from identity, prompt injection shows DID

**Approval gate**: All 1166+ arcagent tests pass. Full round-trip: DID identity → register → send → poll → complete.

---

## Completion Criteria

- [ ] All arcteam tests pass (165+)
- [ ] All arcagent tests pass (1166+)
- [ ] `mypy --strict` passes on both packages
- [ ] `ruff check` clean on both packages
- [ ] No `agent://` or `user://` URIs in new entity registrations
- [ ] Existing stream data readable (backward compatibility)
- [ ] DID flows from arcagent identity → messaging module → arcteam service

---

## Out of Scope (Future)

- CLI `migrate-identity` command (P1 — can be separate PR)
- Message signing with Ed25519 (P2 — builds on this foundation)
- DID rotation/revocation
- W3C DID Document resolution
