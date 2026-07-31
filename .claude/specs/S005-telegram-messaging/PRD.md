# Product Requirements Document: Telegram Messaging Module

## Validation Checklist

- [x] All required sections are complete
- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Steering docs loaded and referenced
- [x] Problem statement is specific and measurable
- [x] Context -> Problem -> Solution flow makes sense
- [x] All MoSCoW categories addressed
- [x] Every feature has testable acceptance criteria (EARS format)
- [x] Every metric has corresponding tracking method
- [x] No contradictions with steering constraints
- [x] A new team member could understand this PRD

---

## Context References

### Steering Doc Links

- **Personas**: `packages/arcagent/.claude/steering/product.md#user-personas`
- **Constraints**: `packages/arcagent/.claude/steering/product.md#business-constraints`
- **Metrics Framework**: `packages/arcagent/.claude/steering/product.md#success-metrics-framework`
- **Current Phase**: `packages/arcagent/.claude/steering/roadmap.md#current-phase`

### Prior Work

- **Brainstorm**: `.claude/brainstorms/2026-02-16-agent-messaging.md` (deepened)
- **Build Decisions**: `.claude/decisions-log.md` > Feature: Telegram Messaging Module (14 decisions)

---

## Product Overview

### Vision

Enable bidirectional text messaging between a human and their ArcAgent via Telegram, so the agent becomes a phone-accessible assistant you can text anytime.

### Problem Statement

ArcAgent has a fully functional core (tools, memory, policy, scheduling) but is only accessible via CLI on the same machine. There is no way to interact with the agent from a phone, receive proactive notifications after cron-triggered work, or have a persistent conversational interface outside a terminal session. This limits the agent's utility to desk-bound, synchronous interactions.

### Value Proposition

- **Phone-native access**: Text the agent from any phone, anywhere, without installing a custom app
- **Proactive notifications**: Agent reaches out after scheduled work completes (via Module Bus events)
- **Zero incremental cost**: Telegram Bot API is free with no per-message fees
- **Removable**: Module Bus participant -- can be removed without touching core

---

## Target Personas

**Primary**: BlackArc Internal Developer (tertiary persona from steering)
- **Feature-Specific Goals**: Text the agent from phone, receive cron results, have conversational back-and-forth while away from desk
- **Feature-Specific Pain Points**: Currently must SSH to the machine or sit at the terminal to interact with the agent

**Secondary**: Federal Systems Integrator (primary persona from steering)
- **Feature-Specific Goals**: Evaluate messaging channels for agent communication in non-classified contexts
- **Feature-Specific Pain Points**: Needs configurable authorization (chat_id allowlist) to prevent unauthorized access

---

## User Journey: Telegram Messaging

1. **Trigger**: User wants to message their agent from their phone, or the agent completes scheduled work and needs to notify the user
2. **Entry**: User sends `/start` to the bot in Telegram. Module stores `chat_id`, creates initial session
3. **Core Flow**:
   - User sends free-text message -> Module queues it -> Routes to `agent.chat()` -> Sends response back
   - Agent completes cron job -> Module Bus `schedule:completed` event fires -> Module sends notification to stored `chat_id`
   - User sends `/new` -> Module starts fresh session
   - User sends `/status` -> Module returns current session info
4. **Success State**: User receives timely, contextually-aware responses from their agent via Telegram, including proactive notifications
5. **Edge Cases**: Message exceeds 4096 chars (smart split), unauthorized chat_id (silently ignored + logged), bot token missing (module stays dormant), agent processing takes > 5s (typing indicator expires naturally)

---

## Feature Requirements

### Must Have (Critical for Launch)

#### 1.1 Inbound Message Handling

- **User Story**: As a developer, I want to text my agent from Telegram so that I can interact with it from my phone
- **Acceptance Criteria** (EARS format):
  - [x] WHEN user sends a text message to the bot THEN system SHALL route it to `agent.chat(message, session_id=current_session)` and send the response back
  - [x] WHEN user sends `/start` THEN system SHALL store the `chat_id`, create a new session, and reply with a welcome message
  - [x] WHEN user sends `/new` THEN system SHALL create a fresh session (new `session_id`) and confirm to the user
  - [x] System SHALL process messages sequentially via `asyncio.Queue` to prevent session state race conditions
- **Edge Cases**:
  - Empty message -> Ignore silently
  - Rapid-fire messages -> Queue and process in order

#### 1.2 Outbound Response Delivery

- **User Story**: As a developer, I want to receive agent responses in Telegram so that conversations feel natural
- **Acceptance Criteria** (EARS format):
  - [x] WHEN agent response exceeds 4096 characters THEN system SHALL split at paragraph boundaries (double-newline first, sentence boundaries second, hard-split at 4096 as fallback) and send as sequential messages
  - [x] WHEN agent begins processing a message THEN system SHALL send a TYPING chat action as acknowledgment
  - [x] System SHALL send responses as plain text (no parse mode)
- **Edge Cases**:
  - Agent returns `None`/empty -> Send "No response" acknowledgment
  - Network error during send -> Log and retry once

#### 1.3 Authorization

- **User Story**: As an operator, I want to restrict who can message the bot so that only authorized users interact with the agent
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL verify incoming `chat_id` against `allowed_chat_ids` config list before processing any message
  - [x] WHEN message comes from unauthorized `chat_id` THEN system SHALL silently ignore it AND log the attempt to telemetry
  - [x] IF `allowed_chat_ids` is empty THEN system SHALL accept no messages (fail-closed)

#### 1.4 Bot Token Management

- **User Story**: As an operator, I want the bot token stored securely so that credentials don't touch the filesystem
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL read bot token from `ARCAGENT_TELEGRAM_BOT_TOKEN` environment variable
  - [x] IF environment variable is not set THEN system SHALL log a warning and not start the polling loop
  - [x] System SHALL NOT log, persist, or include the bot token in any error messages or telemetry

#### 1.5 Module Lifecycle

- **User Story**: As a developer, I want the Telegram module to follow ArcAgent's module conventions so that it's discoverable and manageable
- **Acceptance Criteria** (EARS format):
  - [x] System SHALL implement the Module protocol (`name` property, `async startup(ctx)`, `async shutdown()`)
  - [x] WHEN module starts up THEN system SHALL start a long-polling background task via `asyncio.create_task`
  - [x] WHEN module shuts down THEN system SHALL stop the polling loop gracefully (drain queue, close connections)
  - [x] System SHALL use deferred binding (`set_agent_chat_fn`) for agent access, wired by `agent.py` after startup

### Should Have (Important but not critical)

#### 2.1 Proactive Notifications

- **User Story**: As a developer, I want the agent to text me when scheduled work completes so that I'm informed without checking
- **Acceptance Criteria** (EARS format):
  - [x] WHEN `schedule:completed` event fires on Module Bus THEN system SHALL send the result summary to the stored `chat_id`
  - [x] WHEN `schedule:failed` event fires THEN system SHALL send the error summary to the stored `chat_id`

#### 2.2 Session Status

- **User Story**: As a developer, I want to check agent status from Telegram so that I know what session I'm in
- **Acceptance Criteria** (EARS format):
  - [x] WHEN user sends `/status` THEN system SHALL reply with current session ID, message count, and agent name

### Could Have (Nice to have)

#### 3.1 Telegram-Specific Tools

- Register `send_telegram_message` tool so the agent can proactively message the user during any session

### Won't Have (This Phase)

- Webhook mode (HTTPS required, domain needed)
- Group chat support (multi-user)
- Voice/audio messages
- Rich media (images, files)
- Markdown/HTML formatting
- Inline keyboards or callback buttons
- Multi-tenant support (multiple bots/users)

---

## Success Metrics

| Metric Category | Feature Target | Tracking Method |
|-----------------|----------------|-----------------|
| Adoption | Module enabled and first message exchanged | Telemetry: `telegram:message_received` event |
| Engagement | Messages per session > 3 | Telemetry: message count per session |
| Quality | Response delivery success rate > 99% | Telemetry: send failures vs attempts |
| Latency | Median response time < 30s | Telemetry: time from receive to send |

---

## Constraints

### From Steering (Applicable)

| ID | Constraint | Source |
|----|------------|--------|
| CON-1 | Core < 3,000 LOC (module doesn't count) | tech.md |
| CON-5 | Credentials never plaintext on filesystem | tech.md |
| CON-6 | Every action is an audit event | tech.md |

### Feature-Specific Constraints

| ID | Constraint | Type |
|----|------------|------|
| CON-F1 | Telegram 4096 char per-message limit | Platform |
| CON-F2 | Bot cannot message users who haven't sent `/start` | Platform |
| CON-F3 | Long polling and webhooks are mutually exclusive | Platform |
| CON-F4 | Rate limit: 1 msg/sec per chat, 30 msg/sec broadcast | Platform |
| CON-F5 | Bot token = full control of bot (single credential) | Security |

---

## Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Bot token exposure | High | Low | Env var only, never logged, never in telemetry |
| Telegram API downtime | Medium | Low | Graceful degradation, retry on transient errors |
| Message loss during restart | Low | Medium | Updates stored server-side for 24h by Telegram |
| Rate limiting (429) | Medium | Low | Sequential processing + backoff on 429 |
| Session state corruption | High | Low | asyncio.Queue ensures sequential processing |

---

## Open Questions

None -- all decisions resolved through `/build` interactive session (14 decisions).

---

## Glossary Additions

| Term | Definition | Context |
|------|------------|---------|
| **Long Polling** | HTTP-based pull mechanism where bot calls `getUpdates` periodically | Inbound message transport |
| **chat_id** | Telegram's integer identifier for a private chat between user and bot | Authorization + message routing |
| **Smart Split** | Algorithm that breaks long text at paragraph/sentence boundaries to stay under 4096 chars | Response delivery |
| **Deferred Binding** | Pattern where a callback (e.g., `agent.chat`) is provided to a module after startup completes | Module lifecycle |
