# Implementation Plan: Telegram Messaging Module (S005)

## Status: COMPLETE

**Completed**: 22/22 | **Remaining**: 0/22

---

## What We're NOT Doing

- Webhook mode (polling only)
- Group chat / multi-user
- Voice, audio, images, files
- Markdown/HTML formatting
- Inline keyboards / callback buttons
- Multi-tenant support
- Vault-backed token (env var for now)

---

## Specification References

- **PRD**: `.claude/specs/S005-telegram-messaging/PRD.md`
- **SDD**: `.claude/specs/S005-telegram-messaging/SDD.md`
- **Decisions**: `.claude/decisions-log.md` > Feature: Telegram Messaging Module
- **Quality**: `packages/arcagent/.claude/steering/tech.md#quality-thresholds`
- **Commands**: `packages/arcagent/.claude/steering/tech.md#project-commands`

---

## Phase 1: Config and Module Skeleton [4 tasks]

- [x] **1.1 Create TelegramConfig** `[activity: module-development]` `[parallel: true]`
  - File: `packages/arcagent/arcagent/modules/telegram/config.py`
  - Implementation:
    - Inherit from `ModuleConfig` (same as `SchedulerConfig`)
    - Fields: `enabled`, `allowed_chat_ids`, `poll_interval`, `max_message_length`, `bot_token_env_var`
    - Defaults match SDD specification
  - Purpose: Validated Pydantic config for the Telegram module
  - _Leverage: `arcagent/modules/scheduler/config.py`_
  - _Requirements: 1.4, 1.5_

- [x] **1.2 Create MODULE.yaml** `[activity: module-development]` `[parallel: true]`
  - File: `packages/arcagent/arcagent/modules/telegram/MODULE.yaml`
  - Implementation:
    - name, version, description, author, entry_point
    - entry_point: `arcagent.modules.telegram:TelegramModule`
    - events: subscribes and emits per SDD
  - Purpose: Module manifest for convention-based loader
  - _Leverage: `arcagent/modules/scheduler/MODULE.yaml`_
  - _Requirements: 1.5_

- [x] **1.3 Create TelegramModule skeleton** `[activity: module-development]`
  - File: `packages/arcagent/arcagent/modules/telegram/__init__.py`
  - Implementation:
    - `__init__` with convention-based DI params (config, telemetry, workspace)
    - `name` property returning `"telegram"`
    - `startup(ctx)` stub (subscribes to events, deferred bot creation)
    - `shutdown()` stub
    - `set_agent_chat_fn()` method
    - `__all__` export
  - Purpose: Module entry point following Module protocol
  - _Leverage: `arcagent/modules/scheduler/__init__.py`_
  - _Requirements: 1.5_
  - _Blocked by: 1.1_

- [x] **1.4 Write config and skeleton tests** `[activity: unit-testing]`
  - Files:
    - `packages/arcagent/tests/unit/modules/telegram/__init__.py`
    - `packages/arcagent/tests/unit/modules/telegram/conftest.py`
    - `packages/arcagent/tests/unit/modules/telegram/test_config.py`
    - `packages/arcagent/tests/unit/modules/telegram/test_module.py`
  - Implementation:
    - Config validation: defaults, extra="forbid", empty allowed_chat_ids
    - Module lifecycle: startup/shutdown, name property, set_agent_chat_fn
    - Shared fixtures: mock telemetry, mock ModuleContext, tmp workspace
  - Purpose: TDD foundation for module
  - _Leverage: `tests/unit/modules/scheduler/conftest.py`, `test_module.py`_
  - _Requirements: 1.5_
  - _Blocked by: 1.1, 1.3_

**PAUSE POINT: Verify Phase 1 -- module skeleton loads via ModuleLoader, config validates, tests pass.**

---

## Phase 2: Bot Core [5 tasks]

- [x] **2.1 Implement message splitting** `[activity: module-development]` `[parallel: true]`
  - File: `packages/arcagent/arcagent/modules/telegram/bot.py` (initial creation)
  - Implementation:
    - `split_message(text, max_length=4096) -> list[str]`
    - Priority: double-newline > single-newline > sentence boundary > hard split
    - Handle edge cases: empty text, text shorter than max, text with no boundaries
  - Purpose: Ensure responses respect Telegram's 4096 char limit
  - _Requirements: 1.2_

- [x] **2.2 Write split_message tests** `[activity: unit-testing]` `[parallel: true]`
  - File: `packages/arcagent/tests/unit/modules/telegram/test_bot.py`
  - Implementation:
    - Short text (no split needed)
    - Split at paragraph boundary
    - Split at sentence boundary (no paragraph breaks)
    - Hard split (no natural boundaries)
    - Empty/None input
    - Exactly 4096 chars
    - Multi-split (>8192 chars)
  - Purpose: Full coverage of splitting edge cases
  - _Requirements: 1.2_

- [x] **2.3 Implement TelegramBot class** `[activity: module-development]`
  - File: `packages/arcagent/arcagent/modules/telegram/bot.py`
  - Implementation:
    - Constructor: config, telemetry, workspace
    - `_agent_chat_fn` slot for deferred binding
    - `set_agent_chat_fn()` method
    - `_message_queue: asyncio.Queue`
    - `_current_session_id` and `_chat_id` state
    - `_state_path` for persisting session state to `{workspace}/telegram/state.json`
    - `_load_state()` / `_save_state()` for session persistence
  - Purpose: Bot class with state management
  - _Leverage: `arcagent/modules/scheduler/scheduler.py` (asyncio patterns)_
  - _Requirements: 1.1, 1.5_
  - _Blocked by: 2.1_

- [x] **2.4 Implement polling loop and handlers** `[activity: module-development]`
  - File: `packages/arcagent/arcagent/modules/telegram/bot.py`
  - Implementation:
    - `start()`: read bot token from env, create python-telegram-bot Application, add handlers, start polling via `asyncio.create_task`
    - `stop()`: stop polling, drain queue, close application
    - `_handle_start(update, context)`: store chat_id, create session, welcome
    - `_handle_new(update, context)`: new session_id, confirm
    - `_handle_status(update, context)`: return session info
    - `_handle_message(update, context)`: verify auth, send TYPING, enqueue
    - `_process_queue()`: dequeue loop, call agent_chat_fn, split response, send
    - `send_notification(text)`: send proactive message to stored chat_id
  - Purpose: Complete bot functionality
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2_
  - _Blocked by: 2.3_

- [x] **2.5 Write bot handler tests** `[activity: unit-testing]`
  - File: `packages/arcagent/tests/unit/modules/telegram/test_bot.py` (extend)
  - Implementation:
    - Mock python-telegram-bot's Bot and Update objects
    - Test /start handler: stores chat_id, creates session
    - Test /new handler: new session_id
    - Test /status handler: returns info
    - Test message handler: authorized chat_id -> queue -> agent.chat() -> response
    - Test unauthorized chat_id -> silently ignored
    - Test no bot token -> module stays dormant
    - Test send_notification: sends to stored chat_id
  - Purpose: Comprehensive bot behavior tests
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2_
  - _Blocked by: 2.4_

**PAUSE POINT: Verify Phase 2 -- bot handlers work with mocked Telegram API, all tests pass.**

---

## Phase 3: Module Wiring [4 tasks]

- [x] **3.1 Wire TelegramModule startup to TelegramBot** `[activity: module-development]`
  - File: `packages/arcagent/arcagent/modules/telegram/__init__.py`
  - Implementation:
    - In `startup()`: create TelegramBot, subscribe to `agent:shutdown`, `schedule:completed`, `schedule:failed`
    - Call `bot.start()`
    - In `shutdown()`: call `bot.stop()`
    - In `set_agent_chat_fn()`: delegate to bot
    - Event handlers for schedule events -> `bot.send_notification()`
  - Purpose: Complete module lifecycle
  - _Leverage: `arcagent/modules/scheduler/__init__.py`_
  - _Requirements: 1.5, 2.1_

- [x] **3.2 Add deferred binding in agent.py** `[activity: core-development]`
  - File: `packages/arcagent/arcagent/core/agent.py`
  - Implementation:
    - Add `_wire_telegram_chat_fn()` method (same pattern as `_wire_scheduler_run_fn`)
    - Call it after `_wire_scheduler_run_fn()` in startup sequence
    - Uses `self._bus.get_module("telegram")` to check if module exists
    - Calls `telegram.set_agent_chat_fn(self.chat)`
  - Purpose: Deferred binding of agent.chat() to telegram module
  - _Leverage: `agent.py:464-476` (`_wire_scheduler_run_fn`)_
  - _Requirements: 1.5_

- [x] **3.3 Add python-telegram-bot dependency** `[activity: config-design]`
  - File: `packages/arcagent/pyproject.toml`
  - Implementation:
    - Add `python-telegram-bot` to optional dependencies group (e.g., `[project.optional-dependencies] telegram = ["python-telegram-bot>=20.0"]`)
    - Keep as optional so core doesn't require it
  - Purpose: Telegram library available when module is enabled
  - _Requirements: CON-F2_

- [x] **3.4 Update module lifecycle tests** `[activity: unit-testing]`
  - File: `packages/arcagent/tests/unit/modules/telegram/test_module.py` (extend)
  - Implementation:
    - Test full startup -> bot started
    - Test shutdown -> bot stopped
    - Test set_agent_chat_fn propagates to bot
    - Test schedule:completed event -> send_notification called
    - Test schedule:failed event -> send_notification called
    - Test deferred binding wiring from agent.py
  - Purpose: Verify module-bot integration
  - _Requirements: 1.5, 2.1_
  - _Blocked by: 3.1, 3.2_

**PAUSE POINT: Verify Phase 3 -- module loads via convention, deferred binding works, schedule events trigger notifications.**

---

## Phase 4: Telemetry and Events [3 tasks]

- [x] **4.1 Add telemetry events** `[activity: module-development]`
  - File: `packages/arcagent/arcagent/modules/telegram/bot.py`
  - Implementation:
    - Emit `telegram:message_received` on every inbound message (with chat_id, session_id)
    - Emit `telegram:message_sent` on every outbound response
    - Emit `telegram:notification_sent` on proactive sends
    - Emit `telegram:auth_rejected` on unauthorized access (with chat_id, no sensitive data)
    - Emit `telegram:polling_started` and `telegram:polling_stopped`
    - Emit `telegram:error` on failures
    - Use `self._telemetry.record_event()` if available
  - Purpose: Full audit trail per CON-6
  - _Requirements: 1.3, CON-6_
  - _Blocked by: 2.4_

- [x] **4.2 Add Module Bus event emission** `[activity: module-development]`
  - File: `packages/arcagent/arcagent/modules/telegram/__init__.py`
  - Implementation:
    - Store `ctx.bus` reference during startup
    - Emit Module Bus events for key actions (message_received, auth_rejected, etc.)
    - Include trace_id and agent_did from context
  - Purpose: Module Bus integration for observability
  - _Requirements: 1.5, CON-6_
  - _Blocked by: 3.1_

- [x] **4.3 Write telemetry tests** `[activity: unit-testing]`
  - File: `packages/arcagent/tests/unit/modules/telegram/test_bot.py` (extend)
  - Implementation:
    - Verify telegram:message_received emitted on inbound
    - Verify telegram:auth_rejected emitted on unauthorized
    - Verify telegram:message_sent emitted on outbound
    - Verify no bot token in any event data
  - Purpose: Audit trail verification
  - _Requirements: 1.3, 1.4, CON-6_
  - _Blocked by: 4.1_

**PAUSE POINT: Verify Phase 4 -- all events emitted correctly, no token leakage in telemetry.**

---

## Phase 5: Integration Testing [3 tasks]

- [x] **5.1 Write integration test: full message flow** `[activity: integration-testing]`
  - File: `packages/arcagent/tests/integration/test_telegram_integration.py`
  - Implementation:
    - Create real ArcAgent with TelegramModule
    - Mock python-telegram-bot's Bot at HTTP level
    - Send message -> verify agent.chat() called -> verify response sent
    - Verify session persistence (state.json created)
  - Purpose: End-to-end message flow with real agent
  - _Leverage: `tests/integration/test_memory_integration.py`_
  - _Requirements: 1.1, 1.2, 1.5_

- [x] **5.2 Write integration test: proactive notification** `[activity: integration-testing]`
  - File: `packages/arcagent/tests/integration/test_telegram_integration.py` (extend)
  - Implementation:
    - Set up TelegramModule with stored chat_id
    - Emit schedule:completed event on Module Bus
    - Verify send_notification called with result
  - Purpose: Proactive messaging works end-to-end
  - _Requirements: 2.1_

- [x] **5.3 Write integration test: authorization** `[activity: integration-testing]`
  - File: `packages/arcagent/tests/integration/test_telegram_integration.py` (extend)
  - Implementation:
    - Configure allowed_chat_ids = [111]
    - Send message from chat_id 999
    - Verify no agent.chat() call
    - Verify telegram:auth_rejected event
  - Purpose: Authorization enforcement verified
  - _Requirements: 1.3_

**PAUSE POINT: Verify Phase 5 -- all integration tests pass, coverage >= 80%.**

---

## Phase 6: Quality Gates [3 tasks]

- [x] **6.1 Run full quality checks** `[activity: unit-testing]`
  - Implementation:
    - `cd packages/arcagent && pytest --cov=arcagent/modules/telegram --cov-report=term-missing`
    - `ruff check arcagent/modules/telegram/`
    - `ruff format --check arcagent/modules/telegram/`
    - `mypy arcagent/modules/telegram/`
  - Purpose: Quality gates per steering/tech.md
  - _Requirements: All_

- [x] **6.2 Verify module loads via ModuleLoader** `[activity: integration-testing]`
  - Implementation:
    - Write or extend test: ModuleLoader discovers telegram MODULE.yaml
    - Verify entry_point passes prefix validation
    - Verify constructor DI works (config, telemetry, workspace injected)
  - Purpose: Convention-based loading works
  - _Requirements: 1.5_

- [x] **6.3 Manual verification** `[activity: module-development]`
  - Implementation:
    - Verify no hardcoded tokens in source
    - Verify no bot token in any test output
    - Verify all public methods have docstrings
    - Verify module directory is self-contained (removable)
  - Purpose: Security and quality final check
  - _Requirements: 1.4, CON-5, CON-6_

---

## Success Criteria

### Automated

```bash
# All tests pass
cd packages/arcagent && pytest tests/unit/modules/telegram/ tests/integration/test_telegram_integration.py -v

# Coverage >= 80%
pytest --cov=arcagent/modules/telegram --cov-report=term-missing

# Lint clean
ruff check arcagent/modules/telegram/

# Type clean
mypy arcagent/modules/telegram/
```

### Manual

- [ ] Module loads via convention when `[modules.telegram] enabled = true`
- [ ] Module stays dormant when token env var is not set
- [ ] Module is removable (delete directory + remove config) without core changes
- [ ] No bot token appears in logs, telemetry, or error messages

---

## Requirement Traceability Matrix

| Requirement | Task(s) | Test(s) |
|-------------|---------|---------|
| 1.1 Inbound message handling | 2.3, 2.4 | 2.5 (message handler), 5.1 |
| 1.2 Outbound response delivery | 2.1, 2.4 | 2.2 (split), 2.5 (send), 5.1 |
| 1.3 Authorization | 2.4 | 2.5 (auth rejected), 5.3 |
| 1.4 Bot token management | 2.4 | 2.5 (no token), 6.3 |
| 1.5 Module lifecycle | 1.3, 3.1, 3.2 | 1.4, 3.4, 6.2 |
| 2.1 Proactive notifications | 3.1, 4.2 | 3.4, 5.2 |
| 2.2 Session status | 2.4 | 2.5 (/status handler) |
| CON-6 Audit events | 4.1, 4.2 | 4.3 |
