"""Read tools — page content via accessibility snapshot.

Provides browser_read_page (full AX snapshot with ref IDs) and
browser_get_element_text (text of a specific element by ref).
All returned content is marked as [EXTERNAL WEB CONTENT] to
protect against prompt injection (OWASP LLM01).
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser.tools.read")


def create_read_tools(
    cdp: Any,
    ax: AccessibilityManager,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create page reading tools.

    Returns:
        List containing browser_read_page and browser_get_element_text.
    """

    async def _handle_read_page() -> str:
        """Read the current page as an accessibility snapshot with ref IDs."""
        snapshot = await ax.snapshot()
        _logger.info("Page read: %d chars", len(snapshot))
        return f"[EXTERNAL WEB CONTENT]\n{snapshot}"

    async def _handle_get_element_text(ref: int) -> str:
        """Get the text of a specific element by its ref ID."""
        text = ax.get_element_text(ref)
        return f"[EXTERNAL WEB CONTENT] {text}"

    return [
        RegisteredTool(
            name="browser_read_page",
            description=(
                "Read the current page as a structured accessibility "
                "snapshot with [N] ref IDs for interactive elements. "
                "Use these ref IDs with browser_click, browser_type, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_read_page,
            timeout_seconds=config.timeouts.read_page,
        ),
        RegisteredTool(
            name="browser_get_element_text",
            description="Get the text content of a specific element by its ref ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "The [N] ref ID from browser_read_page",
                    },
                },
                "required": ["ref"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_get_element_text,
            timeout_seconds=config.timeouts.default,
        ),
    ]
