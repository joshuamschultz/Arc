# PLAN: CDP Browser Module

**Spec**: SPEC-005 | **Status**: COMPLETE | **Package**: arcagent

## Phase 1: Foundation (Config + CDP Client + Module Shell)

> Establish the module skeleton, config validation, and CDP connection management.

### Tasks

- [x] 1.1 Create `arcagent/modules/browser/config.py` — BrowserConfig, BrowserSecurityConfig, BrowserConnectionConfig, BrowserTimeoutConfig, BrowserCookieConfig (Pydantic models inheriting ModuleConfig)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_config.py` — validate defaults, url_mode enum, scheme blocking, extra="forbid"
  - **Acceptance**: All config models validate correctly. Invalid values rejected. `mypy --strict` passes.

- [x] 1.2 Create `arcagent/modules/browser/errors.py` — BrowserError (base), CDPConnectionError, URLBlockedError, ElementNotFoundError, BrowserTimeoutError
  - **Activity**: `module-development`
  - **Tests**: None required (simple error classes)
  - **Acceptance**: Error classes follow AgentError pattern with code, message, details.

- [x] 1.3 Create `arcagent/modules/browser/cdp_client.py` — CDPClientManager: Chrome process launch, CDP WebSocket connection, send/receive, graceful shutdown
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_cdp_client.py` — mock subprocess for Chrome launch, mock WebSocket for CDP commands, connection lifecycle, shutdown cleanup
  - **Acceptance**: Can launch Chrome, discover WebSocket URL, send CDP commands, graceful shutdown with no zombie processes.

- [x] 1.4 Create `arcagent/modules/browser/MODULE.yaml` — module manifest following existing pattern
  - **Activity**: `module-development`
  - **Tests**: None (YAML manifest)
  - **Acceptance**: Manifest loads via ModuleLoader.discover(). Entry point resolves.

- [x] 1.5 Create `arcagent/modules/browser/__init__.py` — public API exports
  - **Activity**: `module-development`
  - **Tests**: None (import structure)
  - **Acceptance**: `from arcagent.modules.browser import BrowserModule, BrowserConfig` works.

- [x] 1.6 Create `arcagent/modules/browser/browser_module.py` — BrowserModule class (Module protocol: name, startup, shutdown). Startup connects CDP, registers tools, subscribes to events.
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_browser_module.py` — mock CDP client, verify tool registration count, event subscription, shutdown cleanup
  - **Acceptance**: Module satisfies Module protocol. startup() registers tools. shutdown() disconnects CDP.

**Phase 1 Completion**: 6 tasks | Module loads, connects to Chrome, registers no tools yet (tools added in Phase 2-3).

---

## Phase 2: Core Tools (Navigate + Read + Screenshot)

> The minimum viable set: navigate to a page, read what's there, take a screenshot.

### Tasks

- [x] 2.1 Create `arcagent/modules/browser/accessibility.py` — AccessibilityManager: AX tree snapshot, ref ID assignment, element resolution (ref → backendDOMNodeId), queryAXTree fallback
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_accessibility.py` — mock AX tree responses, ref ID assignment, resolution, edge cases (ignored nodes, iframes, missing labels)
  - **Acceptance**: Snapshot returns formatted text with ref IDs. resolve_ref() returns correct backendDOMNodeId.

- [x] 2.2 Create `arcagent/modules/browser/tools/__init__.py` — create_browser_tools() master factory
  - **Activity**: `module-development`
  - **Tests**: Part of test_browser_module.py (registration count)
  - **Acceptance**: Factory creates and returns all enabled tools based on config.

- [x] 2.3 Create `arcagent/modules/browser/tools/navigate.py` — browser_navigate (with URL security check + redirect validation), browser_go_back, browser_go_forward, browser_reload
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_navigate.py` — navigate success, URL blocked (allowlist), URL blocked (denylist), redirect to blocked domain, scheme blocking, go back/forward/reload
  - **Acceptance**: Navigate works. URL policy enforced pre- and post-navigation. Events emitted.

- [x] 2.4 Create `arcagent/modules/browser/tools/read.py` — browser_read_page (returns AX snapshot), browser_get_element_text
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_read.py` — snapshot format, max text length truncation, element text extraction
  - **Acceptance**: Returns properly formatted accessibility snapshot. Text truncated to max_page_text_length.

- [x] 2.5 Create `arcagent/modules/browser/tools/screenshot.py` — browser_screenshot (base64 PNG, resolution capping)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_screenshot.py` — screenshot returns base64 string, resolution capping
  - **Acceptance**: Returns base64 PNG. Resolution capped to config max.

**Phase 2 Completion**: 5 tasks | Agent can navigate, read pages, and take screenshots.

---

## Phase 3: Interaction Tools (Click + Type + Form + Dialog)

> Full interaction capability — the agent can now complete multi-step workflows.

### Tasks

- [x] 3.1 Create `arcagent/modules/browser/tools/interact.py` — browser_click (resolve ref → click via CDP), browser_type (focus + key events), browser_select (dropdown), browser_hover
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_interact.py` — click by ref, type text, select option, hover, element not found error, events emitted
  - **Acceptance**: Click/type/select/hover work via ref IDs. ElementNotFoundError on invalid ref.

- [x] 3.2 Create `arcagent/modules/browser/tools/form.py` — browser_fill_form (compound: takes dict of field_label→value, finds each field, types value)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_form.py` — fill multiple fields, partial match, field not found
  - **Acceptance**: Multi-field form filling works. Reports which fields succeeded/failed.

- [x] 3.3 Create `arcagent/modules/browser/tools/dialog.py` — browser_handle_dialog (accept/dismiss/type for alert/confirm/prompt)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_dialog.py` — accept alert, dismiss confirm, type in prompt
  - **Acceptance**: Dialog handling works for all three dialog types.

**Phase 3 Completion**: 3 tasks | Agent can interact with all page elements and handle dialogs.

---

## Phase 4: Extended Tools (JS + Cookies + Downloads)

> Capabilities that are configurable/toggleable for security.

### Tasks

- [x] 4.1 Create `arcagent/modules/browser/tools/javascript.py` — browser_execute_js (Runtime.evaluate with configurable toggle)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_javascript.py` — execute JS, return value, toggle disabled blocks execution, error handling
  - **Acceptance**: JS execution works when enabled. Blocked when disabled. Result returned as string.

- [x] 4.2 Create `arcagent/modules/browser/tools/cookies.py` — browser_get_cookies, browser_set_cookies (with optional encrypted persistence)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_cookies.py` — get cookies, set cookies, persistence encrypt/decrypt, ephemeral mode
  - **Acceptance**: Cookie CRUD works. Encrypted persistence works when configured.

- [x] 4.3 Create `arcagent/modules/browser/tools/download.py` — browser_download_file (Browser.setDownloadBehavior + navigation)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_download.py` — download allowed, download blocked, path validation
  - **Acceptance**: Downloads work when enabled. Blocked when disabled. Files go to configured path.

**Phase 4 Completion**: 3 tasks | Full tool surface complete.

---

## Phase 5: CLI + Security Tests + Integration

> CLI debugging commands, security-specific tests, and real Chrome integration test.

### Tasks

- [x] 5.1 Create `arcagent/modules/browser/cli.py` — cli_group(workspace) factory with subcommands: `status` (connection info), `navigate` (quick test), `screenshot` (capture to file)
  - **Activity**: `module-development`
  - **Tests**: `tests/unit/modules/browser/test_browser_cli.py` — CLI command invocation
  - **Acceptance**: `arc agent browser status` shows connection state. `arc agent browser navigate <url>` works.

- [x] 5.2 Create `tests/unit/modules/browser/test_security.py` — URL allowlist/denylist enforcement, scheme blocking, redirect bypass prevention, JS toggle, content marking as external
  - **Activity**: `unit-testing`
  - **Tests**: (this IS the test task)
  - **Acceptance**: All URL security scenarios covered. Redirect attacks blocked. Content marked as external.

- [x] 5.3 Update `arccli/agent.py` — register browser module CLI commands (add to _register_module_clis and _get_module_commands)
  - **Activity**: `core-development`
  - **Tests**: Verify CLI help shows browser commands
  - **Acceptance**: `arc agent browser --help` works.

- [x] 5.4 Create `tests/integration/test_browser_integration.py` — real headless Chrome: navigate, read page, click, type, screenshot on a local test page
  - **Activity**: `integration-testing`
  - **Tests**: (this IS the test task)
  - **Acceptance**: Full flow works with real Chrome. Agent navigates, reads AX tree, clicks button, types text, takes screenshot.

- [x] 5.5 Add `websockets` CDP library to `pyproject.toml` dependencies
  - **Activity**: `module-development`
  - **Tests**: pip install works, import works
  - **Acceptance**: Module can import CDP client library.

**Phase 5 Completion**: 5 tasks | Module fully testable with CLI and integration tests.

---

## Phase 6: Verification

> Final quality gates.

### Tasks

- [x] 6.1 Run full test suite — all existing tests pass, all new tests pass
  - **Activity**: `unit-testing`
  - **Acceptance**: 0 failures, 0 regressions

- [x] 6.2 Run `mypy --strict` on `arcagent/modules/browser/`
  - **Activity**: `core-development`
  - **Acceptance**: 0 type errors

- [x] 6.3 Run `ruff check` on `arcagent/modules/browser/`
  - **Activity**: `core-development`
  - **Acceptance**: 0 lint errors

- [x] 6.4 Verify core LOC unchanged — browser module adds 0 lines to `arcagent/core/`
  - **Activity**: `core-development`
  - **Acceptance**: Core LOC identical to pre-implementation

**Phase 6 Completion**: 4 tasks | All quality gates pass.

---

## Summary

| Phase | Tasks | Focus |
|-------|-------|-------|
| 1. Foundation | 6 | Config, CDP client, module shell |
| 2. Core Tools | 5 | Navigate, read, screenshot |
| 3. Interaction | 3 | Click, type, form, dialog |
| 4. Extended | 3 | JS, cookies, downloads |
| 5. CLI + Integration | 5 | CLI, security tests, real Chrome |
| 6. Verification | 4 | Quality gates |
| **Total** | **26** | |

## Dependencies

- **Phase 2 depends on Phase 1** (tools need CDP client + config)
- **Phase 3 depends on Phase 2** (interaction needs accessibility manager)
- **Phase 4 depends on Phase 1** (extended tools need config for toggles)
- **Phase 5 depends on Phases 1-4** (CLI and integration test full surface)
- **Phase 6 depends on Phase 5** (verification runs everything)
