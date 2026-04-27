"""Browserbase remote browser provider (T4.9).

Connects to a Browserbase (or compatible) remote browser endpoint via
Playwright's ``connect_over_cdp()`` API. The provider is intentionally
thin — it does not know about Browserbase internals; it just dials the
WebSocket endpoint the operator configures.

To plug in a different remote provider (e.g. Browserless, Steel, your
own cloud), set ``remote_endpoint`` to its CDP-over-WebSocket URL and
optionally set ``remote_provider`` for audit identification.

Playwright is an optional dependency (``arcagent[browser]``). Lazy
import follows the same pattern as LocalPlaywrightProvider.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.browser.errors import BrowserNotAvailableError, RemoteProviderError

_logger = logging.getLogger("arcagent.modules.browser.providers.browserbase")


class BrowserbaseProvider:
    """Connects to a remote Browserbase endpoint via Playwright CDP.

    Responsibilities:
    - Lazy-import Playwright (optional dep)
    - Dial the remote endpoint using ``playwright.chromium.connect_over_cdp()``
    - Expose the raw ``Browser`` to callers
    - Clean disconnect on shutdown

    The provider name is captured for audit logs only; it has no effect
    on the connection protocol.

    Usage::

        provider = BrowserbaseProvider(
            endpoint="wss://connect.browserbase.com?apiKey=...",
            provider_name="browserbase",
        )
        browser = await provider.connect()
        # ... use browser ...
        await provider.disconnect()
    """

    def __init__(
        self,
        endpoint: str,
        provider_name: str = "browserbase",
    ) -> None:
        """Initialise without connecting.

        Args:
            endpoint:      WebSocket URL for the remote browser endpoint.
            provider_name: Human-readable name used in logs and audit events.
        """
        if not endpoint:
            raise RemoteProviderError(
                provider=provider_name,
                message="remote_endpoint must not be empty",
            )
        self._endpoint = endpoint
        self._provider_name = provider_name
        self._playwright: Any = None
        self._browser: Any = None

    @property
    def provider_name(self) -> str:
        """Name of the remote provider (for audit events)."""
        return self._provider_name

    @property
    def browser(self) -> Any:
        """The connected Playwright Browser instance, or None."""
        return self._browser

    async def connect(self) -> Any:
        """Connect to the remote browser endpoint.

        Returns:
            The Playwright ``Browser`` instance.

        Raises:
            BrowserNotAvailableError: If Playwright is not installed.
            RemoteProviderError: If the connection to the remote endpoint fails.
        """
        try:
            from playwright.async_api import (  # type: ignore[import-not-found]  # optional dep
                async_playwright,
            )
        except ImportError as exc:
            raise BrowserNotAvailableError(
                message=(
                    "Playwright is not installed. "
                    "Install arcagent[browser] to enable remote browser automation."
                ),
                details={"install_hint": "pip install 'arcagent[browser]'"},
            ) from exc

        _logger.info(
            "Connecting to remote browser provider '%s' at %s",
            self._provider_name,
            self._endpoint,
        )
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(self._endpoint)
        except Exception as exc:
            await self._safe_stop_playwright()
            raise RemoteProviderError(
                provider=self._provider_name,
                message=f"Failed to connect to remote endpoint: {exc}",
                details={"endpoint": self._endpoint},
            ) from exc

        _logger.info("Connected to remote browser provider '%s'", self._provider_name)
        return self._browser

    async def disconnect(self) -> None:
        """Close the remote browser connection and stop Playwright.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                _logger.debug(
                    "Error closing remote browser '%s'", self._provider_name, exc_info=True
                )
            self._browser = None

        await self._safe_stop_playwright()
        _logger.info("Disconnected from remote browser provider '%s'", self._provider_name)

    async def _safe_stop_playwright(self) -> None:
        """Stop Playwright instance without raising."""
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                _logger.debug("Error stopping Playwright", exc_info=True)
            self._playwright = None
