"""Tests for AccessibilityManager — AX tree parsing, ref IDs, element resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import ElementNotFoundError


def _make_cdp() -> AsyncMock:
    """Create a mock CDPClientManager."""
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


# Minimal AX tree response from Accessibility.getFullAXTree
_SAMPLE_AX_TREE = {
    "nodes": [
        {
            "nodeId": "1",
            "role": {"value": "WebArea"},
            "name": {"value": "Test Page"},
            "backendDOMNodeId": 1,
            "childIds": ["2", "3", "4", "5"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"value": "heading"},
            "name": {"value": "Welcome"},
            "backendDOMNodeId": 10,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "3",
            "role": {"value": "textbox"},
            "name": {"value": "Username"},
            "value": {"value": ""},
            "backendDOMNodeId": 20,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "4",
            "role": {"value": "button"},
            "name": {"value": "Submit"},
            "backendDOMNodeId": 30,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "5",
            "role": {"value": "generic"},
            "name": {"value": ""},
            "backendDOMNodeId": 40,
            "childIds": [],
            "ignored": True,
        },
    ]
}


class TestAccessibilitySnapshot:
    """snapshot() returns formatted AX tree with ref IDs."""

    async def test_snapshot_returns_formatted_text(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()

        ax = AccessibilityManager(cdp, config)
        result = await ax.snapshot()

        # Should contain ref IDs for interactive elements
        assert "[1]" in result
        assert "heading" in result
        assert "Welcome" in result
        assert "[2]" in result
        assert "textbox" in result
        assert "Username" in result
        assert "[3]" in result
        assert "button" in result
        assert "Submit" in result

    async def test_snapshot_excludes_ignored_nodes(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()

        ax = AccessibilityManager(cdp, config)
        result = await ax.snapshot()

        # Ignored node should not appear
        assert "generic" not in result

    async def test_snapshot_truncates_to_max_length(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig(security={"max_page_text_length": 20})  # type: ignore[arg-type]

        ax = AccessibilityManager(cdp, config)
        result = await ax.snapshot()

        assert len(result) <= 20 + len("\n[TRUNCATED]")


class TestAccessibilityResolveRef:
    """resolve_ref() maps ref IDs to backendDOMNodeId."""

    async def test_resolve_valid_ref(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()

        ax = AccessibilityManager(cdp, config)
        await ax.snapshot()  # Populates ref map

        # Ref 1 → heading (backendDOMNodeId=10)
        backend_id = ax.resolve_ref(1)
        assert backend_id == 10

    async def test_resolve_invalid_ref_raises(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()

        ax = AccessibilityManager(cdp, config)
        await ax.snapshot()

        with pytest.raises(ElementNotFoundError, match="Ref 999"):
            ax.resolve_ref(999)


class TestAccessibilityGetElementText:
    """get_element_text() returns text of a specific element."""

    async def test_get_element_text(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()

        ax = AccessibilityManager(cdp, config)
        await ax.snapshot()

        text = ax.get_element_text(1)
        assert "Welcome" in text
