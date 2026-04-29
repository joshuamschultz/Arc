"""Decorator-form browser module — SPEC-021 task 3.3.

A single ``@capability(name="browser")`` class manages the CDP client +
accessibility manager lifecycle (setup launches Chrome / connects;
teardown closes the WebSocket and reaps the Chrome process).

Module-level ``@tool`` functions delegate to
``_runtime.state().cdp_client`` and ``ax_manager``, mirroring the
behaviour of the legacy ``create_*_tools`` factories.

State is shared via :mod:`arcagent.modules.browser._runtime`. The agent
configures it once at startup; the capability ``setup()`` populates the
CDP client; the @tool functions read state lazily.

The legacy :class:`BrowserModule` class still exists alongside this
module to keep its existing test surface working; both forms route to
the same :class:`CDPClientManager` and :class:`AccessibilityManager`
classes internally.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.browser import _runtime
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.cdp_client import CDPClientManager
from arcagent.modules.browser.errors import URLBlockedError
from arcagent.modules.browser.tools.navigate import (
    _check_url_policy,
    _get_current_url,
)
from arcagent.tools._decorator import capability, tool

_logger = logging.getLogger("arcagent.modules.browser.capabilities")


# --- Capability lifecycle --------------------------------------------------


@capability(name="browser")
class BrowserCapability:
    """Manage Chrome / CDP lifecycle for the browser module.

    ``setup()`` connects to (or launches) Chrome via CDP and creates the
    accessibility manager. ``teardown()`` disconnects the WebSocket and
    terminates the Chrome process (SIGTERM with SIGKILL fallback per
    :class:`CDPClientManager.disconnect`).
    """

    async def setup(self, _ctx: Any) -> None:
        """Connect CDP and prepare the accessibility manager."""
        st = _runtime.state()
        cdp = CDPClientManager(st.config.connection)
        await cdp.connect()
        st.cdp_client = cdp
        st.ax_manager = AccessibilityManager(cdp, st.config)
        if st.bus is not None:
            await st.bus.emit(
                "browser.connected",
                {"cdp_url": cdp.url},
            )
        _logger.info("Browser capability started (cdp=%s)", cdp.url)

    async def teardown(self) -> None:
        """Disconnect CDP and reap the Chrome process."""
        st = _runtime.state()
        cdp = st.cdp_client
        if cdp is not None:
            await cdp.disconnect()
        st.cdp_client = None
        st.ax_manager = None
        if st.bus is not None:
            await st.bus.emit("browser.disconnected", {})
        _logger.info("Browser capability shut down")


# --- Internal helpers ------------------------------------------------------


def _cdp() -> CDPClientManager:
    """Return the live CDP client. Raises if setup hasn't run."""
    st = _runtime.state()
    if st.cdp_client is None:
        raise RuntimeError(
            "browser CDP client not initialised; "
            "BrowserCapability.setup() must run before tools can be used"
        )
    return st.cdp_client


def _ax() -> AccessibilityManager:
    """Return the live accessibility manager. Raises if setup hasn't run."""
    st = _runtime.state()
    if st.ax_manager is None:
        raise RuntimeError(
            "browser accessibility manager not initialised; "
            "BrowserCapability.setup() must run before tools can be used"
        )
    ax: AccessibilityManager = st.ax_manager
    return ax


async def _emit(event: str, payload: dict[str, Any]) -> None:
    """Emit an event on the configured bus, no-op if bus is None."""
    bus = _runtime.state().bus
    if bus is not None:
        await bus.emit(event, payload)


# --- Navigation tools ------------------------------------------------------


@tool(
    name="browser_navigate",
    description=(
        "Navigate the browser to a URL. Returns the page title after "
        "navigation. URL must pass security policy."
    ),
    classification="state_modifying",
    capability_tags=("browser_navigate",),
)
async def browser_navigate(url: str) -> str:
    """Navigate to a URL. Returns page title after navigation."""
    cdp = _cdp()
    config = _runtime.state().config
    try:
        _check_url_policy(url, config.security)
    except URLBlockedError:
        await _emit("browser.url_blocked", {"url": url})
        raise

    await cdp.send("Page", "navigate", {"url": url})
    try:
        await cdp.send("Page", "loadEventFired")
    except Exception:
        _logger.debug("loadEventFired not received (ignored)", exc_info=True)

    final_url = await _get_current_url(cdp)
    if final_url and final_url != url:
        try:
            _check_url_policy(final_url, config.security)
        except URLBlockedError:
            await _emit(
                "browser.url_blocked",
                {"url": final_url, "original_url": url, "redirect": True},
            )
            await cdp.send("Page", "navigate", {"url": "about:blank"})
            raise

    title_result = await cdp.send("Runtime", "evaluate", {"expression": "document.title"})
    title = title_result.get("result", {}).get("value", "")
    await _emit("browser.navigated", {"url": final_url or url, "title": title})
    _logger.info("Navigated to %s — %s", final_url or url, title)
    return f"[EXTERNAL WEB CONTENT] Navigated to {final_url or url} — {title}"


async def _history_navigate(method: str, direction: str) -> str:
    """Navigate back/forward, validate resulting URL against policy."""
    cdp = _cdp()
    config = _runtime.state().config
    await cdp.send("Page", method)
    current_url = await _get_current_url(cdp)
    if current_url:
        try:
            _check_url_policy(current_url, config.security)
        except URLBlockedError:
            await _emit("browser.url_blocked", {"url": current_url})
            await cdp.send("Page", "navigate", {"url": "about:blank"})
            raise
    return f"[EXTERNAL WEB CONTENT] Navigated {direction} to {current_url}"


@tool(
    name="browser_go_back",
    description="Navigate the browser back in history.",
    classification="state_modifying",
)
async def browser_go_back() -> str:
    """Navigate back in browser history."""
    return await _history_navigate("goBack", "back")


@tool(
    name="browser_go_forward",
    description="Navigate the browser forward in history.",
    classification="state_modifying",
)
async def browser_go_forward() -> str:
    """Navigate forward in browser history."""
    return await _history_navigate("goForward", "forward")


@tool(
    name="browser_reload",
    description="Reload the current page.",
    classification="state_modifying",
)
async def browser_reload() -> str:
    """Reload the current page."""
    await _cdp().send("Page", "reload")
    return "Page reloaded"


# --- Read tools ------------------------------------------------------------


@tool(
    name="browser_read_page",
    description=(
        "Read the current page as a structured accessibility snapshot "
        "with [N] ref IDs for interactive elements. Use these ref IDs "
        "with browser_click, browser_type, etc."
    ),
    classification="read_only",
)
async def browser_read_page() -> str:
    """Return an accessibility snapshot of the current page."""
    snapshot = await _ax().snapshot()
    _logger.info("Page read: %d chars", len(snapshot))
    return f"[EXTERNAL WEB CONTENT]\n{snapshot}"


@tool(
    name="browser_get_element_text",
    description="Get the text content of a specific element by its ref ID.",
    classification="read_only",
)
async def browser_get_element_text(ref: int) -> str:
    """Return the text of an element identified by its ref ID."""
    text = _ax().get_element_text(ref)
    return f"[EXTERNAL WEB CONTENT] {text}"


# --- Screenshot ------------------------------------------------------------


@tool(
    name="browser_screenshot",
    description=(
        "Capture a screenshot of the current page as base64-encoded PNG. "
        "Resolution is capped by config."
    ),
    classification="read_only",
)
async def browser_screenshot() -> str:
    """Capture a base64-encoded PNG screenshot of the current page."""
    cfg = _runtime.state().config
    max_w = cfg.security.max_screenshot_width
    max_h = cfg.security.max_screenshot_height
    result = await _cdp().send(
        "Page",
        "captureScreenshot",
        {
            "format": "png",
            "clip": {
                "x": 0,
                "y": 0,
                "width": max_w,
                "height": max_h,
                "scale": 1,
            },
        },
    )
    data: str = result.get("data", "")
    await _emit("browser.screenshot_taken", {"size": len(data)})
    _logger.info("Screenshot captured: %d bytes base64", len(data))
    return f"[EXTERNAL WEB CONTENT] data:image/png;base64,{data}"


# --- Interaction tools -----------------------------------------------------


async def _get_element_center(cdp: CDPClientManager, backend_node_id: int) -> tuple[float, float]:
    """Resolve a backendDOMNodeId to its center coordinates."""
    from arcagent.modules.browser.errors import ElementNotFoundError

    box_result = await cdp.send("DOM", "getBoxModel", {"backendNodeId": backend_node_id})
    content = box_result.get("model", {}).get("content", [])
    if len(content) >= 8:
        x = (content[0] + content[2] + content[4] + content[6]) / 4
        y = (content[1] + content[3] + content[5] + content[7]) / 4
        return x, y
    raise ElementNotFoundError(
        message="Element has no valid bounding box",
        details={"backendNodeId": backend_node_id},
    )


@tool(
    name="browser_click",
    description="Click an element by its [ref] ID from browser_read_page.",
    classification="state_modifying",
)
async def browser_click(ref: int) -> str:
    """Click an element by its ref ID."""
    cdp = _cdp()
    backend_id = _ax().resolve_ref(ref)
    x, y = await _get_element_center(cdp, backend_id)
    await cdp.send(
        "Input",
        "dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    await cdp.send(
        "Input",
        "dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    await _emit("browser.clicked", {"ref": ref, "x": x, "y": y})
    _logger.info("Clicked ref %d at (%.0f, %.0f)", ref, x, y)
    return f"Clicked element [ref={ref}]"


@tool(
    name="browser_type",
    description="Type text into an input element by its [ref] ID.",
    classification="state_modifying",
)
async def browser_type(ref: int, text: str) -> str:
    """Type text into an input element by its ref ID."""
    cdp = _cdp()
    cfg = _runtime.state().config
    backend_id = _ax().resolve_ref(ref)
    await cdp.send("DOM", "focus", {"backendNodeId": backend_id})
    await cdp.send("Input", "insertText", {"text": text})
    redacted = "[REDACTED]" if cfg.security.redact_inputs else text
    await _emit("browser.typed", {"ref": ref, "text": redacted})
    _logger.info("Typed %d chars into ref %d", len(text), ref)
    return f"Typed '{redacted}' into element [ref={ref}]"


@tool(
    name="browser_select",
    description="Select an option in a dropdown by its [ref] ID.",
    classification="state_modifying",
)
async def browser_select(ref: int, value: str) -> str:
    """Select an option in a dropdown by its ref ID."""
    cdp = _cdp()
    backend_id = _ax().resolve_ref(ref)
    resolve_result = await cdp.send("DOM", "resolveNode", {"backendNodeId": backend_id})
    object_id = resolve_result.get("object", {}).get("objectId", "")
    await cdp.send(
        "Runtime",
        "callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": (
                "function(val) {"
                "  this.value = val;"
                "  this.dispatchEvent(new Event('change', {bubbles: true}));"
                "}"
            ),
            "arguments": [{"value": value}],
        },
    )
    await _emit("browser.selected", {"ref": ref, "value": value})
    _logger.info("Selected '%s' in ref %d", value, ref)
    return f"Selected '{value}' in element [ref={ref}]"


@tool(
    name="browser_hover",
    description="Hover over an element by its [ref] ID.",
    classification="state_modifying",
)
async def browser_hover(ref: int) -> str:
    """Hover over an element by its ref ID."""
    cdp = _cdp()
    backend_id = _ax().resolve_ref(ref)
    x, y = await _get_element_center(cdp, backend_id)
    await cdp.send(
        "Input",
        "dispatchMouseEvent",
        {"type": "mouseMoved", "x": x, "y": y},
    )
    await _emit("browser.hovered", {"ref": ref, "x": x, "y": y})
    _logger.info("Hovered ref %d at (%.0f, %.0f)", ref, x, y)
    return f"Hovered over element [ref={ref}]"


# --- Form tool -------------------------------------------------------------


@tool(
    name="browser_fill_form",
    description=(
        "Fill multiple form fields at once. Takes a dict of "
        "field_label -> value. Finds each field by label match in the "
        "accessibility tree and types the value."
    ),
    classification="state_modifying",
)
async def browser_fill_form(fields: dict[str, str]) -> str:
    """Fill multiple form fields by label matching."""
    cdp = _cdp()
    ax = _ax()
    succeeded: list[str] = []
    failed: list[str] = []
    for label, value in fields.items():
        ref = ax.find_ref_by_name(label)
        if ref is None:
            failed.append(label)
            continue
        backend_id = ax.resolve_ref(ref)
        await cdp.send("DOM", "focus", {"backendNodeId": backend_id})
        await cdp.send("Input", "insertText", {"text": value})
        succeeded.append(label)
    await _emit(
        "browser.form_filled",
        {"succeeded": succeeded, "failed": failed},
    )
    parts = [f"Filled {len(succeeded)} field(s)"]
    if succeeded:
        parts.append(f"Succeeded: {', '.join(succeeded)}")
    if failed:
        parts.append(f"Not found: {', '.join(failed)}")
    result = ". ".join(parts)
    _logger.info("Form fill: %s", result)
    return result


# --- Dialog ----------------------------------------------------------------


@tool(
    name="browser_handle_dialog",
    description=(
        "Handle a JavaScript dialog (alert, confirm, or prompt). Use "
        "action='accept' or action='dismiss'. For prompt dialogs, "
        "provide text to type."
    ),
    classification="state_modifying",
)
async def browser_handle_dialog(action: str, text: str = "") -> str:
    """Accept or dismiss a JavaScript dialog."""
    accept = action.lower() == "accept"
    params: dict[str, Any] = {"accept": accept}
    if text:
        params["promptText"] = text
    await _cdp().send("Page", "handleJavaScriptDialog", params)
    await _emit("browser.dialog_handled", {"action": action, "text": text})
    verb = "Accepted" if accept else "Dismissed"
    _logger.info("Dialog %s", verb.lower())
    return f"{verb} dialog"


# --- Cookies ---------------------------------------------------------------


@tool(
    name="browser_get_cookies",
    description="Get all cookies for the current page.",
    classification="read_only",
)
async def browser_get_cookies() -> str:
    """Return all cookies for the current page as a formatted string."""
    cfg = _runtime.state().config
    result = await _cdp().send("Network", "getCookies")
    cookies = result.get("cookies", [])
    await _emit("browser.cookies_read", {"count": len(cookies)})
    _logger.info("Read %d cookies", len(cookies))
    redact = cfg.security.redact_inputs
    lines = [f"[EXTERNAL WEB CONTENT] {len(cookies)} cookie(s):"]
    for c in cookies:
        name = c.get("name", "")
        value = "[REDACTED]" if redact else c.get("value", "")
        domain = c.get("domain", "")
        lines.append(f"  {name}={value} (domain={domain})")
    return "\n".join(lines)


@tool(
    name="browser_set_cookies",
    description="Set cookies in the browser.",
    classification="state_modifying",
)
async def browser_set_cookies(cookies: list[dict[str, Any]]) -> str:
    """Set cookies in the browser."""
    await _cdp().send("Network", "setCookies", {"cookies": cookies})
    await _emit("browser.cookies_set", {"count": len(cookies)})
    _logger.info("Set %d cookies", len(cookies))
    return f"Set {len(cookies)} cookie(s)"


# --- JavaScript ------------------------------------------------------------


@tool(
    name="browser_execute_js",
    description=(
        "Execute JavaScript in the page context. Returns the result "
        "value as a string. Use for extracting data or performing "
        "actions not available via other tools."
    ),
    classification="state_modifying",
)
async def browser_execute_js(expression: str) -> str:
    """Execute JavaScript in the page context, return the result."""
    result = await _cdp().send(
        "Runtime",
        "evaluate",
        {"expression": expression, "returnByValue": True},
    )
    if "exceptionDetails" in result:
        error_text = result["exceptionDetails"].get("text", "Unknown JS error")
        await _emit(
            "browser.js_executed",
            {"expression": expression, "error": error_text},
        )
        _logger.warning("JS execution error: %s", error_text)
        return f"[EXTERNAL WEB CONTENT] JS Error: {error_text}"
    value = result.get("result", {}).get("value", "")
    value_type = result.get("result", {}).get("type", "undefined")
    await _emit(
        "browser.js_executed",
        {"expression": expression, "type": value_type},
    )
    _logger.info("JS executed: %s → %s", expression[:50], value_type)
    return f"[EXTERNAL WEB CONTENT] {value}"


# --- Download --------------------------------------------------------------


@tool(
    name="browser_download_file",
    description=(
        "Download a file by navigating to its URL. Files are saved to "
        "the configured download path. URL must pass security policy."
    ),
    classification="state_modifying",
)
async def browser_download_file(url: str) -> str:
    """Download a file by navigating to its URL."""
    cdp = _cdp()
    cfg = _runtime.state().config
    _check_url_policy(url, cfg.security)
    download_path = cfg.security.download_path
    await cdp.send(
        "Browser",
        "setDownloadBehavior",
        {"behavior": "allow", "downloadPath": download_path},
    )
    await cdp.send("Page", "navigate", {"url": url})
    await _emit(
        "browser.download_started",
        {"url": url, "path": download_path},
    )
    _logger.info("Download started: %s → %s", url, download_path)
    return f"Download started: {url} → {download_path}"
