"""Integration test: federal tier forces a remote browser on the LIVE path.

Covers PRD §Epic I3 AC2: "Sandbox mode `strict` forces a remote browser;
local headless disabled."

This exercises the real launch path — ``BrowserCapability.setup()`` — not
just the pure policy function. At federal tier with no remote CDP
endpoint configured, setup must raise ``LocalBrowserNotAllowedError``
*before* any Chrome process is launched (fail-closed). With a remote
``cdp_url`` set, setup proceeds and connects.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.browser import _runtime
from arcagent.modules.browser.capabilities import BrowserCapability
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import LocalBrowserNotAllowedError

_REMOTE = "ws://remote-browser.internal:9222/devtools/browser/abc"


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(config: BrowserConfig) -> None:
    bus = MagicMock()
    bus.emit = AsyncMock()
    _runtime.configure(config=config, bus=bus)


@pytest.mark.asyncio
class TestFederalTierLocalBrowserForbidden:
    """Federal tier + local config → setup raises, no Chrome launched."""

    async def test_federal_local_setup_raises_before_launch(self) -> None:
        _configure(BrowserConfig(tier="federal"))  # empty cdp_url → local launch

        with patch(
            "arcagent.modules.browser.capabilities.CDPClientManager"
        ) as mock_cdp_cls:
            with pytest.raises(LocalBrowserNotAllowedError) as exc_info:
                await BrowserCapability().setup(None)

            # Fail-closed: the CDP client must never be constructed/launched.
            mock_cdp_cls.assert_not_called()

        err = exc_info.value
        assert err.code == "BROWSER_LOCAL_NOT_ALLOWED"
        assert err.details["tier"] == "federal"
        assert _runtime.state().cdp_client is None

    async def test_federal_remote_setup_connects(self) -> None:
        _configure(BrowserConfig(tier="federal", connection={"cdp_url": _REMOTE}))  # type: ignore[arg-type]

        with patch(
            "arcagent.modules.browser.capabilities.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.url = _REMOTE
            mock_cdp_cls.return_value = mock_cdp

            await BrowserCapability().setup(None)

            mock_cdp.connect.assert_awaited_once()
            assert _runtime.state().cdp_client is mock_cdp


@pytest.mark.asyncio
class TestNonFederalLocalBrowserAllowed:
    """Non-federal tiers may launch a local headless browser."""

    async def test_personal_local_setup_connects(self) -> None:
        _configure(BrowserConfig(tier="personal"))

        with patch(
            "arcagent.modules.browser.capabilities.CDPClientManager"
        ) as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/local"
            mock_cdp_cls.return_value = mock_cdp

            await BrowserCapability().setup(None)

            mock_cdp.connect.assert_awaited_once()
            assert _runtime.state().cdp_client is mock_cdp
