# SDD: CDP Browser Module

## Architecture

The browser module is a **standard ArcAgent module** that registers browser interaction tools with the ToolRegistry. It uses `cdp-use` (type-safe CDP bindings) to communicate with headless Chrome over WebSocket. The module follows tool-per-file organization for maximum testability.

ArcRun treats browser tools identically to any other tool — no special browser awareness in the loop.

```
arcagent/modules/browser/
├── MODULE.yaml              # Module manifest
├── __init__.py              # Public API exports
├── config.py                # BrowserConfig, BrowserSecurityConfig (Pydantic)
├── errors.py                # BrowserError, CDPConnectionError, URLBlockedError
├── browser_module.py        # Module wiring (startup, shutdown, registration, events)
├── cdp_client.py            # CDP connection + Chrome process management
├── accessibility.py         # AX tree snapshot, ref ID assignment, element resolution
├── tools/                   # Tool-per-file
│   ├── __init__.py          # create_browser_tools() factory
│   ├── navigate.py          # browser_navigate, browser_go_back, browser_go_forward, browser_reload
│   ├── interact.py          # browser_click, browser_type, browser_select, browser_hover
│   ├── form.py              # browser_fill_form
│   ├── read.py              # browser_read_page, browser_get_element_text
│   ├── screenshot.py        # browser_screenshot
│   ├── javascript.py        # browser_execute_js
│   ├── dialog.py            # browser_handle_dialog
│   ├── cookies.py           # browser_get_cookies, browser_set_cookies
│   └── download.py          # browser_download_file
├── cli.py                   # CLI commands for testing/debugging
tests/
├── unit/modules/browser/
│   ├── conftest.py          # Mock CDP client fixtures
│   ├── test_config.py       # Config validation
│   ├── test_cdp_client.py   # Connection management
│   ├── test_accessibility.py # AX tree parsing, ref IDs
│   ├── test_navigate.py     # Navigate tools
│   ├── test_interact.py     # Click, type, select tools
│   ├── test_form.py         # Form fill tool
│   ├── test_read.py         # Page reading tools
│   ├── test_screenshot.py   # Screenshot tool
│   ├── test_javascript.py   # JS execution tool
│   ├── test_dialog.py       # Dialog handling
│   ├── test_cookies.py      # Cookie tools
│   ├── test_download.py     # Download tool
│   ├── test_security.py     # URL filtering, JS toggle
│   └── test_browser_module.py # Module lifecycle
├── integration/
│   └── test_browser_integration.py  # Real headless Chrome
```

## Components

### 1. Config: `config.py`

```python
class BrowserSecurityConfig(ModuleConfig):
    """Security controls for the browser module."""
    url_mode: Literal["allowlist", "denylist"] = "denylist"
    url_patterns: list[str] = []           # Domain patterns (glob: *.example.com)
    blocked_schemes: list[str] = ["file", "chrome", "chrome-extension", "javascript"]
    allow_js_execution: bool = True
    allow_downloads: bool = True
    download_path: str = "/tmp/arcagent-downloads"
    redact_inputs: bool = False            # Redact typed text in audit logs
    max_page_text_length: int = 50_000
    max_screenshot_width: int = 1920
    max_screenshot_height: int = 1080

class BrowserConnectionConfig(ModuleConfig):
    """CDP connection settings."""
    cdp_url: str = ""                      # External CDP endpoint (ws://...)
    chrome_path: str = ""                  # Path to Chrome binary (auto-detect if empty)
    headless: bool = True
    remote_debugging_port: int = 0         # 0 = auto-assign
    chrome_flags: list[str] = []           # Additional Chrome flags
    startup_timeout_seconds: int = 15

class BrowserTimeoutConfig(ModuleConfig):
    """Per-tool timeout defaults."""
    navigate: int = 30
    click: int = 5
    type: int = 5
    screenshot: int = 10
    read_page: int = 15
    execute_js: int = 10
    fill_form: int = 30
    default: int = 10

class BrowserCookieConfig(ModuleConfig):
    """Cookie persistence settings."""
    persist: bool = False
    encryption_key_env: str = "ARCAGENT_BROWSER_COOKIE_KEY"  # Env var for Fernet key
    storage_path: str = ""                 # Defaults to workspace/browser_cookies.enc

class BrowserConfig(ModuleConfig):
    """Root browser module config."""
    security: BrowserSecurityConfig = BrowserSecurityConfig()
    connection: BrowserConnectionConfig = BrowserConnectionConfig()
    timeouts: BrowserTimeoutConfig = BrowserTimeoutConfig()
    cookies: BrowserCookieConfig = BrowserCookieConfig()
    accessibility_tree_depth: int = 10
```

### 2. CDP Client: `cdp_client.py`

Manages Chrome process lifecycle and CDP WebSocket connection.

**Responsibilities:**
- Launch headless Chrome if no `cdp_url` is configured
- Discover CDP WebSocket URL from Chrome's stderr or `/json/version` endpoint
- Maintain CDP WebSocket connection via cdp-use `CDPClient`
- Graceful shutdown: SIGTERM Chrome process, await exit, cleanup

**Chrome launch flags:**
```python
_DEFAULT_FLAGS = [
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-sync",
    "--disable-component-update",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--metrics-recording-only",
    "--mute-audio",
]
```

**Key methods:**
```python
class CDPClientManager:
    async def connect(self) -> None: ...       # Launch Chrome + connect CDP
    async def disconnect(self) -> None: ...    # Close CDP + stop Chrome
    async def send(self, domain: str, method: str, params: dict) -> dict: ...
    @property
    def connected(self) -> bool: ...
```

### 3. Accessibility: `accessibility.py`

Snapshots the accessibility tree and assigns ref IDs for LLM targeting.

**AX tree snapshot format** (returned by `browser_read_page`):
```
[1] heading "Welcome to FlightBooker"
[2] textbox "Departure City" value=""
[3] textbox "Arrival City" value=""
[4] textbox "Date" value=""
[5] button "Search Flights"
[6] link "Help" href="/help"
```

Each `[N]` is a ref ID that maps to a `backendDOMNodeId` internally.

**Key methods:**
```python
class AccessibilityManager:
    async def snapshot(self) -> str: ...
        # CDP Accessibility.getFullAXTree(depth=config.tree_depth)
        # Filter ignored nodes
        # Assign sequential ref IDs to interactive elements
        # Format as structured text

    async def resolve_ref(self, ref: int) -> int: ...
        # Returns backendDOMNodeId for a ref ID
        # Used by click/type/select to target elements

    async def get_element_text(self, ref: int) -> str: ...
        # Get text content of a specific element

    async def query_by_role_name(self, role: str, name: str) -> int | None: ...
        # CDP Accessibility.queryAXTree(role, accessibleName)
        # Returns ref ID if found
```

**Element interaction flow:**
1. Agent calls `browser_read_page()` → gets AX snapshot with ref IDs
2. Agent calls `browser_click(ref=5)` → click tool resolves ref 5 → backendDOMNodeId
3. `DOM.resolveNode(backendNodeId=...)` → get objectId
4. `DOM.getBoxModel(backendNodeId=...)` → get x,y coordinates
5. `Input.dispatchMouseEvent(type="mousePressed", x, y)` + `mouseReleased`

### 4. Tools: `tools/`

Each file exports a `create_*_tools()` function returning `list[RegisteredTool]`.

The master factory in `tools/__init__.py`:
```python
def create_browser_tools(
    cdp: CDPClientManager,
    ax: AccessibilityManager,
    config: BrowserConfig,
    telemetry: AgentTelemetry | None,
    bus: ModuleBus,
) -> list[RegisteredTool]:
    """Create all browser tools."""
    tools: list[RegisteredTool] = []
    tools.extend(create_navigate_tools(cdp, config, bus))
    tools.extend(create_interact_tools(cdp, ax, config, bus))
    tools.extend(create_form_tools(cdp, ax, config, bus))
    tools.extend(create_read_tools(cdp, ax, config, bus))
    tools.extend(create_screenshot_tools(cdp, config, bus))
    if config.security.allow_js_execution:
        tools.extend(create_javascript_tools(cdp, config, bus))
    tools.extend(create_dialog_tools(cdp, config, bus))
    tools.extend(create_cookie_tools(cdp, config, bus))
    if config.security.allow_downloads:
        tools.extend(create_download_tools(cdp, config, bus))
    return tools
```

**Tool registration pattern** (per existing codebase convention):
```python
RegisteredTool(
    name="browser_navigate",
    description="Navigate the browser to a URL. Returns the page title after navigation.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    transport=ToolTransport.NATIVE,
    execute=_handle_navigate,
    timeout_seconds=config.timeouts.navigate,
)
```

### 5. URL Security: Enforcement in navigate tools

**Pre-navigation check:**
```python
def _check_url_policy(url: str, config: BrowserSecurityConfig) -> None:
    parsed = urlparse(url)

    # Block dangerous schemes
    if parsed.scheme in config.blocked_schemes:
        raise URLBlockedError(f"Scheme '{parsed.scheme}' is blocked")

    hostname = parsed.hostname or ""

    if config.url_mode == "allowlist":
        if not any(_match_pattern(hostname, p) for p in config.url_patterns):
            raise URLBlockedError(f"Domain '{hostname}' not in allowlist")
    else:  # denylist
        if any(_match_pattern(hostname, p) for p in config.url_patterns):
            raise URLBlockedError(f"Domain '{hostname}' is blocked")
```

**Post-navigation redirect check:**
After `Page.navigate()`, check the final URL (from `Page.frameNavigated` event) against the same policy. If the redirect target is blocked, navigate to `about:blank` and return an error.

### 6. Browser Module: `browser_module.py`

Wiring class that satisfies the Module protocol.

```python
class BrowserModule:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = BrowserConfig(**(config or {}))
        self._telemetry = telemetry
        self._workspace = workspace
        self._cdp: CDPClientManager | None = None
        self._ax: AccessibilityManager | None = None

    @property
    def name(self) -> str:
        return "browser"

    async def startup(self, ctx: ModuleContext) -> None:
        self._cdp = CDPClientManager(self._config.connection)
        await self._cdp.connect()
        self._ax = AccessibilityManager(self._cdp, self._config)

        tools = create_browser_tools(
            cdp=self._cdp,
            ax=self._ax,
            config=self._config,
            telemetry=self._telemetry,
            bus=ctx.bus,
        )
        for tool in tools:
            ctx.tool_registry.register(tool)

        ctx.bus.subscribe("agent:shutdown", self._on_shutdown)
        await ctx.bus.emit("browser.connected", {"cdp_url": self._cdp.url})

    async def shutdown(self) -> None:
        if self._cdp:
            await self._cdp.disconnect()

    async def _on_shutdown(self, ctx: EventContext) -> None:
        await self.shutdown()
```

### 7. MODULE.yaml

```yaml
name: browser
version: 0.1.0
description: CDP-based browser interaction tools for web automation
author: ArcAgent
entry_point: arcagent.modules.browser:BrowserModule
cli_entry: arcagent.modules.browser.cli:cli_group
dependencies:
  - arcagent.core.module_bus
  - arcagent.core.config
events:
  subscribes:
    - agent:shutdown
  emits:
    - browser.connected
    - browser.disconnected
    - browser.error
    - browser.navigated
    - browser.clicked
    - browser.typed
    - browser.selected
    - browser.hovered
    - browser.form_filled
    - browser.screenshot_taken
    - browser.js_executed
    - browser.dialog_handled
    - browser.cookies_read
    - browser.cookies_set
    - browser.download_started
    - browser.url_blocked
```

### 8. TOML Config

```toml
[modules.browser]
enabled = true

[modules.browser.config]
accessibility_tree_depth = 10

[modules.browser.config.security]
url_mode = "denylist"           # "allowlist" for restricted environments
url_patterns = []               # ["*.internal.gov"] for allowlist
blocked_schemes = ["file", "chrome", "chrome-extension", "javascript"]
allow_js_execution = true
allow_downloads = true
download_path = "/tmp/arcagent-downloads"
redact_inputs = false
max_page_text_length = 50000
max_screenshot_width = 1920
max_screenshot_height = 1080

[modules.browser.config.connection]
cdp_url = ""                    # Leave empty to auto-launch Chrome
chrome_path = ""                # Auto-detect
headless = true
remote_debugging_port = 0       # Auto-assign
chrome_flags = []
startup_timeout_seconds = 15

[modules.browser.config.timeouts]
navigate = 30
click = 5
type = 5
screenshot = 10
read_page = 15
execute_js = 10
fill_form = 30
default = 10

[modules.browser.config.cookies]
persist = false
encryption_key_env = "ARCAGENT_BROWSER_COOKIE_KEY"
storage_path = ""
```

## Data Flow

```
Agent Loop (ArcRun) — model decides to use browser
    |
    v
ToolRegistry.wrapped_execute("browser_navigate", {"url": "https://flights.com"})
    |
    +-- 1. Validate args against input_schema
    +-- 2. Emit agent:pre_tool (policy module may veto)
    +-- 3. Execute with timeout:
    |       |
    |       v
    |   browser_navigate handler:
    |       +-- Check URL against security policy
    |       +-- CDP Page.navigate(url)
    |       +-- Wait for Page.loadEventFired
    |       +-- Check redirect URL against policy
    |       +-- Emit browser.navigated event
    |       +-- Return page title + URL
    |       |
    +-- 4. Emit agent:post_tool
    +-- 5. Audit event (tool.executed)
    |
    v
Model receives: "Navigated to https://flights.com - FlightBooker: Find Cheap Flights"
    |
    v
Model calls browser_read_page()
    |
    v
Returns:
    [1] heading "Find Your Perfect Flight"
    [2] textbox "From" value=""
    [3] textbox "To" value=""
    [4] textbox "Departure Date" value=""
    [5] combobox "Passengers" value="1"
    [6] button "Search"
    |
    v
Model calls browser_type(ref=2, text="New York")
    |
    v
Resolves ref 2 → backendDOMNodeId → click + type via CDP Input events
```

## Security Considerations

| Threat | Mitigation |
|--------|------------|
| Prompt injection via web content (LLM01) | Mark all browser content as `[EXTERNAL WEB CONTENT]` in tool results. Policy module can detect injection patterns. |
| URL bypass via redirects | Check both initial URL and post-redirect URL against security policy. Navigate to about:blank on violation. |
| JS execution RCE (ASI05) | Configurable toggle. When enabled, all scripts logged to audit trail. Consider `Page.createIsolatedWorld` for sandboxed execution in v2. |
| Credential leakage (LLM02) | Cookies encrypted at rest (Fernet). `redact_inputs = true` hides typed text from audit logs. Ephemeral sessions by default. |
| Resource exhaustion (LLM10) | Chrome process memory limit. Page text truncated to 50K chars. Screenshot capped at 1920x1080. Timeouts on all operations. |
| Chrome zombie processes (ASI08) | Process group management. SIGTERM + wait + SIGKILL. atexit handler as safety net. |
| Data exfiltration via downloads | `allow_downloads` toggle. Restricted download path. File size limits via Chrome flags. |

## Dependencies

| Package | Purpose | Version |
|---------|---------|---------|
| cdp-use | Type-safe CDP bindings (auto-generated from Chrome protocol spec) | Latest (vendored or generated) |
| websockets | CDP WebSocket transport (if cdp-use doesn't bundle it) | Latest |
| cryptography | Fernet encryption for cookie persistence | Latest |

**Note**: cdp-use is not on PyPI. Options: (1) vendor the generated code, (2) generate at build time, or (3) fall back to `python-cdp` (PyPI: `python-cdp`) if cdp-use proves unstable. The `CDPClientManager` protocol abstracts this choice — tool code doesn't depend on the specific CDP library.
