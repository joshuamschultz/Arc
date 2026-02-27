"""Accessibility tree management for LLM-friendly page representation.

Snapshots the accessibility tree via CDP, assigns sequential ref IDs
to interactive elements, and resolves refs back to backendDOMNodeId
for click/type/select operations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import ElementNotFoundError

_logger = logging.getLogger("arcagent.modules.browser.accessibility")

# Roles considered interactive (get ref IDs for LLM targeting)
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "heading",
        "link",
        "listbox",
        "menuitem",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)


@dataclass
class _RefEntry:
    """Internal mapping from ref ID to AX node data."""

    ref: int
    role: str
    name: str
    value: str
    backend_dom_node_id: int


class AccessibilityManager:
    """Snapshots the AX tree and assigns ref IDs for LLM targeting.

    Usage:
        1. Agent calls browser_read_page → snapshot() → formatted text with [N] refs
        2. Agent calls browser_click(ref=N) → resolve_ref(N) → backendDOMNodeId
        3. Tool uses backendDOMNodeId for DOM.resolveNode → Input events
    """

    def __init__(self, cdp: Any, config: BrowserConfig) -> None:
        self._cdp = cdp
        self._config = config
        self._refs: dict[int, _RefEntry] = {}
        self._next_ref = 1

    async def snapshot(self) -> str:
        """Fetch AX tree from CDP and format as ref-annotated text.

        Returns:
            Formatted accessibility snapshot with [N] ref IDs for
            interactive elements. Truncated to max_page_text_length.
        """
        self._refs.clear()
        self._next_ref = 1

        result = await self._cdp.send(
            "Accessibility",
            "getFullAXTree",
            {"depth": self._config.accessibility_tree_depth},
        )

        nodes = result.get("nodes", [])
        lines: list[str] = []
        max_len = self._config.security.max_page_text_length
        total_len = 0

        for node in nodes:
            if node.get("ignored", False):
                continue

            role = _get_value(node, "role")
            name = _get_value(node, "name")
            value = _get_value(node, "value")
            backend_id = node.get("backendDOMNodeId", 0)

            if not role or role == "WebArea":
                continue

            if role in _INTERACTIVE_ROLES:
                ref = self._next_ref
                self._next_ref += 1
                self._refs[ref] = _RefEntry(
                    ref=ref,
                    role=role,
                    name=name,
                    value=value,
                    backend_dom_node_id=backend_id,
                )
                line = self._format_interactive(ref, role, name, value)
            else:
                line = self._format_static(role, name)

            if line:
                lines.append(line)
                total_len += len(line) + 1  # +1 for newline
                # Early termination when text exceeds max length
                if total_len > max_len:
                    break

        text = "\n".join(lines)

        # Truncate to max length
        if len(text) > max_len:
            text = text[:max_len] + "\n[TRUNCATED]"

        return text

    def find_ref_by_name(self, name: str) -> int | None:
        """Find a ref ID by matching element name (case-insensitive).

        Tries exact match first, then partial match fallback.

        Args:
            name: The label/name to search for.

        Returns:
            The ref ID if found, None otherwise.
        """
        name_lower = name.lower()
        # Exact match first
        for ref, entry in self._refs.items():
            if entry.name.lower() == name_lower:
                return ref
        # Partial match fallback
        for ref, entry in self._refs.items():
            if name_lower in entry.name.lower():
                return ref
        return None

    def resolve_ref(self, ref: int) -> int:
        """Resolve a ref ID to a backendDOMNodeId.

        Args:
            ref: The [N] ref ID from the accessibility snapshot.

        Returns:
            The backendDOMNodeId for CDP DOM operations.

        Raises:
            ElementNotFoundError: If ref does not exist.
        """
        entry = self._refs.get(ref)
        if entry is None:
            raise ElementNotFoundError(
                message=f"Ref {ref} not found in current page snapshot",
                details={"ref": ref, "available_refs": list(self._refs.keys())},
            )
        return entry.backend_dom_node_id

    def get_element_text(self, ref: int) -> str:
        """Get the text representation of an element by ref.

        Args:
            ref: The [N] ref ID from the accessibility snapshot.

        Returns:
            Formatted text describing the element.

        Raises:
            ElementNotFoundError: If ref does not exist.
        """
        entry = self._refs.get(ref)
        if entry is None:
            raise ElementNotFoundError(
                message=f"Ref {ref} not found in current page snapshot",
                details={"ref": ref},
            )
        return self._format_interactive(entry.ref, entry.role, entry.name, entry.value)

    def _format_interactive(self, ref: int, role: str, name: str, value: str) -> str:
        """Format an interactive element with ref ID."""
        parts = [f"[{ref}]", role]
        if name:
            parts.append(f'"{name}"')
        if value:
            parts.append(f'value="{value}"')
        return " ".join(parts)

    def _format_static(self, role: str, name: str) -> str:
        """Format a non-interactive element."""
        if name:
            return f'{role} "{name}"'
        return ""


def _get_value(node: dict[str, Any], key: str) -> str:
    """Extract a string value from an AX node property."""
    prop = node.get(key)
    if isinstance(prop, dict):
        return prop.get("value", "")
    return ""
