# PRD: CDP Browser Module

## Problem Statement

ArcAgent currently has no web interaction capability. Agents cannot navigate websites, fill forms, click buttons, or extract information from rendered web pages. This prevents agents from completing tasks that require browser interaction — booking travel, filing tickets, gathering web-based intelligence, or automating dashboard workflows.

A browser module using Chrome DevTools Protocol (CDP) would give any ArcAgent the ability to interact with the web as a first-class tool, while maintaining the security controls required for federal deployments.

## Requirements

### Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | Module provides `browser_navigate(url)` tool to navigate to URLs | Must |
| FR-2 | Module provides `browser_click(ref)` tool to click elements by accessibility ref ID | Must |
| FR-3 | Module provides `browser_type(ref, text)` tool to type text into input elements | Must |
| FR-4 | Module provides `browser_screenshot()` tool returning base64 PNG | Must |
| FR-5 | Module provides `browser_read_page()` tool returning accessibility tree snapshot with ref IDs | Must |
| FR-6 | Module provides `browser_fill_form(fields)` compound tool for multi-field form filling | Must |
| FR-7 | Module provides `browser_execute_js(expression)` tool for JavaScript execution in page context | Should |
| FR-8 | Module provides `browser_handle_dialog(action, text)` tool for alert/confirm/prompt handling | Must |
| FR-9 | Module provides `browser_go_back()`, `browser_go_forward()`, `browser_reload()` navigation tools | Should |
| FR-10 | Module provides `browser_get_cookies()` and `browser_set_cookies(cookies)` tools | Should |
| FR-11 | Module provides `browser_select(ref, value)` and `browser_hover(ref)` interaction tools | Should |
| FR-12 | Module provides `browser_download_file(url, path)` tool with configurable download behavior | Should |
| FR-13 | Module can launch headless Chrome and connect via CDP WebSocket | Must |
| FR-14 | Module can connect to an existing CDP endpoint (external Chrome) | Must |
| FR-15 | Page state is represented as structured accessibility tree with role, name, value, and numeric ref IDs | Must |
| FR-16 | Elements are resolved via accessibility tree first, CSS selector fallback | Must |
| FR-17 | URL security: configurable allowlist/denylist mode for URL access control | Must |
| FR-18 | JS execution: configurable toggle (`security.allow_js_execution`) | Must |
| FR-19 | Cookie persistence: configurable encrypted storage for multi-step workflows | Should |
| FR-20 | Every browser action emits a Module Bus event (browser.navigated, browser.clicked, etc.) | Must |
| FR-21 | Tools auto-register on module load via agent:startup event subscription | Must |
| FR-22 | Module follows MODULE.yaml manifest pattern with cli_entry for debugging commands | Must |
| FR-23 | URL validation checks both initial URL and post-redirect URL against security policy | Must |
| FR-24 | Web content returned to LLM is clearly marked as external/untrusted data | Must |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-1 | Navigation timeout | 30s default, configurable |
| NFR-2 | Click/type timeout | 5s default, configurable |
| NFR-3 | Screenshot timeout | 10s default, configurable |
| NFR-4 | Screenshot max resolution | 1920x1080 |
| NFR-5 | Page text max length | 50,000 characters |
| NFR-6 | Accessibility tree max depth | 10 levels |
| NFR-7 | CDP WebSocket connection timeout | 10s |
| NFR-8 | Chrome process memory limit | 512MB (enforced via config) |
| NFR-9 | All existing tests continue passing | 0 regressions |
| NFR-10 | `mypy --strict` passes | 0 errors |
| NFR-11 | `ruff check` passes | 0 errors |
| NFR-12 | Module does NOT increase core LOC | 0 lines added to core/ |

## Success Criteria

1. An agent can navigate to a travel site, search flights, fill in passenger details, and complete a booking end-to-end
2. `browser_read_page()` returns a structured accessibility snapshot with ref IDs that the LLM can use to target elements
3. `browser_click(ref)` and `browser_type(ref, text)` reliably interact with elements identified from the accessibility snapshot
4. URL allowlist mode blocks navigation to non-approved domains and logs the attempt
5. All browser actions emit Module Bus events visible in telemetry
6. Module can be enabled/disabled via `[modules.browser]` in TOML config
7. All existing tests pass with zero regressions

## Out of Scope (v2+)

- Multi-tab orchestration (v1 is single-tab per agent)
- Network interception/modification
- Video recording of browser sessions
- Headed/GUI mode
- Browser extension support
- Visual coordinate-based interaction (screenshot + vision model)
- Proxy configuration
