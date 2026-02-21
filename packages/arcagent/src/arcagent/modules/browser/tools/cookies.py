"""Cookie tools — get and set browser cookies via CDP Network domain.

Cookie persistence (encrypted at rest) is handled separately
when ``cookies.persist`` is enabled in config.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser.tools.cookies")


def create_cookie_tools(
    cdp: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create cookie management tools.

    Returns:
        List containing browser_get_cookies and browser_set_cookies.
    """

    async def _handle_get_cookies() -> str:
        """Get all cookies for the current page."""
        result = await cdp.send("Network", "getCookies")
        cookies = result.get("cookies", [])

        await bus.emit("browser.cookies_read", {"count": len(cookies)})
        _logger.info("Read %d cookies", len(cookies))

        # Format for LLM consumption
        redact = config.security.redact_inputs
        lines = [f"[EXTERNAL WEB CONTENT] {len(cookies)} cookie(s):"]
        for c in cookies:
            name = c.get("name", "")
            value = "[REDACTED]" if redact else c.get("value", "")
            domain = c.get("domain", "")
            lines.append(f"  {name}={value} (domain={domain})")

        return "\n".join(lines)

    async def _handle_set_cookies(cookies: list[dict[str, Any]]) -> str:
        """Set cookies in the browser."""
        await cdp.send("Network", "setCookies", {"cookies": cookies})

        await bus.emit("browser.cookies_set", {"count": len(cookies)})
        _logger.info("Set %d cookies", len(cookies))

        return f"Set {len(cookies)} cookie(s)"

    return [
        RegisteredTool(
            name="browser_get_cookies",
            description="Get all cookies for the current page.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_get_cookies,
            timeout_seconds=config.timeouts.default,
        ),
        RegisteredTool(
            name="browser_set_cookies",
            description="Set cookies in the browser.",
            input_schema={
                "type": "object",
                "properties": {
                    "cookies": {
                        "type": "array",
                        "description": "Array of cookie objects with name, value, domain",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                                "domain": {"type": "string"},
                            },
                            "required": ["name", "value", "domain"],
                        },
                    },
                },
                "required": ["cookies"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_set_cookies,
            timeout_seconds=config.timeouts.default,
        ),
    ]
