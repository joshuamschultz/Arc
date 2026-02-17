"""JavaScript execution tool — Runtime.evaluate with configurable toggle.

JS execution is gated by ``security.allow_js_execution`` in config.
When enabled, all executions are logged to the audit trail.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser.tools.javascript")


def create_javascript_tools(
    cdp: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create JavaScript execution tools.

    Returns:
        List containing browser_execute_js tool.
    """

    async def _handle_execute_js(expression: str) -> str:
        """Execute JavaScript in the page context and return the result."""
        result = await cdp.send(
            "Runtime",
            "evaluate",
            {
                "expression": expression,
                "returnByValue": True,
            },
        )

        # Check for exceptions
        if "exceptionDetails" in result:
            error_text = result["exceptionDetails"].get("text", "Unknown JS error")
            await bus.emit(
                "browser.js_executed",
                {"expression": expression, "error": error_text},
            )
            _logger.warning("JS execution error: %s", error_text)
            return f"[EXTERNAL WEB CONTENT] JS Error: {error_text}"

        value = result.get("result", {}).get("value", "")
        value_type = result.get("result", {}).get("type", "undefined")

        await bus.emit(
            "browser.js_executed",
            {"expression": expression, "type": value_type},
        )
        _logger.info("JS executed: %s → %s", expression[:50], value_type)

        return f"[EXTERNAL WEB CONTENT] {value}"

    return [
        RegisteredTool(
            name="browser_execute_js",
            description=(
                "Execute JavaScript in the page context. Returns the "
                "result value as a string. Use for extracting data or "
                "performing actions not available via other tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "JavaScript expression to evaluate",
                    },
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_execute_js,
            timeout_seconds=config.timeouts.execute_js,
        ),
    ]
