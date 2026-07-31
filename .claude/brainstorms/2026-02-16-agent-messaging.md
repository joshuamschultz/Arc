---
topic: Human-Agent Bidirectional Messaging
date: 2026-02-16
status: deepened
deepened: 2026-02-16
---

## Deepening Summary

**Deepened on:** 2026-02-16
**Sections enhanced:** 7
**Research agents:** 4 (Telegram best practices, edge cases/security, AI+Telegram OSS projects, ArcAgent integration analysis)
**Skills matched:** api-designer, architecture-adr-generator

### Key Findings

1. **Telegram Bot API is the clear winner** -- Free, zero per-message cost, excellent async Python library (python-telegram-bot v20+), built-in job scheduler, and mature ecosystem with 18k+ star reference implementations
2. **ArcAgent already has the right seams** -- `agent.chat(message, session_id=...)` is the exact interface needed; Module Bus supports proactive messaging via event subscriptions; existing `_serve` mode keeps agent warm for 24/7 operation
3. **Critical gap: single-session SessionManager** -- Current `SessionManager` holds one active session at a time; concurrent Telegram chats need either per-chat ArcAgent instances or session-id-per-call pattern (which re-loads from JSONL each time)
4. **Day-1 architecture is ~200 lines** -- Telegram polling + `agent.chat()` + chat_id-to-session_id mapping. No FastAPI, no webhooks, no Redis needed initially

### New Risks Discovered

- **Proactive messaging requires user opt-in**: Telegram bots cannot message users who haven't sent `/start` first -- design onboarding flow accordingly
- **4,096 char message limit**: Agent responses must be chunked; LLM outputs can easily exceed this
- **Bot token = full access**: Single credential controls entire bot -- must be vault-backed per ArcAgent security posture
- **Webhook/polling mutual exclusion**: Cannot use both simultaneously -- architecture decision locks in one approach

---

# Human-Agent Bidirectional Messaging

## Inspiration

ArcAgent is already built -- the core agent with tools, memory, policy, and loop execution exists. The missing piece is a **phone-accessible interface** so Josh can text the agent from his phone and get responses back, like texting a human assistant. This includes both on-demand conversations (asking the agent to do things) and proactive notifications (agent reaching out after cron-triggered work completes).

Decision: **Telegram Bot API** -- simplest, free, great mobile UX.

### Research Insights

**Why Telegram wins:**
- Free Bot API, no per-message costs (Twilio charges ~$0.0079/SMS)
- No phone number required (unlike Signal, WhatsApp, Twilio)
- python-telegram-bot v22.6 is fully async (asyncio), aligns with ArcAgent's async-first design
- Built-in job queue (APScheduler wrapper) for cron-like scheduling
- 18k+ star reference implementation: [chatgpt-telegram-bot](https://github.com/n3d1117/chatgpt-telegram-bot)
- Local dev: just use polling mode, no HTTPS/ngrok needed

**Bot API vs MTProto:**

| Criteria | Bot API (recommended) | MTProto (Pyrogram/Telethon) |
|----------|----------------------|---------------------------|
| Setup | 5 min via BotFather | Complex, phone number needed |
| Use case | Bot interactions | User automation, scraping |
| Reliability | Via Telegram proxy | Direct connection |
| For ArcAgent | Sufficient | Overkill |

---

## Projects

- **Messaging gateway**: A thin layer that bridges Telegram Bot API to ArcAgent's `agent.chat()` interface
- **Hosted ArcAgent**: First on local Mac, then AWS -- the agent needs to be running and reachable
- **Multi-tenant messaging**: Eventually a product where multiple users each text their own ArcAgent instance

### Research Insights

**Architecture patterns from OSS projects:**

1. **Direct integration (Day 1)**: Bot handler calls `agent.chat()` directly. Simplest. Used by most tutorial bots.
2. **Queue-based (Scale)**: Bot enqueues message -> worker processes via agent -> bot sends reply. Used by [django-telegram-bot](https://github.com/ohld/django-telegram-bot) (Django + Celery + Redis + PostgreSQL).
3. **Webhook relay (Production)**: Telegram webhook -> FastAPI -> agent. Better for high-traffic.

**Recommendation:** Start with direct integration (polling mode). Graduate to webhook + queue when moving to AWS/multi-tenant.

**Existing reference projects:**
- [chatgpt-telegram-bot](https://github.com/n3d1117/chatgpt-telegram-bot) -- 18k stars, conversation summarization, plugin system, streaming
- [django-telegram-bot](https://github.com/ohld/django-telegram-bot) -- Full-stack production template with Celery
- [telegram-llm-bot](https://github.com/ma2za/telegram-llm-bot) -- Weaviate vector search + MongoDB

---

## Audience

- **Day 1**: Josh -- single user, personal agent, phone-based interaction
- **Eventually**: Multi-tenant product -- anyone can spin up an ArcAgent and text it from their phone

### Research Insights

**Multi-tenant session isolation pattern:**
```
Session key: agent:<user_id>:telegram:<chat_type>:<chat_id>
```
- Namespace all state by user_id to prevent cross-tenant leakage
- Redis with key prefixes for fast, isolated session access
- Deep linking for user onboarding: `https://t.me/bot?start=<user_token>`

---

## Use Cases

- **On-demand tasks**: Text "run the SEO audit" or "check server status" and get results back as a text message
- **Questions about work**: "What did we work on last?" / "Status of feature X?" -- agent checks memory and responds
- **Proactive notifications**: Agent runs a cron job, finishes, and texts Josh: "Build complete, 3 warnings" or "Found 5 new leads"
- **Conversational flow**: Multi-turn back-and-forth, not just single commands -- agent maintains context across messages
- **Async work**: Send a task, agent works on it, texts back when done (minutes or hours later)

### Research Insights

**Proactive messaging constraints:**
- Bots CANNOT message users who haven't sent `/start` first -- this is a hard platform rule
- Must store `chat_id` on first contact for later proactive messaging
- If user blocks bot, sends will fail silently -- handle gracefully
- ArcAgent's scheduler module already fires `schedule:completed` events -- a Telegram module subscribes to these and sends results to the stored `chat_id`

**Long-running task pattern:**
```
1. User sends task
2. Bot immediately replies "Working on it..."  (send_chat_action TYPING)
3. Agent processes via agent.chat() or agent.run()
4. Bot sends result when done
```
- Typing indicator expires after ~5 seconds -- for tasks >5s, send explicit "Processing..." message
- Use `Application.create_task()` for background work so bot stays responsive
- CPU-bound work (ML inference) should use `ProcessPoolExecutor`

**Message size handling:**
- Telegram limit: 4,096 characters per message
- Agent responses can easily exceed this -- implement smart chunking at paragraph/sentence boundaries
- For very long responses, send as text file attachment instead

---

## Desired Outcomes

- **Text-native interaction**: Feels like texting a knowledgeable assistant, not using an app or dashboard
- **Always reachable**: Agent is running 24/7, can be reached from phone anytime
- **Proactive communication**: Agent doesn't just respond -- it initiates when it has something to report
- **Zero friction**: No app to install (beyond whatever messenger), no web UI to open, just text

---

## Guiding Principles

- **Simplest path**: Least infrastructure, fastest to get working. Polish later.
- **Cheapest to run**: Minimal ongoing costs, avoid per-message fees where possible
- **ArcAgent is the brain**: The messaging layer is just a transport -- all intelligence stays in ArcAgent
- **Multi-tenant ready**: Even if day-1 is single user, don't paint yourself into a corner

---

## Constraints

- **Decided**: Telegram Bot API
- Hosting: local Mac first, then AWS
- Must integrate with existing ArcAgent session/conversation model
- Federal compliance context from CLAUDE.md (security, audit trails) applies to production but not necessarily day-1 prototype

### Research Insights

**Telegram-specific constraints:**
- Bot token is THE authentication credential -- anyone with it controls the bot
- Rate limits: 1 msg/sec per chat, 20 msg/min per group, 30 msg/sec broadcast
- Webhook requires HTTPS (TLS 1.2+), ports 443/80/88/8443 only
- Webhooks and getUpdates are mutually exclusive (409 error if mixed)
- Updates stored server-side for max 24 hours if undelivered
- File downloads: 20MB max via bot API, uploads: 50MB max

**ArcAgent integration constraints (from codebase analysis):**

| ArcAgent Interface | Telegram Maps To |
|-------------------|-----------------|
| `agent.chat(msg, session_id=...)` | Incoming Telegram message |
| `result.content` | Outgoing Telegram reply |
| `session_id` (UUID4) | Mapped from `chat_id` (int) |
| Module Bus `schedule:completed` | Proactive notification trigger |
| `agent.startup()` / `agent.shutdown()` | Bot lifecycle |
| `_serve` mode | 24/7 warm agent pattern |

**Gap: SessionManager is single-session-at-a-time.** The `SessionManager` holds one `_session_id` and one `_messages` list. Each `resume_session()` replaces these. For concurrent Telegram chats:
- Option A: One ArcAgent instance per chat (heavy, ~50MB baseline per agent)
- Option B: Use `session_id` param to `chat()` -- it re-loads from JSONL each time (current behavior, works but slower)
- Option C: Refactor SessionManager for concurrent sessions (future)

**Day-1 recommendation:** Option B is fine for single user. The JSONL reload adds milliseconds, negligible compared to LLM latency.

---

## Scope

**In**:
- Bidirectional text messaging (human <-> agent)
- Proactive agent-initiated messages (cron/event-triggered)
- Multi-turn conversational context
- Basic hosting (serve ArcAgent with messaging endpoint)

**Out** (for now):
- Voice calls or audio messages
- Rich media (images, files) -- text only initially
- Group chats (multi-user in single thread)
- End-to-end encryption at transport layer (rely on Telegram's built-in)
- Web UI or dashboard

---

## ArcAgent Integration Architecture

### Entry Points

```python
# Setup (from codebase analysis of agent.py)
from arcagent.core.agent import ArcAgent
from arcagent.core.config import load_config

config = load_config(Path("arcagent.toml"))
agent = ArcAgent(config, config_path=Path("arcagent.toml"))
await agent.startup()

# For each incoming Telegram message:
result = await agent.chat(
    message=text,                    # str: user's message
    session_id=session_id_for_chat,  # str | None: mapped from chat_id
)
reply_text = result.content          # str | None: agent's response

# Metadata:
result.cost_usd        # float
result.turns           # int
result.tool_calls_made # int

# Shutdown
await agent.shutdown()
```

### Module Bus Events for Proactive Messaging

A Telegram module implementing the `Module` protocol would:
1. Subscribe to `schedule:completed` -- relay cron results to stored chat_id
2. Subscribe to `agent:post_respond` -- intercept responses for routing
3. Register `send_telegram_message` tool -- let agent proactively text the user
4. Run background event loop for Telegram polling/webhook

### Config Location

```toml
# arcagent.toml
[modules.telegram]
enabled = true

[modules.telegram.config]
bot_token_vault_key = "telegram/bot-token"   # Vault-backed, not plaintext
allowed_chat_ids = [123456789]               # Allowlist for day-1
```

### Files to Create

```
arcagent/modules/telegram/
    MODULE.yaml          # Module metadata (convention-based loader)
    __init__.py          # TelegramModule class (Module protocol)
    bot.py               # Telegram bot setup, handlers, polling/webhook
    config.py            # TelegramConfig (Pydantic model)
```

---

## Security Considerations

### Bot Token Management
- Token format: `1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ`
- Must be vault-backed per ArcAgent security posture (`VaultConfig` at config.py:53-57)
- Rotate via BotFather periodically
- Never log, never commit to source control

### Webhook Security (Production)
- Set `secret_token` in `setWebhook` -- Telegram sends `X-Telegram-Bot-Api-Secret-Token` header
- Verify header before processing ANY payload
- Allowlist Telegram's IP ranges: `149.154.160.0/20`, `91.108.4.0/22`
- TLS 1.2+ mandatory, Let's Encrypt for free certs

### User Authorization
- Day-1: Allowlist `chat_id` values in config
- Production: Deep linking + OTP for user onboarding
- Telegram doesn't validate commands -- always validate server-side

### Data Privacy
- Privacy mode ON by default in groups (bot only sees commands + replies to it)
- Store minimal user data; implement retention/deletion policies
- Bots can't see messages from other bots (hard platform rule)

---

## Edge Cases & Gotchas

### Critical (Will Break Things)
1. **4,096 char message limit** -- Agent responses MUST be chunked. Don't rely on Telegram's auto-split.
2. **Webhook/polling mutual exclusion** -- 409 error if both active. Call `deleteWebhook` before switching.
3. **Bot can't message first** -- User must `/start` before proactive messaging works.
4. **Token exposure = full compromise** -- Single credential, no scoping.

### Important (Will Cause Bugs)
5. **Markdown parsing is fragile** -- Use HTML parse mode instead of Markdown/MarkdownV2. Markdown has "many problems with Telegram server support."
6. **UTF-16 entity offsets** -- Emoji/special chars count as 2 units for offset calculation, even though messages are UTF-8.
7. **429 rate limits block ALL calls** -- When triggered, even `getMe` fails for `retry_after` seconds.
8. **Webhook retries on 500** -- If endpoint returns 500, Telegram retries every 60s for 8 attempts. Always return 200, even on processing errors.
9. **Connection pool mismatch** -- python-telegram-bot defaults: 4,096 concurrent updates but only 128 connection pool. Can cause timeouts.

### Nice to Know
10. **callback_data limit**: 64 bytes per inline button. Use server-side storage + hash for complex data.
11. **Updates expire after 24 hours** -- If bot is down >24h, messages are lost.
12. **In-memory state lost on restart** -- Use persistent storage from day 1.
13. **Group chat_id != private chat_id** -- Same user has different IDs in different contexts.

---

## Cost Analysis

### Day-1 (Single User, Local Mac)
| Item | Cost |
|------|------|
| Telegram Bot API | Free |
| python-telegram-bot | Free (OSS) |
| Local hosting | Free (Mac already running) |
| LLM API calls | Existing ArcLLM costs |
| **Total** | **$0/month** |

### Production (AWS, Multi-Tenant)
| Item | Cost |
|------|------|
| Telegram Bot API | Free |
| EC2/ECS instance | $5-20/month |
| Redis (session state) | $0-15/month |
| SSL certificate | Free (Let's Encrypt) |
| LLM API calls | Variable per user |
| **Total** | **~$5-35/month + LLM** |

### Scaling Note
A well-optimized async bot handles ~1,000 concurrent users on a $5/month VPS. Telegram's free broadcast limit is 30 msg/sec. Paid broadcasts (1,000 msg/sec) cost 0.1 Stars per message.

---

## Development Workflow

### Local Dev (Polling)
```python
# No HTTPS, no ngrok, no webhook setup needed
application = Application.builder().token(TOKEN).build()
application.add_handler(MessageHandler(filters.TEXT, handle_message))
application.run_polling()
```

### Production (Webhook + FastAPI)
```python
# Requires HTTPS, domain, reverse proxy
ptb = Application.builder().token(TOKEN).updater(None).build()
# FastAPI endpoint receives webhook POST
# Verify X-Telegram-Bot-Api-Secret-Token header
# Process update via ptb.process_update()
```

### Testing Strategy
- **Unit (70%)**: Mock Telegram API, test `agent.chat()` integration logic
- **Integration (20%)**: [telegram-test-api](https://github.com/jehy/telegram-test-api) mock server
- **E2E (10%)**: Separate test bot via BotFather, real Telegram

---

## Resolved

- **Which messenger?** Telegram Bot API -- free, simple, great mobile UX
- **Who is this for?** Josh day-1, multi-tenant product eventually
- **Where hosted?** Local Mac first, AWS later
- **What matters most?** Simplicity and cost over privacy or UX polish
- **Which Python library?** python-telegram-bot v20+ (async, mature, well-documented)
- **Polling or webhooks?** Polling for local dev, webhooks for production
- **How does it integrate?** `agent.chat(message, session_id=mapped_from_chat_id)`
- **Proactive messaging?** Subscribe to Module Bus `schedule:completed` events

## Related Solutions

- No existing solutions in archive.

## References

- [Telegram Bot API docs](https://core.telegram.org/bots/api)
- [python-telegram-bot docs](https://docs.python-telegram-bot.org/)
- [chatgpt-telegram-bot](https://github.com/n3d1117/chatgpt-telegram-bot) (18k stars, best reference)
- [django-telegram-bot](https://github.com/ohld/django-telegram-bot) (production template)
- [Telegram Webhook Guide](https://core.telegram.org/bots/webhooks)
- [grammY Flood Limits](https://grammy.dev/advanced/flood)
- [FreeCodeCamp: Deploy python-telegram-bot v20 Webhook](https://www.freecodecamp.org/news/how-to-build-and-deploy-python-telegram-bot-v20-webhooks/)
