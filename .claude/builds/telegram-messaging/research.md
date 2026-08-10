# telegram-messaging — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-160–D-173 (14 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Feature: Telegram Messaging Module

**Date**: 2026-02-16
**Source**: `.claude/brainstorms/2026-02-16-agent-messaging.md` (deepened)
**Goal**: Bidirectional text messaging between human (phone) and ArcAgent via Telegram Bot API

#### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|

#### Key Design Principles

- **Module, not core**: Telegram is opt-in. Module Bus participant. Removable without touching nucleus.
- **Polling simplicity**: No HTTPS, no webhooks, no domain. Just start the agent and text it.
- **ArcAgent is the brain**: Telegram is just transport. All intelligence stays in agent.chat().
- **Deferred binding**: Module gets agent.chat() callback after startup, same proven pattern as scheduler.
- **Sequential processing**: One message at a time via asyncio.Queue. No race conditions.

#### Components to Build

1. **`arcagent/modules/telegram/`** — Module directory
   - `MODULE.yaml` — Module manifest (entry_point, dependencies)
   - `__init__.py` — TelegramModule class (Module protocol)
   - `bot.py` — Telegram bot setup, polling loop, message handlers
   - `config.py` — TelegramConfig (Pydantic model)

2. **`arcagent/core/config.py`** — Add TelegramConfig under modules

3. **`arcagent/core/agent.py`** — Wire `set_agent_chat_fn()` for Telegram module (same pattern as scheduler)

4. **Tests**
   - `tests/unit/modules/telegram/` — Mock bot API tests
   - `tests/integration/` — Real agent + mocked Telegram

#### Architecture Diagram

```
arc agent serve
    |
    v
ArcAgent (warm, long-running)
    |
    v
TelegramModule (Module Bus participant)
    |
    +-- startup(): start polling loop (asyncio.create_task)
    +-- set_agent_chat_fn(agent.chat) ← wired by agent.py
    +-- subscribes to schedule:completed for proactive notifications
    |
    v (inbound message from Telegram)
    |
    +-- verify chat_id in allowlist
    +-- send TYPING chat action
    +-- queue message in asyncio.Queue
    +-- process sequentially: await agent_chat_fn(text, session_id=current_session)
    +-- smart-split result.content at paragraph boundaries
    +-- send response(s) via bot.send_message()
    |
    v (proactive / cron notification)
    |
    +-- schedule:completed event fires on Module Bus
    +-- TelegramModule handler extracts result
    +-- send notification to stored chat_id
```

#### Telegram Bot Commands

| Command | Action |
|---------|--------|
| `/start` | Register chat, store chat_id, create first session |
| `/new` | Start fresh session (new session_id) |
| `/status` | Show current session info, agent status |
| Free text | Route to agent.chat() |

#### Config Schema

```toml
[modules.telegram]
enabled = true
priority = 100

[modules.telegram.config]
allowed_chat_ids = []          # Empty = accept none. Must configure.
poll_interval = 1.0            # Seconds between getUpdates calls
max_message_length = 4096      # Telegram limit
```

Token via environment: `ARCAGENT_TELEGRAM_BOT_TOKEN`

#### Open Questions

None — all decisions resolved through interactive build session.

#### Related Solutions

- Scheduler module (same deferred binding pattern, same asyncio task lifecycle)
- OpenClaw Telegram integration (architecture reference)

---

---
