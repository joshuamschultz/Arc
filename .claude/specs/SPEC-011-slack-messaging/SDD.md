# SDD: Slack Messaging Module

## Solution Strategy

- **Architecture Pattern**: Module Bus participant (same as Telegram, scheduler, memory)
- **Integration Approach**: Deferred binding via `agent:ready` event — module receives `agent.chat()` callback without core knowing about Slack
- **SDK**: `slack-bolt` (async) with Socket Mode — no public URL, works behind firewalls
- **Reference Implementation**: `arcagent/modules/telegram/` — identical lifecycle pattern

---

## Building Block View

### System Context

```
User (Slack App)
    ↕ WebSocket (Socket Mode)
SlackModule
    → agent.chat() → ArcAgent → LLM
    ← response ← ArcAgent
    → client.chat_postMessage() → Slack API → User
```

### Component Diagram

```
packages/arcagent/
└── arcagent/
    └── modules/
        └── slack/
            ├── __init__.py    # SlackModule class (Module protocol)
            ├── bot.py         # SlackBot — Socket Mode, handlers, response delivery
            ├── config.py      # SlackConfig (Pydantic)
            └── MODULE.yaml    # Module manifest
```

---

## Interface Specifications

### SlackConfig (Pydantic)

```python
class SlackConfig(ModuleConfig):
    """Config from [modules.slack.config] in arcagent.toml."""

    enabled: bool = False
    allowed_user_ids: list[str] = []       # Slack user IDs (U...). Empty = deny all.
    max_message_length: int = 4000         # Slack safe limit
    bot_token_env_var: str = "ARCAGENT_SLACK_BOT_TOKEN"     # noqa: S105
    app_token_env_var: str = "ARCAGENT_SLACK_APP_TOKEN"     # noqa: S105
```

### SlackModule Class

```python
class SlackModule:
    """Slack messaging module — Module Bus participant."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None: ...

    @property
    def name(self) -> str:
        return "slack"

    async def startup(self, ctx: ModuleContext) -> None:
        """Subscribe to events, create bot, register tools, start Socket Mode."""

    async def shutdown(self) -> None:
        """Stop Socket Mode, clean up."""
```

### SlackBot Class

```python
class SlackBot:
    """Manages Socket Mode connection, message routing, response delivery."""

    def __init__(
        self,
        config: SlackConfig,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None: ...

    def set_agent_chat_fn(self, fn: Callable[..., Awaitable[Any]]) -> None:
        """Bind agent.chat() callback (deferred binding)."""

    async def start(self) -> None:
        """Start async Socket Mode handler in background task."""

    async def stop(self) -> None:
        """Stop Socket Mode handler gracefully."""

    async def send_notification(self, text: str) -> None:
        """Send proactive DM to the stored user."""
```

### Message Splitting

Copy `split_message()` from Telegram (module stays self-contained) with 4000 char limit:

```python
def split_message(text: str, max_length: int = 4000) -> list[str]:
    """Split text respecting paragraph > newline > sentence > hard boundaries."""
```

### MODULE.yaml

```yaml
name: slack
version: 0.1.0
description: Bidirectional Slack messaging for human-agent interaction via Socket Mode
author: ArcAgent
entry_point: arcagent.modules.slack:SlackModule
dependencies:
  - arcagent.core.module_bus
  - arcagent.core.config
events:
  subscribes:
    - agent:shutdown
    - agent:ready
    - schedule:failed
  emits:
    - slack:message_received
    - slack:message_sent
    - slack:notification_sent
    - slack:auth_rejected
    - slack:connected
    - slack:disconnected
    - slack:error
```

### TOML Config Example

```toml
[modules.slack]
enabled = true
priority = 100

[modules.slack.config]
allowed_user_ids = ["U12345678", "U87654321", "U11111111"]
max_message_length = 4000
```

Tokens via environment:
```bash
export ARCAGENT_SLACK_BOT_TOKEN=xoxb-...
export ARCAGENT_SLACK_APP_TOKEN=xapp-...
```

---

## Runtime View

### Primary Flow: Inbound DM

```
1. User sends DM to bot in Slack
2. Slack delivers event via Socket Mode WebSocket
3. SlackBot.handle_message() fires (via @app.event("message"))
4. Skip if event has bot_id (prevent loops)
5. Check user_id against allowed_user_ids (deny if not in list)
6. Check for text commands: 'start', 'new', 'status'
7. Acquire asyncio.Lock (serialize messages)
8. Call agent.chat(text, session_id=current_session)
9. Split response if > 4000 chars
10. Send response as regular DM reply(s) via chat_postMessage
11. Release lock
12. Emit slack:message_sent telemetry event
```

### Proactive Notification Flow

```
1. schedule:failed event fires on Module Bus
2. SlackModule._on_schedule_failed() handler fires
3. SlackBot.send_notification(error_summary)
4. Bot opens DM with first allowed user (or specified user)
5. Posts message via client.chat_postMessage()
```

### Session Management

Single-user model (1 agent = 1 user = 1 session), mirroring Telegram:
- Session state stored in `{workspace}/slack/state.json` with `{user_id, session_id}`
- Text command 'start' → create new session
- Text command 'new' → rotate session_id
- Text command 'status' → show current session info
- First authorized user who messages gets stored as the active user

---

## Error Handling

| Error | Response | Recovery |
|-------|----------|----------|
| No bot token | Module stays dormant, logs warning | Set env var, restart |
| No app token | Module stays dormant, logs warning | Set env var, restart |
| Unauthorized user | Silent ignore, audit log | Add user to allowlist |
| agent.chat() failure | DM error message to user | Retry on next message |
| Slack API error | Log, emit slack:error | Auto-retry by SDK |
| WebSocket disconnect | Log, auto-reconnect | Handled by slack-bolt |
| Rate limit (429) | Backoff, retry | Respect Retry-After header |
| Bot message loop | Skip (check bot_id) | Automatic |

---

## Architecture Decisions

### ADR-1: Socket Mode Over Events API

- **Decision**: Socket Mode (WebSocket) only. No HTTP Events API.
- **Rationale**: Azure GCC may not allow inbound HTTP. Socket Mode establishes an outbound WebSocket — works behind firewalls, NAT, and restricted networks. No public URL needed.
- **Trade-off**: Single connection point (vs distributed HTTP). slack-bolt supports multiple simultaneous Socket Mode connections for HA.

### ADR-2: Single-User Model (Build Decision)

- **Decision**: Single-user per agent (1 agent = 1 user = 1 session), mirroring Telegram.
- **Rationale**: Each agent has its own Slack bot connection. The 3-agent setup has 1 user per agent. No need for per-user session dicts.
- **Trade-off**: Can't serve multiple users from one agent. If needed later, upgrade to per-user dicts (dict instead of single var).

### ADR-3: No Processing Indicators (Build Decision)

- **Decision**: No emoji reactions. Just process and reply.
- **Rationale**: Fewer API calls (3 fewer per message), less code, less that can break. The response appearing IS the feedback.
- **Trade-off**: No visual feedback during long LLM calls (30+ seconds). Acceptable for 1:1 DM where user expects async response.

### ADR-4: Regular DM Replies (Build Decision)

- **Decision**: Regular DM replies, no threads.
- **Rationale**: In a 1:1 DM, threading adds unnecessary clicks. Flat conversation like texting is more natural.
- **Trade-off**: Long multi-message responses appear inline. Acceptable for the 1:1 use case.

### ADR-5: Text Commands Over Slash Commands (Build Decision)

- **Decision**: Use text commands ('start', 'new', 'status') instead of slash commands.
- **Rationale**: No Slack app manifest changes needed. No 3-second ack timeout complexity. No response_url limits. Simpler setup for new users.
- **Trade-off**: No command completion in Slack UI. Users must know the commands. Acceptable for 3-person team.

---

## Quality Requirements

| Aspect | Threshold | Feature Target |
|--------|-----------|----------------|
| Line Coverage | >= 80% | >= 85% |
| Branch Coverage | >= 75% | >= 75% |
| Ruff Errors | 0 | 0 |
| mypy Errors | 0 | 0 |
| Complexity | <= 10/fn | <= 8/fn |

---

## Test Specifications

### Critical Scenarios

**Scenario 1: Happy Path — DM**
```gherkin
Given: SlackModule started with valid tokens and agent_chat_fn bound
And: user_id is in allowed_user_ids
When: User sends "hello" as DM
Then: Module calls agent.chat("hello", session_id=<session>)
And: Response sent as regular DM reply
And: slack:message_received event emitted
```

**Scenario 2: Unauthorized User**
```gherkin
Given: SlackModule started with allowed_user_ids = ["U111"]
When: DM arrives from user U999
Then: Message silently ignored
And: slack:auth_rejected event emitted
And: No agent.chat() call
```

**Scenario 3: Long Response**
```gherkin
Given: agent.chat() returns 6000 chars
When: Module sends response
Then: split_message() produces 2 chunks (<= 4000 each)
And: Both sent as regular DM replies
```

**Scenario 4: No Tokens**
```gherkin
Given: ARCAGENT_SLACK_BOT_TOKEN not set
When: SlackModule starts
Then: Module logs warning and stays dormant
And: No WebSocket connection attempted
```

**Scenario 5: Bot Message Loop Prevention**
```gherkin
Given: SlackModule is running
When: Message arrives with bot_id field set
Then: Message is skipped entirely
And: No agent.chat() call
```

**Scenario 6: Proactive Notification**
```gherkin
Given: SlackModule subscribed to schedule:failed
When: schedule:failed event fires
Then: Module DMs error summary to first allowed user
And: slack:notification_sent event emitted
```

---

## Dependencies

- `slack-bolt >= 1.20.0` (async support)
- `aiohttp` (required for async Socket Mode)

Both as optional dependencies: `pip install 'arcagent[slack]'`

---

## Research Insights

### Socket Mode Lifecycle (Critical)

- **Use `connect_async()`, NOT `start_async()`**: `AsyncSocketModeHandler.start_async()` blocks the event loop. Use `connect_async()` which returns immediately and runs in the background.
- **Use `close_async()` for shutdown**: NOT `disconnect_async()` which leaves orphan asyncio tasks. `close_async()` properly cleans up all resources.
- **Reconnection is linear backoff** (not exponential). Socket Mode reconnects automatically but uses linear delay.
- **Messages CAN be lost during disconnect**: Socket Mode has no durable queue. If the WebSocket drops, in-flight messages are lost. This is acceptable for chat but worth noting.

### DM Handling (Critical)

- **Use `conversations.open` for proactive DMs**: Do NOT pass a user ID directly as the `channel` parameter to `chat_postMessage`. Instead, call `conversations.open(users=user_id)` first to get the DM channel ID, then post to that channel.
- **Use `@app.event("message")`, NOT `@app.message()`**: The `@app.message()` decorator misses some message subtypes. `@app.event("message")` catches all DM events including `message_changed` (which has `hidden: true`).
- **Skip bot messages by checking `bot_id` field**: Not `subtype == "bot_message"` — some bot messages don't have that subtype. Check for `event.get("bot_id")` instead.

### Slash Commands (Critical)

- **3-second ack timeout**: Slack requires slash command acknowledgment within 3 seconds. For long-running operations (like `agent.chat()`), ack immediately and use `asyncio.create_task()` to process asynchronously.
- **`response_url` limited to 5 messages / 30 minutes**: Don't use `response_url` for ongoing updates. Use `chat_postMessage` to the DM channel instead.

### OAuth Scopes (Minimum Required)

```
Bot Token Scopes:
- im:history    (read DM messages)
- im:read       (view DM info)
- im:write      (open DMs)
- chat:write    (send messages)

App-Level Token:
- connections:write (Socket Mode)
```

Note: `commands` and `reactions:write` scopes NOT needed (build decisions: text commands, no reactions).

### Rate Limiting

- **`chat.postMessage`: 1 message/sec/channel** — the per-user asyncio.Lock naturally rate-limits since we wait for agent.chat() between messages.
- **`reactions.add` / `reactions.remove`**: Tier 3 rate limits (50+ per minute). Not a concern for our use case.
- **Retry handler**: slack-bolt has a built-in `AsyncRetryHandler` that can be configured for automatic retry on 429s. Consider enabling it.

### Token Format Validation

- **Bot token**: Starts with `xoxb-`
- **App-level token**: Starts with `xapp-`
- These prefixes can be validated at startup to give clear error messages if tokens are swapped.

### Event Handler Concurrency (Critical — Confirmed)

- **Handlers fire concurrently**: slack-bolt uses `asyncio.ensure_future()` per incoming message in `process_message()`. Multiple messages arriving simultaneously are dispatched as concurrent tasks.
- **asyncio.Lock IS required**: Without it, concurrent `agent.chat()` calls can interleave. The single Lock serializes all message processing, which is correct for the single-user model.
- **`connect_async()` returns `None`**: No return value to check. Wrap in try/except to catch initial connection failures. Reconnection is handled internally by slack-bolt.

### Bot Mention Stripping

- In DMs, bot mentions (`<@U123BOTID>`) may be prepended to messages when the user @-mentions the bot.
- Strip with `re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()` before processing text commands or passing to `agent.chat()`.
- This is defensive — in 1:1 DMs mentions are less common, but users may copy-paste from channels.

### Unhandled Event Types

- Unhandled Slack events (e.g., `message_changed`, `message_deleted`) trigger a WARNING log + silent 200 ack. They do NOT crash the handler.
- Consider registering no-op handlers for `message_changed` and `message_deleted` to suppress WARNING logs and make the log cleaner.
- **Known bug**: `ignoring_self_events` middleware does NOT filter `message_deleted` events from the bot's own message deletions. Register an explicit skip for these.

### Network Requirements

- Socket Mode only needs **outbound port 443** (WSS over TLS) to `wss-*.slack.com`.
- No inbound ports, no public URLs. Works behind firewalls and NAT.
- If deploying behind a proxy: proxy must support WebSocket upgrade. Exempt `wss-*.slack.com` from SSL inspection.

### DM Channel ID Stability

- `conversations.open` returns a stable DM channel ID — it doesn't change for a given user pair.
- Safe to cache indefinitely in `_dm_channel_id`.
- Handle `channel_not_found` error as cache invalidation signal (re-call `conversations.open`).
