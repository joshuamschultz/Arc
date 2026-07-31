# Product Requirements Document: DID Identity Unification

## Validation Checklist

- [x] All required sections are complete
- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Problem statement is specific and measurable
- [x] Context -> Problem -> Solution flow makes sense
- [x] All MoSCoW categories addressed
- [x] Every feature has testable acceptance criteria
- [x] No contradictions with arcteam or arcagent CLAUDE.md constraints

---

## Prior Work References

- **SPEC-006**: ArcTeam Messaging Subsystem (current URI-based identity)
- **Identity module**: `arcagent/core/identity.py` (DID generation, Ed25519)
- **Conversation context**: User explicitly requested: "why are we not using the did agent id's for messaging? lets stick to one id usage for everything."

---

## Product Overview

### Vision

Every entity in the ARC ecosystem — agents, users, services — is identified by a single DID (`did:arc:{org}:{type}/{hash}`). This DID is the canonical identifier for messaging, entity registration, audit trails, tool calls, and all inter-agent operations. Human-friendly aliases (`agent://brad_agent`) resolve to DIDs transparently.

### Problem Statement

Two independent identity systems exist:

1. **arcagent identity** (`identity.py`): `did:arc:{org}:{type}/{hash}` — Ed25519 keypair, cryptographically verifiable, used for signing and telemetry
2. **arcteam messaging** (`types.py`): `agent://{name}`, `user://{name}` — simple URI scheme, used for message addressing, entity registration, and stream routing

These systems are completely disconnected:
- An agent's DID (`did:arc:default:executor/a3b7c9e1`) has no relationship to its messaging URI (`agent://brad_agent`)
- Message senders cannot be cryptographically verified
- No way to prove a message came from a specific keypair
- Two separate "identities" to configure, manage, and reason about

### Value Proposition

- **One identity**: Eliminates dual-system confusion. DID is the identity.
- **Cryptographic attribution**: Messages are from a DID that maps to a keypair. Future message signing becomes trivial.
- **Compliance alignment**: NIST 800-53 IA-2 (identification) and IA-8 (non-organizational users) satisfied by DID-based identity.
- **Simpler mental model**: Developers and operators think about one identity, not two.
- **Future-proof**: DIDs are W3C standard. Interoperable with external identity systems.

---

## Target Users

| User Type | Impact |
|-----------|--------|
| ARC Agents | Identity fields change from URIs to DIDs |
| Human Operators | CLI commands accept name aliases that resolve to DIDs |
| System Administrators | Entity registry shows DID + name mapping |

---

## Feature Requirements

### Must Have (P0)

#### FR-1: DID as Entity ID

- **User Story**: As a developer, I want entities registered with their DID so there's one canonical identifier
- **Acceptance Criteria**:
  - Entity `id` field SHALL contain a DID (e.g., `did:arc:default:executor/a3b7c9e1`)
  - Entity `name` field SHALL contain the human-friendly name (e.g., `brad_agent`)
  - Entity registry SHALL support lookup by DID (primary key) AND by name (alias index)
  - `EntityRegistry.get(did)` SHALL return the entity
  - `EntityRegistry.get_by_name(name)` SHALL return the entity

#### FR-2: DID in Message Sender/To Fields

- **User Story**: As a messaging system, I want sender and recipient fields to use DIDs
- **Acceptance Criteria**:
  - `Message.sender` SHALL be a DID
  - `Message.to` entries SHALL be DIDs for agent/user targets
  - Channel (`channel://ops`) and role (`role://executor`) URIs SHALL remain URI-based (channels/roles are not DID entities)
  - `MessagingService.send()` SHALL validate that the sender DID is registered

#### FR-3: Alias Resolution

- **User Story**: As an operator, I want to use friendly names that automatically resolve to DIDs
- **Acceptance Criteria**:
  - System SHALL accept `agent://brad_agent` as an alias and resolve it to the entity's DID
  - Resolution SHALL occur at the messaging service layer (transparent to callers)
  - `parse_uri()` SHALL continue to work for `channel://` and `role://` targets
  - A new `resolve_identity(uri_or_did)` function SHALL handle DID passthrough and name→DID lookup

#### FR-4: DID in Stream Naming

- **User Story**: As a system designer, I want stream names derived from DID for consistency
- **Acceptance Criteria**:
  - Agent DM inbox streams SHALL use `arc.agent.{did_hash}` (e.g., `arc.agent.a3b7c9e1`)
  - `{did_hash}` is the hash portion of the DID (last segment after `/`)
  - Channel streams SHALL remain `arc.channel.{name}` (unchanged)
  - Role streams SHALL remain `arc.role.{name}` (unchanged)
  - `_stream_name_from_uri()` SHALL be replaced/extended with `_stream_name_from_identity()`

#### FR-5: MessagingModule Integration

- **User Story**: As the messaging module, I want to use the agent's DID from arcagent identity instead of constructing URIs
- **Acceptance Criteria**:
  - `MessagingConfig.entity_id` SHALL default to empty and be populated from `AgentIdentity.did` during startup
  - The messaging module SHALL receive the agent's DID from the module context (not construct `agent://{name}`)
  - `ModuleContext` SHALL expose the agent's DID (already available via `Agent._identity.did`)

### Should Have (P1)

#### FR-6: DID in Audit Trail

- **User Story**: As a compliance officer, I want audit records to use DIDs for actor identification
- **Acceptance Criteria**:
  - `AuditRecord.actor_id` SHALL contain the DID of the acting entity
  - Existing audit records with URI-based actor_ids SHALL remain valid (backward compatible)

#### FR-7: Migration of Existing Data

- **User Story**: As an operator, I want existing messaging data to continue working after the upgrade
- **Acceptance Criteria**:
  - System SHALL read existing streams at old paths (`arc.agent.{name}`) during a configurable migration period
  - New messages SHALL be written to DID-based paths
  - CLI SHALL provide a `migrate-identity` command to move data from URI-based to DID-based paths

### Could Have (P2)

#### FR-8: Message Signing

- **User Story**: As a security engineer, I want messages signed by the sender's Ed25519 key
- **Acceptance Criteria**:
  - `Message.meta.signature` SHALL contain Ed25519 signature of the message body
  - Recipients SHALL be able to verify the signature against the sender's DID public key
  - This builds naturally on DID identity — specified here but may be implemented in a future spec

### Won't Have (This Phase)

- W3C DID Document resolution
- External DID method support (only `did:arc` method)
- DID rotation/revocation protocol
- Federated DID exchange across teams

---

## Success Metrics

| Metric | Target | Tracking Method |
|--------|--------|-----------------|
| Single identity system | 0 URI-based entity IDs in new registrations | Code inspection |
| Alias resolution latency | < 1ms | Benchmark test |
| All tests pass with DID identity | 100% | pytest |
| Existing message streams readable | 100% backward compatibility | Integration test |

---

## Constraints

| ID | Constraint |
|----|-----------|
| CON-1 | arcteam core < 2,000 LOC |
| CON-2 | arcagent core < 3,500 LOC |
| CON-3 | Zero new dependencies |
| CON-4 | Backward compatible with existing SPEC-006 data |
| CON-5 | `mypy --strict` must pass |
| CON-6 | Channel and role URIs are NOT DIDs (they're not entities) |

---

## Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Breaking existing messaging data | High | Medium | Migration path + dual-read during transition |
| DID hash collisions in stream names | Low | Very Low | SHA-256 first 8 hex chars = 4 billion combinations |
| User confusion with DID vs name | Medium | Medium | Alias resolution makes it transparent |
| LOC budget impact | Medium | Low | Minimal new code — mostly refactoring existing |
