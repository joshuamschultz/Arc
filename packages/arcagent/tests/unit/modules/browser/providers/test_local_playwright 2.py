"""Tests for LocalPlaywrightProvider — mocked Playwright, headless flag.

No real browser is launched. playwright.async_api is fully mocked via
sys.modules patching since the provider uses lazy imports.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.browser.errors import BrowserNotAvailable
from arcagent.modules.browser.providers.local_playwright import LocalPlaywrightProvider


def _make_playwright_mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build Playwright mock objects.

    Returns:
        (async_playwright_fn, playwright_instance, browser)
    """
    browser = MagicMock()
    browser.close = AsyncMock()

    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)

    playwright_instance = MagicMock()
    playwright_instance.chromium = chromium
    playwright_instance.stop = AsyncMock()

    # async_playwright() is a callable that returns a context manager
    # We simulate the .start() call pattern
    pw_ctx = MagicMock()
    pw_ctx.start = AsyncMock(return_value=playwright_instance)

    async_playwright_fn = MagicMock(return_value=pw_ctx)
    return async_playwright_fn, playwright_instance, browser


def _make_playwright_module(async_playwright_fn: MagicMock) -> ModuleType:
    """Create a fake playwright.async_api module."""
    mod = ModuleType("playwright.async_api")
    mod.async_playwright = async_playwright_fn  # type: ignore[attr-defined]
    return mod


class TestLocalPlaywrightProviderConnect:
    """connect() launches Playwright with the correct headless flag."""

    async def test_connect_launches_headless_by_default(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = LocalPlaywrightProvider(headless=True)
            result = await provider.connect()

        pw_inst.chromium.launch.assert_called_once_with(headless=True)
        assert result is browser

    async def test_connect_launches_headed_when_requested(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = LocalPlaywrightProvider(headless=False)
            await provider.connect()

        pw_inst.chromium.launch.assert_called_once_with(headless=False)

    async def test_browser_property_returns_launched_browser(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = LocalPlaywrightProvider()
            await provider.connect()

        assert provider.browser is browser

    async def test_raises_browser_not_available_when_playwright_missing(self) -> None:
        """ImportError from missing playwright → BrowserNotAvailable."""
        provider = LocalPlaywrightProvider()

        # Remove playwright from sys.modules to simulate missing install
        with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            with pytest.raises(BrowserNotAvailable) as exc_info:
                await provider.connect()

        assert "playwright" in exc_info.value.message.lower()


class TestLocalPlaywrightProviderDisconnect:
    """disconnect() closes browser and stops Playwright cleanly."""

    async def test_disconnect_closes_browser_and_stops_playwright(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = LocalPlaywrightProvider()
            await provider.connect()
            await provider.disconnect()

        browser.close.assert_called_once()
        pw_inst.stop.assert_called_once()

    async def test_disconnect_idempotent(self) -> None:
        """disconnect() called without connect() is safe."""
        provider = LocalPlaywrightProvider()
        await provider.disconnect()  # must not raise

    async def test_disconnect_clears_references(self) -> None:
        async_pw, pw_inst, browser = _make_playwright_mocks()
        fake_mod = _make_playwright_module(async_pw)

        with patch.dict(sys.modules, {"playwright.async_api": fake_mod}):
            provider = LocalPlaywrightProvider()
            await provider.connect()
            await provider.disconnect()

        assert provider.browser is None
