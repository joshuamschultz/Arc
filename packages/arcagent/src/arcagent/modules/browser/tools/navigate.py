"""Navigate tools — URL navigation with security policy enforcement.

Provides browser_navigate, browser_go_back, browser_go_forward,
and browser_reload. URL policy is checked both pre-navigation
and post-redirect.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any
from urllib.parse import urlparse

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.config import BrowserConfig, BrowserSecurityConfig
from arcagent.modules.browser.errors import URLBlockedError

_logger = logging.getLogger("arcagent.modules.browser.tools.navigate")


def _check_url_policy(url: str, config: BrowserSecurityConfig) -> None:
    """Validate a URL against the security policy.

    Checks scheme blocklist, then allowlist/denylist domain patterns.

    Args:
        url: The URL to validate.
        config: Security config with url_mode and url_patterns.

    Raises:
        URLBlockedError: If the URL violates the security policy.
    """
    parsed = urlparse(url)

    # Block dangerous schemes
    if parsed.scheme in config.blocked_schemes:
        raise URLBlockedError(
            message=f"Scheme '{parsed.scheme}' is blocked by security policy",
            details={"url": url, "scheme": parsed.scheme},
        )

    hostname = parsed.hostname or ""

    if config.url_mode == "allowlist":
        if not any(_match_pattern(hostname, p) for p in config.url_patterns):
            raise URLBlockedError(
                message=f"Domain '{hostname}' not in allowlist",
                details={"url": url, "hostname": hostname, "mode": "allowlist"},
            )
    else:  # denylist
        if any(_match_pattern(hostname, p) for p in config.url_patterns):
            raise URLBlockedError(
                message=f"Domain '{hostname}' is blocked by denylist",
                details={"url": url, "hostname": hostname, "mode": "denylist"},
            )


def _match_pattern(hostname: str, pattern: str) -> bool:
    """Match a hostname against a glob-style domain pattern.

    Supports patterns like ``*.example.com`` and ``example.com``.
    """
    return fnmatch.fnmatch(hostname, pattern)


async def _get_current_url(cdp: Any) -> str:
    """Get the current page URL via Runtime.evaluate."""
    result = await cdp.send("Runtime", "evaluate", {"expression": "window.location.href"})
    url: str = result.get("result", {}).get("value", "")
    return url


def create_navigate_tools(
    cdp: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create navigation tools.

    Returns:
        List containing browser_navigate, browser_go_back,
        browser_go_forward, and browser_reload tools.
    """

    async def _handle_navigate(url: str) -> str:
        """Navigate to a URL. Returns page title after navigation."""
        try:
            _check_url_policy(url, config.security)
        except URLBlockedError:
            await bus.emit("browser.url_blocked", {"url": url})
            raise

        await cdp.send("Page", "navigate", {"url": url})

        # Wait for page load before reading title
        try:
            await cdp.send("Page", "loadEventFired")
        except Exception:
            _logger.debug("loadEventFired not received (ignored)", exc_info=True)

        # Post-redirect URL validation
        final_url = await _get_current_url(cdp)
        if final_url and final_url != url:
            try:
                _check_url_policy(final_url, config.security)
            except URLBlockedError:
                await bus.emit(
                    "browser.url_blocked",
                    {"url": final_url, "original_url": url, "redirect": True},
                )
                # Navigate away from the blocked page
                await cdp.send("Page", "navigate", {"url": "about:blank"})
                raise

        # Get the page title after navigation
        title_result = await cdp.send(
            "Runtime",
            "evaluate",
            {"expression": "document.title"},
        )
        title = title_result.get("result", {}).get("value", "")

        await bus.emit("browser.navigated", {"url": final_url or url, "title": title})
        _logger.info("Navigated to %s — %s", final_url or url, title)

        return f"[EXTERNAL WEB CONTENT] Navigated to {final_url or url} — {title}"

    async def _history_navigate(method: str, direction: str) -> str:
        """Navigate back/forward, validate resulting URL against policy."""
        await cdp.send("Page", method)
        current_url = await _get_current_url(cdp)
        if current_url:
            try:
                _check_url_policy(current_url, config.security)
            except URLBlockedError:
                await bus.emit("browser.url_blocked", {"url": current_url})
                await cdp.send("Page", "navigate", {"url": "about:blank"})
                raise
        return f"[EXTERNAL WEB CONTENT] Navigated {direction} to {current_url}"

    async def _handle_go_back() -> str:
        return await _history_navigate("goBack", "back")

    async def _handle_go_forward() -> str:
        return await _history_navigate("goForward", "forward")

    async def _handle_reload() -> str:
        await cdp.send("Page", "reload")
        return "Page reloaded"

    return [
        RegisteredTool(
            name="browser_navigate",
            description=(
                "Navigate the browser to a URL. Returns the page title "
                "after navigation. URL must pass security policy."
            ),
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
        ),
        RegisteredTool(
            name="browser_go_back",
            description="Navigate the browser back in history.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_go_back,
            timeout_seconds=config.timeouts.default,
        ),
        RegisteredTool(
            name="browser_go_forward",
            description="Navigate the browser forward in history.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_go_forward,
            timeout_seconds=config.timeouts.default,
        ),
        RegisteredTool(
            name="browser_reload",
            description="Reload the current page.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_reload,
            timeout_seconds=config.timeouts.default,
        ),
    ]
