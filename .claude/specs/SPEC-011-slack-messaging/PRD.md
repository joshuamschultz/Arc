# PRD: Slack Messaging Module

## Product Overview

### Vision

Enable bidirectional messaging between humans and their ArcAgent via Slack, so multiple team members can each have a dedicated agent they can DM from any device.

### Problem Statement

ArcAgent's Telegram module (S005) provides human-agent chat but Telegram is not approved for all environments. For Azure GCC deployments, Slack is the standard communication platform. The team needs 3 agents, each paired with a different user, all communicating via Slack DMs. Socket Mode (WebSocket-based) is required because the GCC environment may not allow inbound HTTP connections.

### Value Proposition

- **Enterprise standard**: Slack is already the team's communication platform
- **Multi-user**: Each agent maps to specific allowed users (not 1:1 like Telegram)
- **No public URL**: Socket Mode works behind firewalls (critical for GCC)
- **Removable**: Module Bus participant — can be removed without touching core
- **Familiar UX**: Users interact with the agent in a normal Slack DM

---

## Feature Requirements

### Must Have

#### 1.1 Inbound Message Handling

- **User Story**: As a team member, I want to DM my agent in Slack so I can interact with it from any device
- **Acceptance Criteria**:
  - WHEN user sends a DM to the bot THEN system SHALL route it to `agent.chat(message, session_id=current_session)` and send the response back
  - WHEN user sends `/start` THEN system SHALL create a new session and confirm
  - WHEN user sends `/new` THEN system SHALL create a fresh session (new `session_id`) and confirm
  - System SHALL skip bot messages (prevent infinite loops)
  - System SHALL process messages per-user sequentially via per-user asyncio.Lock

#### 1.2 Outbound Response Delivery

- **User Story**: As a team member, I want agent responses delivered in Slack so conversations feel natural
- **Acceptance Criteria**:
  - WHEN agent response exceeds 4000 characters THEN system SHALL split at paragraph boundaries and send as sequential messages in a thread
  - WHEN agent begins processing THEN system SHALL add :thinking_face: reaction to the user's message
  - WHEN agent completes processing THEN system SHALL remove :thinking_face: and add :white_check_mark:
  - System SHALL send responses as thread replies to the triggering message

#### 1.3 Authorization

- **User Story**: As an operator, I want to restrict who can DM the bot
- **Acceptance Criteria**:
  - System SHALL verify incoming user ID against `allowed_user_ids` config list
  - WHEN message comes from unauthorized user THEN system SHALL silently ignore it AND log the attempt
  - IF `allowed_user_ids` is empty THEN system SHALL accept no messages (fail-closed)

#### 1.4 Token Management

- **User Story**: As an operator, I want tokens stored securely
- **Acceptance Criteria**:
  - System SHALL read bot token from `ARCAGENT_SLACK_BOT_TOKEN` env var
  - System SHALL read app-level token from `ARCAGENT_SLACK_APP_TOKEN` env var
  - IF either token is not set THEN system SHALL log a warning and stay dormant
  - System SHALL NOT log, persist, or include tokens in telemetry

#### 1.5 Module Lifecycle

- **User Story**: As a developer, I want the Slack module to follow ArcAgent module conventions
- **Acceptance Criteria**:
  - System SHALL implement the Module protocol (`name`, `startup(ctx)`, `shutdown()`)
  - WHEN module starts THEN system SHALL establish Socket Mode WebSocket connection
  - WHEN module shuts down THEN system SHALL close the WebSocket gracefully
  - System SHALL use deferred binding via `agent:ready` event for agent.chat() access

### Should Have

#### 2.1 Proactive Notifications

- **User Story**: As a team member, I want the agent to DM me when scheduled work completes
- **Acceptance Criteria**:
  - System SHALL register a `notify_user` tool for the LLM to call
  - WHEN `schedule:failed` event fires THEN system SHALL DM the first allowed user
  - System SHALL support sending to specific user IDs via the notify tool

#### 2.2 Session Status

- **User Story**: As a team member, I want to check agent status
- **Acceptance Criteria**:
  - WHEN user sends `/status` THEN system SHALL reply with session ID, agent name, and queue depth

### Won't Have (This Phase)

- Channel messages (DMs only for v1)
- Multi-workspace support
- Voice/audio messages
- File uploads/downloads
- Interactive buttons/modals
- Webhook mode (Socket Mode only)
- Thread-based conversation branching

---

## Success Metrics

| Metric | Target | Tracking |
|--------|--------|----------|
| Adoption | Module enabled, first message exchanged | Telemetry: `slack:message_received` |
| Quality | Response delivery > 99% | Telemetry: send failures vs attempts |
| Latency | Median response < 30s | Telemetry: receive-to-send timing |

---

## Constraints

### Feature-Specific

| ID | Constraint | Type |
|----|------------|------|
| CON-F1 | Slack safe message limit: 4000 chars | Platform |
| CON-F2 | No typing indicator API for bots | Platform |
| CON-F3 | Slash commands must be registered in Slack app manifest | Platform |
| CON-F4 | Rate limit: 1 msg/sec/channel for chat.postMessage | Platform |
| CON-F5 | Two tokens required (bot + app-level) | Security |
| CON-F6 | Socket Mode requires `connections:write` scope for app token | Platform |

---

## Risks and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Token exposure | High | Low | Env vars only, never logged |
| Slack API downtime | Medium | Low | Graceful dormancy, auto-reconnect by SDK |
| Rate limiting (429) | Medium | Medium | Per-channel send queue with backoff |
| WebSocket disconnect | Medium | Medium | slack-bolt auto-reconnects Socket Mode |
| Bot message loop | High | Low | Skip messages with `bot_id` field |
