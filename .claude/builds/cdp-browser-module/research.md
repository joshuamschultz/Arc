# cdp-browser-module — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-145–D-159 (15 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Feature: CDP Browser Module

**Date**: 2026-02-16
**Source**: `.claude/brainstorms/2026-02-16-cdp-browser-module.md`
**Goal**: General-purpose browser interaction module using Chrome DevTools Protocol — agents can navigate, click, type, fill forms, take screenshots, and complete multi-step web tasks (e.g., booking a flight)

#### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|

#### Key Design Principles

- **Just tools for ArcRun**: Browser actions are registered tools. ArcRun's loop calls them like any other tool. No special browser-aware logic in the loop.
- **cdp-use for protocol**: Type-safe, auto-generated CDP bindings. No Playwright/Selenium abstraction layer.
- **Security is configurable**: URL allowlist/denylist, JS execution toggle, cookie persistence — all in TOML config under [modules.browser.security].
- **Accessibility-first selection**: Elements identified by accessibility tree (role + name), not fragile CSS selectors.
- **Full audit trail**: Every browser action emits a module bus event. Combined with ToolRegistry's existing audit, complete observability.

#### Components to Build

1. **`arcagent/modules/browser/`** — Module directory
   - `MODULE.yaml` — Module manifest
   - `__init__.py` — Public API
   - `config.py` — Pydantic config (BrowserConfig, BrowserSecurityConfig)
   - `errors.py` — Browser-specific errors
   - `browser_module.py` — Module wiring (startup, shutdown, tool registration, events)
   - `cdp_client.py` — CDP connection management (launch Chrome, connect WebSocket)
   - `accessibility.py` — Accessibility tree snapshot and element resolution
   - `tools/` — Tool-per-file directory
     - `navigate.py` — browser_navigate, browser_go_back, browser_go_forward, browser_reload
     - `interact.py` — browser_click, browser_type, browser_select, browser_hover
     - `form.py` — browser_fill_form
     - `read.py` — browser_read_page, browser_get_element_text
     - `screenshot.py` — browser_screenshot
     - `javascript.py` — browser_execute_js
     - `dialog.py` — browser_handle_dialog
     - `cookies.py` — browser_get_cookies, browser_set_cookies
     - `download.py` — browser_download_file
   - `cli.py` — CLI commands for testing/debugging

2. **`arcagent/core/config.py`** — Add BrowserConfig to ArcAgentConfig

3. **Tests**
   - `tests/unit/modules/browser/` — Mock CDP WebSocket tests per tool
   - `tests/integration/` — Real headless Chrome tests

#### Architecture Diagram

```
ArcAgent
    |
    v
BrowserModule (Module Bus participant)
    |
    +-- subscribes to agent:startup → registers tools
    +-- subscribes to agent:shutdown → closes CDP connection
    |
    v
CDPClient (cdp-use)
    |
    +-- launches headless Chrome (optional)
    +-- connects via CDP WebSocket
    +-- manages page session
    |
    v
Tools (registered in ToolRegistry)
    |
    +-- browser_navigate(url) → CDP Page.navigate
    +-- browser_click(ref) → CDP DOM + Input
    +-- browser_type(ref, text) → CDP Input.dispatchKeyEvent
    +-- browser_screenshot() → CDP Page.captureScreenshot
    +-- browser_read_page() → CDP Accessibility.getFullAXTree
    +-- browser_execute_js(expression) → CDP Runtime.evaluate
    +-- browser_handle_dialog(action) → CDP Page.handleJavaScriptDialog
    +-- browser_fill_form(fields) → compound: find elements + type
    +-- browser_get_cookies() → CDP Network.getCookies
    +-- browser_download_file(url) → CDP Page.setDownloadBehavior + navigate
    |
    v
ArcRun (loop) — calls tools via ToolRegistry wrapper
    |
    +-- pre_tool event (policy check)
    +-- execute tool (with timeout)
    +-- post_tool event
    +-- audit event
```

#### Research Insights (via /deepen)

**Enriched**: 2026-02-16 | **Sources**: 5 parallel research agents (cdp-use API, accessibility tree, headless Chrome, browser security, codebase patterns)

##### D1 — CDP Library: cdp-use Assessment

**Critical finding**: `cdp-use` is NOT on PyPI — it's generated locally from Chrome's protocol spec by cloning the browser-use repo and running `python -m cdp_use.generator`. It's a code-generation tool, not a published package.

**API surface**: Provides `CDPClient` with async context manager:
```python
async with CDPClient("ws://localhost:9222/devtools/browser/...") as cdp:
    await cdp.send.Page.navigate({"url": "https://example.com"})
    targets = await cdp.send.Target.getTargets()
```

Two main interfaces: `cdp.send` for commands (Page.navigate, DOM.querySelector), `cdp.register` for event registration (Runtime.consoleAPICalled).

**Alternative approaches** (if cdp-use proves too immature):
- **python-cdp** (PyPI: `python-cdp`) — async CDP client with type wrappers, version 0.3.0
- **PyCDP** (PyPI: `chrome-devtools-protocol`) — full typed CDP bindings, version 0.4.0
- **Raw WebSocket** — `websockets` + JSON, maximum control, protocol JSON from `https://chromedevtools.github.io/devtools-protocol/`

**Recommendation**: Start with cdp-use but wrap it behind an internal `CDPClient` protocol/interface so we can swap implementations without touching tool code.

##### D4/D5 — Accessibility Tree: CDP Methods and Patterns

**CDP Accessibility domain methods** (all experimental):

| Method | Purpose | Key Params |
|--------|---------|------------|
| `getFullAXTree` | Fetch entire accessibility tree | `depth`, `frameId` |
| `getPartialAXTree` | Subtree from a node | `backendNodeId`, `nodeId`, `objectId` |
| `queryAXTree` | Search by role/name | `accessibleName`, `role`, `backendNodeId` |
| `getRootAXNode` | Get root node only | `frameId` |
| `getChildAXNodes` | Children of a node | `id`, `frameId` |

**AXNode structure** (each node in the tree):
```
nodeId: str              # Unique AX ID
role: AXValue            # "button", "textbox", "link", etc.
name: AXValue            # Accessible name ("Submit", "Email")
value: AXValue           # Current value (for inputs)
description: AXValue     # Accessible description
backendDOMNodeId: int     # Maps to DOM node for interaction
parentId: str            # Parent AX node
childIds: list[str]      # Children
ignored: bool            # Whether this is accessibility-hidden
properties: list         # Additional ARIA properties
frameId: str             # Frame context (for iframes)
```

**Element interaction via backendDOMNodeId**: The `backendDOMNodeId` on each AXNode maps directly to the DOM node. To click/type:
1. `Accessibility.getFullAXTree()` → get tree with backendDOMNodeIds
2. Assign sequential ref IDs to interactive elements
3. Agent requests action by ref ID
4. Resolve ref → backendDOMNodeId → `DOM.resolveNode(backendNodeId=...)` → objectId
5. `DOM.getBoxModel(backendNodeId=...)` → get coordinates
6. `Input.dispatchMouseEvent(x, y)` for click, `Input.dispatchKeyEvent()` for type

**Edge cases**:
- **iframes**: Each iframe has its own execution context. Must use `frameId` parameter in getFullAXTree
- **Shadow DOM**: CDP can pierce shadow DOM via `DOM.describeNode(pierce=true)`
- **Dynamic content**: Tree may change between snapshot and interaction. Use `DOM.resolveNode` to validate element still exists
- **Elements without labels**: Fall back to CSS selector via `DOM.querySelector`

**Performance**: `getFullAXTree` on complex pages can be slow (100-500ms). Use `depth` parameter to limit tree depth. `queryAXTree` is faster for targeted lookups.

##### D6 — Chrome Launch: Essential Flags and Process Management

**Essential headless Chrome flags**:
```
--headless=new                  # New headless mode (Chrome 112+)
--remote-debugging-port=0       # CDP port (0 = auto-assign)
--no-first-run                  # Skip first-run UI
--no-default-browser-check      # Skip browser check
--disable-background-networking # No background network activity
--disable-extensions            # No extensions
--disable-sync                  # No Google Sync
--metrics-recording-only        # Minimal telemetry
--disable-default-apps          # No default apps
--mute-audio                    # No audio
--no-sandbox                    # Required in containers (NOT for production)
--disable-dev-shm-usage         # Use /tmp instead of /dev/shm (Docker fix)
--disable-gpu                   # No GPU (headless)
```

**CDP endpoint discovery**: After launch, Chrome writes the WebSocket URL to stderr. Also available via:
```
GET http://localhost:{port}/json/version
→ { "webSocketDebuggerUrl": "ws://localhost:{port}/devtools/browser/{id}" }
```

Using `--remote-debugging-port=0` auto-assigns an available port — parse it from Chrome's stderr output to avoid port conflicts.

**Process management pattern**:
```python
import asyncio
import subprocess
import signal

proc = await asyncio.create_subprocess_exec(
    chrome_path, *flags,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
# On shutdown: proc.terminate(), await proc.wait()
```

**Graceful shutdown**: Send SIGTERM, wait 5s, then SIGKILL. Use process groups (`os.setpgrp()`) to kill Chrome and all child processes.

**Zombie process prevention**: Always `await proc.wait()` after termination. Register an `atexit` handler as a safety net.

**Container considerations**:
- `/dev/shm` is 64MB by default in Docker — Chrome crashes. Use `--disable-dev-shm-usage` or mount larger tmpfs
- `--no-sandbox` required when running as root in containers
- Consider `chrome-headless-shell` image (smaller, purpose-built for automation)

##### D8/D9/D10 — Browser Security Controls

**URL filtering implementation**:
- **Domain-based matching**: Compare against list of allowed/denied domains using `urllib.parse.urlparse(url).hostname`
- **Pattern matching**: Support glob patterns (`*.example.com`) and exact matches
- **Pre-navigation check**: Validate URL BEFORE calling `Page.navigate()`. Also subscribe to `Page.frameNavigated` to catch client-side redirects
- **Protocol restriction**: Default deny `file://`, `chrome://`, `chrome-extension://` schemes. Only allow `http://` and `https://`

**JavaScript execution security**:
- `Runtime.evaluate` runs in the page's main world — full access to DOM, cookies, localStorage
- `Page.createIsolatedWorld` creates a separate JS context — cannot see page variables but CAN see the same DOM
- For maximum safety, use isolated worlds. The code can observe the DOM but cannot access `document.cookie`, `localStorage`, or page-scoped JS variables
- **Risk**: Even isolated worlds can still mutate the DOM. True sandboxing requires additional Chrome flags (`--disable-web-security` is the opposite — never use it)

**Cookie encryption pattern**:
```python
from cryptography.fernet import Fernet
key = Fernet.generate_key()  # Store in vault, NOT filesystem
f = Fernet(key)
encrypted = f.encrypt(json.dumps(cookies).encode())
# Decrypt at session start
```

**Download controls via CDP**:
```
Browser.setDownloadBehavior(behavior="deny")  # Block all downloads
Browser.setDownloadBehavior(behavior="allowAndName", downloadPath="/tmp/safe/")  # Controlled path
```

**Data leakage prevention flags**:
```
--disable-background-networking     # No background requests
--disable-client-side-phishing-detection
--disable-component-update          # No auto-updates
--disable-breakpad                  # No crash reporting
--safebrowsing-disable-auto-update  # No Safe Browsing updates
```

**Audit trail per action** (NIST 800-53 AU-3 compliant):
- URL navigated + final URL (after redirects)
- Element targeted (ref ID, role, name, backendNodeId)
- Action performed (click, type, navigate, etc.)
- Input value (for type — redact if `security.redact_inputs = true`)
- Screenshot hash (if taken)
- Timestamp (ISO 8601 with timezone)
- Agent DID

##### Codebase Integration: Exact Module DI Contract

From `module_loader.py`, the browser module constructor must accept these optional params:

```python
class BrowserModule:
    def __init__(
        self,
        config: dict[str, Any] | None = None,   # [modules.browser.config] from TOML
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = BrowserConfig(**(config or {}))
```

**startup() must**:
1. Register tools via `ctx.tool_registry.register(tool)` (each tool as a `RegisteredTool` with `ToolTransport.NATIVE`)
2. Subscribe to events via `ctx.bus.subscribe(event, handler, priority)`
3. Initialize CDP connection (launch Chrome or connect to existing)

**Tool execute pattern**: async functions with `**kwargs` matching `input_schema.properties` keys.

**CLI pattern**: `cli_group(workspace: Path) -> click.Group` factory function. Must add entry to `_register_module_clis()` in `arccli/agent.py` (line 2205) and `_get_module_commands()` (line 2371).

**Config pattern**: Inherit from `ModuleConfig` (which has `extra="forbid"`). TOML section is `[modules.browser.config]`.

##### Security: Prompt Injection via Browser Content

**Critical risk**: Web page content returned to the LLM is untrusted input. An attacker could craft a page with text like "Ignore all previous instructions and...".

**Three-layer defense**:
1. **Content sanitization**: Strip script tags, event handlers, and suspicious patterns from page text before returning to LLM
2. **Output marking**: Clearly mark browser content as "external web content" in tool results so the LLM treats it as data, not instructions
3. **Policy enforcement**: The policy module can veto tool calls based on the content returned (e.g., if it contains injection patterns)

This aligns with OWASP LLM01 (Prompt Injection) and ASI06 (Memory & Context Poisoning).

##### Performance: Resource Guardrails

| Guardrail | Value | Rationale |
|-----------|-------|-----------|
| Max concurrent browser sessions | 1 per agent | Memory budget (Chrome = 100-300MB) |
| Page load timeout | 30s | Prevent hangs on slow pages |
| Screenshot max size | 1920x1080 | Prevent context explosion |
| Accessibility tree depth | 10 levels | Balance completeness vs. size |
| Max page text length | 50,000 chars | Prevent context overflow |
| CDP WebSocket timeout | 10s | Detect stale connections |
| Chrome process memory limit | 512MB | Enforce via cgroups in containers |

#### New Risks Discovered

1. **cdp-use is not on PyPI** — must vendor or generate locally. Consider fallback to `python-cdp` or `chrome-devtools-protocol` if this is too fragile for a dependency
2. **Prompt injection via web content** — page text returned to LLM is a major injection surface. Must sanitize and clearly mark as external data
3. **Chrome process management** — zombie processes, port conflicts, /dev/shm issues in containers. Need robust process lifecycle
4. **Accessibility tree performance** — `getFullAXTree` can be 100-500ms on complex pages. May need caching or partial tree queries
5. **Redirect-based URL bypass** — validating the initial URL is insufficient; must also check after navigation completes (redirects could land on blocked domains)

---

---
