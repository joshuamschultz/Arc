"""Form fill tool — compound multi-field form filling.

Takes a dict of field_label -> value, finds each field by label
match in the accessibility tree, and types the value. Reports
which fields succeeded and which were not found.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.tools.interact import _focus_element, _type_text

_logger = logging.getLogger("arcagent.modules.browser.tools.form")


def create_form_tools(
    cdp: Any,
    ax: AccessibilityManager,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create form filling tools.

    Returns:
        List containing browser_fill_form tool.
    """

    async def _handle_fill_form(fields: dict[str, str]) -> str:
        """Fill multiple form fields by label matching.

        Looks up each field label in the current AX snapshot refs
        and types the value. Returns a summary of results.
        """
        succeeded: list[str] = []
        failed: list[str] = []

        for label, value in fields.items():
            ref = ax.find_ref_by_name(label)
            if ref is None:
                failed.append(label)
                continue

            # Focus and type into the field using shared helpers
            backend_id = ax.resolve_ref(ref)
            await _focus_element(cdp, backend_id)
            await _type_text(cdp, value)

            succeeded.append(label)

        await bus.emit(
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

    return [
        RegisteredTool(
            name="browser_fill_form",
            description=(
                "Fill multiple form fields at once. Takes a dict of "
                "field_label -> value. Finds each field by label match "
                "in the accessibility tree and types the value."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "object",
                        "description": "Map of field label to value to type",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_fill_form,
            timeout_seconds=config.timeouts.fill_form,
        ),
    ]
