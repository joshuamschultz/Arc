"""Shared fixtures for browser module tests."""

from __future__ import annotations

import pytest

from arcagent.modules.browser.config import (
    BrowserConfig,
    BrowserConnectionConfig,
)


@pytest.fixture
def browser_config() -> BrowserConfig:
    """Default browser config for tests."""
    return BrowserConfig()


@pytest.fixture
def connection_config() -> BrowserConnectionConfig:
    """Default connection config for tests."""
    return BrowserConnectionConfig()
