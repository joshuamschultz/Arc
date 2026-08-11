"""Shared deterministic network policy fixtures for web module tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from arcagent.utils.url_security import set_url_resolver


@pytest.fixture(autouse=True)
def _public_test_dns() -> Iterator[None]:
    """Keep mocked-provider tests independent of ambient DNS access."""
    set_url_resolver(lambda _hostname: ("93.184.216.34",))
    yield
    set_url_resolver(None)
