"""Screenshot tool — capture page as base64 PNG with resolution capping.

Uses CDP Page.captureScreenshot with viewport clip to enforce
maximum resolution from config.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser.tools.screenshot")


def create_screenshot_tools(
    cdp: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create screenshot tools.

    Returns:
        List containing browser_screenshot tool.
    """

    async def _handle_screenshot() -> str:
        """Capture a screenshot of the current page as base64 PNG."""
        max_w = config.security.max_screenshot_width
        max_h = config.security.max_screenshot_height

        result = await cdp.send(
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
        await bus.emit("browser.screenshot_taken", {"size": len(data)})
        _logger.info("Screenshot captured: %d bytes base64", len(data))

        return f"[EXTERNAL WEB CONTENT] data:image/png;base64,{data}"

    return [
        RegisteredTool(
            name="browser_screenshot",
            description=(
                "Capture a screenshot of the current page as base64-encoded PNG. "
                "Resolution is capped by config."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_screenshot,
            timeout_seconds=config.timeouts.screenshot,
        ),
    ]
