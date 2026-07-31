# SPEC-011: Slack Messaging Module

| Field | Value |
|-------|-------|
| **ID** | SPEC-011 |
| **Feature** | Slack Messaging |
| **Type** | Integration (ArcAgent Module) |
| **Status** | COMPLETE |
| **Confidence** | 85% |
| **Route** | Standard (3-phase) |
| **Created** | 2026-02-25 |

## Context

ArcAgent needs a Slack-based human-agent communication module, mirroring the existing Telegram module (S005). This enables users to chat with their agents via Slack DMs, receive proactive notifications, and manage sessions. Uses Slack Bolt SDK (async) with Socket Mode — no public URL needed, making it ideal for Azure GCC deployments behind firewalls.

## Key Decisions

| ID | Decision | Choice |
|----|----------|--------|
| D-001 | Module location | `arcagent/modules/slack/` |
| D-002 | Inbound transport | Socket Mode (WebSocket, no public URL) |
| D-003 | SDK | `slack-bolt` (async) + `aiohttp` |
| D-004 | Agent access | Deferred binding via `agent:ready` event (same as Telegram) |
| D-005 | Sessions | 1 DM = 1 session, `/new` for fresh |
| D-006 | Chat model | 1 bot = N users (allowlist enforced) |
| D-007 | Long messages | Smart split at 4000 chars (Slack safe limit) |
| D-008 | Formatting | Slack mrkdwn (markdown-like) |
| D-009 | Typing indicator | Emoji reaction (:thinking_face:) — Slack has no bot typing API |
| D-010 | Token storage | Two env vars: bot token + app-level token |
| D-011 | Authorization | Slack user ID allowlist in config |
| D-012 | Concurrency | Per-user asyncio.Lock (Slack handles multiple users) |
| D-013 | Commands | Slash commands (/start, /new, /status) via Slack app manifest |
| D-014 | Activation | Auto-start when module enabled in config |

## Prior Work

- **Reference**: `.claude/specs/S005-telegram-messaging/` (Telegram module — same pattern)
- **Codebase**: `arcagent/modules/telegram/` (implemented, working)

## Deepened: 2026-02-25

Research enrichment applied to SDD and PLAN with findings on:
- `connect_async()` not `start_async()` (non-blocking Socket Mode lifecycle)
- `close_async()` not `disconnect_async()` (clean shutdown without orphan tasks)
- `conversations.open` required for proactive DMs (don't pass user ID as channel)
- `@app.event("message")` not `@app.message()` (catches all subtypes)
- 3-second slash command ack timeout — use `asyncio.create_task()` for long-running work
- `response_url` limited to 5 messages / 30 minutes
- Token prefix validation (`xoxb-` for bot, `xapp-` for app-level)
- Minimum OAuth scopes: `im:history`, `im:read`, `im:write`, `chat:write`, `commands`, `reactions:write`, `connections:write`
- Socket Mode reconnection is linear backoff, messages can be lost during disconnect

## Learnings

### Implementation (Phases 1-3)
- Telegram module was an excellent template — nearly 1:1 pattern match
- Socket Mode lifecycle: `connect_async()` returns immediately (non-blocking), `close_async()` for clean shutdown
- `@app.event("message")` is correct handler (not `@app.message()`) — catches all DM subtypes
- `conversations.open(users=user_id)` required for proactive DMs — channel ID is stable and safe to cache
- Token prefix validation (`xoxb-`/`xapp-`) catches misconfiguration early with clear error messages
- No-op handlers for `message_changed`/`message_deleted` prevent WARNING log spam

### Quality Review (Phase 4)
- Coverage: 85% line (exceeds 80% threshold). Uncovered lines are the slack-bolt SDK wiring paths that require the actual SDK at import time
- Extracted `_dispatch_command()` from `_handle_message` to reduce cyclomatic complexity
- Extracted `_attach_mock_app()` test helper to DRY up 10+ identical mock setups
- Optimized `split_message()` sentence boundary search: iterate instead of materializing all regex matches
- Security: zero tokens in source/logs/telemetry, fail-closed auth, 0o600 state file, bot_id loop prevention

### Runbook
- Created `docs/runbooks/slack-setup.md` — step-by-step from Slack app creation to multi-agent deployment
