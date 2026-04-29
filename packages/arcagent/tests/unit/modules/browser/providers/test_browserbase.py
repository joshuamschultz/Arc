"""Tests for BrowserbaseProvider — mocked remote browser endpoint.

No real network connection. playwright.async_api is fully mocked via
sys.modules patching since the provider uses lazy imports.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.browser.errors import BrowserNotAvailableError, RemoteProviderError
from arcagent.modules.browser.providers.browserbase import BrowserbaseProvider

_ENDPOINT = "wss://connect.browserbase.com?apiKey=test-key"


def _make_playwright_mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build Playwright mock objects for CDP remote connect."""
    browser = MagicMock()
    browser.close = AsyncMock()

    chromium = MagicMock()
    chromium.connect_over_cdp = AsyncMock(return_value=browser)

    playwright_instance = MagicMock()
    playwright_instance.chromium = chromium
    playwright_instance.stop = AsyncMock()

    pw_ctx = MagicMock()
    pw_ctx.start = AsyncMock(return_value=playwright_instance)

    async_playwright_fn = MagicMock(return_value=pw_ctx)
    return async_playwright_fn, playwright_instance, browser


def _make_playwright_module(async_playwright_fn: MagicMock) -> ModuleType:
    """Create a fake playwright.async_api module."""
    mod = ModuleType("playwright.async_api")
    mod.async_playwright = async_playwright_fn  # type: ignore[attr-defined]
    return mod


class TestBrowserbaseProviderInit:
    def test_empty_endpoint_raises_remote_provider_error(self) -> None:
        with pytest.raises(RemoteProviderError) as exc_info:
            BrowserbaseProvider(endpoint="")
        assert "endpoint" in exc_info.value.message.lower()

    def test_provider_name_defaults_to_browserbase(self) -> None:
        p = BrowserbaseProvider(endpoint=_ENDPOINT)
        assert p.provider_name == "browserbase"

    def test_custom_provider_name(self) -> None:
        p = BrowserbaseProvider(endpoint=_ENDPOINT, provider_name="custom")
        assert p.provider_name == "custom"


class TestBrowserbaseProviderConnect:
    """connect() dials the remote endpoint via connect_over_cdp."""

    async def test_connect_calls_connect_over_cdp_with_endpoint(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = BrowserbaseProvider(endpoint=_ENDPOINT)
            result = await provider.connect()

        pw_inst.chromium.connect_over_cdp.assert_called_once_with(_ENDPOINT)
        assert result is browser

    async def test_browser_property_after_connect(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = BrowserbaseProvider(endpoint=_ENDPOINT)
            await provider.connect()

        assert provider.browser is browser

    async def test_raises_browser_not_available_when_playwright_missing(self) -> None:
        provider = BrowserbaseProvider(endpoint=_ENDPOINT)
        with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            with pytest.raises(BrowserNotAvailableError):
                await provider.connect()

    async def test_raises_remote_provider_error_on_connection_failure(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        pw_inst.chromium.connect_over_cdp.side_effect = Exception("WebSocket error")
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = BrowserbaseProvider(endpoint=_ENDPOINT)
            with pytest.raises(RemoteProviderError) as exc_info:
                await provider.connect()

        assert exc_info.value.details["provider"] == "browserbase"
        assert "endpoint" in exc_info.value.details


class TestBrowserbaseProviderDisconnect:
    """disconnect() closes browser and stops Playwright cleanly."""

    async def test_disconnect_closes_browser_and_stops_playwright(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = BrowserbaseProvider(endpoint=_ENDPOINT)
            await provider.connect()
            await provider.disconnect()

        browser.close.assert_called_once()
        pw_inst.stop.assert_called_once()

    async def test_disconnect_without_connect_is_safe(self) -> None:
        provider = BrowserbaseProvider(endpoint=_ENDPOINT)
        await provider.disconnect()  # must not raise

    async def test_disconnect_clears_references(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = BrowserbaseProvider(endpoint=_ENDPOINT)
            await provider.connect()
            await provider.disconnect()

        assert provider.browser is None
