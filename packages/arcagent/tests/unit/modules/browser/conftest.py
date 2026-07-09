"""Shared fixtures for browser module tests.

The live browser surface is the ``@capability`` class + module-level
``@tool`` functions in :mod:`arcagent.modules.browser.capabilities`.
They read shared state from :mod:`arcagent.modules.browser._runtime`, so
behavioural tests configure that state with a mock CDP client / AX
manager and then call the tool function directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser import _runtime
from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.config import (
    BrowserConfig,
    BrowserConnectionConfig,
)


@pytest.fixture(autouse=True)
def _reset_browser_runtime() -> Iterator[None]:
    """Clear shared runtime state around every browser test."""
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def browser_config() -> BrowserConfig:
    """Default browser config for tests."""
    return BrowserConfig()


@pytest.fixture
def connection_config() -> BrowserConnectionConfig:
    """Default connection config for tests."""
    return BrowserConnectionConfig()


def make_bus() -> MagicMock:
    """A MagicMock bus whose ``emit`` is awaitable."""
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture
def configure_browser() -> Callable[..., _State]:
    """Return a factory that binds browser runtime state for live @tool tests.

    Usage::

        st = configure_browser(cdp=mock_cdp, ax=mock_ax)
        result = await browser_navigate("https://example.com")
    """

    def _configure(
        config: BrowserConfig | None = None,
        *,
        cdp: Any = None,
        ax: Any = None,
        bus: Any = None,
    ) -> _State:
        _runtime.configure(
            config=config or BrowserConfig(),
            bus=bus if bus is not None else make_bus(),
        )
        st = _runtime.state()
        if cdp is not None:
            st.cdp_client = cdp
        if ax is not None:
            st.ax_manager = ax
        return st

    return _configure
