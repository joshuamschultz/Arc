# SPEC-006: ArcTeam Messaging Subsystem

## Metadata

| Field | Value |
|-------|-------|
| ID | SPEC-006 |
| Feature | ArcTeam Inter-Agent Messaging |
| Status | PENDING |
| Type | Integration |
| Created | 2026-02-17 |
| Package | `packages/arcteam` |
| Branch | `feature/arcTeam-messaging` |
| Confidence | 90% (fast-track) |

## Prior Work

| Source | Location | Contribution |
|--------|----------|-------------|
| Brainstorm (deepened) | `.claude/brainstorms/2026-02-17-arcteam-messaging.md` | 21 resolved decisions, competitive landscape (8 systems), 20 steal-worthy patterns, NATS migration map |
| Full ARC Team PRD | `packages/arcteam/.claude/ARC-Team-PRD-Spec-Architecture.md` | Comprehensive system design (Sections 3-4, 8-9) |
| ArcTeam CLAUDE.md | `packages/arcteam/CLAUDE.md` | Build standards, separation of concerns, project structure |

## Scope

Phase 1 of the ArcTeam messaging subsystem: file-backed, async, pull-based messaging for autonomous agents. Includes StorageBackend abstraction, MessagingService, entity registry, audit logger, and CLI.

**In scope:**
- StorageBackend protocol + FileBackend implementation
- Audit logger with chained HMACs
- Entity registry (agents + users + roles)
- MessagingService (send, poll, cursor management)
- Channel and role management
- CLI (`arc-team` commands)
- Dead Letter Queue

**Out of scope (future phases):**
- Task engine (SPEC-007)
- Knowledge base (SPEC-008)
- File store (SPEC-009)
- ARC Agent plugin (SPEC-010)
- SQLite/Postgres backends (Phase 2-4)
- Cross-team federation
- Real-time push notifications

## Decisions Log

21 decisions captured in brainstorm. Key architectural shifts from original PRD:

1. **Pull model, not fanout** — Messages stay in streams. Agents check subscribed streams via cursors.
2. **Sequence numbers for cursors** — Monotonic integers, not message IDs.
3. **NATS-compatible subject naming** — `arc.channel.X`, `arc.role.X`, `arc.agent.X`
4. **Separate audit stream** — Not inline. Chained HMACs for tamper-evidence.
5. **DLQ for failed deliveries** — Reason tracking, queryable.
6. **15-field message envelope** — Added `seq` field to original PRD schema.

## Learnings

(Updated during implementation)
