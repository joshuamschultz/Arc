# S005: Telegram Messaging Module

## Metadata

| Field | Value |
|-------|-------|
| **Spec ID** | S005 |
| **Feature** | telegram-messaging |
| **Status** | PENDING |
| **Created** | 2026-02-16 |
| **Author** | Claude + Josh |
| **Type** | Integration (ArcAgent Module) |

## Prior Work

- **Brainstorm**: `.claude/brainstorms/2026-02-16-agent-messaging.md` (deepened)
- **Build Decisions**: `.claude/decisions-log.md` > Feature: Telegram Messaging Module (14 decisions)
- **Build State**: `.claude/builds/telegram-messaging/state.json`

## Documents

| Document | Purpose | Status |
|----------|---------|--------|
| PRD.md | Product requirements | Complete |
| SDD.md | Solution design | Complete |
| PLAN.md | Implementation tasks | Complete |

## Key Decisions

| ID | Decision | Choice |
|----|----------|--------|
| D-001 | Module location | `arcagent/modules/telegram/` |
| D-002 | Inbound transport | Long polling only |
| D-003 | Lifecycle | Module owns polling loop |
| D-004 | Agent access | Deferred binding via `set_agent_chat_fn` |
| D-005 | Sessions | Resume last, `/new` for fresh |
| D-006 | Chat model | 1 bot = 1 user = 1 chat |
| D-007 | Long messages | Smart split at paragraph boundaries |
| D-008 | Formatting | Plain text only |
| D-009 | Acknowledgment | Typing indicator only |
| D-010 | Token storage | Environment variable |
| D-011 | Authorization | chat_id allowlist in config |
| D-012 | Concurrency | Sequential via asyncio.Queue |
| D-013 | Testing | Mock bot API + real agent |
| D-014 | Activation | Auto-start in serve mode |

## Learnings

(Captured during implementation)
