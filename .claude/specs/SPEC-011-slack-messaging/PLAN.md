# PLAN: Slack Messaging Module (SPEC-011)

**Spec**: SPEC-011
**Status**: COMPLETE
**Route**: Standard (3 phases + quality)
**Estimated LOC**: ~280 new lines across 8 files

---

## What We're NOT Doing

- Events API / Webhook mode (Socket Mode only)
- Channel messages (DMs only)
- Multi-workspace support
- Voice, audio, file uploads
- Interactive buttons, modals, Block Kit
- Thread replies (flat DM conversation)
- Emoji reactions as typing indicator
- Slash commands (text commands instead)
- Per-user session management (single-user model)

---

## Build Decisions Applied

Key simplifications from `/build` session (see decisions-log.md):
- **Single-user model**: 1 agent = 1 user = 1 session (mirrors Telegram)
- **No thread replies**: Regular DM replies, flat conversation
- **No emoji reactions**: Response appearing IS the feedback
- **Text commands**: 'start', 'new', 'status' as text (no slash commands, no manifest)
- **Inline Lock**: asyncio.Lock instead of Queue + background task
- **Duplicate split_message**: Module stays self-contained (DRY at N=3)
- **Tool name**: `slack_notify_user` (prefixed to avoid collision)
- **Token validation**: Check `xoxb-`/`xapp-` prefixes at startup
- **DM channel caching**: Cache channel ID from `conversations.open`

---

## Specification References

- **PRD**: `.claude/specs/SPEC-011-slack-messaging/PRD.md`
- **SDD**: `.claude/specs/SPEC-011-slack-messaging/SDD.md`
- **Reference**: `arcagent/modules/telegram/` (identical pattern)
- **Build Decisions**: `.claude/decisions-log.md` (SPEC-011 section)

---

## Phase 1: Config, Manifest, and Module Skeleton [4 tasks]

- [x] **1.1 Create SlackConfig** `[parallel: true]`
  - File: `packages/arcagent/src/arcagent/modules/slack/config.py`
  - Implementation:
    - Inherit from `ModuleConfig`
    - Fields: `enabled`, `allowed_user_ids` (list[str]), `max_message_length` (4000), `bot_token_env_var`, `app_token_env_var`
  - _Leverage: `arcagent/modules/telegram/config.py`_

- [x] **1.2 Create MODULE.yaml** `[parallel: true]`
  - File: `packages/arcagent/src/arcagent/modules/slack/MODULE.yaml`
  - Implementation:
    - name: slack, version: 0.1.0
    - entry_point: `arcagent.modules.slack:SlackModule`
    - events: subscribes (agent:shutdown, agent:ready, schedule:failed), emits (slack:*)
  - _Leverage: `arcagent/modules/telegram/MODULE.yaml`_

- [x] **1.3 Create SlackModule skeleton**
  - File: `packages/arcagent/src/arcagent/modules/slack/__init__.py`
  - Implementation:
    - Constructor with convention-based DI (config, telemetry, workspace)
    - `name` property -> `"slack"`
    - `startup(ctx)` stub — subscribe to events, create bot
    - `shutdown()` stub
    - Register `slack_notify_user` tool (prefixed name)
    - `agent:ready` handler for deferred binding
    - `schedule:failed` handler for notifications
    - `__all__` export
  - _Leverage: `arcagent/modules/telegram/__init__.py`_
  - _Blocked by: 1.1_

- [x] **1.4 Write config and skeleton tests**
  - Files:
    - `packages/arcagent/tests/unit/modules/slack/__init__.py`
    - `packages/arcagent/tests/unit/modules/slack/conftest.py`
    - `packages/arcagent/tests/unit/modules/slack/test_config.py`
    - `packages/arcagent/tests/unit/modules/slack/test_module.py`
  - Implementation:
    - Config: defaults, extra="forbid", empty allowed_user_ids
    - Module: lifecycle, name property, deferred binding
    - Shared fixtures: mock telemetry, mock ModuleContext, tmp workspace
  - _Leverage: `tests/unit/modules/telegram/conftest.py`_
  - _Blocked by: 1.1, 1.3_

**PAUSE: Verify Phase 1 — module skeleton loads via ModuleLoader, config validates, tests pass.**

---

## Phase 2: Bot Core [5 tasks]

- [x] **2.1 Implement message splitting** `[parallel: true]`
  - File: `packages/arcagent/src/arcagent/modules/slack/bot.py` (initial creation)
  - Implementation:
    - `split_message(text, max_length=4000) -> list[str]`
    - Copy algorithm from Telegram: paragraph > newline > sentence > hard split
    - Module-local copy (self-contained, removable)
  - _Leverage: `arcagent/modules/telegram/bot.py:split_message`_

- [x] **2.2 Write split_message tests** `[parallel: true]`
  - File: `packages/arcagent/tests/unit/modules/slack/test_bot.py`
  - Implementation:
    - Short text, paragraph split, sentence split, hard split, empty, exact 4000, multi-split
  - _Leverage: `tests/unit/modules/telegram/test_bot.py`_

- [x] **2.3 Implement SlackBot class**
  - File: `packages/arcagent/src/arcagent/modules/slack/bot.py`
  - Implementation:
    - Constructor: config, telemetry, workspace
    - `_agent_chat_fn` slot for deferred binding
    - `set_agent_chat_fn()` method
    - Single `asyncio.Lock` for sequential processing (not per-user)
    - Single session state in `{workspace}/slack/state.json`
    - `_load_state()` / `_save_state()` persistence (mirror Telegram pattern)
    - `_dm_channel_id` cache for proactive DMs
  - _Blocked by: 2.1_

- [x] **2.4 Implement Socket Mode handlers**
  - File: `packages/arcagent/src/arcagent/modules/slack/bot.py`
  - Implementation:
    - `start()`:
      - Read tokens from env vars
      - Validate prefixes: `xoxb-` for bot token, `xapp-` for app token
      - Lazy import `slack_bolt` (graceful if not installed)
      - Create `AsyncApp` + `AsyncSocketModeHandler`
      - Register `@app.event("message")` handler (NOT `@app.message()`)
      - Call `handler.connect_async()` (NOT `start_async()`)
    - `stop()`: call `handler.close_async()` (NOT `disconnect_async()`)
    - `_is_authorized(user_id)`: check against allowlist
    - `handle_message(event)`:
      - Skip if `event.get("bot_id")` (prevent loops)
      - Check authorization
      - Check text commands: 'start', 'new', 'status'
      - Acquire asyncio.Lock
      - Call `agent.chat(text, session_id=session)`
      - Split and send as regular DM reply(s) via `chat_postMessage`
    - `_handle_start(event)`: create session, confirm
    - `_handle_new(event)`: rotate session, confirm
    - `_handle_status(event)`: return session info
    - `send_notification(text)`: proactive DM via `_ensure_dm_channel()` + `chat_postMessage`
    - `_ensure_dm_channel(user_id)`: call `conversations.open` once, cache result
  - _Blocked by: 2.3_

- [x] **2.5 Write bot handler tests**
  - File: `packages/arcagent/tests/unit/modules/slack/test_bot.py` (extend)
  - Implementation:
    - Mock slack_bolt's `AsyncApp` and `WebClient`
    - Test DM handler: authorized user -> agent.chat() -> regular DM reply
    - Test unauthorized user -> silently ignored
    - Test bot message (has bot_id) -> skipped
    - Test text commands: 'start', 'new', 'status'
    - Test no tokens -> dormant
    - Test wrong token prefix -> error logged
    - Test `send_notification` -> `conversations.open` + `chat_postMessage`
    - Test `connect_async()` called (not `start_async()`)
    - Test `close_async()` called on stop (not `disconnect_async()`)
    - Test DM channel caching (conversations.open called once, not per-notification)
  - _Blocked by: 2.4_

**PAUSE: Verify Phase 2 — bot handlers work with mocked Slack SDK, tests pass.**

---

## Phase 3: Wiring, Telemetry, and Integration [5 tasks]

- [x] **3.1 Wire SlackModule to SlackBot**
  - File: `packages/arcagent/src/arcagent/modules/slack/__init__.py`
  - Implementation:
    - In `startup()`: create SlackBot, start it, register `slack_notify_user` tool
    - In `shutdown()`: stop bot
    - `agent:ready` -> bind agent.chat() to bot
    - `schedule:failed` -> send notification
  - _Leverage: `arcagent/modules/telegram/__init__.py`_

- [x] **3.2 Add slack-bolt dependency**
  - File: `packages/arcagent/pyproject.toml`
  - Implementation:
    - Add `[project.optional-dependencies] slack = ["slack-bolt>=1.20.0", "aiohttp"]`
  - _Leverage: existing `telegram` optional dependency_

- [x] **3.3 Add telemetry events**
  - File: `packages/arcagent/src/arcagent/modules/slack/bot.py`
  - Implementation:
    - Emit: `slack:message_received`, `slack:message_sent`, `slack:notification_sent`, `slack:auth_rejected`, `slack:connected`, `slack:disconnected`, `slack:error`
    - Use `self._telemetry.record_event()` if available
    - No tokens in event data

- [x] **3.4 Write integration tests**
  - File: `packages/arcagent/tests/integration/test_slack_integration.py`
  - Implementation:
    - Full message flow with mocked Slack WebClient
    - Proactive notification via schedule:failed event
    - Authorization enforcement
    - Session persistence across messages
    - Token prefix validation
  - _Leverage: `tests/integration/test_telegram_integration.py`_

- [x] **3.5 Write module wiring tests**
  - File: `packages/arcagent/tests/unit/modules/slack/test_module.py` (extend)
  - Implementation:
    - Startup -> bot started
    - Shutdown -> bot stopped
    - agent:ready -> chat_fn bound
    - schedule:failed -> notification sent
    - `slack_notify_user` tool registered and callable

**PAUSE: Verify Phase 3 — full integration works, telemetry clean, no token leakage.**

---

## Phase 4: Quality Gates [3 tasks]

- [x] **4.1 Run full quality checks**
  - `pytest --cov=arcagent/modules/slack --cov-report=term-missing`
  - `ruff check arcagent/modules/slack/`
  - `ruff format --check arcagent/modules/slack/`
  - `mypy arcagent/modules/slack/`

- [x] **4.2 Verify module loads via ModuleLoader**
  - ModuleLoader discovers slack MODULE.yaml
  - Entry_point passes prefix validation
  - Constructor DI works (config, telemetry, workspace injected)

- [x] **4.3 Security review**
  - No tokens in source code
  - No tokens in test output or telemetry events
  - All public methods have docstrings
  - Module directory is self-contained and removable
  - bot_id check prevents infinite loops
  - Token prefix validation at startup

---

## Completion Criteria

- [x] All tasks complete (17 total)
- [x] All existing tests pass (0 regressions)
- [x] Coverage >= 80% on slack module (85%)
- [x] Linter clean, type checker clean
- [x] 0 new dependencies on core (slack-bolt is optional)
- [x] Module loads via convention when enabled
- [x] Module stays dormant when tokens not set
- [x] Module removable without core changes

**Total tasks**: 17
**Completed**: 17
**Remaining**: 0

---

## Requirement Traceability

| Requirement | Task(s) | Test(s) |
|-------------|---------|---------|
| 1.1 Inbound DMs | 2.3, 2.4 | 2.5, 3.4 |
| 1.2 Outbound responses | 2.1, 2.4 | 2.2, 2.5, 3.4 |
| 1.3 Authorization | 2.4 | 2.5, 3.4 |
| 1.4 Token management | 2.4 | 2.5, 4.3 |
| 1.5 Module lifecycle | 1.3, 3.1 | 1.4, 3.5, 4.2 |
| 2.1 Notifications | 3.1, 3.3 | 3.4, 3.5 |
| 2.2 Status command | 2.4 | 2.5 |

---

## Research Insights

### Task 2.3 (SlackBot class) Implementation Notes

- **`conversations.open` for proactive DMs**: Call `conversations.open(users=user_id)` to get DM channel ID first. Cache the result — channel IDs don't change.
- **Token prefix validation**: Validate `xoxb-` prefix for bot token and `xapp-` prefix for app-level token at startup. Log clear error if wrong.

### Task 2.4 (Socket Mode handlers) Critical Details

- **Use `connect_async()`, NOT `start_async()`**: `start_async()` blocks the event loop. `connect_async()` returns immediately.
- **Use `close_async()` for shutdown**: NOT `disconnect_async()` which leaves orphan tasks.
- **Use `@app.event("message")`, NOT `@app.message()`**: The latter misses subtypes like `message_changed`.
- **Check `event.get("bot_id")` to skip bot messages**: Not `subtype == "bot_message"`.

### Task 3.2 (Dependencies) OAuth Scopes Documentation

Minimum required scopes for the Slack app:
- **Bot Token Scopes**: `im:history`, `im:read`, `im:write`, `chat:write`
- **App-Level Token**: `connections:write`
- Note: `commands` and `reactions:write` NOT needed (text commands, no reactions)

### Task 2.4 Handler Concurrency Detail

- Event handlers in slack-bolt fire **concurrently** via `asyncio.ensure_future()` — confirmed by second research pass.
- The asyncio.Lock in `handle_message()` is critical: without it, two messages arriving 50ms apart both call `agent.chat()` simultaneously.
- `connect_async()` returns `None` — wrap in try/except for initial connection failure. Reconnection is handled internally.
- Register no-op handlers for `message_changed` and `message_deleted` to suppress WARNING logs.
- Strip bot mentions from text before command detection: `re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()`

### Task 3.3 (Telemetry) Additional Considerations

- Socket Mode reconnection uses **linear backoff** (not exponential). Messages can be lost during disconnect.
- Consider enabling slack-bolt's built-in `AsyncRetryHandler` for automatic 429 retry.

### Codebase Test Patterns (from Telegram Reference)

**conftest.py pattern** — Use `make_ctx()` factory:
```python
def make_ctx(bus, tool_registry):
    ctx = MagicMock(spec=ModuleContext)
    ctx.bus = bus
    ctx.tool_registry = tool_registry
    return ctx

@pytest.fixture
def bus():
    b = MagicMock()
    b.emit = AsyncMock()
    return b
```

**Bot mock pattern** — Patch at import path:
```python
@patch("arcagent.modules.slack.bot.SlackBot")
async def test_startup(MockBot, module, ctx):
    MockBot.return_value = AsyncMock()
    await module.startup(ctx)
    MockBot.return_value.start.assert_awaited_once()
```

**Integration test pattern** — Use real `ModuleBus()`, mock tool_registry:
```python
bus = ModuleBus()
tool_registry = MagicMock()
tool_registry.register = MagicMock()
ctx = ModuleContext(bus=bus, tool_registry=tool_registry)
```

**Session persistence test** — Two module instances sharing same `tmp_path`:
```python
# First instance sets state
module1 = SlackModule(config=config, workspace=tmp_path)
await module1.startup(ctx)
# ... send message, create session ...
await module1.shutdown()

# Second instance loads state
module2 = SlackModule(config=config, workspace=tmp_path)
await module2.startup(ctx)
# ... verify session persisted ...
```

**pyproject.toml optional dep pattern**:
```toml
[project.optional-dependencies]
slack = ["slack-bolt>=1.20.0", "aiohttp"]
```

**RegisteredTool construction**:
```python
RegisteredTool(
    name="slack_notify_user",
    description="Send a proactive DM notification to the Slack user",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    transport="local",
    execute=self._notify_tool_handler,
    timeout_seconds=30,
    source="module:slack",
)
```
