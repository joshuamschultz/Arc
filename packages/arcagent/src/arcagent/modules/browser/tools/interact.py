"""Interact tools — click, type, select, hover via accessibility ref IDs.

All interactions resolve ref IDs through AccessibilityManager to
get backendDOMNodeId, then use CDP DOM and Input domains for
element targeting and input dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import ElementNotFoundError

_logger = logging.getLogger("arcagent.modules.browser.tools.interact")


async def _get_element_center(cdp: Any, backend_node_id: int) -> tuple[float, float]:
    """Resolve a backendDOMNodeId to its center coordinates.

    Uses DOM.getBoxModel to get the bounding box, then computes
    the center point for mouse events.

    Raises:
        ElementNotFoundError: If the element has no valid bounding box.
    """
    box_result = await cdp.send("DOM", "getBoxModel", {"backendNodeId": backend_node_id})
    content = box_result.get("model", {}).get("content", [])

    if len(content) >= 8:
        # content is [x1,y1, x2,y2, x3,y3, x4,y4] — quad corners
        x = (content[0] + content[2] + content[4] + content[6]) / 4
        y = (content[1] + content[3] + content[5] + content[7]) / 4
        return x, y

    raise ElementNotFoundError(
        message="Element has no valid bounding box",
        details={"backendNodeId": backend_node_id},
    )


async def _focus_element(cdp: Any, backend_node_id: int) -> None:
    """Focus an element via DOM.focus."""
    await cdp.send("DOM", "focus", {"backendNodeId": backend_node_id})


async def _type_text(cdp: Any, text: str) -> None:
    """Type text into the focused element using Input.insertText.

    Uses a single CDP call instead of per-character key events
    for better performance and reliability.
    """
    await cdp.send("Input", "insertText", {"text": text})


def create_interact_tools(
    cdp: Any,
    ax: AccessibilityManager,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create interaction tools.

    Returns:
        List containing browser_click, browser_type,
        browser_select, and browser_hover tools.
    """

    async def _handle_click(ref: int) -> str:
        """Click an element by its ref ID."""
        backend_id = ax.resolve_ref(ref)
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

        await bus.emit("browser.clicked", {"ref": ref, "x": x, "y": y})
        _logger.info("Clicked ref %d at (%.0f, %.0f)", ref, x, y)
        return f"Clicked element [ref={ref}]"

    async def _handle_type(ref: int, text: str) -> str:
        """Type text into an element by its ref ID."""
        backend_id = ax.resolve_ref(ref)
        await _focus_element(cdp, backend_id)
        await _type_text(cdp, text)

        redacted = "[REDACTED]" if config.security.redact_inputs else text
        await bus.emit("browser.typed", {"ref": ref, "text": redacted})
        _logger.info("Typed %d chars into ref %d", len(text), ref)
        return f"Typed '{redacted}' into element [ref={ref}]"

    async def _handle_select(ref: int, value: str) -> str:
        """Select an option in a dropdown by its ref ID."""
        backend_id = ax.resolve_ref(ref)

        # Use Runtime.callFunctionOn with arguments to avoid JS injection
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

        await bus.emit("browser.selected", {"ref": ref, "value": value})
        _logger.info("Selected '%s' in ref %d", value, ref)
        return f"Selected '{value}' in element [ref={ref}]"

    async def _handle_hover(ref: int) -> str:
        """Hover over an element by its ref ID."""
        backend_id = ax.resolve_ref(ref)
        x, y = await _get_element_center(cdp, backend_id)

        await cdp.send(
            "Input",
            "dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y},
        )

        await bus.emit("browser.hovered", {"ref": ref, "x": x, "y": y})
        _logger.info("Hovered ref %d at (%.0f, %.0f)", ref, x, y)
        return f"Hovered over element [ref={ref}]"

    return [
        RegisteredTool(
            name="browser_click",
            description="Click an element by its [ref] ID from browser_read_page.",
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
            execute=_handle_click,
            timeout_seconds=config.timeouts.click,
        ),
        RegisteredTool(
            name="browser_type",
            description="Type text into an input element by its [ref] ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "The [N] ref ID from browser_read_page",
                    },
                    "text": {
                        "type": "string",
                        "description": "The text to type",
                    },
                },
                "required": ["ref", "text"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_type,
            timeout_seconds=config.timeouts.type_,
        ),
        RegisteredTool(
            name="browser_select",
            description="Select an option in a dropdown by its [ref] ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "The [N] ref ID from browser_read_page",
                    },
                    "value": {
                        "type": "string",
                        "description": "The value to select",
                    },
                },
                "required": ["ref", "value"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_select,
            timeout_seconds=config.timeouts.click,
        ),
        RegisteredTool(
            name="browser_hover",
            description="Hover over an element by its [ref] ID.",
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
            execute=_handle_hover,
            timeout_seconds=config.timeouts.click,
        ),
    ]
