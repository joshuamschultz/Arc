---
topic: ArcTeam Messaging Subsystem — Gap Analysis
date: 2026-02-17
status: deepened
deepened: 2026-02-17
prior_work: .claude/brainstorms/2026-02-16-agent-messaging.md (Telegram, different scope)
prd_ref: packages/arcteam/.claude/ARC-Team-PRD-Spec-Architecture.md (Section 4)
---

# ArcTeam Messaging — Gap Analysis Brainstorm

## Context

The ARC Team PRD (Section 4) defines a comprehensive messaging subsystem. This brainstorm explores **gaps and decisions not covered** by the PRD, surfaced during collaborative dialogue before implementation.

The PRD covers WHAT well. This document captures decisions about HOW that the PRD left open.

---

## Mental Model

**Slack for agents.** Not email-for-agents (as the PRD initially framed it). Key mapping:

| Slack Concept | ArcTeam Equivalent |
|---------------|-------------------|
| Workspace | Team |
| Channel | `channel://name` stream |
| DM | Per-entity inbox stream |
| User Group | `role://name` (broadcast) |
| Message | Structured envelope (14-field schema) |
| Read position | Per-entity cursor file |

---

## Decisions Made (Gaps Filled)

### 1. Delivery Guarantee

**Decision:** Deliver once, retry on failure, audit everything.

- At-least-once semantics with idempotent processing
- Message ID (`msg_{timestamp}_{hash}`) serves as natural dedup key
- Every delivery attempt (success or failure) logged to audit trail
- Consuming agents should handle seeing the same message_id twice gracefully

### 2. Message Routing: Pull Model (Not Fanout)

**Decision:** Messages stay in streams. Agents pull from streams they belong to.

**PRD said:** Fan out to per-entity inboxes.
**We decided:** No fanout. Single write to the stream, agents check streams they're subscribed to.

Why:
- Single source of truth per stream (no duplication)
- Simpler write path (one write per message, regardless of recipient count)
- Acts like Slack channels — different histories and contexts
- The ArcAgent plugin needs to get agents to check channels/roles they're on

### 3. DM vs. Channel Split

**Decision:** Two workspaces, two patterns.

| Type | Location | Who checks |
|------|----------|-----------|
| DMs | Agent's personal inbox stream | Only that agent |
| Channels | Team shared workspace | All channel members |
| Roles | Team shared workspace (role streams) | All agents with that role |

- DMs go to a per-entity stream (like a personal inbox) in the agent's workspace
- Channels and roles are shared streams in the team workspace
- Agents check both: personal inbox + subscribed channel/role streams

### 4. Read Cursors

**Decision:** Per-entity cursor file.

Each agent has a JSON file mapping `stream_name → last_read_message_id`:
```json
{
  "channel/project-alpha": "msg_20260217_abc123",
  "role/procurement": "msg_20260217_def456",
  "inbox": "msg_20260217_ghi789"
}
```

On wake-up, agent reads each subscribed stream from its cursor position forward. Crash-safe — cursor only advances after successful processing.

### 5. Message Schema

**Decision:** Full PRD schema (14 fields) from day 1.

The Pydantic model is cheap to define. Establishes the contract. Fields can be optional but the shape is locked in. No phased schema evolution.

Fields: id, ts, sender, to, reply_to, thread_id, msg_type, priority, action_required, subject, body, refs, status, meta.

### 6. Message Body Size Limit

**Decision:** 64KB max.

Anything larger goes to the file store with a `file://` ref in the message. Prevents stream files from ballooning. Clear boundary between "message content" and "artifact storage."

### 7. Message Ordering

**Decision:** Append order is truth.

Messages are ordered by their position in the JSONL file (write order). The `ts` field is metadata for display/filtering, not ordering. Like Slack — order = when the server received it. No clock sync requirements between agents.

### 8. Cross-Team Communication

**Decision:** Team-isolated for Phase 1.

All messaging is scoped to a single team. No cross-team channels or DMs. Teams are fully isolated workspaces. Cross-team can come later if needed.

### 9. Storage Backend

**Decision:** Use the generic StorageBackend from the PRD.

Build storage abstraction first (build order #1), then messaging on top. Consistent across all ArcTeam subsystems. Swappable backends later.

### 10. Standalone Service

**Decision:** Messaging is a standalone service with zero arcagent dependency.

Any Python program, any agent framework can use it. The ArcAgent plugin wraps the messaging service but doesn't own it. CLI is first-class.

```python
from arcteam.messenger import MessagingService
# Works without arcagent installed
```

### 11. Real-Time Notifications

**Decision:** Polling only for Phase 1.

Agents check streams on wake-up and at defined intervals. No real-time push. Matches the async model. Real-time (ZMQ/Unix sockets) can come in Phase 3.

---

## Revised Storage Layout

Based on decisions above, the messaging storage shifts from the PRD:

```
.arc/team/messages/
├── channels/                    # Channel definitions (JSON)
│   ├── project-alpha.json       # {name, description, members: [URI]}
│   └── ops-alerts.json
├── streams/                     # Message content — single source of truth
│   ├── channel/
│   │   ├── project-alpha.jsonl  # All messages in this channel
│   │   └── ops-alerts.jsonl
│   └── role/
│       └── procurement.jsonl    # All messages broadcast to this role
├── inboxes/                     # Per-entity DM streams (personal workspace)
│   ├── agent_procurement-01.jsonl
│   └── user_josh.jsonl
├── cursors/                     # Per-entity read positions (NEW — replaces inbox drain)
│   ├── agent_procurement-01.json
│   └── user_josh.json
└── registry/                    # Entity definitions
    ├── agent_procurement-01.json
    └── user_josh.json
```

**Key changes from PRD:**
- `inboxes/` is now DM-only streams (not fanout targets)
- `cursors/` is new — tracks read positions per entity per stream
- `streams/` is the single source of truth for channel and role messages
- No message duplication across files

---

## Deepening Summary

**Deepened on:** 2026-02-17
**Sections enhanced:** 6 (competitive landscape, storage layout, cursor design, audit, schema, remaining gaps)
**Research agents:** 4 (AgentWorkforce/relay, agent messaging landscape, OpenClaw patterns, async messaging patterns)

### Key Findings

1. **We are the only file-backed, NATS-migration-path system** — Every competitor uses either in-memory (AutoGen, CrewAI, LangGraph), WebSocket RPC (OpenClaw), or terminal-parsing (relay). None have a file→SQLite→Postgres scaling story.
2. **Sequence numbers, not message IDs, should be the cursor unit** — Kafka, NATS JetStream, RabbitMQ Streams, and Liftbridge all use monotonic integers for consumer offsets. Our `msg_{timestamp}_{hash}` IDs are for identity; cursors should track `seq` integers.
3. **Separate audit stream is the correct pattern** — NIST 800-53 AU-2 requires audit as a separate concern with different retention. Inline audit metadata pollutes operational streams.
4. **OpenClaw's announce step is worth stealing** — After agent-to-agent exchange, default behavior surfaces results to the human. Opt-out, not opt-in. Maps to our proactive notification model.
5. **relay's shadow agent pattern is powerful** — Observer agents that receive filtered copies of messages for monitoring/oversight. First-class observability in the routing layer.
6. **Single writer per stream** — Every production system serializes writes. Our `fcntl.flock` approach is correct for Phase 1, and maps directly to NATS server-side serialization.
7. **Our 15-field envelope is the richest in the space** — A2A has ~5 meaningful fields, AutoGen has 2, CAMEL has 7. Our `action_required`, `priority`, and `subject` fields have no equivalent anywhere — they let agents triage without reading the full body.
8. **A2A is the future federation bridge** — Google's Agent-to-Agent protocol (now Linux Foundation) is the emerging standard for cross-org agent communication. Our ArcTeam messenger could expose an A2A-compatible endpoint for Phase 4 cross-team federation.
9. **Nobody else treats messaging as a standalone service** — AutoGen bundles comms with runtime. CrewAI bundles with crew. A2A requires full protocol implementation. Our `from arcteam.messenger import MessagingService` pattern is genuinely unique.
10. **Protocol consolidation: MCP for tools, A2A for agent-to-agent** — The industry is converging. MCP (Anthropic) handles agent-to-tool connections. A2A (Google) handles agent-to-agent. ArcTeam messaging is our internal A2A — and could bridge to the external A2A standard later.

---

## Competitive Landscape

### Research Insights

Four agent messaging systems were analyzed in depth. Here's how they compare to ArcTeam:

| Dimension | ArcTeam (ours) | AgentWorkforce/relay | OpenClaw | AutoGen/CrewAI/LangGraph |
|-----------|---------------|---------------------|----------|--------------------------|
| **Transport** | File-backed streams (Phase 1) → NATS (Phase 4) | Unix domain socket daemon + terminal parsing | WebSocket RPC through central Gateway | In-memory function calls |
| **Persistence** | JSONL append-only logs | SQLite WAL + JSONL fallback | JSONL session files | None (ephemeral) |
| **Addressing** | Typed URIs (`agent://`, `channel://`, `role://`) | Agent names, channels (`#general`), cross-project (`project:agent`) | Session keys (`agent:{id}:{provider}:{scope}:{identifier}`) | Direct Python object references |
| **Message format** | Structured 14-field envelope (Pydantic) | Length-prefixed JSON envelope (50+ message types) | Freeform text body + internal MessageEnvelope | Python dicts or strings |
| **Routing** | Pull from streams (agents check subscriptions) | Central daemon routes via Unix socket | Gateway RPC resolves to target session | Direct function invocation |
| **Delivery** | At-least-once with cursor-based tracking | ACK/NACK with offline queueing + Dead Letter Queue | Fire-and-forget or timeout-based wait | Synchronous (guaranteed in-process) |
| **Threading** | `thread_id` query filter on streams | Thread field in envelope | Session-scoped conversation (implicit) | No threading (sequential turns) |
| **Scale target** | 10,000+ agents (Phase 4) | 10,000 connected agents (single daemon) | Hundreds (single Gateway process) | Tens (in-memory, single process) |
| **Agent framework coupling** | Zero (standalone service) | Zero (terminal output parsing, works with any CLI agent) | Tight (OpenClaw agents only) | Tight (framework-specific) |
| **Security** | NIST 800-53, RBAC, signed messages (Ed25519) | Unix socket permissions only (0o600) | Ed25519 device signing, per-session Docker sandbox | None (trusted in-process) |
| **Federal/compliance** | Yes (FedRAMP, CMMC, air-gap capable) | No | No | No |

### Full Landscape Matrix

| Dimension | ArcTeam | relay | OpenClaw | A2A (Google) | AutoGen | CrewAI | LangGraph | MetaGPT |
|-----------|---------|-------|----------|-------------|---------|--------|-----------|---------|
| **Model** | Async pull (Slack-like) | Daemon-brokered | WebSocket RPC | Sync/async/SSE hybrid | Async event bus | Sync hub-spoke | State machine graph | Global message pool |
| **Envelope** | 15 fields structured | 50+ typed frames | Freeform text + internal envelope | Task+Message (5+ fields) | Minimal (content+source) | Implicit (task output) | None (shared state) | Role-structured |
| **Persistence** | JSONL append-only | SQLite WAL + JSONL | JSONL sessions | Task lifecycle | None (ephemeral) | None | Checkpoints | Pool duration |
| **Addressing** | Channel + Role + DM (URIs) | Names + channels + cross-project | Session keys | Agent Cards (DNS-like) | Named agents | Role-assigned | Graph nodes | Role subscriptions |
| **Threading** | thread_id + reply_to | Thread field | Session-scoped | contextId + taskId | Implicit | Implicit | Thread ID + checkpoint | Implicit |
| **Read tracking** | Per-entity cursor | ACK/NACK + offline queue | None | Client-side polling | None | None | Checkpointer | None |
| **Scale** | 10K+ agents (Phase 4) | 10K connected | Hundreds | Internet-scale | Same process | Same run | Same run | Fixed pipelines |
| **Coupling** | Zero (standalone) | Zero (terminal parsing) | Tight (OpenClaw only) | Protocol-level | Framework-specific | Framework-specific | Framework-specific | Framework-specific |
| **Security** | NIST 800-53, RBAC, Ed25519 | Unix socket perms only | Ed25519, Docker sandbox | OpenAPI auth | None | None | None | None |
| **Federal** | Yes (FedRAMP, CMMC) | No | No | No | No | No | No | No |

### What We Should Steal

**From AgentWorkforce/relay:**

1. **Shadow agent pattern** — Observer agents that receive filtered copies of primary agent messages based on triggers (`EXPLICIT_ASK`, `ALL_MESSAGES`). Our role-based addressing already supports this; we just need a `shadow` role type that gets read-only copies without appearing in the channel membership.

2. **Dead Letter Queue** — Messages that fail delivery after max retries go to a DLQ with reason tracking (TTL expired, target not found, payload too large, rate limited). Our audit trail captures failures, but a queryable DLQ is operationally better.

3. **Consensus voting** — Five voting types (Majority, Supermajority, Unanimous, Weighted, Quorum) for distributed agent decisions. Implemented as standard messages — proposals broadcast, votes parsed from replies. This maps naturally to our `msg_type=request` + `action_required=true` pattern.

4. **Agent work trail** — Separate from message history, agents emit `trail start`, `trail decision`, `trail complete` events that form an audit trail of work done, not just messages sent. Our task engine could subscribe to these.

5. **Idle detection before injection** — relay uses a three-signal confidence threshold before injecting messages into an active agent. For our polling model this isn't needed, but for future Phase 3 push notifications, we'll need something similar.

**From OpenClaw:**

6. **Announce step (human notification)** — After agent-to-agent exchange completes, the result is surfaced to the human via their configured channel (Telegram, Slack, etc.). Default ON, opt-out via `ANNOUNCE_SKIP`. This bridges our arcteam messaging with the Telegram module from the prior brainstorm.

7. **Ping-pong protocol** — Agent A sends to Agent B, then they alternate turns (max 5 rounds). Either can terminate early with `REPLY_SKIP`. This is a structured negotiation pattern that avoids infinite back-and-forth. For arcteam, we could implement this as a `msg_type=request` with `meta.max_rounds` field.

8. **Subagent tool lockout** — Spawned subagents cannot use `sessions_send` or `sessions_spawn`, preventing self-replicating agent trees. For arcteam, this maps to RBAC: subagent roles should have restricted messaging permissions.

9. **Session history access** — `sessions_history` lets agents read another agent's conversation transcript. In arcteam, this would be a "read another agent's DM stream" capability, gated by RBAC.

**From Google A2A protocol:**

10. **Agent Cards for capability discovery** — JSON documents published at well-known endpoints describing identity, capabilities, and auth requirements. Our `registry/*.json` entity definitions should include a `capabilities` field — what message types can this agent handle? What roles can it play? Critical as teams grow.

11. **Task lifecycle state machine** — A2A's `working → input_required → completed/failed/canceled/rejected` is well-designed. When our `msg_type=task` messages trigger work, both sender and receiver need to track the work lifecycle. This bridges messaging and the task engine.

12. **contextId for conversation grouping** — A2A groups related tasks and messages into a `contextId`. Our `thread_id` covers this, but consider: should threads span message types? (A task reply linking back to the original request message in a different channel.)

**From MetaGPT:**

13. **Subscription-based message filtering** — MetaGPT agents only see messages relevant to their role. Our cursor-based pull delivers all messages in a channel. Consider a `msg_type` filter on cursor reads — agents declare which types they care about and skip the rest. Our `msg_type` field already enables this.

14. **Structured deliverables, not just text** — MetaGPT's biggest win: messages carry structured artifacts (designs, code), not conversational text. Our `refs` field + 64KB limit with `file://` for larger content mirrors this. Encourage agents to send structured status objects in body, not prose.

**From LangGraph:**

15. **Checkpointing for long-running tasks** — LangGraph's time-travel (save state at each step, replay from any point) is valuable. Our streams give message history; consider adding `tasks/{task_id}/checkpoint.jsonl` for intermediate work states agents need to resume after restart.

**From CAMEL Agent Network:**

16. **Normalization for heterogeneous agents** — CAMEL's Protocol Translator normalizes different frameworks' message formats into a shared schema. As ArcTeam grows to support non-ArcAgent participants, our 15-field schema should be the normalization target. Consider a spec mapping A2A messages → ArcTeam envelope for future ecosystem bridging.

**From async messaging research (Kafka/NATS/Liftbridge patterns):**

17. **Sequence numbers as cursor unit** — Every production streaming system uses monotonic integers, not string IDs. Our cursor should store `seq: int` + `byte_pos: int`, resume by seq, seek by byte_pos for performance.

18. **Chained HMAC for audit tamper-evidence** — Each audit record's HMAC includes the previous record's HMAC. Gap in sequence = evidence of deletion. Modification breaks the chain. Satisfies NIST 800-53 AU-9 without a blockchain.

19. **Checkpoint frequency** — RabbitMQ Streams recommends checkpointing every 10,000 messages at high volume. For agent messaging (lower volume), every 10-100 messages. The tradeoff: checkpoint frequency vs. message reprocessing on crash.

20. **Message TTL** — Consider an `expires_at` field in `meta`. Messages that expire before being read move to the DLQ with reason `expired`. Maintains audit trail while keeping streams clean.

### What We're Doing Differently (Our Advantages)

1. **File → NATS migration path** — Nobody else has this. Our JSONL streams, cursor files, and subject-based routing map 1:1 to NATS JetStream concepts. Migration is a backend swap, not a rewrite.

2. **Richest envelope schema in the space** — 15 structured fields vs. AutoGen's 2, A2A's ~5, CAMEL's 7. The `action_required`, `priority`, and `subject` fields let agents triage without reading full message bodies — no equivalent exists anywhere.

3. **Messaging as standalone service** — `from arcteam.messenger import MessagingService` works without arcagent installed. No other framework separates messaging from their agent runtime. Any Python program, any agent framework, any CLI tool can participate.

4. **Pull-based with per-entity cursors** — Every other framework uses push (AutoGen events, LangGraph state mutations, relay daemon injection). We're the only pull-based system with durable cursor tracking, which maps directly to NATS JetStream consumers. Agents that sleep, crash, or restart lose nothing.

5. **Role-based broadcasting as first-class concept** — `role://procurement` delivers to all agents with that role. MetaGPT has role subscriptions but they're fixed at startup. CrewAI routes tasks by role, not messages. Our dynamic role-based addressing is genuinely novel.

6. **Federal compliance built-in** — NIST 800-53 audit trail with chained HMACs, RBAC, Ed25519 signing, classification-aware data flow. Nobody else targets FedRAMP/CMMC environments.

7. **Zero external dependencies** — Phase 1 uses Python stdlib only. relay needs Node.js + SQLite + tmux/Rust. OpenClaw needs its entire Gateway. AutoGen/CrewAI/LangGraph need their frameworks.

8. **Storage-as-truth, not memory-as-truth** — Every other framework treats in-memory agent state as authoritative. Our JSONL append-only model makes the file the authoritative record. The agent's memory is just a cache. This is event sourcing for agent communication — every message is an immutable, auditable record.

---

## Revised Storage Layout

### Research Insights

Based on Kafka, NATS JetStream, and Liftbridge patterns, the storage layout should use **NATS-compatible subject naming** so migration is a backend swap:

```
.arc/team/
├── messages/
│   ├── channels/                    # Channel definitions (JSON)
│   │   ├── project-alpha.json       # {name, description, members: [URI]}
│   │   └── ops-alerts.json
│   ├── streams/                     # Message content — single source of truth
│   │   ├── arc.channel.project-alpha/
│   │   │   ├── 00000000.log        # JSONL messages (NATS: stream storage)
│   │   │   └── meta.json           # Stream config (retention, max_bytes)
│   │   ├── arc.channel.ops-alerts/
│   │   │   ├── 00000000.log
│   │   │   └── meta.json
│   │   ├── arc.role.procurement/
│   │   │   ├── 00000000.log
│   │   │   └── meta.json
│   │   └── arc.agent.procurement-01/   # DM inbox stream
│   │       ├── 00000000.log
│   │       └── meta.json
│   ├── cursors/                     # Per-entity read positions (NATS: durable consumers)
│   │   ├── arc.channel.project-alpha/
│   │   │   ├── agent_procurement-01.cursor   # {seq, byte_pos, updated_at}
│   │   │   └── user_josh.cursor
│   │   ├── arc.role.procurement/
│   │   │   ├── agent_procurement-01.cursor
│   │   │   └── agent_procurement-02.cursor
│   │   └── arc.agent.procurement-01/
│   │       └── agent_procurement-01.cursor
│   └── registry/                    # Entity definitions
│       ├── agent_procurement-01.json
│       └── user_josh.json
├── audit/
│   ├── 00000000.log                 # Separate audit stream (NEVER compacted)
│   └── meta.json                    # {retention: "7y", hmac_chain: true}
├── dlq/                             # Dead Letter Queue (NEW)
│   └── 00000000.log                 # Failed deliveries with reason tracking
└── security/
    └── acl.json                     # Role-based permissions
```

**Key changes from previous version:**
- NATS-compatible subject naming (`arc.channel.X`, `arc.role.X`, `arc.agent.X`)
- Segment files (`00000000.log`) instead of `name.jsonl` — matches Kafka/NATS log format
- `meta.json` per stream for retention policy, max bytes, config
- Separate `audit/` directory (not inline) — NIST 800-53 AU-2 requirement
- `dlq/` for Dead Letter Queue — stolen from relay
- Cursor files now include `seq` + `byte_pos` + `updated_at` (not just message ID)
- DM inboxes moved INTO streams/ with `arc.agent.X` naming (consistent with channels/roles)

### NATS Migration Map

| File-Based Concept | NATS JetStream Concept |
|---|---|
| `streams/arc.channel.X/` directory | JetStream Stream on subject `arc.channel.X.>` |
| `00000000.log` JSONL file | Stream file storage (NATS default) |
| `seq` field in each record | `msg.Metadata().Sequence.Stream` |
| `cursors/stream/consumer.cursor` | Durable consumer with named cursor |
| `cursor.seq` integer | `DeliverByStartSequence` value |
| `poll(max_messages=N)` | Pull consumer `fetch(batch=N)` |
| `ack(seq)` advancing cursor | `msg.ack()` |
| Role stream fan-out (multiple cursors) | Multiple consumers on one stream |
| `meta.json` retention config | Stream configuration (`RetentionPolicy`) |
| `dlq/` Dead Letter Queue | Republish to DLQ stream on NAK |
| `audit/` stream | Dedicated audit stream with `RetentionPolicy.LIMITS` |

---

## Cursor Design

### Research Insights

Every production streaming system (Kafka, NATS JetStream, RabbitMQ Streams, Liftbridge) uses monotonic sequence numbers as the primary cursor unit. Our cursor design should mirror this:

**Cursor file format:**
```json
{
  "consumer": "agent_procurement-01",
  "stream": "arc.channel.project-alpha",
  "seq": 1042,
  "byte_pos": 98304,
  "updated_at": "2026-02-17T14:30:00Z"
}
```

**Critical design decisions from research:**

1. **Sequence number is primary, byte position is secondary** — Resume by `seq` (portable across backends). Use `byte_pos` for fast file seeking (optimization, not truth). When migrating to NATS, `seq` maps directly to stream sequence; `byte_pos` is dropped.

2. **Atomic cursor writes** — Always `write-to-temp + os.rename()`. POSIX `rename()` is atomic. Prevents partial cursor state on crash. This is the exact pattern RabbitMQ Streams uses.

3. **Cursor advances AFTER processing, not before** — At-least-once guarantee. If agent crashes mid-processing, it re-reads from last cursor position. Agent must handle duplicate `seq` values gracefully.

4. **Stale cursor cleanup** — Cursor files should include `updated_at`. A cleanup job removes cursors older than an `InactiveThreshold` (e.g., 24 hours). Prevents cursor file accumulation from crashed agents. Mirrors NATS ephemeral consumer auto-delete.

5. **Checkpoint frequency** — Don't checkpoint after every message. For agent messaging volume, every 10 messages or 30 seconds (whichever comes first). Tradeoff: checkpoint frequency vs. reprocessing on crash.

---

## Audit Trail Design

### Research Insights

NIST 800-53 AU-2, AU-3, AU-9, AU-12 require audit as a **separate stream** with tamper-evidence. The production pattern from federal-compliant systems:

**Audit record schema:**
```python
@dataclass
class AuditRecord:
    audit_seq: int              # Monotonic, gap = evidence of deletion
    event_type: str             # "message.published" | "message.delivered" | "cursor.advanced"
    stream: str                 # Which stream was affected
    msg_seq: int | None         # Sequence in the operational stream
    subject: str                # NATS-style subject
    actor_id: str               # Agent DID or entity URI
    target_id: str | None       # Recipient (None for publish events)
    classification: str         # "UNCLASSIFIED" | "CUI" | etc.
    timestamp_utc: str          # ISO8601 with microseconds
    detail: str                 # Human-readable summary
    hmac_sha256: str            # Chained HMAC (includes prev record's HMAC)
```

**Chained HMAC for tamper-evidence:**
- Each record's HMAC includes the previous record's HMAC as input
- Deleting any record breaks all subsequent HMACs (detectable)
- Gap in `audit_seq` means records were removed (detectable)
- Modifying any record breaks its own HMAC (detectable)
- No blockchain needed — chained HMACs provide equivalent tamper-evidence

**Audit stream is NEVER compacted, NEVER deleted through normal operations.** Different retention policy from operational streams.

---

## Message Schema Enhancement

### Research Insights

Based on competitive analysis, add `seq` field to the message envelope:

**Enhanced schema (15 fields, up from 14):**

| Field | Type | Required | Source |
|-------|------|----------|--------|
| **seq** | int | Auto | **NEW** — Monotonic per-stream sequence number. Primary cursor unit. Maps to NATS `msg.Metadata().Sequence.Stream` |
| id | string | Auto | `msg_{timestamp}_{hash}` — Identity/dedup key |
| ts | ISO 8601 | Auto | UTC timestamp (metadata, not ordering) |
| sender | URI | Yes | `agent://x` or `user://x` |
| to | list[URI] | Yes | Target addresses |
| reply_to | string \| null | No | Message ID this replies to |
| thread_id | string \| null | Auto | Root message ID of thread |
| msg_type | enum | Yes | `info \| request \| task \| result \| alert \| ack` |
| priority | enum | Default | `low \| normal \| high \| critical` |
| action_required | bool | Default | Whether recipient must act |
| subject | string | No | Short summary for triage |
| body | string | Yes | Content (max 64KB) |
| refs | list[URI] | No | Cross-references |
| status | string | Auto | `sent \| delivered \| read \| acted` |
| meta | dict | No | Extensible metadata |

**`meta` field conventions** (inspired by relay and OpenClaw):
- `meta.max_rounds` — For request/negotiation: max ping-pong turns before auto-close (from OpenClaw)
- `meta.announce` — Whether to surface result to human via external channel (from OpenClaw)
- `meta.trail_id` — Link to agent work trail for traceability (from relay)
- `meta.dlq_reason` — Set when message lands in Dead Letter Queue (from relay)

---

## Remaining Gaps (Not Addressed — Future Brainstorms)

1. **Stream compaction/segmentation** — When `00000000.log` exceeds a size threshold, should we roll to `00000001.log`? Kafka does this. NATS does this internally. Phase 1 can defer but needs a plan.
2. **Entity deregistration** — What happens to messages/cursors when an agent is removed? Cursor cleanup handles stale cursors, but what about messages addressed to deregistered agents?
3. **Channel membership changes** — If an agent joins a channel late, do they see history? (NATS: `DeliverAll` vs `DeliverNew` policy — configurable per consumer)
4. **Rate limiting specifics** — Per-entity, per-stream. What thresholds? relay uses configurable per-agent throttling. We should too.
5. **Error handling in service layer** — Retry policy for failed writes, corrupt file recovery, partial writes
6. **Testing patterns** — relay uses a MemoryAdapter for tests. We should have an in-memory StorageBackend for the same reason.
7. **Shadow agent implementation** — How does the shadow/observer pattern map to our RBAC model?
8. **Human announce step** — How does arcteam messaging notify humans via external channels (Telegram, etc.)?
9. **Ping-pong protocol** — Should `msg_type=request` support structured multi-turn negotiation with max rounds?
10. **Cross-machine relay** — relay has a cloud backend for cross-machine routing. Our NATS Phase 4 handles this natively, but what about Phase 1-3?
11. **A2A federation bridge** — When cross-team is needed, should we expose an A2A-compatible endpoint? Our 15-field envelope can map to A2A's Task+Message format.
12. **Agent capability discovery** — A2A's Agent Cards declare what an agent can do. Our registry needs a `capabilities` schema for routing and delegation decisions.
13. **Idempotent processing contract** — Cursor-after-processing means agents may see the same message twice on crash. This MUST be documented as an explicit contract — all message processing must be idempotent.
14. **Human on-ramp** — How does a human's message enter a stream? CLI? Telegram module? REST API? The messaging service is standalone but the human interface is unspecified.

---

## Resolved (Full List)

| Question | Decision |
|----------|----------|
| Delivery guarantee | Deliver once, retry failback, audit everything |
| Routing model | Pull (not fanout) — agents check streams |
| DM handling | Per-entity inbox stream (personal workspace) |
| Channel/role handling | Shared streams (team workspace) |
| Read tracking | Per-entity cursor file (seq + byte_pos) |
| Cursor unit | Monotonic sequence number (not message ID) |
| Schema scope | 15-field envelope (added seq) |
| Body size limit | 64KB max |
| Ordering | Append order (file position) is truth |
| Cross-team | Team-isolated, Phase 1 |
| Storage backend | Generic StorageBackend (PRD plan) |
| Standalone | Yes, zero arcagent dependency |
| Real-time | Polling only, Phase 1 |
| Mental model | Slack for agents (not email) |
| Audit trail | Separate stream with chained HMACs |
| Dead Letter Queue | Yes — failed deliveries with reason tracking |
| Subject naming | NATS-compatible (`arc.channel.X`, `arc.role.X`, `arc.agent.X`) |
| Stream file naming | Segment files (`00000000.log`) with `meta.json` |
| Cursor file format | JSON with seq, byte_pos, updated_at; atomic write via rename |
| Cursor checkpoint frequency | Every 10 messages or 30 seconds |
| Stale cursor cleanup | Remove after InactiveThreshold (24h default) |

---

## References

### Projects Analyzed
- [AgentWorkforce/relay](https://github.com/AgentWorkforce/relay) — Terminal-parsed, Unix socket daemon, SQLite/JSONL, 50+ message types, shadow agents, consensus voting
- [OpenClaw](https://github.com/openclaw/openclaw) — WebSocket RPC Gateway, sessions_send, ping-pong protocol, announce step, per-session Docker sandbox
- Kafka, NATS JetStream, RabbitMQ Streams, Liftbridge — Production streaming patterns for cursor design, storage layout, audit trails

### Key Sources
- [NATS JetStream Consumers](https://docs.nats.io/nats-concepts/jetstream/consumers)
- [Liftbridge Cursor Design](https://liftbridge.io/docs/cursors.html)
- [RabbitMQ Streams Offset Tracking](https://www.rabbitmq.com/blog/2021/09/13/rabbitmq-streams-offset-tracking)
- [Kafka Log Storage Deep Dive](https://medium.com/@anil.goyal0057/deep-dive-how-kafka-stores-logs-on-disk-segments-rolling-indexes-retention-compaction-b41500d2d057)
- [NIST 800-53 AU-2 Logging Requirements](https://securestrux.com/resources/cyber-advisory-center/understanding-and-implementing-nist-sp-800-53-au-2-logging-requirements-for-defense-industrial-base-systems/)
- [OpenClaw Session Tool Docs](https://docs.openclaw.ai/concepts/session-tool)
- [relay ARCHITECTURE.md](https://github.com/AgentWorkforce/relay/blob/main/ARCHITECTURE.md)
- [Google A2A Protocol Specification](https://a2a-protocol.org/latest/specification/)
- [A2A Announcement Blog](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
- [AutoGen 0.4 Launch](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/)
- [CAMEL Agent Network with MCP](https://www.camel-ai.org/blogs/creating-your-own-agent-to-agent-communication-with-model-context-protocol)
- [MetaGPT Paper](https://arxiv.org/abs/2308.00352)
- [Survey of Agent Interoperability Protocols](https://arxiv.org/html/2505.02279v1)
- [MCP Specification Nov 2025](https://modelcontextprotocol.io/specification/2025-11-25)

---

## Next Steps

- `/build` — Walk through implementation design decisions (service layer API, CLI commands, cursor update semantics, StorageBackend protocol)
- `/specify` — Formal spec for the messaging subsystem, incorporating all gap-filling decisions and research insights
