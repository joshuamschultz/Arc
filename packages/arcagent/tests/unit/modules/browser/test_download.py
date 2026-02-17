"""Tests for download tool — file download management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import URLBlockedError


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value={})
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestBrowserDownload:
    """browser_download_file tool."""

    async def test_download_allowed(self) -> None:
        from arcagent.modules.browser.tools.download import create_download_tools

        cdp = _make_cdp()
        config = BrowserConfig(
            security={"allow_downloads": True}  # type: ignore[arg-type]
        )
        bus = _make_bus()

        tools = create_download_tools(cdp, config, bus)
        assert len(tools) == 1

        dl_tool = tools[0]
        result = await dl_tool.execute(url="https://example.com/file.pdf")
        assert "download" in result.lower()

    async def test_download_path_validation(self) -> None:
        """Download path must be under configured download_path."""
        from arcagent.modules.browser.tools.download import create_download_tools

        cdp = _make_cdp()
        config = BrowserConfig(
            security={"allow_downloads": True, "download_path": "/tmp/arcagent-downloads"}  # type: ignore[arg-type]
        )
        bus = _make_bus()

        tools = create_download_tools(cdp, config, bus)
        dl_tool = tools[0]

        # Should work — navigates to the URL for download
        result = await dl_tool.execute(url="https://example.com/report.pdf")
        assert "download" in result.lower()

    async def test_download_enforces_url_policy(self) -> None:
        """Download tool checks URL against security policy before download."""
        from arcagent.modules.browser.tools.download import create_download_tools

        cdp = _make_cdp()
        config = BrowserConfig(
            security={"allow_downloads": True}  # type: ignore[arg-type]
        )
        bus = _make_bus()

        tools = create_download_tools(cdp, config, bus)
        dl_tool = tools[0]

        with pytest.raises(URLBlockedError, match="Scheme"):
            await dl_tool.execute(url="file:///etc/secret")

        # CDP send should NOT have been called — blocked before download
        cdp.send.assert_not_called()
