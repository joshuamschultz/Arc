# Product Requirements Document: ArcTeam Messaging Subsystem

## Validation Checklist

- [x] All required sections are complete
- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Problem statement is specific and measurable
- [x] Context -> Problem -> Solution flow makes sense
- [x] All MoSCoW categories addressed
- [x] Every feature has testable acceptance criteria (EARS format)
- [x] Every metric has corresponding tracking method
- [x] No contradictions with arcteam CLAUDE.md constraints
- [x] A new team member could understand this PRD

---

## Prior Work References

- **Brainstorm (deepened)**: `.claude/brainstorms/2026-02-17-arcteam-messaging.md`
- **Full ARC Team PRD**: `packages/arcteam/.claude/ARC-Team-PRD-Spec-Architecture.md` (Sections 3-4, 8-9)
- **Competitive Research**: AgentWorkforce/relay, OpenClaw, Google A2A, AutoGen, CrewAI, LangGraph, MetaGPT, CAMEL

---

## Product Overview

### Vision

Enable thousands of autonomous ARC agents to communicate asynchronously through persistent, pull-based message streams — like Slack for agents — with durable delivery, structured envelopes, and federal-grade audit trails.

### Problem Statement

Individual ARC agents operate effectively in isolation but cannot communicate. There is no mechanism for agents to send messages, receive notifications, coordinate via channels, or broadcast to role groups. Users must manually transfer context between agents. Without messaging, the task engine, knowledge base, and file store (future ArcTeam subsystems) have no coordination backbone.

### Value Proposition

- **Async-native**: Messages persist in streams. Agents read at their own pace. No agent needs to be alive to "receive" a message.
- **Pull-based**: Agents control their read rate via cursors. No back-pressure issues. Maps directly to NATS JetStream for Phase 4.
- **Standalone**: Zero arcagent dependency. Any Python program can `from arcteam.messenger import MessagingService`.
- **Federal-ready**: NIST 800-53 audit trail with chained HMACs. RBAC. Classification-aware. Air-gap capable.
- **Zero external dependencies**: Python 3.12 stdlib only (Phase 1). No databases, no brokers, no infrastructure.

---

## Target Users

| User Type | Primary Interactions |
|-----------|---------------------|
| ARC Agents | Send/receive messages, poll channels, check inbox, advance cursors |
| Human Operators | Send messages via CLI, monitor channels, manage entities/roles |
| System Administrators | Configure ACLs, review audit logs, manage entity registry |

---

## User Journey

### Agent Wake-Up Flow

1. **Trigger**: Agent wakes (cron, event, or manual invocation)
2. **Poll**: Agent calls `poll()` on each subscribed stream (inbox, channels, roles), reading from cursor forward
3. **Triage**: Agent sorts unread messages by priority (`critical` > `high` > `normal` > `low`), then by `action_required`
4. **Process**: For each message, agent takes appropriate action (reply, create task, update KB)
5. **Advance cursor**: After successful processing, cursor advances. Crash-safe — if agent dies mid-processing, it re-reads on next wake-up.
6. **Sleep**: Agent enters idle or shuts down

### Human CLI Flow

1. Human runs `arc-team send --to channel://project-alpha --body "Prioritize vendor analysis" --type task --priority high --action`
2. Message is appended to `streams/arc.channel.project-alpha/00000000.log`
3. Audit record written to `audit/00000000.log`
4. All agents subscribed to `channel://project-alpha` see it on next poll

---

## Feature Requirements

### Must Have (P0)

#### FR-1: StorageBackend Protocol + FileBackend

- **User Story**: As a developer, I want a swappable storage layer so messaging works on files today and databases later
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL expose a `StorageBackend` Protocol with methods: `read`, `write`, `delete`, `append`, `read_stream`, `query`, `list_keys`, `exists`
  - [x] System SHALL provide a `FileBackend` implementation using JSON files for records and JSONL for streams
  - [x] WHEN writing a record THEN system SHALL write to a `.tmp` file and atomically `os.replace()` into final path
  - [x] WHEN appending to a stream THEN system SHALL use `fcntl.flock(LOCK_EX)` for write serialization
  - [x] WHEN reading a stream THEN system SHALL NOT acquire any lock (concurrent reads allowed)

#### FR-2: Audit Logger

- **User Story**: As a compliance officer, I want every write operation logged with tamper-evidence so audit trails satisfy NIST 800-53
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL write audit records to a separate `audit/00000000.log` stream
  - [x] EACH audit record SHALL contain: `audit_seq`, `event_type`, `stream`, `msg_seq`, `subject`, `actor_id`, `target_id`, `classification`, `timestamp_utc`, `detail`, `hmac_sha256`
  - [x] EACH audit record's HMAC SHALL chain to the previous record's HMAC (tamper-evidence)
  - [x] System SHALL NEVER delete, modify, or compact the audit stream through normal operations
  - [x] WHEN a gap exists in `audit_seq` THEN system SHALL detect it as evidence of tampering

#### FR-3: Entity Registry

- **User Story**: As an operator, I want to register agents and users with roles so messaging can route by identity and role
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL store entity records at `registry/{entity_id}.json` with: `id`, `name`, `type` (agent|user), `roles`, `capabilities`, `created`, `status`
  - [x] WHEN querying by role THEN system SHALL return all entities with that role in their `roles` list
  - [x] System SHALL support entity types: `agent` and `user`
  - [x] System SHALL reject duplicate entity IDs on registration

#### FR-4: MessagingService — Send

- **User Story**: As an agent, I want to send structured messages to other agents, channels, or roles
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL accept a message with 15 fields: `seq`, `id`, `ts`, `sender`, `to`, `reply_to`, `thread_id`, `msg_type`, `priority`, `action_required`, `subject`, `body`, `refs`, `status`, `meta`
  - [x] WHEN `to` contains a `channel://` URI THEN system SHALL append the message to `streams/arc.channel.{name}/00000000.log`
  - [x] WHEN `to` contains a `role://` URI THEN system SHALL append the message to `streams/arc.role.{role}/00000000.log`
  - [x] WHEN `to` contains an `agent://` or `user://` URI THEN system SHALL append the message to `streams/arc.agent.{id}/00000000.log` (DM inbox)
  - [x] System SHALL auto-assign monotonic `seq` per stream
  - [x] System SHALL auto-generate `id` as `msg_{timestamp}_{hash}`
  - [x] System SHALL auto-set `thread_id` to own `id` if `reply_to` is null (new thread), or inherit from replied message
  - [x] WHEN message body exceeds 64KB THEN system SHALL reject with validation error
  - [x] System SHALL write an audit record for every send operation

#### FR-5: MessagingService — Poll

- **User Story**: As an agent, I want to pull unread messages from my subscribed streams since my last cursor position
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL accept a `poll(stream, entity_id, max_messages)` call
  - [x] System SHALL read the entity's cursor file to determine resume position (`seq` + `byte_pos`)
  - [x] System SHALL return up to `max_messages` messages from the stream starting after the cursor's `seq`
  - [x] System SHALL use `byte_pos` for fast file seeking when available
  - [x] IF no cursor exists THEN system SHALL start from the beginning of the stream (seq=0)

#### FR-6: MessagingService — Cursor Advance

- **User Story**: As an agent, I want to advance my cursor after successfully processing a message so I don't re-read it
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL accept an `ack(stream, entity_id, seq, byte_pos)` call
  - [x] System SHALL write the cursor atomically (write-to-temp + `os.rename()`)
  - [x] Cursor file SHALL contain: `consumer`, `stream`, `seq`, `byte_pos`, `updated_at`
  - [x] System SHALL only advance cursor forward (reject `seq` < current cursor seq)

#### FR-7: Channel Management

- **User Story**: As an operator, I want to create channels and manage membership so agents can communicate in topic-specific streams
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL store channel definitions at `channels/{name}.json` with: `name`, `description`, `members` (list of URIs), `created`
  - [x] WHEN an entity joins a channel THEN system SHALL add them to the members list
  - [x] WHEN sending to a channel THEN system SHALL verify the sender is a member (unless sender has admin role)
  - [x] System SHALL create the stream directory (`streams/arc.channel.{name}/`) on channel creation

#### FR-8: CLI Commands

- **User Story**: As an operator, I want to manage messaging from the command line
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL provide `arc-team register` to register entities with `--roles` and `--name`
  - [x] System SHALL provide `arc-team entities` to list registered entities with optional `--role` filter
  - [x] System SHALL provide `arc-team channel` to create channels with `--members` and `--description`
  - [x] System SHALL provide `arc-team join` to add entities to channels
  - [x] System SHALL provide `arc-team send` with `--to`, `--body`, `--subject`, `--type`, `--priority`, `--action`, `--refs`, `--reply-to`
  - [x] System SHALL provide `arc-team inbox` to poll an entity's DM stream and subscribed channels
  - [x] System SHALL provide `arc-team read` to read channel/DM history with `--limit`
  - [x] System SHALL provide `arc-team thread` to view a message thread by thread_id
  - [x] All CLI commands SHALL accept `--root` (data directory) and `--as` (entity identity)

### Should Have (P1)

#### FR-9: Dead Letter Queue

- **User Story**: As an operator, I want failed message deliveries tracked so I can diagnose and recover
- **Acceptance Criteria** (EARS format):
  - [x] WHEN a message fails delivery (invalid stream, validation error, write failure) THEN system SHALL append it to `dlq/00000000.log` with `meta.dlq_reason`
  - [x] System SHALL provide `arc-team dlq` CLI command to list DLQ entries
  - [x] DLQ reasons SHALL include: `validation_error`, `stream_not_found`, `write_failed`, `body_too_large`, `sender_unauthorized`

#### FR-10: Role-Based Addressing

- **User Story**: As an agent, I want to broadcast to all agents with a specific role without knowing their IDs
- **Acceptance Criteria** (EARS format):
  - [x] WHEN message targets `role://procurement` THEN system SHALL append to `streams/arc.role.procurement/00000000.log`
  - [x] WHEN an entity with `procurement` role polls THEN system SHALL include `arc.role.procurement` stream in their subscribed streams
  - [x] System SHALL auto-subscribe entities to role streams matching their registered roles

#### FR-11: Stale Cursor Cleanup

- **User Story**: As an administrator, I want crashed agent cursors cleaned up automatically
- **Acceptance Criteria** (EARS format):
  - [x] Cursor files SHALL include `updated_at` timestamp
  - [x] System SHALL provide a cleanup mechanism that removes cursor files older than `InactiveThreshold` (default 24 hours)

### Could Have (P2)

#### FR-12: Message Status Tracking

- Track per-recipient message status (`delivered` → `read` → `acted`)

#### FR-13: Action Required Tracking

- Separate view of `action_required=true` messages that haven't been marked `acted`

#### FR-14: Stream Meta Configuration

- Per-stream `meta.json` with retention policy, max_bytes, max_age

### Won't Have (This Phase)

- SQLite/Postgres backends (Phase 2-4)
- Real-time push notifications (Phase 3)
- Cross-team federation (Phase 4)
- ARC Agent plugin (separate spec)
- Task engine integration (separate spec)
- Shadow agent pattern (future)
- Ping-pong negotiation protocol (future)
- A2A protocol bridge (Phase 4)

---

## Success Metrics

| Metric | Target | Tracking Method |
|--------|--------|-----------------|
| Message append latency (local file) | < 5ms p99 | Benchmark test |
| Stream poll latency (100 messages) | < 50ms p99 | Benchmark test |
| Cursor advance latency | < 1ms p99 | Benchmark test |
| Concurrent stream readers | 50+ without degradation | Load test |
| Audit trail completeness | 100% of write operations logged | Integration test |
| Core LOC | < 2,000 (arcteam budget) | `wc -l` |

---

## Constraints

### From ArcTeam CLAUDE.md

| ID | Constraint |
|----|-----------|
| CON-1 | Core < 2,000 LOC |
| CON-2 | Python 3.12+ stdlib only (Phase 1) |
| CON-3 | `mypy --strict` must pass |
| CON-4 | Pydantic 2.x for all data boundaries |
| CON-5 | Every action is an audit event |
| CON-6 | Ed25519 signing for inter-agent comms |

### Feature-Specific

| ID | Constraint |
|----|-----------|
| CON-F1 | Message body max 64KB |
| CON-F2 | Append order is truth (not timestamp) |
| CON-F3 | Cursors advance forward only |
| CON-F4 | Audit stream never compacted/deleted |
| CON-F5 | NATS-compatible subject naming (`arc.{type}.{name}`) |
| CON-F6 | Atomic file writes via `os.replace()` |
| CON-F7 | Stream write serialization via `fcntl.flock()` |
| CON-F8 | Zero arcagent dependency |

---

## Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| File locking contention under high write load | Medium | Low (Phase 1 scale) | Single writer per stream enforced; NATS migration path eliminates this |
| JSONL file corruption from partial writes | High | Low | Atomic append with flock; readers skip incomplete trailing lines |
| Cursor file loss on filesystem failure | Medium | Low | Cursor only advances after processing; agent re-reads from last known position |
| LOC budget exceeded | Medium | Medium | Storage abstraction + audit are shared across all subsystems, amortizing LOC |
| Stale cursors from crashed agents | Low | Medium | InactiveThreshold cleanup (24h default) |

---

## Glossary

| Term | Definition |
|------|-----------|
| **Stream** | Append-only JSONL file containing messages. One stream per channel, role, or agent DM inbox. |
| **Cursor** | Per-entity JSON file tracking read position (seq + byte_pos) in a stream |
| **seq** | Monotonic per-stream sequence number. Primary cursor unit. Maps to NATS stream sequence. |
| **Poll** | Agent pulling unread messages from a stream starting after its cursor position |
| **DLQ** | Dead Letter Queue — stream of failed deliveries with reason tracking |
| **Chained HMAC** | Tamper-evidence scheme where each audit record's HMAC includes the previous record's HMAC |
