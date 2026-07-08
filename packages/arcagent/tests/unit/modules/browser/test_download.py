"""Tests for download tool — file download management."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.capabilities import browser_download_file
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import URLBlockedError


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value={})
    return cdp


class TestBrowserDownload:
    """browser_download_file tool."""

    async def test_download_allowed(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        config = BrowserConfig(security={"allow_downloads": True})  # type: ignore[arg-type]
        configure_browser(config, cdp=cdp)

        result = await browser_download_file(url="https://example.com/file.pdf")
        assert "download" in result.lower()

    async def test_download_path_validation(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Download navigates to the URL for the configured download path."""
        cdp = _make_cdp()
        config = BrowserConfig(
            security={"allow_downloads": True, "download_path": "/tmp/arcagent-downloads"}  # type: ignore[arg-type]
        )
        configure_browser(config, cdp=cdp)

        result = await browser_download_file(url="https://example.com/report.pdf")
        assert "download" in result.lower()

    async def test_download_enforces_url_policy(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Download tool checks URL against security policy before download."""
        cdp = _make_cdp()
        config = BrowserConfig(security={"allow_downloads": True})  # type: ignore[arg-type]
        configure_browser(config, cdp=cdp)

        with pytest.raises(URLBlockedError, match="Scheme"):
            await browser_download_file(url="file:///etc/secret")

        # CDP send should NOT have been called — blocked before download
        cdp.send.assert_not_called()
