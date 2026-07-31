---
topic: CDP Browser Module for ArcAgent
date: 2026-02-16
status: complete
---

# CDP Browser Module for ArcAgent

## Inspiration

General capability — agents should be able to interact with the web as a first-class tool. Not driven by a single project but by the vision that any ArcAgent should be able to navigate, interact with, and extract information from web applications when needed.

## Projects

This is a new ArcAgent module (`arcagent.modules.browser`) that provides Chrome DevTools Protocol (CDP) browser interaction as agent tools. Follows the existing module pattern (MODULE.yaml, event bus integration, CLI entry point).

## Audience

All agent types that need web interaction:
- **Research agents** — gather information, read pages, extract structured data
- **QA/Testing agents** — validate web applications, click buttons, fill forms, check UI state
- **Automation agents** — perform tasks on web platforms: file tickets, submit forms, interact with dashboards, book flights

## Use Cases

- **Navigate + Read**: Go to URLs, read rendered page content, extract structured data from DOM
- **Interact + Automate**: Click elements, fill forms, handle dialogs, navigate multi-step workflows (e.g., booking a flight end-to-end)
- **Screenshot + Observe**: Take screenshots for visual analysis, monitor page state, capture evidence
- **Full browser agent**: Complete browser control — an agent can accomplish any task a human could in a browser

## Desired Outcomes

- **Primary**: An agent can navigate to a travel site, search flights, fill in passenger details, and complete a booking — end-to-end without human intervention
- **Secondary**: Agents can reliably complete arbitrary multi-step web tasks with the same tools
- **Tertiary**: Module integrates cleanly with ArcAgent's security, telemetry, and policy systems

## Guiding Principles

- **Security AND Simplicity**: Clean API surface (navigate, click, type, read, screenshot) with strong configurable security boundaries
- **Configurable lockdown**: All capabilities available by default but lockable via module security config in TOML — URL allowlists, JS execution toggle, download policies, etc.
- **Headless only**: Agents run in containers/servers. CDP connects to headless Chrome/Chromium. No GUI.
- **Audit everything**: Every navigation, click, form fill, and screenshot is an auditable event via OpenTelemetry
- **Module pattern compliance**: Follows existing MODULE.yaml, event bus, CLI entry point patterns

## Constraints

- **Headless Chrome/Chromium**: No headed browser support
- **CDP protocol**: Direct CDP over WebSocket — no Playwright/Selenium abstraction layer
- **Federal context**: Security controls must be configurable for restricted environments (URL allowlists, network isolation, no credential leakage)
- **Module system**: Must integrate as a standard ArcAgent module — MODULE.yaml, event subscriptions, tool registration

## Scope

**In**:
- CDP connection management (connect to headless Chrome endpoint)
- Navigation (goto URL, back, forward, reload)
- DOM interaction (click, type, select, hover)
- Form filling and submission
- Screenshot capture
- Page content extraction (text, HTML, structured data)
- JavaScript execution in page context
- File download handling
- Dialog handling (alerts, confirms, prompts)
- Cookie and session management
- Configurable security controls (URL allowlists, JS toggle, download policy)
- OpenTelemetry audit trail on all actions
- CLI commands for testing/debugging

**Out**:
- Browser lifecycle management (no installing/launching Chrome — assumes CDP endpoint exists)
- Headed/GUI mode
- Video recording
- Network interception/modification (v1)
- Multi-tab orchestration (v1 — single tab per tool call)

## Open Questions

_(None — all resolved during brainstorm)_

## Related Solutions

- No existing solutions in `.claude/solutions/` relate to browser tooling
