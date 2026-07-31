# PRD — SPEC-031 ArcTeam Refresh

**Requirements format:** EARS. Each requirement is tagged with its governing **pillar** (Simplicity / Modularity / Security / Scalability) and carries an acceptance criterion tied to that pillar. IDs are monotonic `REQ-NNN`. Priority via MoSCoW.

---

## Goal & non-goals

**Goal:** first-class, plug-and-play communication so agents (and humans) coordinate as a team over one signed bus.

**Non-goals (this spec):** external Slack/Teams/Telegram bridging (seam only), consensus/voting/coordinator task-allocation, the goal/target/relevance-triage system (separate build; a `goal_ref` pointer is the only hook).

---

## Requirements

### Identity & naming — *Must*

- **REQ-001** *(Security)* The system SHALL assign every entity (agent and human) a cryptographic DID sourced from `arctrust` and a unique display `@handle` at registration.
  - *Accept:* a created agent has a resolvable `did:` and handle; no entity exists without a DID (fail-closed).
- **REQ-002** *(Simplicity)* The system SHALL resolve `@handle → DID → stream` through one resolution path for all addressing, and SHALL reject an unknown handle with a typed `UnknownHandle` error, not a silent DLQ.
  - *Accept:* one `resolve()` function is the sole resolver; sending to `@ghost` raises `UnknownHandle`.
- **REQ-003** *(Modularity)* The system SHALL unify `arc agent create` and `arc team register` on one DID-keyed identity so an auto-registered agent is immediately addressable.
  - *Accept:* the current `sender_unauthorized` DLQ bug is gone; an integration test sends to a freshly-created agent and it arrives.
- **REQ-004** *(Simplicity)* WHEN a message body or `to` contains `@handle`, the system SHALL record a Mention for that DID and set attention flags (`action_required=true`, min `priority=high`).
  - *Accept:* `"hi @builder"` yields `mentions=[builder_did]` and raised attention.

### Team & roster — *Must*

- **REQ-010** *(Modularity)* The system SHALL provide a first-class `Team` (id, name, members, default channel, optional `goal_ref`) persisted and audited, owned solely by `arcteam`.
  - *Accept:* `Team` lives in `arcteam`; no other module defines a team concept.
- **REQ-011** *(Simplicity)* The system SHALL provide a `Roster` snapshot of a team: members, handles, live status, capabilities.
  - *Accept:* `roster(team)` returns one flat list readable cold.
- **REQ-012** *(Modularity)* The `arc team` CLI SHALL expose the full lifecycle (`create`, `add/remove member`, `up`, `down`, `status`, `send`, `inbox`, `read`, `thread`) as the single team CLI.
  - *Accept:* the standalone `arcteam` messaging CLI is removed; one CLI remains.

### Transport — *Must*

- **REQ-020** *(Scalability)* The system SHALL use NATS JetStream as the substrate with subjects `arc.agent.{did}`, `arc.channel.{name}`, `arc.role.{role}`.
  - *Accept:* messages flow over JetStream; a load test sustains ≥10k msg/min across ≥10 agents.
- **REQ-021** *(Scalability)* Each entity SHALL consume its subjects via a **durable consumer** so a running entity is pushed live AND a restarted entity resumes from its last ack.
  - *Accept:* kill+restart an agent mid-stream; it resumes with zero missed/duplicated messages.
- **REQ-022** *(Modularity)* The `NatsBackend` SHALL implement the existing `StorageBackend` Protocol; `MemoryBackend` remains for tests.
  - *Accept:* messenger API is unchanged above the backend; swapping backends needs no messenger edit.

### Signing & replay — *Must*

- **REQ-030** *(Security)* The system SHALL Ed25519-sign every message with the sender's `arctrust` keypair and verify before delivery; failure → DLQ `bad_signature`.
  - *Accept:* a tampered body fails verification and is quarantined, not delivered.
- **REQ-031** *(Security)* Each message SHALL carry `nonce` + `ts`; the system SHALL reject replays within the window → DLQ `replay`.
  - *Accept:* re-submitting a captured message is rejected.

### Mid-task delivery (steering) — *Must*

- **REQ-040** *(Simplicity)* WHEN a message arrives for an agent mid-task, the system SHALL inject it via `arcrun.follow_up` at the next turn boundary by default.
  - *Accept:* a teammate message is absorbed at turn end without corrupting the in-flight turn.
- **REQ-041** *(Security)* WHEN a message is `priority=critical` OR a Mention with `action_required`, the system MAY inject via `steer` (mid-turn) ONLY IF the `arctrust` policy pipeline permits it for that `caller_did`.
  - *Accept:* a non-permitted caller's steer is denied and audited; a permitted critical mention interrupts.
- **REQ-042** *(Security)* `arcrun` `steer`/`follow_up` SHALL require a verified `caller_did` and SHALL emit an audit event (`steer.injected`/`followup.injected`) at each injection.
  - *Accept:* every injection appears in the hash-chained audit with an attributable DID.

### Autonomy & orchestration — *Must*

- **REQ-050** *(Simplicity)* A team-member agent SHALL run its autonomous inbox loop by default when served, without manual `[modules.messaging]` config.
  - *Accept:* `arc agent create` for a team member yields an agent that reacts to teammates out of the box.
- **REQ-051** *(Scalability)* `arc team up <team>` SHALL boot all members as supervised, reconnecting daemons on the bus; `arc team down` SHALL stop them gracefully.
  - *Accept:* `up` then `down` leaves no orphan processes; a killed member is restarted by the supervisor.

### UI as view — *Must*

- **REQ-060** *(Modularity)* arcui SHALL be a pure view + input-forwarding client with **zero messaging/coordination logic** — it renders flows and forwards human input, never routes/handles/executes/coordinates.
  - *Accept:* a boundary test asserts arcui contains no send/route/sign logic.
- **REQ-061** *(Modularity)* Direct human↔single-agent messaging SHALL remain an `arcagent` concern (arccli + existing 1:1 view); team/group coordination SHALL be an `arcteam` concern; arcui forwards a human post to the correct owner.
  - *Accept:* a group post routes through arcteam; a 1:1 post routes through arcagent.
- **REQ-062** *(Simplicity)* arcui SHALL subscribe to the arcteam bus read-only to stream team flows live, display handles (never DIDs), render mentions distinctly, and remove the 5-second DB poll.
  - *Accept:* team messages appear without the 5s poll; DIDs never render.
- **REQ-063** *(Simplicity)* Any existing arcui-side messaging/coordination handling beyond view+forward SHALL be removed.
  - *Accept:* the leftover patch is deleted; boundary test green.

### Cleanup — *Should*

- **REQ-070** *(Simplicity)* The system SHALL remove superseded/dead code: `FileBackend`, redundant cursor bookkeeping, `arcagent/modules/{slack,telegram}`, the dual CLI, the dead `emit_team_event→UIReporter` stub — with no compat shims.
  - *Accept:* named symbols are gone; `ruff`/`mypy --strict` clean; no `_deprecated`/compat stubs added.

---

## MoSCoW summary

- **Must:** REQ-001…063 (identity, team, transport, signing, steering, autonomy, UI view).
- **Should:** REQ-070 (cleanup — done inline with the edits that supersede each item, per "delete in the same edit").
- **Won't (this spec):** external gateway bridging, consensus/coordinator, goal system.

## Traceability

Every REQ maps to an SDD component and ≥1 PLAN task; PLAN tasks are scoped to a single module (Modularity pillar).
