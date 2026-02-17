"""Dialog handling tool — accept/dismiss/type for JavaScript dialogs.

Handles alert(), confirm(), and prompt() dialogs via CDP
Page.handleJavaScriptDialog.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser.tools.dialog")


def create_dialog_tools(
    cdp: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create dialog handling tools.

    Returns:
        List containing browser_handle_dialog tool.
    """

    async def _handle_dialog(action: str, text: str = "") -> str:
        """Handle a JavaScript dialog (alert/confirm/prompt)."""
        accept = action.lower() == "accept"

        params: dict[str, Any] = {"accept": accept}
        if text:
            params["promptText"] = text

        await cdp.send("Page", "handleJavaScriptDialog", params)

        await bus.emit(
            "browser.dialog_handled",
            {"action": action, "text": text},
        )
        verb = "Accepted" if accept else "Dismissed"
        _logger.info("Dialog %s", verb.lower())
        return f"{verb} dialog"

    return [
        RegisteredTool(
            name="browser_handle_dialog",
            description=(
                "Handle a JavaScript dialog (alert, confirm, or prompt). "
                "Use action='accept' or action='dismiss'. For prompt "
                "dialogs, provide text to type."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["accept", "dismiss"],
                        "description": "Whether to accept or dismiss the dialog",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type in a prompt dialog (optional)",
                        "default": "",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_dialog,
            timeout_seconds=config.timeouts.default,
        ),
    ]
